# Detonator — Agent Guidance

Interactive URL detonation framework for a home malware/phishing analysis lab. This file orients future Claude sessions. For the phase tracker, see [SPEC.md](SPEC.md). For user-facing setup, see [README.md](README.md).

## What this project is

- Analyst submits a URL → it detonates in a sandboxed VM → artifacts (HAR, response bodies, navigations, screenshots, DOM, console) are captured → the host enriches them (WHOIS/DNS/TLS/favicon/hosting/TLD) → navigations and the initiator graph are used to classify scope vs noise → Sigma rules match techniques → results are stored and queryable.
- One VM, stateless, reverted per run. The host owns routing and isolation. All analysis runs outside the VM against dumped artifacts.
- Home lab scope: single operator, trusted host network, no auth on the host API. Engineering is done to production standards so the design can scale.

## Non-negotiable design principles

- **Technology-agnostic at every integration boundary.** Hypervisor, browser engine, egress method, enrichment source, analysis module — all behind an ABC. New implementations slot in without touching callers. Never shortcut this by coupling a caller to a concrete provider.
- **Campaigns are the primary analytical entity, not runs.** Runs are evidence-collection events. The analyst thinks in terms of sites/campaigns. API surfaces campaigns as first-class. See the three-tier data model below.
- **Navigations are first-class evidence.** Top-level page transitions (`main` frame and sub-frame navigations) are captured by the agent into `navigations.json` and drive scope/noise classification. The HAR's `_initiator` graph is a secondary input — it fills in non-navigation transitive edges — but the navigation timeline is the authoritative account of "where the browser actually went."
- **No MITM.** No mitmproxy, no TLS interception. Chromium is the only supported browser because (a) its CDP surface gives us authoritative response-body capture via `Network.getResponseBody` and (b) the HAR `_initiator` field is richest there.
- **Headed browser only.** Interactive takeover (VNC/SPICE) requires a real desktop session.
- **Stateless VM.** Every run starts from a clean snapshot. Any malware executing during detonation is destroyed on revert. Never add persistent state to the guest.
- **Isolation enforced at the network layer, not in the agent.** The agent has no auth. Host nftables rules guarantee the agent's port is only reachable from the orchestrator on the isolated bridge.
- **UI and API are peer consumers of the repository.** Both `detonator/ui/routes.py` and `detonator/orchestrator/api.py` call `deps.database.*` directly — neither makes HTTP calls to the other. Enforced rules: (1) any data shape the UI can render must be reachable via an equivalent HTTP endpoint, so the API stays canonically complete; (2) raw SQL lives only in `detonator/storage/database.py` — never in route handlers.

## Architecture at a glance

```
Analyst → Host Orchestrator (FastAPI)
            ├── REST API  (JSON, at /)
            ├── Web UI    (Jinja2 + HTMX, at /ui/)
            ├── VMProvider        (Proxmox)
            ├── EgressProvider    (direct / tether; VPN deferred)
            ├── AgentManager      (HTTP client for the in-VM agent)
            ├── Enrichment pipeline
            │     ├── core/   (always run: navigations, dom)
            │     └── plugins/ (opt-in: whois, dns, tls, favicon, tld, hosting)
            ├── Analysis pipeline (Sigma rules → TechniqueHit)
            ├── Navigation-scope extractor + noise filter
            └── Storage (SQLite metadata + filesystem CAS)

In-VM Agent (FastAPI + Playwright Chromium, headed)
            ├── REST API: /health, /detonate, /status, /resume, /artifacts[/name]
            └── Capture subsystems (all write into bodies/ + manifest.jsonl):
                  ├── Playwright HAR (record_har_content="attach")
                  ├── NetworkCapture          (request bodies via context.on)
                  ├── CDPResponseTap          (response bodies via Network.getResponseBody)
                  └── RouteDocumentInterceptor (main-frame document bodies)
```

## Capture & ingestion (important detail)

The agent runs **four** capture subsystems in parallel, all landing in `bodies/`:

1. **Playwright HAR attach mode** writes `har_full.har` with per-entry `_file` refs; Playwright names body files by **SHA-1** of the body.
2. **`NetworkCapture`** ([agent/browser/network_capture.py](agent/browser/network_capture.py)) handles request bodies via `context.on("request")` and is the sink for the CDP tap; body files are named by **SHA-256**. Writes one JSONL line per capture event to `bodies/manifest.jsonl`.
3. **`CDPResponseTap`** ([agent/browser/cdp_response_tap.py](agent/browser/cdp_response_tap.py)) attaches a CDP session per page and pulls response bodies inside `loadingFinished` — closes the disposal race that makes `response.body()` miss main-frame docs on fast-redirecting pages. Feeds into `NetworkCapture` as a sink.
4. **`RouteDocumentInterceptor`** ([agent/browser/route_document_interceptor.py](agent/browser/route_document_interceptor.py)) catches main-frame document responses via route interception (subresources fall back to the CDP tap).

Host ingestion in `Runner._collect_artifacts()` unions two body-ref sources **by basename**: `map_body_files(har_full.har)` (SHA-1 keys) + `load_capture_manifest(bodies/manifest.jsonl)` (SHA-256 keys). Because the two naming schemes don't collide, identical bodies captured by both paths currently produce **two artifact rows pointing at the same CAS blob**. This is a known duplication bug; the design direction is to make the agent's capture path the single source of truth and drop Playwright's body attachments. See the "known issues" note in SPEC.md.

## Three-tier data model

The SQLite schema is designed so rows map directly to Neo4j nodes/edges when the graph migration happens post-v1. Do not collapse these layers.

- **Observables** — raw atomic indicators pulled from artifacts (domain, IP, URL, favicon hash, email, phone, TLS fingerprint, crypto wallet, registrant, ASN). Future graph nodes.
- **Techniques** — behavioral patterns/signatures produced by the analysis pipeline. A `technique_matches` row links a technique to the run + evidence that triggered it. Future graph nodes.
- **Campaigns** — groupings of related runs/sites representing a single threat operation. The primary analytical entity. Campaigns reference observables and techniques.

Relationship tables (`observable_links`, `run_observables`, `campaign_observables`, `campaign_techniques`, `campaign_runs`) are the future graph edges. Keep them typed and evidenced.

## Technology selections

| Boundary | Abstraction | First implementation |
|---|---|---|
| Hypervisor | `VMProvider` | Proxmox (`proxmoxer`) |
| Browser | `BrowserModule` | Playwright Chromium, headed |
| Egress — direct | `EgressProvider` | Linux bridge + nftables |
| Egress — tether | `EgressProvider` | USB RNDIS via ipheth |
| Egress — VPN | `EgressProvider` | Deferred (WireGuard planned) |
| Enrichment — core | `Enricher` | `NavigationEnricher`, `DomExtractor` |
| Enrichment — WHOIS | `Enricher` | `asyncwhois` / raw RDAP |
| Enrichment — DNS | `Enricher` | `dnspython` |
| Enrichment — TLS | `Enricher` | `cryptography` / `ssl` stdlib |
| Enrichment — Favicon | `Enricher` | `mmh3` + `hashlib` |
| Enrichment — TLD | `Enricher` | stdlib + IDN decode |
| Enrichment — Hosting | `Enricher` | Team Cymru IP→ASN via DNS |
| Analysis | `AnalysisModule` | `SigmaModule` (YAML rulepack) |
| Host API | — | FastAPI + uvicorn |
| Agent API | — | FastAPI |
| Web UI | — | Jinja2 + HTMX + vendored Pico CSS; cytoscape.js planned for graph |
| Storage | — | SQLite (`aiosqlite`) + content-addressed filesystem |
| Config | — | TOML (`tomllib`) + Pydantic |
| VM guest OS | — | **Windows 10/11** (not Linux) |

## Repo layout

```
detonator/
  pyproject.toml
  config.example.toml
  SPEC.md                              # Phase tracker
  CLAUDE.md                            # This file
  README.md                            # Setup guide
  scripts/
    backfill_artifact_captured_at.py   # One-off DB migration
  detonator/                           # Host orchestrator package
    config.py                          # TOML config loading + Pydantic models
    logging.py                         # JSON formatter + RunAdapter
    egress_ctl.py                      # CLI helper for egress provider control
    models/                            # Shared data models
      run.py                           # RunState, EgressType, ArtifactType, RunConfig, RunRecord
      observables.py                   # Observable, ObservableType, ObservableLink, RelationshipType
      vm.py                            # VMState, VMInfo, NetworkInfo
    orchestrator/
      api.py                           # FastAPI app factory + all REST routes
      runner.py                        # Run state machine
      state.py                         # AppState, in-flight run registry
      agent_manager.py                 # HTTP client for the in-VM agent
    providers/
      vm/                              # VMProvider ABC + ProxmoxProvider
      egress/                          # EgressProvider ABC + Direct + Tether (_routing.py shared)
    enrichment/
      base.py                          # Enricher ABC, RunContext, EnrichmentResult, observable_id()
      pipeline.py                      # EnrichmentPipeline, build_from_config()
      har.py                           # HAR → domains/IPs helper
      core/                            # Always-run enrichers (navigations, dom)
      plugins/                         # Opt-in enrichers (whois, dns, tls, favicon, tld, hosting)
    analysis/
      navigation.py                    # parse_har, build_initiator_graph, extract_navigation_scope
      filter.py                        # NoiseFilter (tracking domains, resource types, out-of-scope)
      har_body_map.py                  # map_body_files (HAR) + load_capture_manifest (JSONL)
      modules/                         # AnalysisModule ABC + SigmaModule + AnalysisPipeline
      rules/                           # YAML Sigma rulepack (flat dir, currently 1 rule)
    storage/
      database.py                      # SQLite schema + all queries (the ONLY place raw SQL lives)
      filesystem.py                    # ArtifactStore (CAS, symlinks, cleanup)
      manifest.py                      # build_manifest() consolidates run state → manifest.json
    ui/
      routes.py                        # UI + HTMX-partial handlers, mounted at /ui/
      templates/                       # Jinja2 pages + partials
      static/                          # Vendored htmx.min.js + pico.min.css + style.css
  agent/                               # In-VM agent (runs on the Windows sandbox)
    api.py                             # FastAPI REST API
    config.py                          # uvicorn entrypoint
    browser/
      base.py                          # BrowserModule ABC + DetonationRequest/Result + StealthProfile
      playwright_chromium.py           # Playwright Chromium implementation
      network_capture.py               # SHA-256 body sink + bodies/manifest.jsonl writer
      cdp_response_tap.py              # Per-page CDP Network listener for response bodies
      route_document_interceptor.py    # Main-frame document body capture via route()
      _driver.py                       # Playwright browser driver instantiation
      stealth.js                       # Fingerprint-hardening injected script
    README.md                          # Windows base-image setup
  tests/
```

Note: `detonator/services/` does not exist yet — see the "Where logic lives" section.

## Run lifecycle state machine

`RunState` ([detonator/models/run.py](detonator/models/run.py)):

`pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`

Every transition is persisted with timestamp + optional detail. Each stage has a configurable timeout (`timeouts.*_sec`). On failure at any stage, the runner transitions to `error` with partial artifacts and a partial `manifest.json` preserved — never discard what was already captured.

## Working conventions

- **Python 3.11+**, `from __future__ import annotations`, Pydantic models, `datetime.now(UTC)` (never `utcnow()`).
- **Async throughout** for I/O-bound work (provider calls, agent HTTP, enrichment/analysis fan-out).
- **Virtualenv at `.venv/`** at the repo root. Install with extras you need: `pip install -e ".[dev,proxmox,agent,enrichment,analysis,ui]"`. Required extras: `ui` (jinja2 + python-multipart) for `/ui/`; `analysis` (pyyaml) for Sigma rule evaluation.
- **Agents are configured, not defaulted.** `config.toml` declares one or more `[[agents]]` entries (name + vm_id + snapshot + port + health timeouts). The Runner takes an `AgentInstanceConfig` explicitly — do not re-introduce any `default_vm_id` / `default_snapshot` fallback path.
- **Tests use pytest + pytest-asyncio**. Proxmox tests mock at the module level (patch `detonator.providers.vm.proxmox.asyncio.to_thread`) — `AsyncMock` doesn't play nicely with `asyncio.to_thread` in 3.12+.
- **No secrets in code or tests.** Config loads from TOML; example at `config.example.toml`.
- **Structured JSON logging** with per-run context (`RunAdapter` attaches `run_id` automatically). Enabled via `--json-logs` on the CLI.

### Where logic lives

- **Repositories** ([detonator/storage/database.py](detonator/storage/database.py)) stay dumb: CRUD, typed queries, schema-shaped returns. No multi-step orchestration, no cross-store coordination. View-composing queries that join many read-only tables (e.g. `get_observable_detail`) are fine — they're still just reads. Callers handle shape-massaging like JSON-field parsing.
- **Route handlers** (UI and API) stay thin: validate inputs, call one or two repository methods, render/serialize. No raw SQL. No business logic beyond shape-massaging.
- **Services** (`detonator/services/`) do not exist yet. Introduce them when domain logic genuinely can't fit in a repository or a handler — e.g. multi-store transactions (see `delete_run`'s orphan blob reconciliation in [detonator/orchestrator/api.py](detonator/orchestrator/api.py)), graph traversals with cost/depth limits, or derived fields synthesized across runs. Grow services from real complexity, not symmetry — a service that only wraps one repo call is cost, not value.
- **Seams that will promote to services when next touched:** `delete_run` (orphan blob cleanup), observable/campaign detail composition once graph traversals land (multi-hop reachability, shared-infra clustering, campaign confidence from technique overlap). The `Runner` is already service-shaped — leave it where it is.
- **Trigger conditions for introducing `detonator/services/`:** first multi-hop graph traversal query; dual-store operations during the SQLite → Neo4j migration window; any second multi-step operation with the flavor of `delete_run`'s orphan reconciliation.

## Out of scope (don't build these)

- Active traffic manipulation (mitmproxy)
- Multi-browser (Firefox, WebKit)
- Headless mode
- Multi-VM concurrent runs
- Authentication on the host API
- Linux guest support (Windows first)
- Service-worker / shared-worker body capture — CDP child-session routing is not exposed by Playwright Python. See SPEC.md "Explicitly Out of Scope" for the full rationale.

## When starting new work

1. Read [SPEC.md](SPEC.md) for what's built, what's in flight, and what's deferred.
2. Check what ABCs and models already exist — don't redefine them.
3. Honor the technology-agnostic boundary: new implementations go behind an existing ABC, not in the caller.
4. Code is truth. When docs disagree with code, fix the docs.

## When completing work

1. Update [CLAUDE.md](CLAUDE.md), [SPEC.md](SPEC.md), and [README.md](README.md) to reflect the new state of the project before closing the task.
2. Update in-code docstrings that have drifted — especially at module level, where the story of "what this file is for" lives.
