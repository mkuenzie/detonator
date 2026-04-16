"""FastAPI host orchestrator.

Exposes the REST API documented in SPEC.md.

Interactive API documentation is served automatically by FastAPI at ``/docs``
(Swagger UI) and ``/redoc`` (ReDoc).
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
from detonator.enrichment.pipeline import EnrichmentPipeline
from detonator.logging import setup_logging
from detonator.models import EgressType, RunConfig
from detonator.orchestrator.runner import Runner
from detonator.orchestrator.state import AppState
from detonator.providers.egress.base import EgressProvider
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
    agent: str | None = None
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


async def build_egress_provider(
    egress_type: EgressType, config: DetonatorConfig
) -> EgressProvider | None:
    """Instantiate and configure the egress provider for the given egress type.

    Returns ``None`` when no matching egress config exists (preflight is skipped).
    New provider types slot in here without touching callers.
    """
    egress_name = egress_type.value  # "direct", "vpn", "tether"
    egress_cfg = config.egress.get(egress_name)
    if egress_cfg is None:
        logger.warning("No egress config found for type %r — skipping egress setup", egress_name)
        return None

    provider_type = egress_cfg.type.lower()
    if provider_type == "direct":
        from detonator.providers.egress.direct import DirectEgressProvider

        provider: EgressProvider = DirectEgressProvider()
    else:
        logger.warning("Unknown egress provider type %r — skipping egress setup", provider_type)
        return None

    await provider.configure(egress_cfg.settings)
    return provider


def create_app(
    config: DetonatorConfig,
    *,
    vm_provider: VMProvider | None = None,
    database: Database | None = None,
    artifact_store: ArtifactStore | None = None,
    enrichment_pipeline: EnrichmentPipeline | None = None,
) -> FastAPI:
    """Build a FastAPI app. All deps are optional for testability."""
    vm_provider = vm_provider or build_vm_provider(config)
    database = database or Database(config.storage.db_path)
    artifact_store = artifact_store or ArtifactStore(config.storage.data_dir)
    enrichment_pipeline = enrichment_pipeline or EnrichmentPipeline.build_from_config(
        config, database, artifact_store
    )

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
            enrichment_pipeline=enrichment_pipeline,
        )
        try:
            yield
        finally:
            await app.state.deps.shutdown()

    app = FastAPI(title="Detonator Orchestrator", version="0.1.0", lifespan=lifespan)
    _register_routes(app)
    try:
        from detonator.ui import mount_ui

        mount_ui(app)
    except ImportError:
        # UI is an optional extra — running without jinja2 is fine.
        logger.info("UI disabled (jinja2 not installed)")
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

    @app.get("/config/agents", summary="List configured agents")
    async def list_agents_cfg(request: Request) -> list[dict]:
        """Return each configured agent along with its backing VM state
        (if the VM provider is reachable).  Active run IDs using this agent
        are included under ``active_run_ids`` for live correlation."""
        deps = _deps(request)
        active_by_vm: dict[str, list[str]] = {}
        for run_id, runner in list(deps._runners.items()):
            vm_id = runner.agent.vm_id
            active_by_vm.setdefault(vm_id, []).append(str(run_id))

        out: list[dict] = []
        for agent in deps.config.agents:
            entry = agent.model_dump()
            entry["vm_state"] = None
            entry["active_run_ids"] = active_by_vm.get(agent.vm_id, [])
            try:
                state = await deps.vm_provider.get_state(agent.vm_id)
                entry["vm_state"] = state.value
            except Exception as exc:  # VM provider may be unreachable in dev
                logger.debug("get_state for agent %s failed: %s", agent.name, exc)
            out.append(entry)
        return out

    # ── Runs ─────────────────────────────────────────────────────

    @app.post("/runs", response_model=CreateRunResponse)
    async def create_run(body: CreateRunBody, request: Request) -> CreateRunResponse:
        deps = _deps(request)
        try:
            agent = (
                deps.config.get_agent(body.agent) if body.agent else deps.config.default_agent()
            )
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

        run_config = RunConfig(
            url=body.url,
            egress=body.egress,
            timeout_sec=body.timeout_sec,
            interactive=body.interactive,
            vm_id=body.vm_id,
            snapshot_id=body.snapshot_id,
            screenshot_interval_sec=body.screenshot_interval_sec,
        )
        egress_provider = await build_egress_provider(body.egress, deps.config)
        runner = Runner(
            config=deps.config,
            agent=agent,
            vm_provider=deps.vm_provider,
            database=deps.database,
            artifact_store=deps.artifact_store,
            run_config=run_config,
            run_id=uuid4(),
            egress_provider=egress_provider,
            enrichment_pipeline=deps.enrichment_pipeline,
        )
        task = asyncio.create_task(runner.execute())
        deps.register(runner, task)
        return CreateRunResponse(run_id=runner.run_id, state="pending")

    @app.get("/runs", summary="List runs")
    async def list_runs(
        request: Request,
        status: str | None = Query(None, description="Filter by exact status (e.g. complete, error)"),
        domain: str | None = Query(None, description="Substring match against seed URL (e.g. evil.com)"),
        date_from: str | None = Query(None, description="ISO-8601 lower bound on created_at (inclusive)"),
        date_to: str | None = Query(None, description="ISO-8601 upper bound on created_at (inclusive)"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[dict]:
        """List detonation runs with optional filters."""
        deps = _deps(request)
        return await deps.database.list_runs(
            status=status,
            domain=domain,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )

    @app.get("/runs/{run_id}", summary="Get run detail")
    async def get_run(run_id: UUID, request: Request) -> dict:
        """Return run detail including artifact manifest and, when the run is
        interactive-paused and actively managed, a ``console_url`` for
        VNC/SPICE access."""
        deps = _deps(request)
        row = await deps.database.get_run(str(run_id))
        if not row:
            raise HTTPException(404, f"Run {run_id} not found")
        row["artifacts"] = await deps.database.get_artifacts(str(run_id))

        # Surface VNC/SPICE console URL while the run is in interactive mode.
        if row.get("status") == "interactive":
            runner = deps.get_runner(run_id)
            if runner is not None:
                vm_id = runner.record.config.vm_id or runner.agent.vm_id
                try:
                    row["console_url"] = await deps.vm_provider.get_console_url(vm_id)
                except Exception as exc:
                    logger.warning("Could not get console URL for vm=%s: %s", vm_id, exc)

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

    @app.get("/domain/{domain}/runs", summary="Cross-run domain correlation")
    async def get_runs_by_domain(
        domain: str,
        request: Request,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict]:
        """Return all runs that touched *domain*, either as the seed URL or as
        an enriched domain observable.  Useful for correlating runs that share
        infrastructure across campaigns."""
        deps = _deps(request)
        return await deps.database.find_runs_by_domain(domain, limit=limit)

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
    json_logs = "--json-logs" in sys.argv
    setup_logging(level=config.log_level, json_logs=json_logs)
    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
