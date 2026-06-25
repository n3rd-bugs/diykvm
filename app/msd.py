"""Mass Storage Drive (MSD) manager for the DIY PiKVM.

Safe mutual-exclusion model — the image is EITHER:
  * ATTACHED to the target (gadget lun.0/file = image path; Pi does NOT mount it), or
  * MOUNTED ON THE PI for editing (lun.0/file = ""; image loop-mounted at MNT).
Never both — that would corrupt the filesystem. File operations are only allowed while
mounted on the Pi (i.e., detached from the target).
"""
import os
import threading
import subprocess

IMAGE = "/opt/kvm/images/drive.img"
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

    @_locked
    def status(self) -> dict:
        attached = self.is_attached()
        mounted = self.is_mounted()
        size = os.path.getsize(self.image) if os.path.exists(self.image) else 0
        return {
            "image": self.image,
            "image_size": size,
            "attached": attached,          # visible to the target
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
        """Eject from the target and mount the ESP on the Pi for editing (via the helper)."""
        self._run_helper("detach")
        return self.status()

    @_locked
    def attach(self) -> dict:
        """Unmount the ESP and hand the (updated) image back to the target (via the helper)."""
        self._run_helper("attach")
        return self.status()

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
