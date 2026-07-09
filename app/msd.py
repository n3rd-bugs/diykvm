"""Mass Storage Drive (MSD) manager for the DIY PiKVM.

Two ways to give the target USB media:
  * an image LIBRARY under /opt/kvm/images — upload whole disk images / ISOs, list, select and
    delete them; the selected image backs the gadget LUN (ISOs are exposed read-only as a CD-ROM); or
  * the built-in editable EFI drive (drive.img), whose ESP can be loop-mounted on the Pi for file edits.

Safe mutual-exclusion model — the backing image is EITHER:
  * ATTACHED to the target (gadget lun.0/file = image path; Pi does NOT mount it), or
  * MOUNTED ON THE PI for editing (lun.0/file = ""; the EFI image loop-mounted at MNT).
Never both — that would corrupt the filesystem. File operations are only allowed while mounted on
the Pi (i.e., detached from the target). The privileged transitions go through kvm-msd-helper.
"""
import os
import re
import threading
import subprocess

IMAGE = "/opt/kvm/images/drive.img"
IMAGES_DIR = "/opt/kvm/images"        # ROOT-owned image library (the app reads it; the helper writes it)
NAME_PTR = "/run/kvm/msd-name"        # app writes the image name for the next attach/import/delete op
ALLOWED_EXT = (".img", ".iso", ".bin", ".raw")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LUN_FILE = "/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0/file"
MNT = "/run/kvmmnt"
HELPER = "/usr/local/sbin/kvm-msd-helper"


class MSDError(Exception):
    pass


def _locked(fn):
    """Serialize a method under the instance lock so attach/detach/file-ops never race
    (a race could mount the image on the Pi and the target at once -> FAT corruption)."""
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper


class MSD:
    def __init__(self, image=IMAGE, lun=LUN_FILE, mnt=MNT):
        self.image = image
        self.lun = lun
        self.mnt = mnt
        self._lock = threading.RLock()

    # ---- state ----
    def _lun_path(self) -> str:
        try:
            with open(self.lun) as f:
                return f.read().strip()
        except OSError:
            return ""

    def is_attached(self) -> bool:
        return self._lun_path() != ""

    def is_mounted(self) -> bool:
        return os.path.ismount(self.mnt)

    def _attached_name(self):
        p = self._lun_path()
        return os.path.basename(p) if p else None

    def status(self) -> dict:
        # NOT @_locked on purpose: this is a read-only advisory snapshot (lun.0/file is world-readable and
        # the mount is a stat) called every poll by the events loop + on every /ws/events connect. Taking
        # the RLock made it block for the ENTIRE duration of a multi-GB image upload (save_image_upload holds
        # the lock across its whole streaming loop), stalling the poller/snapshot. A torn read here is
        # harmless. The privileged attach/detach transitions stay serialized by their own @_locked.
        attached = self.is_attached()
        mounted = self.is_mounted()
        size = os.path.getsize(self.image) if os.path.exists(self.image) else 0
        return {
            "image": self.image,
            "image_size": size,
            "attached": attached,          # visible to the target
            "attached_image": self._attached_name(),  # which image the target currently sees
            "editing": mounted,            # mounted on the Pi for file ops
            "state": "attached" if attached else ("editing" if mounted else "detached"),
            "can_edit": mounted,
        }

    def _run_helper(self, cmd: str):
        """Perform a privileged attach/detach transition via the root helper (sudo).
        Status reads stay unprivileged: lun.0/file is world-readable and the mount is
        checked with os.path.ismount, so only the two transitions need privilege."""
        r = subprocess.run(["sudo", "-n", HELPER, cmd],
                           capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            raise MSDError((r.stderr or r.stdout or "mass-storage helper failed").strip())

    # ---- transitions (privileged; serialized by @_locked) ----
    @_locked
    def detach(self) -> dict:
        """Eject from the target and mount the ESP on the Pi for editing (via the helper).
        Editing always targets the default EFI image, so point the selection back at it."""
        self._set_name(os.path.basename(self.image))
        self._run_helper("detach")
        return self.status()

    @_locked
    def attach(self) -> dict:
        """Hand the active image back to the target (via the helper)."""
        self._run_helper("attach")
        return self.status()

    # ---- image library (upload / select / delete whole images) ----
    def _safe_name(self, name: str) -> str:
        name = os.path.basename((name or "").strip())
        if not _NAME_RE.match(name) or not name.lower().endswith(ALLOWED_EXT):
            raise MSDError("invalid image name (letters/digits/._- ending in .img/.iso/.bin/.raw)")
        return name

    def _img_path(self, name: str) -> str:
        return os.path.join(IMAGES_DIR, self._safe_name(name))

    def _set_name(self, name: str):
        """Record the image name for the next privileged attach/import/delete (the helper re-validates)."""
        with open(NAME_PTR, "w") as f:
            f.write(self._safe_name(name) + "\n")

    @_locked
    def list_images(self) -> dict:
        attached = self._attached_name()
        default_real = os.path.realpath(self.image)
        items = []
        try:
            names = os.listdir(IMAGES_DIR)        # IMAGES_DIR is root-owned but world-readable
        except OSError:
            names = []
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = os.path.join(IMAGES_DIR, name)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            items.append({"name": name, "size": size, "attached": name == attached,
                          "default": os.path.realpath(full) == default_real,
                          "cdrom": name.lower().endswith(".iso")})
        return {"images": items, "attached": attached}

    @_locked
    def attach_image(self, name: str) -> dict:
        name = self._safe_name(name)
        if not os.path.isfile(self._img_path(name)):
            raise MSDError("no such image")
        self._set_name(name)
        self._run_helper("attach")
        return self.status()

    @_locked
    def eject(self) -> dict:
        """Remove the medium from the target (and drop any Pi-side edit mount)."""
        self._run_helper("eject")
        return self.status()

    @_locked
    def save_image_upload(self, filename: str, fileobj, declared_size: int = 0) -> dict:
        name = self._safe_name(filename)
        if name == os.path.basename(self.image):
            raise MSDError("cannot overwrite the default EFI drive image")
        if name == self._attached_name():
            raise MSDError("that image is attached to the target — eject it first")
        try:
            st = os.statvfs(IMAGES_DIR)
            if declared_size and declared_size + (64 << 20) > st.f_bavail * st.f_frsize:
                raise MSDError("not enough free space on the Pi for that image")
        except OSError:
            pass
        self._set_name(name)
        # Stream the upload into the ROOT-owned library through the privileged helper (the app cannot
        # write IMAGES_DIR itself, which is what makes the directory untamperable / race-free).
        proc = subprocess.Popen(["sudo", "-n", HELPER, "import"],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                proc.stdin.write(chunk)
        except BrokenPipeError:
            pass                       # helper exited early (e.g. rejected the name); see stderr below
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise MSDError(err.strip() or "upload failed")
        full = self._img_path(name)
        return {"name": name, "size": os.path.getsize(full) if os.path.exists(full) else 0}

    @_locked
    def delete_image(self, name: str) -> dict:
        name = self._safe_name(name)
        if name == os.path.basename(self.image):
            raise MSDError("cannot delete the default EFI drive image")
        if name == self._attached_name():
            raise MSDError("image is attached to the target — eject it first")
        if not os.path.isfile(self._img_path(name)):
            raise MSDError("no such image")
        self._set_name(name)
        self._run_helper("delete")
        return {"ok": True}

    # ---- file ops (only while mounted on the Pi) ----
    def _require_editing(self):
        if not self.is_mounted():
            raise MSDError("drive is attached to the target — detach to edit files")

    def _resolve(self, rel: str) -> str:
        rel = (rel or "").lstrip("/")
        full = os.path.realpath(os.path.join(self.mnt, rel))
        if full != self.mnt and not full.startswith(self.mnt + os.sep):
            raise MSDError("path escapes the drive")
        return full

    @_locked
    def listdir(self, rel: str = "") -> dict:
        self._require_editing()
        base = self._resolve(rel)
        if not os.path.isdir(base):
            raise MSDError("not a directory")
        items = []
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            try:
                isdir = os.path.isdir(p)
                items.append({"name": name, "dir": isdir,
                              "size": 0 if isdir else os.path.getsize(p)})
            except OSError:
                pass
        return {"path": rel.strip("/"), "items": items}

    @_locked
    def resolve_for_download(self, rel: str) -> str:
        self._require_editing()
        full = self._resolve(rel)
        if not os.path.isfile(full):
            raise MSDError("not a file")
        return full

    @_locked
    def save_upload(self, rel_dir: str, filename: str, fileobj):
        self._require_editing()
        safe_name = os.path.basename(filename)
        if not safe_name:
            raise MSDError("bad filename")
        dest = self._resolve(os.path.join(rel_dir or "", safe_name))
        with open(dest, "wb") as out:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        subprocess.run(["sync"], timeout=20)

    @_locked
    def delete(self, rel: str):
        self._require_editing()
        full = self._resolve(rel)
        if full == self.mnt:
            raise MSDError("refusing to delete the root")
        if os.path.isdir(full):
            os.rmdir(full)             # only empty dirs (safety)
        elif os.path.exists(full):
            os.remove(full)
        else:
            raise MSDError("not found")
        subprocess.run(["sync"], timeout=20)

    @_locked
    def mkdir(self, rel: str):
        self._require_editing()
        full = self._resolve(rel)
        os.makedirs(full, exist_ok=True)
