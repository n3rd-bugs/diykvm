#!/bin/bash
# Build the diykvm .deb. Works on any machine with dpkg-deb (the package is
# Architecture: all — Python deps are installed into a venv on the target at install time).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/packaging"
VERSION="$(awk -F': ' '/^Version:/{print $2}' "$PKG/DEBIAN/control")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"                 # mktemp makes 0700; the package root must be 0755
OUT="${1:-$ROOT/diykvm_${VERSION}_all.deb}"

install -d "$STAGE/DEBIAN" \
           "$STAGE/opt/kvm/app" "$STAGE/opt/kvm/images" \
           "$STAGE/usr/lib/systemd/system" \
           "$STAGE/usr/local/sbin" \
           "$STAGE/usr/lib/udev/rules.d" \
           "$STAGE/usr/share/doc/diykvm" \
           "$STAGE/etc/kvm" "$STAGE/etc/sudoers.d"

# control + maintainer scripts
install -m0644 "$PKG/DEBIAN/control"   "$STAGE/DEBIAN/control"
install -m0644 "$PKG/DEBIAN/conffiles" "$STAGE/DEBIAN/conffiles"
for s in postinst prerm postrm; do install -m0755 "$PKG/DEBIAN/$s" "$STAGE/DEBIAN/$s"; done

# application code
cp -r "$ROOT/app/." "$STAGE/opt/kvm/app/"
rm -rf "$STAGE/opt/kvm/app/__pycache__" "$STAGE/opt/kvm/app/README.md"

# systemd units
install -m0644 "$ROOT/pi/kvm-gadget.service" "$ROOT/pi/ustreamer.service" "$ROOT/pi/kvm-web.service" \
        "$STAGE/usr/lib/systemd/system/"

# gadget scripts + helpers
install -m0755 "$ROOT/pi/kvm-gadget-up.sh" "$ROOT/pi/kvm-gadget-down.sh" "$STAGE/usr/local/sbin/"
install -m0755 "$PKG/files/usr/local/sbin/kvm-conf-get" \
               "$PKG/files/usr/local/sbin/kvm-ustreamer" \
               "$PKG/files/usr/local/sbin/kvm-mkimage" \
               "$PKG/files/usr/local/sbin/kvm-msd-helper" "$STAGE/usr/local/sbin/"

# udev rule, default config, and the sudoers drop-in (0440) for the privileged helper
install -m0644 "$PKG/files/lib/udev/rules.d/99-kvm-hidg.rules" "$STAGE/usr/lib/udev/rules.d/"
install -m0644 "$PKG/files/etc/kvm/kvm.conf" "$STAGE/etc/kvm/kvm.conf"
install -m0440 "$PKG/files/etc/sudoers.d/diykvm" "$STAGE/etc/sudoers.d/diykvm"
install -m0644 "$PKG/files/usr/share/doc/diykvm/copyright" "$STAGE/usr/share/doc/diykvm/copyright"

dpkg-deb --root-owner-group --build "$STAGE" "$OUT"
echo "built: $OUT"
