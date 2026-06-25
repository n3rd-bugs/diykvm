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

## Design note — one HID interface
Two *separate* HID functions (keyboard + mouse) on the Pi's **dwc2** controller make the host
re‑enumerate the device every ~10 s (unusable). Keyboard and mouse are therefore collapsed into a
**single HID interface multiplexed by Report IDs**, which uses one interrupt endpoint and is stable.
The host still exposes both a keyboard and a mouse. Mass storage uses bulk endpoints and coexists fine.

## Report protocol — write to `/dev/hidg0`
- **Keyboard** — Report ID 1, 9 bytes: `01 <mods> 00 <k1..k6>`
- **Absolute mouse** — Report ID 2, 7 bytes: `02 <buttons> <Xlo Xhi> <Ylo Yhi> <wheel>` (X/Y 0..32767 LE)
- **Relative mouse** — Report ID 3, 5 bytes: `03 <buttons> <dx> <dy> <wheel>` (signed)
- buttons: bit0 = left, bit1 = right, bit2 = middle.

```sh
# type 'a'
printf '\x01\x00\x00\x04\x00\x00\x00\x00\x00' | sudo tee /dev/hidg0 >/dev/null
printf '\x01\x00\x00\x00\x00\x00\x00\x00\x00' | sudo tee /dev/hidg0 >/dev/null
# move the absolute pointer to screen centre
printf '\x02\x00\xff\x3f\xff\x3f\x00' | sudo tee /dev/hidg0 >/dev/null
```

## Notes
- The USB‑C port carries data only; power the Pi independently (PoE or 5 V) so the port is free.
- The absolute pointer maps to the target's **primary** display; use relative moves for multi‑monitor.
- Gadget id: VID `0x1d6b` / PID `0x0107`, serial `kvm-0001`.
