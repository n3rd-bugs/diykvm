# Pi side — USB gadget + services

System pieces installed by the package (or by hand). They turn the Pi's USB‑C/OTG port into a
composite USB device the target sees as a **keyboard + mouse + mass‑storage** drive, and run the
streamer and web app.

## Files
- `kvm-gadget-up.sh` / `kvm-gadget-down.sh` → `/usr/local/sbin/` — create / tear down the configfs gadget.
- `kvm-gadget.service`, `ustreamer.service`, `kvm-web.service` → systemd units.

## Boot config (one‑time)
The package adds this to `/boot/firmware/config.txt` (reboot required) to put the USB‑C/OTG port in
peripheral mode:
```
dtoverlay=dwc2,dr_mode=peripheral
```

## Design note — three HID interfaces (BIOS/UEFI‑safe, Linux‑target‑safe)
The keyboard is a USB **boot keyboard** (subclass=1, protocol=1, an 8‑byte report with **no Report
ID**) on its own HID function, so it works in the target's **BIOS/UEFI** firmware — which speaks only
the HID boot protocol and cannot parse Report IDs. The **absolute** pointer (Report ID 2) and the
**relative** pointer (Report ID 3) are two further, **separate** HID functions. They used to be two
collections multiplexed on one interface — which Windows splits into two devices, but **Linux**
(hid‑generic) merges into a single input device and silently **drops the second collection's
buttons**, so relative‑mode clicks never arrived on Linux targets. As separate interfaces every OS
binds two complete mice. Multiple HID functions on the Pi's **dwc2** controller would normally make
the host re‑enumerate the device every ~10 s; that is avoided by making each HID function
**IN‑endpoint‑only** (`no_out_endpoint=1`), so the HID functions plus mass storage stay within the
dwc2 endpoint budget and enumerate once, stably. Keyboard LEDs arrive via SET_REPORT on EP0. Mass
storage uses bulk endpoints and coexists fine.

## Report protocol
- **Keyboard** — write to `/dev/hidg0` (boot keyboard, **no Report ID**, 8 bytes): `<mods> 00 <k1..k6>`
- **Absolute mouse** — write to `/dev/hidg1`, Report ID 2, 7 bytes:
  `02 <buttons> <Xlo Xhi> <Ylo Yhi> <wheel>` (X/Y 0..32767 LE)
- **Relative mouse** — write to `/dev/hidg2`, Report ID 3, 5 bytes: `03 <buttons> <dx> <dy> <wheel>` (signed)
- buttons: bit0 = left, bit1 = right, bit2 = middle.

```sh
# type 'a' (8-byte boot report, no Report ID)
printf '\x00\x00\x04\x00\x00\x00\x00\x00' | sudo tee /dev/hidg0 >/dev/null
printf '\x00\x00\x00\x00\x00\x00\x00\x00' | sudo tee /dev/hidg0 >/dev/null
# move the absolute pointer to screen centre (Report ID 2, on hidg1)
printf '\x02\x00\xff\x3f\xff\x3f\x00' | sudo tee /dev/hidg1 >/dev/null
# left-click where the cursor is, on the relative mouse (Report ID 3, on hidg2)
printf '\x03\x01\x00\x00\x00' | sudo tee /dev/hidg2 >/dev/null
printf '\x03\x00\x00\x00\x00' | sudo tee /dev/hidg2 >/dev/null
```

## Target power & KVM switch (GPIO)
Two optional GPIO features, both driven **unprivileged** through the `gpio` group (udev gives
`/dev/gpiochip*` and `/dev/gpiomem` group `gpio`, mode 0660) — no sudo. Configure them on the **Config**
page (or in `/etc/kvm/kvm.conf`); nothing is ever driven unless you enable it and an authenticated
request asks for it.

- **`[power]`** — connect/cut each target's power. Wire one GPIO per target to a relay that switches its
  power, and list them as `targets = Label:BCMpin, …` (plus `active_low` for relays that are on when
  driven low, or `open_drain` to *sink to ground / release high‑Z* instead of ever driving the line high
  — handy for 5 V relay/opto inputs). The level is **latched** with `pinctrl` (register-level, so it
  survives an app restart and nothing is power-cycled on restart). The UI shows On/Off per target.
- **`[kvmswitch]`** — drive an external hardware KVM switch (display + USB) between targets. Wire a GPIO
  to each of its select buttons, list them as `ports = Label:BCMpin, …`, and the UI shows one button per
  target that **pulses** the line (`gpioset`, `pulse_ms`) to press that select button.

## Notes
- The USB‑C port carries data only; power the Pi independently (PoE or 5 V) so the port is free.
- The absolute pointer maps to the target's **primary** display; use relative moves for multi‑monitor.
- Gadget id: VID `0x1d6b` / PID `0x0108` (or `0x0109` when `usb_serial` is on — the PID tracks the
  interface set), serial `kvm-0001`.
