# USB Tether Egress — Host Setup Guide

This guide covers the one-time host configuration needed before
`TetherEgressProvider` can route sandbox traffic through a USB-tethered iPhone.
The provider itself is minimal — it assumes the tether interface already has an
IPv4 lease when `activate()` is called.  Everything here is operator setup.

---

## 1. Proxmox USB Passthrough

The orchestrator runs inside a Proxmox VM.  USB passthrough is required so the
iPhone's `ipheth` interface appears inside the VM rather than on the Proxmox
host.

Pin the device by vendor/product ID so it survives re-plug:

```
Apple iPhone — USB ID: 05ac:12a8
```

In the Proxmox web UI: VM → Hardware → Add → USB Device → Use USB Vendor/Device
ID → enter `05ac:12a8`.  Alternatively, in the VM config file:

```
usb0: host=05ac:12a8
```

Pinning by vendor:product (not by port) ensures the same device is passed
through regardless of which USB port it is connected to.

---

## 2. Kernel Module and usbmuxd

Two components must be present on the orchestrator VM:

**`ipheth` kernel module** — exposes the iPhone as a network interface.  Shipped
with stock kernels (5.x+); verify it is loaded:

```sh
lsmod | grep ipheth
# If absent:
modprobe ipheth
```

To load it automatically at boot:

```sh
echo ipheth | sudo tee /etc/modules-load.d/ipheth.conf
```

**`usbmuxd`** — multiplexes USB traffic to/from the phone; required for `ipheth`
pairing.  Install and enable it:

```sh
apt install usbmuxd
systemctl enable --now usbmuxd
```

---

## 3. One-Time Trust Pairing

The first time the phone is connected to this host, iOS shows a
**"Trust This Computer?"** prompt on the phone's screen.

- The phone must be **unlocked** when you plug it in.
- Tap **Trust** on the phone.
- This is per-host — if the orchestrator VM is rebuilt you must pair again.

After pairing, `usbmuxd` handles re-authentication automatically on subsequent
plug-ins as long as the phone trusts the host certificate (stored by usbmuxd).

---

## 4. systemd-networkd: DHCP Without Stealing the Default Route

The iPhone's Personal Hotspot DHCP server assigns addresses from
`172.20.10.0/28` to the host interface.  Without configuration, the phone's
DHCP lease may advertise a default route that overwrites the host's LAN gateway
— breaking all non-sandbox traffic.

Drop the following unit at `/etc/systemd/network/50-iphone-tether.network`:

```ini
[Match]
Name=enx*

[Network]
DHCP=ipv4
IPv6AcceptRA=no

[DHCPv4]
UseDNS=no
UseRoutes=no
RouteMetric=1000
```

- `UseRoutes=no` — prevents the phone's advertised route from replacing the
  host's default route.
- `RouteMetric=1000` — gives the lease a high metric so it is never preferred
  over the LAN default route for host traffic.
- The `enx*` glob matches any `ipheth` interface regardless of MAC-derived
  suffix.  If you have other `enx*` interfaces that should not run DHCP from
  this unit, narrow the match with the full interface name.

Apply the unit:

```sh
systemctl restart systemd-networkd
# Then re-plug the phone (or: ip link set enxea98eebb97c7 down && ip link set enxea98eebb97c7 up)
```

---

## 5. Verify Before Running the Provider

`TetherEgressProvider.preflight_check()` will fail fast with a clear message if
any of the following checks fail — but it is faster to confirm them manually
before submitting a run.

Replace `enxea98eebb97c7` with the actual interface name on your host
(`ip -br link` to list interfaces; the `ipheth` one will match `enx*`).

**Interface is up and has a carrier:**

```sh
networkctl status enxea98eebb97c7
# Expected: State: carrier (or routable)
```

**IPv4 lease from the phone:**

```sh
ip -br addr show enxea98eebb97c7
# Expected: enxea98eebb97c7  UP  172.20.10.2/28
```

**Traffic actually exits via the tether interface:**

```sh
curl --interface enxea98eebb97c7 https://api.ipify.org
# Expected: a carrier IP address, distinct from your LAN's public IP
```

If `ip -br link` shows `NO-CARRIER` despite the phone being unlocked with
Personal Hotspot enabled, the problem is either:

- usbmuxd is not running (`systemctl status usbmuxd`)
- The phone has not been trusted on this host (re-plug with the phone unlocked
  and tap Trust on the phone screen)
- The USB passthrough is not configured in Proxmox

The provider cannot resolve any of these — they are host/phone configuration
issues.
