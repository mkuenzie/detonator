"""UI route handlers — Jinja2-rendered pages + HTMX partials."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from detonator.models import EgressType, RunConfig
from detonator.orchestrator.state import AppState

logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(UI_DIR / "templates"))

# ── Jinja filters ─────────────────────────────────────────────────


def _fmt_datetime(value: Any) -> str:
    """Render an ISO-8601 string (or datetime) as 'YYYY-MM-DD HH:MM:SS' UTC."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _fmt_duration(start: Any, end: Any) -> str:
    """Return '12.3s' or '1m 23s' between two ISO-8601 strings."""
    def _parse(v):
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    s = _parse(start)
    e = _parse(end)
    if not s or not e:
        return "—"
    secs = (e - s).total_seconds()
    if secs < 60:
        return f"{secs:.1f}s"
    m, rem = divmod(int(secs), 60)
    return f"{m}m {rem}s"


def _fmt_bytes(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


TEMPLATES.env.filters["datetime"] = _fmt_datetime
TEMPLATES.env.filters["duration"] = _fmt_duration
TEMPLATES.env.filters["bytes"] = _fmt_bytes
TEMPLATES.env.filters["domain"] = _domain_of


# ── Helpers ───────────────────────────────────────────────────────


def _deps(request: Request) -> AppState:
    return request.app.state.deps


async def _load_agents(deps: AppState) -> list[dict]:
    """Same shape as /config/agents — used by UI to avoid duplicate logic."""
    active_by_vm: dict[str, list[str]] = {}
    for run_id, runner in list(deps._runners.items()):
        active_by_vm.setdefault(runner.agent.vm_id, []).append(str(run_id))

    out: list[dict] = []
    for agent in deps.config.agents:
        entry = agent.model_dump()
        entry["vm_state"] = None
        entry["active_run_ids"] = active_by_vm.get(agent.vm_id, [])
        try:
            state = await deps.vm_provider.get_state(agent.vm_id)
            entry["vm_state"] = state.value
        except Exception:
            pass
        out.append(entry)
    return out


# ── Mount ─────────────────────────────────────────────────────────


def mount_ui(app: FastAPI) -> None:
    """Attach UI static files + all UI routes onto an existing FastAPI app."""
    app.mount("/ui/static", StaticFiles(directory=str(UI_DIR / "static")), name="ui-static")
    _register_routes(app)


# ── Route registration ────────────────────────────────────────────


def _register_routes(app: FastAPI) -> None:

    # Dashboard ----------------------------------------------------

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False, response_class=HTMLResponse)
    async def dashboard(request: Request):
        deps = _deps(request)
        agents = await _load_agents(deps)
        vms, vm_err = await _load_vm_list(deps)
        recent_runs = await deps.database.list_runs(limit=5, offset=0)
        active_run_ids = [str(rid) for rid in deps.active_run_ids()]
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "agents": agents,
                "vms": vms,
                "vm_error": vm_err,
                "vm_provider_type": deps.config.vm_provider.type,
                "egress_options": list(deps.config.egress.keys()),
                "recent_runs": recent_runs,
                "active_run_ids": active_run_ids,
            },
        )

    # Config -------------------------------------------------------

    @app.get("/ui/config", include_in_schema=False, response_class=HTMLResponse)
    async def config_page(request: Request):
        deps = _deps(request)
        agents = await _load_agents(deps)
        egress = {
            name: cfg.model_dump() for name, cfg in deps.config.egress.items()
        }
        return TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "agents": agents,
                "vm_provider": deps.config.vm_provider.model_dump(),
                "egress": egress,
                "timeouts": deps.config.timeouts.model_dump(),
                "enrichment_modules": deps.config.enrichment_modules,
            },
        )

    # Run list -----------------------------------------------------

    @app.get("/ui/runs", include_in_schema=False, response_class=HTMLResponse)
    async def runs_page(
        request: Request,
        status: str | None = Query(None),
        domain: str | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        deps = _deps(request)
        runs = await deps.database.list_runs(
            status=status or None,
            domain=domain or None,
            date_from=date_from or None,
            date_to=date_to or None,
            limit=limit,
            offset=offset,
        )
        active_ids = {str(rid) for rid in deps.active_run_ids()}
        for r in runs:
            r["is_active"] = r["id"] in active_ids
        agents = await _load_agents(deps)
        return TEMPLATES.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": runs,
                "filters": {
                    "status": status or "",
                    "domain": domain or "",
                    "date_from": date_from or "",
                    "date_to": date_to or "",
                    "limit": limit,
                    "offset": offset,
                },
                "agent_names": [a["name"] for a in agents],
                "egress_options": list(deps.config.egress.keys()),
            },
        )

    @app.post("/ui/runs", include_in_schema=False)
    async def submit_run(
        request: Request,
        url: str = Form(...),
        agent: str = Form(...),
        egress: str = Form("direct"),
        interactive: bool = Form(False),
        timeout_sec: int = Form(60),
    ):
        deps = _deps(request)
        try:
            agent_cfg = deps.config.get_agent(agent)
        except KeyError:
            raise HTTPException(400, f"Unknown agent: {agent}")

        try:
            egress_type = EgressType(egress)
        except ValueError:
            raise HTTPException(400, f"Unknown egress: {egress}")

        # Lazy imports to avoid circular dependency at module load.
        import asyncio
        from uuid import uuid4

        from detonator.orchestrator.api import build_egress_provider
        from detonator.orchestrator.runner import Runner

        run_config = RunConfig(
            url=url,
            egress=egress_type,
            timeout_sec=timeout_sec,
            interactive=interactive,
        )
        egress_provider = await build_egress_provider(egress_type, deps.config)
        runner = Runner(
            config=deps.config,
            agent=agent_cfg,
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
        return RedirectResponse(f"/ui/runs/{runner.run_id}", status_code=303)

    # Run detail ---------------------------------------------------

    @app.get("/ui/runs/{run_id}", include_in_schema=False, response_class=HTMLResponse)
    async def run_detail(run_id: UUID, request: Request):
        deps = _deps(request)
        row = await deps.database.get_run(str(run_id))
        if not row:
            raise HTTPException(404, f"Run {run_id} not found")
        artifacts = await deps.database.get_artifacts(str(run_id))

        run_cfg = {}
        try:
            run_cfg = json.loads(row.get("config_json") or "{}")
        except Exception:
            pass

        transitions = _read_transitions(deps, str(run_id))
        console_url = None
        runner = deps.get_runner(run_id)
        if row.get("status") == "interactive" and runner is not None:
            vm_id = runner.record.config.vm_id or runner.agent.vm_id
            try:
                console_url = await deps.vm_provider.get_console_url(vm_id)
            except Exception as exc:
                logger.warning("console_url lookup failed: %s", exc)

        manifest = _read_manifest(deps, str(run_id))
        enrichment = _read_enrichment(deps, str(run_id))
        technique_matches = await deps.database.get_technique_matches_for_run(str(run_id))
        for tm in technique_matches:
            if tm.get("evidence_json"):
                try:
                    tm["evidence"] = json.loads(tm["evidence_json"])
                except Exception:
                    pass

        run_observables = await _load_run_observables(deps, str(run_id))

        is_terminal = row.get("status") in {"complete", "error"}

        return TEMPLATES.TemplateResponse(
            request,
            "run_detail.html",
            {
                "run": row,
                "run_cfg": run_cfg,
                "artifacts": artifacts,
                "transitions": transitions,
                "console_url": console_url,
                "manifest": manifest,
                "enrichment": enrichment,
                "technique_matches": technique_matches,
                "observables": run_observables,
                "is_active": runner is not None,
                "is_terminal": is_terminal,
            },
        )

    @app.post("/ui/runs/{run_id}/resume", include_in_schema=False)
    async def ui_resume(run_id: UUID, request: Request):
        deps = _deps(request)
        runner = deps.get_runner(run_id)
        if runner is None:
            raise HTTPException(404, f"No active run {run_id}")
        runner.signal_resume()
        return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)

    # HTMX partials ------------------------------------------------

    @app.get(
        "/ui/_partials/run-state/{run_id}",
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    async def partial_run_state(run_id: UUID, request: Request):
        deps = _deps(request)
        row = await deps.database.get_run(str(run_id))
        if not row:
            raise HTTPException(404)
        runner = deps.get_runner(run_id)
        transitions = _read_transitions(deps, str(run_id))
        is_terminal = row.get("status") in {"complete", "error"}
        return TEMPLATES.TemplateResponse(
            request,
            "_partials/run_state.html",
            {
                "run": row,
                "transitions": transitions,
                "is_active": runner is not None,
                "is_terminal": is_terminal,
            },
        )

    @app.get(
        "/ui/_partials/runs-table",
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    async def partial_runs_table(
        request: Request,
        status: str | None = Query(None),
        domain: str | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        deps = _deps(request)
        runs = await deps.database.list_runs(
            status=status or None,
            domain=domain or None,
            date_from=date_from or None,
            date_to=date_to or None,
            limit=limit,
            offset=offset,
        )
        active_ids = {str(rid) for rid in deps.active_run_ids()}
        for r in runs:
            r["is_active"] = r["id"] in active_ids
        return TEMPLATES.TemplateResponse(
            request,
            "_partials/runs_table.html",
            {"runs": runs},
        )

    @app.get(
        "/ui/_partials/agents",
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    async def partial_agents(request: Request):
        deps = _deps(request)
        agents = await _load_agents(deps)
        return TEMPLATES.TemplateResponse(
            request,
            "_partials/agents.html",
            {"agents": agents},
        )


# ── Artifact readers ──────────────────────────────────────────────


def _read_transitions(deps: AppState, run_id: str) -> list[dict]:
    """Read state transitions from the run's meta.json (written at the end)
    or from the active Runner record (for in-flight runs)."""
    runner = None
    try:
        runner = deps.get_runner(UUID(run_id))
    except Exception:
        pass
    if runner is not None:
        return [
            {
                "from_state": t.from_state.value,
                "to_state": t.to_state.value,
                "timestamp": t.timestamp.isoformat(),
                "detail": t.detail,
            }
            for t in runner.record.transitions
        ]
    meta_path = deps.artifact_store.run_dir(run_id) / "meta.json"
    if not meta_path.exists():
        return []
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        return meta.get("transitions", [])
    except Exception:
        return []


def _read_manifest(deps: AppState, run_id: str) -> dict | None:
    path = deps.artifact_store.run_dir(run_id) / "manifest.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _read_enrichment(deps: AppState, run_id: str) -> dict | None:
    path = deps.artifact_store.run_dir(run_id) / "enrichment.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


async def _load_run_observables(deps: AppState, run_id: str) -> list[dict]:
    """Return observables found during this run, joined with their type/value."""
    cursor = await deps.database.db.execute(
        """SELECT o.id, o.type, o.value, o.first_seen, o.last_seen, ro.source, ro.context_json
           FROM observables o
           JOIN run_observables ro ON o.id = ro.observable_id
           WHERE ro.run_id = ?
           ORDER BY o.type, o.value""",
        (run_id,),
    )
    rows = []
    for r in await cursor.fetchall():
        d = dict(r)
        ctx = json.loads(d.pop("context_json") or "{}")
        d["enricher"] = ctx.get("enricher") or d["source"]
        rows.append(d)
    return rows
