# Kernel-module tweaks

An optional DKMS module (`diykvm-userial`) for the USB gadget serial. Build it on the Pi (`dkms`,
`linux-headers-$(uname -r)`, `curl`); it falls back to the stock `u_serial` if it can't build, and
`dkms remove` restores stock.

It patches `u_serial` two ways: a
**[re-enumeration self-heal](#gadget-serial-out-endpoint-dies-out-after-a-re-enumeration)**
(`survive_reenum` + `rearm_ms`) so the serial OUT endpoint doesn't "die out" after a USB re-enumerate, and a
**[deeper RX/TX request queue](#gadget-serial-out-queue-depth-u_serial-queue_size)** (`queue_size`). The
self-heal **is the fix for the "endpoint dies out" stall** (verified on a Pi 4B, kernel 6.12.25).

> An earlier `dwc2` "disable USB2 LPM" module was investigated and **dropped**: the BCM2711 dwc2 core has no
> LPM in hardware (`GHWCFG3` bit15=0, so `params.lpm`=0), so disabling LPM is a no-op — the real trigger is
> **re-enumeration**, handled by the self-heal above.

---

# Gadget serial OUT-endpoint "dies out" after a re-enumeration

**Symptom (the original "DWC death").** While a bulk-OUT endpoint is under load and the gadget
**re-enumerates** (USB reset / UDC rebind — which happens on this device), the OUT endpoint is left
**un-rearmed**: its request queue drains to empty and is never refilled, so the endpoint NAKs every transfer
and the interface goes dead. It recovers only on a port **close+reopen** (what the `kvm-web` serial
auto-reopen does) — or, for mass storage, a full re-enumerate. Confirmed on a Pi 4B (6.12.25): forcing a
re-enumerate during a serial bulk-OUT burst froze `ttyGS0` RX and wedged the CDC OUT endpoint (`ep2out`
`DOEPCTL.EPENA=0`) until reopen.

**Root cause.** On disconnect, `u_serial`'s `gserial_disconnect()` calls `tty_hangup()`, which zeroes
`port.count`; on reconnect, `gserial_connect()` re-arms the OUT queue **only if `port.count`** is non-zero, so
an open port that was hung up never re-arms. (This is *not* LPM — LPM is disabled on this silicon.)

**The fix — `u_serial` re-enumeration self-heal (DKMS).** The `diykvm-userial` module (below) adds two knobs:

| Module param | Default | What it does |
|---|---|---|
| `survive_reenum` | `1` | Skip the `tty_hangup` on a transient gadget disconnect, so an **open** `ttyGS` port keeps its `port.count` across a re-enumerate and `gserial_connect()` re-arms it transparently. |
| `rearm_ms` | `250` | A watchdog that, every `rearm_ms`, kicks each open+connected port's `push` work (`gs_rx_push → gs_start_rx`) to **top up the OUT queue** — a cause-agnostic backstop if the immediate re-arm raced the re-enumerate. `0` disables it. Read-only (`0444`): set at module load (a modprobe option), not at runtime. |

Verify (on the Pi):
```sh
cat /sys/module/u_serial/parameters/survive_reenum   # Y
cat /sys/module/u_serial/parameters/rearm_ms         # 250
# Drive a serial bulk-OUT burst to the target COM port while forcing re-enumerates
# (kvm-gadget-helper reenumerate): RX keeps flowing across each re-enumerate instead of freezing.
```

> **Note — mass storage (`ep1out`).** The same family of failure hits the mass-storage OUT endpoint under a
> re-enumerate (there it can surface as `dwc2_hsotg_ep_stop_xfr: timeout GOUTNAKEFF`). `f_mass_storage` has no
> tty/`port.count`, so this `u_serial` fix does not cover it; that path still relies on a re-enumerate to
> recover. See the report for next steps.

---

# Gadget serial OUT queue depth (`u_serial` QUEUE_SIZE)

Optional kernel-module tweak for the gadget CDC-ACM serial. Raises how many bulk-OUT (target→Pi) USB
requests are pre-queued, so a back-to-back High-Speed bulk-OUT burst doesn't NAK-stall. Most setups do
**not** need it — see "Do you need this?".

## Is it a hardware limit?

**No.** The stall is a software default in `drivers/usb/gadget/function/u_serial.c`:

```c
#define QUEUE_SIZE  16      /* RX and TX queues buffer QUEUE_SIZE packets before they hit the FIFO */
```

Only 16 OUT requests are pre-allocated and queued; `gs_read_complete()` re-queues each after copying it to
the tty. A sustained back-to-back HS bulk-OUT burst consumes the 16-deep pool faster than the re-queue path
(alloc + DMA-map + submit) can refill it, and the endpoint NAKs once the pool is empty.

The hardware sustains a much deeper queue — each extra request is just one MaxPacket (512 B HS) of host RAM:

| Resource (Pi 4, dwc2 `fe980000.usb`) | Value | Relevance |
|---|---|---|
| RX FIFO (`g-rx-fifo-size`) | 558 words (~2.2 KB) | HW staging, DMA'd into the SW request pool — not the bottleneck for a burst |
| Endpoints | 8 (gadget uses ~7: acm, hid×2, msc) | limits adding **functions**, not queue depth |
| SPRAM total | ~4080 words (~3662 allocated) | FIFO partition headroom; unrelated to request count |
| Queue @ 256 | 256 × 512 B × 2 (rx+tx) = **256 KB** host RAM | trivial |
| 87 × 80 B handshake | ~7 KB | fits in the first ~14 of 256 requests |

So raising `QUEUE_SIZE` is a software change; the dwc2 FIFO + DMA already support it.

## The fix — a DKMS module (shipped + installer-managed)

The package ships a DKMS module at `/usr/src/diykvm-userial-1.0/` (source in this repo:
`packaging/files/usr/src/diykvm-userial-1.0/`). Its build step fetches the `u_serial` source **matching the
running kernel** from raspberrypi/linux, rewrites the `QUEUE_SIZE` `#define` into a module parameter
`queue_size` (default 256), and builds `u_serial.ko`. DKMS installs it into `updates/dkms/`, which depmod
prefers over the in-tree module, and **rebuilds it automatically on every kernel update** (`AUTOINSTALL`).

The installer (`postinst`) runs this best-effort when `dkms` + matching `linux-headers` + `curl` are present
(all `Recommends`). If they're missing or the build fails (e.g. no internet), the stock `u_serial` is used —
serial still works, just at the default depth (16). The deeper depth takes effect **after a reboot** (when
the gadget reloads `u_serial`).

Verify (on the Pi):
```sh
modinfo u_serial | grep filename                  # .../updates/dkms/u_serial.ko.xz   <- the patched one
cat /sys/module/u_serial/parameters/queue_size    # 256
```

### Manual / ad-hoc build
`build-u_serial-queue.sh` does the same fetch+patch+build standalone (no DKMS), leaving an unloaded
`u_serial.ko` for inspection:
```sh
sudo apt install -y linux-headers-$(uname -r) build-essential curl
./build-u_serial-queue.sh rpi-6.12.y 256
```

### Tune the depth
Default is 256. Override with a modprobe option **only when the patched module is installed** — the stock
module has no such parameter and would refuse to load:
```sh
echo 'options u_serial queue_size=512' | sudo tee /etc/modprobe.d/diykvm-u_serial.conf
sudo reboot
```

### Recover (restore the stock module)
```sh
sudo dkms remove -m diykvm-userial -v 1.0 --all
sudo depmod -a && sudo reboot
```

## Do you need this?

Usually **no**. If the sender is rate-limited (≤ the re-queue rate) it stays within the 16-deep pool and a
single boot completes the handshake with **Change 1** alone (serial TX binding on first enumeration — see
`~/feedback/`). Use the deeper queue only when you need a **full-speed** bulk-OUT burst. It replaces a core
gadget module: if a build is ever broken, `dkms remove` (above) restores the stock module.
