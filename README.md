# Detonator — Host Orchestrator Setup

The host orchestrator receives detonation requests, manages the sandbox VM lifecycle via Proxmox, drives the in-VM agent, collects and stores artifacts, and exposes a REST API for interacting with runs, campaigns, and observables.

This guide covers setting up the orchestrator on the Linux host that has Proxmox access and controls the detonation network.

## Host Requirements

- **OS**: Linux (the orchestrator makes direct use of the host network stack for isolation in Phase 3; other distros work for Phases 0–2)
- **Python**: 3.11 or later, 64-bit
- **Proxmox VE**: accessible from the host, with API token credentials
- **Network bridge**: an isolated bridge (e.g. `vmbr1`) in Proxmox hosting the sandbox VM's NIC — see [Network Setup](#network-setup) below

## Installation

Clone or copy the repository to the host, then create a virtualenv and install:

```bash
cd /opt/detonator          # or wherever you placed the repo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[proxmox,enrichment]"
```

The `proxmox` extra adds `proxmoxer` (Proxmox API client). The `enrichment` extra adds `dnspython`, `cryptography`, and `mmh3`. Omit `enrichment` if you are only running Phases 0–2 (enrichment pipeline is Phase 4).

Verify the install:

```bash
python -c "from detonator.config import load_config; print('ok')"
```

## Configuration

Copy the example config and edit it:

```bash
cp config.example.toml config.toml
$EDITOR config.toml
```

### `config.toml` reference

```toml
log_level = "INFO"           # DEBUG, INFO, WARNING, ERROR

# VM to use when a run doesn't specify one explicitly.
default_vm_id = "100"
default_snapshot = "clean"

[vm_provider]
type = "proxmox"

[vm_provider.settings]
host        = "192.168.1.10"       # Proxmox host IP or hostname
port        = 8006                 # Proxmox API port (default 8006)
user        = "root@pam"           # API token owner
token_name  = "detonator"          # Token ID (see Proxmox token setup below)
token_value = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
verify_ssl  = false                # Set true if Proxmox has a valid cert
node        = "pve"                # Proxmox node name

[storage]
data_dir = "data"               # Artifacts land here: data/runs/{run-uuid}/
db_path  = "data/detonator.db" # SQLite database

[agent]
port              = 8000   # The port the in-VM agent listens on
health_timeout_sec = 60    # How long to wait for the agent to become healthy after VM start
health_poll_sec    = 2     # Polling interval during health wait

[timeouts]
provision_sec = 120   # VM revert + start
preflight_sec = 30    # Pre-flight checks (stub in Phase 2; real work in Phase 3)
detonate_sec  = 120   # Browser detonation timeout; overridden per-run by timeout_sec
collect_sec   = 60    # Artifact download from the agent
enrich_sec    = 120   # Enrichment pipeline (stub in Phase 2; real work in Phase 4)

# Egress entries define named routing paths for detonation traffic.
# Phase 3 will automate nftables setup for each; for now these are
# read by the API at GET /config/egress but not enforced in the network.
[egress.direct]
type = "direct"

[egress.direct.settings]
bridge  = "vmbr1"
gateway = "192.168.1.1"

# Optional: VPN and tether entries follow the same pattern.
# See config.example.toml for examples.

enrichment_modules = ["whois", "dns", "tls", "favicon"]
```

## Proxmox API Token Setup

The orchestrator authenticates to Proxmox with an API token — not a password. Create one in the Proxmox web UI:

1. **Datacenter → Permissions → API Tokens → Add**
   - User: `root@pam` (or a dedicated user)
   - Token ID: `detonator`
   - Uncheck **Privilege Separation** unless you want to scope permissions manually
2. Copy the token value shown at creation — it is only displayed once.
3. If using privilege separation, grant the token permissions on the node, storage, and VM:
   - Datacenter → Permissions → Add → API Token Permission
   - Path: `/` (or narrow to `/nodes/pve`, `/vms/100`)
   - Role: `PVEVMAdmin` (covers revert, start, stop, snapshot, QEMU agent)

Put the token ID and value in `config.toml` under `[vm_provider.settings]`.

## Network Setup

The sandbox VM needs an isolated bridge — traffic from that bridge must not reach the host LAN uncontrolled.

**Minimal setup (direct egress, manual routing):**

In the Proxmox web UI or `/etc/network/interfaces` on the Proxmox host:

```
auto vmbr1
iface vmbr1 inet static
    address 192.168.100.1/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up echo 1 > /proc/sys/net/ipv4/ip_forward
    post-up iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o <wan-interface> -j MASQUERADE
    post-down iptables -t nat -D POSTROUTING -s 192.168.100.0/24 -o <wan-interface> -j MASQUERADE
```

Assign the VM's NIC to `vmbr1`. The VM gets an IP on `192.168.100.0/24`; the orchestrator reaches it from the host at that IP. The agent API port (8000) is only reachable on this bridge — it is not exposed to the host LAN.

> **Phase 3 note:** Automated nftables-based isolation (whitelisting the egress path, blocking LAN access, tearing down rules post-run) is not yet implemented. The above is a manual baseline that is sufficient for Phases 0–2.

## Running the Orchestrator

```bash
source .venv/bin/activate
python -m detonator.orchestrator.api config.toml
```

The API listens on `0.0.0.0:8080` by default. Confirm it is running:

```bash
curl http://localhost:8080/health
# {"status":"ok","vm_provider":"proxmox","active_runs":0}
```

## Submitting a Run

```bash
curl -s -X POST http://localhost:8080/runs \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","timeout_sec":120,"interactive":false}' \
  | python3 -m json.tool
```

Response:
```json
{"run_id": "3f8a1d2c-ab44-4e7a-b901-2f3c91e4560d", "state": "pending"}
```

### Interactive mode

Set `"interactive": true` to pause the browser after navigation. The run enters the `interactive` state and waits for a resume signal. Use this to manually inspect the page or interact with it via VNC before artifact collection.

```bash
# Resume an interactive run
curl -X POST http://localhost:8080/runs/<run-id>/resume
```

### Run states

`pending → provisioning → preflight → detonating → [interactive] → collecting → enriching → filtering → complete | error`

Every transition is persisted with a timestamp. Partial artifacts are preserved on error.

## Checking Run Status

```bash
curl -s http://localhost:8080/runs/<run-id> | python3 -m json.tool
```

The response includes the run record and a list of all collected artifacts from the `artifacts` table.

```bash
# List recent runs
curl "http://localhost:8080/runs?limit=10"

# Filter by status
curl "http://localhost:8080/runs?status=complete"
```

## Downloading Artifacts

### Individual file

```bash
curl -O http://localhost:8080/runs/<run-id>/artifacts/har_full.har
curl -O http://localhost:8080/runs/<run-id>/artifacts/dom.html
curl -O "http://localhost:8080/runs/<run-id>/artifacts/screenshots/screenshot_1744567890.png"
```

### Full run as a zip

Downloads all artifacts as a zip archive. The domain name is used as the root directory inside the archive, preserving the full hierarchy:

```bash
curl -O http://localhost:8080/runs/<run-id>/artifacts.zip
# Saves: example.com_3f8a1d2c.zip
# Extracts to: example.com/har_full.har
#              example.com/dom.html
#              example.com/console.json
#              example.com/screenshots/screenshot_*.png
#              example.com/meta.json
```

## Data Layout

```
data/
  detonator.db                  ← SQLite (runs, artifacts, campaigns, observables)
  runs/
    {run-uuid}/
      har_full.har              ← Full Playwright HAR
      dom.html                  ← document.documentElement.outerHTML
      console.json              ← Browser console + page errors
      meta.json                 ← Serialized RunRecord (config, state, transitions)
      screenshots/
        screenshot_{epoch}.png  ← Periodic + final screenshots
      enrichment/               ← Enrichment outputs (Phase 4)
```

The `data/` directory is created on startup relative to the working directory. Set `data_dir` in `config.toml` to use an absolute path.

## API Reference

### Runs

| Endpoint | Method | Description |
|---|---|---|
| `/runs` | POST | Submit a detonation run |
| `/runs` | GET | List runs (`?status=`, `?limit=`, `?offset=`) |
| `/runs/{id}` | GET | Run detail + artifact manifest |
| `/runs/{id}/artifacts/{name:path}` | GET | Download one artifact |
| `/runs/{id}/artifacts.zip` | GET | Download all artifacts as a zip |
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
| `/observables/{id}` | GET | Detail with linked runs |
| `/observables/{id}/graph` | GET | Observable neighborhood (links + campaigns) |
| `/techniques` | GET | List all techniques |
| `/techniques/{id}/matches` | GET | Runs that matched this technique |

### System

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Orchestrator health + active-run count |
| `/config/egress` | GET | Configured egress options |
| `/config/vms` | GET | VM list from the provider (503 if Proxmox is unreachable) |
