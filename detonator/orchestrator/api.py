"""FastAPI host orchestrator.

Exposes the REST API documented in SPEC.md. Phase 2 lands the runs lifecycle
plus stubbed campaign / observable / technique / config endpoints that read
directly from the database.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from detonator.config import DetonatorConfig, load_config
from detonator.models import EgressType, RunConfig
from detonator.orchestrator.runner import Runner
from detonator.orchestrator.state import AppState
from detonator.providers.vm.base import VMProvider
from detonator.storage.database import Database
from detonator.storage.filesystem import ArtifactStore

logger = logging.getLogger(__name__)


# ── Request / response models ───────────────────────────────────


class CreateRunBody(BaseModel):
    url: str
    egress: EgressType = EgressType.DIRECT
    timeout_sec: int = 60
    interactive: bool = False
    vm_id: str | None = None
    snapshot_id: str | None = None
    screenshot_interval_sec: int | None = None


class CreateRunResponse(BaseModel):
    run_id: UUID
    state: str


class RunSummary(BaseModel):
    id: UUID
    seed_url: str
    status: str
    egress_type: str
    created_at: str
    completed_at: str | None = None
    error: str | None = None


class CreateCampaignBody(BaseModel):
    name: str
    description: str = ""


class UpdateCampaignBody(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    confidence: float | None = None


# ── App factory ──────────────────────────────────────────────────


def build_vm_provider(config: DetonatorConfig) -> VMProvider:
    """Instantiate the VM provider named in config.

    Extend this switch to support additional providers without touching
    the orchestrator wiring.
    """
    provider_type = config.vm_provider.type.lower()
    if provider_type == "proxmox":
        from detonator.providers.vm.proxmox import ProxmoxProvider

        return ProxmoxProvider()
    raise ValueError(f"Unknown VM provider type: {provider_type}")


def create_app(
    config: DetonatorConfig,
    *,
    vm_provider: VMProvider | None = None,
    database: Database | None = None,
    artifact_store: ArtifactStore | None = None,
) -> FastAPI:
    """Build a FastAPI app. All deps are optional for testability."""
    vm_provider = vm_provider or build_vm_provider(config)
    database = database or Database(config.storage.db_path)
    artifact_store = artifact_store or ArtifactStore(config.storage.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(config.storage.data_dir).mkdir(parents=True, exist_ok=True)
        Path(config.storage.db_path).parent.mkdir(parents=True, exist_ok=True)
        await database.connect()

        if vm_provider and config.vm_provider.settings:
            try:
                await vm_provider.configure(config.vm_provider.settings)
            except Exception:
                # Don't fail app startup if provider can't connect (tests, dev).
                logger.exception("VM provider configure failed — continuing")

        app.state.deps = AppState(
            config=config,
            vm_provider=vm_provider,
            database=database,
            artifact_store=artifact_store,
        )
        try:
            yield
        finally:
            await app.state.deps.shutdown()

    app = FastAPI(title="Detonator Orchestrator", version="0.1.0", lifespan=lifespan)
    _register_routes(app)
    return app


def _deps(request: Request) -> AppState:
    return request.app.state.deps


# ── Route registration ──────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:
    # ── System ───────────────────────────────────────────────────

    @app.get("/health")
    async def health(request: Request) -> dict:
        deps = _deps(request)
        return {
            "status": "ok",
            "vm_provider": deps.config.vm_provider.type,
            "active_runs": len(deps.active_run_ids()),
        }

    @app.get("/config/egress")
    async def list_egress(request: Request) -> dict:
        deps = _deps(request)
        return {
            name: cfg.model_dump()
            for name, cfg in deps.config.egress.items()
        }

    @app.get("/config/vms")
    async def list_vms_cfg(request: Request) -> list[dict]:
        deps = _deps(request)
        try:
            vms = await deps.vm_provider.list_vms()
            return [vm.model_dump() for vm in vms]
        except Exception as exc:
            raise HTTPException(503, f"VM provider unavailable: {exc}") from exc

    # ── Runs ─────────────────────────────────────────────────────

    @app.post("/runs", response_model=CreateRunResponse)
    async def create_run(body: CreateRunBody, request: Request) -> CreateRunResponse:
        deps = _deps(request)
        run_config = RunConfig(
            url=body.url,
            egress=body.egress,
            timeout_sec=body.timeout_sec,
            interactive=body.interactive,
            vm_id=body.vm_id,
            snapshot_id=body.snapshot_id,
            screenshot_interval_sec=body.screenshot_interval_sec,
        )
        runner = Runner(
            config=deps.config,
            vm_provider=deps.vm_provider,
            database=deps.database,
            artifact_store=deps.artifact_store,
            run_config=run_config,
            run_id=uuid4(),
        )
        task = asyncio.create_task(runner.execute())
        deps.register(runner, task)
        return CreateRunResponse(run_id=runner.run_id, state="pending")

    @app.get("/runs")
    async def list_runs(
        request: Request,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[dict]:
        deps = _deps(request)
        return await deps.database.list_runs(status=status, limit=limit, offset=offset)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        row = await deps.database.get_run(str(run_id))
        if not row:
            raise HTTPException(404, f"Run {run_id} not found")
        row["artifacts"] = await deps.database.get_artifacts(str(run_id))
        return row

    @app.get("/runs/{run_id}/artifacts/{artifact_name:path}")
    async def get_run_artifact(
        run_id: UUID, artifact_name: str, request: Request
    ) -> FileResponse:
        deps = _deps(request)
        path = deps.artifact_store.get_artifact_path(str(run_id), artifact_name)
        if path is None:
            raise HTTPException(404, f"Artifact not found: {artifact_name}")
        # Path traversal guard — get_artifact_path already checks is_relative_to.
        return FileResponse(path)

    @app.get("/runs/{run_id}/artifacts.zip")
    async def download_run_zip(run_id: UUID, request: Request) -> StreamingResponse:
        deps = _deps(request)
        row = await deps.database.get_run(str(run_id))
        if not row:
            raise HTTPException(404, f"Run {run_id} not found")
        run_dir = deps.artifact_store.run_dir(str(run_id))
        if not run_dir.exists():
            raise HTTPException(404, "No artifacts found for this run")

        domain = urlparse(row["seed_url"]).netloc or "unknown"
        # Sanitise for use as both an arcname and a filename.
        domain_safe = domain.replace(":", "_")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(run_dir.rglob("*")):
                if file_path.is_file():
                    arcname = Path(domain_safe) / file_path.relative_to(run_dir)
                    zf.write(file_path, arcname)
        buf.seek(0)

        filename = f"{domain_safe}_{str(run_id)[:8]}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        runner = deps.get_runner(run_id)
        if runner is None:
            raise HTTPException(404, f"No active run {run_id}")
        runner.signal_resume()
        return {"run_id": str(run_id), "resumed": True}

    @app.delete("/runs/{run_id}")
    async def delete_run(run_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        runner = deps.get_runner(run_id)
        if runner is not None:
            raise HTTPException(409, "Cannot delete an active run")
        removed = await deps.database.delete_run(str(run_id))
        if not removed:
            raise HTTPException(404, f"Run {run_id} not found")
        deps.artifact_store.delete_run(str(run_id))
        return {"run_id": str(run_id), "deleted": True}

    # ── Campaigns ────────────────────────────────────────────────

    @app.post("/campaigns")
    async def create_campaign(body: CreateCampaignBody, request: Request) -> dict:
        deps = _deps(request)
        campaign_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        await deps.database.insert_campaign(campaign_id, body.name, body.description, now)
        return {"id": campaign_id, "name": body.name, "description": body.description}

    @app.get("/campaigns")
    async def list_campaigns(request: Request) -> list[dict]:
        deps = _deps(request)
        cursor = await deps.database.db.execute(
            "SELECT * FROM campaigns ORDER BY last_seen DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    @app.get("/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        campaign = await deps.database.get_campaign(str(campaign_id))
        if not campaign:
            raise HTTPException(404, f"Campaign {campaign_id} not found")

        runs_cursor = await deps.database.db.execute(
            """SELECT r.* FROM runs r
               JOIN campaign_runs cr ON r.id = cr.run_id
               WHERE cr.campaign_id = ?""",
            (str(campaign_id),),
        )
        obs_cursor = await deps.database.db.execute(
            """SELECT o.*, co.role FROM observables o
               JOIN campaign_observables co ON o.id = co.observable_id
               WHERE co.campaign_id = ?""",
            (str(campaign_id),),
        )
        tech_cursor = await deps.database.db.execute(
            """SELECT t.* FROM techniques t
               JOIN campaign_techniques ct ON t.id = ct.technique_id
               WHERE ct.campaign_id = ?""",
            (str(campaign_id),),
        )
        return {
            **campaign,
            "runs": [dict(r) for r in await runs_cursor.fetchall()],
            "observables": [dict(r) for r in await obs_cursor.fetchall()],
            "techniques": [dict(r) for r in await tech_cursor.fetchall()],
        }

    @app.put("/campaigns/{campaign_id}")
    async def update_campaign(
        campaign_id: UUID, body: UpdateCampaignBody, request: Request
    ) -> dict:
        deps = _deps(request)
        existing = await deps.database.get_campaign(str(campaign_id))
        if not existing:
            raise HTTPException(404, f"Campaign {campaign_id} not found")

        updates: dict[str, Any] = {}
        for field in ("name", "description", "status", "confidence"):
            val = getattr(body, field)
            if val is not None:
                updates[field] = val
        if not updates:
            return existing

        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = (*updates.values(), str(campaign_id))
        await deps.database.db.execute(
            f"UPDATE campaigns SET {set_clause} WHERE id=?", params
        )
        await deps.database.db.commit()
        return await deps.database.get_campaign(str(campaign_id))

    @app.post("/campaigns/{campaign_id}/runs")
    async def add_run_to_campaign(
        campaign_id: UUID, body: dict, request: Request
    ) -> dict:
        deps = _deps(request)
        run_id = body.get("run_id")
        if not run_id:
            raise HTTPException(400, "run_id is required")
        await deps.database.link_campaign_run(str(campaign_id), run_id)
        return {"campaign_id": str(campaign_id), "run_id": run_id, "linked": True}

    # ── Observables & techniques ─────────────────────────────────

    @app.get("/observables")
    async def list_observables(
        request: Request,
        type: str | None = None,
        value: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict]:
        deps = _deps(request)
        return await deps.database.find_observables(
            obs_type=type,
            value_pattern=f"%{value}%" if value else None,
            limit=limit,
        )

    @app.get("/observables/{observable_id}")
    async def get_observable(observable_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        cursor = await deps.database.db.execute(
            "SELECT * FROM observables WHERE id=?", (str(observable_id),)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"Observable {observable_id} not found")
        obs = dict(row)

        runs_cursor = await deps.database.db.execute(
            """SELECT r.*, ro.source FROM runs r
               JOIN run_observables ro ON r.id = ro.run_id
               WHERE ro.observable_id = ?""",
            (str(observable_id),),
        )
        obs["runs"] = [dict(r) for r in await runs_cursor.fetchall()]
        return obs

    @app.get("/observables/{observable_id}/graph")
    async def get_observable_graph(observable_id: UUID, request: Request) -> dict:
        deps = _deps(request)
        return await deps.database.get_observable_graph(str(observable_id))

    @app.get("/techniques")
    async def list_techniques(request: Request) -> list[dict]:
        deps = _deps(request)
        cursor = await deps.database.db.execute("SELECT * FROM techniques")
        return [dict(row) for row in await cursor.fetchall()]

    @app.get("/techniques/{technique_id}/matches")
    async def get_technique_matches(technique_id: UUID, request: Request) -> list[dict]:
        deps = _deps(request)
        cursor = await deps.database.db.execute(
            """SELECT tm.*, r.seed_url, r.status FROM technique_matches tm
               JOIN runs r ON tm.run_id = r.id
               WHERE tm.technique_id = ?""",
            (str(technique_id),),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            if r.get("evidence_json"):
                try:
                    r["evidence"] = json.loads(r["evidence_json"])
                except Exception:
                    pass
        return rows


# ── Entrypoint ───────────────────────────────────────────────────


def main() -> None:
    """CLI entrypoint: `python -m detonator.orchestrator.api [config.toml]`."""
    import sys

    import uvicorn

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    config = load_config(config_path)
    logging.basicConfig(level=config.log_level)
    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
