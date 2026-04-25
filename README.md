# Detonator — Host Orchestrator Setup

The host orchestrator receives detonation requests, manages the sandbox VM lifecycle via Proxmox, drives the in-VM agent, collects and stores artifacts, enriches them, and exposes a REST + web UI for interacting with runs, campaigns, and observables.

This guide covers setting up the orchestrator on the Linux host that has Proxmox access and controls the detonation network. For internal architecture and design principles, see [CLAUDE.md](CLAUDE.md). For phase status, see [SPEC.md](SPEC.md).

## Host Requirements

- **OS**: Linux (the orchestrator owns the sandbox network stack — bridge, nftables, sysctls)
- **Python**: 3.11 or later, 64-bit
- **Proxmox VE**: accessible from the host, with API token credentials
- **Sandbox bridge**: an isolated L2 bridge (e.g. `vmbr1`) connecting the orchestrator's sandbox NIC to the agent VM's NIC — see [Network Setup](#network-setup)

## Installation

```bash
cd /opt/detonator          # or wherever you placed the repo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[proxmox,enrichment,analysis,ui]"
```

Extras:
- `proxmox` — `proxmoxer` (Proxmox API client)
- `enrichment` — `dnspython`, `cryptography`, `asyncwhois`, `mmh3`
- `analysis` — `pyyaml` (Sigma rule evaluation)
- `ui` — `jinja2`, `python-multipart` (required to serve `/ui/`)
- `agent` — the in-VM agent's runtime deps (install on the guest image, not the orchestrator)
- `dev` — pytest + friends

Verify:

```bash
python -c "from detonator.config import load_config; print('ok')"
```

## Configuration

```bash
cp config.example.toml config.toml
$EDITOR config.toml
```

### Key sections

```toml
log_level = "INFO"

[vm_provider]
type = "proxmox"

[vm_provider.settings]
host        = "192.168.1.10"
port        = 8006
user        = "root@pam"
token_name  = "detonator"
token_value = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
verify_ssl  = false
node        = "pve"

[storage]
data_dir = "data"
db_path  = "data/detonator.db"

# One or more named agents. Runs pick by name, or fall back to the first entry.
[[agents]]
name               = "win11-sandbox"
vm_id              = "100"
snapshot           = "clean"
port               = 8000
health_timeout_sec = 60
health_poll_sec    = 2
# Optional inline stealth profile:
# stealth = { enabled = true, locale = "en-US", timezone_id = "America/Los_Angeles", viewport_width = 1920, viewport_height = 1080 }

[timeouts]
provision_sec = 120
preflight_sec = 30
detonate_sec  = 120
collect_sec   = 60
enrich_sec    = 120
filter_sec    = 30

# Orchestrator-local egress. The orchestrator is the L3 gateway — Proxmox just
# provides the L2 bridge. See docs/tether-setup.md for the USB-tether variant.
[egress.direct]
type = "direct"

[egress.direct.settings]
uplink_interface = "ens18"             # NIC with internet access
sandbox_cidr     = "192.168.100.0/24"  # Sandbox subnet (orchestrator's sandbox NIC + agent VM)
gateway          = "192.168.0.1"       # LAN gateway
lan_cidr         = "192.168.1.0/24"    # LAN subnet blocked from the sandbox

# Noise filter supplements the built-in tracker list.
[filter]
noise_domains = []
noise_resource_types = []

# Analysis: Sigma-style YAML rulepacks. Add your own rule directories.
[analysis]
modules    = ["sigma"]
rules_dirs = ["detonator/analysis/rules"]

# Enrichment plugin modules. Core enrichers (navigations, dom) always run.
[enrichment]
modules = ["whois", "dns", "tls", "favicon"]
```

See [config.example.toml](config.example.toml) for the full annotated template including tether egress and stealth overrides.

## Proxmox API Token Setup

The orchestrator authenticates to Proxmox with an API token — not a password.

1. **Datacenter → Permissions → API Tokens → Add**
   - User: `root@pam` (or a dedicated user)
   - Token ID: `detonator`
   - Uncheck **Privilege Separation** unless you want to scope permissions manually
2. Copy the token value shown at creation — it is only displayed once.
3. If using privilege separation, grant `PVEVMAdmin` on `/nodes/<node>` or narrower.

## Network Setup

The sandbox VM needs an isolated L2 bridge to the orchestrator. The orchestrator applies nftables rules on run start (MASQUERADE out the uplink, LAN-drop from the sandbox, deactivate cleanly in `finally`).

**Proxmox side** (`/etc/network/interfaces` on the Proxmox host):

```
auto vmbr1
iface vmbr1 inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0
```

Assign the sandbox VM's NIC to `vmbr1`. Assign the orchestrator VM a second NIC on the same `vmbr1` — that NIC (`ens19` by convention, `sandbox_cidr` subnet) is the sandbox-side interface the `DirectEgressProvider` masquerades through.

The orchestrator's activation code configures `net.ipv4.ip_forward`, loads the nftables table atomically, and verifies egress via ipify before the agent is contacted. See [docs/tether-setup.md](docs/tether-setup.md) for USB tether variant.

## Running the Orchestrator

```bash
source .venv/bin/activate
python -m detonator.orchestrator.api config.toml
# Optional: --json-logs for structured JSON logging
```

Listens on `0.0.0.0:8080` by default.

```bash
curl http://localhost:8080/health
# {"status":"ok","vm_provider":"proxmox","active_runs":0}
```

## Web UI

With the `ui` extra installed, the orchestrator serves a browser UI at `/ui/`:

- `/ui/` — dashboard: VM provider + agent status, recent runs, quick-submit form
- `/ui/config` — VM provider details, configured agents, egress options, enrichment module + exclusion matrix editor
- `/ui/runs` — filterable run list with live status polling (status / domain / date range)
- `/ui/runs/{id}` — run detail: state timeline, artifacts table, enrichment summary, observables, technique matches, chain stats; interactive runs expose a console URL + resume button

Jinja2 + HTMX (vendored, no build step). Active runs poll for state updates every 2s.

## Submitting a Run

```bash
curl -s -X POST http://localhost:8080/runs \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","agent":"win11-sandbox","egress":"direct","timeout_sec":120,"interactive":false}' \
  | python3 -m json.tool
```

`agent` is optional (first configured agent wins). `egress` selects a named egress block (`direct`, `tether`, etc.). Response:

```json
{"run_id": "3f8a1d2c-ab44-4e7a-b901-2f3c91e4560d", "state": "pending"}
```

### Interactive mode

`"interactive": true` pauses the browser after navigation. The run enters `interactive` and waits for a resume signal — use the VNC/SPICE console exposed in the UI or fetch `console_url` from `GET /runs/{id}` to inspect manually.

```bash
curl -X POST http://localhost:8080/runs/<run-id>/resume
```

### Run states

`pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`

Every transition is persisted with a timestamp. Partial artifacts + a partial `manifest.json` are preserved on error.

## Checking Run Status

```bash
curl -s http://localhost:8080/runs/<run-id> | python3 -m json.tool
curl "http://localhost:8080/runs?limit=10"
curl "http://localhost:8080/runs?status=complete&domain=example.com"
```

## Downloading Artifacts

```bash
# Individual file (path may include sub-directories like screenshots/)
curl -O http://localhost:8080/runs/<run-id>/artifacts/har_full.har
curl -O http://localhost:8080/runs/<run-id>/artifacts/dom.html
curl -O http://localhost:8080/runs/<run-id>/artifacts/navigations.json

# Full run as a zip (root dir is the seed domain for easy unpacking)
curl -O http://localhost:8080/runs/<run-id>/artifacts.zip
```

## Data Layout

```
data/
  detonator.db                        ← SQLite (runs, artifacts, campaigns, observables, techniques, ...)
  blobs/
    {sha256-prefix}/{sha256-rest}     ← Content-addressed blob store (deduped across runs)
  runs/
    {run-uuid}/
      har_full.har                    ← Full Playwright HAR
      har_navigation.json             ← HAR filtered to navigation scope (in-scope entries)
      filter_result.json              ← Scope / noise classification per URL
      navigations.json                ← Top-level navigation timeline (main + sub frames)
      dom.html                        ← document.documentElement.outerHTML at end of detonation
      console.json                    ← Browser console + page errors
      meta.json                       ← Serialized RunRecord (config, state, transitions)
      manifest.json                   ← Consolidated run rollup (config + artifacts + enrichment + techniques)
      screenshots/
        screenshot_{epoch}.png        ← Periodic + final screenshots
      bodies/
        manifest.jsonl                ← JSONL: one entry per captured request/response body
        {sha256}.{ext}                ← Content-addressed body files (symlinked into blobs/)
```

All files under `runs/{run-uuid}/` that are content-addressed are symlinks into `blobs/`. The `data/` directory is created on startup; set `storage.data_dir` in `config.toml` to use an absolute path.

## API Reference

### Runs

| Endpoint | Method | Description |
|---|---|---|
| `/runs` | POST | Submit a detonation run |
| `/runs` | GET | List runs (`status`, `domain`, `date_from`, `date_to`, `limit`, `offset`) |
| `/runs/{id}` | GET | Run detail + artifact manifest (and `console_url` when interactive) |
| `/runs/{id}/artifacts/{name:path}` | GET | Download one artifact (path-traversal guarded) |
| `/runs/{id}/artifacts.zip` | GET | Download all artifacts as a zip (seed domain is the archive root) |
| `/runs/{id}/resume` | POST | Resume an interactive run |
| `/runs/{id}` | DELETE | Delete run record + artifacts (blocked while active) |

### Campaigns

| Endpoint | Method | Description |
|---|---|---|
| `/campaigns` | POST | Create campaign |
| `/campaigns` | GET | List campaigns |
| `/campaigns/{id}` | GET | Campaign detail (runs, observables, techniques) |
| `/campaigns/{id}` | PUT | Update name / description / status / confidence |
| `/campaigns/{id}/runs` | POST | Associate a run (`{"run_id": "..."}`) |

### Observables & Techniques

| Endpoint | Method | Description |
|---|---|---|
| `/observables` | GET | Search by type + value pattern |
| `/observables/{id}` | GET | Detail with linked runs, outgoing/incoming links, campaigns |
| `/observables/{id}/graph` | GET | Observable neighborhood |
| `/techniques` | GET | List all techniques |
| `/techniques/{id}/matches` | GET | Runs that matched this technique |
| `/domain/{domain}/runs` | GET | Cross-run lookup: all runs that touched `{domain}` via seed URL or enrichment |

### Graph (cytoscape-compatible)

| Endpoint | Method | Description |
|---|---|---|
| `/graph/search` | GET | Search across observables / techniques / campaigns (`?q=...&limit=...`) |
| `/graph/nodes/{node_type}/{node_id}/neighbors` | GET | `{nodes, edges}` suitable for direct cytoscape ingestion |

### Enrichment exclusions

| Endpoint | Method | Description |
|---|---|---|
| `/config/enrichment/exclusions` | GET | All exclusions as `{enricher: [host, ...]}` |
| `/config/enrichment/exclusions` | POST | Add exclusion `{"enricher_name": "dns", "host_pattern": "cdn.example.com"}` |
| `/config/enrichment/exclusions` | DELETE | Remove exclusion (same body shape as POST) |

Exclusions are stored in SQLite and take effect on the next run without a restart. Matching: a host is excluded if it equals the pattern exactly *or* ends with `.<pattern>` — so `googleapis.com` suppresses `fonts.googleapis.com` too. Case-insensitive. Logic lives in `Enricher._is_host_excluded()`. Default exclusions (CDNs, cloud hosts) are seeded on first startup from `database.py`.

### System

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Orchestrator health + active-run count |
| `/config/agents` | GET | Configured agents + current VM state + active run IDs |
| `/config/egress` | GET | Configured egress options |
| `/config/vms` | GET | VM list from the provider (503 if Proxmox is unreachable) |
| `/docs` | GET | Swagger UI (auto-generated) |
| `/redoc` | GET | ReDoc (auto-generated) |
| `/ui/` | GET | Web UI (requires `ui` extra) |
