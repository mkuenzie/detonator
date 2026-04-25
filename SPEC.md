# Detonator — Spec & Phase Tracker

Living document tracking what's built, what's next, and what's deferred. Code is truth; update this file when reality diverges.

## Status Summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | VM Provider Abstraction | Complete |
| 1 | In-VM Agent | Complete (core); optional test coverage gaps remain |
| 2 | Host Orchestrator | Complete |
| 3 | Egress & Isolation | Partial — direct + tether complete; VPN deferred |
| 4 | Enrichment Pipeline | Complete |
| 5 | Navigation scope + noise filter | Complete (renamed from "Chain Extraction") |
| 5b | Analysis Modularization | Partial — Sigma module live; builtin module was removed |
| 6 | Manifest & Polish | Complete |
| 7 | Web UI | Complete — graph view + campaign/observable detail pages deferred |

---

## Known issues (actively tracked, not phase-scoped)

- **Duplicate `site_resource` artifacts.** `Runner._collect_artifacts()` unions HAR body refs (SHA-1 basenames, from Playwright's `record_har_content="attach"`) with the agent's `bodies/manifest.jsonl` refs (SHA-256 basenames). Because the naming schemes don't collide, identical bodies captured by both paths produce two artifact rows pointing at the same CAS blob.

  **Empirical capture-set diff** (run via [scripts/capture_diff.py](scripts/capture_diff.py)) on run `3213f825-c2cc-4cb3-858d-7f3606d7d67c`: HAR=130, manifest=119, both=119, HAR-only=11, manifest-only=0. Of the 11 HAR-only URLs, 8 are empty/aborted/redirect (no body to lose) and 3 are cross-origin sub-frame URLs (`ssl.kaptcha.com/{logo.htm, fin, md}`) that the manifest never sees at all — including a 24KB `logo.htm` body. Root cause: `CDPResponseTap` attaches via `context.new_cdp_session(page)`, which doesn't reach cross-origin iframes that Chromium's site isolation puts in a separate renderer / CDP target.

  **Resolution path:** extend `CDPResponseTap` with `Target.setAutoAttach({autoAttach: true, flatten: true})` + `Target.attachedToTarget` handling so child targets (iframes, and later workers — same machinery) get `Network.enable` and feed the same sink. Once that lands and a re-run of `capture_diff.py` shows HAR-only ≈ 0 across varied runs, disable `record_har_content="attach"` in [agent/browser/playwright_chromium.py](agent/browser/playwright_chromium.py) and drop the HAR-side branch in `Runner._collect_artifacts()`. Until then the dup is benign (CAS dedupes the bytes; only the artifact rows are duplicated).

- **Default `analysis.modules` still lists `"builtin"`.** [detonator/config.py](detonator/config.py) defaults `AnalysisConfig.modules = ["builtin", "sigma"]`, but `_build_module()` in [detonator/analysis/modules/pipeline.py](detonator/analysis/modules/pipeline.py) only recognises `"sigma"` — the builtin module was deleted. Every run with default config logs a warning. Fix by dropping `"builtin"` from the default.

- **Default `analysis.rules_dirs` points at a non-existent directory.** Defaults to `["detonator/analysis/rules/builtin"]`; actual rules live flat in `detonator/analysis/rules/` (currently one file: `gcs_js_location_redirect.yml`). Fix by updating the default and `config.example.toml`.

- **Legacy alias `load_extra_bodies`.** [detonator/analysis/har_body_map.py:214](detonator/analysis/har_body_map.py) still exports `load_extra_bodies = load_capture_manifest` for pre-v2 callers. Audit callers and delete if unused.

---

## Phase 0 — VM Provider Abstraction (Complete)

- [x] `VMProvider` ABC ([detonator/providers/vm/base.py](detonator/providers/vm/base.py))
- [x] Data models: `VMState`, `VMInfo`, `NetworkInfo` ([detonator/models/vm.py](detonator/models/vm.py))
- [x] `ProxmoxProvider` implementation ([detonator/providers/vm/proxmox.py](detonator/providers/vm/proxmox.py))
- [x] Unit tests with mocked Proxmox API ([tests/test_proxmox_provider.py](tests/test_proxmox_provider.py))
- [x] Manual integration test against real Proxmox instance

---

## Phase 1 — In-VM Agent (Complete, with test coverage gaps)

### Done
- [x] Agent REST API: `/health`, `/detonate`, `/status`, `/resume`, `/artifacts`, `/artifacts/{name}` ([agent/api.py](agent/api.py))
- [x] `BrowserModule` ABC + `DetonationRequest` / `DetonationResult` / `StealthProfile` ([agent/browser/base.py](agent/browser/base.py))
- [x] Playwright Chromium module ([agent/browser/playwright_chromium.py](agent/browser/playwright_chromium.py)) — HAR capture, screenshots, DOM dump, console collection, navigation timeline, interactive pause/resume
- [x] `NetworkCapture` — SHA-256-addressed body store + `bodies/manifest.jsonl` ([agent/browser/network_capture.py](agent/browser/network_capture.py))
- [x] `CDPResponseTap` — per-page CDP Network listener pulling bodies in `loadingFinished` ([agent/browser/cdp_response_tap.py](agent/browser/cdp_response_tap.py))
- [x] `RouteDocumentInterceptor` — main-frame document body capture ([agent/browser/route_document_interceptor.py](agent/browser/route_document_interceptor.py))
- [x] Agent entrypoint / uvicorn launcher ([agent/config.py](agent/config.py))
- [x] Windows base image setup guide ([agent/README.md](agent/README.md))
- [x] End-to-end smoke validated against a real Windows VM

### Remaining
- [ ] Unit tests for the agent API (FastAPI TestClient, mocked BrowserModule)
- [ ] Tests for the Playwright module (integration, run on demand)
- [ ] Empirical capture-set diff: compare URLs that appear only in HAR `_file` refs vs. URLs that appear only in `bodies/manifest.jsonl` across a representative batch. Informs the "kill HAR body attach" decision (see Known Issues).

---

## Phase 2 — Host Orchestrator (Complete)

### Implemented
- [x] FastAPI app factory with injectable deps, lifespan-managed DB + VM provider ([detonator/orchestrator/api.py](detonator/orchestrator/api.py))
- [x] Shared app state / in-flight run registry ([detonator/orchestrator/state.py](detonator/orchestrator/state.py))
- [x] Run lifecycle state machine ([detonator/orchestrator/runner.py](detonator/orchestrator/runner.py))
  - States: `pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`
  - Every transition logged + persisted with timestamp + detail
  - Per-stage `asyncio.timeout` enforcement
  - Partial-artifact preservation on error (`_fail` runs before finalization)
  - `meta.json` + `manifest.json` always written to the run dir
- [x] Agent HTTP client ([detonator/orchestrator/agent_manager.py](detonator/orchestrator/agent_manager.py))
  - Renamed from `agent_client.py` — previous SPEC references are stale
  - `wait_for_health` with retry until timeout; `detonate`, `status`, `resume`; `wait_for_terminal` with optional `pause_on_interactive`; `download_all` preserves sub-paths
- [x] Full flow wired: VM revert → start → wait for agent → detonate → collect → force-stop (always in `finally`)
- [x] Artifact persistence via `ArtifactStore` (content-addressed blob store; symlinks from `runs/{id}/`)
- [x] Run/artifact/transition persistence via `Database`
- [x] Config via `load_config` ([detonator/config.py](detonator/config.py))

### REST endpoints
See [README.md](README.md) for the full reference. Route source: [detonator/orchestrator/api.py](detonator/orchestrator/api.py).

### Tests
54+ tests covering the agent client, Runner state transitions, and API surface. All green.

---

## Phase 3 — Egress & Isolation (Partial — direct + tether complete, VPN deferred)

**Architecture decision:** The orchestrator host acts as the L3 sandbox gateway.
Proxmox's only role is VM lifecycle; all routing, NAT, and firewall rules live in
the orchestrator's own kernel. See [.claude/plans/sprightly-singing-curry.md](.claude/plans/sprightly-singing-curry.md)
for the topology and rationale.

### Done
- [x] `EgressProvider` ABC + `PreflightResult` ([detonator/providers/egress/base.py](detonator/providers/egress/base.py))
- [x] `DirectEgressProvider` ([detonator/providers/egress/direct.py](detonator/providers/egress/direct.py))
  - `activate()`: enables `net.ipv4.ip_forward` via sysctl; atomically loads an nftables table with MASQUERADE (postrouting) and forward chains (LAN isolation + sandbox → uplink accept)
  - `deactivate()`: idempotently deletes the nftables table; always called in runner `finally` block
  - `preflight_check()`: confirms public IP via ipify; returns `PreflightResult`
- [x] `TetherEgressProvider` ([detonator/providers/egress/tether.py](detonator/providers/egress/tether.py))
  - Separate nftables table `detonator-tether` so both providers coexist
  - Preflight adds uplink IPv4-liveness check (fails fast if Personal Hotspot is off)
  - `get_public_ip()` binds the httpx connection to the tether interface IP to measure the tether path, not the default route
- [x] Shared ruleset generator ([detonator/providers/egress/_routing.py](detonator/providers/egress/_routing.py))
- [x] `config.example.toml` updated: orchestrator-local egress (`uplink_interface`, `sandbox_cidr`, `lan_cidr`); tether block documented
- [x] Runner integration (`_preflight`, `_teardown_egress` in `finally`)
- [x] `build_egress_provider()` in `api.py` maps `EgressType` → provider instance
- [x] Unit tests: 12 for direct egress, 13 for tether
- [x] [docs/tether-setup.md](docs/tether-setup.md): Proxmox USB passthrough, ipheth + usbmuxd setup, Trust This Computer pairing

### Remaining / deferred
- [ ] VPN egress provider (WireGuard)
- [ ] Preflight: LAN isolation probe (agent attempts to reach host-LAN IP, asserts failure)
- [ ] Preflight: DNS-path check (DNS queries exit via expected egress)
- [ ] Post-teardown verification (assert nftables table absent after run)
- [ ] Manual integration: submit a run, confirm public IP matches, confirm LAN blocked from inside VM

**Security invariants enforced by nftables:**
- `ip saddr <sandbox_cidr> ip daddr <lan_cidr> drop` — VM cannot reach host LAN
- `ip saddr <sandbox_cidr> oif <uplink> masquerade` — sandbox traffic NATed out uplink only
- `ip saddr <sandbox_cidr> drop` — all other sandbox forward attempts dropped
- Rules loaded atomically via `nft -f`; deleted idempotently on run exit

---

## Phase 4 — Enrichment Pipeline (Complete)

Enrichers split into `core/` (always run, artifact-parsing) and `plugins/` (opt-in, external lookups). `EnrichmentPipeline` concurrently fans out to enrichers that `accept()` the available artifact types; one failing enricher never aborts the rest (`return_exceptions=True`). Observables are deduplicated by deterministic uuid5 before DB upsert.

- [x] `Enricher` ABC, `RunContext`, `EnrichmentResult` ([detonator/enrichment/base.py](detonator/enrichment/base.py))
- [x] `observable_id(type, value)` — deterministic uuid5 for deduplication
- [x] Core enrichers ([detonator/enrichment/core/](detonator/enrichment/core/))
  - `NavigationEnricher` — extracts the navigation initiator scope from `navigations.json` + HAR
  - `DomExtractor` — emails, US phone numbers, BTC (legacy + bech32) and ETH wallets, `<form action>` targets, `<meta http-equiv=refresh>` redirects
- [x] Plugin enrichers ([detonator/enrichment/plugins/](detonator/enrichment/plugins/))
  - `WhoisEnricher` — registrar/dates/name servers/registrant org; creates REGISTRANT observable
  - `DnsEnricher` — A/AAAA/CNAME/MX/NS/TXT; creates IP observables linked with `resolves_to`
  - `TlsEnricher` — subject/issuer/SANs/fingerprint on 443; creates TLS_FINGERPRINT observable with `issued_by` link
  - `FaviconEnricher` — mmh3 + MD5 on `/favicon.ico`; creates FAVICON_HASH observable with `serves_favicon` link
  - `TldEnricher` — TLD, label count, subdomain depth, punycode/IDN detection
  - `HostingEnricher` — IP → ASN via Team Cymru DNS; creates HOSTING_PROVIDER observable with `hosted_by` link
- [x] HAR extractor helper ([detonator/enrichment/har.py](detonator/enrichment/har.py)) — hostnames vs IPs split
- [x] Pipeline ([detonator/enrichment/pipeline.py](detonator/enrichment/pipeline.py))
  - `build_from_config(config, db, store)` factory reads `enrichment.modules`
  - Fault-isolated concurrent fan-out; observables + links upserted to DB
  - Every observable linked back to the run via `run_observables` (source=enrichment)
- [x] Runner wired: `_enrich()` calls `EnrichmentPipeline.run()` under `enrich_sec` timeout
- [x] Exclusion matrix — `enrichment_exclusions` table; hosts matched by exact suffix (case-insensitive). Seeded with well-known CDNs on first startup.
- [x] Tests: enrichment pipeline unit tests cover HAR extraction, `observable_id` determinism, TLD/DOM extractors, end-to-end pipeline + fault isolation

---

## Phase 5 — Navigation Scope & Noise Filter (Complete)

This phase was originally scoped as "Chain Extraction" on top of the HAR `_initiator` graph. When navigations were promoted to first-class evidence (commit `57bf27a`), the module was restructured: `detonator/analysis/chain.py` was renamed to [detonator/analysis/navigation.py](detonator/analysis/navigation.py), and the top-level function is now `extract_navigation_scope()` rather than `extract_chain()`.

- [x] HAR parser ([detonator/analysis/navigation.py](detonator/analysis/navigation.py))
  - `parse_har(path)` → `list[HarEntry]`; extracts `_initiator.type`/URL (redirect, parser, script via callFrames), `_resourceType`, `serverIPAddress`
- [x] Initiator graph — `build_initiator_graph(entries)` returns forward adjacency (parent → children)
- [x] Navigation events loader — `load_navigation_events(navigations_path)` reads `navigations.json` (main + sub frame transitions)
- [x] `walk_from_roots(entries, roots)` — BFS over the initiator graph from multiple seed URLs (one per navigation)
- [x] `extract_navigation_scope(har_path, navigations_path, seed_url) → NavigationScope` — top-level entry point producing in-scope / out-of-scope classifications
- [x] Noise classifier ([detonator/analysis/filter.py](detonator/analysis/filter.py)) — `NoiseFilter`
  - `REASON_NO_CHAIN` — not reachable from any navigation root via initiator graph
  - `REASON_TRACKER` — domain in built-in tracking list (Google Analytics, GTM, DoubleClick, Facebook, Hotjar, Segment, Intercom, Bing, Yandex, TikTok, LinkedIn, …)
  - `REASON_RESOURCE_TYPE` — `_resourceType` ∈ `{ping, preflight, csp-violation-report, beacon}`
  - `noise_domains` / `noise_resource_types` config fields supplement (do not replace) built-ins
- [x] Outputs: `har_navigation.json` (in-scope HAR subset) + `filter_result.json`, both registered in DB
- [x] Runner wired: `_filter()` runs under `filter_sec` timeout; persists technique matches via `database.upsert_technique` + `insert_technique_match`
- [x] Config: `[filter]` section with `noise_domains`, `noise_resource_types`, `require_initiator_chain`; `timeouts.filter_sec`
- [x] Tests cover HAR parsing, graph construction, walk algorithms, scope extraction, JSON serialization

---

## Phase 5b — Analysis Modularization (Partial)

- Status: Sigma module complete; builtin module was **deleted** rather than ported.
- Rationale: the original 8 Python detectors were being superseded by YAML rules. The Sigma evaluator proved expressive enough that keeping a parallel Python module was cost, not value. The `detonator/analysis/rules/builtin/` directory originally targeted for the YAML ports was never populated.

### Current state
- [x] `detonator/analysis/modules/base.py` — `AnalysisContext`, `TechniqueHit` (with `detection_module` field), `AnalysisModule` ABC, `AnalysisContext.from_chain()` classmethod
- [x] `detonator/analysis/modules/pipeline.py` — `AnalysisPipeline` with concurrent fan-out, exception swallowing, deduplication by `technique_id` (highest confidence wins), `build_from_config()` factory. **`_build_module()` only recognises `"sigma"`.** Any other module name logs a warning and is skipped.
- [x] `detonator/analysis/modules/sigma.py` — `SigmaModule` loading `*.yml`/`*.yaml` from `rules_dirs`. Supports modifiers: `contains`, `startswith`, `endswith`, `re`, `gte`, `lte`. Condition parser supports `and`/`or`/`not` + parentheses. Unsupported rule constructs skipped at load time.
- [x] Rule directory: [detonator/analysis/rules/](detonator/analysis/rules/). Currently **1 rule**: `gcs_js_location_redirect.yml`. Not organized into a `builtin/` subdirectory.
- [x] `pyproject.toml` — `analysis = ["pyyaml>=6.0"]` optional extra
- [x] Tests: `tests/test_analysis_sigma.py` (modifier + combinator coverage), `tests/test_analysis_pipeline.py` (fan-out, dedup, fault isolation)

### Status
- [x] Config defaults fixed — `AnalysisConfig.modules = ["sigma"]`, `rules_dirs = ["detonator/analysis/rules"]`.
- The other 7 detection patterns from the original `BuiltinTechniqueModule` are not being ported — explicitly dropped. New rules are authored as Sigma YAML in [detonator/analysis/rules/](detonator/analysis/rules/) on demand.

---

## Phase 6 — Manifest & Polish (Complete)

- [x] Manifest assembly ([detonator/storage/manifest.py](detonator/storage/manifest.py))
  - `build_manifest()` consolidates run config + artifact inventory + enrichment summary + chain/filter stats + technique matches into `manifest.json`
  - Written by `Runner._write_manifest()` after every run (complete or error); registered in DB as `manifest` artifact type
- [x] Run listing: `GET /runs` accepts `status`, `domain`, `date_from`, `date_to`, `limit`, `offset`
- [x] Cross-run domain correlation: `GET /domain/{domain}/runs` LEFT-JOINs `run_observables` + `observables` so both seed-URL and enrichment paths are covered
- [x] Interactive mode: `GET /runs/{id}` returns `console_url` when the run is in `interactive` state and the runner is still active
- [x] Error recovery: `Runner._fail()` + `finally` on `execute()` always write partial manifest + meta
- [x] OpenAPI/Swagger UI at `/docs`, ReDoc at `/redoc`
- [x] Structured JSON logging ([detonator/logging.py](detonator/logging.py)) — `JsonFormatter`, `RunAdapter`, `setup_logging()`; enabled via `--json-logs`

---

## Phase 7 — Web UI (Complete — graph view + some detail pages deferred)

Server-rendered dashboard on the existing FastAPI app. No JS build step, no auth. Jinja2 + HTMX + vendored Pico CSS.

### Agent config refactor (prerequisite — done)
- [x] `AgentInstanceConfig` with per-agent name/vm_id/snapshot/port/health timeouts
- [x] `DetonatorConfig.agents: list[AgentInstanceConfig]` + `get_agent(name)` / `default_agent()` helpers
- [x] Runner takes an `AgentInstanceConfig` directly — old `default_vm_id` / `default_snapshot` / `AgentConfig` path removed

### UI implementation
- [x] UI router mounted at `/ui/` ([detonator/ui/routes.py](detonator/ui/routes.py))
- [x] Vendored `htmx.min.js` + `pico.min.css` under [detonator/ui/static/](detonator/ui/static/)
- [x] Optional extra: `pip install -e ".[ui]"` (jinja2 + python-multipart)
- [x] Pages
  - `/ui/` — dashboard (VM provider type, active runs, agent cards, submit form, recent runs)
  - `/ui/config` — VM provider, known VMs, configured agents, egress providers, enrichment modules + exclusion matrix editor, timeouts
  - `/ui/runs` — filtered run list (status / domain / date range / limit); active rows auto-refresh
  - `/ui/runs/{id}` — run detail: state timeline, artifacts, enrichment summary, observables, technique matches, chain stats, console URL + resume button for interactive runs, zip download link
- [x] HTMX partials
  - `/ui/_partials/run-state/{id}` — live state badge
  - `/ui/_partials/runs-table` — live run-list body
  - `/ui/_partials/agents` — live agent status cards
- [x] Form POSTs
  - `POST /ui/runs` — redirects (303) to `/ui/runs/{id}`
  - `POST /ui/runs/{id}/resume` — redirects back

### Graph endpoints (data layer complete, UI deferred)
- [x] `GET /graph/search?q=...` — match across observables / techniques / campaigns; returns typed node stubs for search results
- [x] `GET /graph/nodes/{node_type}/{node_id}/neighbors` — cytoscape-shaped `{nodes, edges}`
- [x] `GET /observables/{id}/graph` — observable neighborhood (outgoing links, incoming links, campaigns)

### Remaining / deferred
- [ ] **Graph view UI** — cytoscape.js-powered neighborhood explorer. Endpoints exist; frontend not wired.
- [ ] Campaign UI pages (list + detail) — JSON endpoints present
- [ ] Observable detail page (currently only reachable via JSON API)
- [ ] UI TestClient snapshot tests

---

## Cross-Cutting (ongoing)

- [x] Structured JSON logging with per-run context (Phase 6)
- [x] Consistent error handling with partial-result preservation (Phase 6)
- [x] Per-stage timeouts with sane defaults (`timeouts.*_sec` in config)
- [x] Idempotency: every run starts from a clean VM snapshot; egress teardown is idempotent
- [ ] Post-detonation verification that nftables rules were torn down cleanly

---

## Explicitly Out of Scope (v1)

- Active traffic manipulation (mitmproxy) — deferred to optional separate VM image if ever needed
- Multi-browser (Firefox, WebKit) — Chromium only, because CDP + `_initiator` are richest there
- Headless mode — headed always for v1
- Multi-VM concurrent runs — single VM, sequential runs
- Authentication on the host API — home lab, trusted network
- Linux guest support — Windows first per user direction
- **Service worker / shared worker response body capture** — the CDP tap in [agent/browser/cdp_response_tap.py](agent/browser/cdp_response_tap.py) attaches per-page via `context.new_cdp_session(page)`, which does not reach worker-owned CDP targets. Fixing it requires `Target.setAutoAttach({flatten: true})` with child-session routing, but Playwright Python's public API doesn't expose flattened child sessions (`new_cdp_session` accepts only `Page | Frame`; `Worker` has no CDP method). Workarounds (poking Playwright privates, opening a parallel raw-CDP websocket) were rejected as too fragile for v1. Outer SW-initiated requests still land in HAR; we only lose bodies for SW-intercepted fetches. Revisit when Playwright exposes child CDP sessions or when a real analysis case traces a missed body to SW interception. The existing stash/sink in `cdp_response_tap.py` is target-agnostic, so the v2 change is additive: acquire a child session per `Target.attachedToTarget` with `type ∈ {service_worker, shared_worker, worker}`, `Network.enable`, reuse the same handlers.

---

## Future: Threat Actor Correlation Graph (post-v1)

The observable/technique/campaign model in SQLite is structurally ready for a Neo4j migration. The three-tier data model was chosen specifically so rows map directly to nodes and edges.

**Node types**: Observables (domain, IP, favicon hash, phone, email, TLS fingerprint, registrant, crypto wallet, ASN/hosting provider), Techniques (hosting patterns, redirect chains, obfuscation families), Campaigns (groupings of runs/sites sharing observables + techniques).

**Edge types**: observable↔observable (`resolves_to`, `redirects_to`, `serves_favicon`, `registered_by`, `hosted_by`, `issued_by`, `co_occurs_with`), run→observable (`found_in`), run→technique (`matches`), campaign→observable (`uses_infrastructure`, `associated_contact`), campaign→technique (`employs`).

**Target queries**: "all campaigns using Google Storage for phishing", "this new URL shares a favicon hash and registrant with Campaign X", "what techniques does this actor use", "find other sites using the same phone number".

v1 captures everything needed in the relational schema; migration is ETL, not a restructuring.
