# Detonator — Spec & Phase Tracker

Living document tracking what's built, what's next, and what's deferred. Update as phases complete.

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | VM Provider Abstraction | Complete |
| 1 | In-VM Agent | Partial (code scaffolded, not yet run on a real VM) |
| 2 | Host Orchestrator | Complete (unit-tested; end-to-end smoke pending a real VM) |
| 3 | Egress & Isolation | Partial (direct egress complete; VPN/tether deferred) |
| 4 | Enrichment Pipeline | Complete |
| 5 | Chain Extraction & Filtering | Complete |
| 5b | Analysis Modularization | Complete |
| 6 | Manifest & Polish | Complete |
| 7 | Web UI | Complete (read-only dashboard + run submission; graph view deferred) |

---

## Phase 0 — VM Provider Abstraction (Complete)

- [x] `VMProvider` ABC ([detonator/providers/vm/base.py](detonator/providers/vm/base.py))
- [x] Data models: `VMState`, `VMInfo`, `NetworkInfo` ([detonator/models/vm.py](detonator/models/vm.py))
- [x] `ProxmoxProvider` implementation ([detonator/providers/vm/proxmox.py](detonator/providers/vm/proxmox.py))
- [x] Unit tests with mocked Proxmox API ([tests/test_proxmox_provider.py](tests/test_proxmox_provider.py))
- [x] Manual integration test against real Proxmox instance

---

## Phase 1 — In-VM Agent (Partial)

### Done
- [x] Agent REST API skeleton: `/health`, `/detonate`, `/status`, `/resume`, `/artifacts`, `/artifacts/{name}` ([agent/api.py](agent/api.py))
- [x] Browser automation ABC ([agent/browser/base.py](agent/browser/base.py))
- [x] Playwright Chromium module: HAR capture, screenshots, DOM dump, console collection, interactive pause/resume ([agent/browser/playwright_chromium.py](agent/browser/playwright_chromium.py))
- [x] Agent entrypoint / uvicorn launcher ([agent/config.py](agent/config.py))
- [x] Windows base image setup guide ([agent/README.md](agent/README.md))

### Remaining
- [x] Build and test a real Windows base VM image per the README
- [x] End-to-end smoke: hit `/detonate` against a hardcoded URL, verify HAR + screenshot + DOM + console are valid
- [ ] Tests for the agent API (FastAPI TestClient, mocked BrowserModule)
- [ ] Tests for the Playwright module (integration, run on demand, not in default pytest)
- [ ] Decide: Linux guest support (deferred — Windows first per user direction)

---

## Phase 2 — Host Orchestrator (Complete — pending real-VM smoke)

### Work items
- [x] FastAPI app skeleton ([detonator/orchestrator/api.py](detonator/orchestrator/api.py))
  - `create_app()` factory with injectable deps for testability
  - Lifespan context manages DB connect/disconnect and VM provider configure
  - `build_vm_provider()` switch — Proxmox wired; new providers slot in here
- [x] Shared app state / in-flight run registry ([detonator/orchestrator/state.py](detonator/orchestrator/state.py))
- [x] Run lifecycle state machine ([detonator/orchestrator/runner.py](detonator/orchestrator/runner.py))
  - States: `pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`
  - Every transition logged + persisted with timestamp + detail
  - Per-stage `asyncio.timeout` enforcement
  - Partial-artifact preservation on error (`_fail` runs before the run record is finalized)
  - `preflight`/`enriching`/`filtering` are stubs that transition through — real work lands in Phases 3/4/5
  - `meta.json` always dumped to the run dir at the end of `execute()`
- [x] Agent REST client ([detonator/orchestrator/agent_client.py](detonator/orchestrator/agent_client.py))
  - `wait_for_health` polls `/health` with retry until timeout
  - `detonate`, `status`, `resume`
  - `wait_for_terminal` with optional `pause_on_interactive`
  - `download_all` streams artifacts and preserves sub-paths (e.g. `screenshots/*.png`)
- [x] Wire full flow: VM revert → start → wait for agent → detonate → collect → force-stop
- [x] Persist artifacts via `ArtifactStore`
- [x] Persist run metadata, artifacts, transitions via `Database`
- [x] Load config via `load_config` ([detonator/config.py](detonator/config.py))

### API endpoints implemented

**Runs**
- [x] `POST /runs` — submit new run (launches `Runner.execute()` as a background task)
- [x] `GET /runs` — list with `status`, `limit`, `offset` filters
- [x] `GET /runs/{id}` — detail + artifact manifest (joins `artifacts` table)
- [x] `GET /runs/{id}/artifacts/{name:path}` — download one artifact (path-traversal guarded)
- [x] `POST /runs/{id}/resume` — signals the active runner's resume event
- [x] `DELETE /runs/{id}` — refuses while active; otherwise drops DB row + artifact dir

**Campaigns**
- [x] `POST /campaigns` — create
- [x] `GET /campaigns` — list
- [x] `GET /campaigns/{id}` — detail (linked runs, observables, techniques via joins)
- [x] `PUT /campaigns/{id}` — update name/description/status/confidence
- [x] `POST /campaigns/{id}/runs` — associate a run

**Observables & Techniques**
- [x] `GET /observables` — search/filter by type + value pattern
- [x] `GET /observables/{id}` — detail with joined `runs`
- [x] `GET /observables/{id}/graph` — neighborhood (outgoing, incoming, campaigns)
- [x] `GET /techniques` — list
- [x] `GET /techniques/{id}/matches` — matching runs (with `evidence_json` decoded)

**System**
- [x] `GET /config/egress` — available egress options from config
- [x] `GET /config/vms` — delegates to `VMProvider.list_vms()`; 503s if provider is down
- [x] `GET /config/agents` — configured agents with live VM state + active run IDs (added Phase 7)
- [x] `GET /health` — orchestrator health + active-run count

### Tests (54 total, all green)
- [x] `tests/test_agent_client.py` (7) — httpx MockTransport: health, retries, timeout, detonate payload, terminal polling, interactive pause, download_all
- [x] `tests/test_runner.py` (5) — StubVMProvider + FakeAgentClient: happy path, agent error, missing VM IP, missing vm_id/snapshot, interactive pause/resume
- [x] `tests/test_orchestrator_api.py` (10) — TestClient: health, config endpoints, run CRUD 404s, campaign round-trip, observables/techniques empty, run creation schedules the background task

### Verification
- [x] End-to-end smoke: submit a URL against a real Windows VM → full lifecycle completes → artifacts on disk → row in SQLite. 

### Known gaps / deferred
- `preflight` stage is a no-op transition. Phase 3 plugs `EgressProvider.preflight_check()` in here.
- `enriching` / `filtering` stages are no-op transitions. Phases 4 / 5 land the real work.
- No structured JSON logging yet — standard `logging` only. Cross-cutting concern, tracked below.
- No manifest consolidation — Phase 6.

---

## Phase 3 — Egress & Isolation (Partial — direct + tether complete, VPN deferred)

**Architecture decision:** The orchestrator VM acts as the L3 sandbox gateway.
Proxmox's only role is VM lifecycle; all routing, NAT, and firewall rules live in
the orchestrator's own kernel.  See [the Phase 3 design plan](.claude/plans/sprightly-singing-curry.md)
for the full topology and rationale.

### Done
- [x] `EgressProvider` ABC + `PreflightResult` ([detonator/providers/egress/base.py](detonator/providers/egress/base.py))
- [x] `DirectEgressProvider` ([detonator/providers/egress/direct.py](detonator/providers/egress/direct.py))
  - `activate()`: enables `net.ipv4.ip_forward` via sysctl; atomically loads an nftables table with MASQUERADE (postrouting) and forward chains (LAN isolation + sandbox → uplink accept)
  - `deactivate()`: idempotently deletes the nftables table; called in runner `finally` block
  - `preflight_check()`: hits IP-echo service to confirm public IP; returns `PreflightResult`
  - `get_public_ip()`: httpx GET to `api.ipify.org`
- [x] `config.example.toml` updated: egress settings are now orchestrator-local (`uplink_interface`, `sandbox_cidr`, `lan_cidr`); Proxmox bridge references removed
- [x] Runner integration ([detonator/orchestrator/runner.py](detonator/orchestrator/runner.py))
  - `_preflight()` calls `egress.activate()` then `egress.preflight_check()`; raises `RunnerError` on failed preflight
  - `_teardown_egress()` always called from `execute()` finally block (idempotent, non-fatal errors)
  - `egress_provider` is an optional constructor arg — runs without egress if not provided
- [x] `api.py`: `build_egress_provider()` maps `EgressType` → provider instance + configure; passed to each Runner at run creation
- [x] Unit tests — 12 tests in [tests/test_direct_egress.py](tests/test_direct_egress.py): configure, ruleset generation (with/without lan_cidr), activate command sequence, deactivate idempotency, preflight pass/fail, get_public_ip
- [x] `TetherEgressProvider` ([detonator/providers/egress/tether.py](detonator/providers/egress/tether.py))
  - Same nftables structure as `DirectEgressProvider`; separate table name `detonator-tether` so both providers can coexist
  - `preflight_check()` adds uplink IPv4-liveness check before calling ipify — fails fast with a clear message if Personal Hotspot is off
  - `get_public_ip()` binds the httpx connection to the tether interface IP via `AsyncHTTPTransport(local_address=...)` so the check measures the tether path, not the default route
  - `build_egress_provider()` in `api.py` updated with `elif provider_type == "tether":` branch
- [x] Unit tests — 13 tests in [tests/test_tether_egress.py](tests/test_tether_egress.py): configure, ruleset generation (table name, with/without lan_cidr), activate, deactivate idempotency, preflight pass/fail/no-IPv4, get_public_ip
- [x] `config.example.toml` updated: tether block uncommented with `enxea98eebb97c7`-style placeholder and 172.20.10.0/28 subnet note
- [x] [docs/tether-setup.md](docs/tether-setup.md): Proxmox USB passthrough (05ac:12a8), ipheth + usbmuxd prereqs, Trust This Computer pairing, systemd-networkd unit, verify-before-running checklist

### Remaining / deferred
- [ ] VPN egress provider (WireGuard tunnel steering)
- [ ] Pre-flight: LAN isolation probe (agent attempts to reach host-LAN IP, asserts failure)
- [ ] Pre-flight: DNS-path check (DNS queries exit via expected egress)
- [ ] Post-teardown verification (assert nftables table absent after run)
- [ ] Manual integration test: submit a run, confirm public IP matches, confirm LAN blocked from inside VM

**Security invariants enforced by nftables (direct egress):**
- `ip saddr <sandbox_cidr> ip daddr <lan_cidr> drop` — VM cannot reach host LAN
- `ip saddr <sandbox_cidr> oif <uplink> masquerade` — sandbox traffic NATed out uplink only
- `ip saddr <sandbox_cidr> drop` — all other sandbox forward attempts dropped
- Rules loaded atomically via `nft -f`; deleted idempotently on run exit

---

## Phase 4 — Enrichment Pipeline (Complete)

- [x] `Enricher` ABC, `RunContext`, `EnrichmentResult` ([detonator/enrichment/base.py](detonator/enrichment/base.py))
  - Extended `EnrichmentResult` with `observables` + `observable_links` fields
  - Added `observable_id(type, value)` — deterministic uuid5 for deduplication
- [x] WHOIS enricher ([detonator/enrichment/whois.py](detonator/enrichment/whois.py)) — `asyncwhois>=1.0`
  - Returns registrar, dates, name servers, registrant org
  - Creates REGISTRANT observable when org is present
- [x] DNS enricher ([detonator/enrichment/dns.py](detonator/enrichment/dns.py)) — `dnspython`
  - Queries A/AAAA/CNAME/MX/NS/TXT per domain
  - Creates IP observables linked to domain with `resolves_to`
- [x] TLS cert chain enricher ([detonator/enrichment/tls.py](detonator/enrichment/tls.py)) — `cryptography` / `ssl`
  - Connects port 443, extracts subject/issuer/SANs/fingerprint
  - Creates TLS_FINGERPRINT observable linked to domain with `issued_by`
- [x] Favicon hash enricher ([detonator/enrichment/favicon.py](detonator/enrichment/favicon.py)) — `mmh3` + `httpx`
  - Fetches `/favicon.ico` per unique origin
  - Shodan-style mmh3 hash + MD5; creates FAVICON_HASH observable with `serves_favicon` link
- [x] TLD analysis enricher ([detonator/enrichment/tld.py](detonator/enrichment/tld.py)) — stdlib only
  - TLD extraction, label count, subdomain depth, punycode/IDN detection, decoded display form
- [x] HAR extractor ([detonator/enrichment/har.py](detonator/enrichment/har.py))
  - Parses `har_full.har`; separates hostnames from IPs; populates `RunContext`
- [x] Pipeline runner ([detonator/enrichment/pipeline.py](detonator/enrichment/pipeline.py))
  - Checks available artifact types (har, dom) and fans out to accepting enrichers concurrently
  - `return_exceptions=True` — one failing enricher never aborts the rest
  - Deduplicates observables by deterministic uuid5 before DB upsert
  - Writes `enrichment.json` to the artifact dir alongside other artifacts
  - `EnrichmentPipeline.build_from_config(config, db, store)` factory reads `enrichment_modules`
- [x] DOM content extractor ([detonator/enrichment/dom.py](detonator/enrichment/dom.py))
  - Reads `dom.html` from artifact dir (stdlib html.parser + regex)
  - Extracts: emails, US phone numbers, BTC (legacy + bech32) and ETH wallets
  - Extracts: `<form action>` targets and `<meta http-equiv=refresh>` redirect URLs
  - All indicators stored as typed Observable rows
- [x] Store enrichment results to filesystem + SQLite
  - `enrichment.json` written to artifact dir; observables + links upserted to DB
  - Every observable linked back to the run via `run_observables` (source=enrichment)
- [x] Runner wired: `_enrich()` now calls `EnrichmentPipeline.run()` under `enrich_sec` timeout
- [x] API wired: `create_app()` builds pipeline from config; `create_run` passes it to each `Runner`
- [x] Tests: 19 tests in [tests/test_enrichment_pipeline.py](tests/test_enrichment_pipeline.py)
  - HAR extractor (domain/IP separation, missing/invalid file)
  - `observable_id` determinism and case-insensitivity
  - TLD enricher (basic structure, IDN detection, empty context)
  - DOM extractor (email, phone, crypto wallet, form action, meta refresh, missing file)
  - Pipeline end-to-end: enrichers run, `enrichment.json` written, DB called
  - Pipeline fault isolation: crashing enricher returns error result, others still complete
  - `build_from_config` wires known modules and skips unknown ones

---

## Phase 5 — Chain Extraction & Filtering (Complete)

- [x] HAR parser ([detonator/analysis/chain.py](detonator/analysis/chain.py))
  - `parse_har(path)` — parses entries, extracts `_initiator.type`/URL (redirect, parser, script via callFrames), `_resourceType`, `serverIPAddress`
  - `HarEntry` Pydantic model per entry
- [x] Initiator graph builder — `build_initiator_graph(entries)` returns forward adjacency map (parent → children)
- [x] Chain walk algorithm — `walk_chain(entries, seed_url)` BFS from seed, `_best_seed_url()` handles normalisation (trailing slash, fragment)
- [x] `extract_chain(path, seed_url) → ChainResult` — top-level: parse, walk, split into `chain_entries`/`noise_entries`, produce `har_chain` dict (chain entries only)
- [x] Noise classifier ([detonator/analysis/filter.py](detonator/analysis/filter.py)) — `NoiseFilter`
  - `REASON_NO_CHAIN` — not reachable from seed via initiator graph
  - `REASON_TRACKER` — domain in built-in tracking domain list (23 domains; Google Analytics, GTM, DoubleClick, Facebook, Hotjar, Segment, Intercom, Bing, Yandex, TikTok, LinkedIn)
  - `REASON_RESOURCE_TYPE` — `_resourceType` in `{ping, preflight, csp-violation-report, beacon}`
  - `noise_domains` / `noise_resource_types` config fields supplement (do not replace) built-ins
- [x] Output `har_chain.json` (clean chain only) + `filter_result.json` alongside artifacts; both registered in DB
- [x] Technique detection — `TechniqueDetector` with 8 named detectors:
  - Google Cloud Storage phishing host (`storage.googleapis.com`)
  - Cloudflare Workers abuse (`*.workers.dev`)
  - GitHub Pages phishing host (`*.github.io`)
  - Google Forms credential harvester (`docs.google.com/forms`)
  - Data URI payload (`data:` scheme)
  - Blob URI redirect (`blob:` scheme)
  - Microsoft SharePoint phishing host (`*.sharepoint.com`)
  - Cross-origin redirect chain (≥2 distinct netlocs in `redirect`-type chain)
  - Technique IDs are deterministic `uuid5` of the technique name — idempotent across runs
- [x] Runner wired: `_filter()` calls `extract_chain()` + `NoiseFilter.run()` under `filter_sec` timeout; persists hits via `database.upsert_technique` + `insert_technique_match`
- [x] Config: `[filter]` section with `noise_domains` + `noise_resource_types`; `timeouts.filter_sec = 30`
- [x] Tests: 31 tests in [tests/test_chain_filter.py](tests/test_chain_filter.py)
  - HAR parsing (entry count, initiator fields, script callFrames extraction, missing/invalid file)
  - Initiator graph edges, orphan absence
  - Chain walk: redirect follow, script initiator follow, seed URL normalisation, empty entries
  - `extract_chain`: chain/noise split, `har_chain` dict contents, missing file returns None
  - `NoiseFilter`: tracking domain, ping resource type, no-chain orphan, clean chain entries, counts, extra config domain, final HAR exclusions
  - `TechniqueDetector`: GCS, workers.dev, cross-origin redirect, no hits, deterministic IDs
  - JSON serialisation round-trip

---

## Phase 5b — Analysis Modularization (Complete)

- Status: Complete
- Trigger: aligns analysis with enrichment's modular shape; enables user-authored rules without code changes.
- Summary: `TechniqueDetector` removed from `filter.py`; replaced with `AnalysisPipeline` of `AnalysisModule`s. Two initial modules: `builtin` (pure Python, ports the original 8 detectors) + `sigma` (YAML rulepack, evaluated against `AnalysisContext` without a SIEM backend). Rules live in `detonator/analysis/rules/`.

### What was built

- [x] `detonator/analysis/modules/` package
  - `base.py` — `AnalysisContext`, `TechniqueHit` (with `detection_module` field), `AnalysisModule` ABC, `_tech_id` helper, `AnalysisContext.from_chain()` classmethod
  - `pipeline.py` — `AnalysisPipeline` with `asyncio.gather` fan-out, exception swallowing, deduplication by `technique_id` (first writer wins, highest confidence kept), `build_from_config()` factory
  - `builtin.py` — `BuiltinTechniqueModule` porting the 7 per-entry + 1 chain-level detectors; each hit carries `detection_module="builtin"`
  - `sigma.py` — `SigmaModule` loading `*.yml`/`*.yaml` from configured dirs; custom evaluator against `AnalysisContext` fact dict; supported modifiers: `contains`, `startswith`, `endswith`, `re`, `gte`, `lte`; condition expression parser supporting `and`/`or`/`not` and parentheses; unsupported rules skipped at load time
- [x] Rulepack `detonator/analysis/rules/builtin/` — 8 YAML files with stable `uuid5` IDs matching the old `_tech_id()` output
- [x] `detonator/config.py` — `AnalysisModuleConfig` placeholder, `AnalysisConfig(modules, rules_dirs)`, wired into `DetonatorConfig.analysis`
- [x] `config.example.toml` — new `[analysis]` section with `modules` and `rules_dirs`
- [x] Runner wired: `_filter()` builds `AnalysisContext.from_chain()`, calls `await pipeline.run(ctx)`, persists hits with `detection_module=hit.detection_module`
- [x] `detonator/analysis/filter.py` pruned: `TechniqueDetector`, `TechniqueHit`, `_ENTRY_DETECTORS`, `_TECH_NS`, `_tech_id` removed; `FilterResult.technique_hits` removed; default noise catalogues preserved
- [x] `pyproject.toml` — new `analysis = ["pyyaml>=6.0"]` optional extra
- [x] Tests: 3 new test files
  - `tests/test_analysis_builtin.py` — parity + detection_module field verification
  - `tests/test_analysis_sigma.py` — all modifier types, all condition combinators, list OR semantics, error cases
  - `tests/test_analysis_pipeline.py` — exception swallowing, aggregation, deduplication
- [x] `tests/test_chain_filter.py` — `TechniqueDetector` imports and technique-hit tests removed; noise-filter coverage preserved

---

## Phase 6 — Manifest & Polish (Complete)

- [x] Manifest assembly ([detonator/storage/manifest.py](detonator/storage/manifest.py))
  - `build_manifest()` consolidates run config + artifact inventory + enrichment summary + chain/filter stats + technique matches into `manifest.json`
  - Written by `Runner._write_manifest()` after every run (complete or error); registered in DB as `manifest` artifact type
- [x] Run listing/querying via API (filter by URL, date, status, domain)
  - `GET /runs` now accepts `status`, `domain`, `date_from`, `date_to`, `limit`, `offset`
  - `Database.list_runs` updated with matching parameters
- [x] Cross-run domain correlation queries
  - `GET /domain/{domain}/runs` — returns all runs that touched a domain via seed URL or enriched observable
  - `Database.find_runs_by_domain()` — LEFT JOINs `run_observables` + `observables` so both seed-URL and enrichment paths are covered
- [x] Interactive mode polish
  - `GET /runs/{id}` now includes `console_url` when run status is `interactive` and the runner is still active
  - `console_url` fetched from `VMProvider.get_console_url()`; errors logged but non-fatal
- [x] Error recovery: `Runner._fail()` + `finally` block on `execute()` ensure partial artifacts and manifest are always written; `_write_manifest()` is non-fatal on failure
- [x] OpenAPI spec auto-generated from FastAPI, served at `/docs` (Swagger UI) and `/redoc` (ReDoc)
  - Endpoints annotated with `summary=` and docstrings for richer generated docs
- [x] Structured JSON logging throughout, per-run log context (run ID)
  - `detonator/logging.py`: `JsonFormatter`, `RunAdapter`, `setup_logging()`
  - `Runner` uses `RunAdapter` — every log line carries `run_id` automatically
  - `setup_logging(json_logs=True)` activated via `--json-logs` flag on the CLI entrypoint

---

## Phase 7 — Web UI (Complete — graph view deferred)

Server-rendered dashboard layered onto the existing FastAPI app. No JS build
step, no auth (home-lab scope unchanged). Designed so cytoscape.js can drop in
later for the observable/technique/campaign graph view without changing the
stack.

### Agent config refactor (prerequisite)

Agents are now explicitly named in config. This replaces the flat
`default_vm_id` / `default_snapshot` / `[agent]` triple from Phase 2.

- [x] `AgentInstanceConfig` in [detonator/config.py](detonator/config.py)
  — `name`, `vm_id`, `snapshot`, `port`, `health_timeout_sec`, `health_poll_sec`
- [x] `DetonatorConfig.agents: list[AgentInstanceConfig]` + helpers
  `get_agent(name)` and `default_agent()`
- [x] Runner takes `agent: AgentInstanceConfig` directly — no more global
  lookup for vm_id/snapshot/port/timeouts
- [x] `POST /runs` accepts optional `agent: str` (agent name); falls back to
  `default_agent()` when omitted
- [x] `config.example.toml` uses `[[agents]]` TOML array
- [x] Removed `default_vm_id`, `default_snapshot`, and `AgentConfig` from config model

### UI implementation

- [x] UI router mounted at `/ui/` via `detonator.ui.mount_ui(app)`
  ([detonator/ui/routes.py](detonator/ui/routes.py))
- [x] Jinja2 templates + HTMX polling — no JS build step
  - Vendored `htmx.min.js` (1.9.12) and `pico.min.css` (2.0.6) under
    [detonator/ui/static/](detonator/ui/static/)
  - New optional extra: `pip install -e ".[ui]"` (jinja2 + python-multipart)
- [x] Pages
  - `/ui/` — dashboard: VM provider type, active runs, agent cards, submit
    form (URL + agent + egress + interactive), recent runs
  - `/ui/config` — VM provider, known VMs from provider, configured agents,
    egress providers, enrichment modules, timeouts
  - `/ui/runs` — filtered run list (status / domain / date range / limit);
    rows for active runs auto-refresh
  - `/ui/runs/{id}` — run detail: state timeline, artifacts table, enrichment
    summary, observables, technique matches, chain stats, console URL +
    resume button for interactive runs, zip download link
- [x] HTMX partials (2–5s polling, `include_in_schema=False`)
  - `/ui/_partials/run-state/{id}` — live state badge + latest transition
  - `/ui/_partials/runs-table` — live run-list body
  - `/ui/_partials/agents` — live agent status cards
- [x] Form POSTs
  - `POST /ui/runs` — submit run, redirects (303) to `/ui/runs/{id}`
  - `POST /ui/runs/{id}/resume` — resume interactive run, redirects back

### Remaining / deferred

- [ ] **Graph view** — cytoscape.js-powered neighborhood explorer over
  `GET /observables/{id}/graph`. UI stack chosen to accommodate this; data
  endpoints already exist.
- [ ] Campaign UI pages (list + detail) — JSON endpoints present, UI not yet
  wired.
- [ ] Observable detail page (currently only reachable via JSON API).
- [ ] UI tests — TestClient snapshot tests for each page and partial.

---

## Cross-Cutting (ongoing)

- [x] Structured JSON logging with per-run context (Phase 6)
- [x] Consistent error handling with partial-result preservation (Phase 6)
- [ ] Configurable per-stage timeouts with sane defaults
- [ ] Idempotency: every run starts from a clean VM snapshot
- [ ] Security: agent bound only to isolated bridge; nftables verified pre-detonation

---

## Explicitly Out of Scope (v1)

- Active traffic manipulation (mitmproxy) — deferred to optional separate VM image
- Signal/signature taxonomy — `signals.py` stub returns empty until the framework is stable
- Multi-browser (Firefox, WebKit) — Chromium only (HAR `_initiator` graph is cleanest there)
- Headless mode — headed always for v1
- Multi-VM concurrent runs — single VM, sequential runs
- Authentication on the host API — home lab, trusted network
- Linux guest support — Windows first per user direction

---

## Future: Threat Actor Correlation Graph (post-v1)

The observable/technique/campaign model in SQLite is structurally ready for a Neo4j migration. The three-tier data model (observables / techniques / campaigns) was chosen specifically so rows map directly to nodes and edges when the time comes.

**Node types**: Observables (domain, IP, favicon hash, phone, email, TLS fingerprint, registrant, crypto wallet), Techniques (hosting patterns, redirect chains, obfuscation families), Campaigns (groupings of runs/sites sharing observables + techniques).

**Edge types**: observable↔observable (`resolves_to`, `redirects_to`, `serves_favicon`, `registered_by`, `co_occurs_with`), run→observable (`found_in`), run→technique (`matches`), campaign→observable (`uses_infrastructure`, `associated_contact`), campaign→technique (`employs`).

**Target queries**: "all campaigns using Google Storage for phishing", "this new URL shares a favicon hash and registrant with Campaign X", "what techniques does this actor use", "find other sites using the same phone number".

v1 captures everything needed in the relational schema; migration is ETL, not a restructuring.
