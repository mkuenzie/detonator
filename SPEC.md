# Detonator — Spec & Phase Tracker

Living document tracking what's built, what's next, and what's deferred. Update as phases complete.

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | VM Provider Abstraction | Complete |
| 1 | In-VM Agent | Partial (code scaffolded, not yet run on a real VM) |
| 2 | Host Orchestrator | Complete (unit-tested; end-to-end smoke pending a real VM) |
| 3 | Egress & Isolation | Not started |
| 4 | Enrichment Pipeline | Not started |
| 5 | Chain Extraction & Filtering | Not started |
| 6 | Manifest & Polish | Not started |

---

## Phase 0 — VM Provider Abstraction (Complete)

- [x] `VMProvider` ABC ([detonator/providers/vm/base.py](detonator/providers/vm/base.py))
- [x] Data models: `VMState`, `VMInfo`, `NetworkInfo` ([detonator/models/vm.py](detonator/models/vm.py))
- [x] `ProxmoxProvider` implementation ([detonator/providers/vm/proxmox.py](detonator/providers/vm/proxmox.py))
- [x] Unit tests with mocked Proxmox API ([tests/test_proxmox_provider.py](tests/test_proxmox_provider.py))
- [ ] Manual integration test against real Proxmox instance

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
- [x] `GET /health` — orchestrator health + active-run count

### Tests (54 total, all green)
- [x] `tests/test_agent_client.py` (7) — httpx MockTransport: health, retries, timeout, detonate payload, terminal polling, interactive pause, download_all
- [x] `tests/test_runner.py` (5) — StubVMProvider + FakeAgentClient: happy path, agent error, missing VM IP, missing vm_id/snapshot, interactive pause/resume
- [x] `tests/test_orchestrator_api.py` (10) — TestClient: health, config endpoints, run CRUD 404s, campaign round-trip, observables/techniques empty, run creation schedules the background task

### Verification
- [ ] End-to-end smoke: submit a URL against a real Windows VM → full lifecycle completes → artifacts on disk → row in SQLite. Blocked on Phase 1's "build a real Windows base image" item.

### Known gaps / deferred
- `preflight` stage is a no-op transition. Phase 3 plugs `EgressProvider.preflight_check()` in here.
- `enriching` / `filtering` stages are no-op transitions. Phases 4 / 5 land the real work.
- No structured JSON logging yet — standard `logging` only. Cross-cutting concern, tracked below.
- No manifest consolidation — Phase 6.

---

## Phase 3 — Egress & Isolation (Not Started)

- [x] `EgressProvider` ABC + `PreflightResult` ([detonator/providers/egress/base.py](detonator/providers/egress/base.py))
- [ ] Direct egress provider (Linux bridge + nftables rules)
- [ ] VPN egress provider (WireGuard tunnel steering)
- [ ] USB tether egress provider (RNDIS/CDC interface routing)
- [ ] Pre-flight verification module
  - [ ] DNS resolution check (DNS goes through expected path)
  - [ ] Public IP check via external endpoint
  - [ ] LAN probe (confirm host LAN is unreachable from VM)
- [ ] Post-run teardown and verification
- [ ] Integration with orchestrator state machine (`preflight` stage)
- [ ] Verification: run with each egress type, confirm public IP matches, confirm LAN blocked

**Security invariants to enforce:**
- VM bridge has no default route to host LAN
- nftables whitelists ONLY the designated egress path
- All rules torn down AND verified absent after run completes

---

## Phase 4 — Enrichment Pipeline (Not Started)

- [x] `Enricher` ABC, `RunContext`, `EnrichmentResult` ([detonator/enrichment/base.py](detonator/enrichment/base.py))
- [ ] WHOIS/RDAP enricher (`asyncwhois` or raw RDAP HTTP)
- [ ] DNS enricher (`dnspython` — A/AAAA/CNAME/MX/NS/TXT)
- [ ] TLS cert chain enricher (`cryptography` / `ssl`)
- [ ] Favicon hash enricher (`mmh3` + `hashlib`)
- [ ] TLD analysis enricher (age, punycode/IDN detection)
- [ ] Pipeline runner ([detonator/enrichment/pipeline.py](detonator/enrichment/pipeline.py))
  - Extract domains/URLs from stored HAR
  - Fan out to enrichers concurrently
  - Collect + persist results
- [ ] DOM content extraction (feeds observables)
  - Regex extractors: emails, phone numbers, crypto wallets, social handles
  - Form actions, meta tags, embedded URLs
  - Results stored as `Observable` rows linked to the run
- [ ] Store enrichment results to filesystem + SQLite
- [ ] Tests: run enrichment against a captured HAR fixture

---

## Phase 5 — Chain Extraction & Filtering (Not Started)

- [ ] HAR parser ([detonator/analysis/chain.py](detonator/analysis/chain.py))
- [ ] Initiator graph builder (uses Chromium's `_initiator` field)
- [ ] Chain walk algorithm (seed URL → full initiator tree)
- [ ] Noise classifier ([detonator/analysis/filter.py](detonator/analysis/filter.py))
  - Known tracking domain lists (configurable)
  - Heuristic: no initiator relationship to seed chain
  - Request type flags (beacon, ping, prefetch)
- [ ] Output filtered `har_chain.json` alongside `har_full.json`
- [ ] Technique detection hooks (e.g. "hosted on storage.googleapis.com" → `technique_matches` row)
- [ ] Tests: fixture HAR with known noise → verify chain preserved, noise removed

---

## Phase 6 — Manifest & Polish (Not Started)

- [ ] Manifest assembly ([detonator/storage/manifest.py](detonator/storage/manifest.py))
  - Consolidate run config + artifacts + enrichment + technique matches into `manifest.json`
- [ ] Run listing/querying via API (filter by URL, date, status, domain)
- [ ] Cross-run domain correlation queries
- [ ] Interactive mode polish
  - Pause/resume flow end-to-end
  - Surface VNC/SPICE console URL in `GET /runs/{id}`
- [ ] Error recovery: verify partial-artifact preservation on every failure path
- [ ] OpenAPI spec auto-generated from FastAPI, served at `/docs`
- [ ] Structured JSON logging throughout, per-run log context (run ID)

---

## Cross-Cutting (ongoing)

- [ ] Structured JSON logging with per-run context
- [ ] Consistent error handling with partial-result preservation
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
- Web UI — API-first; UI is a future layer
- Linux guest support — Windows first per user direction

---

## Future: Threat Actor Correlation Graph (post-v1)

The observable/technique/campaign model in SQLite is structurally ready for a Neo4j migration. The three-tier data model (observables / techniques / campaigns) was chosen specifically so rows map directly to nodes and edges when the time comes.

**Node types**: Observables (domain, IP, favicon hash, phone, email, TLS fingerprint, registrant, crypto wallet), Techniques (hosting patterns, redirect chains, obfuscation families), Campaigns (groupings of runs/sites sharing observables + techniques).

**Edge types**: observable↔observable (`resolves_to`, `redirects_to`, `serves_favicon`, `registered_by`, `co_occurs_with`), run→observable (`found_in`), run→technique (`matches`), campaign→observable (`uses_infrastructure`, `associated_contact`), campaign→technique (`employs`).

**Target queries**: "all campaigns using Google Storage for phishing", "this new URL shares a favicon hash and registrant with Campaign X", "what techniques does this actor use", "find other sites using the same phone number".

v1 captures everything needed in the relational schema; migration is ETL, not a restructuring.
