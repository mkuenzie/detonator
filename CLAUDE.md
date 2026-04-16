# Detonator — Agent Guidance

Interactive, HAR-first URL detonation framework for a home malware/phishing analysis lab. This file orients future Claude sessions. For the full spec and phase tracker, see [SPEC.md](SPEC.md).

## What this project is

- Analyst submits a URL → it detonates in a sandboxed VM → artifacts (HAR, screenshots, DOM, console) are captured → the host enriches them (WHOIS/DNS/TLS/favicon) → the HAR is filtered to the initiator chain → results are stored and queryable.
- One VM, stateless, reverted per run. The host owns routing and isolation. All analysis runs outside the VM against dumped artifacts.
- Home lab scope: single operator, trusted host network, no auth on the host API.

## Non-negotiable design principles

- **Technology-agnostic at every integration boundary.** Hypervisor, browser engine, egress method, enrichment source — all behind an ABC. New implementations slot in without touching callers. Never shortcut this by coupling a caller to a concrete provider.
- **Campaigns are the primary analytical entity, not runs.** Runs are evidence-collection events. The analyst thinks in terms of sites/campaigns. API surfaces campaigns as first-class. See the three-tier data model below.
- **HAR-first, no MITM.** No mitmproxy, no TLS interception in v1. The `_initiator` field from Chromium drives chain extraction — this is why Chromium is the only supported browser.
- **Headed browser only for v1.** Interactive takeover (VNC/SPICE) requires a real desktop session. No headless mode.
- **Stateless VM.** Every run starts from a clean snapshot. Any malware executing during detonation is destroyed on revert. Never add persistent state to the guest.
- **Isolation enforced at the network layer, not in the agent.** The agent has no auth. Host nftables rules guarantee the agent's port is only reachable from the orchestrator on the isolated bridge.

## Architecture at a glance

```
Analyst → Host Orchestrator (FastAPI)
            ├── REST API  (JSON, at /)
            ├── Web UI    (Jinja2 + HTMX, at /ui/)
            ├── VMProvider (Proxmox first)
            ├── EgressProvider (direct / vpn / tether)
            ├── Agent REST client
            ├── Enrichment pipeline (WHOIS, DNS, TLS, favicon, ...)
            ├── Chain extractor (filters HAR to initiator chain)
            └── Storage (SQLite metadata + filesystem blobs)

In-VM Agent (FastAPI + Playwright Chromium, headed)
            └── /health, /detonate, /status, /resume, /artifacts[/name]
```

## Three-tier data model (critical)

The SQLite schema is designed so rows map directly to Neo4j nodes/edges when the graph migration happens post-v1. Do not collapse these layers.

- **Observables** — raw atomic indicators pulled from artifacts (domain, IP, URL, favicon hash, email, phone, TLS fingerprint, crypto wallet, registrant, ASN). Graph nodes.
- **Techniques** — behavioral patterns/signatures derived from analysis (e.g. "hosted on storage.googleapis.com", "base64 meta-refresh redirect", "Cloudflare Workers abuse"). Graph nodes. A `technique_matches` row links a technique to the run + evidence that triggered it.
- **Campaigns** — groupings of related runs/sites representing a single threat operation. The primary analytical entity. Campaigns reference both observables and techniques.

Relationship tables (`observable_links`, `run_observables`, `campaign_observables`, `campaign_techniques`, `campaign_runs`) are the future graph edges. Keep them typed and evidenced.

## Technology selections (first modules)

| Boundary | Abstraction | First implementation |
|---|---|---|
| Hypervisor | `VMProvider` | Proxmox (`proxmoxer`) |
| Browser | `BrowserModule` | Playwright Chromium, headed |
| Egress — direct | `EgressProvider` | Linux bridge + nftables |
| Egress — VPN | `EgressProvider` | WireGuard tunnel |
| Egress — tether | `EgressProvider` | USB RNDIS/CDC |
| WHOIS | `Enricher` | `asyncwhois` / raw RDAP |
| DNS | `Enricher` | `dnspython` |
| TLS | `Enricher` | `cryptography` / `ssl` stdlib |
| Favicon hash | `Enricher` | `mmh3` + `hashlib` |
| Host API | — | FastAPI + uvicorn |
| Agent API | — | FastAPI |
| Web UI | — | Jinja2 + HTMX (+ Pico CSS, vendored); cytoscape.js planned for graph |
| Storage | — | SQLite (`aiosqlite`) + filesystem |
| Config | — | TOML (`tomllib`) |
| VM guest OS | — | **Windows 10/11 first** (not Linux) |

## Repo layout

```
detonator/
  pyproject.toml
  config.example.toml
  SPEC.md                        # Phase tracker / remaining work
  CLAUDE.md                      # This file
  detonator/                     # Host orchestrator package
    config.py                    # TOML config loading + Pydantic models
    models/                      # Shared data models (vm, run, observables)
    orchestrator/                # FastAPI app + runner + agent manager (Phase 2)
    providers/
      vm/                        # VMProvider ABC + Proxmox impl
      egress/                    # EgressProvider ABC + direct/vpn/tether (Phase 3)
    enrichment/                  # Enricher ABC + whois/dns/tls/favicon (Phase 4)
    analysis/                    # chain.py, filter.py (Phase 5)
    storage/                     # database.py, filesystem.py, manifest.py
    ui/                          # Jinja2 + HTMX web UI, mounted at /ui/ (Phase 7)
      routes.py                  # All UI + HTMX-partial handlers
      templates/                 # Jinja2 pages (dashboard, config, runs, run_detail)
      static/                    # Vendored htmx.min.js + pico.min.css + style.css
  agent/                         # In-VM agent (runs on the Windows sandbox)
    api.py                       # FastAPI REST API
    browser/
      base.py                    # BrowserModule ABC
      playwright_chromium.py     # Playwright Chromium implementation
    config.py                    # Entrypoint (uvicorn launcher)
    README.md                    # Windows base-image setup guide
  tests/
```

## Run lifecycle state machine

`pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`

Every transition is logged with a timestamp and detail. Each stage has a configurable timeout. Failures move to `error` with partial artifacts preserved — never discard what was already captured.

## Working conventions

- **Python 3.11+**, `from __future__ import annotations`, Pydantic models, `datetime.now(UTC)` (never `utcnow()`).
- **Async throughout** for I/O-bound work (provider calls, agent HTTP, enrichment fan-out).
- **Virtualenv at `.venv/`** at the repo root. Install via `pip install -e ".[dev,proxmox,agent,enrichment,ui]"` depending on what you're touching. The `ui` extra (jinja2 + python-multipart) is required to serve `/ui/`; omit it for headless deployments.
- **Agents are configured, not defaulted.** `config.toml` declares one or more `[[agents]]` entries (name + vm_id + snapshot + port + health timeouts). The Runner takes an `AgentInstanceConfig` explicitly — do not re-introduce the old `default_vm_id` / `default_snapshot` fallback path.
- **Tests use pytest + pytest-asyncio**. Proxmox tests mock at the module level (patch `detonator.providers.vm.proxmox.asyncio.to_thread`) — `AsyncMock` doesn't play nicely with `asyncio.to_thread` in 3.12+.
- **No secrets in code or tests.** Config loads from TOML; example at `config.example.toml`.
- **Structured JSON logging** with per-run context (run ID). Not fully wired yet — add it as components land.

## Out of scope for v1 (don't build these)

- Active traffic manipulation (mitmproxy)
- Signal/signature taxonomy (`signals.py` stub returns empty until framework stabilizes)
- Multi-browser (Firefox, WebKit)
- Headless mode
- Multi-VM concurrent runs
- Authentication on the host API
- Linux guest support (Windows first)

## When starting a new phase

1. Read [SPEC.md](SPEC.md) for the current phase checklist.
2. Check what ABCs and models already exist — don't redefine them.
3. Honor the technology-agnostic boundary: implementation goes behind an existing ABC, not in the caller.
4. Update [SPEC.md](SPEC.md) as items complete.
