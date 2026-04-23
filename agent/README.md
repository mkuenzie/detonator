# Detonator Agent — VM Image Setup

The agent runs inside the sandbox VM and exposes a REST API on port 8000 for the host orchestrator to trigger URL detonation and retrieve artifacts (HAR, screenshots, DOM, console logs).

This guide covers preparing a base VM image that the orchestrator can snapshot and revert between runs.

## VM Requirements

- **OS**: Windows 10 or 11 (Pro recommended for RDP/remote management). Linux support is planned for a future release.
- **Desktop**: Standard Windows desktop. The browser runs headed for v1 — there is no headless mode.
- **Python**: 3.11 or later, **64-bit** (install from [python.org](https://www.python.org/downloads/)).
- **Disk**: 40 GB minimum (Windows base + Python + Chromium).
- **Network**: Single NIC on the isolated detonation bridge (e.g. `vmbr1`). The host controls routing and egress.
- **Proxmox guest tools**: Install the [VirtIO drivers](https://pve.proxmox.com/wiki/Windows_VirtIO_Drivers) and QEMU guest agent for network info and clean shutdown support.

## Installation

### 1. Install Python

Download and run the **64-bit** Python 3.11+ installer from python.org. During installation:

- Check **"Add python.exe to PATH"**.
- Choose **"Customize installation"** and ensure pip is included.

Verify in PowerShell:

```powershell
python --version
pip --version
```

### 2. Create a dedicated local user

Create a standard (non-admin) local account for the agent:

```powershell
net user detonator <password> /add
```

Log in as this user for the remaining steps, or prefix commands with `runas /user:detonator`.

### 3. Deploy the agent code

Copy the `agent/` directory from this repository to the VM. For example, using SCP from the host (requires OpenSSH on the Windows VM):

```powershell
# From the host:
scp -r agent/ detonator@<vm-ip>:C:/Users/detonator/agent/
```

Or use a shared folder, RDP file transfer, or any other method to place the files at `C:\Users\detonator\agent\`.

### 4. Install Google Chrome

The agent drives a **real Google Chrome install** (not the Playwright-bundled Chromium) to avoid the build-fingerprint and user-agent tells that cloakers check.

Download and install the latest stable Chrome for Windows from the official source. The installer places Chrome at `C:\Program Files\Google\Chrome\Application\chrome.exe`, which is the path Playwright's `channel="chrome"` resolves to.

If you prefer to keep using the bundled Chromium (e.g. for testing or environments without a Chrome license), set `stealth.enabled = false` in your agent config — this skips the `channel="chrome"` option and falls back to bundled Chromium.

### 5. Set up the Python environment

Open PowerShell as the `detonator` user:

```powershell
cd C:\Users\detonator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install fastapi uvicorn playwright patchright msvc-runtime
playwright install chromium
patchright install chrome
```

`playwright install chromium` installs the Playwright-managed Chromium fallback (used when `stealth.enabled = false` or when the Patchright driver is switched off). The real Chrome install from step 4 is used by default via Patchright.

`patchright install chrome` installs Patchright's patched Chrome binaries, which patch the `Runtime.enable` CDP leak and several other fingerprint vectors that anti-bot systems check.

> **Note:** `msvc-runtime` is required because `playwright`'s `greenlet` dependency (2.0+) links against the Microsoft Visual C++ runtime, which is not present on a fresh Windows install. Without it, `import greenlet` fails with `ImportError: DLL load failed while importing _greenlet: The specified module could not be found`. `msvc-runtime` drops the needed DLLs into the venv — no system-wide VC++ Redistributable installer required.

### Switching browser drivers

The active driver is controlled by a single constant in `agent/browser/_driver.py`:

```python
_DRIVER = "patchright"   # default — stealth-hardened, passes Cloudflare/DataDome
# _DRIVER = "playwright"  # vanilla upstream — useful when debugging a Patchright-specific issue
```

To revert to vanilla Playwright:
1. Change `_DRIVER = "playwright"` in `agent/browser/_driver.py`.
2. Re-deploy the agent code to the VM (no reinstall needed — both packages are already installed).

Every run records which driver was active in `DetonationResult.meta["browser_driver"]`, so a Patchright regression is answerable from run history alone.

### 5. Verify the agent starts

```powershell
cd C:\Users\detonator
.\.venv\Scripts\Activate.ps1
python -m agent.config 0.0.0.0 8000
```

From the host, confirm the health endpoint responds:

```bash
curl http://<vm-ip>:8000/health
# Expected: {"status":"ok","browser":"playwright_chromium"}
```

Stop the agent with `Ctrl+C` once verified.

## Auto-Start on Login

The agent must start after the desktop is available (headed browser requirement). Use a Scheduled Task triggered at user logon.

### Option A: Create via PowerShell

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Users\detonator\.venv\Scripts\python.exe" `
    -Argument "-m agent.config 0.0.0.0 8000" `
    -WorkingDirectory "C:\Users\detonator"

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "detonator"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "DetonatorAgent" `
    -Action $action -Trigger $trigger -Settings $settings `
    -User "detonator" -RunLevel Limited
```

### Option B: Startup folder shortcut

Place a shortcut in the `detonator` user's Startup folder:

```
Shell:startup → C:\Users\detonator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup
```

Create a shortcut pointing to:

```
C:\Users\detonator\.venv\Scripts\python.exe -m agent.config 0.0.0.0 8000
```

Set **"Start in"** to `C:\Users\detonator`.

### Enable auto-logon

The `detonator` user must be logged in for the headed browser to work. Configure auto-logon so the VM boots directly to the desktop:

```powershell
$regPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $regPath -Name "AutoAdminLogon" -Value "1"
Set-ItemProperty -Path $regPath -Name "DefaultUserName" -Value "detonator"
Set-ItemProperty -Path $regPath -Name "DefaultPassword" -Value "<password>"
```

## Taking the Clean Snapshot

Once everything is installed, auto-logon is configured, and the agent starts on boot:

1. Reboot the VM and confirm the agent is reachable at `http://<vm-ip>:8000/health` without manual intervention.
2. Shut down the VM cleanly.
3. In Proxmox (or your hypervisor), take a snapshot named `clean`.
4. Register the VM as a named agent in your host `config.toml`:

```toml
[[agents]]
name     = "win11-sandbox"
vm_id    = "100"
snapshot = "clean"
port     = 8000
```

The orchestrator will revert to this snapshot before every detonation run. Declare additional `[[agents]]` entries for more sandbox VMs.

## Agent API Reference

| Endpoint                      | Method | Description                          |
|-------------------------------|--------|--------------------------------------|
| `/health`                     | GET    | Readiness probe                      |
| `/detonate`                   | POST   | Start a detonation (`{url, timeout_sec, wait_for_idle, interactive}`) |
| `/status`                     | GET    | Current run state                    |
| `/resume`                     | POST   | Resume after interactive pause       |
| `/artifacts`                  | GET    | List available artifact files        |
| `/artifacts/{artifact_name}`  | GET    | Download a specific artifact file    |

## Security Notes

- The agent listens on `0.0.0.0:8000` inside the VM. The host's nftables rules ensure this port is only reachable from the orchestrator on the isolated bridge — it is never exposed to the LAN.
- The agent has no authentication. Isolation is enforced at the network layer by the host.
- The VM is stateless. Every run starts from the clean snapshot. Any malware that executes during detonation is destroyed on revert.
- Auto-logon stores the password in the registry in cleartext. This is acceptable because the VM is ephemeral and network-isolated — it exists only to be detonated and reverted.
