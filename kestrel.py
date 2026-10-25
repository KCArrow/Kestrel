#!/usr/bin/env python3
"""
Kestrel File Browser — a fast, Windows-Explorer-style file manager for Linux desktops.

Native GTK 3 application, no pip dependencies: needs only python3, python3-gobject
and gtk3, which Rocky Linux 9 Workstation already has
(otherwise: sudo dnf install python3-gobject gtk3).

Usage:  kestrel [folder ...]
        kestrel --demo [KestrelDemo folder]     fictional machine for screenshots (docs/DEMO.md)
        kestrel --selftest | --preview-bench | --stress | --nav-bench | --profile | --probe-gl | --probe-model
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import glob
import json
import os
import pwd
import re
import shlex
import shutil
import stat as _stat
import subprocess
import sys
import threading
import signal
import time
from typing import Callable, Dict, List, Optional, Tuple

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

# numpy is optional: the 3D previewer uses it for projection/sorting when present (h5py pulls it in anyway),
# and falls back to pure Python otherwise.
try:
    import numpy as np  # type: ignore
except ImportError:   # pragma: no cover
    np = None

# GtkSourceView is optional: it gives the preview pane syntax colouring, line numbers and a proper code
# editor. Rocky 9's GNOME ships it (gedit uses it); without it the preview falls back to a plain TextView.
GtkSource = None
for _ver in ("4", "3.0"):
    try:
        gi.require_version("GtkSource", _ver)
        from gi.repository import GtkSource  # noqa: E402
        break
    except (ValueError, ImportError):
        GtkSource = None

_T0 = float(os.environ.get("EXPLORER_BENCH_T0") or time.time())
BENCH = bool(os.environ.get("EXPLORER_BENCH"))


def bench(label: str):
    if BENCH:
        sys.stderr.write(f"bench: {label} {int((time.time() - _T0) * 1000)} ms\n")
        sys.stderr.flush()


APP_ID = "io.github.kestrel_files"
APP_NAME = "Kestrel"
APP_FULL_NAME = "Kestrel File Browser"
VERSION = "1.0"
HOME = os.path.expanduser("~")

# --------------------------------------------------------------------------- demo mode (kestrel --demo folder)
# A fictional machine for screenshots and demos: drives, disks, free space, user folders, user name, Quick access
# and Recent all come from the demo folder; the real config is neither read nor written.
DEMO: Optional[dict] = None


def demo_setup(root: str) -> dict:
    """Build the fake machine around `root` (the unzipped KestrelDemo folder). Returns the demo descriptor."""
    root = os.path.abspath(root)
    sub_ = lambda *a: os.path.join(root, *a)
    drives = [  # label, path, kind, device, fstype, total, used, source
        ("Local Disk (/)", sub_("System"), "local", "nvme0n1p2", "xfs", 1_000_204_886_016, 312_000_000_000, ""),
        ("Projects (/mnt/projects)", sub_("Projects"), "local", "nvme1n1p1", "xfs", 4_000_787_030_016, 2_610_000_000_000, ""),
        ("Archive (/mnt/archive)", sub_("Archive"), "local", "sda1", "ext4", 8_001_563_222_016, 6_950_000_000_000, ""),
        ("KINGSTON (/run/media/artist/KINGSTON)", sub_("KINGSTON"), "removable", "sdb1", "exfat", 61_847_142_400, 8_200_000_000, ""),
        ("studio (nas01:/export/studio)", sub_("studio_nas"), "network", "", "nfs4", 43_980_465_111_040, 31_200_000_000_000, "nas01:/export/studio"),
    ]
    disks = [
        {"name": "nvme0n1", "model": "Samsung SSD 980 PRO 1TB", "size": 1_000_204_886_016, "tran": "NVME", "removable": False,
         "parts": [{"name": "nvme0n1p1", "size": 629_145_600, "fstype": "vfat", "label": "EFI", "mountpoints": []},
                   {"name": "nvme0n1p2", "size": 999_575_740_416, "fstype": "xfs", "label": "rocky", "mountpoints": [sub_("System")]}]},
        {"name": "nvme1n1", "model": "WD_BLACK SN850X 4TB", "size": 4_000_787_030_016, "tran": "NVME", "removable": False,
         "parts": [{"name": "nvme1n1p1", "size": 4_000_787_030_016, "fstype": "xfs", "label": "projects", "mountpoints": [sub_("Projects")]}]},
        {"name": "sda", "model": "Seagate IronWolf 8TB", "size": 8_001_563_222_016, "tran": "SATA", "removable": False,
         "parts": [{"name": "sda1", "size": 7_501_563_222_016, "fstype": "ext4", "label": "archive", "mountpoints": [sub_("Archive")]},
                   {"name": "sda2", "size": 500_000_000_000, "fstype": "ntfs", "label": "Windows", "mountpoints": []}]},
        {"name": "sdb", "model": "Kingston DataTraveler 3.0", "size": 61_847_142_400, "tran": "USB", "removable": True,
         "parts": [{"name": "sdb1", "size": 61_847_142_400, "fstype": "exfat", "label": "KINGSTON", "mountpoints": [sub_("KINGSTON")]}]},
    ]
    for d in drives:
        os.makedirs(d[1], exist_ok=True)
    home = sub_("Home")
    os.makedirs(home, exist_ok=True)
    return {"root": root, "home": home, "drives": drives, "disks": disks, "user": "artist",
            "usage": {d[1]: (d[6], d[5]) for d in drives}}


def demo_config() -> dict:
    """A fresh config with Quick access / Recent pointing into the demo folder."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    r = DEMO["root"]
    j = lambda *a: os.path.join(r, *a)
    cfg["pinned"] = [p for p in (j("Projects", "Hollowmere"), j("Projects", "Hollowmere", "renders"), j("Projects", "Hollowmere", "reference", "concept"),
                                 j("studio_nas", "library", "hdri"), j("Home", "Downloads")) if os.path.isdir(p)]
    cfg["recent"] = [p for p in (j("Projects", "Hollowmere", "docs", "dailies_notes_2026-10-24.txt"),
                                 j("Projects", "Hollowmere", "assets", "creatures", "bogwraith", "model", "bogwraith_sculpt_v007.obj"),
                                 j("Projects", "Hollowmere", "renders", "sh010", "beauty", "sh010_beauty_v004.1001.exr"),
                                 j("Projects", "Hollowmere", "reference", "concept", "hollowmere_keyart_v03.png"),
                                 j("Projects", "Hollowmere", "shots", "sh010", "playblast", "sh010_anim_latest.mp4"),
                                 j("Home", "Documents", "timesheet_oct.csv")) if os.path.isfile(p)]
    cfg["frequent"] = {j("Projects", "Hollowmere", "shots"): 41, j("Projects", "Hollowmere", "assets"): 37, j("Home", "Pictures"): 12, j("Archive"): 9}
    cfg["preview_pane"] = cfg["details_pane"] = True
    return cfg


_DEMO_MOUNTS = {"System": "", "Projects": "/mnt/projects", "Archive": "/mnt/archive", "KINGSTON": "/run/media/artist/KINGSTON",
                "studio_nas": "/mnt/studio", "Home": "/home/artist"}


def pretty_path(path: str) -> str:
    """The path as shown to the user. Identity normally; in demo mode the demo folder is presented as the
    fictional machine's mount points so no real path (or user name) appears anywhere in the window."""
    if DEMO is None or not path:
        return path
    root = DEMO["root"]
    if path == root:
        return "/"
    if path.startswith(root + os.sep):
        rest = path[len(root) + 1:].split(os.sep, 1)
        top = _DEMO_MOUNTS.get(rest[0])
        if top is not None:
            return (top + ("/" + rest[1] if len(rest) > 1 else "")) or "/"
    return path


def demo_drive_root(path: str) -> Optional[Tuple[str, str]]:
    """(short label, drive path) of the demo drive containing `path`."""
    if DEMO is None:
        return None
    for label, dp, _k, *_ in DEMO["drives"]:
        if path == dp or path.startswith(dp + os.sep):
            return label.split(" (")[0], dp
    if path == DEMO["home"] or path.startswith(DEMO["home"] + os.sep):
        return "artist", DEMO["home"]
    return None


_Usage = __import__("collections").namedtuple("_Usage", "total used free")


def disk_usage(path: str):
    """shutil.disk_usage, or the fictional numbers in demo mode."""
    if DEMO is not None:
        for mp, (used, total) in DEMO["usage"].items():
            if path == mp or path.startswith(mp + os.sep):
                return _Usage(total, used, total - used)
    return shutil.disk_usage(path)
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "kestrel")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

QUICK, THISPC, TRASH, NETWORK, RECENT = "quick://", "pc://", "trash:///", "net://", "recent://"
VIRTUAL = (QUICK, THISPC, TRASH, NETWORK, RECENT)

DEFAULT_CONFIG = {
    "open_to": "quick", "browse_in_new_tab": False, "single_click": False,
    "show_recent": True, "show_frequent": True,
    "show_hidden": False, "hide_extensions": False, "full_path_title": False, "folders_first": True,
    "confirm_delete": True, "checkboxes": False, "gl_preview": True, "gdk_backend": "", "rv_enabled": False, "rv_path": "", "ui_cores": 2, "archive_action": "extract", "index_enabled": False, "index_roots": [HOME], "index_network": False, "index_hidden": False, "index_interval_h": 6, "nav_pane": True, "preview_pane": False, "details_pane": False,
    "size_in_bytes": False, "date_format": "%Y-%m-%d %H:%M", "layout": "details",
    "sort_key": "name", "sort_desc": False, "group_by": "none",
    "columns": ["name", "modified", "type", "size"],
    "search_subfolders": True, "search_contents": False,
    "window": [1100, 700], "nav_width": 220, "preview_width": 300, "side_width": 0, "side_split": 0,
    "pinned": [], "recent": [], "frequent": {},
}
COLUMN_TITLES = {"name": "Name", "modified": "Date modified", "type": "Type", "size": "Size", "created": "Date created",
                 "accessed": "Date accessed", "owner": "Owner", "perms": "Permissions", "location": "Folder path",
                 "deleted": "Date deleted"}
ATTRS = ("standard::name,standard::type,standard::size,standard::icon,standard::fast-content-type,"
         "standard::is-hidden,standard::is-backup,standard::is-symlink,standard::symlink-target,standard::display-name,"
         "standard::target-uri,time::modified,time::created,time::access,unix::mode,unix::uid,owner::user,trash::orig-path,trash::deletion-date")


# --------------------------------------------------------------------------- config
def load_config() -> dict:
    if DEMO is not None:
        return demo_config()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    old = os.path.join(GLib.get_user_config_dir(), "explorer-gtk", "config.json")
    if not os.path.exists(CONFIG_FILE) and os.path.exists(old):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            shutil.copy2(old, CONFIG_FILE)
        except OSError:
            pass
    try:
        with open(CONFIG_FILE) as f:
            for k, v in json.load(f).items():
                if k in cfg:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    if DEMO is not None:
        return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


# --------------------------------------------------------------------------- formatting
def fmt_size(n: Optional[int], in_bytes: bool = False) -> str:
    if n is None:
        return ""
    if in_bytes:
        return f"{n:,}"
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for u in ("KB", "MB", "GB", "TB", "PB"):
        v /= 1024.0
        if v < 1024 or u == "PB":
            return f"{v:,.1f} {u}" if v < 10 else f"{v:,.0f} {u}"
    return str(n)


def fmt_date(ts: Optional[float], fmt: str) -> str:
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).strftime(fmt)
    except Exception:
        return ""


def unique_name(dest: str) -> str:
    if not os.path.lexists(dest):
        return dest
    d, base = os.path.split(dest)
    stem, ext = (base, "") if os.path.isdir(dest) else os.path.splitext(base)
    cand = os.path.join(d, f"{stem} - Copy{ext}")
    n = 2
    while os.path.lexists(cand):
        cand = os.path.join(d, f"{stem} - Copy ({n}){ext}")
        n += 1
    return cand


# --------------------------------------------------------------------------- entries
class Entry:
    __slots__ = ("name", "path", "uri", "is_dir", "is_link", "size", "mtime", "ctime", "atime", "mode", "owner",
                 "hidden", "type", "icon", "content_type", "location", "deleted", "orig", "usage", "section", "group", "extra_dims",
                 "drive_kind", "volume")

    def __init__(self):
        self.name = self.path = self.uri = ""
        self.is_dir = self.is_link = self.hidden = False
        self.size: Optional[int] = None
        self.mtime = self.ctime = self.atime = 0.0
        self.mode = 0
        self.owner = ""
        self.type = ""
        self.icon: Optional[Gio.Icon] = None
        self.content_type = ""
        self.location = ""
        self.deleted = 0.0
        self.orig = ""
        self.drive_kind = ""          # local | removable | network | volume (unmounted), for This PC rows
        self.volume = None            # GVolume for an unmounted removable
        self.usage: Optional[Tuple[int, int]] = None
        self.section = ""
        self.group = ""
        self.extra_dims = None

    @property
    def ext(self) -> str:
        return "" if self.is_dir else os.path.splitext(self.name)[1].lower()

    @property
    def gfile(self) -> Gio.File:
        return Gio.File.new_for_uri(self.uri) if self.uri else Gio.File.new_for_path(self.path)


def entry_from_info(info: Gio.FileInfo, parent: Gio.File) -> Entry:
    e = Entry()
    e.name = info.get_name()
    child = parent.get_child(e.name)
    e.path = child.get_path() or ""
    e.uri = child.get_uri()
    if not e.path:
        target = info.get_attribute_string("standard::target-uri")
        if target:
            e.path = Gio.File.new_for_uri(target).get_path() or ""
    ftype = info.get_file_type()
    e.is_dir = ftype == Gio.FileType.DIRECTORY
    e.is_link = info.get_is_symlink()
    if e.is_link and not e.is_dir and e.path and os.path.isdir(e.path):
        e.is_dir = True
    e.size = None if e.is_dir else info.get_size()
    e.mtime = info.get_attribute_uint64("time::modified") or 0
    e.ctime = info.get_attribute_uint64("time::created") or 0
    e.atime = info.get_attribute_uint64("time::access") or 0
    e.mode = info.get_attribute_uint32("unix::mode") or 0
    e.owner = info.get_attribute_string("owner::user") or ""
    e.hidden = info.get_is_hidden() or info.get_is_backup()
    e.content_type = info.get_attribute_string("standard::fast-content-type") or info.get_content_type() or ""
    if e.is_dir:
        e.type = "File folder"
    else:
        e.type = Gio.content_type_get_description(e.content_type) if e.content_type else "File"
    if e.is_link:
        e.type += " (link)"
    e.icon = info.get_icon()
    orig = info.get_attribute_byte_string("trash::orig-path") or info.get_attribute_string("trash::orig-path")
    if orig:
        e.orig = orig
        e.location = os.path.dirname(orig)
    dd = info.get_attribute_string("trash::deletion-date")
    if dd:
        try:
            e.deleted = _dt.datetime.strptime(dd, "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            pass
    return e


def entry_for_path(path: str, name: Optional[str] = None) -> Entry:
    f = Gio.File.new_for_path(path)
    try:
        info = f.query_info(ATTRS, Gio.FileQueryInfoFlags.NONE, None)
        e = entry_from_info(info, f.get_parent() or f)
    except GLib.Error:
        e = Entry()
        e.name = os.path.basename(path)
        e.type = "File"
        e.icon = Gio.ThemedIcon.new("text-x-generic")
    e.path = path
    e.uri = f.get_uri()
    if name:
        e.name = name
    return e


_CT_CACHE: Dict[str, Tuple[str, Gio.Icon]] = {}
_FOLDER_ICON = Gio.ThemedIcon.new("folder")


def _ct_info(name: str) -> Tuple[str, str, Gio.Icon]:
    """(content_type, description, icon) for a file name — cached by extension/content type."""
    ext = os.path.splitext(name)[1].lower() or name.lower()
    hit = _CT_CACHE.get(ext)
    if hit is None:
        ct, _ = Gio.content_type_guess(name, None)
        hit = (ct, Gio.content_type_get_description(ct), Gio.content_type_get_icon(ct))
        _CT_CACHE[ext] = hit
    return hit


def _shallow_entry(d: os.DirEntry) -> Entry:
    """Name + kind only, from the directory entry itself (d_type) — no stat, so no network round trip.
    Used for the instant first paint of a big folder on a share; the full entries replace it."""
    e = Entry()
    e.name = d.name
    e.path = d.path
    try:
        e.is_dir = d.is_dir(follow_symlinks=False)
        e.is_link = d.is_symlink()
    except OSError:
        pass
    e.hidden = d.name.startswith(".") or d.name.endswith("~")
    if e.is_dir:
        e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
    else:
        e.content_type, e.type, e.icon = _ct_info(d.name)
    return e


def _entry_from_dirent(d: os.DirEntry) -> Entry:
    e = Entry()
    e.name = d.name
    e.path = d.path
    try:
        is_link = d.is_symlink()
        st = d.stat()
        is_dir = d.is_dir()
    except OSError:
        try:
            st = d.stat(follow_symlinks=False)
        except OSError:
            st = None
        is_link, is_dir = True, False
    e.is_link, e.is_dir = is_link, is_dir
    if st is not None:
        e.size = None if is_dir else st.st_size
        e.mtime, e.ctime, e.atime, e.mode = st.st_mtime, st.st_ctime, st.st_atime, st.st_mode
        e.owner = _owner_name(st.st_uid)
    e.hidden = d.name.startswith(".") or d.name.endswith("~")
    if is_dir:
        e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
    else:
        e.content_type, e.type, e.icon = _ct_info(d.name)
        if st is not None and (st.st_mode & 0o111) and e.content_type in ("application/octet-stream", "text/plain"):
            e.type = "Executable" if e.content_type == "application/octet-stream" else e.type
    if is_link:
        e.type += " (link)"
    return e


_OWNERS: Dict[int, str] = {}


def _owner_name(uid: int) -> str:
    if DEMO is not None:
        return DEMO["user"] if uid == os.getuid() else ("root" if uid == 0 else "pipeline")
    n = _OWNERS.get(uid)
    if n is None:
        try:
            n = pwd.getpwuid(uid).pw_name
        except Exception:
            n = str(uid)
        _OWNERS[uid] = n
    return n


_NET_FS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "sshfs", "fuse.rclone", "davfs", "fuse.davfs", "9p", "afs", "ceph", "glusterfs", "fuse.gvfsd-fuse")
_MOUNT_CACHE: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}


def _mounts() -> List[Tuple[str, str]]:
    """[(mountpoint, fstype)] longest first, cached 10 s."""
    now = time.time()
    hit = _MOUNT_CACHE.get("m")
    if hit and now - hit[0] < 10:
        return hit[1]
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    out.append((parts[1].replace("\\040", " "), parts[2]))
    except OSError:
        pass
    out.sort(key=lambda t: -len(t[0]))
    _MOUNT_CACHE["m"] = (now, out)
    return out


def is_network_path(path: str) -> bool:
    for mp, fs in _mounts():
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            return fs in _NET_FS
    return False


def list_folder(uri_or_path: str, show_hidden: bool, progress: Optional[Callable[[List[Entry]], None]] = None) -> List[Entry]:
    """List a folder. If `progress` is given it is called with a snapshot of what has been read so far —
    first after ~80 ms, then only every ~1.2 s — so a huge folder can be shown while the rest is still being stat'ed."""
    if "://" not in uri_or_path:
        out: List[Entry] = []
        try:
            with os.scandir(uri_or_path) as it:
                dirents = [d for d in it if show_hidden or not (d.name.startswith(".") or d.name.endswith("~"))]
            if len(dirents) > 400 and is_network_path(uri_or_path):
                # On NFS/SMB/sshfs each stat is a round trip (≈5 ms on a NAS → 2,400 files ≈ 1 s even when
                # overlapped). Paint the names and icons immediately from the dirents, then overlap the stats
                # 32-wide and let the dated/sized rows stream in behind.
                if progress:
                    progress([_shallow_entry(d) for d in dirents])
                from concurrent.futures import ThreadPoolExecutor
                chunks = [dirents[i:i + 128] for i in range(0, len(dirents), 128)]
                t_next = time.time() + 0.4
                with ThreadPoolExecutor(min(32, len(chunks))) as ex:
                    for part in ex.map(lambda ch: [_entry_from_dirent(d) for d in ch], chunks):
                        out.extend(part)
                        if progress and time.time() >= t_next:
                            progress(list(out))
                            t_next = time.time() + 1.2
            else:
                t_next = time.time() + 0.08 if progress else float("inf")
                app = out.append
                for n, d in enumerate(dirents):
                    app(_entry_from_dirent(d))
                    if (n & 255) == 0 and time.time() >= t_next:
                        progress(list(out))
                        t_next = time.time() + 1.2
        except PermissionError:
            raise GLib.Error("You don't have permission to access this folder.")
        except OSError as ex:
            raise GLib.Error(str(ex))
        return out
    f = Gio.File.new_for_uri(uri_or_path)
    out = []
    en = f.enumerate_children(ATTRS, Gio.FileQueryInfoFlags.NONE, None)
    while True:
        info = en.next_file(None)
        if info is None:
            break
        if not show_hidden and (info.get_is_hidden() or info.get_is_backup()):
            continue
        out.append(entry_from_info(info, f))
    en.close(None)
    return out


def sort_entries(entries: List[Entry], key: str, desc: bool, folders_first: bool) -> List[Entry]:
    def k(e: Entry):
        n = e.name.lower()
        if key == "modified":
            return (e.mtime, n)
        if key == "created":
            return (e.ctime, n)
        if key == "accessed":
            return (e.atime, n)
        if key == "type":
            return (e.type.lower(), n)
        if key == "size":
            return (e.size or 0, n)
        if key == "owner":
            return (e.owner, n)
        if key == "location":
            return (e.location.lower(), n)
        if key == "deleted":
            return (e.deleted, n)
        return n
    res = sorted(entries, key=k, reverse=desc)
    if folders_first:
        res = [e for e in res if e.is_dir] + [e for e in res if not e.is_dir]
    return res


def group_label(e: Entry, group_by: str) -> str:
    if group_by == "name":
        c = e.name[:1].upper()
        return c if c.isalpha() else ("0-9" if c.isdigit() else "Other")
    if group_by == "type":
        return e.type
    if group_by == "owner":
        return e.owner or "Unknown"
    if group_by == "size":
        if e.is_dir:
            return "Folders"
        s = e.size or 0
        for lim, lab in ((0, "Empty (0 KB)"), (16 << 10, "Tiny (0 - 16 KB)"), (1 << 20, "Small (16 KB - 1 MB)"),
                         (128 << 20, "Medium (1 - 128 MB)"), (1 << 30, "Large (128 MB - 1 GB)"), (4 << 30, "Huge (1 - 4 GB)")):
            if s <= lim:
                return lab
        return "Gigantic (> 4 GB)"
    if group_by in ("modified", "created", "accessed"):
        ts = {"modified": e.mtime, "created": e.ctime, "accessed": e.atime}[group_by]
        if not ts:
            return "Unspecified"
        d = _dt.datetime.fromtimestamp(ts).date()
        today = _dt.date.today()
        delta = (today - d).days
        if delta <= 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        if delta < 7:
            return "Earlier this week"
        if delta < 14:
            return "Last week"
        if d.year == today.year and d.month == today.month:
            return "Earlier this month"
        if d.year == today.year:
            return "Earlier this year"
        return "A long time ago"
    return ""


def xdg_user_dirs() -> List[Tuple[str, str, str]]:
    """[(label, path, icon-name)]"""
    out = []
    if DEMO is not None:
        for label, icon in (("Desktop", "user-desktop"), ("Documents", "folder-documents"), ("Downloads", "folder-download"),
                            ("Music", "folder-music"), ("Pictures", "folder-pictures"), ("Videos", "folder-videos")):
            p = os.path.join(DEMO["home"], label)
            if os.path.isdir(p):
                out.append((label, p, icon))
        return out
    for key, label, icon in ((GLib.UserDirectory.DIRECTORY_DESKTOP, "Desktop", "user-desktop"),
                             (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "Documents", "folder-documents"),
                             (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "Downloads", "folder-download"),
                             (GLib.UserDirectory.DIRECTORY_MUSIC, "Music", "folder-music"),
                             (GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures", "folder-pictures"),
                             (GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos", "folder-videos")):
        p = GLib.get_user_special_dir(key)
        if p and os.path.isdir(p):
            out.append((label, p, icon))
    return out


def _sys_block_info(dev: Optional[str]) -> Tuple[bool, bool]:
    """(is_removable, is_usb) for /dev/sdX1 etc. from sysfs: the kernel's removable flag on the whole disk and
    whether the device sits on a USB bus. Internal SATA/NVMe partitions of other operating systems (dual boot)
    come back (False, False) and are treated as ordinary local disks — never as something to eject."""
    if not dev or not dev.startswith("/dev/"):
        return False, False
    name = os.path.basename(dev)
    try:
        real = os.path.realpath(f"/sys/class/block/{name}")
        is_usb = "/usb" in real
        # whole-disk node: /sys/class/block/sdb1 → parent dir is sdb
        disk = real
        if os.path.exists(os.path.join(real, "partition")):
            disk = os.path.dirname(real)
        with open(os.path.join(disk, "removable")) as f:
            removable = f.read().strip() == "1"
        return removable or is_usb, is_usb
    except OSError:
        return False, False


def usb_icon_for(path: str, default: Optional[Gio.Icon] = None) -> Gio.Icon:
    """USB sticks get the USB glyph; other removables keep their media icon."""
    rem, usb = _sys_block_info(block_device_for(path))
    if usb:
        return Gio.ThemedIcon.new_with_default_fallbacks("drive-removable-media-usb")
    return default or Gio.ThemedIcon.new("drive-removable-media")


def drives(kind: Optional[str] = None) -> List[Tuple[str, str, Gio.Icon, str]]:
    """[(label, mountpoint, icon, kind)] with kind in local | removable | network.
    Pass kind to filter. Network = NFS, SMB/CIFS, sshfs, davfs, rclone, gvfs mounts…"""
    out: List[Tuple[str, str, Gio.Icon, str]] = []
    if DEMO is not None:
        for label, path, k, _dev, _fs, _tot, _used, _src in DEMO["drives"]:
            icon = Gio.ThemedIcon.new_with_default_fallbacks("drive-removable-media-usb") if k == "removable" else \
                Gio.ThemedIcon.new("network-server" if k == "network" else "drive-harddisk")
            out.append((label, path, icon, k))
        return [d for d in out if kind is None or d[3] == kind]
    seen = set()
    fs_of = {mp: fs for mp, fs in _mounts()}

    def classify(mp: str, fstype: str) -> str:
        if fstype in _NET_FS or fstype.startswith(("fuse.gvfsd", "fuse.rclone", "fuse.sshfs")):
            return "network"
        # removable = the kernel says so (USB bus or removable flag), not the filesystem type: an internal
        # NTFS Windows partition is a local disk, a USB stick formatted ext4 is removable
        rem, _usb = _sys_block_info(block_device_for(mp))
        if rem or fstype in ("iso9660", "udf"):
            return "removable"
        return "local"

    def add(label, path, icon, k):
        if path and path not in seen and os.path.isdir(path):
            seen.add(path)
            if k == "removable":
                icon = usb_icon_for(path, icon)
            elif icon is None:
                icon = Gio.ThemedIcon.new({"network": "network-server"}.get(k, "drive-harddisk"))
            out.append((label, path, icon, k))
    add("Local Disk (/)", "/", None, "local")
    for m in Gio.VolumeMonitor.get().get_mounts():
        root = m.get_root()
        p = root.get_path()
        if p:
            k = classify(p, fs_of.get(p, ""))
            if m.is_shadowed() or root.get_uri().startswith(("smb://", "sftp://", "ftp://", "dav://", "nfs://")):
                k = "network"
            add(m.get_name(), p, m.get_icon(), k)
    # plain mounts not represented as GMount (e.g. /home, /boot, /mnt/*, fstab NFS/CIFS shares)
    for mp, fstype in _mounts():
        if fstype in ("ext4", "ext3", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "zfs", "iso9660", "udf") or fstype in _NET_FS:
            if mp.startswith(("/boot/efi", "/var/lib", "/snap", "/run/user")):
                continue
            k = classify(mp, fstype)
            label = ("Local Disk" if mp == "/" else os.path.basename(mp) or mp) + (f" ({mp})" if mp != "/" else " (/)")
            if k == "network":
                # try to show where it comes from: "server:/export" or "//host/share"
                src = _mount_source(mp)
                if src:
                    label = f"{os.path.basename(mp) or mp} ({src})"
            add(label, mp, None, k)
    out.sort(key=lambda t: ({"local": 0, "removable": 1, "network": 2}[t[3]], t[1] != "/", t[0].lower()))
    return [d for d in out if kind is None or d[3] == kind]


def network_browse() -> List[Entry]:
    """Servers/shares GVfs can see (network:/// — needs gvfs-smb / avahi); empty if unavailable."""
    out: List[Entry] = []
    try:
        f = Gio.File.new_for_uri("network:///")
        en = f.enumerate_children("standard::name,standard::display-name,standard::icon,standard::target-uri,standard::type",
                                  Gio.FileQueryInfoFlags.NONE, None)
        while True:
            info = en.next_file(None)
            if info is None:
                break
            e = Entry()
            e.name = info.get_display_name() or info.get_name()
            e.uri = info.get_attribute_string("standard::target-uri") or f.get_child(info.get_name()).get_uri()
            e.is_dir = True
            e.icon = info.get_icon() or Gio.ThemedIcon.new("network-server")
            e.type = "Network location"
            e.location = e.uri
            e.section = "Network"
            out.append(e)
        en.close(None)
    except GLib.Error:
        pass
    return out


def mount_uri(uri: str, parent, on_done: Callable[[Optional[str], Optional[str]], None]):
    """Mount a smb:// sftp:// ftp:// dav:// nfs:// … location through GVfs (asks for credentials with a
    GTK mount dialog), then call on_done(local_fuse_path_or_None, error_or_None) on the main loop."""
    f = Gio.File.new_for_uri(uri)
    op = Gtk.MountOperation(parent=parent) if parent is not None else None

    def cb(src, res, data):
        try:
            src.mount_enclosing_volume_finish(res)
        except GLib.Error as ex:
            if not ex.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                on_done(None, ex.message)
                return
        on_done(f.get_path() or f.get_uri(), None)
    f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, cb, None)


def unmount_path(path: str, parent, on_done: Callable[[Optional[str]], None]):
    f = Gio.File.new_for_path(path)
    try:
        m = f.find_enclosing_mount(None)
    except GLib.Error as ex:
        on_done(ex.message)
        return
    op = Gtk.MountOperation(parent=parent) if parent is not None else None

    def cb(src, res, data):
        try:
            src.unmount_with_operation_finish(res)
            on_done(None)
        except GLib.Error as ex:
            on_done(ex.message)
    m.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, cb, None)


def invalidate_mounts():
    _MOUNT_CACHE.pop("m", None)


_LSBLK_CACHE: Dict[str, Tuple[float, list]] = {}


def physical_disks() -> List[dict]:
    """Physical disks and their partitions, from lsblk (util-linux): [{name, model, size, tran, removable,
    parts: [{name, size, fstype, label, mountpoints}]}]. Cached 10 s; empty if lsblk is unavailable."""
    if DEMO is not None:
        return DEMO["disks"]
    now = time.time()
    hit = _LSBLK_CACHE.get("d")
    if hit and now - hit[0] < 10:
        return hit[1]
    disks: List[dict] = []
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,KNAME,TYPE,SIZE,MODEL,TRAN,RM,MOUNTPOINTS,FSTYPE,LABEL,VENDOR"],
                           capture_output=True, text=True, timeout=10)
        tree = json.loads(r.stdout or "{}").get("blockdevices", [])
    except Exception:
        tree = []

    def parts_of(node, acc):
        for ch in node.get("children", []) or []:
            if ch.get("type") in ("part", "lvm", "crypt", "md", "raid1", "raid0"):
                acc.append({"name": ch.get("kname") or ch.get("name"), "size": int(ch.get("size") or 0), "fstype": ch.get("fstype") or "",
                            "label": ch.get("label") or "", "mountpoints": [m for m in (ch.get("mountpoints") or []) if m]})
            parts_of(ch, acc)
    for d in tree:
        if d.get("type") != "disk":
            continue
        size = int(d.get("size") or 0)
        if size == 0 or (d.get("kname") or "").startswith(("zram", "loop", "ram")):
            continue
        parts: list = []
        parts_of(d, parts)
        if not parts:          # whole-disk filesystem (common for USB sticks / VMs)
            parts.append({"name": d.get("kname") or d.get("name"), "size": size, "fstype": d.get("fstype") or "", "label": d.get("label") or "",
                          "mountpoints": [m for m in (d.get("mountpoints") or []) if m]})
        model = " ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x) or (d.get("kname") or "disk")
        disks.append({"name": d.get("kname") or d.get("name"), "model": model, "size": size, "tran": (d.get("tran") or "").upper(),
                      "removable": bool(d.get("rm")), "parts": parts})
    _LSBLK_CACHE["d"] = (now, disks)
    return disks


def disk_for_mount(path: str) -> Optional[Tuple[dict, dict]]:
    """(disk, partition) that holds the given mount point, or None (network / virtual)."""
    for d in physical_disks():
        for p in d["parts"]:
            if path in p["mountpoints"]:
                return d, p
    return None


def volumes_unmounted() -> List[Tuple[str, "Gio.Volume", Gio.Icon]]:
    """Removable volumes the system knows about but hasn't mounted (a stick inserted with automount off, or
    ejected but not unplugged). Internal partitions — a dual-boot Windows or another Linux install — are NOT
    listed: they only appear once the user mounts them explicitly (then they show as local disks).
    [(label, GVolume, icon)]."""
    out = []
    if DEMO is not None:
        return out
    for v in Gio.VolumeMonitor.get().get_volumes():
        if v.get_mount() is not None or not v.can_mount():
            continue
        dev = v.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE)
        rem, usb = _sys_block_info(dev)
        drv = v.get_drive()
        if not (rem or (drv is not None and (drv.is_removable() or drv.is_media_removable()))):
            continue
        icon = Gio.ThemedIcon.new_with_default_fallbacks("drive-removable-media-usb") if usb else (v.get_icon() or Gio.ThemedIcon.new("drive-removable-media"))
        out.append((v.get_name(), v, icon))
    return out


def eject_or_unmount(path: str, parent, on_done: Callable[[Optional[str]], None]):
    """Eject the drive behind `path` if it can be ejected (USB sticks, card readers, optical), otherwise just
    unmount. GIO handles polkit/udisks; the mount operation lets GTK prompt if needed."""
    f = Gio.File.new_for_path(path)
    try:
        m = f.find_enclosing_mount(None)
    except GLib.Error as ex:
        on_done(ex.message)
        return
    op = Gtk.MountOperation(parent=parent) if parent is not None else None

    def done(src, res, data):
        try:
            (src.eject_with_operation_finish if data == "eject" else src.unmount_with_operation_finish)(res)
            on_done(None)
        except GLib.Error as ex:
            on_done(ex.message)
    if m.can_eject():
        m.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None, done, "eject")
    else:
        m.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, done, "unmount")


def mount_volume(vol, parent, on_done: Callable[[Optional[str], Optional[str]], None]):
    op = Gtk.MountOperation(parent=parent) if parent is not None else None

    def done(src, res, _):
        try:
            src.mount_finish(res)
            m = src.get_mount()
            on_done(m.get_root().get_path() if m else None, None)
        except GLib.Error as ex:
            on_done(None, ex.message)
    vol.mount(Gio.MountMountFlags.NONE, op, None, done, None)


def block_device_for(path: str) -> Optional[str]:
    """/dev/sdX1 behind a mount point, from /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            best = ("", "")
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    mp = parts[1].replace("\\040", " ")
                    if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best[0]):
                        best = (mp, parts[0])
            return best[1] or None
    except OSError:
        return None


FORMAT_CHOICES = [("exfat", "exFAT", "exfatprogs"),
                  ("vfat", "FAT32 (4 GB file limit)", "dosfstools"),
                  ("ntfs", "NTFS", "ntfsprogs"),
                  ("ext4", "ext4 (Linux only)", "e2fsprogs")]


def mkfs_available(fstype: str) -> bool:
    return shutil.which({"exfat": "mkfs.exfat", "vfat": "mkfs.vfat", "ntfs": "mkfs.ntfs", "ext4": "mkfs.ext4"}[fstype]) is not None


def udisks_format(device: str, fstype: str, label: str, erase: bool, on_done: Callable[[Optional[str]], None]):
    """Format a block device through UDisks2 (org.freedesktop.UDisks2.Block.Format) — the same path GNOME Disks
    uses, so polkit asks for the user's password rather than needing sudo, and the device is unmounted and
    re-probed by udisks itself. Runs the D-Bus call asynchronously; on_done(error or None) on the main loop."""
    name = os.path.basename(device)
    obj = "/org/freedesktop/UDisks2/block_devices/" + name.replace("-", "_x2d")
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    except GLib.Error as ex:
        on_done(f"system bus: {ex.message}")
        return
    opts = {"label": GLib.Variant("s", label[:11] if fstype == "vfat" else label), "take-ownership": GLib.Variant("b", fstype == "ext4"),
            "update-partition-type": GLib.Variant("b", True)}
    if erase:
        opts["erase"] = GLib.Variant("s", "zero")
    params = GLib.Variant("(sa{sv})", (fstype, opts))

    def done(src, res, _):
        try:
            src.call_finish(res)
            on_done(None)
        except GLib.Error as ex:
            msg = ex.message
            if "NotAuthorized" in msg or "not authorized" in msg.lower():
                msg = "Not authorised to format this device (polkit). Try from a local session, or use GNOME Disks."
            on_done(msg)
    bus.call("org.freedesktop.UDisks2", obj, "org.freedesktop.UDisks2.Block", "Format", params, None,
             Gio.DBusCallFlags.NONE, 3_600_000, None, done, None)


def _mount_source(mp: str) -> str:
    if DEMO is not None:
        return next((d[7] for d in DEMO["drives"] if d[1] == mp), "")
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1].replace("\\040", " ") == mp:
                    return parts[0]
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------- background file operations
class FileOp(threading.Thread):
    """copy | move | delete | trash — runs in a thread, reports via callbacks on the GTK main loop."""

    def __init__(self, kind: str, items: List[str], dest: Optional[str], plan: Dict[str, str], on_done: Callable):
        super().__init__(daemon=True)
        self.kind, self.items, self.dest, self.plan, self.on_done = kind, items, dest, plan, on_done
        self.cancel = Gio.Cancellable()
        self.total = 0
        self.done = 0
        self.current = ""
        self.errors: List[str] = []
        self.results: List[Tuple[str, str]] = []
        self.started = time.time()
        self.finished = False
        self._base = 0
        self.title = ""
        self.dialog = None
        self.total_items = 0
        self.done_items = 0
        self.paused = threading.Event()   # set = paused
        self.speed_samples: List[float] = []

    def _count(self):
        for p in self.items:
            if os.path.isdir(p) and not os.path.islink(p):
                for root, _, fs in os.walk(p):
                    for f in fs:
                        self.total_items += 1
                        try:
                            self.total += os.lstat(os.path.join(root, f)).st_size
                        except OSError:
                            pass
            else:
                self.total_items += 1
                try:
                    self.total += os.lstat(p).st_size
                except OSError:
                    pass

    def _wait_if_paused(self):
        while self.paused.is_set() and not self.cancel.is_cancelled():
            time.sleep(0.1)

    def _progress(self, current, total, *_):
        self.done = self._base + current

    def _copy_file(self, src: str, dst: str, move: bool):
        self._wait_if_paused()
        self.current = src
        self._base = self.done
        s, d = Gio.File.new_for_path(src), Gio.File.new_for_path(dst)
        flags = Gio.FileCopyFlags.OVERWRITE | Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA
        if move:
            s.move(d, flags, self.cancel, self._progress, None)
        else:
            s.copy(d, flags, self.cancel, self._progress, None)
        try:
            self.done = self._base + (os.lstat(dst).st_size if not os.path.islink(dst) else 0)
        except OSError:
            pass
        self.done_items += 1

    def _copy_tree(self, src: str, dst: str, move: bool):
        os.makedirs(dst, exist_ok=True)
        with os.scandir(src) as it:
            for de in it:
                if self.cancel.is_cancelled():
                    raise GLib.Error("cancelled")
                s, t = de.path, os.path.join(dst, de.name)
                try:
                    if de.is_dir(follow_symlinks=False):
                        self._copy_tree(s, t, move)
                    else:
                        self._copy_file(s, t, move)
                except GLib.Error as ex:
                    if ex.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED) or "cancelled" in str(ex):
                        raise
                    self.errors.append(f"{s}: {ex.message}")
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
        if move:
            try:
                os.rmdir(src)
            except OSError:
                pass

    def run(self):
        try:
            if self.kind in ("copy", "move"):
                self._count()
            else:
                self.total_items = len(self.items)
            for src in self.items:
                if self.cancel.is_cancelled():
                    break
                self._wait_if_paused()
                try:
                    self._one(src)
                    if self.kind not in ("copy", "move"):
                        self.done_items += 1
                except GLib.Error as ex:
                    if ex.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED) or "cancelled" in str(ex):
                        break
                    self.errors.append(f"{os.path.basename(src)}: {ex.message}")
                except Exception as ex:
                    self.errors.append(f"{os.path.basename(src)}: {ex}")
        finally:
            self.finished = True
            GLib.idle_add(self.on_done, self)

    def _one(self, src: str):
        self.current = src
        g = Gio.File.new_for_path(src)
        if self.kind == "delete":
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.rmtree(src)
            else:
                g.delete(self.cancel)
            return
        if self.kind == "trash":
            g.trash(self.cancel)
            self.results.append((src, os.path.basename(src)))
            return
        base = os.path.basename(src.rstrip("/"))
        dst = os.path.join(self.dest, base)
        pol = self.plan.get(src, "replace")
        if os.path.lexists(dst):
            if pol == "skip":
                return
            if pol == "keep":
                dst = unique_name(dst)
            elif not (os.path.isdir(src) and os.path.isdir(dst)):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
        move = self.kind == "move"
        if move:
            try:
                os.rename(src, dst)
                self.results.append((src, dst))
                return
            except OSError:
                pass
        if os.path.isdir(src) and not os.path.islink(src):
            self._copy_tree(src, dst, move)
        else:
            self._copy_file(src, dst, move)
        self.results.append((src, dst))


RV_IMAGE_EXTS = (".exr", ".dpx", ".cin", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".tga", ".iff", ".hdr", ".sgi", ".rgb",
                 ".psd", ".bmp", ".gif", ".webp", ".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".m4v", ".rv", ".tx", ".jp2", ".j2k")


def find_rv(configured: str = "") -> Optional[str]:
    """OpenRV / RV executable: the configured path if it runs, else $RV_HOME/bin/rv, rv/openrv on PATH, or the
    usual install trees (/opt/rv*, /opt/OpenRV*, /usr/local/rv*, ~/rv*, ~/OpenRV*, ~/Applications/*rv*)."""
    import glob
    cands = [configured, os.path.join(os.environ.get("RV_HOME", ""), "bin", "rv") if os.environ.get("RV_HOME") else "",
             shutil.which("rv") or "", shutil.which("openrv") or ""]
    for pat in ("/opt/rv*/bin/rv", "/opt/OpenRV*/bin/rv", "/opt/openrv*/bin/rv", "/usr/local/rv*/bin/rv", "/usr/local/OpenRV*/bin/rv",
                "~/rv*/bin/rv", "~/OpenRV*/bin/rv", "~/Applications/*rv*/bin/rv", "~/Applications/*RV*/bin/rv", "/opt/*/OpenRV*/bin/rv"):
        cands += sorted(glob.glob(os.path.expanduser(pat)), reverse=True)
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def rv_sequence_for(path: str) -> Optional[str]:
    """If path is one frame of an image sequence (name.####.ext with ≥2 frames on disk), the RV sequence
    spec for the whole thing (name.#.ext); else None."""
    d, name = os.path.split(path)
    m = re.match(r"^(.*?)(\d{2,})(\.[A-Za-z0-9]+)$", name)
    if not m:
        return None
    head, digits, ext = m.groups()
    try:
        n = sum(1 for f in os.listdir(d) if f.startswith(head) and f.endswith(ext) and f[len(head):-len(ext)].isdigit())
    except OSError:
        return None
    return os.path.join(d, f"{head}#{ext}") if n >= 2 else None


def sevenzip() -> Optional[str]:
    """The 7-Zip command line, whichever flavour is installed: 7zz (7-Zip 21+), 7z/7za (p7zip), 7zr (reduced)."""
    for n in ("7zz", "7z", "7za", "7zr"):
        p = shutil.which(n)
        if p:
            return p
    return None


SEVENZIP_EXTS = (".7z", ".rar", ".cab", ".iso", ".wim", ".arj", ".lzh", ".lha", ".cpio", ".rpm", ".deb", ".z", ".lz", ".lzma", ".zst", ".tar.zst")


class ArchiveOp(threading.Thread):
    """zip | extract with byte-level progress; same interface as FileOp for the progress window.
    zip/tar/tar.gz/tar.xz are done in-process with the stdlib; 7z (and rar, cab, iso… — anything 7-Zip reads)
    go through the 7z command line with its -bsp1 percentage stream parsed for the progress window."""

    def __init__(self, kind: str, items: List[str], dest: str, fmt: str, on_done: Callable):
        super().__init__(daemon=True)
        self.kind, self.items, self.dest, self.fmt, self.on_done = kind, items, dest, fmt, on_done
        self.cancel = Gio.Cancellable()
        self.paused = threading.Event()
        self.total = self.done = 0
        self.total_items = self.done_items = 0
        self.current = ""
        self.errors: List[str] = []
        self.results: List[Tuple[str, str]] = []
        self.started = time.time()
        self.finished = False
        self.title = ""
        self.dialog = None
        self.speed_samples: List[float] = []

    def _wait(self):
        while self.paused.is_set() and not self.cancel.is_cancelled():
            time.sleep(0.1)
        if self.cancel.is_cancelled():
            raise InterruptedError

    def run(self):
        try:
            if self.kind == "zip":
                self._zip()
            else:
                self._extract()
        except InterruptedError:
            pass
        except Exception as ex:
            self.errors.append(str(ex))
        finally:
            self.finished = True
            GLib.idle_add(self.on_done, self)

    def _files(self):
        """[(abs_path, arcname)] for every file in the selection, plus empty dirs."""
        out = []
        for p in self.items:
            base = os.path.basename(p.rstrip("/"))
            if os.path.isdir(p) and not os.path.islink(p):
                for root, dirs, fs in os.walk(p):
                    rel = os.path.relpath(root, os.path.dirname(p))
                    if not fs and not dirs:
                        out.append((root, rel + "/"))
                    for f in fs:
                        out.append((os.path.join(root, f), os.path.join(rel, f)))
            else:
                out.append((p, base))
        return out

    def _zip(self):
        import zipfile, tarfile
        files = self._files()
        self.total_items = len(files)
        for ap, arc in files:
            if arc.endswith("/"):
                continue
            try:
                self.total += os.lstat(ap).st_size
            except OSError:
                pass
        tmp = self.dest + ".part"
        try:
            if self.fmt == "7z":
                sz = sevenzip()
                if not sz:
                    raise RuntimeError("7-Zip is not installed")
                cwd = os.path.dirname(self.items[0].rstrip("/")) or "/"
                names = [os.path.basename(p.rstrip("/")) for p in self.items]
                self._run_7z([sz, "a", "-y", "-bsp1", "-bso0", "-bse1", "-t7z", "-mx=5", tmp, "--"] + names, cwd=cwd)
                self.done_items = self.total_items
            elif self.fmt == "zip":
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                    for ap, arc in files:
                        self._wait()
                        self.current = ap
                        if arc.endswith("/"):
                            z.writestr(arc, b"")
                        else:
                            self._zip_add(z, ap, arc)
                        self.done_items += 1
            else:
                mode = {"gztar": "w:gz", "xztar": "w:xz", "tar": "w"}[self.fmt]
                with tarfile.open(tmp, mode) as tf:
                    for ap, arc in files:
                        self._wait()
                        self.current = ap
                        arc = arc.rstrip("/")
                        if os.path.isdir(ap):
                            tf.add(ap, arc, recursive=False)
                        else:
                            ti = tf.gettarinfo(ap, arc)
                            with open(ap, "rb") as f:
                                tf.addfile(ti, _ProgressReader(f, self))
                        self.done_items += 1
            os.replace(tmp, self.dest)
            self.results.append((self.dest, self.dest))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _zip_add(self, z, ap, arc):
        import zipfile
        zi = zipfile.ZipInfo.from_file(ap, arc)
        zi.compress_type = zipfile.ZIP_DEFLATED
        with open(ap, "rb") as src, z.open(zi, "w", force_zip64=True) as dst:
            while True:
                if self.cancel.is_cancelled():
                    raise InterruptedError
                buf = src.read(1 << 20)
                if not buf:
                    break
                dst.write(buf)
                self.done += len(buf)

    def _run_7z(self, argv: List[str], cwd: Optional[str] = None):
        """Run 7-Zip with progress on stdout (-bsp1), mapping its percentage onto done/total so the dialog's
        bar, speed and ETA work. Pause = SIGSTOP/SIGCONT, cancel = kill."""
        self.total = 10000
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", bufsize=0, start_new_session=True)
        buf = ""
        stopped = False
        try:
            while True:
                if self.cancel.is_cancelled():
                    os.killpg(proc.pid, signal.SIGKILL)
                    raise InterruptedError
                if self.paused.is_set() != stopped:
                    stopped = self.paused.is_set()
                    os.killpg(proc.pid, signal.SIGSTOP if stopped else signal.SIGCONT)
                ch = proc.stdout.read(1)
                if not ch:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if ch in "\r\n\b":
                    line = buf.strip()
                    buf = ""
                    m = re.search(r"(\d{1,3})%(?:\s+\d+)?\s*(?:-\s*)?(.*)", line)
                    if m:
                        self.done = int(m.group(1)) * 100
                        if m.group(2):
                            self.current = m.group(2).strip()
                else:
                    buf += ch
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        err = proc.stderr.read().strip()
        if proc.returncode not in (0, 1):             # 1 = warnings (e.g. a locked file), still produced output
            raise RuntimeError(err.splitlines()[-1] if err else f"7z exited with {proc.returncode}")
        if proc.returncode == 1 and err:
            self.errors.append(err.splitlines()[-1])
        self.done = self.total

    def _extract(self):
        import zipfile, tarfile
        src = self.items[0]
        os.makedirs(self.dest, exist_ok=True)
        dest_real = os.path.realpath(self.dest)
        low = src.lower()
        if not zipfile.is_zipfile(src) and not tarfile.is_tarfile(src):
            sz = sevenzip()
            if not sz:
                raise RuntimeError("7-Zip is needed for this archive type")
            self.total_items = 1
            self.current = os.path.basename(src)
            self._run_7z([sz, "x", "-y", "-bsp1", "-bso0", "-bse1", f"-o{self.dest}", src])
            self.done_items = 1
            self.results.append((src, self.dest))
            return
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as z:
                members = z.infolist()
                self.total_items = len(members)
                self.total = sum(m.file_size for m in members)
                for m in members:
                    self._wait()
                    self.current = m.filename
                    target = os.path.realpath(os.path.join(self.dest, m.filename))
                    if not target.startswith(dest_real + os.sep) and target != dest_real:
                        self.errors.append(f"skipped unsafe path {m.filename}")
                        continue
                    if m.is_dir():
                        os.makedirs(target, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with z.open(m) as fi, open(target, "wb") as fo:
                            while True:
                                if self.cancel.is_cancelled():
                                    raise InterruptedError
                                buf = fi.read(1 << 20)
                                if not buf:
                                    break
                                fo.write(buf)
                                self.done += len(buf)
                        mode = (m.external_attr >> 16) & 0o777
                        if mode:
                            try:
                                os.chmod(target, mode)
                            except OSError:
                                pass
                    self.done_items += 1
        else:
            with tarfile.open(src) as tf:
                members = tf.getmembers()
                self.total_items = len(members)
                self.total = sum(m.size for m in members)
                for m in members:
                    self._wait()
                    self.current = m.name
                    target = os.path.realpath(os.path.join(self.dest, m.name))
                    if not target.startswith(dest_real + os.sep) and target != dest_real:
                        self.errors.append(f"skipped unsafe path {m.name}")
                        continue
                    if m.isfile():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        fi = tf.extractfile(m)
                        with open(target, "wb") as fo:
                            while True:
                                if self.cancel.is_cancelled():
                                    raise InterruptedError
                                buf = fi.read(1 << 20)
                                if not buf:
                                    break
                                fo.write(buf)
                                self.done += len(buf)
                        try:
                            os.chmod(target, m.mode & 0o777)
                            os.utime(target, (m.mtime, m.mtime))
                        except OSError:
                            pass
                    else:
                        try:
                            tf.extract(m, self.dest, set_attrs=False)
                        except Exception as ex:
                            self.errors.append(f"{m.name}: {ex}")
                    self.done_items += 1
        self.results.append((src, self.dest))


class _ProgressReader:
    def __init__(self, f, op):
        self.f, self.op = f, op

    def read(self, n=-1):
        if self.op.cancel.is_cancelled():
            raise InterruptedError
        buf = self.f.read(n)
        self.op.done += len(buf)
        return buf


class SearchOp(threading.Thread):
    def __init__(self, root: str, pattern: str, subfolders: bool, contents: bool, show_hidden: bool, on_batch: Callable):
        super().__init__(daemon=True)
        self.root, self.pattern, self.subfolders, self.contents, self.show_hidden, self.on_batch = root, pattern, subfolders, contents, show_hidden, on_batch
        self.cancel = threading.Event()
        self.scanned = 0

    def run(self):
        pat = self.pattern.lower()
        globby = any(c in pat for c in "*?[")
        batch: List[Entry] = []
        last = time.time()
        for root, dirs, files in os.walk(self.root, onerror=lambda e: None):
            if self.cancel.is_set():
                break
            if not self.show_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in dirs + files:
                if self.cancel.is_set():
                    break
                self.scanned += 1
                if not self.show_hidden and name.startswith("."):
                    continue
                hit = fnmatch.fnmatch(name.lower(), pat) if globby else pat in name.lower()
                p = os.path.join(root, name)
                if not hit and self.contents and name in files:
                    hit = self._grep(p, pat)
                if hit:
                    e = entry_for_path(p)
                    e.location = root
                    batch.append(e)
            if batch and (time.time() - last > 0.25 or len(batch) > 200):
                GLib.idle_add(self.on_batch, batch, False)
                batch, last = [], time.time()
            if not self.subfolders:
                break
        GLib.idle_add(self.on_batch, batch, True)

    @staticmethod
    def _grep(path: str, pat: str) -> bool:
        try:
            if os.path.getsize(path) > 8 << 20:
                return False
            with open(path, "rb") as f:
                data = f.read()
            return b"\0" not in data[:4096] and pat in data.decode("utf-8", "ignore").lower()
        except Exception:
            return False


_DIR_STATS_CACHE: Dict[str, Tuple[float, float, Tuple[int, int, int]]] = {}   # path -> (dir mtime, when, (files, folders, bytes))
DIR_STATS_TTL = 600.0


def dir_stats_cached(path: str, cancel: threading.Event) -> Tuple[int, int, int]:
    """dir_stats with a 10-minute cache keyed on the folder's own mtime, so re-selecting a folder
    (or clicking away and back) doesn't rescan it."""
    try:
        m = os.stat(path).st_mtime
    except OSError:
        m = 0.0
    hit = _DIR_STATS_CACHE.get(path)
    now = time.time()
    if hit and hit[0] == m and now - hit[1] < DIR_STATS_TTL:
        return hit[2]
    res = dir_stats(path, cancel)
    if not cancel.is_set():
        _DIR_STATS_CACHE[path] = (m, now, res)
        if len(_DIR_STATS_CACHE) > 2000:
            for k in list(_DIR_STATS_CACHE)[:500]:
                del _DIR_STATS_CACHE[k]
    return res


def dir_stats(path: str, cancel: threading.Event) -> Tuple[int, int, int]:
    files = folders = total = 0
    for root, ds, fs in os.walk(path, onerror=lambda e: None):
        if cancel.is_set():
            break
        folders += len(ds)
        files += len(fs)
        for f in fs:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return files, folders, total


# --------------------------------------------------------------------------- launching
def launch_default(path_or_uri: str, parent: Optional[Gtk.Window] = None) -> bool:
    uri = path_or_uri if "://" in path_or_uri else Gio.File.new_for_path(path_or_uri).get_uri()
    try:
        ctx = parent.get_display().get_app_launch_context() if parent else None
        Gio.AppInfo.launch_default_for_uri(uri, ctx)
        return True
    except GLib.Error:
        return False


def find_terminal() -> Optional[List[str]]:
    t = os.environ.get("TERMINAL")
    if t and shutil.which(t):
        return [t]
    for cand in ("ptyxis", "gnome-terminal", "konsole", "kitty", "alacritty", "wezterm", "foot", "tilix",
                 "xfce4-terminal", "mate-terminal", "lxterminal", "terminator", "urxvt", "xterm"):
        if shutil.which(cand):
            return [cand]
    return None


def spawn_terminal(cwd: str, cmd: Optional[List[str]] = None) -> bool:
    """Open a terminal in cwd; if cmd is given, run it there (and keep the window open afterwards)."""
    term = find_terminal()
    if not term:
        return False
    name = os.path.basename(term[0])
    argv = list(term)
    if name in ("gnome-terminal", "ptyxis", "tilix", "xfce4-terminal", "mate-terminal", "lxterminal"):
        argv.append("--working-directory=" + cwd)
    elif name == "konsole":
        argv += ["--workdir", cwd]
    elif name in ("kitty", "foot", "alacritty"):
        argv += ["--working-directory", cwd]
    elif name == "wezterm":
        argv += ["start", "--cwd", cwd]
    if cmd:
        quoted = " ".join(shlex.quote(c) for c in cmd)
        wrapped = ["bash", "-c", f"{quoted}; echo; echo \"[process exited with code $?]\"; read -r -p 'Press Enter to close…'"]
        if name in ("gnome-terminal", "ptyxis", "tilix"):
            argv += ["--"] + wrapped
        elif name in ("konsole", "xfce4-terminal", "mate-terminal", "lxterminal", "terminator"):
            argv += ["-e", " ".join(shlex.quote(c) for c in wrapped)] if name != "konsole" else ["-e"] + wrapped
        elif name in ("xterm", "urxvt"):
            argv += ["-e"] + wrapped
        else:
            argv += wrapped
    try:
        subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def script_runner(path: str) -> Optional[List[str]]:
    """How to run this file: python scripts via python3, shell scripts via bash, executables directly."""
    low = path.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(200)
    except OSError:
        head = b""
    if low.endswith(".py") or head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0]:
        return [sys.executable if not shutil.which("python3") else "python3", path]
    if low.endswith((".sh", ".bash")) or head.startswith(b"#!") and (b"/sh" in head.split(b"\n", 1)[0] or b"bash" in head.split(b"\n", 1)[0]):
        return ["bash", path]
    if os.access(path, os.X_OK) and not os.path.isdir(path):
        return [path]
    return None


def run_detached(cmd: List[str], cwd: str) -> bool:
    try:
        subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def shortcut_name(e) -> str:
    """'name - Shortcut.ext' so the link keeps its file type (and icon)."""
    if e.is_dir:
        return e.name + " - Shortcut"
    stem, ext = os.path.splitext(e.name)
    return f"{stem} - Shortcut{ext}"


def set_clipboard_text(text: str) -> None:
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.set_text(text, -1)
    cb.store()


# =========================================================================== extended previews
# Everything here runs on a worker thread and returns plain data (RGB bytes or text); the main thread
# turns it into a GdkPixbuf. Pure-Python decoders cover the VFX formats GdkPixbuf has no loader for
# (Maya IFF, OpenEXR, Radiance HDR); external tools, when installed, cover the long tail.

import struct
import zlib
import math
import tempfile

_HALF_TABLE: Optional[List[float]] = None


def _half_table() -> List[float]:
    """65536-entry lookup for IEEE half → float (struct supports 'e' since Python 3.6)."""
    global _HALF_TABLE
    if _HALF_TABLE is None:
        _HALF_TABLE = [struct.unpack("<e", struct.pack("<H", i))[0] for i in range(65536)]
    return _HALF_TABLE


class RawImage:
    """Decoded preview: 8-bit RGB bytes, row-major, plus metadata for the quick-view card."""

    def __init__(self, w: int, h: int, rgb: bytes, info: Optional[List[Tuple[str, str]]] = None):
        self.w, self.h, self.rgb, self.info = w, h, rgb, info or []


def _tonemap(samples: List[Tuple[float, float, float]], w: int, h: int, exposure: Optional[float] = None) -> bytes:
    """Linear float RGB → display sRGB-ish bytes with auto exposure (geometric-mean key) and gamma 2.2."""
    if exposure is None:
        lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in samples]
        pos = [l for l in lum if l > 1e-6]
        if pos:
            logmean = math.exp(sum(math.log(l) for l in pos[::max(1, len(pos) // 4000)]) / max(1, len(pos[::max(1, len(pos) // 4000)])))
            exposure = 0.18 / logmean if logmean > 0 else 1.0
            exposure = max(0.02, min(exposure, 64.0))
        else:
            exposure = 1.0
    inv = 1.0 / 2.2
    out = bytearray(w * h * 3)
    i = 0
    for r, g, b in samples:
        for c in (r, g, b):
            v = c * exposure
            v = v / (1.0 + v)                 # Reinhard, keeps highlights from clipping
            v = 0.0 if not (v > 0) else (1.0 if v >= 1 else v ** inv)      # 'not (v > 0)' also catches NaN
            out[i] = int(v * 255 + 0.5)
            i += 1
    return bytes(out)


def _tonemap_np(rgb, exposure: Optional[float] = None) -> bytes:
    """numpy version of _tonemap: rgb is an (N, 3) float array of linear values."""
    rgb = np.nan_to_num(rgb.astype(np.float32), nan=0.0, posinf=1e4, neginf=0.0)
    if exposure is None:
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        pos = lum[lum > 1e-6]
        if len(pos):
            logmean = float(np.exp(np.log(pos[::max(1, len(pos) // 20000)]).mean()))
            exposure = max(0.02, min(0.18 / logmean if logmean > 0 else 1.0, 64.0))
        else:
            exposure = 1.0
    v = rgb * np.float32(exposure)
    v = v / (1.0 + v)
    v = np.clip(v, 0.0, 1.0) ** np.float32(1 / 2.2)
    return (v * 255 + 0.5).astype(np.uint8).tobytes()


def _decode_exr_np(data, offsets, compression, lines_per, line_bytes, chan_off, rgb_names, sizes, w, h, ymin, ymax, stride, ow, oh, names):
    """Vectorised scanline EXR decode: per chunk, zlib/RLE → un-delta (cumsum) → interleave (reshape) → view
    the needed channels as float16/float32/uint32 → sample every `stride`-th column. ~50× the pure-Python path."""
    import zlib
    rows = []
    dt_for = {1: "<f2", 2: "<f4", 0: "<u4"}
    for bi, o in enumerate(offsets):
        y0 = struct.unpack_from("<i", data, o)[0]
        size = struct.unpack_from("<i", data, o + 4)[0]
        nlines = min(lines_per, ymax - y0 + 1)
        want = [k for k in range(nlines) if ((y0 - ymin) + k) % stride == 0]
        if not want:
            continue
        raw = data[o + 8:o + 8 + size]
        expected = nlines * line_bytes
        if size < expected and compression in (1, 2, 3):
            dec = zlib.decompress(raw) if compression in (2, 3) else _exr_rle(raw)
            d = np.frombuffer(dec, dtype=np.uint8).astype(np.int32)
            und = ((np.cumsum(d - 128) + 128) & 255).astype(np.uint8)       # un-delta
            n = len(und)
            half_n = (n + 1) // 2
            buf = np.empty(n, dtype=np.uint8)
            buf[0::2] = und[:half_n]
            buf[1::2] = und[half_n:half_n + n // 2]                            # interleave
        else:
            buf = np.frombuffer(raw, dtype=np.uint8)
        for k in want:
            base = k * line_bytes
            chans = []
            for cname in rgb_names:
                off, ptype = chan_off[cname]
                arr = np.frombuffer(buf[base + off: base + off + sizes[ptype] * w].tobytes(), dtype=dt_for[ptype])
                if ptype == 0:
                    arr = arr.astype(np.float32) / 4294967295.0
                chans.append(arr[::stride].astype(np.float32))
            rows.append(np.stack(chans, axis=1))
    if not rows:
        return None
    rgb = np.concatenate(rows)
    if len(rgb) != ow * oh:
        pad = np.zeros((max(0, ow * oh - len(rgb)), 3), dtype=np.float32)
        rgb = np.concatenate((rgb, pad))[:ow * oh]
    comp = {0: "uncompressed", 1: "RLE", 2: "ZIPS", 3: "ZIP"}[compression]
    return RawImage(ow, oh, _tonemap_np(rgb),
                    [("Format", f"OpenEXR {comp}, {len(names)} channel{'s' if len(names) != 1 else ''}"),
                     ("Dimensions", f"{w} × {h} px"), ("Channels", ", ".join(names[:12]) + ("…" if len(names) > 12 else ""))])


def _stride_for(w: int, h: int, max_w: int, max_h: int) -> int:
    return max(1, int(math.ceil(max(w / max_w, h / max_h))))


# --------------------------------------------------------------------------- Maya IFF (.iff, 8-bit RGB/RGBA, RLE or raw)
def decode_maya_iff(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"FOR4" or data[8:12] != b"CIMG":
        return None
    pos = 12
    end = 8 + struct.unpack(">I", data[4:8])[0]
    width = height = 0
    flags = bpc = tiles = compress = 0
    rgba_chunks = []

    def walk(pos, end):
        nonlocal width, height, flags, bpc, tiles, compress
        while pos + 8 <= end:
            cid, size = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
            body = pos + 8
            if cid == b"FOR4":
                walk(body + 4, body + size)
            elif cid == b"TBHD":
                width, height, _prn, _prd, flags, bpc, tiles, compress = struct.unpack(">IIHHIHHI", data[body:body + 24])
            elif cid == b"RGBA":
                rgba_chunks.append((body, size))
            pos = body + size + (size & 1) + ((4 - (size + (size & 1)) % 4) % 4 if (size % 4) else 0)
    walk(pos, end)
    if not width or not height or bpc != 0:
        return None            # 16-bit IFFs go to the external-tool path
    nch = 4 if (flags & 2) else 3
    stride = _stride_for(width, height, max_w, max_h)
    ow, oh = (width + stride - 1) // stride, (height + stride - 1) // stride
    out = bytearray(ow * oh * 3)
    for body, size in rgba_chunks:
        x1, y1, x2, y2 = struct.unpack(">HHHH", data[body:body + 8])
        tw, th = x2 - x1 + 1, y2 - y1 + 1
        raw = data[body + 8:body + size]
        npx = tw * th
        if len(raw) == npx * nch:
            # uncompressed: interleaved pixels, channel order B G R (A)
            planes = [raw[c::nch] for c in range(nch)]
            chans = [planes[2], planes[1], planes[0]]      # stored B,G,R(,A) → R,G,B
        else:
            # RLE: planes stored one after another, order A,B,G,R (or B,G,R)
            dec, p = [], 0
            for _c in range(nch):
                plane = bytearray()
                need = npx
                while len(plane) < need and p < len(raw):
                    b = raw[p]
                    p += 1
                    cnt = (b & 0x7F) + 1
                    if b & 0x80:
                        plane += bytes([raw[p]]) * cnt
                        p += 1
                    else:
                        plane += raw[p:p + cnt]
                        p += cnt
                dec.append(bytes(plane[:need]))
            chans = [dec[-1], dec[-2], dec[-3]]     # R, G, B
        rch, gch, bch = chans
        for ty in range(th):
            sy = y1 + ty                      # IFF rows run bottom-up
            if sy % stride:
                continue
            oy = (height - 1 - sy) // stride
            if oy < 0 or oy >= oh:
                continue
            row = ty * tw
            for tx in range(0, tw):
                sx = x1 + tx
                if sx % stride:
                    continue
                ox = sx // stride
                o = (oy * ow + ox) * 3
                i = row + tx
                out[o], out[o + 1], out[o + 2] = rch[i], gch[i], bch[i]
    return RawImage(ow, oh, bytes(out), [("Format", f"Maya IFF {'RGBA' if nch == 4 else 'RGB'} 8-bit{' RLE' if compress else ''}"),
                                         ("Dimensions", f"{width} × {height} px")])


# --------------------------------------------------------------------------- Radiance HDR (.hdr / .pic)
def decode_hdr(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"#?"):
        return None
    pos = data.find(b"\n\n")
    if pos < 0:
        return None
    header, pos = data[:pos], pos + 2
    nl = data.find(b"\n", pos)
    res = data[pos:nl].decode("ascii", "ignore").split()
    pos = nl + 1
    if len(res) != 4 or res[0] != "-Y" or res[2] != "+X":
        return None
    h, w = int(res[1]), int(res[3])
    stride = _stride_for(w, h, max_w, max_h)
    ow, oh = (w + stride - 1) // stride, (h + stride - 1) // stride
    samples: List[Tuple[float, float, float]] = []
    for y in range(h):
        if pos + 4 > len(data):
            break
        if data[pos] == 2 and data[pos + 1] == 2 and (data[pos + 2] << 8 | data[pos + 3]) == w and w >= 8 and w < 32768:
            pos += 4
            planes = []
            for _c in range(4):
                plane = bytearray()
                while len(plane) < w:
                    c = data[pos]
                    pos += 1
                    if c > 128:
                        plane += bytes([data[pos]]) * (c - 128)
                        pos += 1
                    else:
                        plane += data[pos:pos + c]
                        pos += c
                planes.append(plane)
            scan = [(planes[0][x], planes[1][x], planes[2][x], planes[3][x]) for x in range(0, w, stride)]
        else:
            scan = [tuple(data[pos + x * 4:pos + x * 4 + 4]) for x in range(0, w, stride)]
            pos += w * 4
        if y % stride == 0:
            for r, g, b, e in scan:
                if e:
                    f = math.ldexp(1.0, e - 136)
                    samples.append(((r + 0.5) * f, (g + 0.5) * f, (b + 0.5) * f))
                else:
                    samples.append((0.0, 0.0, 0.0))
    if len(samples) != ow * oh:
        samples = (samples + [(0.0, 0.0, 0.0)] * (ow * oh))[:ow * oh]
    return RawImage(ow, oh, _tonemap(samples, ow, oh), [("Format", "Radiance HDR (RGBE)"), ("Dimensions", f"{w} × {h} px")])


# --------------------------------------------------------------------------- OpenEXR (scanline; NONE / RLE / ZIPS / ZIP)
def decode_exr(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x76\x2f\x31\x01":
        return None
    version = struct.unpack("<I", data[4:8])[0]
    if version & 0x200 or version & 0x1000:          # tiled / multipart: external tools
        return None
    pos = 8
    channels, compression, dw = [], 0, None

    def cstr(p):
        e = data.index(b"\0", p)
        return data[p:e].decode("latin-1"), e + 1
    while True:
        name, pos = cstr(pos)
        if name == "":
            break
        typ, pos = cstr(pos)
        size = struct.unpack("<i", data[pos:pos + 4])[0]
        pos += 4
        body = data[pos:pos + size]
        pos += size
        if name == "channels" and typ == "chlist":
            p = 0
            while p < len(body) and body[p] != 0:
                e = body.index(b"\0", p)
                cname = body[p:e].decode("latin-1")
                ptype = struct.unpack("<i", body[e + 1:e + 5])[0]
                xs, ys = struct.unpack("<ii", body[e + 9:e + 17])
                channels.append((cname, ptype, xs, ys))
                p = e + 17
        elif name == "compression":
            compression = body[0]
        elif name == "dataWindow":
            dw = struct.unpack("<iiii", body)
    if dw is None or not channels:
        return None
    xmin, ymin, xmax, ymax = dw
    w, h = xmax - xmin + 1, ymax - ymin + 1
    if any(xs != 1 or ys != 1 for _, _, xs, ys in channels):
        return None
    lines_per = {0: 1, 1: 1, 2: 1, 3: 16}.get(compression)
    if lines_per is None:
        return None                                   # PIZ/PXR24/B44/DWA → external tools
    sizes = {0: 4, 1: 2, 2: 4}                        # UINT, HALF, FLOAT
    chan_order = sorted(channels, key=lambda c: c[0])  # EXR stores channels alphabetically
    names = [c[0] for c in chan_order]

    def pick(suffixes):
        for n in names:
            if n in suffixes or n.split(".")[-1] in suffixes:
                return n
        return None
    rn, gn, bn = pick(("R",)), pick(("G",)), pick(("B",))
    if rn is None:
        yn = pick(("Y",))
        if yn is None:
            rn = gn = bn = names[0]
        else:
            rn = gn = bn = yn
    gn, bn = gn or rn, bn or rn
    nblocks = (h + lines_per - 1) // lines_per
    offsets = struct.unpack("<%dQ" % nblocks, data[pos:pos + 8 * nblocks])
    stride = _stride_for(w, h, max_w, max_h)
    ow, oh = (w + stride - 1) // stride, (h + stride - 1) // stride
    half = _half_table()
    line_bytes = sum(sizes[t] * w for _, t, _, _ in chan_order)
    chan_off: Dict[str, Tuple[int, int]] = {}
    off = 0
    for cname, ptype, _, _ in chan_order:
        chan_off[cname] = (off, ptype)
        off += sizes[ptype] * w
    if np is not None:
        return _decode_exr_np(data, offsets, compression, lines_per, line_bytes, chan_off, (rn, gn, bn), sizes, w, h, ymin, ymax, stride, ow, oh, names)
    samples: List[Tuple[float, float, float]] = []

    def rd(buf, base, ptype, x):
        if ptype == 1:
            return half[buf[base + x * 2] | (buf[base + x * 2 + 1] << 8)]
        if ptype == 2:
            return struct.unpack_from("<f", buf, base + x * 4)[0]
        return struct.unpack_from("<I", buf, base + x * 4)[0] / 4294967295.0

    for bi, o in enumerate(offsets):
        y0 = struct.unpack_from("<i", data, o)[0]
        size = struct.unpack_from("<i", data, o + 4)[0]
        raw = data[o + 8:o + 8 + size]
        nlines = min(lines_per, ymax - y0 + 1)
        expected = nlines * line_bytes
        if any(((y0 - ymin) + k) % stride == 0 for k in range(nlines)):
            if compression in (2, 3):
                buf = _exr_unpredict(zlib.decompress(raw)) if size < expected else raw
            elif compression == 1:
                buf = _exr_unpredict(_exr_rle(raw)) if size < expected else raw
            else:
                buf = raw
            for k in range(nlines):
                sy = (y0 - ymin) + k
                if sy % stride:
                    continue
                base = k * line_bytes
                ro, rt = chan_off[rn]
                go, gt = chan_off[gn]
                bo, bt = chan_off[bn]
                for x in range(0, w, stride):
                    samples.append((rd(buf, base + ro, rt, x), rd(buf, base + go, gt, x), rd(buf, base + bo, bt, x)))
    if len(samples) != ow * oh:
        samples = (samples + [(0.0, 0.0, 0.0)] * (ow * oh))[:ow * oh]
    comp = {0: "uncompressed", 1: "RLE", 2: "ZIPS", 3: "ZIP"}[compression]
    return RawImage(ow, oh, _tonemap(samples, ow, oh),
                    [("Format", f"OpenEXR {comp}, {len(channels)} channel{'s' if len(channels) != 1 else ''}"),
                     ("Dimensions", f"{w} × {h} px"), ("Channels", ", ".join(names[:12]) + ("…" if len(names) > 12 else ""))])


def _exr_rle(src: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        i += 1
        if c > 127:                 # signed negative: literal run
            cnt = 256 - c
            out += src[i:i + cnt]
            i += cnt
        else:
            out += bytes([src[i]]) * (c + 1)
            i += 1
    return bytes(out)


def _exr_unpredict(buf: bytes) -> bytes:
    """Undo EXR's delta + interleave predictor used by ZIP/ZIPS/RLE."""
    # OpenEXR compresses as: reorder (split into even/odd byte halves) THEN delta-code. So we undo the delta
    # first, then interleave the two halves back together.
    b = bytearray(buf)
    for i in range(1, len(b)):
        b[i] = (b[i - 1] + b[i] - 128) & 0xFF
    half = (len(b) + 1) // 2
    out = bytearray(len(b))
    out[0::2] = b[:half][:len(out[0::2])]
    out[1::2] = b[half:half + len(out[1::2])]
    return bytes(out)


# --------------------------------------------------------------------------- external converters
# --------------------------------------------------------------------------- Photoshop PSD / PSB
def _packbits_line(data: bytes, pos: int, end: int, out_len: int) -> bytes:
    """Decode one PackBits-compressed scanline (data[pos:end]) to out_len bytes."""
    out = bytearray()
    i = pos
    while i < end and len(out) < out_len:
        n = data[i]
        i += 1
        if n < 128:
            out += data[i:i + n + 1]
            i += n + 1
        elif n > 128:
            out += data[i:i + 1] * (257 - n)
            i += 1
    return bytes(out[:out_len])


def decode_psd(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    """Photoshop PSD/PSB preview from the flattened composite that Photoshop stores at the end of the file
    (when 'Maximize Compatibility' was on, which is the default). Handles RGB / grayscale / CMYK / indexed /
    duotone / Lab-ish, 8- and 16-bit, raw or RLE. Only every k-th scanline is decoded for the preview size,
    which is what keeps a 10k×10k file under a second. If the composite is missing (compatibility off), the
    embedded JPEG thumbnail (image resource 1036) is used instead; failing that, the caller falls back to
    ImageMagick."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"8BPS":
        return None
    version = struct.unpack(">H", data[4:6])[0]
    psb = version == 2
    channels, height, width, depth, mode = struct.unpack(">HIIHH", data[12:26])
    mode_name = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK", 7: "Multichannel", 8: "Duotone", 9: "Lab"}.get(mode, str(mode))
    pos = 26
    cm_len = struct.unpack(">I", data[pos:pos + 4])[0]
    palette = data[pos + 4:pos + 4 + cm_len] if mode == 2 else b""
    pos += 4 + cm_len
    res_len = struct.unpack(">I", data[pos:pos + 4])[0]
    resources = data[pos + 4:pos + 4 + res_len]
    pos += 4 + res_len
    # image resources: count layers? we only want the thumbnail (1036/1033) as a fallback
    thumb = None
    r = 0
    while r + 12 <= len(resources):
        if resources[r:r + 4] != b"8BIM":
            break
        rid = struct.unpack(">H", resources[r + 4:r + 6])[0]
        nlen = resources[r + 6]
        r += 7 + nlen + ((nlen + 1) % 2)
        size = struct.unpack(">I", resources[r:r + 4])[0]
        r += 4
        if rid in (1036, 1033):
            thumb = resources[r + 28:r + size]          # 28-byte thumbnail header, then JPEG
        r += size + (size % 2)
    lm_len = struct.unpack(">Q" if psb else ">I", data[pos:pos + (8 if psb else 4)])[0]
    pos += (8 if psb else 4) + lm_len
    info = [("Dimensions", f"{width} × {height}"), ("Mode", f"{mode_name} {depth}-bit, {channels} channel{'s' if channels != 1 else ''}"),
            ("Format", "PSB (large document)" if psb else "PSD")]
    # count layers for the quick view (cheap: the layer info header)
    try:
        lp = pos - lm_len
        li_len = struct.unpack(">Q" if psb else ">I", data[lp:lp + (8 if psb else 4)])[0]
        if li_len:
            lp += 8 if psb else 4
            nlayers = abs(struct.unpack(">h", data[lp:lp + 2])[0])
            info.append(("Layers", str(nlayers)))
    except struct.error:
        pass
    if pos + 2 > len(data) or width == 0 or height == 0:
        return _psd_thumb(thumb, info)
    compression = struct.unpack(">H", data[pos:pos + 2])[0]
    pos += 2
    bps = depth // 8 if depth >= 8 else 1
    if depth not in (8, 16) and mode != 0:
        return _psd_thumb(thumb, info)
    # scanline sampling
    step = max(1, int(math.ceil(max(width / max(max_w, 1), height / max(max_h, 1)))))
    rows = list(range(0, height, step))
    cols = list(range(0, width, step))
    ow, oh = len(cols), len(rows)
    row_bytes = width * bps if depth >= 8 else (width + 7) // 8
    nch = channels
    planes: List[List[bytes]] = []
    if compression == 0:
        for c in range(nch):
            base = pos + c * row_bytes * height
            if base + row_bytes * height > len(data) + 1:
                return _psd_thumb(thumb, info)
            planes.append([data[base + y * row_bytes: base + (y + 1) * row_bytes] for y in rows])
    elif compression == 1:
        cnt_sz = 4 if psb else 2
        counts = struct.unpack(f">{nch * height}{'I' if psb else 'H'}", data[pos:pos + nch * height * cnt_sz])
        p = pos + nch * height * cnt_sz
        offsets = []
        for c in range(nch):
            for y in range(height):
                offsets.append(p)
                p += counts[c * height + y]
        for c in range(nch):
            lines = []
            for y in rows:
                k = c * height + y
                lines.append(_packbits_line(data, offsets[k], offsets[k] + counts[k], row_bytes))
            planes.append(lines)
    else:
        return _psd_thumb(thumb, info)      # ZIP-compressed composites are rare (PSB only)

    def px8(line: bytes, x: int) -> int:
        if depth == 16:
            return line[2 * x] if 2 * x < len(line) else 0
        return line[x] if x < len(line) else 0
    out = bytearray(ow * oh * 3)
    o = 0
    for ri, y in enumerate(rows):
        L = [pl[ri] for pl in planes]
        for x in cols:
            if mode == 3 and nch >= 3:
                out[o], out[o + 1], out[o + 2] = px8(L[0], x), px8(L[1], x), px8(L[2], x)
            elif mode == 4 and nch >= 4:
                c, m, yy, k = (px8(L[i], x) for i in range(4))          # Photoshop stores CMYK inverted
                out[o], out[o + 1], out[o + 2] = c * k // 255, m * k // 255, yy * k // 255
            elif mode == 2 and palette:
                idx = px8(L[0], x)
                out[o], out[o + 1], out[o + 2] = palette[idx], palette[256 + idx], palette[512 + idx]
            elif mode == 0:
                bit = (L[0][x // 8] >> (7 - x % 8)) & 1 if x // 8 < len(L[0]) else 0
                out[o] = out[o + 1] = out[o + 2] = 0 if bit else 255
            else:                                                       # grayscale, duotone, Lab (L only), multichannel
                g = px8(L[0], x)
                out[o] = out[o + 1] = out[o + 2] = g
            o += 3
    if nch == 4 and mode == 3:
        info.append(("Alpha", "yes"))
    if step > 1:
        info.append(("Preview", f"1/{step} of the composite"))
    return RawImage(ow, oh, bytes(out), info)


def _psd_thumb(thumb: Optional[bytes], info: list) -> Optional[RawImage]:
    """Fallback: decode the embedded JPEG thumbnail with GdkPixbuf (worker process has no display, which is fine)."""
    if not thumb:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        loader.write(thumb)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return None
        if pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0) if False else pb
        w, h, rs, nch = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_n_channels()
        raw = pb.get_pixels()
        rgb = bytearray(w * h * 3)
        for y in range(h):
            row = raw[y * rs: y * rs + w * nch]
            if nch == 3:
                rgb[y * w * 3:(y + 1) * w * 3] = row
            else:
                for x in range(w):
                    rgb[(y * w + x) * 3:(y * w + x) * 3 + 3] = row[x * nch:x * nch + 3]
        return RawImage(w, h, bytes(rgb), info + [("Preview", "embedded thumbnail (composite not saved — enable 'Maximize compatibility')")])
    except Exception:
        return None


def _tool(*names) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def convert_with_tools(path: str, ct: str, max_w: int, max_h: int) -> Optional[Tuple[str, str]]:
    """Try installed converters; returns (png_path, tool_name) or None. Caller deletes the png."""
    out = os.path.join(tempfile.gettempdir(), f"kestrel-preview-{os.getpid()}-{abs(hash(path)) & 0xffffff:x}.png")
    low = path.lower()
    attempts = []
    is_video = ct.startswith("video/") or low.endswith((".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".r3d"))
    if is_video:
        ff = _tool("ffmpeg")
        if ff:
            attempts.append((ff, [ff, "-v", "error", "-y", "-ss", "1", "-i", path, "-frames:v", "1",
                                  "-vf", f"scale='min({max_w},iw)':-2", out]))
    if low.endswith(".pdf"):
        pt = _tool("pdftoppm")
        if pt:
            attempts.append((pt, [pt, "-png", "-singlefile", "-r", "40", "-f", "1", "-l", "1", path, out[:-4]]))
    oiio = _tool("oiiotool")
    if oiio:
        attempts.append((oiio, [oiio, path, "--subimage", "0", "--ch", "R,G,B", "--resize", f"{max_w}x{max_h}", "--colorconvert", "linear", "sRGB", "-o", out]))
        attempts.append((oiio, [oiio, path, "--subimage", "0", "--resize", f"{max_w}x{max_h}", "-o", out]))
    im = _tool("magick", "convert")
    if im:
        attempts.append((im, [im, path + "[0]", "-resize", f"{max_w}x{max_h}", "-colorspace", "sRGB", "-alpha", "off", out]))
    imgcvt = _tool("imgcvt")           # ships with Maya: handles .iff, 16-bit, .tdi …
    if imgcvt:
        attempts.append((imgcvt, [imgcvt, "-t", "png", path, out]))
    for tool, argv in attempts:
        try:
            r = subprocess.run(argv, capture_output=True, timeout=20)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return out, os.path.basename(tool)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            os.remove(out)
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------- information previews (text)
def describe_archive(path: str) -> Optional[str]:
    import zipfile, tarfile
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                total = sum(i.file_size for i in infos)
                lines = [f"{len(infos)} entries · {fmt_size(total)} uncompressed · {fmt_size(os.path.getsize(path))} on disk", ""]
                lines += [f"{fmt_size(i.file_size):>10}   {i.filename}" for i in infos[:400]]
                if len(infos) > 400:
                    lines.append(f"… {len(infos) - 400} more")
                return "\n".join(lines)
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                members = []
                for m in t:
                    members.append(m)
                    if len(members) > 400:
                        break
                lines = [f"{'400+' if len(members) > 400 else len(members)} entries · {fmt_size(os.path.getsize(path))} on disk", ""]
                lines += [f"{fmt_size(m.size):>10}   {m.name}{'/' if m.isdir() else ''}" for m in members[:400]]
                return "\n".join(lines)
    except Exception as ex:
        return f"Could not read archive: {ex}"
    sz = sevenzip()
    if sz:
        try:
            # (no -ba: p7zip 16.02 on EL9 predates it; the archive's own header block is filtered out below)
            r = subprocess.run([sz, "l", "-slt", "--", path], capture_output=True, text=True, errors="replace", timeout=30)
            if r.returncode == 0 and "Path = " in r.stdout:
                entries, cur = [], {}
                body = r.stdout.split("\n----------\n", 1)[-1]        # everything after the header separator
                for line in body.splitlines():
                    if not line.strip():
                        if cur.get("Path"):
                            entries.append(cur)
                        cur = {}
                    elif " = " in line:
                        k, v = line.split(" = ", 1)
                        cur[k] = v
                if cur.get("Path"):
                    entries.append(cur)
                entries = [e for e in entries if not (e.get("Path") in (path, os.path.basename(path)) and "Type" in e)]
                total = sum(int(e.get("Size") or 0) for e in entries)
                lines = [f"{len(entries)} entries · {fmt_size(total)} uncompressed · {fmt_size(os.path.getsize(path))} on disk  (7-Zip)", ""]
                for e in entries[:400]:
                    is_dir = (e.get("Attributes") or "").startswith("D") or (e.get("Folder") == "+")
                    lines.append(f"{'' if is_dir else fmt_size(int(e.get('Size') or 0)):>10}   {e['Path']}{'/' if is_dir else ''}")
                if len(entries) > 400:
                    lines.append(f"… {len(entries) - 400} more")
                return "\n".join(lines)
            if r.returncode != 0:
                return f"7-Zip could not read this archive: {(r.stderr.strip().splitlines() or ['unknown error'])[-1]}"
        except (OSError, subprocess.TimeoutExpired) as ex:
            return f"Could not read archive: {ex}"
    elif path.lower().endswith(SEVENZIP_EXTS):
        return "This archive type needs 7-Zip to list and extract.\nTools → Dependencies (p7zip)"
    return None


def describe_media(path: str) -> List[Tuple[str, str]]:
    """Duration / resolution / codecs via ffprobe when available."""
    fp = _tool("ffprobe")
    if not fp:
        return []
    try:
        r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
                            "-of", "json", path], capture_output=True, text=True, timeout=10)
        j = json.loads(r.stdout or "{}")
    except Exception:
        return []
    rows = []
    f = j.get("format", {})
    if f.get("duration"):
        d = float(f["duration"])
        rows.append(("Duration", f"{int(d // 3600):d}:{int(d % 3600 // 60):02d}:{d % 60:05.2f}" if d >= 3600 else f"{int(d // 60)}:{d % 60:05.2f}"))
    if f.get("bit_rate"):
        rows.append(("Bit rate", f"{int(f['bit_rate']) // 1000} kb/s"))
    for s in j.get("streams", []):
        if s.get("codec_type") == "video":
            fr = s.get("r_frame_rate", "")
            try:
                a, b = fr.split("/")
                fr = f"{float(a) / float(b):.3g} fps"
            except Exception:
                pass
            rows.append(("Video", f"{s.get('codec_name', '?')} {s.get('width', '?')}×{s.get('height', '?')} {fr}"))
        elif s.get("codec_type") == "audio":
            rows.append(("Audio", f"{s.get('codec_name', '?')} {s.get('sample_rate', '?')} Hz {s.get('channels', '?')} ch"))
    return rows


def describe_obj(path: str) -> List[Tuple[str, str]]:
    v = vn = vt = fc = 0
    groups = 0
    try:
        with open(path, "rb") as f:
            for line in f.readlines(8 << 20):
                if line.startswith(b"v "):
                    v += 1
                elif line.startswith(b"f "):
                    fc += 1
                elif line.startswith(b"vn "):
                    vn += 1
                elif line.startswith(b"vt "):
                    vt += 1
                elif line.startswith((b"g ", b"o ")):
                    groups += 1
    except OSError:
        return []
    return [("Vertices", f"{v:,}"), ("Faces", f"{fc:,}"), ("Normals / UVs", f"{vn:,} / {vt:,}"), ("Groups", f"{groups:,}")]


_POOL = None


# --------------------------------------------------------------------------- non-blocking existence checks
class PathCache:
    """os.path.exists / isdir for paths that may live on a network share, without ever blocking the GTK
    thread. The first query of a path answers optimistically (True) and schedules a real stat on a worker
    thread; later queries return the cached verdict, re-checked at most every 30 s. A dead NFS/SMB mount
    costs the worker a hang, not the window."""

    def __init__(self):
        self._v: Dict[str, Tuple[bool, bool, float]] = {}        # path → (exists, isdir, checked_at)
        self._pending: set = set()
        self._lock = threading.Lock()
        self.on_change: Optional[Callable] = None               # called (idle) when a verdict flips

    def _schedule(self, path: str):
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)

        def work():
            pin_worker()
            try:
                st = os.stat(path)
                ex, isd = True, _stat.S_ISDIR(st.st_mode)
            except OSError:
                ex, isd = False, False
            with self._lock:
                old = self._v.get(path)
                self._v[path] = (ex, isd, time.monotonic())
                self._pending.discard(path)
            if old is not None and old[:2] != (ex, isd) and self.on_change:
                GLib.idle_add(lambda: (self.on_change(), False)[1])
            elif old is None and not ex and self.on_change:
                GLib.idle_add(lambda: (self.on_change(), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _get(self, path: str) -> Tuple[bool, bool]:
        with self._lock:
            v = self._v.get(path)
        if v is None:
            self._schedule(path)
            return True, True                  # optimistic until the worker answers
        if time.monotonic() - v[2] > 30:
            self._schedule(path)
        return v[0], v[1]

    def exists(self, path: str) -> bool:
        return self._get(path)[0]

    def isdir(self, path: str) -> bool:
        ex, isd = self._get(path)
        return ex and isd


PATHS = PathCache()


# --------------------------------------------------------------------------- CPU parking
# Preview work (decoders, model loading, software rendering, thumbnails) is kept off the cores the interface
# runs on, so a 5M-triangle render or a 100 MB EXR decode can never starve the GTK main loop. UI_CORES is set
# from the config at startup; with fewer than 4 CPUs nothing is pinned.
UI_CORES = 2


def _cpu_sets():
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None, None
    n = max(0, min(UI_CORES, len(cpus) - 2))
    if len(cpus) < 4 or n == 0:
        return None, None
    return set(cpus[:n]), set(cpus[n:])


def pin_ui_thread():
    """Call from the GTK main thread once: it lives on the reserved cores."""
    ui, _ = _cpu_sets()
    if ui:
        try:
            os.sched_setaffinity(threading.get_native_id(), ui)
        except (AttributeError, OSError):
            pass


def pin_worker():
    """Call at the start of any preview worker thread or process: everything except the reserved cores,
    and a lower scheduling priority so the interface wins ties on shared caches/memory bandwidth."""
    _, work = _cpu_sets()
    if work:
        try:
            os.sched_setaffinity(threading.get_native_id(), work)
        except (AttributeError, OSError):
            pass
    try:
        if os.getpid() != MAIN_PID:
            os.nice(5)
    except OSError:
        pass


MAIN_PID = os.getpid()


def install_thread_pinning():
    """New threads inherit the creating thread's CPU mask, so threads spawned by the (pinned) GTK thread
    would land on the reserved cores. Wrap threading.Thread so every Python worker thread moves itself to
    the worker cores as its first act."""
    _, work = _cpu_sets()
    if not work:
        return
    base = threading.Thread

    class PinnedThread(base):
        def run(self):
            pin_worker()
            super().run()
    threading.Thread = PinnedThread


def warm_workers():
    """Fork the decoder pool (and the 3D render worker) right after startup, while RSS is a few tens of MB.
    os.fork() copies the page tables with the GIL held, so forking later — with a 5M-face mesh and thumbnail
    caches resident — would freeze the interface for a noticeable fraction of a second."""
    try:
        run_heavy(int, 0)
    except Exception:
        pass
    try:
        get_render_worker()
    except Exception:
        pass


def run_heavy(fn, *args):
    """Run a CPU-bound decoder in a forked worker process (one at a time). Pure-Python decoding holds the
    GIL, which would make the GTK main loop stutter; a separate process keeps the window fluid. Falls back
    to an in-thread call if the pool can't be used."""
    global _POOL
    try:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("fork"), initializer=pin_worker)
        return _POOL.submit(fn, *args).result(timeout=240)
    except Exception:
        try:
            if _POOL is not None:
                _POOL.shutdown(wait=False)
        except Exception:
            pass
        _POOL = None
        return fn(*args)


PREVIEW_DECODERS = {".iff": decode_maya_iff, ".tdi": decode_maya_iff, ".exr": decode_exr, ".hdr": decode_hdr, ".pic": decode_hdr, ".rgbe": decode_hdr,
                    ".psd": decode_psd, ".psb": decode_psd}


def generate_preview(path: str, ct: str, max_w: int, max_h: int) -> dict:
    """Worker-thread entry point. Returns {'raw': RawImage} | {'png': path, 'tool': name} | {'text': str} | {}
    always with an 'info' list for the quick-view card."""
    ext = os.path.splitext(path)[1].lower()
    info: List[Tuple[str, str]] = []
    try:
        if ext in PREVIEW_DECODERS and os.path.getsize(path) < 400 << 20:
            try:
                img = run_heavy(PREVIEW_DECODERS[ext], path, max_w, max_h)
                if img is not None:
                    return {"raw": img, "info": img.info}
            except Exception as ex:
                info.append(("Decoder", f"built-in failed ({ex.__class__.__name__}); trying tools"))
        if ext == ".obj":
            info += describe_obj(path)
        if ct.startswith(("video/", "audio/")) or ext in (".mov", ".mp4", ".mkv", ".mxf", ".wav", ".flac", ".mp3", ".aif", ".aiff"):
            info += describe_media(path)
        arch = describe_archive(path) if (ext in (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".whl") or path.lower().endswith(SEVENZIP_EXTS)) else None
        if arch:
            return {"text": arch, "info": info}
        conv = convert_with_tools(path, ct, max_w, max_h)
        if conv:
            png, tool = conv
            info.append(("Preview via", tool))
            return {"png": png, "tool": tool, "info": info}
    except Exception as ex:
        info.append(("Preview", f"failed: {ex}"))
    return {"info": info}


# --------------------------------------------------------------------------- profiling: main-loop stall watchdog
class StallWatchdog:
    """Finds what freezes the interface. A GLib timeout stamps a heartbeat every 50 ms on the GTK thread; a
    watcher thread checks the stamp every 20 ms, and whenever it is more than 120 ms stale, samples the GTK
    thread's Python stack (sys._current_frames) until the heartbeat resumes. Each stall is recorded with its
    duration and the most common sampled stack, so the report says exactly which function the UI thread was
    stuck in. Overhead when nothing stalls: one timestamp per 50 ms and one comparison per 20 ms.
    Also records long synchronous handlers that don't stall the loop quite that badly (>60 ms), from a
    second, coarser heartbeat, so death-by-a-thousand-cuts shows up too."""

    THRESHOLD = 0.12
    FREEZE = 4.0              # seconds: a stall this long is a lock-up → dump every thread's stack to a file
    MAX_RECORDS = 400

    def __init__(self):
        self.records: List[dict] = []
        self.total_stall = 0.0
        self.started = time.time()
        self._beat = time.monotonic()
        self._main_tid = threading.get_ident()
        self._running = False
        self.log_path: Optional[str] = None

    def start(self, log_path: Optional[str] = None):
        if self._running:
            return
        self._running = True
        self.log_path = log_path
        GLib.timeout_add(50, self._heartbeat)
        threading.Thread(target=self._watch, daemon=True, name="kestrel-stall-watchdog").start()

    def _heartbeat(self):
        self._beat = time.monotonic()
        return True

    def _sample(self) -> List[Tuple[str, int, str]]:
        frame = sys._current_frames().get(self._main_tid)
        out = []
        while frame is not None and len(out) < 40:
            out.append((os.path.basename(frame.f_code.co_filename), frame.f_lineno, frame.f_code.co_name))
            frame = frame.f_back
        return out

    def _watch(self):
        stall_start = None
        samples: dict = {}
        dumped = False
        while True:
            time.sleep(0.02)
            age = time.monotonic() - self._beat
            if age > self.THRESHOLD:
                if stall_start is None:
                    stall_start = self._beat
                    samples = {}
                    dumped = False
                try:
                    st = tuple(self._sample())
                except Exception:
                    st = ()
                samples[st] = samples.get(st, 0) + 1
                if age > self.FREEZE and not dumped:
                    dumped = True
                    self.dump_freeze(age, samples)
            elif stall_start is not None:
                dur = self._beat - stall_start
                self._record(dur, samples)
                stall_start = None
                samples = {}

    def dump_freeze(self, age: float, samples: dict):
        """The interface has been dead for FREEZE seconds. Write every thread's stack (faulthandler, which
        works even if the GIL is contended) plus the sampled main-thread stack to a file, and to stderr, so a
        lock-up can be diagnosed after the fact — or while it's still frozen, from another terminal."""
        import faulthandler
        try:
            d = os.path.join(GLib.get_user_cache_dir(), "kestrel")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"freeze-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt")
            with open(path, "w") as f:
                f.write(f"Kestrel interface frozen for {age:.1f}s at {_dt.datetime.now():%H:%M:%S}\n")
                f.write(f"threads: {[t.name for t in threading.enumerate()]}\n\n")
                if samples:
                    stack, n = max(samples.items(), key=lambda kv: kv[1])
                    f.write(f"main thread (most common of {sum(samples.values())} samples):\n")
                    for fr in stack:
                        f.write(f"    {fr[2]}  ({fr[0]}:{fr[1]})\n")
                    f.write("\n")
                f.write("faulthandler, all threads:\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
            sys.stderr.write(f"\nkestrel: INTERFACE FROZEN for {age:.0f}s — stacks written to {path}\n")
            sys.stderr.flush()
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception:
            pass

    def _record(self, dur: float, samples: dict):
        if not samples:
            return
        stack, n = max(samples.items(), key=lambda kv: kv[1])
        # innermost frame that belongs to Kestrel itself (its file is kestrel.py or one of the parts)
        mine = [f for f in stack if f[0].startswith(("kestrel", "p1", "p2", "p3", "p4", "p5", "explorer"))]
        where = mine[0] if mine else (stack[0] if stack else ("?", 0, "?"))
        rec = {"t": time.time(), "dur": dur, "where": f"{where[2]}  ({where[0]}:{where[1]})",
               "stack": [f"{f[2]}  ({f[0]}:{f[1]})" for f in stack], "share": n / max(1, sum(samples.values()))}
        self.records.append(rec)
        self.total_stall += dur
        del self.records[:-self.MAX_RECORDS]
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(f"{_dt.datetime.fromtimestamp(rec['t']).strftime('%H:%M:%S')}  {dur * 1000:6.0f} ms  {rec['where']}\n")
                    for line in rec["stack"][:14]:
                        f.write(f"        {line}\n")
            except OSError:
                pass

    def report(self) -> str:
        up = time.time() - self.started
        lines = [f"Session: {up / 60:.1f} min · {len(self.records)} UI stalls over {self.THRESHOLD * 1000:.0f} ms · {self.total_stall:.1f} s frozen in total"
                 f" ({self.total_stall / max(up, 1) * 100:.1f} % of the session)", ""]
        if not self.records:
            lines.append("No stalls recorded: the interface thread has not been blocked for more than 120 ms.")
            return "\n".join(lines)
        agg: dict = {}
        for r in self.records:
            a = agg.setdefault(r["where"], [0, 0.0, 0.0])
            a[0] += 1
            a[1] += r["dur"]
            a[2] = max(a[2], r["dur"])
        lines.append("Where the interface thread was stuck (by total time):")
        for where, (n, tot, mx) in sorted(agg.items(), key=lambda kv: -kv[1][1])[:15]:
            lines.append(f"  {tot * 1000:7.0f} ms total  {n:4d}×  max {mx * 1000:5.0f} ms   {where}")
        lines.append("")
        lines.append("Most recent stalls (newest first), with the sampled stack:")
        for r in reversed(self.records[-12:]):
            lines.append(f"  {_dt.datetime.fromtimestamp(r['t']).strftime('%H:%M:%S')}  {r['dur'] * 1000:6.0f} ms  {r['where']}   [{r['share'] * 100:.0f}% of samples]")
            for line in r["stack"][:10]:
                lines.append(f"        {line}")
        return "\n".join(lines)


WATCHDOG = StallWatchdog()
NAV_LOG: List[dict] = []    # recent navigations: click → listed → shown timings (Performance report)


def nav_report(limit: int = 20) -> str:
    """Timeline of the last navigations: how long the folder read took in the worker thread (click→listed)
    and how long the main thread took to display it (listed→shown), plus how many threads were alive and
    how many UI stalls happened meanwhile. Separates 'the disk/share is slow' from 'the interface is busy'."""
    if not NAV_LOG:
        return "No navigations recorded yet."
    lines = [f"{'folder':<48} {'items':>7} {'read':>8} {'display':>8} {'threads':>7}  stalls  phase"]
    for r in NAV_LOG[-limit:]:
        read = (r["listed"] - r["t0"]) * 1000 if r["listed"] else None
        disp = (r["shown"] - r["listed"]) * 1000 if (r["shown"] and r["listed"]) else None
        p = r["path"] if len(r["path"]) <= 48 else "…" + r["path"][-47:]
        lines.append(f"{p:<48} {r['n']:>7,} {(f'{read:7.0f}ms' if read is not None else '   (pending)'):>8} "
                     f"{(f'{disp:7.0f}ms' if disp is not None else '        -'):>8} {r['threads']:>7}  {str(r.get('stalls_during', '-')):<6}  {r.get('tag', '')}"
                     + (f"  ERROR {r['err']}" if r.get("err") else ""))
    return "\n".join(lines)
STRESS = None             # (folder, cycles) when started with --stress
PROFILE = None            # cProfile.Profile when started with --profile


def profiling_report(top: int = 40) -> str:
    """cProfile summary for --profile runs: top functions by own time and by cumulative time."""
    if PROFILE is None:
        return "Start Kestrel with --profile to collect a cProfile of the interface thread."
    import pstats, io
    PROFILE.disable()
    try:
        out = io.StringIO()
        st = pstats.Stats(PROFILE, stream=out)
        st.sort_stats("tottime").print_stats(top)
        out.write("\n\n==== by cumulative time ====\n")
        st.sort_stats("cumulative").print_stats(top)
        return out.getvalue()
    finally:
        PROFILE.enable()


# --------------------------------------------------------------------------- self-test / benchmark
def run_selftest(bench: bool = False) -> int:
    """`kestrel.py --selftest [--bench]`: unit tests for the non-GUI machinery (listing, sorting, decoders,
    mesh pipeline, rasteriser, archives, rename, helpers) with timings, so a slowdown can be pinned to a
    component without the GUI. --bench scales the data up (50k files, 5M-face mesh) and prints a table."""
    import unittest, tempfile, zipfile, struct as _st

    timings: List[Tuple[str, float]] = []

    def timed(name):
        def deco(fn):
            def wrapper(self):
                t0 = time.perf_counter()
                try:
                    fn(self)
                finally:
                    timings.append((name, time.perf_counter() - t0))
            wrapper.__name__ = fn.__name__
            return wrapper
        return deco

    class T(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.tmp = tempfile.mkdtemp(prefix="kestrel-selftest-")
            cls.nfiles = 50000 if bench else 3000
            d = os.path.join(cls.tmp, "many")
            os.makedirs(d)
            for i in range(cls.nfiles):
                with open(os.path.join(d, f"file_{i:05d}.{['txt', 'png', 'exr', 'py'][i % 4]}"), "w") as f:
                    f.write("x")
            for i in range(50):
                os.makedirs(os.path.join(d, f"dir_{i:02d}"))
            cls.many = d

        @timed("list_folder")
        def test_list_folder(self):
            ents = list_folder(self.many, False)
            self.assertEqual(len(ents), self.nfiles + 50)
            t0 = time.perf_counter()
            list_folder(self.many, False)
            dt = time.perf_counter() - t0
            self.assertLess(dt, 0.00004 * len(ents) + 0.5, f"listing {len(ents)} entries took {dt:.2f}s")

        @timed("sort_entries ×4")
        def test_sort(self):
            ents = list_folder(self.many, False)
            for key in ("name", "size", "mtime", "type"):
                out = sort_entries(ents, key, False, True)
                self.assertEqual(len(out), len(ents))
                self.assertTrue(all(e.is_dir for e in out[:50]))

        @timed("fmt helpers")
        def test_fmt(self):
            self.assertEqual(fmt_size(0), "0 B")
            self.assertIn("MB", fmt_size(5 << 20))
            self.assertEqual(MainWindow.archive_stem("a.tar.gz"), "a")
            self.assertEqual(MainWindow.archive_stem("b.7z"), "b")

        @timed("rv sequence")
        def test_rv_sequence(self):
            d = os.path.join(self.tmp, "seq")
            os.makedirs(d, exist_ok=True)
            for i in (1001, 1002, 1003):
                open(os.path.join(d, f"shot.{i}.exr"), "w").close()
            self.assertEqual(rv_sequence_for(os.path.join(d, "shot.1002.exr")), os.path.join(d, "shot.#.exr"))
            self.assertIsNone(rv_sequence_for(os.path.join(d, "nope.exr")))

        @timed("zip round trip (ArchiveOp)")
        def test_zip_roundtrip(self):
            src = os.path.join(self.tmp, "ziptest")
            os.makedirs(os.path.join(src, "sub"), exist_ok=True)
            with open(os.path.join(src, "sub", "a.txt"), "w") as f:
                f.write("hello" * 1000)
            out = os.path.join(self.tmp, "t.zip")
            done = []
            op = ArchiveOp("zip", [src], out, "zip", lambda o: done.append(o))
            op.run()
            self.assertFalse(op.errors)
            self.assertTrue(zipfile.is_zipfile(out))
            dest = os.path.join(self.tmp, "unz")
            op2 = ArchiveOp("extract", [out], dest, "", lambda o: None)
            op2.run()
            self.assertFalse(op2.errors)
            self.assertTrue(os.path.exists(os.path.join(dest, "ziptest", "sub", "a.txt")))
            self.assertIn("entries", describe_archive(out))

        @timed("PSD decode (RLE, sampled)")
        def test_psd(self):
            w, h = (4000, 3000) if bench else (400, 300)
            rows = [bytes(((x * 7 + y * 3) & 255) for x in range(w)) for y in range(h)]
            def packbits(line):
                out = bytearray(); i = 0; n = len(line)
                while i < n:
                    k = i
                    while k + 1 < n and k - i < 126:
                        k += 1
                    out += bytes([k - i]) + line[i:k + 1]
                    i = k + 1
                return bytes(out)
            enc = [packbits(r) for r in rows]
            body = b"8BPS" + _st.pack(">H", 1) + b"\0" * 6 + _st.pack(">HIIHH", 3, h, w, 8, 3)
            body += _st.pack(">I", 0) + _st.pack(">I", 0) + _st.pack(">I", 0) + _st.pack(">H", 1)
            body += b"".join(_st.pack(">H", len(e)) for e in enc) * 3 + b"".join(enc) * 3
            p = os.path.join(self.tmp, "t.psd")
            with open(p, "wb") as f:
                f.write(body)
            img = decode_psd(p, 300, 300)
            self.assertIsNotNone(img)
            self.assertLessEqual(img.w, 300)
            self.assertEqual(img.info[0], ("Dimensions", f"{w} × {h}"))

        @timed("mesh pipeline (clustering)")
        def test_mesh_pipeline(self):
            if np is None:
                self.skipTest("numpy not installed")
            g = 1600 if bench else 300
            th, ph = np.meshgrid(np.linspace(0.01, np.pi - 0.01, g), np.linspace(0, 2 * np.pi, g))
            V = np.column_stack((np.sin(th.ravel()) * np.cos(ph.ravel()), np.sin(th.ravel()) * np.sin(ph.ravel()), np.cos(th.ravel())))
            i = np.arange(g * g).reshape(g, g)
            a, b, c, d = i[:-1, :-1].ravel(), i[:-1, 1:].ravel(), i[1:, 1:].ravel(), i[1:, :-1].ravel()
            F = np.concatenate((np.column_stack((a, b, c)), np.column_stack((a, c, d))))
            r = _finish_model_numpy(V, F, 500000, len(F), [])
            self.assertLessEqual(len(r["faces"]), 500000)
            self.assertEqual(len(r["vnormals"]), len(r["verts"]))
            self.__class__.mesh = r

        @timed("software render frame")
        def test_render(self):
            if np is None:
                self.skipTest("numpy not installed")
            r = getattr(self.__class__, "mesh", None) or self.test_mesh_pipeline() or self.__class__.mesh
            t0 = time.perf_counter()
            res = software_render(np.asarray(r["verts"]), np.asarray(r["faces"]), np.asarray(r["normals"]), np.asarray(r["vnormals"]),
                                  (480, 400, 0.6, 0.35, 1.0, 0, 0, True, False, (34, 34, 34), (0.8, 0.8, 0.8)))
            dt = time.perf_counter() - t0
            self.assertIsNotNone(res)
            self.assertEqual(len(res[2]), 480 * 400 * 4)
            self.assertLess(dt, 3.0, f"frame took {dt:.2f}s")

        @timed("render worker round trip")
        def test_render_worker(self):
            if np is None:
                self.skipTest("numpy not installed")
            r = getattr(self.__class__, "mesh", None) or self.test_mesh_pipeline() or self.__class__.mesh
            w = RenderWorker()
            w.set_model(np.asarray(r["verts"]), np.asarray(r["faces"]), np.asarray(r["normals"]), np.asarray(r["vnormals"]))
            got = {}
            ev = threading.Event()
            saved = GLib.idle_add
            GLib.idle_add = lambda f, *a: f(*a)
            try:
                w.render((320, 240, 0.6, 0.35, 1.0, 0, 0, True, False, (34, 34, 34), (0.8, 0.8, 0.8)), lambda p, res: (got.update(r=res), ev.set()))
                self.assertTrue(ev.wait(30), "worker did not answer")
            finally:
                GLib.idle_add = saved
                w.close()
            self.assertIsNotNone(got.get("r"))

        @timed("batch rename plan")
        def test_batch_rename(self):
            d = os.path.join(self.tmp, "ren")
            os.makedirs(d, exist_ok=True)
            for n in ("b.txt", "a.txt"):
                open(os.path.join(d, n), "w").close()
            done, errors = apply_renames([(os.path.join(d, "a.txt"), os.path.join(d, "b.txt")), (os.path.join(d, "b.txt"), os.path.join(d, "a.txt"))])   # swap
            self.assertFalse(errors)
            self.assertEqual(len(done), 2)
            self.assertTrue(os.path.exists(os.path.join(d, "a.txt")) and os.path.exists(os.path.join(d, "b.txt")))

        @timed("dir_stats (cached)")
        def test_dir_stats(self):
            r = dir_stats_cached(self.many, threading.Event())
            self.assertGreaterEqual(r[0] + r[1], self.nfiles)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\nTimings:")
    for name, dt in timings:
        print(f"  {dt * 1000:8.0f} ms  {name}")
    return 0 if res.wasSuccessful() else 1


# --------------------------------------------------------------------------- preview benchmark: large files of every type
def _synth_png(path: str, w: int, h: int):
    """RGB PNG with a gradient+noise pattern (stdlib zlib only)."""
    import zlib
    rows = []
    for y in range(h):
        base = bytes(((x * 3 + y) & 255) for x in range(w))
        rows.append(b"\0" + bytes(b for x in range(w) for b in (base[x], (base[x] * 2) & 255, (y + x // 3) & 255)))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def _synth_psd(path: str, w: int, h: int):
    """RGB 8-bit RLE PSD (3 channels) with a sparse pattern that compresses well — like a real layout file."""
    def packbits(line: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(line)
        while i < n:
            j = i
            while j + 1 < n and line[j + 1] == line[j] and j - i < 126:
                j += 1
            if j > i:
                out += bytes([257 - (j - i + 1), line[i]])
                i = j + 1
            else:
                k = i
                while k + 1 < n and line[k + 1] != line[k] and k - i < 126:
                    k += 1
                out += bytes([k - i]) + line[i:k + 1]
                i = k + 1
        return bytes(out)
    enc = []
    for c in range(3):
        for y in range(h):
            line = bytes((((x // 64) * 40 + (y // 64) * 30 + c * 70) & 255) for x in range(w))
            enc.append(packbits(line))
    hdr = b"8BPS" + struct.pack(">H", 1) + b"\0" * 6 + struct.pack(">HIIHH", 3, h, w, 8, 3)
    with open(path, "wb") as f:
        f.write(hdr + struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">H", 1))
        f.write(b"".join(struct.pack(">H", len(e)) for e in enc))
        f.write(b"".join(enc))


def _synth_exr(path: str, w: int, h: int):
    """Scanline OpenEXR, HALF RGB, ZIP compression (16 lines/chunk) — the common render-output layout."""
    import zlib

    def attr(name, typ, body):
        return name.encode() + b"\0" + typ.encode() + b"\0" + struct.pack("<i", len(body)) + body
    chl = b"".join(n.encode() + b"\0" + struct.pack("<i", 1) + b"\0\0\0\0" + struct.pack("<ii", 1, 1) for n in ("B", "G", "R")) + b"\0"
    header = (attr("channels", "chlist", chl) + attr("compression", "compression", b"\x03") +
              attr("dataWindow", "box2i", struct.pack("<iiii", 0, 0, w - 1, h - 1)) + attr("displayWindow", "box2i", struct.pack("<iiii", 0, 0, w - 1, h - 1)) +
              attr("lineOrder", "lineOrder", b"\0") + attr("pixelAspectRatio", "float", struct.pack("<f", 1.0)) +
              attr("screenWindowCenter", "v2f", struct.pack("<ff", 0, 0)) + attr("screenWindowWidth", "float", struct.pack("<f", 1.0)) + b"\0")

    def half(v: float) -> int:        # float → IEEE half bits (positive, normal range)
        if v <= 0:
            return 0
        import math as _m
        m, e = _m.frexp(v)                       # v = m * 2**e, 0.5 <= m < 1
        e = e - 1 + 15
        if e <= 0:
            return 0
        if e >= 31:
            return 0x7BFF
        return (e << 10) | (int((m * 2 - 1) * 1024) & 0x3FF)
    nblocks = (h + 15) // 16
    chunks = []
    for b in range(nblocks):
        y0 = b * 16
        nl = min(16, h - y0)
        lines = bytearray()
        for k in range(nl):
            y = y0 + k
            for cidx in range(3):                      # B, G, R alphabetical order
                row = bytearray()
                for x in range(w):
                    v = ((x * (cidx + 1) + y) % 256) / 255.0 * 4.0       # HDR-ish range 0..4
                    hb = half(v)
                    row += bytes((hb & 0xFF, hb >> 8))
                lines += row
        # predictor (inverse of the decoder's un-delta → interleave): de-interleave first, then delta-encode
        n = len(lines)
        half_n = (n + 1) // 2
        split = bytearray(n)
        split[:half_n] = lines[0::2]
        split[half_n:] = lines[1::2]
        delta = bytearray(n)
        prev = 0
        for i_ in range(n):
            delta[i_] = (split[i_] - prev + 128 + 256) & 255
            prev = split[i_]
        comp = zlib.compress(bytes(delta))
        if len(comp) >= n:
            comp = bytes(lines)
        chunks.append((y0, comp))
    table_pos = 8 + len(header)
    data_pos = table_pos + 8 * nblocks
    offsets, body = [], bytearray()
    for y0, comp in chunks:
        offsets.append(data_pos + len(body))
        body += struct.pack("<ii", y0, len(comp)) + comp
    with open(path, "wb") as f:
        f.write(b"\x76\x2f\x31\x01" + struct.pack("<I", 2) + header + struct.pack("<%dQ" % nblocks, *offsets) + body)


def _synth_hdr(path: str, w: int, h: int):
    """Radiance RGBE, flat (uncompressed) scanlines."""
    with open(path, "wb") as f:
        f.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n" + f"-Y {h} +X {w}\n".encode())
        for y in range(h):
            f.write(bytes(b for x in range(w) for b in ((x * 2) & 255, (y * 2) & 255, ((x + y)) & 255, 128)))


def _synth_iff(path: str, w: int, h: int):
    """Maya IFF, 8-bit RGB, uncompressed, one tile per 64 rows."""
    tiles = []
    for ty in range(0, h, 64):
        th = min(64, h - ty)
        pix = bytearray()
        for y in range(th):
            for x in range(w):
                pix += bytes(((x + ty + y) & 255, (x * 2) & 255, (ty + y) & 255))       # B, G, R
        body = struct.pack(">HHHH", 0, ty, w - 1, ty + th - 1) + bytes(pix)
        tiles.append(b"RGBA" + struct.pack(">I", len(body)) + body + (b"\0" * ((4 - len(body) % 4) % 4)))
    tbhd = struct.pack(">IIHHIHHI", w, h, 0, 0, 1, 0, len(tiles), 0)
    inner = b"CIMG" + b"TBHD" + struct.pack(">I", len(tbhd)) + tbhd
    tbmp = b"".join(tiles)
    for4_tbmp = b"FOR4" + struct.pack(">I", 4 + len(tbmp)) + b"TBMP" + tbmp
    content = inner + for4_tbmp
    with open(path, "wb") as f:
        f.write(b"FOR4" + struct.pack(">I", len(content)) + content)


def _synth_obj(path: str, nfaces: int):
    g = int(math.sqrt(nfaces / 2)) + 1
    with open(path, "w") as f:
        for y in range(g):
            for x in range(g):
                f.write(f"v {x / g:.5f} {y / g:.5f} {math.sin(x / 7.0) * math.cos(y / 5.0) * 0.2:.5f}\n")
        for y in range(g - 1):
            for x in range(g - 1):
                a = y * g + x + 1
                f.write(f"f {a} {a + 1} {a + g + 1}\nf {a} {a + g + 1} {a + g}\n")


def _synth_stl(path: str, nfaces: int):
    g = int(math.sqrt(nfaces / 2)) + 1
    rec = struct.Struct("<12fH")
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", 2 * (g - 1) * (g - 1)))
        for y in range(g - 1):
            for x in range(g - 1):
                p = lambda i, j: (i / g, j / g, math.sin(i / 7.0) * 0.2)
                a, b, c, d = p(x, y), p(x + 1, y), p(x + 1, y + 1), p(x, y + 1)
                f.write(rec.pack(0, 0, 1, *a, *b, *c, 0))
                f.write(rec.pack(0, 0, 1, *a, *c, *d, 0))


def _synth_text(path: str, mb: int):
    line = "def function_%d(argument):\n    # comment with some words in it\n    return {'key': argument * %d, 'other': [1, 2, 3]}\n\n"
    with open(path, "w") as f:
        n = 0
        while f.tell() < mb << 20:
            f.write(line % (n, n))
            n += 1


def _synth_zip(path: str, nfiles: int):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(nfiles):
            z.writestr(f"folder_{i % 20}/file_{i:05d}.txt", ("data %d\n" % i) * 50)


def _synth_pdf(path: str, pages: int):
    objs = []
    kids = []
    content = b"BT /F1 24 Tf 72 720 Td (Kestrel preview benchmark) Tj ET " + b"".join(
        b"BT /F1 10 Tf 72 %d Td (line of text number %d for the renderer to lay out) Tj ET " % (700 - 12 * k, k) for k in range(50))
    for p in range(pages):
        kids.append(len(objs) + 4)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>" % (len(objs) + 5))
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    head = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % pages,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    allobjs = head + objs
    out = bytearray(b"%PDF-1.4\n")
    xref = []
    for i, o in enumerate(allobjs, 1):
        xref.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xpos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(allobjs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in xref)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(allobjs) + 1, xpos)
    with open(path, "wb") as f:
        f.write(out)


def run_preview_bench(big: bool = True) -> int:
    """`kestrel.py --preview-bench`: write a large synthetic file of every previewable type into a temp
    folder, run the exact code path the preview pane uses (generate_preview in the worker process, load_model
    + a software frame for meshes), and log a timing table. Types needing an external tool (video, 7z, PDF,
    Alembic) are generated only when that tool is present and reported as 'skipped' otherwise. Results are
    appended to ~/.cache/kestrel/preview-bench.log so runs can be compared over time."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="kestrel-preview-bench-")
    S = 1 if big else 4          # size divisor for --quick
    specs = [
        ("PNG 4096²", "big.png", lambda p: _synth_png(p, 4096 // S, 4096 // S), "image/png"),
        ("PSD 6000×4000 RLE", "big.psd", lambda p: _synth_psd(p, 6000 // S, 4000 // S), "image/vnd.adobe.photoshop"),
        ("EXR 2048² half ZIP", "big.exr", lambda p: _synth_exr(p, 2048 // S, 2048 // S), "image/x-exr"),
        ("HDR 2048²", "big.hdr", lambda p: _synth_hdr(p, 2048 // S, 2048 // S), "image/vnd.radiance"),
        ("Maya IFF 2048²", "big.iff", lambda p: _synth_iff(p, 2048 // S, 2048 // S), "application/octet-stream"),
        ("OBJ 1M faces", "big.obj", lambda p: _synth_obj(p, 1_000_000 // S), "model/obj"),
        ("STL 2M faces", "big.stl", lambda p: _synth_stl(p, 2_000_000 // S), "model/stl"),
        ("text 8 MB (.py)", "big.py", lambda p: _synth_text(p, 8 // S or 1), "text/x-python"),
        ("ZIP 5000 files", "big.zip", lambda p: _synth_zip(p, 5000 // S), "application/zip"),
        ("PDF 200 pages", "big.pdf", lambda p: _synth_pdf(p, 200 // S), "application/pdf"),
    ]
    tools = []
    if shutil.which("ffmpeg"):
        specs.append(("MP4 1080p 20 s", "big.mp4", lambda p: subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30", "-t", str(20 // S or 2), "-pix_fmt", "yuv420p", p], check=True, timeout=300), "video/mp4"))
    else:
        tools.append("ffmpeg (video)")
    if sevenzip():
        def mk7z(p):
            src = os.path.join(tmp, "7zsrc")
            os.makedirs(src, exist_ok=True)
            for i in range(2000 // S):
                with open(os.path.join(src, f"f{i:04d}.txt"), "w") as f:
                    f.write("payload %d\n" % i * 40)
            subprocess.run([sevenzip(), "a", "-bso0", "-bse0", p, src], check=True, timeout=300)
        specs.append(("7z 2000 files", "big.7z", mk7z, "application/x-7z-compressed"))
    else:
        tools.append("7z")
    if np is not None:
        try:
            import h5py  # type: ignore

            def mkabc(p):
                g = 1000 // S
                xs, ys = np.meshgrid(np.linspace(0, 1, g), np.linspace(0, 1, g))
                P = np.column_stack((xs.ravel(), ys.ravel(), np.sin(xs.ravel() * 6) * 0.2)).astype(np.float32).ravel()
                i = np.arange(g * g).reshape(g, g)
                a, b, c, d = i[:-1, :-1].ravel(), i[:-1, 1:].ravel(), i[1:, 1:].ravel(), i[1:, :-1].ravel()
                idx = np.column_stack((a, b, c, d)).ravel().astype(np.int32)
                with h5py.File(p, "w") as h5:
                    grp = h5.create_group("ABC/bench/benchShape/.prop/.geom")
                    grp.create_dataset("P.smp0", data=P)
                    grp.create_dataset(".faceCounts.smp0", data=np.full(len(a), 4, dtype=np.int32))
                    grp.create_dataset(".faceIndices.smp0", data=idx)
            specs.append(("Alembic HDF5 2M faces", "big.abc", mkabc, "application/octet-stream"))
        except ImportError:
            tools.append("h5py (Alembic)")
    print(f"Kestrel preview benchmark — files in {tmp}")
    if tools:
        print("skipped (tool not installed): " + ", ".join(tools))
    print(f"{'type':<24} {'size':>9} {'generate':>9} {'preview':>9}  result")
    results = []
    for label, fname, gen, ct in specs:
        path = os.path.join(tmp, fname)
        t0 = time.perf_counter()
        try:
            gen(path)
        except Exception as ex:
            print(f"{label:<24} {'-':>9} {'-':>9} {'-':>9}  generator failed: {ex}")
            continue
        tg = time.perf_counter() - t0
        size = os.path.getsize(path)
        ext = os.path.splitext(fname)[1]
        t0 = time.perf_counter()
        try:
            if ext in (".obj", ".stl", ".abc"):
                m = run_heavy(load_model, path, 500000)
                if m and "error" not in m:
                    tl = time.perf_counter() - t0
                    t1 = time.perf_counter()
                    fr = software_render(np.asarray(m["verts"]), np.asarray(m["faces"]), np.asarray(m["normals"]), np.asarray(m["vnormals"]),
                                         (480, 400, 0.6, 0.35, 1.0, 0, 0, True, False, (34, 34, 34), (0.8, 0.8, 0.8))) if np is not None else None
                    tf = time.perf_counter() - t1
                    res = f"{m['info'][1][1]}; load {tl * 1000:.0f} ms + frame {tf * 1000:.0f} ms"
                else:
                    res = "FAILED: " + str((m or {}).get("error", "no result"))[:120]
            elif ext in (".py",):
                with open(path, "rb") as f:
                    data = f.read(64 << 10)
                res = f"text preview reads first 64 KB ({len(data)} bytes) — editor loads lazily"
            else:
                out = generate_preview(path, ct, 480, 400)
                if out.get("raw") is not None:
                    res = f"decoded {out['raw'].w}×{out['raw'].h}" + (f" via built-in" if "tool" not in out else "")
                elif out.get("png"):
                    res = f"converted via {out.get('tool', 'tool')}"
                elif out.get("text"):
                    res = f"text listing, {len(out['text'])} chars"
                else:
                    res = "no preview: " + "; ".join(f"{k}={v}" for k, v in out.get("info", [])[:2])
        except Exception as ex:
            res = f"EXCEPTION {ex.__class__.__name__}: {ex}"
        tp = time.perf_counter() - t0
        results.append((label, size, tg, tp, res))
        flag = "  <-- slow" if tp > 2.0 else ""
        print(f"{label:<24} {fmt_size(size):>9} {tg * 1000:7.0f}ms {tp * 1000:7.0f}ms  {res}{flag}")
    try:
        log = os.path.join(GLib.get_user_cache_dir(), "kestrel", "preview-bench.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a") as f:
            f.write(f"\n== {_dt.datetime.now():%Y-%m-%d %H:%M:%S}  host={os.uname().nodename}  python={sys.version.split()[0]}  numpy={'yes' if np is not None else 'no'}\n")
            for label, size, tg, tp, res in results:
                f.write(f"{label:<24} {fmt_size(size):>9} gen {tg * 1000:7.0f}ms  preview {tp * 1000:7.0f}ms  {res}\n")
        print(f"\nlogged to {log}")
    except OSError:
        pass
    if "--keep" not in sys.argv:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"files kept in {tmp} (open it in Kestrel and click through them to compare with the pane)")
    return 0


# --------------------------------------------------------------------------- GUI stress test
class StressDriver:
    """`kestrel.py --stress [folder] [--cycles N]`: drives the real window from inside the main loop — the
    way a user would, but faster and tirelessly — while the stall watchdog records every freeze and the
    freeze dumper catches lock-ups. Each step is one GLib timeout; between steps the loop runs normally so
    worker results, monitors and redraws interleave exactly as in real use.

    The script, per cycle:  open the folder → switch through every layout → select every file in turn with
    the preview + quick view on (each type's full preview path, including models and video) → rapid-fire
    selection (10 ms apart, previews cancelling each other) → sort by each key → search, clear search →
    split, select in the second pane, unsplit → new tab, back, forward, up, close tab → select all / none →
    open and close the Options dialog → compress the folder to a zip and extract it back → trash + restore
    a scratch file → nav pane expand/collapse.  Dialogs that need a human (conflict, save prompt) are not
    expected: everything acts on files the driver created.

    At the end it prints the watchdog report and exits non-zero if the interface froze for more than
    FREEZE seconds at any point or any step raised."""

    def __init__(self, win, folder: Optional[str], cycles: int, mode: str = "stress"):
        self.win = win
        self.mode = mode                  # "stress" (everything) or "nav" (navigation under preview load)
        self.cycles = cycles
        self.cycle = 0
        self.errors: List[str] = []
        self.steps_done = 0
        self.t0 = time.time()
        self.folder = folder
        self.scratch: Optional[str] = None       # where zip/extract/trash steps write (never the user's folder)
        self.step_log: List[Tuple[str, float]] = []
        self._queue: List[Tuple[str, Callable, int]] = []

    # ---- plumbing
    def start(self):
        if not self.folder and self.mode == "nav":
            print("nav-bench: give it a folder:  kestrel.py --nav-bench /path/with/subfolders", file=sys.stderr)
            self.win.app.quit()
            return
        if not self.folder:
            self.folder = self._make_folder()
            self.scratch = self.folder
        else:
            import tempfile
            self.scratch = tempfile.mkdtemp(prefix="kestrel-stress-scratch-")
            sub = os.path.join(self.scratch, "subfolder")
            os.makedirs(sub)
            for i in range(200):
                with open(os.path.join(sub, f"item_{i:03d}.txt"), "w") as f:
                    f.write("x" * i)
        win = self.win
        win.cfg["preview_pane"] = True
        win.cfg["details_pane"] = True
        win._apply_panes()
        print(f"stress: {self.cycles} cycle(s) over {self.folder}", flush=True)
        self._plan_cycle()
        GLib.timeout_add(500, self._next)

    def _make_folder(self) -> str:
        import tempfile
        d = tempfile.mkdtemp(prefix="kestrel-stress-")
        print("stress: generating test files…", flush=True)
        _synth_png(os.path.join(d, "image.png"), 1024, 768)
        _synth_psd(os.path.join(d, "layout.psd"), 1500, 1000)
        _synth_exr(os.path.join(d, "render.exr"), 512, 512)
        _synth_hdr(os.path.join(d, "env.hdr"), 512, 256)
        _synth_iff(os.path.join(d, "maya.iff"), 512, 512)
        _synth_obj(os.path.join(d, "mesh.obj"), 200000)
        _synth_stl(os.path.join(d, "part.stl"), 200000)
        _synth_text(os.path.join(d, "script.py"), 1)
        _synth_zip(os.path.join(d, "bundle.zip"), 300)
        _synth_pdf(os.path.join(d, "doc.pdf"), 5)
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("plain text\n" * 2000)
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "3", "-pix_fmt", "yuv420p", os.path.join(d, "clip.mp4")], timeout=120)
        sub = os.path.join(d, "subfolder")
        os.makedirs(sub)
        for i in range(400):
            with open(os.path.join(sub, f"item_{i:03d}.txt"), "w") as f:
                f.write("x" * i)
        return d

    def step(self, name: str, fn: Callable, delay_ms: int = 250):
        self._queue.append((name, fn, delay_ms))

    def _next(self):
        if not self._queue:
            self.cycle += 1
            if self.cycle >= self.cycles:
                return self._finish()
            self._plan_cycle()
        name, fn, delay = self._queue.pop(0)
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as ex:
            self.errors.append(f"{name}: {ex.__class__.__name__}: {ex}")
            print(f"stress: step {name!r} raised {ex!r}", flush=True)
        dt = time.perf_counter() - t0
        self.step_log.append((name, dt))
        self.steps_done += 1
        if dt > 0.25:
            print(f"stress: {name:<40} {dt * 1000:6.0f} ms  (synchronous part)", flush=True)
        GLib.timeout_add(delay, self._next)
        return False

    def _finish(self):
        rep = WATCHDOG.report()
        slow = sorted(self.step_log, key=lambda x: -x[1])[:10]
        if self.mode == "nav":
            print("\n==== navigation timeline (click → read → shown) ====")
            print(nav_report(40))
            base = [r for r in NAV_LOG if r.get("listed") and r.get("tag") == "baseline"]
            under = [r for r in NAV_LOG if r.get("listed") and r.get("tag") == "preview"]
            for name, rows in (("baseline (nothing selected)", base), ("while a folder preview loads", under)):
                if rows:
                    reads = sorted((r["listed"] - r["t0"]) * 1000 for r in rows)
                    print(f"  {name:<34} n={len(rows):<3} read median {reads[len(reads) // 2]:6.0f} ms   max {reads[-1]:6.0f} ms")
        print("\n==== stress test finished ====")
        print(f"{self.steps_done} steps in {time.time() - self.t0:.0f}s, {len(self.errors)} step errors")
        for e in self.errors[:20]:
            print("  ERROR", e)
        print("\nslowest synchronous steps:")
        for name, dt in slow:
            print(f"  {dt * 1000:6.0f} ms  {name}")
        print("\n" + rep, flush=True)
        froze = any(r["dur"] > StallWatchdog.FREEZE for r in WATCHDOG.records)
        code = 2 if froze else (1 if self.errors else 0)
        print(f"\nresult: {'LOCK-UP DETECTED' if froze else ('step errors' if self.errors else 'OK')} (exit {code})", flush=True)
        self.win.app.quit()
        os._exit(code) if froze else None
        return False

    # ---- the script
    def _plan_nav_cycle(self):
        """--nav-bench: reproduce 'navigating while a folder preview is loading'. For every subfolder of the
        target: select it (starts the preview listing + size walk), wait 400 ms so those are in flight, then
        navigate into it, wait for it to be shown, select its first subfolder, go back. Also one pass with
        no selection at all, for a baseline. The navigation table at the end has the answer."""
        w = self.win
        d = self.folder
        S = self.step
        subs = sorted(f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f)))[:25]
        S("navigate root", lambda: w.navigate(d), 1500)
        S("select none", lambda: w.tab.select_none(), 300)
        for sd in subs:
            S(f"[baseline] enter {sd}", lambda sd=sd: (w.navigate(os.path.join(d, sd)), NAV_LOG[-1].__setitem__("tag", "baseline")), 1500)
            S("back", lambda: w.go_back(), 1000)
        for sd in subs:
            S(f"select {sd} (preview starts)", lambda sd=sd: w.tab.select_names([sd]), 400)
            S(f"[under preview] enter {sd}", lambda sd=sd: (w.navigate(os.path.join(d, sd)), NAV_LOG[-1].__setitem__("tag", "preview")), 1500)
            S("select first child", lambda: w.tab.select_names([e.name for e in w.tab.entries if e.is_dir][:1]), 400)
            S("back (under preview)", lambda: w.go_back(), 1200)
        # double-click simulation: select then navigate 120 ms later (inside the debounce window)
        for sd in subs[:10]:
            S(f"dbl-click select {sd}", lambda sd=sd: w.tab.select_names([sd]), 120)
            S(f"[dbl-click] enter {sd}", lambda sd=sd: (w.navigate(os.path.join(d, sd)), NAV_LOG[-1].__setitem__("tag", "dblclick")), 1500)
            S("back", lambda: w.go_back(), 1000)

    def _plan_cycle(self):
        if self.mode == "nav":
            return self._plan_nav_cycle()
        w = self.win
        d = self.folder
        files = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
        S = self.step
        S("navigate folder", lambda: w.navigate(d), 600)
        dirs = sorted(f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f)))[:3]
        for sd in dirs:                       # real folders: walk into the first few subfolders too
            S(f"enter {sd}", lambda sd=sd: w.navigate(os.path.join(d, sd)), 700)
            S("preview first items", lambda: w.tab.select_names([e.name for e in w.tab.entries[:1]]), 600)
            S("back", lambda: w.go_back(), 400)
        for lay in ("list", "small", "medium", "large", "xlarge", "tiles", "content"):
            S(f"layout {lay}", lambda lay=lay: w.set_layout(lay), 200)
        S("layout details", lambda: w.set_layout("details"), 300)
        for f in files:
            S(f"preview {f}", lambda f=f: w.tab.select_names([f]), 900 if f.endswith((".obj", ".stl", ".abc", ".mp4", ".exr")) else 400)
        S("stop video", lambda: w.video.stop() if getattr(w, "video", None) else None, 100)
        for _ in range(3):
            for f in files:
                S(f"rapid {f}", lambda f=f: w.tab.select_names([f]), 10)
        S("select none", lambda: w.tab.select_none(), 300)
        for key in ("name", "size", "type", "mtime"):
            S(f"sort {key}", lambda key=key: w.sort_by(key), 150)
        S("sort name", lambda: w.sort_by("name"), 150)
        S("search", lambda: w.tab.start_search("item"), 800)
        S("stop search", lambda: w.tab.stop_search(), 300)
        S("split", lambda: w.toggle_split() if len(w.page.panes) == 1 else None, 500)
        sub = os.path.join(d, "subfolder") if os.path.isdir(os.path.join(d, "subfolder")) else os.path.join(self.scratch, "subfolder")
        S("navigate pane 2", lambda: w.navigate(sub), 500)
        S("select in pane", lambda: w.tab.select_names(["item_010.txt", "item_011.txt"]), 300)
        S("unsplit", lambda: w.toggle_split() if len(w.page.panes) == 2 else None, 400)
        S("new tab", lambda: w.new_tab(sub), 500)
        S("back", lambda: w.go_back(), 300)
        S("forward", lambda: w.go_forward(), 300)
        S("up", lambda: w.go_up(), 300)
        S("close tab", lambda: w.close_tab(), 300)
        S("select all", lambda: w.tab.select_all(), 300)
        S("select none", lambda: w.tab.select_none(), 200)
        S("options open/close", self._options_roundtrip, 400)
        S("zip folder", self._zip, 2500)
        S("extract zip", self._extract, 2500)
        S("trash scratch", self._trash, 1200)
        S("restore scratch", self._restore, 1200)
        S("nav expand", lambda: w.nav_tree.expand_row(w.nav_store.get_path(w.nav_quick), False), 300)
        S("nav collapse", lambda: w.nav_tree.collapse_row(w.nav_store.get_path(w.nav_quick)), 300)
        S("navigate home", lambda: w.navigate(HOME), 500)
        S("navigate This PC", lambda: w.navigate(THISPC), 500)
        S("navigate Quick access", lambda: w.navigate(QUICK), 500)
        S("navigate Recent", lambda: w.navigate(RECENT), 500)

    def _options_roundtrip(self):
        w = self.win
        dlg = OptionsDialog(w, w.cfg, lambda: None, 0)
        GLib.timeout_add(300, lambda: (dlg.response(Gtk.ResponseType.CLOSE), False)[1])
        dlg.run()
        dlg.destroy()

    def _zip(self):
        w = self.win
        w.navigate(self.scratch)
        w.tab.select_names(["subfolder"])
        out = os.path.join(self.scratch, "stress-roundtrip.zip")
        if os.path.exists(out):
            os.remove(out)
        op = ArchiveOp("zip", [os.path.join(self.scratch, "subfolder")], out, "zip", w._op_done)
        w._start_op(op, "stress: compressing")

    def _extract(self):
        w = self.win
        src = os.path.join(self.scratch, "stress-roundtrip.zip")
        dest = os.path.join(self.scratch, "stress-extracted")
        shutil.rmtree(dest, ignore_errors=True)
        if os.path.exists(src):
            op = ArchiveOp("extract", [src], dest, "", w._op_done)
            w._start_op(op, "stress: extracting")

    def _trash(self):
        w = self.win
        p = os.path.join(self.scratch, "scratch-trash-me.txt")
        with open(p, "w") as f:
            f.write("bye")
        w.navigate(self.scratch)
        w.tab.select_names(["scratch-trash-me.txt"])
        w.cfg["confirm_delete"] = False
        w.delete()

    def _restore(self):
        w = self.win
        w.navigate(TRASH)
        GLib.timeout_add(500, lambda: (w.tab.select_names(["scratch-trash-me.txt"]), w.restore(), False)[2])


# =========================================================================== encrypted file index
# An optional, opt-in map of the user's folders (name, kind, size, mtime per entry, keyed by directory) that
# makes two things instant: painting a folder the moment you navigate to it (the real listing replaces the
# indexed one a moment later, like the progressive listing does today), and name search across whole trees
# without walking the disk. Because such a map is a complete inventory of the machine, it never touches the
# disk in the clear: the index file is AES-256-CBC (via the system `openssl`, PBKDF2-stretched key) with an
# HMAC-SHA256 over the ciphertext (encrypt-then-MAC), and the key lives in the desktop keyring through
# libsecret's `secret-tool` when available, otherwise in a 0600 file in ~/.config/kestrel. Plaintext exists
# only in this process's memory.
INDEX_FILE = os.path.join(GLib.get_user_cache_dir(), "kestrel", "index.enc")
INDEX_KEYFILE = os.path.join(CONFIG_DIR, "index.key")
INDEX_EXCLUDE_DIRS = {".cache", ".git", ".svn", "node_modules", "__pycache__", ".Trash", ".local/share/Trash", "proc", "sys", "dev", "run",
                      ".thumbnails", ".mozilla", ".var", "snap", "lost+found", ".npm", ".cargo", ".rustup", ".conda", ".pyenv", "site-packages"}
INDEX_SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/var/cache", "/var/lib", "/snap")


class IndexKey:
    """32-byte key for the index. Keyring first (secret-tool / libsecret), file fallback. `source` tells the UI
    where it ended up so the user knows what the protection actually is."""

    def __init__(self):
        self.source = ""

    def get(self, create: bool = True) -> Optional[bytes]:
        st = shutil.which("secret-tool")
        if st:
            try:
                r = subprocess.run([st, "lookup", "application", "kestrel", "purpose", "index-key"], capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and len(r.stdout.strip()) == 64:
                    self.source = "desktop keyring"
                    return bytes.fromhex(r.stdout.strip())
                if create:
                    key = os.urandom(32)
                    r = subprocess.run([st, "store", "--label=Kestrel file index key", "application", "kestrel", "purpose", "index-key"],
                                       input=key.hex(), capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        self.source = "desktop keyring"
                        return key
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if os.path.exists(INDEX_KEYFILE):
                with open(INDEX_KEYFILE, "rb") as f:
                    data = f.read().strip()
                if len(data) == 64:
                    self.source = "key file (install libsecret-tools / a keyring daemon for keyring storage)"
                    return bytes.fromhex(data.decode())
            if create:
                key = os.urandom(32)
                os.makedirs(CONFIG_DIR, exist_ok=True)
                fd = os.open(INDEX_KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key.hex())
                self.source = "key file (install libsecret-tools / a keyring daemon for keyring storage)"
                return key
        except OSError:
            pass
        return None

    def forget(self):
        st = shutil.which("secret-tool")
        if st:
            subprocess.run([st, "clear", "application", "kestrel", "purpose", "index-key"], capture_output=True, timeout=15)
        try:
            os.remove(INDEX_KEYFILE)
        except OSError:
            pass


def _openssl(args: List[str], data: bytes, key: bytes) -> bytes:
    """Run openssl enc with the key passed over a pipe fd (never argv/env, which other users can read)."""
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, key.hex().encode())
        os.close(wfd)
        r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt", "-pass", f"fd:{rfd}"] + args,
                           input=data, capture_output=True, pass_fds=(rfd,), timeout=600)
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip().splitlines()[-1:] or "openssl failed")
    return r.stdout


def _mac(key: bytes, blob: bytes) -> bytes:
    import hmac, hashlib
    return hmac.new(hashlib.sha256(key + b"kestrel-index-mac").digest(), blob, hashlib.sha256).digest()


class FileIndex:
    MAGIC = b"KSTRLIDX1"

    def __init__(self):
        self.data: Dict[str, Tuple[float, list]] = {}      # dir → (dir mtime, [(name, is_dir, size, mtime), …])
        self.lock = threading.RLock()
        self.key = IndexKey()
        self._k: Optional[bytes] = None
        self.loaded = False
        self.dirty = False
        self.crawling = False
        self.cancel = threading.Event()
        self.progress = {"dirs": 0, "files": 0, "current": "", "started": 0.0, "finished": 0.0}
        self.last_error = ""
        self._flat: Optional[List[Tuple[str, str, int]]] = None    # (lower name, dir, idx) for search
        self.on_status: Optional[Callable] = None

    # ---- persistence
    def _key(self) -> Optional[bytes]:
        if self._k is None:
            self._k = self.key.get()
        return self._k

    def load(self) -> bool:
        if not os.path.exists(INDEX_FILE):
            self.loaded = True
            return False
        k = self._key()
        if not k:
            self.last_error = "no key available"
            return False
        try:
            with open(INDEX_FILE, "rb") as f:
                blob = f.read()
            if not blob.startswith(self.MAGIC):
                raise ValueError("not a Kestrel index")
            mac, body = blob[len(self.MAGIC):len(self.MAGIC) + 32], blob[len(self.MAGIC) + 32:]
            import hmac as _h
            if not _h.compare_digest(mac, _mac(k, body)):
                raise ValueError("index authentication failed (wrong key or tampered file) — it will be rebuilt")
            import zlib
            raw = zlib.decompress(_openssl(["-d"], body, k))
            obj = json.loads(raw.decode("utf-8"))
            with self.lock:
                self.data = {d: (m, [tuple(x) for x in ents]) for d, (m, ents) in obj["dirs"].items()}
                self._flat = None
            self.progress["finished"] = obj.get("finished", 0.0)
            self.loaded = True
            return True
        except Exception as ex:
            self.last_error = str(ex)
            self.loaded = True
            return False

    def save(self) -> bool:
        k = self._key()
        if not k:
            return False
        try:
            import zlib
            with self.lock:
                obj = {"version": 1, "finished": self.progress["finished"], "dirs": {d: [m, ents] for d, (m, ents) in self.data.items()}}
            raw = zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)
            body = _openssl([], raw, k)
            blob = self.MAGIC + _mac(k, body) + body
            os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
            tmp = INDEX_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, INDEX_FILE)
            self.dirty = False
            return True
        except Exception as ex:
            self.last_error = f"save: {ex}"
            return False

    def wipe(self):
        with self.lock:
            self.data = {}
            self._flat = None
        try:
            os.remove(INDEX_FILE)
        except OSError:
            pass
        self.key.forget()
        self._k = None

    # ---- queries
    def lookup(self, path: str) -> Optional[list]:
        with self.lock:
            hit = self.data.get(path.rstrip("/") or "/")
        return hit[1] if hit else None

    def update_dir(self, path: str, entries: List["Entry"]):
        """Called with a freshly listed folder: keep the index current for places the user actually visits."""
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            return
        with self.lock:
            self.data[path.rstrip("/") or "/"] = (mt, [(e.name, e.is_dir, e.size or 0, e.mtime or 0.0) for e in entries])
            self._flat = None
            self.dirty = True
        self._schedule_save()

    def _schedule_save(self, delay: float = 20.0):
        """Write the encrypted file a little after the last change (coalesces a burst of visits)."""
        t = getattr(self, "_save_timer", None)
        if t is not None:
            t.cancel()

        def go():
            self._save_timer = None
            if self.dirty and not self.crawling:
                pin_worker()
                self.save()
        self._save_timer = threading.Timer(delay, go)
        self._save_timer.daemon = True
        self._save_timer.start()

    def search(self, pattern: str, root: Optional[str] = None, limit: int = 5000) -> List[Tuple[str, bool, int, float]]:
        """Case-insensitive substring or glob match over every indexed name. ~1M names in well under a second."""
        pat = pattern.lower()
        globby = any(c in pat for c in "*?[")
        with self.lock:
            if self._flat is None:
                self._flat = [(ents, d) for d, (m, ents) in self.data.items()]
            flat = self._flat
        out = []
        rootp = (root.rstrip("/") + "/") if root and root != "/" else None
        for ents, d in flat:
            if rootp and not (d == root.rstrip("/") or d.startswith(rootp)):
                continue
            for name, is_dir, size, mtime in ents:
                low = name.lower()
                if (fnmatch.fnmatch(low, pat) if globby else pat in low):
                    out.append((os.path.join(d, name), is_dir, size, mtime))
                    if len(out) >= limit:
                        return out
        return out

    def stats(self) -> dict:
        with self.lock:
            dirs = len(self.data)
            files = sum(len(v[1]) for v in self.data.values())
        try:
            size = os.path.getsize(INDEX_FILE)
        except OSError:
            size = 0
        return {"dirs": dirs, "files": files, "size": size, "finished": self.progress["finished"], "key": self.key.source or "—",
                "crawling": self.crawling, "error": self.last_error}

    # ---- crawling
    def crawl(self, roots: List[str], include_network: bool, include_hidden: bool, full: bool = False):
        """Walk the roots in a low-priority thread. Directories whose mtime hasn't changed since they were
        indexed are skipped (full=False), so re-crawls touch only what moved. Saves every 60 s and at the end."""
        if self.crawling:
            return
        self.crawling = True
        self.cancel.clear()
        self.progress.update(dirs=0, files=0, current="", started=time.time())

        def work():
            pin_worker()
            try:
                os.nice(10)
            except OSError:
                pass
            last_save = time.time()
            seen_dev = {}
            for root in roots:
                root = os.path.abspath(os.path.expanduser(root))
                if not os.path.isdir(root):
                    continue
                stack = [root]
                while stack and not self.cancel.is_set():
                    d = stack.pop()
                    if d != root and d.startswith(INDEX_SYSTEM_PREFIXES) and not root.startswith(INDEX_SYSTEM_PREFIXES):
                        continue            # never wander into /proc, /sys, /run… unless that IS the chosen root
                    if not include_network and is_network_path(d):
                        continue
                    try:
                        st = os.stat(d)
                    except OSError:
                        continue
                    self.progress["current"] = d
                    with self.lock:
                        old = self.data.get(d)
                    if old is not None and not full and abs(old[0] - st.st_mtime) < 1e-6:
                        # unchanged directory: still descend (subfolders may have changed)
                        for name, is_dir, _, _ in old[1]:
                            if is_dir and (include_hidden or not name.startswith(".")) and name not in INDEX_EXCLUDE_DIRS:
                                stack.append(os.path.join(d, name))
                        self.progress["dirs"] += 1
                        continue
                    ents = []
                    try:
                        with os.scandir(d) as it:
                            for de in it:
                                if not include_hidden and de.name.startswith("."):
                                    continue
                                try:
                                    is_dir = de.is_dir(follow_symlinks=False)
                                    s = de.stat(follow_symlinks=False)
                                except OSError:
                                    continue
                                ents.append((de.name, is_dir, 0 if is_dir else s.st_size, s.st_mtime))
                                if is_dir and de.name not in INDEX_EXCLUDE_DIRS:
                                    stack.append(de.path)
                    except OSError:
                        continue
                    with self.lock:
                        self.data[d] = (st.st_mtime, ents)
                        self._flat = None
                    self.dirty = True
                    self.progress["dirs"] += 1
                    self.progress["files"] += len(ents)
                    if time.time() - last_save > 60:
                        self.save()
                        last_save = time.time()
                        if self.on_status:
                            GLib.idle_add(lambda: (self.on_status(), False)[1])
            self.progress["finished"] = time.time()
            self.progress["current"] = ""
            self.save()
            self.crawling = False
            if self.on_status:
                GLib.idle_add(lambda: (self.on_status(), False)[1])
        threading.Thread(target=work, daemon=True, name="kestrel-indexer").start()

    def stop(self):
        self.cancel.set()


INDEX = FileIndex()


def index_entries_for(path: str, rows: list) -> List["Entry"]:
    """Index tuples → Entry objects (icons/types from the extension cache) for the instant first paint."""
    out = []
    for name, is_dir, size, mtime in rows:
        e = Entry()
        e.name, e.path, e.is_dir = name, os.path.join(path, name), bool(is_dir)
        e.size = None if is_dir else int(size)
        e.mtime = float(mtime)
        e.hidden = name.startswith(".")
        if is_dir:
            e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
        else:
            e.content_type, e.type, e.icon = _ct_info(name)
        out.append(e)
    return out


# --------------------------------------------------------------------------- small dialog helpers
def message(parent, title: str, text: str, kind=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, secondary: str = "") -> int:
    if STRESS is not None:                 # stress run: a modal box would stall the script; log it instead
        print(f"stress: [message] {title}: {text}", file=sys.stderr, flush=True)
        return Gtk.ResponseType.OK
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind, buttons=buttons, text=title)
    if text:
        d.format_secondary_text(text)
    r = d.run()
    d.destroy()
    return r


def ask_yes_no(parent, title: str, text: str, default_no: bool = False) -> bool:
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                          buttons=Gtk.ButtonsType.YES_NO, text=title)
    d.format_secondary_text(text)
    d.set_default_response(Gtk.ResponseType.NO if default_no else Gtk.ResponseType.YES)
    r = d.run()
    d.destroy()
    return r == Gtk.ResponseType.YES


def ask_text(parent, title: str, label: str, value: str = "", select_stem: bool = False) -> Optional[str]:
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_OK", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    box = d.get_content_area()
    box.set_spacing(6)
    box.set_border_width(12)
    box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
    entry = Gtk.Entry(text=value, activates_default=True, width_chars=50)
    box.pack_start(entry, False, False, 0)
    d.show_all()
    if select_stem and value:
        stem = os.path.splitext(value)[0]
        entry.select_region(0, len(stem))
    r = d.run()
    txt = entry.get_text()
    d.destroy()
    return txt if r == Gtk.ResponseType.OK else None


def pick_folder(parent, title: str, start: str) -> Optional[str]:
    d = Gtk.FileChooserDialog(title=title, transient_for=parent, action=Gtk.FileChooserAction.SELECT_FOLDER)
    d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    if start and os.path.isdir(start):
        d.set_current_folder(start)
    d.set_create_folders(True)
    r = d.run()
    p = d.get_filename()
    d.destroy()
    return p if r == Gtk.ResponseType.OK else None


def conflict_dialog(parent, src: str, dst: str) -> Tuple[Optional[str], bool]:
    """Returns (replace|skip|keep|None, apply_to_all)."""
    both_dirs = os.path.isdir(src) and os.path.isdir(dst)
    what = "folder" if both_dirs else "file"
    d = Gtk.Dialog(title="Replace or Skip Files", transient_for=parent, modal=True)
    d.add_buttons("_Replace", 1, "_Skip", 2, "_Keep both", 3, "_Cancel", Gtk.ResponseType.CANCEL)
    d.set_default_response(1)
    box = d.get_content_area()
    box.set_border_width(12)
    box.set_spacing(8)
    lab = Gtk.Label(xalign=0, wrap=True)
    lab.set_markup(f"The destination already has a {what} named <b>{GLib.markup_escape_text(os.path.basename(dst))}</b>."
                   + ("\nMerging folders replaces files with the same name." if both_dirs else ""))
    box.pack_start(lab, False, False, 0)
    grid = Gtk.Grid(column_spacing=12, row_spacing=4)
    for i, (title, p) in enumerate((("Source", src), ("Destination", dst))):
        try:
            st = os.stat(p)
            info = f"{'folder' if os.path.isdir(p) else fmt_size(st.st_size)}  ·  {fmt_date(st.st_mtime, '%Y-%m-%d %H:%M')}"
        except OSError:
            info = "?"
        grid.attach(Gtk.Label(label=title + ":", xalign=0), 0, i, 1, 1)
        grid.attach(Gtk.Label(label=info, xalign=0), 1, i, 1, 1)
    box.pack_start(grid, False, False, 0)
    chk = Gtk.CheckButton(label="Do this for all conflicts")
    box.pack_start(chk, False, False, 0)
    d.show_all()
    r = d.run()
    all_ = chk.get_active()
    d.destroy()
    return {1: "replace", 2: "skip", 3: "keep"}.get(r), all_


class ProgressDialog(Gtk.Window):
    """Windows 11-style file-operation window: percent, bar, speed graph, items/time remaining, Pause, Cancel."""

    def __init__(self, parent, op, title: str):
        super().__init__(title=title, transient_for=parent, type_hint=Gdk.WindowTypeHint.DIALOG)
        self.op = op
        self.set_default_size(520, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.last_t, self.last_done = time.time(), 0
        self.rate = 0.0
        self.expanded = True
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        self.add(v)
        self.head = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.head.set_markup(f"<span size='large'>{GLib.markup_escape_text(title)}</span>")
        v.pack_start(self.head, False, False, 0)
        self.pct = Gtk.Label(xalign=0)
        self.pct.set_markup("<span size='x-large' weight='bold'>0%</span> complete")
        v.pack_start(self.pct, False, False, 0)
        self.bar = Gtk.ProgressBar()
        v.pack_start(self.bar, False, False, 0)
        self.graph = Gtk.DrawingArea()
        self.graph.set_size_request(-1, 70)
        self.graph.connect("draw", self._draw_graph)
        v.pack_start(self.graph, False, False, 0)
        self.details = Gtk.Grid(column_spacing=12, row_spacing=3)
        self.rows = {}
        for i, key in enumerate(("Name", "Speed", "Items remaining", "Time remaining")):
            k = Gtk.Label(label=key + ":", xalign=0)
            k.get_style_context().add_class("dim-label")
            val = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
            self.details.attach(k, 0, i, 1, 1)
            self.details.attach(val, 1, i, 1, 1)
            self.rows[key] = val
        v.pack_start(self.details, False, False, 0)
        hb = Gtk.Box(spacing=6)
        self.more = Gtk.Button(label="Fewer details", relief=Gtk.ReliefStyle.NONE)
        self.more.connect("clicked", self._toggle)
        hb.pack_start(self.more, False, False, 0)
        self.btn_cancel = Gtk.Button(label="Cancel")
        self.btn_cancel.connect("clicked", lambda b: op.cancel.cancel())
        hb.pack_end(self.btn_cancel, False, False, 0)
        self.btn_pause = Gtk.Button(label="Pause")
        self.btn_pause.connect("clicked", self._pause)
        hb.pack_end(self.btn_pause, False, False, 0)
        v.pack_start(hb, False, False, 0)
        self.connect("delete-event", lambda w, e: (op.cancel.cancel(), True)[1])
        self.timer = GLib.timeout_add(150, self.tick)
        self.show_all()

    def _toggle(self, b):
        self.expanded = not self.expanded
        self.details.set_visible(self.expanded)
        self.graph.set_visible(self.expanded)
        self.more.set_label("Fewer details" if self.expanded else "More details")
        self.resize(520, 1)

    def _pause(self, b):
        op = self.op
        if op.paused.is_set():
            op.paused.clear()
            self.btn_pause.set_label("Pause")
        else:
            op.paused.set()
            self.btn_pause.set_label("Resume")

    def _draw_graph(self, w, cr):
        a = w.get_allocation()
        sc = w.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.06)
        cr.rectangle(0, 0, a.width, a.height)
        cr.fill()
        samples = self.op.speed_samples[-120:]
        if len(samples) < 2:
            return False
        mx = max(samples) or 1.0
        step = a.width / max(1, len(samples) - 1)
        cr.set_source_rgba(0.0, 0.47, 0.84, 0.35)
        cr.move_to(0, a.height)
        for i, sp in enumerate(samples):
            cr.line_to(i * step, a.height - (sp / mx) * (a.height - 6))
        cr.line_to((len(samples) - 1) * step, a.height)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(0.0, 0.47, 0.84, 1.0)
        cr.set_line_width(1.5)
        for i, sp in enumerate(samples):
            (cr.move_to if i == 0 else cr.line_to)(i * step, a.height - (sp / mx) * (a.height - 6))
        cr.stroke()
        return False

    def tick(self):
        op = self.op
        if op.finished:
            return False
        now = time.time()
        dt = now - self.last_t
        if dt >= 0.5:
            inst = (op.done - self.last_done) / dt
            self.rate = inst if self.rate == 0 else self.rate * 0.6 + inst * 0.4
            op.speed_samples.append(max(0.0, inst))
            self.last_t, self.last_done = now, op.done
            self.graph.queue_draw()
        paused = op.paused.is_set()
        if op.total:
            frac = min(1.0, op.done / op.total)
            self.bar.set_fraction(frac)
            self.pct.set_markup(f"<span size='x-large' weight='bold'>{int(frac * 100)}%</span> complete" + ("  (paused)" if paused else ""))
            remain_b = op.total - op.done
            self.rows["Time remaining"].set_text(_fmt_eta(remain_b / self.rate) if self.rate > 1 and not paused else ("Paused" if paused else "Calculating…"))
        else:
            self.bar.pulse()
            self.pct.set_markup("<span size='x-large' weight='bold'>…</span> " + ("paused" if paused else "working"))
            self.rows["Time remaining"].set_text("Calculating…")
        self.rows["Name"].set_text(os.path.basename(op.current.rstrip("/")) or "Preparing…")
        self.rows["Speed"].set_text(f"{fmt_size(int(self.rate))}/s" if self.rate > 0 else "—")
        left_items = max(0, op.total_items - op.done_items)
        self.rows["Items remaining"].set_text(f"{left_items:,} ({fmt_size(max(0, op.total - op.done))})" if op.total_items else "—")
        return True


def _fmt_eta(secs: float) -> str:
    secs = int(secs)
    if secs < 5:
        return "A few seconds"
    if secs < 60:
        return f"About {secs} seconds"
    if secs < 3600:
        return f"About {secs // 60} minute{'s' if secs // 60 != 1 else ''}"
    return f"About {secs // 3600} h {(secs % 3600) // 60} min"


# --------------------------------------------------------------------------- Properties
class PropertiesDialog(Gtk.Dialog):
    def __init__(self, parent, e: Entry, cfg: dict, on_rename: Callable[[str, str], bool]):
        super().__init__(title=f"{e.name} Properties", transient_for=parent, modal=True)
        self.e, self.cfg, self.on_rename = e, cfg, on_rename
        self.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.APPLY, "_OK", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(480, -1)
        self.cancel = threading.Event()
        nb = Gtk.Notebook()
        self.get_content_area().pack_start(nb, True, True, 0)
        try:
            self.st = os.lstat(e.path)
        except OSError:
            self.st = None
        nb.append_page(self._general(), Gtk.Label(label="General"))
        nb.append_page(self._security(), Gtk.Label(label="Security"))
        nb.append_page(self._details(), Gtk.Label(label="Details"))
        self.connect("response", self._response)
        self.connect("destroy", lambda *_: self.cancel.set())
        self.show_all()

    def _grid(self):
        g = Gtk.Grid(column_spacing=12, row_spacing=6, border_width=12)
        return g

    def _row(self, g, r, key, widget):
        g.attach(Gtk.Label(label=key, xalign=1), 0, r, 1, 1)
        if isinstance(widget, str):
            widget = Gtk.Label(label=widget, xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        widget.set_hexpand(True)
        g.attach(widget, 1, r, 1, 1)
        return widget

    def _general(self):
        e, st, cfg = self.e, self.st, self.cfg
        g = self._grid()
        hb = Gtk.Box(spacing=8)
        img = Gtk.Image.new_from_gicon(e.icon or Gio.ThemedIcon.new("text-x-generic"), Gtk.IconSize.DIALOG)
        hb.pack_start(img, False, False, 0)
        self.name_entry = Gtk.Entry(text=e.name, hexpand=True)
        hb.pack_start(self.name_entry, True, True, 0)
        g.attach(hb, 0, 0, 2, 1)
        g.attach(Gtk.Separator(), 0, 1, 2, 1)
        r = 2
        self._row(g, r, "Type of file:", e.type); r += 1
        if not e.is_dir:
            app = Gio.AppInfo.get_default_for_type(e.content_type, False) if e.content_type else None
            self._row(g, r, "Opens with:", app.get_display_name() if app else "Unknown application"); r += 1
        self._row(g, r, "Location:", pretty_path(os.path.dirname(e.path))); r += 1
        self.size_lab = self._row(g, r, "Size:", fmt_size(e.size or 0) + f" ({(e.size or 0):,} bytes)"); r += 1
        blocks = (st.st_blocks * 512) if st else 0
        self._row(g, r, "Size on disk:", f"{fmt_size(blocks)} ({blocks:,} bytes)"); r += 1
        if e.is_dir:
            self.contains_lab = self._row(g, r, "Contains:", "calculating…"); r += 1
            threading.Thread(target=self._calc, daemon=True).start()
        g.attach(Gtk.Separator(), 0, r, 2, 1); r += 1
        f = "%A, %B %d, %Y, %H:%M:%S"
        self._row(g, r, "Created:", fmt_date(e.ctime, f) or fmt_date(st.st_ctime if st else 0, f) + "  (inode change)"); r += 1
        self._row(g, r, "Modified:", fmt_date(st.st_mtime if st else e.mtime, f)); r += 1
        self._row(g, r, "Accessed:", fmt_date(st.st_atime if st else e.atime, f)); r += 1
        g.attach(Gtk.Separator(), 0, r, 2, 1); r += 1
        mode = _stat.S_IMODE(st.st_mode) if st else 0
        self.ro = Gtk.CheckButton(label="Read-only", active=not (mode & 0o222))
        self.hidden = Gtk.CheckButton(label="Hidden (dot-file)", active=e.name.startswith("."))
        g.attach(Gtk.Label(label="Attributes:", xalign=1), 0, r, 1, 1)
        g.attach(self.ro, 1, r, 1, 1); r += 1
        g.attach(self.hidden, 1, r, 1, 1); r += 1
        return g

    def _calc(self):
        f, d, b = dir_stats_cached(self.e.path, self.cancel)
        GLib.idle_add(self._calc_done, f, d, b)

    def _calc_done(self, f, d, b):
        if not self.cancel.is_set():
            self.size_lab.set_text(f"{fmt_size(b)} ({b:,} bytes)")
            self.contains_lab.set_text(f"{f:,} Files, {d:,} Folders")

    def _security(self):
        st = self.st
        g = self._grid()
        owner = self.e.owner
        try:
            import grp
            group = grp.getgrgid(st.st_gid).gr_name if st else ""
        except Exception:
            group = str(st.st_gid) if st else ""
        self._row(g, 0, "Owner:", f"{owner} ({st.st_uid})" if st else owner)
        self._row(g, 1, "Group:", f"{group} ({st.st_gid})" if st else group)
        g.attach(Gtk.Separator(), 0, 2, 2, 1)
        pg = Gtk.Grid(column_spacing=18, row_spacing=4)
        for c, lab in enumerate(("", "Read", "Write", "Execute")):
            pg.attach(Gtk.Label(label=lab), c, 0, 1, 1)
        mode = _stat.S_IMODE(st.st_mode) if st else 0
        self.perm_checks = []
        for r, (who, shift) in enumerate((("Owner", 6), ("Group", 3), ("Others", 0)), start=1):
            pg.attach(Gtk.Label(label=who, xalign=0), 0, r, 1, 1)
            row = []
            for c, bit in enumerate((4, 2, 1), start=1):
                cb = Gtk.CheckButton(active=bool(mode & (bit << shift)))
                cb.set_halign(Gtk.Align.CENTER)
                pg.attach(cb, c, r, 1, 1)
                row.append(cb)
            self.perm_checks.append(row)
        g.attach(Gtk.Label(label="Permissions:", xalign=1, valign=Gtk.Align.START), 0, 3, 1, 1)
        g.attach(pg, 1, 3, 1, 1)
        self.mode_lab = self._row(g, 4, "Mode:", f"{_stat.filemode(st.st_mode) if st else ''}  ({oct(mode)})")
        note = Gtk.Label(label="Changing the owner requires root (use chown in a terminal).", xalign=0)
        note.get_style_context().add_class("dim-label")
        g.attach(note, 0, 5, 2, 1)
        return g

    def _details(self):
        e, st = self.e, self.st
        g = self._grid()
        rows = [("Full path:", e.path), ("MIME type:", e.content_type or "unknown"),
                ("Inode:", str(st.st_ino) if st else ""), ("Device:", str(st.st_dev) if st else ""),
                ("Hard links:", str(st.st_nlink) if st else ""),
                ("Symbolic link:", ("yes → " + os.readlink(e.path)) if e.is_link else "no"),
                ("Extension:", e.ext or "(none)")]
        for i, (k, v) in enumerate(rows):
            self._row(g, i, k, v)
        if not e.is_dir:
            lab = self._row(g, len(rows), "SHA-256:", "computing…")
            lab.set_selectable(True)

            def work():
                import hashlib
                h = hashlib.sha256()
                try:
                    with open(e.path, "rb") as f:
                        while not self.cancel.is_set():
                            buf = f.read(1 << 20)
                            if not buf:
                                break
                            h.update(buf)
                    digest = h.hexdigest() if not self.cancel.is_set() else ""
                except OSError:
                    digest = "unavailable"
                GLib.idle_add(lambda: (lab.set_text(digest), False)[1])
            threading.Thread(target=work, daemon=True).start()
        return g

    def _apply(self):
        e, st = self.e, self.st
        if st is None:
            return
        mode = _stat.S_IMODE(st.st_mode)
        new_mode = mode & 0o7000
        for row, shift in zip(self.perm_checks, (6, 3, 0)):
            for cb, bit in zip(row, (4, 2, 1)):
                if cb.get_active():
                    new_mode |= bit << shift
        if self.ro.get_active():
            new_mode &= ~0o222
        elif (new_mode & 0o222) == 0 and (mode & 0o222) == 0:
            new_mode |= 0o200
        if new_mode != mode:
            try:
                os.chmod(e.path, new_mode)
                self.st = os.lstat(e.path)
                self.mode_lab.set_text(f"{_stat.filemode(self.st.st_mode)}  ({oct(new_mode)})")
            except OSError as ex:
                message(self, "Properties", f"Couldn't change permissions: {ex}", Gtk.MessageType.ERROR)
        name = self.name_entry.get_text().strip()
        if self.hidden.get_active() and not name.startswith("."):
            name = "." + name
        elif not self.hidden.get_active() and name.startswith("."):
            name = name[1:]
        if name and name != e.name:
            if self.on_rename(e.path, os.path.join(os.path.dirname(e.path), name)):
                e.path = os.path.join(os.path.dirname(e.path), name)
                e.name = name
                self.name_entry.set_text(name)
                self.set_title(f"{name} Properties")

    def _response(self, d, r):
        if r in (Gtk.ResponseType.APPLY, Gtk.ResponseType.OK):
            self._apply()
        if r != Gtk.ResponseType.APPLY:
            self.cancel.set()
            self.destroy()


class MultiPropertiesDialog(Gtk.Dialog):
    def __init__(self, parent, sel: List[Entry]):
        super().__init__(title=f"{len(sel)} Items Properties", transient_for=parent, modal=True)
        self.add_button("_OK", Gtk.ResponseType.OK)
        self.cancel = threading.Event()
        g = Gtk.Grid(column_spacing=12, row_spacing=6, border_width=12)
        nf = sum(1 for e in sel if not e.is_dir)
        g.attach(Gtk.Label(label="Type:", xalign=1), 0, 0, 1, 1)
        g.attach(Gtk.Label(label=f"Multiple Types ({nf} Files, {len(sel) - nf} Folders)", xalign=0), 1, 0, 1, 1)
        g.attach(Gtk.Label(label="Location:", xalign=1), 0, 1, 1, 1)
        g.attach(Gtk.Label(label=f"All in {pretty_path(os.path.dirname(sel[0].path))}", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE), 1, 1, 1, 1)
        g.attach(Gtk.Label(label="Size:", xalign=1), 0, 2, 1, 1)
        self.size_lab = Gtk.Label(label="calculating…", xalign=0)
        g.attach(self.size_lab, 1, 2, 1, 1)
        g.attach(Gtk.Label(label="Contains:", xalign=1), 0, 3, 1, 1)
        self.cont_lab = Gtk.Label(label="…", xalign=0)
        g.attach(self.cont_lab, 1, 3, 1, 1)
        self.get_content_area().pack_start(g, True, True, 0)
        self.connect("response", lambda *_: (self.cancel.set(), self.destroy()))
        self.show_all()
        threading.Thread(target=self._calc, args=(sel,), daemon=True).start()

    def _calc(self, sel):
        f = d = b = 0
        for e in sel:
            if self.cancel.is_set():
                return
            if e.is_dir:
                ff, dd, bb = dir_stats(e.path, self.cancel)
                f, d, b = f + ff, d + dd + 1, b + bb
            else:
                f, b = f + 1, b + (e.size or 0)
        GLib.idle_add(lambda: (self.size_lab.set_text(f"{fmt_size(b)} ({b:,} bytes)"), self.cont_lab.set_text(f"{f:,} Files, {d:,} Folders")) and False)


# --------------------------------------------------------------------------- Folder Options
class OptionsDialog(Gtk.Dialog):
    def __init__(self, parent, cfg: dict, on_apply: Callable, page: int = 0):
        super().__init__(title="Preferences", transient_for=parent, modal=True)
        self.cfg, self.on_apply = cfg, on_apply
        self.add_buttons("Restore _Defaults", 10, "_Cancel", Gtk.ResponseType.CANCEL, "_OK", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        nb = Gtk.Notebook()
        self.get_content_area().pack_start(nb, True, True, 0)
        self.w: Dict[str, Gtk.Widget] = {}
        nb.append_page(self._general(), Gtk.Label(label="General"))
        nb.append_page(self._view(), Gtk.Label(label="View"))
        nb.append_page(self._search(), Gtk.Label(label="Search"))
        nb.append_page(self._dependencies(), Gtk.Label(label="Optional"))
        self.set_default_size(720, 560)
        self.show_all()
        nb.set_current_page(page)
        self.connect("response", self._response)

    @staticmethod
    def _vbox():
        return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=12)

    @staticmethod
    def _heading(box, text):
        l = Gtk.Label(xalign=0)
        l.set_markup(f"<b>{text}</b>")
        l.set_margin_top(8)
        box.pack_start(l, False, False, 0)

    def _radios(self, box, key, options):
        group = None
        for value, label in options:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = group or rb
            rb.set_active(self.cfg[key] == value)
            rb.value = value
            box.pack_start(rb, False, False, 0)
            self.w.setdefault("radio:" + key, []).append(rb)

    def _check(self, box, key, label):
        cb = Gtk.CheckButton(label=label, active=bool(self.cfg[key]))
        box.pack_start(cb, False, False, 0)
        self.w[key] = cb

    def _general(self):
        b = self._vbox()
        self._heading(b, "Open Kestrel to:")
        self._radios(b, "open_to", [("quick", "Quick access"), ("pc", "This PC"), ("home", "Home folder")])
        self._heading(b, "Browse folders")
        self._radios(b, "browse_in_new_tab", [(False, "Open each folder in the same tab"), (True, "Open each folder in its own tab")])
        self._heading(b, "Click items as follows")
        self._radios(b, "single_click", [(True, "Single-click to open an item (point to select)"), (False, "Double-click to open an item (single-click to select)")])
        self._heading(b, "Archives (zip, 7z, tar…)")
        self._radios(b, "archive_action", [("extract", "Double-click extracts to a folder beside the archive and opens it"),
                                           ("open", "Double-click opens the archive with the default application")])
        self._heading(b, "Performance")
        row = Gtk.Box(spacing=6)
        row.pack_start(Gtk.Label(label="CPU cores reserved for the interface:"), False, False, 0)
        self.cores_spin = Gtk.SpinButton.new_with_range(0, max(0, os.cpu_count() or 0), 1)
        self.cores_spin.set_value(int(self.cfg.get("ui_cores", 2)))
        self.cores_spin.set_tooltip_text("Preview decoding, 3D rendering and thumbnails run on the other cores so the window never stalls.\n0 disables pinning. Takes effect after a restart.")
        row.pack_start(self.cores_spin, False, False, 0)
        b.pack_start(row, False, False, 0)
        self._heading(b, "Privacy")
        self._check(b, "show_recent", "Show recently used files in Quick access")
        self._check(b, "show_frequent", "Show frequently used folders in Quick access")
        btn = Gtk.Button(label="Clear Kestrel history", halign=Gtk.Align.START)
        btn.connect("clicked", lambda *_: (self.cfg["recent"].clear(), self.cfg["frequent"].clear()))
        b.pack_start(btn, False, False, 4)
        return b

    def _view(self):
        b = self._vbox()
        self._heading(b, "Files and Folders")
        for key, lab in (("show_hidden", "Show hidden files, folders, and drives"),
                         ("hide_extensions", "Hide extensions for known file types"),
                         ("full_path_title", "Display the full path in the title bar"),
                         ("folders_first", "Show folders before files"),
                         ("confirm_delete", "Display delete confirmation dialog"),
                         ("checkboxes", "Use check boxes to select items"),
                         ("size_in_bytes", "Display file sizes in bytes")):
            self._check(b, key, lab)
        self._heading(b, "Panes")
        for key, lab in (("nav_pane", "Show navigation pane"), ("preview_pane", "Show preview pane"), ("details_pane", "Show details pane")):
            self._check(b, key, lab)
        self._heading(b, "Date format (strftime)")
        e = Gtk.Entry(text=self.cfg["date_format"])
        b.pack_start(e, False, False, 0)
        self.w["date_format"] = e
        return b

    def _search(self):
        b = self._vbox()
        self._heading(b, "How to search")
        self._check(b, "search_subfolders", "Include subfolders in search results")
        self._check(b, "search_contents", "Always search file names and contents (slower)")
        self._heading(b, "File index (faster navigation and search)")
        self._check(b, "index_enabled", "Keep an encrypted index of my folders")
        info = Gtk.Label(xalign=0, wrap=True)
        info.set_markup("<small>Folders you open paint instantly from the index while the real listing loads, and name searches "
                        "answer from it immediately. The index is AES-256 encrypted and authenticated; its key is kept in your desktop "
                        "keyring (or a private key file), never next to the index. Plaintext exists only in Kestrel's memory.</small>")
        b.pack_start(info, False, False, 0)
        b.pack_start(Gtk.Label(label="Folders to index:", xalign=0), False, False, 0)
        self.roots_store = Gtk.ListStore(str)
        for r in self.cfg.get("index_roots", [HOME]):
            self.roots_store.append([r])
        tv = Gtk.TreeView(model=self.roots_store, headers_visible=False)
        tv.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        sw = Gtk.ScrolledWindow(min_content_height=90)
        sw.add(tv)
        b.pack_start(sw, False, False, 0)
        row = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add folder…")
        rem = Gtk.Button(label="Remove")
        add.connect("clicked", lambda w: self._root_add())
        rem.connect("clicked", lambda w: self._root_remove(tv))
        row.pack_start(add, False, False, 0)
        row.pack_start(rem, False, False, 0)
        b.pack_start(row, False, False, 0)
        self._check(b, "index_network", "Include network shares (slow to crawl; off by default)")
        self._check(b, "index_hidden", "Include hidden files and folders")
        row2 = Gtk.Box(spacing=6)
        row2.pack_start(Gtk.Label(label="Re-check every"), False, False, 0)
        self.index_interval = Gtk.SpinButton.new_with_range(1, 168, 1)
        self.index_interval.set_value(int(self.cfg.get("index_interval_h", 6)))
        row2.pack_start(self.index_interval, False, False, 0)
        row2.pack_start(Gtk.Label(label="hours (only changed folders are re-read)"), False, False, 0)
        b.pack_start(row2, False, False, 0)
        row3 = Gtk.Box(spacing=6)
        upd = Gtk.Button(label="Update now")
        full = Gtk.Button(label="Rebuild from scratch")
        wipe = Gtk.Button(label="Delete index and key")
        wipe.get_style_context().add_class("destructive-action")
        upd.connect("clicked", lambda w: self._index_action("update"))
        full.connect("clicked", lambda w: self._index_action("full"))
        wipe.connect("clicked", lambda w: self._index_action("wipe"))
        for w_ in (upd, full):
            row3.pack_start(w_, False, False, 0)
        row3.pack_end(wipe, False, False, 0)
        b.pack_start(row3, False, False, 0)
        self.index_status = Gtk.Label(xalign=0, wrap=True)
        self.index_status.get_style_context().add_class("dim-label")
        b.pack_start(self.index_status, False, False, 0)
        self._index_status_refresh()
        GLib.timeout_add(1500, self._index_status_tick)
        return b

    def _index_status_tick(self):
        if not self.get_visible():
            return False
        self._index_status_refresh()
        return True

    def _index_status_refresh(self):
        parent = self.get_transient_for()
        try:
            self.index_status.set_text(parent.index_status_line() if parent is not None and hasattr(parent, "index_status_line") else "")
        except Exception:
            pass

    def _root_add(self):
        d = Gtk.FileChooserDialog(title="Folder to index", transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK and d.get_filename():
            p = d.get_filename()
            if p not in [r[0] for r in self.roots_store]:
                self.roots_store.append([p])
        d.destroy()

    def _root_remove(self, tv):
        model, it = tv.get_selection().get_selected()
        if it:
            model.remove(it)

    def _index_action(self, what: str):
        parent = self.get_transient_for()
        self._collect()                                  # use the roots/options as currently edited
        if what == "wipe":
            if ask_yes_no(self, "Delete the file index?", "The encrypted index and its key will be deleted. Navigation and search fall back to reading the disk.", default_no=True):
                INDEX.stop()
                INDEX.wipe()
                self._index_status_refresh()
            return
        if parent is not None and hasattr(parent, "index_now"):
            parent.cfg.update({k: self.cfg[k] for k in ("index_roots", "index_network", "index_hidden")})
            parent.index_now(full=(what == "full"))

    def _rv_detect(self):
        found = find_rv(self.rv_entry.get_text().strip())
        self.rv_status.set_text(f"Detected: {found}" if found else "Not found — install OpenRV (github.com/AcademySoftwareFoundation/OpenRV) or point this at its bin/rv")

    def _pick_rv(self, btn):
        d = Gtk.FileChooserDialog(title="Locate the rv executable", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK:
            self.rv_entry.set_text(d.get_filename() or "")
        d.destroy()

    def _dependencies(self):
        """The 'Optional' page: everything Kestrel can use but doesn't need — hardware acceleration, the
        OpenRV integration, the optional-tool checklist with install/copy actions, and the diagnostics report."""
        outer = self._vbox()
        self._heading(outer, "3D preview")
        self.gl_check = Gtk.CheckButton(label="Hardware-accelerated 3D preview (OpenGL) — models render white; software renderer is blue",
                                        active=bool(self.cfg.get("gl_preview", True)))
        self.gl_check.set_sensitive(HAVE_GL)
        if not HAVE_GL:
            self.gl_check.set_tooltip_text("PyOpenGL is not installed — tick it in the list below")
        outer.pack_start(self.gl_check, False, False, 0)
        self._heading(outer, "OpenRV")
        self._check(outer, "rv_enabled", "Enable OpenRV (right-click → Open in RV, Ctrl+Shift+R)")
        row = Gtk.Box(spacing=6)
        row.pack_start(Gtk.Label(label="RV executable:"), False, False, 0)
        self.rv_entry = Gtk.Entry(text=self.cfg.get("rv_path", ""), hexpand=True, placeholder_text="auto-detect (rv on PATH, $RV_HOME, /opt/rv*, ~/OpenRV*)")
        row.pack_start(self.rv_entry, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._pick_rv)
        row.pack_start(browse, False, False, 0)
        outer.pack_start(row, False, False, 0)
        self.rv_status = Gtk.Label(xalign=0, wrap=True)
        self.rv_status.get_style_context().add_class("dim-label")
        outer.pack_start(self.rv_status, False, False, 0)
        self.rv_entry.connect("changed", lambda e: self._rv_detect())
        self._rv_detect()
        self._heading(outer, "Optional tools")
        self.dep_status = Gtk.Label(label="Checking what's installed…", xalign=0)
        outer.pack_start(self.dep_status, False, False, 0)
        self.dep_grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True, min_content_height=220)
        sw.add(self.dep_grid)
        outer.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        self.dep_install = Gtk.Button(label="Install selected in terminal…", sensitive=False)
        self.dep_install.get_style_context().add_class("suggested-action")
        self.dep_install.connect("clicked", lambda b: self._dep_install())
        self.dep_copy = Gtk.Button(label="Copy commands", sensitive=False)
        self.dep_copy.connect("clicked", lambda b: self._dep_copy())
        refresh = Gtk.Button(label="Re-check")
        refresh.connect("clicked", lambda b: self._dep_refresh())
        row.pack_start(self.dep_install, False, False, 0)
        row.pack_start(self.dep_copy, False, False, 0)
        row.pack_end(refresh, False, False, 0)
        outer.pack_start(row, False, False, 0)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("<small>Commands run in a terminal window so you can see them and enter your sudo password; "
                        "EPEL is enabled automatically when needed. Restart Kestrel afterwards.</small>")
        outer.pack_start(note, False, False, 0)
        exp = Gtk.Expander(label="Diagnostics (renderer, importers, converters, interpreters)")
        self.diag_view = Gtk.TextView(editable=False, monospace=True)
        dsw = Gtk.ScrolledWindow(min_content_height=200)
        dsw.add(self.diag_view)
        exp.add(dsw)
        exp.connect("notify::expanded", lambda e, p: e.get_expanded() and self._diag_load())
        outer.pack_start(exp, False, False, 0)
        self.dep_checks: List[Tuple[Gtk.CheckButton, dict]] = []
        self._dep_refresh()
        return outer

    def _dep_refresh(self):
        for c in self.dep_grid.get_children():
            self.dep_grid.remove(c)
        self.dep_checks = []
        self.dep_status.set_text("Checking what's installed…")
        self.dep_install.set_sensitive(False)
        self.dep_copy.set_sensitive(False)

        def fill(deps):
            self.dep_status.set_text(f"{sum(1 for x in deps if x['found'])} of {len(deps)} optional tools found.")
            for i, dep in enumerate(deps):
                cb = Gtk.CheckButton(active=dep["found"] is None, sensitive=dep["found"] is None)
                self.dep_grid.attach(cb, 0, i, 1, 1)
                name = Gtk.Label(xalign=0)
                name.set_markup(f"<b>{GLib.markup_escape_text(dep['name'])}</b>")
                name.set_tooltip_text(dep["cmd"])
                self.dep_grid.attach(name, 1, i, 1, 1)
                st = Gtk.Label(xalign=0)
                st.set_markup("<span foreground='#2e8b57'>installed</span>" if dep["found"] else "<span foreground='#c0392b'>missing</span>")
                self.dep_grid.attach(st, 2, i, 1, 1)
                g = Gtk.Label(label=dep["gives"], xalign=0, wrap=True, hexpand=True)
                g.get_style_context().add_class("dim-label")
                self.dep_grid.attach(g, 3, i, 1, 1)
                self.dep_checks.append((cb, dep))
            self.dep_grid.show_all()
            self.dep_install.set_sensitive(True)
            self.dep_copy.set_sensitive(True)
            return False
        threading.Thread(target=lambda: GLib.idle_add(fill, optional_deps()), daemon=True).start()

    def _dep_chosen(self):
        return [dep for cb, dep in self.dep_checks if cb.get_active() and dep["found"] is None]

    def _dep_copy(self):
        set_clipboard_text("\n".join(dep["cmd"] for dep in self._dep_chosen()) or "# nothing selected")
        self.dep_status.set_text("Commands copied")

    def _dep_install(self):
        chosen = self._dep_chosen()
        if not chosen:
            self.dep_status.set_text("Nothing selected")
            return
        script = os.path.join(GLib.get_user_cache_dir(), "kestrel", "install-deps.sh")
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with open(script, "w") as f:
            f.write(install_script(chosen))
        os.chmod(script, 0o755)
        if spawn_terminal(HOME, ["bash", script]):
            self.dep_status.set_text("Installing in a terminal window")
        else:
            self.dep_status.set_text(f"No terminal found. Script: {script}")

    def _diag_load(self):
        self.diag_view.get_buffer().set_text("Checking…")
        threading.Thread(target=lambda: GLib.idle_add(lambda: self.diag_view.get_buffer().set_text(preview_tool_report()) or False), daemon=True).start()

    def _collect(self):
        cfg = self.cfg
        if hasattr(self, "gl_check"):
            cfg["gl_preview"] = self.gl_check.get_active()
        if hasattr(self, "rv_entry"):
            cfg["rv_path"] = self.rv_entry.get_text().strip()
        if hasattr(self, "cores_spin"):
            cfg["ui_cores"] = int(self.cores_spin.get_value())
        if hasattr(self, "roots_store"):
            cfg["index_roots"] = [r[0] for r in self.roots_store] or [HOME]
            cfg["index_interval_h"] = int(self.index_interval.get_value())
        for key, w in self.w.items():
            if key.startswith("radio:"):
                for rb in w:
                    if rb.get_active():
                        cfg[key[6:]] = rb.value
            elif isinstance(w, Gtk.CheckButton):
                cfg[key] = w.get_active()
            elif isinstance(w, Gtk.Entry):
                try:
                    _dt.datetime.now().strftime(w.get_text())
                    cfg[key] = w.get_text() or DEFAULT_CONFIG[key]
                except Exception:
                    pass

    def _response(self, d, r):
        if r == 10:
            for k, v in DEFAULT_CONFIG.items():
                if k not in ("pinned", "recent", "frequent", "window", "nav_width", "preview_width", "side_width", "side_split", "rv_path", "index_roots"):
                    self.cfg[k] = json.loads(json.dumps(v))
            self.on_apply()
            self.destroy()
            return
        if r == Gtk.ResponseType.OK:
            self._collect()
            self.on_apply()
        self.destroy()


SHORTCUTS = [
    ("Enter", "Open"), ("Backspace / Alt+Left / Mouse Back", "Back"), ("Alt+Right / Mouse Forward", "Forward"),
    ("Alt+Up", "Up one level"), ("F2", "Rename"), ("Delete", "Delete (Recycle Bin)"), ("Shift+Delete", "Delete permanently"),
    ("Ctrl+Shift+R", "Open in OpenRV"), ("Ctrl+C / X / V", "Copy / Cut / Paste"), ("Ctrl+Shift+C", "Copy as path"), ("Ctrl+A", "Select all"),
    ("Ctrl+click / Shift+click", "Multi-select"), ("Ctrl+Z", "Undo"), ("Ctrl+N", "New window"),
    ("Ctrl+F2 (or F2 with several selected)", "Batch rename"), ("Ctrl+Shift+N", "New folder"), ("Ctrl+T / Ctrl+W", "New tab / Close tab"), ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"),
    ("Ctrl+1..9", "Switch to tab"), ("Ctrl+L / F4 / Alt+D", "Address bar"), ("Ctrl+E / Ctrl+F / F3", "Search"),
    ("F5", "Refresh"), ("F10 / Alt+F", "Menu bar"), ("Shift+F10 / Menu / Right-click", "Context menu"),
    ("Alt+Enter", "Properties"), ("Ctrl+H", "Toggle hidden items"), ("Ctrl+1..", ""), ("Alt+P", "Preview pane"),
    ("Alt+Shift+P", "Details pane"), ("Ctrl+Shift+S", "Split view (two folders side by side)"), ("Drag a tab out / right-click tab", "Move tab to its own window"), ("F11", "Full screen"), ("Ctrl+Q", "Quit"), ("Middle-click folder", "Open in new tab"),
    ("Type letters", "Jump to item"),
]


def show_shortcuts(parent):
    d = Gtk.Dialog(title="Keyboard shortcuts", transient_for=parent, modal=True)
    d.add_button("_Close", Gtk.ResponseType.CLOSE)
    d.set_default_size(520, 520)
    store = Gtk.ListStore(str, str)
    for k, v in SHORTCUTS:
        if v:
            store.append([k, v])
    tv = Gtk.TreeView(model=store, headers_visible=True)
    for i, t in enumerate(("Key", "Action")):
        tv.append_column(Gtk.TreeViewColumn(t, Gtk.CellRendererText(), text=i))
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    sw.add(tv)
    d.get_content_area().pack_start(sw, True, True, 0)
    d.show_all()
    d.run()
    d.destroy()


def show_about(parent):
    d = Gtk.AboutDialog(transient_for=parent, modal=True, program_name=APP_FULL_NAME, version=VERSION,
                        comments="A fast, Windows-Explorer-style file manager for Linux.\nGTK 3 · PyGObject · GIO",
                        logo_icon_name="system-file-manager", website="https://github.com/")
    d.run()
    d.destroy()


# --------------------------------------------------------------------------- Batch rename (PowerRename-style)
class BatchRenameDialog(Gtk.Dialog):
    """Rename many files at once with a live preview.

    Operations are applied in this order to each file's *name* (the extension is handled separately so
    'IMG_001.jpg' keeps '.jpg'):  find/replace (plain or regex)  →  prefix / suffix  →  numbering pattern
    →  case change.  The preview updates on every keystroke; Apply performs the renames in two phases
    (via temporary names) so swaps and shifts like 1→2, 2→3 never collide."""

    CASE_OPTIONS = [("keep", "Keep case"), ("lower", "lowercase"), ("upper", "UPPERCASE"), ("title", "Title Case"), ("sentence", "Sentence case")]

    def __init__(self, parent, entries: List[Entry], on_done: Callable[[List[Tuple[str, str]]], None]):
        super().__init__(title="Batch Rename", transient_for=parent, modal=True)
        self.entries = [e for e in entries if e.path]
        self.on_done = on_done
        self.set_default_size(780, 560)
        self.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)

        # ---- controls
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        box.pack_start(grid, False, False, 0)
        r = 0
        grid.attach(Gtk.Label(label="Find:", xalign=1), 0, r, 1, 1)
        self.find = Gtk.Entry(hexpand=True, placeholder_text="text or regular expression")
        grid.attach(self.find, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="Replace with:", xalign=1), 2, r, 1, 1)
        self.replace = Gtk.Entry(hexpand=True, placeholder_text="use \\1 … for regex groups")
        grid.attach(self.replace, 3, r, 1, 1)
        r += 1
        opts = Gtk.Box(spacing=12)
        self.regex = Gtk.CheckButton(label="Regular expression")
        self.icase = Gtk.CheckButton(label="Ignore case")
        self.incl_ext = Gtk.CheckButton(label="Include extension")
        for w in (self.regex, self.icase, self.incl_ext):
            opts.pack_start(w, False, False, 0)
        grid.attach(opts, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Prefix:", xalign=1), 0, r, 1, 1)
        self.prefix = Gtk.Entry(hexpand=True)
        grid.attach(self.prefix, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="Suffix:", xalign=1), 2, r, 1, 1)
        self.suffix = Gtk.Entry(hexpand=True, placeholder_text="added before the extension")
        grid.attach(self.suffix, 3, r, 1, 1)
        r += 1
        grid.attach(Gtk.Label(label="Numbering:", xalign=1), 0, r, 1, 1)
        nb = Gtk.Box(spacing=6)
        self.pattern = Gtk.Entry(hexpand=True, placeholder_text="e.g.  Scene_{n}   or   {name}_{n}   (empty = off)")
        self.pattern.set_tooltip_text("{n} = counter, {name} = current name, {ext} = extension, {date} = file date, {i} = position")
        nb.pack_start(self.pattern, True, True, 0)
        nb.pack_start(Gtk.Label(label="start"), False, False, 0)
        self.start = Gtk.SpinButton.new_with_range(0, 999999, 1)
        self.start.set_value(1)
        nb.pack_start(self.start, False, False, 0)
        nb.pack_start(Gtk.Label(label="step"), False, False, 0)
        self.step = Gtk.SpinButton.new_with_range(1, 1000, 1)
        self.step.set_value(1)
        nb.pack_start(self.step, False, False, 0)
        nb.pack_start(Gtk.Label(label="digits"), False, False, 0)
        self.digits = Gtk.SpinButton.new_with_range(1, 10, 1)
        self.digits.set_value(max(2, len(str(len(self.entries)))))
        nb.pack_start(self.digits, False, False, 0)
        grid.attach(nb, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Case:", xalign=1), 0, r, 1, 1)
        cb = Gtk.Box(spacing=12)
        self.case = Gtk.ComboBoxText()
        for key, lab in self.CASE_OPTIONS:
            self.case.append(key, lab)
        self.case.set_active_id("keep")
        cb.pack_start(self.case, False, False, 0)
        self.order = Gtk.ComboBoxText()
        for key, lab in (("name", "Number by name"), ("modified", "Number by date modified"), ("size", "Number by size"), ("selection", "Number by selection order")):
            self.order.append(key, lab)
        self.order.set_active_id("name")
        cb.pack_start(self.order, False, False, 0)
        self.only_matching = Gtk.CheckButton(label="Only rename items that match Find")
        cb.pack_start(self.only_matching, False, False, 0)
        grid.attach(cb, 1, r, 3, 1)

        # ---- preview
        self.store = Gtk.ListStore(str, str, str)       # old, new, status
        tv = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Original", "New name", "")):
            rend = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
            col = Gtk.TreeViewColumn(title, rend, text=i)
            col.set_resizable(True)
            col.set_expand(i < 2)
            if i == 2:
                rend.set_property("foreground", "#d04040")
            tv.append_column(col)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.get_style_context().add_class("dim-label")
        box.pack_start(self.summary, False, False, 0)

        for w in (self.find, self.replace, self.prefix, self.suffix, self.pattern):
            w.connect("changed", lambda *_: self.refresh())
        for w in (self.regex, self.icase, self.incl_ext, self.only_matching):
            w.connect("toggled", lambda *_: self.refresh())
        for w in (self.start, self.step, self.digits):
            w.connect("value-changed", lambda *_: self.refresh())
        for w in (self.case, self.order):
            w.connect("changed", lambda *_: self.refresh())
        self.connect("response", self._response)
        self.plan: List[Tuple[str, str]] = []
        self.show_all()
        self.refresh()

    # ---- computing the new names
    def _ordered(self) -> List[Entry]:
        key = self.order.get_active_id()
        if key == "selection":
            return list(self.entries)
        if key == "modified":
            return sorted(self.entries, key=lambda e: (e.mtime, e.name.lower()))
        if key == "size":
            return sorted(self.entries, key=lambda e: (e.size or 0, e.name.lower()))
        return sorted(self.entries, key=lambda e: _natural_key(e.name))

    def _new_name(self, e: Entry, idx: int, counter: int) -> Optional[str]:
        """Apply the operations; None = leave this file alone."""
        import re
        if e.is_dir or not self.incl_ext.get_active():
            stem, ext = (e.name, "") if e.is_dir else os.path.splitext(e.name)
        else:
            stem, ext = e.name, ""
        find = self.find.get_text()
        matched = True
        if find:
            flags = re.IGNORECASE if self.icase.get_active() else 0
            try:
                pat = re.compile(find if self.regex.get_active() else re.escape(find), flags)
            except re.error:
                return None
            matched = bool(pat.search(stem))
            if matched:
                try:
                    stem = pat.sub(self.replace.get_text(), stem)
                except re.error:
                    return None
        if self.only_matching.get_active() and not matched:
            return None
        stem = self.prefix.get_text() + stem + self.suffix.get_text()
        pattern = self.pattern.get_text()
        if pattern:
            n = str(counter).zfill(int(self.digits.get_value()))
            date = fmt_date(e.mtime, "%Y-%m-%d") if e.mtime else ""
            stem = (pattern.replace("{n}", n).replace("{name}", stem).replace("{ext}", ext.lstrip("."))
                    .replace("{date}", date).replace("{i}", str(idx + 1)))
        case = self.case.get_active_id()
        if case == "lower":
            stem = stem.lower()
        elif case == "upper":
            stem = stem.upper()
        elif case == "title":
            stem = stem.title()
        elif case == "sentence":
            stem = stem[:1].upper() + stem[1:].lower()
        new = stem + ext
        return new if new and new != e.name else None

    def refresh(self):
        self.store.clear()
        self.plan = []
        counter = int(self.start.get_value())
        step = int(self.step.get_value())
        targets: Dict[str, str] = {}
        problems = 0
        for idx, e in enumerate(self._ordered()):
            new = self._new_name(e, idx, counter)
            status = ""
            if new is not None:
                counter += step
                dest = os.path.join(os.path.dirname(e.path), new)
                if "/" in new or new in (".", ".."):
                    status, new = "invalid name", None
                elif dest in targets:
                    status = "duplicate target"
                elif os.path.lexists(dest) and dest not in {x.path for x in self.entries}:
                    status = "already exists"
                if status:
                    problems += 1
                else:
                    targets[dest] = e.path
                    self.plan.append((e.path, dest))
            self.store.append([e.name, new or "", status or ("" if new else "—")])
        self.summary.set_text(f"{len(self.plan)} of {len(self.entries)} items will be renamed" + (f"   ·   {problems} problem{'s' if problems != 1 else ''} (shown in red)" if problems else ""))
        self.set_response_sensitive(Gtk.ResponseType.OK, bool(self.plan) and not problems)

    def _response(self, d, r):
        if r == Gtk.ResponseType.OK and self.plan:
            self.on_done(list(self.plan))
        self.destroy()


def _natural_key(s: str):
    """'file2' < 'file10' — sort numbers inside names numerically."""
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def apply_renames(plan: List[Tuple[str, str]]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Rename in two phases through temporary names so A→B, B→A style plans work.
    Returns (done [(old, new)], errors)."""
    import uuid
    done, errors, temps = [], [], []
    for src, dst in plan:
        tmp = os.path.join(os.path.dirname(src), f".kestrel-rename-{uuid.uuid4().hex}")
        try:
            os.rename(src, tmp)
            temps.append((tmp, src, dst))
        except OSError as ex:
            errors.append(f"{os.path.basename(src)}: {ex}")
    for tmp, src, dst in temps:
        try:
            if os.path.lexists(dst):
                raise OSError("target exists")
            os.rename(tmp, dst)
            done.append((src, dst))
        except OSError as ex:
            errors.append(f"{os.path.basename(src)}: {ex}")
            try:
                os.rename(tmp, src)      # put it back
            except OSError:
                errors.append(f"{os.path.basename(src)}: left as {os.path.basename(tmp)}")
    return done, errors


# --------------------------------------------------------------------------- row hover for tree views
def clip_width(widget: Gtk.Widget) -> Gtk.Widget:
    """Wrap a horizontal toolbar so it never dictates the pane's minimum width: a ScrolledWindow with an
    external horizontal policy reports a tiny minimum and simply clips what doesn't fit. Without this the
    preview pane grew whenever a wide toolbar (text editor, model bar, video controls) became visible."""
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
    sw.set_propagate_natural_height(True)
    sw.add(widget)
    return sw


class FitImage(Gtk.DrawingArea):
    """A picture that scales DOWN to whatever space it is given (never up), centred, with a minimum size of
    zero — unlike Gtk.Image, whose minimum is the pixbuf's full size and therefore stops a pane from being
    made smaller than the picture. Used for the preview image and the big fallback icon."""

    def __init__(self, max_size: int = 0):
        super().__init__()
        self.pixbuf = None
        self.max_size = max_size              # 0 = natural size cap is the pixbuf itself
        self.set_size_request(1, 1)
        self.connect("draw", self._draw)

    def set_from_pixbuf(self, pb):
        self.pixbuf = pb
        self.queue_draw()

    def _draw(self, w, cr):
        pb = self.pixbuf
        if pb is None:
            return False
        a = self.get_allocation()
        pw, ph = pb.get_width(), pb.get_height()
        cap = self.max_size or max(pw, ph)
        s = min(1.0, a.width / pw, a.height / ph, cap / max(pw, ph))
        dw, dh = pw * s, ph * s
        x, y = (a.width - dw) / 2, (a.height - dh) / 2
        cr.save()
        cr.translate(x, y)
        cr.scale(s, s)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.get_source().set_filter(cairo_filter_good())
        cr.paint()
        cr.restore()
        return False


def cairo_filter_good():
    try:
        import cairo
        return cairo.FILTER_GOOD
    except Exception:
        return 0


def enable_row_hover(tv: Gtk.TreeView):
    """Track the row under the pointer in tv.hover_path and repaint; cell data funcs read it via hover_bg()."""
    tv.hover_path = None
    tv.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

    def motion(w, event):
        hit = w.get_path_at_pos(int(event.x), int(event.y))
        p = hit[0] if hit else None
        old = w.hover_path
        if (p is None) != (old is None) or (p is not None and old is not None and p.compare(old) != 0):
            w.hover_path = p
            w.queue_draw()
        return False

    def leave(w, event):
        if w.hover_path is not None:
            w.hover_path = None
            w.queue_draw()
        return False
    tv.connect("motion-notify-event", motion)
    tv.connect("leave-notify-event", leave)


def hover_bg(tv: Gtk.TreeView, cell, path_or_iter_model, it):
    """Apply/clear the subtle hover background on a renderer for the row being drawn."""
    hp = getattr(tv, "hover_path", None)
    on = hp is not None and path_or_iter_model.get_path(it).compare(hp) == 0 and not tv.get_selection().iter_is_selected(it)
    if on:
        fg = tv.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        cell.set_property("cell-background-rgba", Gdk.RGBA(fg.red, fg.green, fg.blue, 0.07))
    cell.set_property("cell-background-set", on)


# --------------------------------------------------------------------------- soft grid lines for tree views
def enable_soft_grid(tv: Gtk.TreeView, columns: bool = True):
    """Draw our own separators after the rows: faint horizontal rules, and (optionally) vertical rules that
    fade out as they approach each horizontal rule so the crossings never form a visible '+'."""
    tv.set_grid_lines(Gtk.TreeViewGridLines.NONE)

    def draw(w, cr):
        model = w.get_model()
        if model is None:
            return False
        rng = w.get_visible_range()
        if not rng or rng[0] is None:
            return False
        start, end = rng
        cols = [c for c in w.get_columns() if c.get_visible()]
        if not cols:
            return False
        fg = w.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        alloc = w.get_allocation()
        rows = []
        it = model.get_iter(start)
        while it is not None:
            p = model.get_path(it)
            r = w.get_background_area(p, cols[0])
            _, y = w.convert_bin_window_to_widget_coords(0, r.y)
            v = model[it][0]
            if not (getattr(v, "kind", "") == "spacer"):
                rows.append((y, r.height))
            if p.compare(end) == 0:
                break
            it = model.iter_next(it)
            if it is None:
                break
        cr.save()
        cr.set_line_width(1)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.055)
        for y, h in rows:
            cr.move_to(0, y + h - 0.5)
            cr.line_to(alloc.width, y + h - 0.5)
        cr.stroke()
        if columns and rows:
            xs = []
            for c in cols[:-1]:
                r = w.get_background_area(start, c)
                x, _ = w.convert_bin_window_to_widget_coords(r.x + r.width, 0)
                xs.append(x - 0.5)
            fade = 7
            for x in xs:
                for y, h in rows:
                    y0, y1 = y, y + h - 1
                    if h <= 2 * fade + 2:
                        continue
                    pat = cairo_linear(x, y0, x, y1, fg, fade / h)
                    cr.set_source(pat)
                    cr.move_to(x, y0)
                    cr.line_to(x, y1)
                    cr.stroke()
        cr.restore()
        return False
    tv.connect_after("draw", draw)


def cairo_linear(x0, y0, x1, y1, fg, frac):
    import cairo
    pat = cairo.LinearGradient(x0, y0, x1, y1)
    pat.add_color_stop_rgba(0.0, fg.red, fg.green, fg.blue, 0.0)
    pat.add_color_stop_rgba(frac, fg.red, fg.green, fg.blue, 0.055)
    pat.add_color_stop_rgba(1.0 - frac, fg.red, fg.green, fg.blue, 0.055)
    pat.add_color_stop_rgba(1.0, fg.red, fg.green, fg.blue, 0.0)
    return pat


# --------------------------------------------------------------------------- virtualized icon grid
class IconGrid(Gtk.Layout):
    """A Nautilus-style icon view: only the tiles inside the viewport are laid out and painted, so a
    folder with 100k entries costs the same per frame as one with 100. Reads rows straight from the
    FolderView ListStore (col 0 Entry, col 2 pixbuf, col 3 markup). API mirrors the bits of
    Gtk.IconView that FolderView used: paths are Gtk.TreePath, signals item-activated / selection-changed."""

    __gsignals__ = {
        "item-activated": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, model: Gtk.ListStore):
        super().__init__()
        self.model: Optional[Gtk.ListStore] = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK |
                        Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.get_style_context().add_class("icongrid")
        # geometry
        self.icon_size = 48
        self.horizontal = False
        self.tile_w, self.tile_h = 96, 90
        self.margin = 8
        self.cols = 1
        self.text_lines = 2
        # state
        self.selected: set = set()
        self.cursor = -1
        self.anchor = -1
        self.prelight = -1
        self.single_click = False
        self.press_index = -1
        self.press_pos = None
        self.press_was_selected = False
        self.rubber: Optional[Tuple[float, float, float, float]] = None
        self.rubber_base: set = set()
        self.drag_targets = None
        self.drag_actions = 0
        self.provider: Optional[Callable] = None     # entry -> (pixbuf, markup); used when model cols 2/3 are None
        self.muted = False                           # inactive split pane: grey icons, dimmer text
        self._cache: Dict[int, Tuple[object, str]] = {}
        self._layout_cache: Dict[int, Pango.Layout] = {}
        self.connect("draw", self._draw)
        self.connect("size-allocate", lambda w, a: self._relayout())
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", lambda w, e: self._set_prelight(-1))
        self.connect("key-press-event", self._key)
        self.connect("focus-in-event", lambda w, e: self.queue_draw() or False)
        self.connect("focus-out-event", lambda w, e: self.queue_draw() or False)
        self.connect("drag-begin", self._drag_begin)
        self.connect("grab-broken-event", lambda w, e: self._reset_press())
        self.connect("grab-notify", lambda w, was_grabbed: None if was_grabbed else self._reset_press())
        self.set_model(model)

    def _reset_press(self):
        """A dialog or drag grab interrupted a click: forget the pending press/rubber band."""
        self.press_index, self.press_pos = -1, None
        if self.rubber is not None:
            self.rubber = None
            self.queue_draw()
        return False

    def _drag_begin(self, w, ctx):
        if self.cursor >= 0 and self.model is not None:
            try:
                pb = self._tile_data(self.cursor, self.model[self.cursor])[0]
                if pb is not None:
                    Gtk.drag_set_icon_pixbuf(ctx, pb, pb.get_width() // 2, pb.get_height() // 2)
            except (IndexError, ValueError):
                pass

    # ---- model
    def set_model(self, model):
        if self.model is not None:
            for h in getattr(self, "_mh", []):
                self.model.disconnect(h)
        self.model = model
        self._mh = []
        if model is not None:
            self._mh = [model.connect("row-changed", self._row_changed),
                        model.connect("row-inserted", lambda m, p, it: self._model_resized()),
                        model.connect("row-deleted", lambda m, p: self._model_resized())]
        self.selected.clear()
        self.cursor = self.anchor = -1
        self._model_resized()

    def get_model(self):
        return self.model

    def _row_changed(self, m, p, it):
        self._cache.pop(p.get_indices()[0], None)
        self.queue_draw()

    def invalidate(self):
        self._cache.clear()
        self.queue_draw()

    def _tile_data(self, i, row):
        """(pixbuf, markup) for a row: model columns if filled, else computed once via provider and cached."""
        pb, mk = row[2], row[3]
        if pb is not None and mk is not None:
            return pb, mk
        hit = self._cache.get(i)
        if hit is None:
            hit = self.provider(row[0]) if self.provider else (None, "")
            self._cache[i] = hit
        return hit

    def _n(self) -> int:
        return len(self.model) if self.model is not None else 0

    def _model_resized(self):
        n = self._n()
        self.selected = {i for i in self.selected if i < n}
        if self.cursor >= n:
            self.cursor = n - 1
        self._cache.clear()
        self._relayout()

    # ---- layout
    def configure(self, icon_size: int, horizontal: bool, item_width: int, single_click: bool):
        self.icon_size, self.horizontal, self.single_click = icon_size, horizontal, single_click
        probe = self.create_pango_layout("Xg")
        line = probe.get_pixel_size()[1] + 1
        if horizontal:
            self.text_lines = 3 if icon_size >= 48 else 1
            self.tile_w = item_width
            self.tile_h = max(icon_size, self.text_lines * line) + 12
        else:
            self.text_lines = 2
            self.tile_w = max(90, item_width)
            self.tile_h = icon_size + 10 + self.text_lines * line + 10
        self._layout_cache.clear()
        self._cache.clear()
        self._relayout()

    def _relayout(self):
        w = max(1, self.get_allocated_width() - 2 * self.margin)
        self.cols = max(1, w // self.tile_w)
        rows = (self._n() + self.cols - 1) // self.cols
        self.set_size(self.get_allocated_width(), rows * self.tile_h + 2 * self.margin)
        self.queue_draw()

    def _tile_rect(self, i: int) -> Tuple[int, int, int, int]:
        r, c = divmod(i, self.cols)
        return self.margin + c * self.tile_w, self.margin + r * self.tile_h, self.tile_w, self.tile_h

    def _index_at(self, x: float, y: float) -> int:
        """Index of the tile under canvas coords, or -1 (empty space counts as -1 only outside the tile box)."""
        c = int((x - self.margin) // self.tile_w)
        r = int((y - self.margin) // self.tile_h)
        if x < self.margin or y < self.margin or c < 0 or c >= self.cols or r < 0:
            return -1
        i = r * self.cols + c
        return i if i < self._n() else -1

    def _visible_range(self) -> Tuple[int, int]:
        v = Gtk.Scrollable.get_vadjustment(self)
        top = int(v.get_value()) if v else 0
        h = self.get_allocated_height()
        r0 = max(0, (top - self.margin) // self.tile_h)
        r1 = (top + h - self.margin) // self.tile_h + 1
        return int(r0 * self.cols), int(min(self._n(), (r1 + 1) * self.cols))

    # ---- drawing
    def _draw(self, w, cr):
        n = self._n()
        if n == 0:
            return False
        # GTK 3.24 hands us a widget-relative cairo context; the scrolled canvas lives in the bin window,
        # which sits at (-hadj, -vadj). Translate so we can draw in canvas coordinates.
        cr.save()
        cr.translate(-self._hoff(), -self._voff())
        try:
            self._draw_tiles(cr)
        finally:
            cr.restore()
        return False

    def _draw_tiles(self, cr):
        n = self._n()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        ok, sel = sc.lookup_color("theme_selected_bg_color")
        sel = sel if ok else Gdk.RGBA(0.2, 0.5, 0.9, 1)
        ok, selfg = sc.lookup_color("theme_selected_fg_color")
        selfg = selfg if ok else Gdk.RGBA(1, 1, 1, 1)
        focused = self.has_focus()
        a, b = self._visible_range()
        pad = 6
        if self.horizontal:
            cr.save()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.055)
            cr.set_line_width(1)
            for i in range(a, b, self.cols):
                x, y, tw, th = self._tile_rect(i)
                cr.move_to(self.margin, y + th - 0.5)
                cr.line_to(self.margin + self.cols * self.tile_w, y + th - 0.5)
            cr.stroke()
            cr.restore()
        for i in range(a, b):
            try:
                row = self.model[i]
            except (IndexError, ValueError):
                break
            x, y, tw, th = self._tile_rect(i)
            is_sel = i in self.selected
            if is_sel or i == self.prelight:
                cr.save()
                _rounded(cr, x + 2, y + 2, tw - 4, th - 4, 6)
                if is_sel:
                    g_ = 0.2126 * sel.red + 0.7152 * sel.green + 0.0722 * sel.blue
                    if self.muted:
                        cr.set_source_rgba(g_, g_, g_, 0.35)
                    else:
                        cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.85 if focused else 0.5)
                else:
                    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
                cr.fill()
                cr.restore()
            pb, markup = self._tile_data(i, row)
            size = self.icon_size
            if self.horizontal:
                ix, iy = x + pad, y + (th - size) // 2
                tx, ty, tw_text = x + pad + size + 8, y + 6, tw - size - 3 * pad - 8
            else:
                ix, iy = x + (tw - size) // 2, y + pad
                tx, ty, tw_text = x + 4, y + pad + size + 6, tw - 8
            if pb is not None:
                if self.muted:
                    pb = ICONS.desaturated(pb)
                ox, oy = (size - pb.get_width()) // 2, (size - pb.get_height()) // 2
                Gdk.cairo_set_source_pixbuf(cr, pb, ix + ox, iy + oy)
                cr.paint()
            lay = self._text_layout(cr, i, markup, tw_text)
            if is_sel:
                cr.set_source_rgba(selfg.red, selfg.green, selfg.blue, 1)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6 if self.muted else 1)
            cr.move_to(tx, ty if self.horizontal else ty)
            PangoCairo.show_layout(cr, lay)
            if i == self.cursor and focused:
                cr.save()
                _rounded(cr, x + 2, y + 2, tw - 4, th - 4, 6)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
                cr.set_line_width(1)
                cr.set_dash([2, 2])
                cr.stroke()
                cr.restore()
        if self.rubber:
            x0, y0, x1, y1 = self.rubber
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.2)
            cr.fill_preserve()
            cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.8)
            cr.set_line_width(1)
            cr.stroke()
        return False

    def _text_layout(self, cr, i, markup, width):
        lay = self.create_pango_layout("")        # widget's own Pango context: same font as the rest of the UI
        lay.set_markup(markup or "", -1)
        lay.set_width(max(10, int(width * Pango.SCALE)))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_height(-self.text_lines)
        lay.set_alignment(Pango.Alignment.LEFT if self.horizontal else Pango.Alignment.CENTER)
        return lay

    # ---- selection API (Gtk.IconView-compatible subset)
    def _emit_sel(self):
        self.emit("selection-changed")
        self.queue_draw()

    def get_selected_items(self):
        return [Gtk.TreePath.new_from_indices([i]) for i in sorted(self.selected)]

    def path_is_selected(self, path) -> bool:
        return path.get_indices()[0] in self.selected

    def select_path(self, path):
        self.selected.add(path.get_indices()[0])
        self._emit_sel()

    def unselect_path(self, path):
        self.selected.discard(path.get_indices()[0])
        self._emit_sel()

    def select_all(self):
        self.selected = set(range(self._n()))
        self._emit_sel()

    def unselect_all(self):
        if self.selected:
            self.selected.clear()
            self._emit_sel()

    def set_cursor(self, path, cell=None, start_editing=False):
        self.cursor = path.get_indices()[0]
        self.anchor = self.cursor
        self.queue_draw()

    def get_cursor(self):
        return (Gtk.TreePath.new_from_indices([self.cursor]) if self.cursor >= 0 else None, None)

    def scroll_to_path(self, path, use_align=False, row_align=0.0, col_align=0.0):
        i = path.get_indices()[0]
        x, y, tw, th = self._tile_rect(i)
        v = Gtk.Scrollable.get_vadjustment(self)
        if v is None:
            return
        top, page = v.get_value(), v.get_page_size()
        if y < top:
            v.set_value(y - self.margin)
        elif y + th > top + page:
            v.set_value(y + th - page + self.margin)

    def get_path_at_pos(self, x, y):
        """x, y in bin-window (canvas) coordinates, which is what button events on a Gtk.Layout carry."""
        i = self._index_at(x, y)
        return Gtk.TreePath.new_from_indices([i]) if i >= 0 else None

    def get_dest_item_at_pos(self, x, y):
        """x, y in widget coordinates (what drag-data-received gives)."""
        p = self.get_path_at_pos(x + self._hoff(), y + self._voff())
        return (p, Gtk.IconViewDropPosition.DROP_INTO) if p is not None else None

    def set_activate_on_single_click(self, v):
        self.single_click = v

    def enable_model_drag_source(self, start_button_mask, targets, actions):
        self.drag_targets = Gtk.TargetList.new(targets)
        self.drag_actions = actions

    def _voff(self):
        v = Gtk.Scrollable.get_vadjustment(self)          # (Gtk.Layout's own getter is deprecated)
        return v.get_value() if v else 0

    def _hoff(self):
        h = Gtk.Scrollable.get_hadjustment(self)
        return h.get_value() if h else 0

    # ---- mouse
    def _set_prelight(self, i):
        if i != self.prelight:
            self.prelight = i
            self.queue_draw()

    def _press(self, w, event):
        self.grab_focus()
        x, y = event.x, event.y          # bin-window coordinates
        i = self._index_at(x, y)
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            if i >= 0 and not self.single_click:
                self.emit("item-activated", Gtk.TreePath.new_from_indices([i]))
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if event.button == 1:
            self.press_index, self.press_pos = i, (event.x, event.y)
            self.press_was_selected = i in self.selected
            if i < 0:
                if not (ctrl or shift):
                    self.unselect_all()
                self.rubber = (x, y, x, y)
                self.rubber_base = set(self.selected) if ctrl else set()
                return True
            if ctrl:
                (self.selected.discard if i in self.selected else self.selected.add)(i)
                self.cursor = self.anchor = i
                self._emit_sel()
            elif shift and self.anchor >= 0:
                lo, hi = min(self.anchor, i), max(self.anchor, i)
                self.selected = set(range(lo, hi + 1))
                self.cursor = i
                self._emit_sel()
            elif i not in self.selected:
                self.selected = {i}
                self.cursor = self.anchor = i
                self._emit_sel()
            else:
                self.cursor = i
                self.queue_draw()
            return True
        if event.button == 3:
            return False     # FolderView handles the context menu (it reads get_path_at_pos)
        return False

    def _motion(self, w, event):
        x, y = event.x, event.y
        if self.rubber is not None:
            self.rubber = (self.rubber[0], self.rubber[1], x, y)
            x0, y0, x1, y1 = self.rubber
            rx0, ry0, rx1, ry1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
            hit = set()
            r0, r1 = max(0, int((ry0 - self.margin) // self.tile_h)), int((ry1 - self.margin) // self.tile_h)
            for r in range(r0, r1 + 1):
                for c in range(self.cols):
                    i = r * self.cols + c
                    if i >= self._n():
                        break
                    tx, ty, tw, th = self._tile_rect(i)
                    if tx < rx1 and tx + tw > rx0 and ty < ry1 and ty + th > ry0:
                        hit.add(i)
            new = self.rubber_base ^ hit if self.rubber_base else hit
            if new != self.selected:
                self.selected = new
                self.emit("selection-changed")
            self.queue_draw()
            # autoscroll near edges
            v = Gtk.Scrollable.get_vadjustment(self)
            if v:
                vy = event.y - v.get_value()
                if vy < 12:
                    v.set_value(max(v.get_lower(), v.get_value() - 10))
                elif vy > self.get_allocated_height() - 12:
                    v.set_value(min(v.get_upper() - v.get_page_size(), v.get_value() + 10))
            return True
        if self.press_index >= 0 and self.press_pos is not None and self.drag_targets is not None and (event.state & Gdk.ModifierType.BUTTON1_MASK):
            if self.drag_check_threshold(int(self.press_pos[0]), int(self.press_pos[1]), int(event.x), int(event.y)):
                if self.press_index not in self.selected:
                    self.selected = {self.press_index}
                    self._emit_sel()
                self.drag_begin_with_coordinates(self.drag_targets, self.drag_actions, 1, event, int(event.x), int(event.y))
                self.press_index, self.press_pos = -1, None
                return True
        self._set_prelight(self._index_at(x, y))
        return False

    def _release(self, w, event):
        if event.button != 1:
            return False
        if self.rubber is not None:
            self.rubber = None
            self.queue_draw()
            return True
        i = self.press_index
        if i >= 0 and self.press_pos is not None:
            mods = event.state & Gtk.accelerator_get_default_mod_mask()
            moved = self.drag_check_threshold(int(self.press_pos[0]), int(self.press_pos[1]), int(event.x), int(event.y))
            if not moved and not mods:
                if self.single_click:
                    self.emit("item-activated", Gtk.TreePath.new_from_indices([i]))
                elif self.press_was_selected and self.selected != {i}:
                    self.selected = {i}     # click on one of several selected items selects just it
                    self._emit_sel()
        self.press_index, self.press_pos = -1, None
        return True

    # ---- keyboard
    def _key(self, w, event):
        n = self._n()
        if n == 0:
            return False
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        cur = self.cursor if self.cursor >= 0 else 0
        page_rows = max(1, int(self.get_allocated_height() // self.tile_h))
        moves = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -self.cols, Gdk.KEY_Down: self.cols,
                 Gdk.KEY_Page_Up: -self.cols * page_rows, Gdk.KEY_Page_Down: self.cols * page_rows,
                 Gdk.KEY_KP_Left: -1, Gdk.KEY_KP_Right: 1, Gdk.KEY_KP_Up: -self.cols, Gdk.KEY_KP_Down: self.cols}
        if key in moves or key in (Gdk.KEY_Home, Gdk.KEY_End):
            new = 0 if key == Gdk.KEY_Home else (n - 1 if key == Gdk.KEY_End else cur + moves[key])
            new = max(0, min(n - 1, new))
            self.cursor = new
            if shift:
                a = self.anchor if self.anchor >= 0 else cur
                self.selected = set(range(min(a, new), max(a, new) + 1))
            elif not ctrl:
                self.selected = {new}
                self.anchor = new
            self.scroll_to_path(Gtk.TreePath.new_from_indices([new]))
            self._emit_sel()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not mods and self.cursor >= 0:
            self.emit("item-activated", Gtk.TreePath.new_from_indices([self.cursor]))
            return True
        if key == Gdk.KEY_space and not mods and self.cursor >= 0:
            self.selected = {self.cursor}
            self.anchor = self.cursor
            self._emit_sel()
            return True
        if key == Gdk.KEY_a and ctrl:
            self.select_all()
            return True
        return False


def _rounded(cr, x, y, w, h, r):
    import math
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# --------------------------------------------------------------------------- This PC: Windows-style tile view
class DriveBar(Gtk.DrawingArea):
    """The thin capacity bar under a drive's name: used/total, red past 90 % like Explorer."""

    def __init__(self):
        super().__init__()
        self.frac = None
        self.set_size_request(120, 10)
        self.connect("draw", self._draw)

    def set_fraction(self, frac):
        self.frac = frac
        self.queue_draw()

    def _draw(self, w, cr):
        a = self.get_allocation()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        _rounded(cr, 0, 2, a.width, a.height - 4, 2)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.fill()
        if self.frac is not None:
            f = max(0.0, min(1.0, self.frac))
            if f > 0:
                _rounded(cr, 0, 2, max(4, a.width * f), a.height - 4, 2)
                if f > 0.9:
                    cr.set_source_rgb(0.85, 0.22, 0.2)
                else:
                    ok, sel = sc.lookup_color("theme_selected_bg_color")
                    cr.set_source_rgb(sel.red, sel.green, sel.blue) if ok else cr.set_source_rgb(0.2, 0.5, 0.9)
                cr.fill()
        return False


class PCView(Gtk.ScrolledWindow):
    """Explorer-style 'This PC': collapsible sections (Folders, one per physical disk, Network locations) of
    tiles. Folder tiles are icon + name; drive tiles are icon, name, capacity bar and 'x free of y'.
    Built from ordinary GTK widgets (FlowBox per section) so selection, keyboard navigation, hover and
    activation behave like the rest of the desktop. Emits 'selection-changed', 'item-activated' (Entry) and
    'context-menu' (Entry or None, event)."""

    __gsignals__ = {"selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
                    "item-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
                    "context-menu": (GObject.SignalFlags.RUN_FIRST, None, (object, object))}

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=12)
        self.add(self.box)
        self.flows: List[Gtk.FlowBox] = []
        self.tiles: Dict[int, Tuple[Gtk.FlowBoxChild, Entry, Optional[DriveBar], Optional[Gtk.Label]]] = {}
        self.single_click = False
        self._suppress = False
        self.collapsed: set = set()

    # ---- build
    def set_entries(self, ents: List[Entry], single_click: bool):
        self.single_click = single_click
        for c in self.box.get_children():
            self.box.remove(c)
        self.flows, self.tiles = [], {}
        sections: List[Tuple[str, List[Entry]]] = []
        for e in ents:
            if sections and sections[-1][0] == e.section:
                sections[-1][1].append(e)
            else:
                sections.append((e.section, [e]))
        for title, items in sections:
            exp = Gtk.Expander(expanded=title not in self.collapsed)
            lab = Gtk.Label(xalign=0)
            lab.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>  <span alpha='55%'>({len(items)})</span>")
            exp.set_label_widget(lab)
            exp.connect("notify::expanded", lambda ex, p, t=title: (self.collapsed.discard(t) if ex.get_expanded() else self.collapsed.add(t)))
            fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.MULTIPLE, homogeneous=True, max_children_per_line=12, min_children_per_line=1,
                             row_spacing=4, column_spacing=4, margin_top=4, margin_bottom=8, margin_start=16, valign=Gtk.Align.START)
            fb.set_activate_on_single_click(single_click)
            fb.connect("selected-children-changed", self._sel_changed)
            fb.connect("child-activated", lambda f, ch: self.emit("item-activated", self.tiles[id(ch)][1]))
            fb.connect("button-press-event", self._press)
            for e in items:
                ch = Gtk.FlowBoxChild()
                tile, bar, info = self._make_tile(e)
                ch.add(tile)
                fb.add(ch)
                self.tiles[id(ch)] = (ch, e, bar, info)
            exp.add(fb)
            self.flows.append(fb)
            self.box.pack_start(exp, False, False, 0)
            sep = Gtk.Separator()
            sep.set_opacity(0.35)
            self.box.pack_start(sep, False, False, 2)
        self.box.show_all()
        self.refresh()

    def _make_tile(self, e: Entry):
        is_drive = e.section not in ("Folders",) and (getattr(e, "drive_kind", "") or e.usage is not None)
        h = Gtk.Box(spacing=10, margin=6)
        img = Gtk.Image.new_from_pixbuf(ICONS.pixbuf(e.icon, 48)) if e.icon else Gtk.Image.new_from_icon_name("folder", Gtk.IconSize.DIALOG)
        img.set_valign(Gtk.Align.START)
        h.pack_start(img, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=e.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26)
        v.pack_start(name, False, False, 0)
        bar = info = None
        if is_drive:
            bar = DriveBar()
            v.pack_start(bar, False, False, 2)
            info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
            info.get_style_context().add_class("dim-label")
            v.pack_start(info, False, False, 0)
            h.set_size_request(250, -1)
        else:
            sub = Gtk.Label(label=e.location or "", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=26)
            sub.get_style_context().add_class("dim-label")
            v.pack_start(sub, False, False, 0)
            h.set_size_request(200, -1)
        h.pack_start(v, True, True, 0)
        if getattr(e, "drive_kind", "") == "unmounted":
            h.set_opacity(0.55)
        return h, bar, info

    def refresh(self):
        """Re-read usage/type on the entries (the free-space thread fills them in after the first paint)."""
        for ch, e, bar, info in self.tiles.values():
            if bar is not None:
                used, total = (e.usage or (0, 0))
                bar.set_fraction((used / total) if total else None)
            if info is not None:
                info.set_text(e.type or "")

    # ---- selection
    def _sel_changed(self, fb):
        if self._suppress:
            return
        self._suppress = True
        for other in self.flows:            # one selection across all sections (plain click only)
            if other is not fb and not (Gtk.get_current_event_state()[1] & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)):
                other.unselect_all()
        self._suppress = False
        self.emit("selection-changed")

    def selected_entries(self) -> List[Entry]:
        out = []
        for fb in self.flows:
            for ch in fb.get_selected_children():
                out.append(self.tiles[id(ch)][1])
        return out

    def select_none(self):
        self._suppress = True
        for fb in self.flows:
            fb.unselect_all()
        self._suppress = False
        self.emit("selection-changed")

    def select_all(self):
        self._suppress = True
        for fb in self.flows:
            fb.select_all()
        self._suppress = False
        self.emit("selection-changed")

    def select_names(self, names: List[str]):
        self._suppress = True
        for fb in self.flows:
            fb.unselect_all()
            for ch in fb.get_children():
                if self.tiles[id(ch)][1].name in names:
                    fb.select_child(ch)
        self._suppress = False
        self.emit("selection-changed")

    def _press(self, fb, event):
        if event.button == 3:
            ch = fb.get_child_at_pos(int(event.x), int(event.y))
            if ch is not None:
                if not ch.is_selected():
                    self.select_none()
                    fb.select_child(ch)
                self.emit("context-menu", self.tiles[id(ch)][1], event)
            else:
                self.select_none()
                self.emit("context-menu", None, event)
            return True
        if event.button == 1 and fb.get_child_at_pos(int(event.x), int(event.y)) is None:
            self.select_none()
        return False


# --------------------------------------------------------------------------- lightweight 3D model preview
def load_model(path: str, max_faces: int = 500000) -> Optional[dict]:
    """Parse OBJ / STL (ascii+binary) / PLY (ascii) into {verts: [(x,y,z)], faces: [(i,j,k)], info: [...]}.
    Big meshes are decimated by skipping faces so the preview stays interactive."""
    global LAST_VFX_ERROR
    ext = os.path.splitext(path)[1].lower()
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    nfaces_total = 0
    extra: List[Tuple[str, str]] = []
    if ext == ".obj" and np is None:
        ext = ".obj-slow"
    if ext == ".obj":
        # numpy fast path: pull the 'v' and 'f' lines out in bulk; the Python fallback below handles the rest
        with open(path, "rb") as f:
            data = f.read()
        vlines = [l for l in data.split(b"\n") if l[:2] == b"v "]
        try:
            V = np.array(b" ".join(l[2:].strip() for l in vlines).split(), dtype=np.float64)
            V = V.reshape(len(vlines), -1)[:, :3] if len(vlines) else np.zeros((0, 3))
        except ValueError:
            V = None
        if V is not None and len(V):
            verts = V
            flines = [l[2:].strip() for l in data.split(b"\n") if l[:2] == b"f "]
            nvert = len(V)
            # drop the /vt/vn parts, then every remaining token is a vertex index; polygons are fan-triangulated
            # with the same vectorised scheme as the Alembic reader
            blob = re.sub(rb"[ \t]+", b" ", re.sub(rb"/\S*", b"", b"\n".join(flines))).strip()
            rows = blob.split(b"\n")
            counts = np.fromiter((r.count(b" ") + 1 for r in rows), dtype=np.int64, count=len(rows))
            toks = blob.split()
            idx = np.array(toks).astype(np.int64) if toks else np.zeros(0, dtype=np.int64)
            idx = np.where(idx > 0, idx - 1, nvert + idx)
            starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
            tris = []
            for t in range(1, int(counts.max()) - 1 if len(counts) else 0):
                sel = counts > t + 1
                s0 = starts[sel]
                tris.append(np.stack((idx[s0], idx[s0 + t], idx[s0 + t + 1]), axis=1))
            faces = np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)
            nfaces_total = len(faces)
            del data, vlines, flines, blob, rows, toks
        else:
            ext = ".obj-slow"
    if ext == ".obj-slow":
        with open(path, "rb") as f:
            for line in f:
                if line.startswith(b"v "):
                    p = line.split()
                    if len(p) >= 4:
                        verts.append((float(p[1]), float(p[2]), float(p[3])))
                elif line.startswith(b"f "):
                    idx = []
                    for tok in line.split()[1:]:
                        s = tok.split(b"/")[0]
                        if s:
                            i = int(s)
                            idx.append(i - 1 if i > 0 else len(verts) + i)
                    for k in range(1, len(idx) - 1):          # fan-triangulate n-gons
                        nfaces_total += 1
                        faces.append((idx[0], idx[k], idx[k + 1]))
    elif ext == ".stl":
        with open(path, "rb") as f:
            head = f.read(84)
            f.seek(0)
            if head[:5].lower() == b"solid" and b"facet" in f.read(4096):
                f.seek(0)
                cur = []
                for line in f:
                    t = line.split()
                    if len(t) == 4 and t[0] == b"vertex":
                        cur.append((float(t[1]), float(t[2]), float(t[3])))
                        if len(cur) == 3:
                            base = len(verts)
                            verts.extend(cur)
                            faces.append((base, base + 1, base + 2))
                            nfaces_total += 1
                            cur = []
            elif np is not None:
                # binary STL in one numpy read: 50-byte records (normal, 3 vertices, attribute)
                f.seek(80)
                n = struct.unpack("<I", f.read(4))[0]
                nfaces_total = n
                dt = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
                arr = np.fromfile(f, dtype=dt, count=n)
                step = max(1, len(arr) // max_faces)
                tri = arr["v"][::step]
                verts = tri.reshape(-1, 3)
                faces = np.arange(len(verts), dtype=np.int64).reshape(-1, 3)
            else:
                f.seek(80)
                n = struct.unpack("<I", f.read(4))[0]
                nfaces_total = n
                step = max(1, n // max_faces)
                rec = struct.Struct("<12fH")
                for i in range(n):
                    data = f.read(50)
                    if len(data) < 50:
                        break
                    if i % step:
                        continue
                    v = rec.unpack(data)
                    base = len(verts)
                    verts.extend([(v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])])
                    faces.append((base, base + 1, base + 2))
    elif ext == ".ply":
        with open(path, "rb") as f:
            nv = nf = 0
            props = []
            fmt = "ascii"
            while True:
                line = f.readline().decode("latin-1").strip()
                if line.startswith("format"):
                    fmt = line.split()[1]
                elif line.startswith("element vertex"):
                    nv = int(line.split()[2])
                    section = "v"
                elif line.startswith("element face"):
                    nf = int(line.split()[2])
                    section = "f"
                elif line.startswith("property") and section == "v":
                    props.append(line.split()[2])
                elif line == "end_header" or not line:
                    break
            if fmt != "ascii":
                return None
            xi, yi, zi = props.index("x"), props.index("y"), props.index("z")
            for _ in range(nv):
                t = f.readline().split()
                verts.append((float(t[xi]), float(t[yi]), float(t[zi])))
            nfaces_total = nf
            for _ in range(nf):
                t = f.readline().split()
                k = int(t[0])
                idx = [int(x) for x in t[1:1 + k]]
                for j in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[j], idx[j + 1]))
    elif ext == ".fbx":
        try:
            verts, faces, nm = load_fbx(path)
        except Exception:
            verts, faces, nm = [], [], 0
        extra = [("Meshes", str(nm)), ("Source", "FBX " + ("binary" if open(path, "rb").read(18) == b"Kaydara FBX Binary" else "ASCII"))]
        if not verts:
            for loader in (load_via_assimp, load_via_blender):
                got = loader(path)
                if got:
                    verts, faces = got[0], got[1]
                    extra = [("Source", f"via {got[2]}")]
                    break
            if not verts:
                return {"error": "Native FBX parser found no mesh. " + (LAST_VFX_ERROR or "")}
        nfaces_total = len(faces)
    elif ext in (".usd", ".usda", ".usdc", ".usdz", ".abc", ".gltf", ".glb", ".dae", ".blend", ".3ds", ".x3d", ".lwo", ".off"):
        verts, faces, src = [], [], ""
        if ext == ".usda":
            with open(path, "r", errors="ignore") as f:
                verts, faces, nm = load_usda_text(f.read())
            src = f"USDA text, {nm} mesh{'es' if nm != 1 else ''}"
        if not verts and ext in (".usd", ".usdc", ".usdz") and shutil.which("usdcat"):
            try:
                r = subprocess.run(["usdcat", "--flatten", path], capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    verts, faces, nm = load_usda_text(r.stdout)
                    src = f"usdcat, {nm} mesh{'es' if nm != 1 else ''}"
            except (OSError, subprocess.TimeoutExpired):
                pass
        # open-source routes first: Blender (abc/usd/gltf/dae/blend…), assimp (fbx/dae/gltf/3ds…); vendor Pythons last
        LAST_VFX_ERROR = ""
        abc_fmt = alembic_format(path) if ext == ".abc" else ""
        if abc_fmt == "hdf5":
            # Legacy HDF5 Alembic: Blender and every current open-source reader only speak Ogawa.
            _vfx_err("Legacy HDF5 Alembic (pre-2014 container): Blender only reads Ogawa archives.")
            # open-source first: h5py reads the HDF5 container directly; vendor PyAlembic only as a last resort
            loaders = [load_abc_hdf5]          # (Maya's PyAlembic rejects HDF5 too: "Unsupported core type")
        else:
            loaders = [load_via_blender, load_via_assimp, load_via_vfx_python]
        for loader in loaders:
            if len(verts):
                break
            got = loader(path)
            if got:
                verts, faces, src = got[0], got[1], f"via {got[2]}"
        if not len(verts):
            return {"error": LAST_VFX_ERROR or "No importer produced any geometry."}
        nfaces_total = len(faces)
        extra = [("Source", src)] + ([("Alembic", {"ogawa": "Ogawa", "hdf5": "HDF5 (legacy)"}.get(abc_fmt, "unknown container"))] if ext == ".abc" else [])
    elif ext not in (".obj", ".obj-slow"):
        return None
    if not len(verts):
        return None
    if np is not None:
        return _finish_model_numpy(verts, faces, max_faces, nfaces_total, extra)
    nverts_total = len(verts)
    if len(faces) > max_faces:
        step = len(faces) // max_faces + 1
        faces = faces[::step]
        # keep only the vertices the surviving faces reference: a 3.7M-vertex cache would otherwise be
        # normalised, pickled back from the worker process and transformed every frame for nothing
        used = sorted({i for f in faces for i in f})
        remap = {old: new for new, old in enumerate(used)}
        verts = [verts[i] for i in used]
        faces = [(remap[i], remap[j], remap[k]) for i, j, k in faces]
    # normalize into a unit box around the origin
    xs, ys, zs = (sorted(c) for c in zip(*verts))
    cx, cy, cz = (xs[0] + xs[-1]) / 2, (ys[0] + ys[-1]) / 2, (zs[0] + zs[-1]) / 2
    scale = max(xs[-1] - xs[0], ys[-1] - ys[0], zs[-1] - zs[0], 1e-9)
    verts = [((x - cx) / scale, (y - cy) / scale, (z - cz) / scale) for x, y, z in verts]
    info = [("Vertices", f"{nverts_total:,}" + (f" (showing {len(verts):,})" if len(verts) < nverts_total else "")), ("Faces", f"{nfaces_total:,}" + (f" (showing {len(faces):,})" if len(faces) < nfaces_total else "")),
            ("Bounds", f"{xs[-1] - xs[0]:.3g} × {ys[-1] - ys[0]:.3g} × {zs[-1] - zs[0]:.3g}")] + extra
    # face normals once, in model space (rotated per frame with 6 multiplies instead of a cross product)
    normals = []
    for i, j, k in faces:
        ax, ay, az = verts[i]
        bx, by, bz = verts[j]
        cx, cy, cz = verts[k]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        normals.append((nx / ln, ny / ln, nz / ln))
    return {"verts": verts, "faces": faces, "normals": normals, "info": info, "nfaces_total": nfaces_total}


def _finish_model_numpy(verts, faces, max_faces: int, nfaces_total: int, extra: list) -> dict:
    """Vectorised post-processing: decimate, drop unused vertices, normalise to a unit box, face + vertex
    normals. verts/faces may be Python lists or numpy arrays. Decimation is by VERTEX CLUSTERING (snap
    vertices to a grid, merge, drop collapsed faces) rather than by skipping faces: skipping leaves a
    swiss-cheese surface where back faces show through the holes, clustering gives a closed low-res proxy
    like a LOD. Everything here is sized for multi-million-face meshes: no np.unique on faces (sort-bound),
    bincount instead of add.at, float32/int32 outputs so the worker→UI pickle stays small."""
    V = np.asarray(verts, dtype=np.float32).reshape(-1, 3)
    F = np.asarray(faces, dtype=np.int32).reshape(-1, 3)
    nverts_total = len(V)
    lo, hi = V.min(axis=0), V.max(axis=0)
    size = (hi - lo).astype(np.float64)
    scale = max(float(size.max()), 1e-9)
    V = ((V - (lo + hi) / 2) / scale).astype(np.float32)   # unit box around the origin, longest side = 1
    decim_note = ""
    if len(F) > max_faces:
        # face count scales ~ grid² for a surface: start from that estimate, then correct at most 3 times
        grid = int(max(16, min(400, 256 * math.sqrt(max_faces / len(F)) * 1.15)))
        best = None
        for _ in range(4):
            q = np.floor((V + 0.5) * grid).astype(np.int64).clip(0, grid - 1)
            key = (q[:, 0] * grid + q[:, 1]) * grid + q[:, 2]
            uniq, inv = np.unique(key, return_inverse=True)
            Fq = inv[F].astype(np.int64)
            keep = (Fq[:, 0] != Fq[:, 1]) & (Fq[:, 1] != Fq[:, 2]) & (Fq[:, 0] != Fq[:, 2])
            Fq = Fq[keep]
            if len(Fq):
                srt = np.sort(Fq, axis=1)
                nc = len(uniq)
                if nc < (1 << 21):
                    packed = (srt[:, 0] << 42) | (srt[:, 1] << 21) | srt[:, 2]
                    _, first = np.unique(packed, return_index=True)
                else:
                    _, first = np.unique(srt.view([("a", np.int64), ("b", np.int64), ("c", np.int64)]).ravel(), return_index=True)
                Fq = Fq[np.sort(first)]                         # dedupe, keep original winding
            best = (grid, uniq, inv, Fq)
            if len(Fq) <= max_faces and len(Fq) >= max_faces * 0.6:
                break
            if len(Fq) <= max_faces and grid >= 400:
                break
            grid = int(max(16, min(400, grid * math.sqrt(max_faces / max(len(Fq), 1)) * 0.98)))
            if grid == best[0]:
                break
        grid, uniq, inv, Fq = best
        if len(Fq) > max_faces:                                  # still over (pathological): fall back to a strict grid
            Fq = Fq[:max_faces]
        # cluster position = mean of the member vertices (smoother than the cell centre)
        counts = np.bincount(inv, minlength=len(uniq)).astype(np.float32)
        Vc = np.empty((len(uniq), 3), dtype=np.float32)
        for c in range(3):
            Vc[:, c] = np.bincount(inv, weights=V[:, c], minlength=len(uniq)) / counts
        V, F = Vc, Fq.astype(np.int32)
        decim_note = f" (showing {len(F):,} after clustering at {grid}³)"
    # drop vertices no face references (mask + cumsum: np.unique would sort 3×faces ints)
    used = np.zeros(len(V), dtype=bool)
    used[F] = True
    remap = np.cumsum(used, dtype=np.int32) - 1
    V = V[used]
    F = remap[F]
    if len(F):
        A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        # Winding convention differs by source (Alembic/Maya export clockwise, OBJ/STL counter-clockwise).
        # The signed volume (divergence theorem) tells which way this mesh faces; flip if negative so that
        # face normals point outwards for the flat-shading path.
        signed_vol = float(np.einsum("ij,ij->i", A.astype(np.float64), np.cross(B, C).astype(np.float64)).sum())
        if signed_vol < 0:
            F = np.ascontiguousarray(F[:, [0, 2, 1]])
            B, C = C, B
        N = np.cross(B - A, C - A).astype(np.float32)
        ln = np.linalg.norm(N, axis=1)
        ln[ln == 0] = 1.0
        N /= ln[:, None]
        # vertex normals = normalised sum of the adjacent unit face normals → smooth shading
        VN = np.zeros_like(V)
        for col in range(3):
            idx = F[:, col]
            for c in range(3):
                VN[:, c] += np.bincount(idx, weights=N[:, c], minlength=len(V)).astype(np.float32)
        vl = np.linalg.norm(VN, axis=1)
        vl[vl == 0] = 1.0
        VN /= vl[:, None]
    else:
        N = np.zeros((0, 3), dtype=np.float32)
        VN = np.zeros((0, 3), dtype=np.float32)
    info = [("Vertices", f"{nverts_total:,}" + (f" (showing {len(V):,})" if len(V) < nverts_total else "")),
            ("Faces", f"{nfaces_total:,}" + decim_note),
            ("Bounds", f"{size[0]:.3g} × {size[1]:.3g} × {size[2]:.3g}")] + extra
    return {"verts": V, "faces": F, "normals": N, "vnormals": VN, "info": info, "nfaces_total": nfaces_total}



def software_render(V, F, N, VN, params):
    """Render one frame of a mesh with the numpy rasteriser. params = (W, H, yaw, pitch, zoom, panx, pany,
    smooth, wire, bg_rgb, fg_rgb). Returns (W, H, BGRX bytes) or None. Runs in the render worker process."""
    W, H, yaw, pitch, zoom, panx, pany, smooth, wire, bg_rgb, fg_rgb = params
    if W < 2 or H < 2 or F is None or not len(F):
        return None
    f32 = np.float32
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    k16 = min(W, H) * 0.8 * zoom * 1.6
    ox, oy = W / 2 + panx, H / 2 + pany
    x, y, z = V[:, 0], V[:, 1], V[:, 2]
    xr = x * f32(cy_) + z * f32(sy_)
    zr = -x * f32(sy_) + z * f32(cy_)
    yp = y * f32(cp) - zr * f32(sp)
    depth = y * f32(sp) + zr * f32(cp)
    wdiv = f32(k16) / (f32(2.6) - depth)
    px = f32(ox) + xr * wdiv
    py = f32(oy) - yp * wdiv
    i, j, k = F[:, 0], F[:, 1], F[:, 2]
    if wire:
        return _wire_render(W, H, px, py, i, j, k, bg_rgb, fg_rgb)

    def lit(NN):
        nx1 = NN[:, 0] * f32(cy_) + NN[:, 2] * f32(sy_)
        nz1 = -NN[:, 0] * f32(sy_) + NN[:, 2] * f32(cy_)
        ny2 = NN[:, 1] * f32(cp) - nz1 * f32(sp)
        nz2 = NN[:, 1] * f32(sp) + nz1 * f32(cp)
        d = np.abs(nx1 * f32(0.4) + ny2 * f32(0.7) + nz2 * f32(0.6))      # two-sided lighting
        return f32(0.35) + f32(0.65) * np.minimum(d, f32(1.0))
    if smooth and VN is not None and len(VN):
        vshade, fshade = lit(VN), None
    else:
        vshade = None
        fshade = lit(N) if N is not None and len(N) else np.full(len(F), 1.0, dtype=f32)
    rgb = _rasterize(W, H, px, py, depth, i, j, k, vshade, fshade, bg_rgb)
    if rgb is None:
        return None
    return W, H, rgb.tobytes()


def _wire_render(W, H, px, py, i, j, k, bg_rgb, fg_rgb):
    """Wireframe as point splats along every edge (no per-line Cairo calls, so it scales to millions of
    edges): each edge is sampled every pixel of its length and the samples are scattered into the image."""
    f32 = np.float32
    out = np.empty((H * W, 4), dtype=np.uint8)
    out[:, 2], out[:, 1], out[:, 0], out[:, 3] = bg_rgb[0], bg_rgb[1], bg_rgb[2], 255
    col = (int(fg_rgb[2] * 255 * 0.55 + bg_rgb[2] * 0.45), int(fg_rgb[1] * 255 * 0.55 + bg_rgb[1] * 0.45), int(fg_rgb[0] * 255 * 0.55 + bg_rgb[0] * 0.45))
    for a_i, b_i in ((i, j), (j, k), (k, i)):
        ax, ay, bx, by = px[a_i], py[a_i], px[b_i], py[b_i]
        on = ((np.maximum(ax, bx) >= 0) & (np.minimum(ax, bx) < W) & (np.maximum(ay, by) >= 0) & (np.minimum(ay, by) < H))
        ax, ay, bx, by = ax[on], ay[on], bx[on], by[on]
        n = np.maximum(1, np.ceil(np.maximum(np.abs(bx - ax), np.abs(by - ay)))).astype(np.int64)
        # cap the work for absurd zoom levels
        total = int(n.sum())
        if total > 30_000_000:
            keep = np.random.default_rng(0).random(len(n)) < 30_000_000 / total
            ax, ay, bx, by, n = ax[keep], ay[keep], bx[keep], by[keep], n[keep]
        rep = np.repeat(np.arange(len(n)), n)
        local = (np.arange(len(rep)) - np.repeat(np.cumsum(n) - n, n)).astype(f32)
        t = local / np.repeat(n, n).astype(f32)
        xs = (ax[rep] + (bx[rep] - ax[rep]) * t).astype(np.int32)
        ys = (ay[rep] + (by[rep] - ay[rep]) * t).astype(np.int32)
        ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        pix = ys[ok] * W + xs[ok]
        out[pix, 2], out[pix, 1], out[pix, 0] = col[2], col[1], col[0]
    return W, H, out.reshape(H, W, 4).tobytes()


def _rasterize(W, H, px, py, depth, i, j, k, vshade, fshade, bg_rgb):
    """Vectorised triangle rasteriser with a z-buffer, in numpy.

    px/py/depth are per-VERTEX screen coordinates and view depth (larger = nearer); i/j/k index them per
    face; vshade is a per-vertex shade (Gouraud) or None, in which case fshade is the per-face shade.

    Two lanes, chosen per triangle by its screen bounding box:
      * tiny (spans at most 1 px — the overwhelming majority on a multi-million-face mesh at preview size):
        splatted as a single pixel at the centroid with the mean corner shade. Edge tests on sub-pixel
        triangles cost a lot and change nothing visible.
      * everything else: the bounding box is expanded into candidate pixels (np.repeat), the three edge
        functions decide which are inside, depth and shade are interpolated barycentrically.
    np.maximum.at resolves the nearest fragment per pixel (fast path in numpy ≥ 1.25). Large lanes are
    chunked so the candidate arrays stay around ~2M entries (float32) whatever the mesh.
    Returns a BGRX uint8 buffer (cairo RGB24) or None if nothing is on screen."""
    f32 = np.float32
    ax, ay, bx, by, cx, cy = px[i], py[i], px[j], py[j], px[k], py[k]
    da, db, dc = depth[i], depth[j], depth[k]
    if vshade is not None:
        sa, sb, sc = vshade[i], vshade[j], vshade[k]
    else:
        sa = sb = sc = fshade.astype(f32)
    minx = np.minimum(np.minimum(ax, bx), cx)
    maxx = np.maximum(np.maximum(ax, bx), cx)
    miny = np.minimum(np.minimum(ay, by), cy)
    maxy = np.maximum(np.maximum(ay, by), cy)
    onscreen = (maxx >= 0) & (minx < W) & (maxy >= 0) & (miny < H)
    tiny = onscreen & ((maxx - minx) <= 1.0) & ((maxy - miny) <= 1.0)
    big = onscreen & ~tiny
    zbuf = np.full(W * H, -np.inf, dtype=f32)
    sbuf = np.zeros(W * H, dtype=f32)

    # ---- lane 1: splats
    if tiny.any():
        t = np.nonzero(tiny)[0]
        sx = np.clip(((ax[t] + bx[t] + cx[t]) * f32(1 / 3)).astype(np.int32), 0, W - 1)
        sy = np.clip(((ay[t] + by[t] + cy[t]) * f32(1 / 3)).astype(np.int32), 0, H - 1)
        pix = sy * W + sx
        zc = (da[t] + db[t] + dc[t]) * f32(1 / 3)
        np.maximum.at(zbuf, pix, zc)
        win = zc >= zbuf[pix]
        sbuf[pix[win]] = ((sa[t] + sb[t] + sc[t]) * f32(1 / 3))[win]

    # ---- lane 2: real rasterisation for triangles that cover pixels
    if big.any():
        x0 = np.clip(np.floor(minx), 0, W).astype(np.int32)
        x1 = np.clip(np.ceil(maxx) + 1, 0, W).astype(np.int32)
        y0 = np.clip(np.floor(miny), 0, H).astype(np.int32)
        y1 = np.clip(np.ceil(maxy) + 1, 0, H).astype(np.int32)
        bw, bh = x1 - x0, y1 - y0
        area2 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        ok = big & (bw > 0) & (bh > 0) & (np.abs(area2) > 1e-6)
        idx_all = np.nonzero(ok)[0]
        counts_all = bw[idx_all].astype(np.int64) * bh[idx_all]
        if len(idx_all):
            csum = np.cumsum(counts_all)
            bounds = np.searchsorted(csum, np.arange(0, csum[-1] + 2_000_000, 2_000_000))
            bounds = np.unique(np.concatenate((bounds, [len(idx_all)])))
            for s0, s1 in zip(bounds[:-1], bounds[1:]):
                t = idx_all[s0:s1]
                n = counts_all[s0:s1]
                total = int(n.sum())
                if not len(t) or total == 0:
                    continue
                tri = np.repeat(np.arange(len(t), dtype=np.int32), n)
                local = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(n) - n, n)
                w_rep = np.repeat(bw[t], n)
                pxs = np.repeat(x0[t], n) + (local % w_rep).astype(np.int32)
                pys = np.repeat(y0[t], n) + (local // w_rep).astype(np.int32)
                fx = pxs.astype(f32) + f32(0.5)
                fy = pys.astype(f32) + f32(0.5)
                tt = t[tri]
                A_x, A_y, B_x, B_y, C_x, C_y = ax[tt], ay[tt], bx[tt], by[tt], cx[tt], cy[tt]
                w0 = (B_x - fx) * (C_y - fy) - (B_y - fy) * (C_x - fx)      # weight of A
                w1 = (C_x - fx) * (A_y - fy) - (C_y - fy) * (A_x - fx)      # weight of B
                w2 = (A_x - fx) * (B_y - fy) - (A_y - fy) * (B_x - fx)      # weight of C
                inside = ((w0 >= 0) & (w1 >= 0) & (w2 >= 0)) | ((w0 <= 0) & (w1 <= 0) & (w2 <= 0))
                if not inside.any():
                    continue
                sel = np.nonzero(inside)[0]
                tt = tt[sel]
                inv_a = f32(1.0) / area2[tt]
                w0s, w1s, w2s = w0[sel] * inv_a, w1[sel] * inv_a, w2[sel] * inv_a
                zz = w0s * da[tt] + w1s * db[tt] + w2s * dc[tt]
                pix = pys[sel] * W + pxs[sel]
                np.maximum.at(zbuf, pix, zz)
                win = zz >= zbuf[pix]
                tw = tt[win]
                sbuf[pix[win]] = w0s[win] * sa[tw] + w1s[win] * sb[tw] + w2s[win] * sc[tw]

    hit = zbuf > -np.inf
    if not hit.any():
        return None
    out = np.empty((H * W, 4), dtype=np.uint8)
    base_r, base_g, base_b = bg_rgb
    out[:, 2], out[:, 1], out[:, 0], out[:, 3] = base_r, base_g, base_b, 255
    sh = sbuf[hit]
    out[hit, 2] = np.clip((0.42 * sh + 0.1) * 255, 0, 255).astype(np.uint8)    # R
    out[hit, 1] = np.clip((0.55 * sh + 0.1) * 255, 0, 255).astype(np.uint8)    # G
    out[hit, 0] = np.clip((0.72 * sh + 0.12) * 255, 0, 255).astype(np.uint8)   # B
    return np.ascontiguousarray(out.reshape(H, W, 4))



def _render_worker_main(conn):
    """Render worker process: holds one model, answers ('frame', params) with the pixels."""
    pin_worker()
    V = F = N = VN = None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        kind = msg[0]
        if kind == "quit":
            return
        if kind == "model":
            V, F, N, VN = msg[1]
            continue
        if kind == "frame":
            try:
                res = software_render(V, F, N, VN, msg[1])
            except Exception as ex:          # never let a bad frame kill the worker
                res = None
                print("kestrel: render failed —", ex, file=sys.stderr)
            try:
                conn.send((msg[1], res))
            except (OSError, ValueError):
                return


_RENDER_WORKER = None


def get_render_worker():
    """One render process for the whole app (created early by warm_workers, otherwise on first use)."""
    global _RENDER_WORKER
    if _RENDER_WORKER is None or not _RENDER_WORKER.proc.is_alive():
        _RENDER_WORKER = RenderWorker()
    return _RENDER_WORKER


class RenderWorker:
    """Owns the render process and the result thread; results are delivered on the GTK thread via idle_add."""

    def __init__(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_render_worker_main, args=(child,), daemon=True)
        self.proc.start()
        child.close()
        self._cb = None
        # ONE sender thread with a queue: messages must reach the worker in the order they were issued
        # (model before the frame that needs it). Separate per-message threads raced, and a frame request
        # overtaking its model made the worker answer with no image — the software viewer then never showed.
        import queue
        self._q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    def _sender(self):
        while True:
            msg = self._q.get()
            try:
                self.conn.send(msg)
            except (OSError, ValueError):
                return

    def set_model(self, V, F, N, VN):
        self._q.put(("model", (V, F, N, VN)))      # a 5M-face model takes ~0.3 s to pipe across, off-thread

    def render(self, params, cb):
        self._cb = cb
        self._q.put(("frame", params))

    def _reader(self):
        while True:
            try:
                params, res = self.conn.recv()
            except (EOFError, OSError):
                return
            cb = self._cb
            if cb:
                GLib.idle_add(lambda: (cb(params, res), False)[1])

    def close(self):
        try:
            self._q.put(("quit",))
        except Exception:
            pass
        try:
            self.proc.join(timeout=1)
            if self.proc.is_alive():
                self.proc.kill()
        except Exception:
            pass


class ModelNav:
    """Maya-style navigation shared by the software (ModelView) and OpenGL (GLModelView) previewers:
    LMB orbit, MMB pan, Alt+RMB drag-zoom, wheel zoom, double-click reset, W wireframe, S flat/smooth."""

    def _nav_init(self):
        self.model: Optional[dict] = None
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        self.pan = [0.0, 0.0]
        self.wire = False
        self.smooth = True
        self.thin = False
        self.drag = None            # (x, y, mode) mode: orbit | pan | zoom
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", lambda w, e: (setattr(self, "drag", None), self.queue_draw()) and False)
        self.connect("motion-notify-event", self._motion)
        self.connect("scroll-event", self._scroll)
        self.connect("key-press-event", self._key)

    def _reset_view(self):
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        self.pan = [0.0, 0.0]

    def _press(self, w, event):
        """Maya-style navigation: LMB (or Alt+LMB) orbit, MMB (or Alt+MMB) pan, Alt+RMB zoom-drag."""
        self.grab_focus()
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self._reset_view()
            self.queue_draw()
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if event.button == 2:
            mode = "pan"
        elif event.button == 3 and alt:
            mode = "zoom"
        elif event.button == 1:
            mode = "orbit"
        else:
            return False
        self.drag = (event.x, event.y, mode)
        return True

    def _motion(self, w, event):
        if not self.drag:
            return False
        x0, y0, mode = self.drag
        dx, dy = event.x - x0, event.y - y0
        if mode == "orbit":
            self.yaw += dx * 0.01
            self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.01))
        elif mode == "pan":
            self.pan[0] += dx
            self.pan[1] += dy
        elif mode == "zoom":
            self.zoom = max(0.2, min(8.0, self.zoom * (1.0 + (dx - dy) * 0.005)))
        self.drag = (event.x, event.y, mode)
        self.queue_draw()
        return True

    def _scroll(self, w, event):
        d = 1.0
        if event.direction == Gdk.ScrollDirection.UP:
            d = 1.15
        elif event.direction == Gdk.ScrollDirection.DOWN:
            d = 1 / 1.15
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            d = 1 / 1.15 if event.delta_y > 0 else 1.15
        self.zoom = max(0.2, min(8.0, self.zoom * d))
        self.queue_draw()
        return True

    def _key(self, w, event):
        if event.keyval in (Gdk.KEY_w, Gdk.KEY_W):
            self.wire = not self.wire
            self.queue_draw()
            return True
        if event.keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.smooth = not self.smooth
            self.queue_draw()
            return True
        return False


class ModelView(ModelNav, Gtk.DrawingArea):
    """Software previewer drawn with Cairo + numpy: z-buffer rasteriser (see _rasterize), Gouraud shading,
    wireframe on W. Used when hardware acceleration is off or PyOpenGL is missing."""

    def __init__(self):
        super().__init__()
        self._nav_init()
        self._worker = None
        self._frame = None            # (w, h, BGRX bytes) last rendered frame
        self._frame_params = None
        self._inflight = False
        self._want = None
        self.on_first_frame = None       # one-shot: the window fades the pane in when the first frame lands
        self._V = self._F = self._N = self._VN = None
        self.connect("draw", self._draw)
        # (the render process is shared and lives for the whole session; nothing to tear down per view)

    def set_model(self, model: Optional[dict]):
        self.model = model
        self._reset_view()
        self._V = self._F = self._N = self._VN = None
        if model and np is not None:
            # numpy copies for the vectorised path: projection, culling, sorting and lighting become array
            # ops, leaving only the Cairo path building in Python
            self._V = np.asarray(model["verts"], dtype=np.float32).reshape(-1, 3)
            self._F = np.asarray(model["faces"], dtype=np.int32).reshape(-1, 3)
            self._N = np.asarray(model["normals"], dtype=np.float32).reshape(-1, 3) if model.get("normals") is not None and len(model["normals"]) else None
            self._VN = np.asarray(model["vnormals"], dtype=np.float32).reshape(-1, 3) if model.get("vnormals") is not None and len(model["vnormals"]) else None
            self._worker = get_render_worker()
            self._worker.set_model(self._V, self._F, self._N, self._VN)
        # Keep the previous frame on screen until the new model's first frame lands (model → model would
        # otherwise blank the viewport for the whole load, which reads as a flash before the crossfade).
        self._frame_params = None
        self._want = None
        self._awaiting_first = True
        if model is None:
            self._frame = None
        self.queue_draw()

    def _draw(self, w, cr):
        """Paint the latest frame from the render worker; if the camera moved, ask for a new one. The GTK
        thread never rasterises: the worker process does, on the non-reserved cores, and posts the pixels back.
        Until the new frame lands the previous one stays on screen, so orbiting is never blocked."""
        a = self.get_allocation()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.04)
        cr.rectangle(0, 0, a.width, a.height)
        cr.fill()
        if not self.model:
            return False
        if self._V is None:
            self._draw_python(cr, a, fg, 1)         # no numpy: synchronous fallback
            return False
        params = self._params(a.width, a.height)
        if self._frame is not None:
            fw, fh, buf = self._frame
            try:
                import cairo
                surf = cairo.ImageSurface.create_for_data(memoryview(buf), cairo.FORMAT_RGB24, fw, fh, fw * 4)
                if (fw, fh) != (a.width, a.height):
                    cr.save()
                    cr.scale(a.width / fw, a.height / fh)
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint()
                    cr.restore()
                else:
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint()
            except Exception as ex:
                bench(f"paint failed: {ex}")
        if params != self._frame_params:
            self._request(params)
        return False

    def _params(self, w: int, h: int):
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        return (w, h, round(self.yaw, 4), round(self.pitch, 4), round(self.zoom, 4),
                round(self.pan[0]), round(self.pan[1]), self.smooth, self.wire,
                self._bg_rgb(fg), (fg.red, fg.green, fg.blue))

    def prime(self, w: int, h: int):
        """Request the first frame before the widget is on screen (a hidden stack child is never drawn, so
        _draw alone would never ask). w/h: the size it will be shown at."""
        if self._V is not None and getattr(self, "_awaiting_first", False) and w > 1 and h > 1:
            self._request(self._params(w, h))

    def _request(self, params):
        """Ask the worker for a frame; while one is in flight just remember the newest wish (coalescing)."""
        if self._inflight:
            self._want = params
            return
        self._want = None
        self._inflight = True
        self._worker.render(params, self._on_frame)

    def _on_frame(self, params, frame):
        self._inflight = False
        first = getattr(self, "_awaiting_first", False) and frame is not None
        if first:
            self._awaiting_first = False
        if frame is not None:
            w, h, buf = frame
            # the pipe delivers immutable bytes; cairo.ImageSurface.create_for_data needs a WRITABLE buffer
            # (it raised TypeError and the viewport stayed blank)
            self._frame = (w, h, bytearray(buf))
            self._frame_params = params
        if first and self.on_first_frame:
            cb, self.on_first_frame = self.on_first_frame, None
            cb()
        if self._want is not None and self._want != params:
            self._request(self._want)
        self.queue_draw()

    def _camera(self, a):
        cy_, sy_ = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        s = min(a.width, a.height) * 0.8 * self.zoom
        ox, oy = a.width / 2 + self.pan[0], a.height / 2 + self.pan[1]
        return cy_, sy_, cp, sp, s * 1.6, ox, oy

    def _bg_rgb(self, fg):
        """The viewport background as 8-bit RGB: the theme's window colour with the 4 % fg tint we paint over it."""
        try:
            ok, bgc = self.get_style_context().lookup_color("theme_bg_color")
            if not ok:
                raise ValueError
            r, g, b = bgc.red, bgc.green, bgc.blue
        except Exception:
            r, g, b = (0.13, 0.13, 0.13) if fg.red > 0.5 else (0.98, 0.98, 0.98)
        mix = lambda c, f: int(round((c * 0.96 + f * 0.04) * 255))
        return mix(r, fg.red), mix(g, fg.green), mix(b, fg.blue)

    def _draw_python(self, cr, a, fg, step):
        """Pure-Python fallback (no numpy): same picture, ~20× slower on big meshes."""
        m = self.model
        cy_, sy_, cp, sp, k16, ox, oy = self._camera(a)
        faces, normals = m["faces"], m.get("normals")
        proj = [(ox + (x * cy_ + z * sy_) * k16 / (2.6 - (y * sp + (-x * sy_ + z * cy_) * cp)),
                 oy - (y * cp - (-x * sy_ + z * cy_) * sp) * k16 / (2.6 - (y * sp + (-x * sy_ + z * cy_) * cp)),
                 y * sp + (-x * sy_ + z * cy_) * cp) for x, y, z in m["verts"]]
        if self.wire:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.set_line_width(0.6)
            for fi in range(0, len(faces), step):
                i, j, k = faces[fi]
                p, q, r = proj[i], proj[j], proj[k]
                cr.move_to(p[0], p[1])
                cr.line_to(q[0], q[1])
                cr.line_to(r[0], r[1])
                cr.close_path()
            cr.stroke()
            return
        idx = range(0, len(faces), step)
        order = sorted(idx, key=lambda fi: proj[faces[fi][0]][2] + proj[faces[fi][1]][2] + proj[faces[fi][2]][2])
        lx, ly, lz = 0.4, 0.7, 0.6
        for fi in order:
            i, j, k = faces[fi]
            p, q, r = proj[i], proj[j], proj[k]
            if (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]) > 0:
                continue                                   # back face
            nx, ny, nz = normals[fi] if normals else (0.0, 0.0, 1.0)
            nx1, nz1 = nx * cy_ + nz * sy_, -nx * sy_ + nz * cy_
            ny2, nz2 = ny * cp - nz1 * sp, ny * sp + nz1 * cp
            d = nx1 * lx + ny2 * ly + nz2 * lz
            shade = 0.35 + 0.65 * (d if d > 0 else 0.0)
            cr.set_source_rgb(0.42 * shade + 0.1, 0.55 * shade + 0.1, 0.72 * shade + 0.12)
            cr.move_to(p[0], p[1])
            cr.line_to(q[0], q[1])
            cr.line_to(r[0], r[1])
            cr.close_path()
            cr.fill()


# --------------------------------------------------------------------------- OpenGL previewer (optional PyOpenGL)
try:
    from OpenGL import GL as _GL          # type: ignore
    from OpenGL.GL import shaders as _GLshaders   # type: ignore
    HAVE_GL = True
except Exception:                          # ImportError, or a broken libGL
    _GL = None
    HAVE_GL = False

# Context flavours tried in order; the shader header is picked to match (see _gl_header).
GL_ATTEMPTS = [("core 3.2", False, (3, 2)), ("core 3.0", False, (3, 0)), ("GLES 3.0", True, (3, 0)), ("legacy/default", False, None)]


def gl_try_contexts(gdk_window, log: Optional[list] = None):
    """Create a GdkGLContext on gdk_window, trying each flavour in GL_ATTEMPTS. Returns (ctx, label) or
    (None, None); each attempt's outcome is appended to log when given. Shared by GLModelView and
    `kestrel.py --probe-gl`, so the probe tests exactly what the viewer does."""
    for label, use_es, ver in GL_ATTEMPTS:
        try:
            ctx = gdk_window.create_gl_context()
            ctx.set_use_es(1 if use_es else 0)
            if ver:
                ctx.set_required_version(*ver)
            ctx.realize()
            if log is not None:
                v = ctx.get_version()
                log.append(f"{label}: OK (got {v[0]}.{v[1]}{' ES' if ctx.get_use_es() else ''})")
            return ctx, label
        except Exception as ex:
            if log is not None:
                log.append(f"{label}: {getattr(ex, 'message', ex)}")
    return None, None


def _gl_header(ctx) -> str:
    if ctx.get_use_es():
        return "#version 300 es\nprecision highp float;\n"
    v = ctx.get_version()
    return "#version 150 core\n" if (v[0], v[1]) >= (3, 2) else "#version 130\n"


_GL_VERT = """
in vec3 pos;
in vec3 nrm;
uniform mat4 mvp;
uniform mat4 mv;
uniform mat3 nmat;
out vec3 vN;
out vec3 vP;
void main() {
    gl_Position = mvp * vec4(pos, 1.0);
    vN = nmat * nrm;
    vP = (mv * vec4(pos, 1.0)).xyz;
}
"""
_GL_FRAG = """
in vec3 vN;
in vec3 vP;
uniform int smoothShade;
uniform int wire;
uniform vec3 wireColor;
out vec4 frag;
void main() {
    if (wire == 1) { frag = vec4(wireColor, 1.0); return; }
    vec3 n = (smoothShade == 1) ? normalize(vN) : normalize(cross(dFdx(vP), dFdy(vP)));
    float d = abs(dot(n, normalize(vec3(0.4, 0.7, 0.6))));     // two-sided
    float sh = 0.35 + 0.65 * min(d, 1.0);
    frag = vec4(vec3(0.08 + 0.90 * sh), 1.0);        // neutral white: tells the GL path from the blue software one
}
"""


class GLModelView(ModelNav, Gtk.GLArea):
    """Hardware-accelerated previewer: the mesh is uploaded to the GPU once (interleaved position+normal VBO,
    uint32 index buffer) and every frame is a single glDrawElements, so a 5M-triangle mesh orbits at the
    display's refresh rate. Same camera maths as the software path (yaw → pitch → distance 2.6, pan in
    pixels), same shading (two-sided, Gouraud or flat via screen-space derivatives), same keys."""

    def __init__(self):
        super().__init__()
        self._nav_init()
        self.set_has_depth_buffer(True)
        self.set_auto_render(True)
        self.gl_flavour = ""
        self.connect("create-context", self._create_context)
        self._prog = None
        self._vao = self._vbo = self._ebo = None
        self._count = 0
        self._dirty = False
        self.gl_info = ""
        self.gl_error: Optional[str] = None
        self.on_error = None                  # callable(msg) set by the window: shown in the UI, triggers fallback
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    def _fail(self, where: str, ex) -> None:
        """Record the first OpenGL failure (later ones are noise), log it, and tell the window."""
        if self.gl_error:
            return
        msg = f"{where}: {ex}".replace("\n", " ")[:400]
        self.gl_error = msg
        GL_STATUS[:] = [f"OpenGL FAILED ({self.gl_info or 'no context'}): {msg}"]
        print("kestrel: OpenGL preview failed —", msg, file=sys.stderr)
        if self.on_error:
            GLib.idle_add(lambda: (self.on_error(msg), False)[1])

    def _create_context(self, area):
        """GLArea asks us for a context: negotiate the best flavour the driver/compositor will give us."""
        log: list = []
        ctx, label = gl_try_contexts(self.get_window(), log)
        if ctx is None:
            self._fail("context", " | ".join(log) + "   (run: python3 kestrel.py --probe-gl)")
            return None
        self.gl_flavour = label
        return ctx

    # ---- lifecycle
    def _on_realize(self, w):
        try:
            self.make_current()
            err = self.get_error()
            if err is not None:
                if not self.gl_error:
                    self._fail("context", err.message if hasattr(err, "message") else err)
                return
            ctx = self.get_context()
            ver = ctx.get_version() if ctx else (0, 0)
            self.gl_info = f"GL {ver[0]}.{ver[1]}" + (" ES" if ctx and ctx.get_use_es() else "")
            try:
                self.gl_info += " — " + _GL.glGetString(_GL.GL_RENDERER).decode(errors="replace")
            except Exception:
                pass
            hdr = _gl_header(ctx)
            self.gl_info += f" [{self.gl_flavour}]"
            self._prog = _GLshaders.compileProgram(_GLshaders.compileShader(hdr + _GL_VERT, _GL.GL_VERTEX_SHADER),
                                                   _GLshaders.compileShader(hdr + _GL_FRAG, _GL.GL_FRAGMENT_SHADER))
            self._vao = _GL.glGenVertexArrays(1)
            self._vbo = _GL.glGenBuffers(1)
            self._ebo = _GL.glGenBuffers(1)
            self._dirty = self.model is not None
            GL_STATUS[:] = [f"OpenGL OK: {self.gl_info}"]
        except Exception as ex:
            self._fail("init", ex)

    def _on_unrealize(self, w):
        self.make_current()
        try:
            if self._vao:
                _GL.glDeleteVertexArrays(1, [self._vao])
            if self._vbo:
                _GL.glDeleteBuffers(1, [self._vbo])
            if self._ebo:
                _GL.glDeleteBuffers(1, [self._ebo])
            if self._prog:
                _GL.glDeleteProgram(self._prog)
        except Exception:
            pass
        self._prog = self._vao = self._vbo = self._ebo = None

    def set_model(self, model: Optional[dict]):
        self.model = model
        self._reset_view()
        self._dirty = True
        self.queue_render()

    def queue_draw(self):             # ModelNav calls queue_draw(); GLArea wants queue_render()
        self.queue_render()

    # ---- upload
    def _upload(self):
        m = self.model
        self._dirty = False
        if not m or not len(m["faces"]):
            self._count = 0
            return
        V = np.asarray(m["verts"], dtype=np.float32).reshape(-1, 3)
        F = np.asarray(m["faces"], dtype=np.uint32).reshape(-1, 3)
        VN = m.get("vnormals")
        if VN is None or not len(VN):
            VN = np.zeros_like(V)
            VN[:, 2] = 1.0
        data = np.ascontiguousarray(np.hstack((V, np.asarray(VN, dtype=np.float32).reshape(-1, 3))))
        _GL.glBindVertexArray(self._vao)
        _GL.glBindBuffer(_GL.GL_ARRAY_BUFFER, self._vbo)
        _GL.glBufferData(_GL.GL_ARRAY_BUFFER, data.nbytes, data, _GL.GL_STATIC_DRAW)
        _GL.glBindBuffer(_GL.GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        idx = np.ascontiguousarray(F)
        _GL.glBufferData(_GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, _GL.GL_STATIC_DRAW)
        stride = 6 * 4
        lp = _GL.glGetAttribLocation(self._prog, "pos")
        ln = _GL.glGetAttribLocation(self._prog, "nrm")
        _GL.glEnableVertexAttribArray(lp)
        import ctypes
        _GL.glVertexAttribPointer(lp, 3, _GL.GL_FLOAT, _GL.GL_FALSE, stride, ctypes.c_void_p(0))
        _GL.glEnableVertexAttribArray(ln)
        _GL.glVertexAttribPointer(ln, 3, _GL.GL_FLOAT, _GL.GL_FALSE, stride, ctypes.c_void_p(12))
        _GL.glBindVertexArray(0)
        self._count = int(idx.size)

    # ---- matrices (row-major numpy, uploaded with transpose=True)
    def _matrices(self, w: int, h: int):
        cy_, sy_ = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        Ry = np.array([[cy_, 0, sy_, 0], [0, 1, 0, 0], [-sy_, 0, cy_, 0], [0, 0, 0, 1]], dtype=np.float64)
        Rx = np.array([[1, 0, 0, 0], [0, cp, -sp, 0], [0, sp, cp, 0], [0, 0, 0, 1]], dtype=np.float64)
        T = np.eye(4)
        T[2, 3] = -2.6
        mv = T @ Rx @ Ry
        s = min(w, h) * 0.8 * self.zoom * 1.6
        fx, fy = s / (w / 2), s / (h / 2)
        near, far = 0.05, 20.0
        P = np.array([[fx, 0, 0, 0], [0, fy, 0, 0],
                      [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                      [0, 0, -1, 0]], dtype=np.float64)
        # pan in pixels → NDC offset after projection
        Pan = np.eye(4)
        Pan[0, 3] = self.pan[0] * 2.0 / w
        Pan[1, 3] = -self.pan[1] * 2.0 / h
        mvp = Pan @ P @ mv
        return mvp.astype(np.float32), mv.astype(np.float32), (Rx @ Ry)[:3, :3].astype(np.float32)

    # ---- render
    def _on_render(self, area, ctx):
        try:
            self._render()
        except Exception as ex:
            self._fail("render", ex)
        return True

    def _render(self):
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        try:
            ok, bgc = sc.lookup_color("theme_bg_color")
            r, g, b = (bgc.red, bgc.green, bgc.blue) if ok else ((0.13,) * 3 if fg.red > 0.5 else (0.98,) * 3)
        except Exception:
            r, g, b = (0.13,) * 3
        _GL.glClearColor(r * 0.96 + fg.red * 0.04, g * 0.96 + fg.green * 0.04, b * 0.96 + fg.blue * 0.04, 1.0)
        _GL.glClear(_GL.GL_COLOR_BUFFER_BIT | _GL.GL_DEPTH_BUFFER_BIT)
        if self.gl_error or not self._prog:
            return
        if self._dirty:
            self._upload()
        if not self._count:
            return
        scale = self.get_scale_factor()
        w, h = max(1, self.get_allocated_width()), max(1, self.get_allocated_height())
        _GL.glViewport(0, 0, w * scale, h * scale)
        mvp, mv, nmat = self._matrices(w, h)
        _GL.glEnable(_GL.GL_DEPTH_TEST)
        _GL.glDisable(_GL.GL_CULL_FACE)
        _GL.glUseProgram(self._prog)
        _GL.glUniformMatrix4fv(_GL.glGetUniformLocation(self._prog, "mvp"), 1, _GL.GL_TRUE, mvp)
        _GL.glUniformMatrix4fv(_GL.glGetUniformLocation(self._prog, "mv"), 1, _GL.GL_TRUE, mv)
        _GL.glUniformMatrix3fv(_GL.glGetUniformLocation(self._prog, "nmat"), 1, _GL.GL_TRUE, nmat)
        _GL.glUniform1i(_GL.glGetUniformLocation(self._prog, "smoothShade"), 1 if self.smooth else 0)
        _GL.glUniform1i(_GL.glGetUniformLocation(self._prog, "wire"), 1 if self.wire else 0)
        _GL.glUniform3f(_GL.glGetUniformLocation(self._prog, "wireColor"), fg.red, fg.green, fg.blue)
        _GL.glPolygonMode(_GL.GL_FRONT_AND_BACK, _GL.GL_LINE if self.wire else _GL.GL_FILL)
        _GL.glBindVertexArray(self._vao)
        _GL.glDrawElements(_GL.GL_TRIANGLES, self._count, _GL.GL_UNSIGNED_INT, None)
        _GL.glBindVertexArray(0)
        _GL.glPolygonMode(_GL.GL_FRONT_AND_BACK, _GL.GL_FILL)
        _GL.glUseProgram(0)


# --------------------------------------------------------------------------- video / audio playback (GStreamer)
GST_ERROR = ""
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore
    Gst.init(None)
    HAVE_GST = True
except Exception as _ex:                 # typelib missing (gstreamer1 / python3-gobject), or a broken plugin registry
    Gst = None
    HAVE_GST = False
    GST_ERROR = f"{_ex.__class__.__name__}: {_ex}"


def gst_sink_available(refresh: bool = False) -> bool:
    """gtksink lives in gstreamer1-plugins-good-gtk on Rocky; without it we can't embed video in the pane.
    refresh=True re-scans the plugin registry so a package installed while Kestrel runs is picked up."""
    if not HAVE_GST:
        return False
    if refresh:
        try:
            Gst.update_registry()
        except Exception:
            pass
    return Gst.ElementFactory.find("gtksink") is not None


H264_DECODERS = ("avdec_h264", "openh264dec", "nvh264dec", "nvh264sldec", "vah264dec", "vaapih264dec", "v4l2h264dec", "d3d11h264dec")


def gst_decoder(*names) -> Optional[str]:
    if not HAVE_GST:
        return None
    for n in names:
        if Gst.ElementFactory.find(n) is not None:
            return n
    return None


def gst_h264_status() -> Tuple[Optional[str], str]:
    """(decoder element name or None, explanation). Looks at every H.264 decoder GStreamer could offer
    (software libav/openh264, NVIDIA, VA-API, V4L2) and, when none is registered, says whether the libav
    plugin is installed-but-blacklisted (the usual cause: a libav plugin built against a different ffmpeg)."""
    if not HAVE_GST:
        return None, "GStreamer not importable"
    dec = gst_decoder(*H264_DECODERS)
    if dec:
        return dec, ""
    reg = Gst.Registry.get()
    pl = reg.find_plugin("libav")
    if pl is None:
        return None, "no libav plugin in the registry"
    # the plugin exists: does it provide any element?
    feats = [f.get_name() for f in reg.get_feature_list_by_plugin("libav")]
    if not feats:
        return None, (f"libav plugin found at {pl.get_filename()} but it loaded no elements — "
                      "it is probably blacklisted for an ffmpeg library mismatch: run  gst-inspect-1.0 libav  in a terminal to see the error, "
                      "then  rm -rf ~/.cache/gstreamer-1.0  and restart")
    return None, f"libav plugin loaded {len(feats)} elements but no H.264 decoder among them"


class VideoPreview(Gtk.Box):
    """In-pane player for the preview area: playbin → gtksink widget, with play/pause, a seek bar, time
    readout and mute. Opens paused on the first frame (no autoplay noise), Space toggles playback.
    Codec coverage is whatever GStreamer has: the free EPEL plugins do VP8/VP9/AV1/Theora/MJPEG; H.264/H.265
    and ProRes need gstreamer1-plugins-ugly + gstreamer1-libav from RPM Fusion (see Dependencies)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.player = Gst.ElementFactory.make("playbin", "kestrel-player")
        sink = Gst.ElementFactory.make("gtksink", None)
        self.player.set_property("video-sink", sink)
        self.sink_widget = sink.get_property("widget")
        self.sink_widget.set_size_request(-1, 160)
        # The sink widget keeps showing the LAST frame it ever drew — through a stop, and even on an audio-only
        # file where it never draws a new one. So it lives in a stack behind a blank and an audio card, and is
        # only shown once the new file has prerolled a video frame.
        self.screen = Gtk.Stack()
        self.screen.set_homogeneous(False)
        blank = Gtk.DrawingArea()
        blank.connect("draw", lambda w, cr: (cr.set_source_rgb(0, 0, 0), cr.paint(), False)[2])
        self.screen.add_named(blank, "blank")
        self.screen.add_named(self.sink_widget, "video")
        audio = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        audio.pack_start(Gtk.Image.new_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.DIALOG), False, False, 0)
        self.audio_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.audio_label.get_style_context().add_class("dim-label")
        audio.pack_start(self.audio_label, False, False, 0)
        self.screen.add_named(audio, "audio")
        self.screen.set_visible_child_name("blank")
        # The sink widget's NATURAL size is the video's pixel size (1920×1080 for HD). Let that propagate and
        # GTK keeps re-requesting the toplevel at that width — the window twitched sideways every frame. A
        # scrolled window with external policies swallows the natural size; the screen fills whatever the
        # pane gives it and the sink scales the picture inside.
        holder = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        holder.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.EXTERNAL)
        holder.add(self.screen)
        self.pack_start(holder, True, True, 0)
        bar = Gtk.Box(spacing=4)
        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self.play_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.play_btn.connect("clicked", lambda b: self.toggle())
        bar.pack_start(self.play_btn, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self._seeking = False
        self.scale.connect("button-press-event", lambda w, e: setattr(self, "_seeking", True) and False)
        self.scale.connect("button-release-event", self._seek_release)
        self.scale.connect("change-value", self._seek_change)
        bar.pack_start(self.scale, True, True, 0)
        self.time_lab = Gtk.Label(label="0:00 / 0:00")
        self.time_lab.get_style_context().add_class("dim-label")
        bar.pack_start(self.time_lab, False, False, 4)
        self.mute_btn = Gtk.ToggleButton()
        self.mute_btn.set_image(Gtk.Image.new_from_icon_name("audio-volume-high-symbolic", Gtk.IconSize.BUTTON))
        self.mute_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mute_btn.set_tooltip_text("Mute")
        self.mute_btn.connect("toggled", self._mute)
        bar.pack_start(self.mute_btn, False, False, 0)
        self.pack_start(clip_width(bar), False, False, 0)
        self.err_lab = Gtk.Label(xalign=0, wrap=True)
        self.err_lab.get_style_context().add_class("dim-label")
        self.pack_start(self.err_lab, False, False, 0)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", lambda b, m: self._on_eos())
        bus.connect("message::state-changed", self._on_state)
        self._timer = None
        self.playing = False
        self.on_ready = None             # called once the first frame is prerolled (window fades the pane in then)
        self._ready_sent = False
        # All pipeline state changes go through one queue thread, in order. set_state() can block for
        # hundreds of ms (decoder teardown), so it must not run on the GTK thread — but two free threads
        # (stop() then load()) could reorder and leave a fresh load stuck at NULL.
        import queue
        self._ops: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._op_loop, daemon=True, name="kestrel-video-state").start()
        self.set_can_focus(True)
        self.connect("key-press-event", self._key)

    def _op_loop(self):
        while True:
            fn = self._ops.get()
            try:
                fn()
            except Exception as ex:
                print("kestrel: video state change failed —", ex, file=sys.stderr)

    # ---- control
    def load(self, path: str):
        self.playing = False
        self.err_lab.set_text("")
        self._ready_sent = False
        self.screen.set_visible_child_name("blank")          # never show the previous file's frame
        self.audio_label.set_text(os.path.basename(path))
        uri = GLib.filename_to_uri(path, None)
        player = self.player

        def do_load():
            player.set_state(Gst.State.NULL)
            player.set_property("uri", uri)
            player.set_state(Gst.State.PAUSED)          # preroll: first frame, no sound
        self._ops.put(do_load)
        self._set_icon(False)
        if self._timer is None:
            self._timer = GLib.timeout_add(250, self._tick)

    def stop(self):
        player = self.player
        self._ops.put(lambda: player.set_state(Gst.State.NULL))
        self.screen.set_visible_child_name("blank")
        self.playing = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self._set_icon(False)

    def toggle(self):
        self.playing = not self.playing
        st = Gst.State.PLAYING if self.playing else Gst.State.PAUSED
        player = self.player
        self._ops.put(lambda: player.set_state(st))
        self._set_icon(self.playing)

    def _set_icon(self, playing: bool):
        self.play_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic", Gtk.IconSize.BUTTON))

    def _mute(self, b):
        player, on = self.player, b.get_active()
        self._ops.put(lambda: player.set_property("mute", on))
        b.set_image(Gtk.Image.new_from_icon_name("audio-volume-muted-symbolic" if b.get_active() else "audio-volume-high-symbolic", Gtk.IconSize.BUTTON))

    def _key(self, w, event):
        if event.keyval == Gdk.KEY_space:
            self.toggle()
            return True
        return False

    # ---- seeking / clock
    # Every GStreamer call that can block — queries and seeks included, not just state changes — goes
    # through the op thread. query_duration() on a file that is still prerolling (a movie on a share)
    # blocks until the demuxer has the header; called from the GTK thread it froze the whole window.
    def _seek_change(self, scale, scroll, value):
        player = self.player
        v = max(0.0, min(value, 1000.0)) / 1000.0

        def do_seek():
            ok, dur = player.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(v * dur))
        self._ops.put(do_seek)
        return False

    def _seek_release(self, w, e):
        self._seeking = False
        return False

    @staticmethod
    def _fmt(ns: int) -> str:
        s = max(0, int(ns // Gst.SECOND))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _tick(self):
        """GTK-thread timer: only ENQUEUES a poll; the op thread talks to GStreamer and posts results back."""
        if getattr(self, "_poll_pending", False):
            return True
        self._poll_pending = True
        player = self.player

        def poll():
            try:
                _, cur, _pend = player.get_state(0)
                ok_p, pos = player.query_position(Gst.Format.TIME)
                ok_d, dur = player.query_duration(Gst.Format.TIME)
                nvid = player.get_property("n-video") if cur in (Gst.State.PAUSED, Gst.State.PLAYING) else -1
            except Exception:
                cur, ok_p, pos, ok_d, dur, nvid = None, False, 0, False, 0, -1
            GLib.idle_add(self._poll_done, cur, ok_p and ok_d and dur > 0, pos, dur, nvid)
        self._ops.put(poll)
        return True

    def _poll_done(self, cur, ok, pos, dur, nvid=-1):
        self._poll_pending = False
        if not self._ready_sent and cur in (Gst.State.PAUSED, Gst.State.PLAYING):
            self._mark_ready(nvid)
        if ok:
            if not self._seeking:
                self.scale.set_value(pos / dur * 1000.0)
            self.time_lab.set_text(f"{self._fmt(pos)} / {self._fmt(dur)}")
        return False

    # ---- bus
    def _mark_ready(self, nvid: int):
        """First usable state for the current file: reveal the video surface (or the audio card) and tell the
        window it can fade the pane in. The ONLY place that sets _ready_sent, so the screen can't stay blank."""
        if self._ready_sent:
            return
        self._ready_sent = True
        self.screen.set_visible_child_name("video" if nvid != 0 else "audio")
        if self.on_ready:
            self.on_ready()

    def _on_error(self, bus, msg):
        if not self._ready_sent:                              # show the pane anyway so the error is visible
            GLib.idle_add(lambda: (self._mark_ready(0), False)[1])
        err, dbg = msg.parse_error()
        text = err.message
        if "decoder" in text.lower() or "plugin" in text.lower() or "codec" in text.lower():
            text += "\nMissing codec."
        self.err_lab.set_text(text)
        self.stop()
        self.screen.set_visible_child_name("audio")      # card with the file name under the error

    def _on_eos(self):
        player = self.player
        self._ops.put(lambda: (player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0), player.set_state(Gst.State.PAUSED)))
        self.playing = False
        self._set_icon(False)

    def _on_state(self, bus, msg):
        if msg.src is self.player:
            old, new, pending = msg.parse_state_changed()
            if new == Gst.State.PAUSED and old == Gst.State.READY:
                self._tick()          # the poll it enqueues decides video-vs-audio and fires on_ready


# --------------------------------------------------------------------------- FBX / USD / Alembic loaders
def _fbx_binary(data: bytes):
    """Walk a binary FBX node tree and collect every Geometry's Vertices + PolygonVertexIndex."""
    version = struct.unpack_from("<I", data, 23)[0]
    big = version >= 7500
    hdr = struct.Struct("<QQQB") if big else struct.Struct("<IIIB")
    meshes = []
    pos = 27

    def read_prop(p):
        t = data[p:p + 1]
        p += 1
        if t in (b"Y",):
            return struct.unpack_from("<h", data, p)[0], p + 2
        if t == b"C":
            return data[p], p + 1
        if t == b"I":
            return struct.unpack_from("<i", data, p)[0], p + 4
        if t == b"F":
            return struct.unpack_from("<f", data, p)[0], p + 4
        if t == b"D":
            return struct.unpack_from("<d", data, p)[0], p + 8
        if t == b"L":
            return struct.unpack_from("<q", data, p)[0], p + 8
        if t in (b"f", b"d", b"l", b"i", b"b"):
            n, enc, clen = struct.unpack_from("<III", data, p)
            p += 12
            raw = data[p:p + clen]
            p += clen
            if enc == 1:
                raw = zlib.decompress(raw)
            fmt = {b"f": "f", b"d": "d", b"l": "q", b"i": "i", b"b": "b"}[t]
            import array
            arr = array.array(fmt)
            arr.frombytes(raw[:n * arr.itemsize])
            return arr, p
        if t in (b"S", b"R"):
            n = struct.unpack_from("<I", data, p)[0]
            return data[p + 4:p + 4 + n], p + 4 + n
        raise ValueError(f"unknown FBX property type {t!r}")

    def read_node(p, depth):
        end, nprops, plen, nlen = hdr.unpack_from(data, p)
        if end == 0:
            return None, p + hdr.size
        p += hdr.size
        name = data[p:p + nlen]
        p += nlen
        props = []
        pend = p + plen
        want = name in (b"Vertices", b"PolygonVertexIndex") or depth < 3
        while p < pend:
            v, p2 = read_prop(p) if want else (None, None)
            if not want:
                break
            props.append(v)
            p = p2
        p = pend
        children = {}
        if name in (b"Objects", b"Geometry") or depth == 0:
            while p < end:
                cname, cprops, p = read_node_full(p, depth + 1)
                if cname is None:
                    break
                if cname in (b"Geometry", b"Vertices", b"PolygonVertexIndex"):
                    children.setdefault(cname, []).append(cprops)
        if name == b"Geometry" and b"Vertices" in children and b"PolygonVertexIndex" in children:
            meshes.append((children[b"Vertices"][0][0], children[b"PolygonVertexIndex"][0][0]))
        return name, props, end

    def read_node_full(p, depth):
        r = read_node(p, depth)
        if r[0] is None:
            return None, None, r[1]
        return r

    while pos + hdr.size <= len(data):
        name, props, pos = read_node_full(pos, 0)
        if name is None:
            break
    return meshes


def _fbx_ascii(text: str):
    import re
    meshes = []
    for gm in re.finditer(r"Geometry:[^{]*\{(.*?)\n\t\}", text, re.S):
        body = gm.group(1)
        v = re.search(r"Vertices:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", body, re.S)
        i = re.search(r"PolygonVertexIndex:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", body, re.S)
        if v and i:
            verts = [float(x) for x in v.group(1).replace("\n", "").split(",") if x.strip()]
            idx = [int(x) for x in i.group(1).replace("\n", "").split(",") if x.strip()]
            meshes.append((verts, idx))
    return meshes


def load_fbx(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"Kaydara FBX Binary"):
        meshes = _fbx_binary(data)
    else:
        meshes = _fbx_ascii(data.decode("latin-1"))
    verts, faces = [], []
    for vflat, idx in meshes:
        base = len(verts)
        verts.extend((vflat[i], vflat[i + 1], vflat[i + 2]) for i in range(0, len(vflat) - 2, 3))
        poly = []
        for raw in idx:
            last = raw < 0
            poly.append((~raw if last else raw) + base)
            if last:
                for k in range(1, len(poly) - 1):
                    faces.append((poly[0], poly[k], poly[k + 1]))
                poly = []
    return verts, faces, len(meshes)


def load_usda_text(text: str):
    """Pull points / faceVertexIndices / faceVertexCounts out of every Mesh prim in a USDA file."""
    import re
    verts, faces = [], []
    nmesh = 0
    for m in re.finditer(r"def\s+Mesh\s+\"[^\"]*\"[^{]*\{", text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            c = text[i]
            depth += (c == "{") - (c == "}")
            i += 1
        body = text[start:i]
        p = re.search(r"point3f\[\]\s+points\s*=\s*\[(.*?)\]", body, re.S)
        fi = re.search(r"int\[\]\s+faceVertexIndices\s*=\s*\[(.*?)\]", body, re.S)
        fc = re.search(r"int\[\]\s+faceVertexCounts\s*=\s*\[(.*?)\]", body, re.S)
        if not (p and fi and fc):
            continue
        base = len(verts)
        pts = re.findall(r"\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)", p.group(1))
        verts.extend((float(a), float(b), float(c)) for a, b, c in pts)
        idx = [int(x) + base for x in fi.group(1).replace("\n", " ").split(",") if x.strip()]
        counts = [int(x) for x in fc.group(1).replace("\n", " ").split(",") if x.strip()]
        k = 0
        for n in counts:
            poly = idx[k:k + n]
            k += n
            for j in range(1, len(poly) - 1):
                faces.append((poly[0], poly[j], poly[j + 1]))
        nmesh += 1
    return verts, faces, nmesh


_MESH_SCRIPT = r'''
import sys
path = sys.argv[1]
def emit(verts, counts, idx, base):
    for v in verts: sys.stdout.write("v %r %r %r\n" % (float(v[0]), float(v[1]), float(v[2])))
    k = 0
    for n in counts:
        poly = [int(idx[k + j]) + base + 1 for j in range(int(n))]
        k += int(n)
        sys.stdout.write("f " + " ".join(map(str, poly)) + "\n")
    return base + len(verts)
if path.lower().endswith(".abc"):
    import alembic
    from alembic.Abc import IArchive
    from alembic.AbcGeom import IPolyMesh
    arch = IArchive(path)
    base = 0
    def walk(obj):
        global base
        if IPolyMesh.matches(obj.getMetaData()):
            samp = IPolyMesh(obj.getParent(), obj.getName()).getSchema().getValue()
            base = emit(samp.getPositions(), samp.getFaceCounts(), samp.getFaceIndices(), base)
        for i in range(obj.getNumChildren()): walk(obj.getChild(i))
    walk(arch.getTop())
else:
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.Open(path)
    base = 0
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            m = UsdGeom.Mesh(prim)
            xf = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            pts = [xf.Transform(p) for p in (m.GetPointsAttr().Get() or [])]
            base = emit(pts, m.GetFaceVertexCountsAttr().Get() or [], m.GetFaceVertexIndicesAttr().Get() or [], base)
'''


def alembic_format(path: str) -> str:
    """'ogawa' (modern, what Blender reads), 'hdf5' (pre-2014 legacy container) or 'unknown' from the file's magic bytes."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"
    if head.startswith(b"Ogawa"):
        return "ogawa"
    if head == b"\x89HDF\r\n\x1a\n":
        return "hdf5"
    return "unknown"


def load_abc_hdf5(path: str) -> Optional[Tuple[list, list, str]]:
    """Read a legacy HDF5-container Alembic with h5py (pip install --user h5py; the wheel bundles libhdf5, no
    compile). Alembic's HDF5 backend stores a property's first sample as the dataset '<prop>.smp0' inside the
    schema group (e.g. ABC/<obj>/<shape>/.prop/.geom/P.smp0), so a PolyMesh is any group holding 'P',
    '.faceCounts' and '.faceIndices' samples.
    The walk is name-based and structure-agnostic on purpose: it survives the schema nesting differences between
    Alembic 1.0–1.5 writers. Positions are object-local (no xform stack), which is fine for a preview."""
    try:
        import h5py  # type: ignore
    except ImportError:
        _vfx_err("h5py: not installed (python3 -m pip install --user h5py) — needed to read HDF5 Alembic without Maya")
        return None
    import re
    # first sample of a property is the dataset '<prop>.smp0'; later samples live under '<prop>.smpi/0001…'
    pat = re.compile(r"^(P|\.faceCounts|\.faceIndices)\.smp0$")
    meshes: dict = {}
    seen_names: List[str] = []
    try:
        with h5py.File(path, "r") as h5:
            def visit(name, obj):
                if not isinstance(obj, h5py.Dataset):
                    return
                base = name.rsplit("/", 1)[-1]
                if len(seen_names) < 40:
                    seen_names.append(name)
                mobj = pat.match(base)
                if not mobj:
                    return
                prop = mobj.group(1)
                parent = name.rsplit("/", 1)[0] if "/" in name else ""
                meshes.setdefault(parent, {})[prop] = (0, name)
            h5.visititems(visit)
            verts, faces = [], []
            for parent, props in meshes.items():
                if not all(k in props for k in ("P", ".faceCounts", ".faceIndices")):
                    continue
                import numpy as np
                P = np.asarray(h5[props["P"][1]][()])
                counts = np.asarray(h5[props[".faceCounts"][1]][()], dtype=np.int64)
                idx = np.asarray(h5[props[".faceIndices"][1]][()], dtype=np.int64)
                pts = P.reshape(-1, 3).astype(np.float64)
                base = sum(len(v) for v in verts)
                verts.append(pts)
                # vectorised fan triangulation: triangle t of a polygon is (v0, v_t, v_t+1)
                starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
                for t in range(1, int(counts.max()) - 1 if len(counts) else 0):
                    sel = counts > t + 1
                    s0 = starts[sel]
                    faces.append(np.stack((idx[s0], idx[s0 + t], idx[s0 + t + 1]), axis=1) + base)
            if verts:
                nm = sum(1 for p in meshes.values() if "P" in p)
                return (np.concatenate(verts), np.concatenate(faces) if faces else np.zeros((0, 3), dtype=np.int64),
                        f"h5py (HDF5 Alembic, {nm} mesh{'es' if nm != 1 else ''})")
    except Exception as ex:
        _vfx_err(f"h5py: {ex}")
        return None
    _vfx_err("h5py: opened the file but found no PolyMesh samples. Datasets seen: " + ", ".join(seen_names[:12]))
    return None


def _vfx_pythons(include_vendor: bool = False) -> List[str]:
    """Interpreters that might have PyAlembic / pxr: the running one, system python3, and — only on PATH, or
    when include_vendor is set for HDF5 Alembic that nothing open-source can read — Maya's mayapy / Houdini's hython."""
    import glob
    cands = [sys.executable, shutil.which("python3") or "", shutil.which("mayapy") or "", shutil.which("hython") or ""]
    if include_vendor:
        cands += sorted(glob.glob("/usr/autodesk/maya*/bin/mayapy") + glob.glob("/opt/autodesk/maya*/bin/mayapy") +
                        glob.glob("/opt/hfs*/bin/hython"), reverse=True)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and os.path.exists(c):
            seen.add(c)
            out.append(c)
    return out


LAST_VFX_ERROR = ""


def _vfx_err(msg: str):
    """Append one line to the importer error log (shown in the preview pane when every route fails)."""
    global LAST_VFX_ERROR
    LAST_VFX_ERROR = (LAST_VFX_ERROR + "\n" + msg).strip()


def load_via_vfx_python(path: str, include_vendor: bool = False) -> Optional[Tuple[list, list, str]]:
    """Export mesh data through any interpreter that can import alembic / pxr. Returns (verts, faces, interpreter)."""
    global LAST_VFX_ERROR
    errs = []
    for py in _vfx_pythons(include_vendor):
        if BLENDER_VERBOSE:
            print(f"  trying {py} …", flush=True)
        try:
            rc, out_lines, err_lines = _run_streaming([py, "-c", _MESH_SCRIPT, path], timeout=120, verbose=BLENDER_VERBOSE)
        except OSError as ex:
            errs.append(f"{py}: {ex}")
            continue
        r = subprocess.CompletedProcess([py], rc, "\n".join(out_lines), "\n".join(err_lines))
        if r.returncode != 0 or "v " not in r.stdout:
            tail = (r.stderr.strip().splitlines() or [f"exit {r.returncode}, no mesh output"])[-1]
            errs.append(f"{os.path.basename(py)}: {tail}")
            continue
        verts, faces = [], []
        for line in r.stdout.splitlines():
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = [int(x) - 1 for x in line.split()[1:]]
                for k in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[k], idx[k + 1]))
        return verts, faces, os.path.basename(py)
    for e_ in errs[-3:]:
        _vfx_err(e_)
    return None


def preview_tool_report() -> str:
    """Environment report for Preferences → Dependencies → Diagnostics. Deliberately does NOT repeat the
    installed/missing status or install commands — the checklist above it shows those. It reports what the
    checklist can't: versions, which binary was picked when several exist, the OpenGL context outcome, which
    Python modules are importable in which interpreter, and which GStreamer decoders are present."""
    L = []
    cpus = os.cpu_count() or 0
    try:
        ui = sorted(os.sched_getaffinity(threading.get_native_id()))
        aff = f"interface thread on CPU {ui[0]}–{ui[-1]}" if len(ui) < cpus else "interface thread not pinned"
    except Exception:
        aff = "affinity unavailable"
    L.append("Runtime")
    L.append(f"  python     {sys.version.split()[0]}  ({sys.executable})")
    L.append(f"  numpy      {np.__version__ if np is not None else '— (pure-Python fallbacks in use)'}")
    L.append(f"  GTK        {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}   GtkSourceView {'yes' if GtkSource is not None else 'no'}")
    L.append(f"  session    {os.environ.get('XDG_SESSION_TYPE', '?')}, GDK_BACKEND={os.environ.get('GDK_BACKEND', 'auto')}   CPUs {cpus}, {aff}")
    L.append("")
    L.append("OpenGL")
    if HAVE_GL:
        try:
            import OpenGL  # type: ignore
            L.append(f"  PyOpenGL   {OpenGL.__version__}")
        except Exception:
            L.append("  PyOpenGL   importable")
        L.append("  context    " + (GL_STATUS[0] if GL_STATUS else "not created yet"))
    else:
        L.append("  PyOpenGL   not importable in this interpreter")
    L.append("")
    L.append("3D importers")
    bl = _find_blender()
    L.append(f"  blender    {bl[0] if bl else '—'}")
    L.append(f"  assimp     {shutil.which('assimp') or '—'}")
    usd = shutil.which("usdcat")
    L.append(f"  USD        {usd or ('pxr (Python module)' if _has_py_module('pxr') else '—')}")
    L.append(f"  7-Zip      {sevenzip() or '—'}")
    L.append("")
    L.append("Image / video / document converters")
    im = shutil.which("magick") or shutil.which("convert")
    L.append(f"  ImageMagick {im + ('  (IM7)' if im and im.endswith('magick') else '  (IM6)') if im else '—'}")
    for t in ("oiiotool", "imgcvt", "ffmpeg", "ffprobe", "pdftoppm"):
        L.append(f"  {t:<11} {shutil.which(t) or '—'}")
    L.append("")
    L.append("GStreamer")
    if HAVE_GST:
        try:
            L.append(f"  version    {Gst.version_string()}")
        except Exception:
            pass
        have = [n for n in ("gtksink",) + H264_DECODERS + ("avdec_h265", "vp8dec", "vp9dec", "av1dec", "dav1ddec", "avdec_prores", "theoradec", "jpegdec")
                if Gst.ElementFactory.find(n)]
        L.append("  elements   " + (", ".join(have) or "none of the ones Kestrel looks for"))
        dec, why = gst_h264_status()
        L.append("  H.264      " + (dec if dec else "NONE — " + why))
        if dec and dec.startswith("nv") and not Gst.ElementFactory.find("avdec_h264"):
            L.append("  note       decoding on the NVIDIA GPU; libav has no avdec_h264 because EL9's ffmpeg-free omits H.264 — that's expected")
        try:
            reg = Gst.Registry.get()
            for name in ("libav", "openh264", "ugly", "good", "bad", "gtk", "nvcodec", "va", "vaapi"):
                pl = reg.find_plugin(name)
                if pl is not None:
                    L.append(f"  plugin     {name:<9} {pl.get_version():<10} {len(reg.get_feature_list_by_plugin(name))} elements   {pl.get_filename()}")
        except Exception:
            pass
    else:
        L.append(f"  not importable: {GST_ERROR or 'unknown error'}")
        L.append("  needs the Gst-1.0 typelib (package gstreamer1) visible to this Python's gi")
    L.append("")
    L.append("Python modules (numpy: fast paths · h5py: HDF5 Alembic · pxr: USD · OpenGL: GPU preview)")
    seen, interps = set(), []
    for py in _vfx_pythons():
        rp = os.path.realpath(py)
        if rp not in seen:                       # /usr/bin/python and python3 are usually the same binary
            seen.add(rp)
            interps.append(py)
    for py in interps:
        mods = []
        for mod in ("numpy", "h5py", "pxr", "OpenGL"):
            try:
                r = subprocess.run([py, "-c", f"import {mod}; print(getattr({mod}, '__version__', 'ok'))"], capture_output=True, text=True, timeout=60)
                mods.append(f"{mod} {r.stdout.strip()[:12]}" if r.returncode == 0 else f"{mod} —")
            except (OSError, subprocess.TimeoutExpired):
                mods.append(f"{mod} ?")
        L.append(f"  {py}\n      " + "   ".join(mods))
    return "\n".join(L)


# --------------------------------------------------------------------------- open-source converters: Blender, assimp
_BLENDER_SCRIPT = r"""
import bpy, sys, traceback
try:
    argv = sys.argv[sys.argv.index("--") + 1:]
    path = argv[0]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = path.lower().rsplit(".", 1)[-1]
    def first(*names):
        for n in names:
            op = bpy.ops
            try:
                for part in n.split("."):
                    op = getattr(op, part)
                return op
            except AttributeError:
                continue
        return None
    if ext == "abc":
        res = bpy.ops.wm.alembic_import(filepath=path, as_background_job=False)
    elif ext in ("usd", "usda", "usdc", "usdz"):
        res = bpy.ops.wm.usd_import(filepath=path)
    elif ext == "fbx":
        res = bpy.ops.import_scene.fbx(filepath=path)
    elif ext in ("gltf", "glb"):
        res = bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "dae":
        res = bpy.ops.wm.collada_import(filepath=path)
    elif ext == "blend":
        res = bpy.ops.wm.open_mainfile(filepath=path)
    elif ext == "stl":
        res = (first("wm.stl_import") or bpy.ops.import_mesh.stl)(filepath=path)
    elif ext == "ply":
        res = (first("wm.ply_import") or bpy.ops.import_mesh.ply)(filepath=path)
    elif ext == "obj":
        res = (first("wm.obj_import") or bpy.ops.import_scene.obj)(filepath=path)
    elif ext == "x3d":
        res = bpy.ops.import_scene.x3d(filepath=path)
    else:
        res = "unsupported"
    print("KESTREL import:", res)
    dg = bpy.context.evaluated_depsgraph_get()
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    print("KESTREL meshes:", len(meshes), "objects:", len(bpy.data.objects))
    out = []
    base = 0
    for ob in meshes:
        try:
            me = ob.evaluated_get(dg).to_mesh()
        except Exception:
            me = ob.data
        mw = ob.matrix_world
        for v in me.vertices:
            co = mw @ v.co
            out.append("v %r %r %r" % (co.x, co.y, co.z))
        for pg in me.polygons:
            out.append("f " + " ".join(str(i + base + 1) for i in pg.vertices))
        base += len(me.vertices)
    sys.stdout.write("KESTREL-BEGIN\n" + "\n".join(out) + "\nKESTREL-END\n")
    sys.stdout.flush()
except Exception:
    traceback.print_exc(file=sys.stdout)
"""

BLENDER_EXTS = (".abc", ".usd", ".usda", ".usdc", ".usdz", ".fbx", ".gltf", ".glb", ".dae", ".blend", ".x3d", ".stl", ".ply", ".obj")
ASSIMP_EXTS = (".fbx", ".dae", ".gltf", ".glb", ".3ds", ".x3d", ".lwo", ".ms3d", ".md5mesh", ".ase", ".ifc", ".off")


_BLENDER_CACHE: Optional[Tuple[str, List[str]]] = None


def _find_blender() -> Optional[Tuple[str, List[str]]]:
    """Locate Blender → (description, argv prefix). Checks PATH, /opt-style unpacked builds, Flatpak and snap.
    A Flatpak Blender runs in its own sandbox with a private /tmp, so it is launched through `flatpak run`
    with --filesystem grants added per call (see load_via_blender)."""
    global _BLENDER_CACHE
    if _BLENDER_CACHE is not None:
        return _BLENDER_CACHE
    import glob
    found = None
    p = shutil.which("blender")
    if p:
        found = (p, [p])
    if not found:
        for c in sorted(glob.glob("/opt/blender*/blender") + glob.glob("/usr/local/blender*/blender") +
                        glob.glob(os.path.expanduser("~/blender*/blender")) + glob.glob(os.path.expanduser("~/Applications/blender*/blender")), reverse=True):
            if os.access(c, os.X_OK):
                found = (c, [c])
                break
    if not found and shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "info", "org.blender.Blender"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                found = ("flatpak org.blender.Blender", ["flatpak", "run", "org.blender.Blender"])
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not found and os.path.exists("/snap/bin/blender"):
        found = ("/snap/bin/blender", ["/snap/bin/blender"])
    _BLENDER_CACHE = found
    return found


BLENDER_VERBOSE = False      # --probe-model sets this: echo Blender's chatter live


def _run_streaming(argv: List[str], timeout: float, verbose: bool = False) -> Tuple[int, List[str], List[str]]:
    """Run argv in its own process group, collecting stdout lines; on timeout kill the WHOLE group.
    subprocess.run(timeout=) only kills the direct child — with `flatpak run` that leaves bwrap/Blender alive
    holding the pipe, and the caller blocks forever on EOF. Returns (rc, stdout_lines, stderr_lines)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                            start_new_session=True, stdin=subprocess.DEVNULL)
    out, err = [], []
    t0 = time.time()

    def pump(stream, sink, echo):
        for line in stream:
            sink.append(line.rstrip("\n"))
            if echo and verbose and not line.startswith(("v ", "f ")):
                print(f"  [{time.time() - t0:5.1f}s] {line.rstrip()}", flush=True)
    th_o = threading.Thread(target=pump, args=(proc.stdout, out, True), daemon=True)
    th_e = threading.Thread(target=pump, args=(proc.stderr, err, True), daemon=True)
    th_o.start()
    th_e.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        rc = -9
        err.append(f"timed out after {timeout:.0f}s (process group killed)")
    th_o.join(timeout=5)
    th_e.join(timeout=5)
    return rc, out, err


def load_via_blender(path: str) -> Optional[Tuple[list, list, str]]:
    """Headless Blender import → vertices/faces (applies modifiers, world transforms).
    The export script is passed with --python-expr and the mesh comes back on stdout, so nothing depends on a
    shared filesystem — important for the Flatpak build, whose sandbox has a private /tmp."""
    bl = _find_blender()
    if not bl:
        _vfx_err("Blender: not found (sudo dnf install blender, or flatpak install flathub org.blender.Blender)")
        return None
    argv = list(bl[1])
    path = os.path.abspath(path)
    if argv[0] == "flatpak":
        argv[2:2] = [f"--filesystem={os.path.dirname(path)}:ro"]      # the model's folder, whatever the app's defaults are
    argv += ["-b", "--factory-startup", "-noaudio", "--python-exit-code", "3", "--python-expr", _BLENDER_SCRIPT, "--", path]
    if BLENDER_VERBOSE:
        print("  running:", " ".join(shlex.quote(a) if a is not _BLENDER_SCRIPT else "<export script>" for a in argv), flush=True)
    try:
        rc, out, err = _run_streaming(argv, timeout=90, verbose=BLENDER_VERBOSE)
    except OSError as ex:
        _vfx_err(f"Blender: {ex}")
        return None
    if "KESTREL-BEGIN" in out and "KESTREL-END" in out:
        body = out[out.index("KESTREL-BEGIN") + 1:out.index("KESTREL-END")]
        verts, faces = _parse_obj_lines(body)
        if verts:
            return verts, faces, "Blender"
    interesting = [l.strip() for l in out + err
                   if l.strip() and not l.startswith(("v ", "f ")) and ("KESTREL" in l or "rror" in l or "Traceback" in l or "arning" in l or "annot" in l or "timed out" in l)]
    _vfx_err("Blender: " + (" | ".join(x[:160] for x in interesting[-4:]) or f"exit {rc}, no geometry; tail: " + " | ".join((out + err)[-3:])))
    return None


def load_via_assimp(path: str) -> Optional[Tuple[list, list, str]]:
    """assimp CLI export → OBJ text → vertices/faces."""
    global LAST_VFX_ERROR
    a = shutil.which("assimp")
    if not a:
        _vfx_err("assimp: not found (sudo dnf install assimp)")
        return None
    out = os.path.join(tempfile.gettempdir(), f"kestrel-assimp-{os.getpid()}-{abs(hash(path)) & 0xffffff:x}.obj")
    try:
        r = subprocess.run([a, "export", path, out, "-ptv", "-tri"], capture_output=True, text=True, timeout=120)
        if os.path.exists(out):
            verts, faces = _parse_obj_text(out)
            if verts:
                return verts, faces, "assimp"
        _vfx_err(f"assimp: {(r.stderr.strip().splitlines() or ['no output'])[-1][:200]}")
    except (OSError, subprocess.TimeoutExpired) as ex:
        _vfx_err(f"assimp: {ex}")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return None


def _parse_obj_text(path: str) -> Tuple[list, list]:
    with open(path, "rb") as f:
        return _parse_obj_lines(f)


def _parse_obj_lines(lines) -> Tuple[list, list]:
    """Minimal OBJ reader for converter output: 'v x y z' and 'f a b c …' lines (str or bytes); faces fan-triangulated."""
    verts, faces = [], []
    for line in lines:
        if isinstance(line, str):
            line = line.encode()
        if line.startswith(b"v "):
            p = line.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith(b"f "):
            idx = []
            for tok in line.split()[1:]:
                s = tok.split(b"/")[0]
                if s:
                    i = int(s)
                    idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):
                faces.append((idx[0], idx[k], idx[k + 1]))
    return verts, faces


GL_STATUS: List[str] = []       # last OpenGL viewer status line (set by GLModelView), for the diagnostics report


# --------------------------------------------------------------------------- optional dependencies
def _has_py_module(name: str) -> bool:
    try:
        r = subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def optional_deps() -> List[dict]:
    """Everything Kestrel can use but doesn't require, with a check and the Rocky 9 install command.
    Each entry: {key, name, gives, found: str|None, cmd: str}. The commands are plain shell so the user sees
    exactly what runs (Preferences → Optional opens them in a terminal)."""
    bl = _find_blender()
    flatpak = shutil.which("flatpak")
    deps = [
        dict(key="blender", name="Blender", gives="Alembic, USD, FBX, glTF, Collada, .blend model previews",
             found=bl[0] if bl else None,
             cmd=("flatpak install -y flathub org.blender.Blender" if flatpak else
                  "sudo dnf install -y flatpak && flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo && "
                  "flatpak install -y flathub org.blender.Blender")),
        dict(key="assimp", name="assimp", gives="FBX, DAE, glTF, 3DS, X3D model previews",
             found=shutil.which("assimp"), cmd="sudo dnf install -y assimp"),
        dict(key="oiiotool", name="OpenImageIO tools", gives="EXR (all codecs), DPX, TIFF, 16-bit IFF, .tx previews",
             found=shutil.which("oiiotool"), cmd="sudo dnf install -y OpenImageIO-utils"),
        dict(key="magick", name="ImageMagick", gives="DDS, XCF, TGA, SVG and other image formats",
             found=shutil.which("magick") or shutil.which("convert"), cmd="sudo dnf install -y ImageMagick"),
        dict(key="ffmpeg", name="ffmpeg (free build)", gives="video frame thumbnails and media info",
             found=shutil.which("ffmpeg"), cmd="sudo dnf install -y ffmpeg-free"),
        dict(key="pdftoppm", name="poppler-utils", gives="PDF first-page previews",
             found=shutil.which("pdftoppm"), cmd="sudo dnf install -y poppler-utils"),
        dict(key="gtksourceview", name="GtkSourceView", gives="syntax colouring + line numbers in the quick editor",
             found="loaded" if GtkSource is not None else None, cmd="sudo dnf install -y gtksourceview4"),
        dict(key="pyopengl", name="PyOpenGL (Python)", gives="hardware-accelerated 3D preview",
             found="OpenGL importable" if HAVE_GL else None, cmd="python3 -m pip install --user PyOpenGL"),
        dict(key="h5py", name="h5py (Python)", gives="HDF5 (legacy) Alembic previews",
             found="h5py importable" if _has_py_module("h5py") else None, cmd="python3 -m pip install --user h5py"),
        dict(key="usd", name="usd-core (Python)", gives="USD previews",
             found="pxr importable" if _has_py_module("pxr") else None, cmd="python3 -m pip install --user usd-core"),
        dict(key="gst", name="GStreamer (Python bindings)", gives="video / audio playback engine" + (f" — import failed: {GST_ERROR}" if GST_ERROR else ""),
             found=(Gst.version_string() if HAVE_GST else None),
             cmd="sudo dnf install -y gstreamer1 gstreamer1-plugins-base python3-gobject"),
        dict(key="gstgtk", name="GStreamer GTK sink", gives="video player in the preview pane",
             found="gtksink" if gst_sink_available(refresh=True) else None, cmd="sudo dnf install -y gstreamer1-plugins-good gstreamer1-plugins-good-gtk"),
        dict(key="gstcodecs", name="GStreamer codecs (RPM Fusion)",
             gives="H.264 / H.265 / ProRes / AAC playback" + (f" — {gst_h264_status()[1]}" if HAVE_GST and not gst_h264_status()[0] else ""),
             found=gst_h264_status()[0],
             cmd="sudo dnf install -y https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-9.noarch.rpm && sudo dnf install -y gstreamer1-plugins-ugly gstreamer1-libav"),
        dict(key="rv", name="OpenRV", gives="Open in RV (right-click, Ctrl+Shift+R)",
             found=find_rv(load_config().get("rv_path", "")), cmd="echo 'Download OpenRV from https://github.com/AcademySoftwareFoundation/OpenRV/releases and unpack it under /opt or ~/, then set the path in Preferences → Optional'"),
        dict(key="7z", name="7-Zip (p7zip)", gives=".7z, .rar, .cab, .iso archives: preview, extract and create",
             found=sevenzip(), cmd="sudo dnf install -y p7zip p7zip-plugins"),
        dict(key="xdotool", name="xdotool", gives="fm-bench.py benchmark script only",
             found=shutil.which("xdotool"), cmd="sudo dnf install -y xdotool"),
    ]
    return deps


def install_script(deps: List[dict]) -> str:
    """Shell script that installs the given deps one after another, continuing past failures and summarising."""
    lines = ["#!/bin/bash", "# Kestrel optional dependencies — review before running", "set -u",
             "if ! rpm -q epel-release >/dev/null 2>&1; then echo '== enabling EPEL (needed for most of these)'; sudo dnf install -y epel-release; fi",
             "fail=()"]
    for d in deps:
        lines += [f"echo; echo '== {d['name']}: {d['gives']}'", f"if ! ( {d['cmd']} ); then fail+=('{d['name']}'); fi"]
    lines += ["echo", "if [ ${#fail[@]} -eq 0 ]; then echo 'All done. Restart Kestrel to pick up the new tools.';",
              "else echo \"Failed: ${fail[*]}\"; fi"]
    return "\n".join(lines) + "\n"


def describe_alembic(path: str) -> Optional[str]:
    """Hierarchy listing via abctree / abcls when PyAlembic isn't around."""
    for tool in ("abctree", "abcls"):
        t = shutil.which(tool)
        if t:
            try:
                r = subprocess.run([t, path], capture_output=True, text=True, timeout=15)
                if r.stdout.strip():
                    return r.stdout[:20000]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


# --------------------------------------------------------------------------- icon / thumbnail cache
class IconCache:
    def __init__(self):
        self.cache: Dict[Tuple[str, int], GdkPixbuf.Pixbuf] = {}
        self.thumbs: Dict[Tuple[str, int], Optional[GdkPixbuf.Pixbuf]] = {}
        self.pending: set = set()
        self.theme = Gtk.IconTheme.get_default()
        self.lock = threading.Lock()
        self.queue: List[Tuple[str, int, Callable]] = []
        self.worker: Optional[threading.Thread] = None

    def pixbuf(self, icon: Optional[Gio.Icon], size: int) -> Optional[GdkPixbuf.Pixbuf]:
        key = (icon.to_string() if icon else "text-x-generic", size)
        pb = self.cache.get(key)
        if pb is not None:
            return pb
        try:
            info = self.theme.lookup_by_gicon(icon, size, Gtk.IconLookupFlags.FORCE_SIZE) if icon else None
            if info is None:
                info = self.theme.lookup_icon("text-x-generic", size, Gtk.IconLookupFlags.FORCE_SIZE)
            pb = info.load_icon() if info else None
        except GLib.Error:
            pb = None
        if pb is None:
            try:
                pb = self.theme.load_icon("text-x-generic", size, Gtk.IconLookupFlags.FORCE_SIZE)
            except GLib.Error:
                pb = None
        self.cache[key] = pb
        return pb

    def desaturated(self, pb: Optional[GdkPixbuf.Pixbuf]) -> Optional[GdkPixbuf.Pixbuf]:
        """Greyed copy of a pixbuf (cached) — used for the inactive split pane."""
        if pb is None:
            return None
        key = ("desat", id(pb))
        hit = self.cache.get(key)
        if hit is None:
            hit = pb.copy()
            pb.saturate_and_pixelate(hit, 0.12, False)
            self.cache[key] = hit
        return hit

    def thumbnail(self, e: Entry, size: int, on_ready: Callable) -> Optional[GdkPixbuf.Pixbuf]:
        """Return a cached thumbnail, or None and schedule generation."""
        if not e.path or not (e.content_type or "").startswith("image/") or (e.size is not None and e.size < 64):
            return None
        key = (e.path, size)
        if key in self.thumbs:
            return self.thumbs[key]
        if key in self.pending:
            return None
        self.pending.add(key)
        with self.lock:
            self.queue.append((e.path, size, on_ready))
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=lambda: (pin_worker(), self._work()), daemon=True)
                self.worker.start()
        return None

    def _work(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                path, size, cb = self.queue.pop()
            pb = None
            try:
                if os.path.getsize(path) < 40 << 20:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                    pb = pb.apply_embedded_orientation() or pb
            except Exception:
                pb = None
            GLib.idle_add(self._done, path, size, pb, cb)

    def _done(self, path, size, pb, cb):
        self.thumbs[(path, size)] = pb
        self.pending.discard((path, size))
        if pb is not None:
            cb(path)
        return False


ICONS = IconCache()


def cursor_path(view):
    """Cursor TreePath of a TreeView or IconView (IconView.get_cursor returns (ok, path, cell))."""
    r = view.get_cursor()
    if isinstance(view, Gtk.IconView):
        return r[1] if r and r[0] else None
    return r[0] if r else None


def touch_all(store):
    """Ask every row to redraw (cell data funcs re-run)."""
    store.foreach(lambda m, p, it, d: (m.row_changed(p, it), False)[1], None)

LAYOUT_ICON_SIZE = {"xlarge": 160, "large": 96, "medium": 48, "small": 24, "list": 16, "tiles": 48, "details": 16, "content": 32}

URI_TARGETS = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]


def decide_drop(paths: List[str], dest: str, widget: Gtk.Widget) -> Optional[str]:
    """Explorer rules: drop into the folder the items already live in = nothing;
    Ctrl = copy, Shift = move; otherwise move on the same disk, copy across disks."""
    paths = [p for p in paths if p]
    if not paths or not dest:
        return None
    if all(os.path.dirname(p.rstrip("/")) == dest for p in paths):
        return None
    mask = 0
    try:
        win = widget.get_window()
        dev = Gdk.Display.get_default().get_default_seat().get_pointer()
        if win and dev:
            mask = win.get_device_position(dev)[3]
    except Exception:
        mask = 0
    if mask & Gdk.ModifierType.CONTROL_MASK:
        return "copy"
    if mask & Gdk.ModifierType.SHIFT_MASK:
        return "move"
    try:
        same_disk = all(os.stat(p).st_dev == os.stat(dest).st_dev for p in paths)
    except OSError:
        same_disk = False
    return "move" if same_disk else "copy"


# --------------------------------------------------------------------------- navigation pane nodes
class NavNode:
    def __init__(self, label: str, path: Optional[str], icon: Gio.Icon, expandable: bool = True, kind: str = "folder"):
        self.label, self.path, self.icon, self.expandable, self.kind = label, path, icon, expandable, kind
        self.loaded = False


# --------------------------------------------------------------------------- a tab (one folder view)
class FolderView(Gtk.Box):
    def __init__(self, win: "MainWindow", path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.cfg = win.cfg
        self.path = path
        self.back: List[str] = []
        self.fwd: List[str] = []
        self.entries: List[Entry] = []
        self.search: Optional[SearchOp] = None
        self.search_text = ""
        self.error = ""
        self.monitor: Optional[Gio.FileMonitor] = None
        self.reload_pending = 0
        self.gen = 0
        self.remember: Dict[str, str] = {}
        self.loading = False          # chunked fill in progress
        self.indexed_paint = False    # the view currently shows the index's copy, not yet confirmed by the disk
        self.busy = False             # a listing is running in the worker thread (drives the status spinner)
        self.partial = False
        self.layout = win.cfg["layout"]      # per-pane; the config value is the default for new panes
        self.muted = False                    # inactive pane of a split: desaturated icons, dim text
        self.page: Optional["Page"] = None
        # slim header shown only in split view
        self.header = Gtk.Box(spacing=4)
        self.header.get_style_context().add_class("pane-header")
        self.header_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, margin_start=8)
        self.header.pack_start(self.header_label, True, True, 0)
        hclose = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        hclose.set_relief(Gtk.ReliefStyle.NONE)
        hclose.set_tooltip_text("Close this pane")
        hclose.connect("clicked", lambda b: self.page.unsplit(self) if self.page else None)
        self.header.pack_end(hclose, False, False, 0)
        hev = Gtk.EventBox()
        hev.add(self.header)
        hev.connect("button-press-event", lambda w, e: self.view.grab_focus() or False)
        self.header_box = hev
        self.pack_start(hev, False, False, 0)
        self.header_box.set_no_show_all(True)

        # Entry-or-label, is_group, icon-view pixbuf, icon-view markup.  Columns 2/3 are bound directly to the
        # icon view's renderers so GTK lays out 50k tiles without calling back into Python per tile.
        self.store = Gtk.ListStore(GObject.TYPE_PYOBJECT, bool, GdkPixbuf.Pixbuf, str)
        self._index: Optional[Dict[str, int]] = None
        self.stack = Gtk.Stack()
        self.pack_start(self.stack, True, True, 0)
        self.err_label = Gtk.Label(xalign=0.5)
        self.err_label.get_style_context().add_class("dim-label")
        self.stack.add_named(self.err_label, "error")
        self._build_tree()
        self._build_iconview()
        self.apply_layout()

    # ---- details view
    def _build_tree(self):
        tv = Gtk.TreeView(model=self.store, enable_search=True, rubber_banding=True, headers_clickable=True)
        tv.set_search_equal_func(self._search_equal, None)
        tv.set_fixed_height_mode(True)      # O(1) row layout: 100k-row folders scroll like 100
        enable_soft_grid(tv, columns=True)              # faint rules, crossings fade out
        enable_row_hover(tv)
        self.tree = tv
        sel = tv.get_selection()
        sel.set_mode(Gtk.SelectionMode.MULTIPLE)
        sel.set_select_function(lambda s, m, p, cur, d: not m[p][1], None)
        sel.connect("changed", lambda s: self.win.on_selection_changed(self))
        tv.connect("row-activated", lambda t, p, c: self.win.activate_entry(self.entry_at(p)))
        tv.connect("button-press-event", self._on_button, "tree")
        tv.connect("key-press-event", self.win.on_view_key, self)
        tv.connect("popup-menu", lambda w: self.win.context_menu(self, None) or True)
        tv.connect("focus-in-event", lambda w, e: self.win.set_active_pane(self) or False)
        tv.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.ASK)
        tv.connect("drag-data-get", self._drag_get)
        tv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tv.connect("drag-data-received", self._drag_received, "tree")
        self.name_renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        self.name_renderer.connect("edited", self._on_edited)
        self.name_renderer.connect("editing-canceled", lambda r: self._end_edit())
        self.name_renderer.connect("editing-started", self._on_editing_started)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        self.stack.add_named(sw, "details")
        self.rebuild_columns()

    def rebuild_columns(self):
        tv = self.tree
        for c in tv.get_columns():
            tv.remove_column(c)
        cols = list(self.cfg["columns"])
        if "name" not in cols:
            cols.insert(0, "name")
        # Size stays small and sits right after Name so it never gets pushed off-screen by wide columns
        if "size" in cols:
            cols.remove("size")
            cols.insert(1, "size")
        if self.search or self.path in (TRASH, QUICK, RECENT):
            if "location" not in cols:
                cols.append("location")
        if self.path == TRASH:
            cols.insert(2, "deleted")
        if self.path == THISPC:
            cols = ["name", "usage", "type"]
        if self.path == NETWORK:
            cols = ["name", "location", "usage", "type"]
        if self.cfg["checkboxes"]:
            tog = Gtk.CellRendererToggle()
            tog.connect("toggled", self._on_toggled)
            c = Gtk.TreeViewColumn("", tog)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_fixed_width(28)
            c.set_cell_data_func(tog, self._chk_data)
            tv.append_column(c)
        for key in cols:
            col = Gtk.TreeViewColumn(COLUMN_TITLES.get(key, "Space used" if key == "usage" else key))
            col.set_resizable(True)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width({"name": 260, "location": 260, "modified": 150, "type": 170, "size": 90, "usage": 170, "deleted": 150}.get(key, 120))
            col.key = key
            if key == "name":
                pix = Gtk.CellRendererPixbuf()
                col.pack_start(pix, False)
                col.set_cell_data_func(pix, self._icon_data)
                col.pack_start(self.name_renderer, True)
                col.set_cell_data_func(self.name_renderer, self._name_data)
                col.set_expand(True)
                col.set_min_width(180)
            elif key == "usage":
                prog = Gtk.CellRendererProgress()
                col.pack_start(prog, True)
                col.set_cell_data_func(prog, self._usage_data)
                col.set_min_width(160)
            else:
                r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
                if key == "size":
                    r.set_property("xalign", 1.0)
                col.pack_start(r, True)
                col.set_cell_data_func(r, self._text_data, key)
                col.set_min_width({"modified": 150, "type": 160, "size": 90}.get(key, 120))
                if key == "location":
                    col.set_fixed_width(260)
            if self.path != THISPC:
                col.set_clickable(True)
                col.connect("clicked", lambda c: self.win.sort_by(c.key))
                if key == self.cfg["sort_key"]:
                    col.set_sort_indicator(True)
                    col.set_sort_order(Gtk.SortType.DESCENDING if self.cfg["sort_desc"] else Gtk.SortType.ASCENDING)
            tv.append_column(col)

    def _search_equal(self, model, column, key, it, data):
        v = model[it][0]
        return not (isinstance(v, Entry) and v.name.lower().startswith(key.lower()))

    # cell data funcs
    def _icon_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("pixbuf", None)
            return
        size = 32 if self.layout == "content" else 16
        pb = ICONS.pixbuf(v.icon, size)
        cell.set_property("pixbuf", ICONS.desaturated(pb) if self.muted else pb)

    def _name_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("markup", f"<span foreground='#1a5fb4'><b>{GLib.markup_escape_text(str(v))}</b></span>")
            cell.set_property("foreground-set", False)
            return
        name = self.win.display_name(v)
        if self.layout == "content":
            sub = v.type + ("" if v.is_dir else f"  ·  {fmt_size(v.size, self.cfg['size_in_bytes'])}") + (f"  ·  {v.location}" if v.location else "")
            cell.set_property("markup", f"{GLib.markup_escape_text(name)}\n<small><span foreground='#777'>{GLib.markup_escape_text(sub)}</span></small>")
        else:
            cell.set_property("markup", GLib.markup_escape_text(name))
        cut = self.win.clipboard["mode"] == "cut" and v.path in self.win.clipboard["paths"]
        dim = cut or v.hidden or self.muted
        cell.set_property("foreground", "#999999" if dim else None)
        cell.set_property("foreground-set", bool(dim))

    def _text_data(self, col, cell, model, it, key):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("text", "")
            return
        cell.set_property("text", self.win.cell_text(v, key))
        cell.set_property("foreground", "#999999" if self.muted else None)
        cell.set_property("foreground-set", self.muted)

    def _usage_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if isinstance(v, Entry) and v.usage:
            used, total = v.usage
            pct = int(100 * used / total) if total else 0
            cell.set_property("value", pct)
            cell.set_property("text", f"{pct}% used")
            cell.set_property("visible", True)
        else:
            cell.set_property("visible", False)

    def _chk_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        cell.set_property("visible", isinstance(v, Entry))
        cell.set_property("active", isinstance(v, Entry) and self.tree.get_selection().iter_is_selected(it))

    def _on_toggled(self, cell, path):
        sel = self.tree.get_selection()
        p = Gtk.TreePath.new_from_string(path)
        if sel.path_is_selected(p):
            sel.unselect_path(p)
        else:
            sel.select_path(p)

    # ---- icon view
    def _build_iconview(self):
        iv = IconGrid(self.store)
        self.icons = iv
        iv.provider = self._iv_tile
        iv.connect("item-activated", lambda v, p: self.win.activate_entry(self.entry_at(p)))
        iv.connect("selection-changed", lambda v: self.win.on_selection_changed(self))
        iv.connect("button-press-event", self._on_button, "icons")
        iv.connect("key-press-event", self.win.on_view_key, self)
        iv.connect("popup-menu", lambda w: self.win.context_menu(self, None) or True)
        iv.connect("focus-in-event", lambda w, e: self.win.set_active_pane(self) or False)
        iv.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.ASK)
        iv.connect("drag-data-get", self._drag_get)
        iv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        iv.connect("drag-data-received", self._drag_received, "icons")
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(iv)
        self.stack.add_named(sw, "icons")
        # This PC tile view (Windows-style sections); used for every layout except Details
        self.pcview = PCView()
        self.pcview.connect("selection-changed", lambda v: self.win.on_selection_changed(self))
        self.pcview.connect("item-activated", lambda v, e: self.win.activate_entry(e))
        self.pcview.connect("context-menu", lambda v, e, ev: self.win.context_menu(self, ev))
        self.stack.add_named(self.pcview, "pc")

    # ---- per-row icon-view data (computed once at fill time, see _rows_for)
    def _iv_tile(self, e: Entry):
        """Called by the grid the first time a tile scrolls into view (and cached there)."""
        size = LAYOUT_ICON_SIZE.get(self.layout, 48)
        pb = None
        if size >= 48 and len(self.entries) <= 2500:
            pb = ICONS.thumbnail(e, size, self._thumb_ready)
        return (pb or ICONS.pixbuf(e.icon, size), self._iv_markup(e))

    def _iv_markup(self, e: Entry) -> str:
        name = GLib.markup_escape_text(self.win.display_name(e))
        cut = self.win.clipboard["mode"] == "cut" and e.path in self.win.clipboard["paths"]
        if cut or e.hidden:
            name = f"<span foreground='#999999'>{name}</span>"
        if self.layout == "tiles":
            sub = e.type + ("" if e.is_dir else f"\n{fmt_size(e.size, self.cfg['size_in_bytes'])}")
            return f"{name}\n<small><span foreground='#777'>{GLib.markup_escape_text(sub)}</span></small>"
        return name

    def _row_index(self) -> Dict[str, int]:
        if self._index is None:
            idx = {}
            for i, row in enumerate(self.store):
                e = row[0]
                if isinstance(e, Entry) and e.path:
                    idx[e.path] = i
            self._index = idx
        return self._index

    def _thumb_ready(self, path):
        i = self._row_index().get(path)
        if i is None:
            return
        try:
            it = self.store.get_iter(Gtk.TreePath.new_from_indices([i]))
            e = self.store[it][0]
            if isinstance(e, Entry) and e.path == path:
                self.store.row_changed(self.store.get_path(it), it)
        except (ValueError, TypeError):
            pass

    def refresh_rows(self, paths):
        """Recompute the icon-view label for specific rows (cut-state changed)."""
        for p in paths:
            i = self._row_index().get(p)
            if i is None:
                continue
            try:
                it = self.store.get_iter(Gtk.TreePath.new_from_indices([i]))
            except ValueError:
                continue
            e = self.store[it][0]
            if isinstance(e, Entry):
                self.store.row_changed(self.store.get_path(it), it)

    @property
    def pc_mode(self) -> bool:
        return self.path == THISPC and self.layout != "details"

    def apply_layout(self):
        lay = self.layout
        if self.pc_mode:
            self.stack.set_visible_child_name("pc")
            if self.entries:
                self.pcview.set_entries(self.entries, self.cfg["single_click"])
            return
        if self.path == THISPC or lay in ("details", "content"):
            self.stack.set_visible_child_name("details")
            self.tree.set_activate_on_single_click(self.cfg["single_click"])
            self.rebuild_columns()
        else:
            self.stack.set_visible_child_name("icons")
            size = LAYOUT_ICON_SIZE.get(lay, 48)
            if lay in ("list", "small"):
                self.icons.configure(size, True, 220, self.cfg["single_click"])
            elif lay == "tiles":
                self.icons.configure(size, True, 260, self.cfg["single_click"])
            else:
                self.icons.configure(size, False, size + 44, self.cfg["single_click"])
            self.icons.invalidate()
        self.tree.queue_draw()      # visible rows re-run their data funcs; no need to signal every row

    @property
    def view(self):
        return self.icons if self.stack.get_visible_child_name() == "icons" else self.tree

    # ---- selection helpers
    def entry_at(self, path) -> Optional[Entry]:
        try:
            v = self.store[path][0]
            return v if isinstance(v, Entry) else None
        except Exception:
            return None

    def selected_paths(self) -> List[Gtk.TreePath]:
        if self.pc_mode:
            return []
        if self.view is self.tree:
            return self.tree.get_selection().get_selected_rows()[1]
        return self.icons.get_selected_items()

    def selected_entries(self) -> List[Entry]:
        if self.pc_mode:
            return self.pcview.selected_entries()
        out = []
        for p in self.selected_paths():
            e = self.entry_at(p)
            if e:
                out.append(e)
        out.sort(key=lambda e: e.name.lower())
        return out

    def current_entry(self) -> Optional[Entry]:
        sel = self.selected_entries()
        if sel:
            return sel[0]
        p = cursor_path(self.view)
        return self.entry_at(p) if p is not None else None

    def select_all(self):
        if self.pc_mode:
            self.pcview.select_all()
            return
        if self.view is self.tree:
            self.tree.get_selection().select_all()
        else:
            self.icons.select_all()

    def select_none(self):
        if self.pc_mode:
            self.pcview.select_none()
            return
        if self.view is self.tree:
            self.tree.get_selection().unselect_all()
        else:
            self.icons.unselect_all()

    def invert_selection(self):
        if self.view is self.tree:
            sel = self.tree.get_selection()
            for row in self.store:
                if isinstance(row[0], Entry):
                    (sel.unselect_path if sel.path_is_selected(row.path) else sel.select_path)(row.path)
        else:
            cur = set(str(p) for p in self.icons.get_selected_items())
            for row in self.store:
                if isinstance(row[0], Entry):
                    (self.icons.unselect_path if str(row.path) in cur else self.icons.select_path)(row.path)

    def select_names(self, names: List[str], scroll: bool = True):
        if self.pc_mode:
            self.pcview.select_names(names)
            return
        self.select_none()
        first = None
        for row in self.store:
            v = row[0]
            if isinstance(v, Entry) and v.name in names:
                if self.view is self.tree:
                    self.tree.get_selection().select_path(row.path)
                else:
                    self.icons.select_path(row.path)
                first = first or row.path
        if first is not None and scroll:
            if self.view is self.tree:
                self.tree.set_cursor(first, None, False)
                self.tree.scroll_to_cell(first, None, False, 0, 0)
            else:
                self.icons.set_cursor(first, None, False)
                self.icons.scroll_to_path(first, False, 0, 0)

    # ---- mouse
    def _on_button(self, view, event, kind):
        if event.button == 3:
            if kind == "tree":
                hit = view.get_path_at_pos(int(event.x), int(event.y))
                path = hit[0] if hit else None
                sel = view.get_selection()
                if path is not None and not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
                    view.set_cursor(path, None, False)
                elif path is None:
                    sel.unselect_all()
            else:
                path = view.get_path_at_pos(int(event.x), int(event.y))
                if path is not None and not view.path_is_selected(path):
                    view.unselect_all()
                    view.select_path(path)
                elif path is None:
                    view.unselect_all()
            self.win.context_menu(self, event)
            return True
        if event.button == 2:
            path = (view.get_path_at_pos(int(event.x), int(event.y)) or [None])[0] if kind == "tree" else view.get_path_at_pos(int(event.x), int(event.y))
            e = self.entry_at(path) if path is not None else None
            if e and e.is_dir:
                self.win.new_tab(e.path)
                return True
        if event.button in (8, 9):
            (self.win.go_back if event.button == 8 else self.win.go_forward)()
            return True
        return False

    # ---- drag & drop
    def _drag_get(self, widget, ctx, sel, info, t):
        uris = [e.gfile.get_uri() for e in self.selected_entries()]
        sel.set_uris(uris)

    def _drag_received(self, widget, ctx, x, y, sel, info, t, kind):
        uris = sel.get_uris()
        if not uris:
            Gtk.drag_finish(ctx, False, False, t)
            return
        dest = self.path if self.path not in VIRTUAL else None
        if kind == "tree":
            hit = widget.get_dest_row_at_pos(x, y)
            if hit:
                e = self.entry_at(hit[0])
                if e and e.is_dir:
                    dest = e.path
        else:
            hit = widget.get_dest_item_at_pos(x, y)
            if hit:
                e = self.entry_at(hit[0])
                if e and e.is_dir:
                    dest = e.path
        paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        kind = decide_drop(paths, dest, widget)
        ok = bool(kind) and self.win.transfer(paths, dest, kind)
        Gtk.drag_finish(ctx, ok, False, t)

    # ---- rename
    def start_rename(self):
        if len(self.selected_entries()) > 1:
            self.win.batch_rename()
            return
        if self.view is not self.tree:
            e = self.current_entry()
            if e:
                new = ask_text(self.win, "Rename", "New name:", e.name, select_stem=not e.is_dir)
                if new and new != e.name:
                    self.win.do_rename(e.path, os.path.join(os.path.dirname(e.path), new))
            return
        p = cursor_path(self.tree)
        if p is None:
            return
        col = next((c for c in self.tree.get_columns() if getattr(c, "key", "") == "name"), None)
        if col is None:
            return
        self.name_renderer.set_property("editable", True)
        self.tree.set_cursor(p, col, True)

    def _on_editing_started(self, renderer, editable, path):
        self.win.editing = True
        e = self.entry_at(Gtk.TreePath.new_from_string(path))
        if isinstance(editable, Gtk.Entry) and e and not e.is_dir:
            stem = os.path.splitext(e.name)[0]
            GLib.idle_add(lambda: editable.select_region(0, len(stem)) or False)

    def _end_edit(self):
        self.name_renderer.set_property("editable", False)
        self.win.editing = False

    def _on_edited(self, renderer, path, new_text):
        self._end_edit()
        e = self.entry_at(Gtk.TreePath.new_from_string(path))
        if e and new_text and new_text != e.name:
            self.win.do_rename(e.path, os.path.join(os.path.dirname(e.path), new_text))

    # ---- loading
    def title(self) -> str:
        if self.search:
            return f"Search Results in {self.win.display_path(self.path)}"
        return self.win.display_path(self.path)

    def short_title(self) -> str:
        """Tab label: parent + own folder, e.g. ~/Documents/Autodesk/ or …/parent/name/"""
        p = self.path
        if p in VIRTUAL:
            return self.win.display_path(p)
        if self.search:
            return "Search: " + self.search_text
        if p == HOME:
            return "~/"
        if p.startswith(HOME + "/"):
            rel = "~/" + p[len(HOME) + 1:]
        else:
            rel = pretty_path(p)
        parts = [x for x in rel.split("/") if x]
        if rel.startswith("/"):
            parts.insert(0, "")
        if len(parts) > 3:
            return "…/" + "/".join(parts[-2:]) + "/"
        return "/".join(parts) + "/" if parts != [""] else "/"

    def load(self, keep_selection: bool = True):
        self.gen += 1
        gen = self.gen
        old = [e.name for e in self.selected_entries()] if keep_selection else []
        cfg = self.cfg
        path = self.path
        if self.search:
            return
        self.err_label.set_text("")
        self.busy = True
        self.win.on_tab_busy(self)
        if path in (THISPC, NETWORK):
            try:
                ents = {THISPC: self.win.pc_entries, NETWORK: self.win.net_entries}[path]()
                self._fill(gen, ents, "", old)
            except Exception as ex:
                self._fill(gen, [], str(ex), old)
            self._watch()
            return
        if path in (QUICK, RECENT):
            fn = self.win.quick_entries if path == QUICK else self.win.recent_entries

            def vwork():
                try:
                    ents, err = fn(), ""
                except Exception as ex:
                    ents, err = [], str(ex)
                GLib.idle_add(self._fill, gen, ents, err, old)
            threading.Thread(target=vwork, daemon=True).start()
            self._watch()
            return

        if cfg.get("index_enabled") and INDEX.loaded:
            rows = INDEX.lookup(path)
            if rows:
                # instant first paint from the index (sorted like the real listing); the real one replaces it
                ents0 = sort_entries(index_entries_for(path, rows), cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
                if not cfg["show_hidden"]:
                    ents0 = [e for e in ents0 if not e.hidden]
                self._fill_partial(gen, ents0[:3000], len(ents0))
                self.indexed_paint = True

        def partial(snapshot):
            """Early/intermediate view of a big folder while the rest is still being read."""
            if gen != self.gen:
                return
            part = sort_entries(snapshot, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])[:3000]
            GLib.idle_add(self._fill_partial, gen, part, len(snapshot))

        def work():
            try:
                ents = list_folder(path, cfg["show_hidden"], partial)
                err = ""
            except GLib.Error as ex:
                ents, err = [], ("You don't have permission to access this folder." if ex.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED) else ex.message)
            except Exception as ex:
                ents, err = [], str(ex)
            if path not in (THISPC, QUICK):
                ents = sort_entries(ents, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
            if cfg.get("index_enabled") and INDEX.loaded and not err and (cfg["show_hidden"] or cfg.get("index_hidden")) is not None:
                try:
                    if any(path == r or path.startswith(r.rstrip("/") + "/") for r in cfg.get("index_roots", [])) and (cfg.get("index_network") or not is_network_path(path)):
                        INDEX.update_dir(path, [e for e in ents if cfg.get("index_hidden") or not e.hidden])
                except Exception:
                    pass
            GLib.idle_add(self._fill, gen, ents, err, old)
        threading.Thread(target=work, daemon=True).start()
        self._watch()

    CHUNK = 4000          # rows appended per main-loop iteration (~15 ms)

    def _fill_partial(self, gen, ents, read_so_far=0):
        """Show the first few thousand entries read so far (cheap); the final _fill replaces it."""
        if gen != self.gen or self.search:
            return False
        self.partial = True
        self.partial_count = read_so_far
        self.entries = ents
        rows = self._rows_for(ents)
        view = self.view
        view.set_model(None)
        self.store.clear()
        app = self.store.append
        for r in rows:
            app(r)
        view.set_model(self.store)
        self.win.on_tab_loaded(self)
        return False

    def _rows_for(self, ents: List[Entry]) -> List[list]:
        group = self.cfg["group_by"]
        details = self.stack.get_visible_child_name() == "details" or self.path == THISPC
        rows = []
        self._index = None
        if details:
            # details/content views use cell-data funcs (only visible rows are rendered); columns 2/3 unused
            if self.path in (THISPC, QUICK, NETWORK, RECENT):
                last = None
                for e in ents:
                    if e.section != last:
                        rows.append([e.section, True, None, ""])
                        last = e.section
                    rows.append([e, False, None, ""])
            elif group != "none":
                last = None
                for e in ents:
                    g = group_label(e, group)
                    if g != last:
                        rows.append([g, True, None, ""])
                        last = g
                    rows.append([e, False, None, ""])
            else:
                rows = [[e, False, None, ""] for e in ents]
        else:
            # icon/tile/list: tiles are rendered lazily by IconGrid, so rows carry no pixbuf/markup up front
            rows = [[e, False, None, None] for e in ents]
        self._index = None      # built lazily by _row_index() the first time a thumbnail/refresh needs it
        return rows

    def _fill(self, gen, ents, err, old_names):
        """Runs on the main loop. Small folders are filled at once; big ones in chunks so the UI never stalls."""
        if gen != self.gen:
            return False
        self.busy = False
        self.indexed_paint = False
        for rec in reversed(NAV_LOG):
            if rec["path"] == self.path and rec["listed"] is None:
                rec["listed"] = time.perf_counter()
                rec["n"] = len(ents)
                rec["err"] = err
                break
        self.partial = False
        self.entries = ents
        self.error = err
        rows = self._rows_for(ents)
        self.store.clear()
        if self.view.get_model() is None:
            self.view.set_model(self.store)
        if err:
            self.err_label.set_text(err)
            self.stack.set_visible_child_name("error")
        elif self.stack.get_visible_child_name() == "error":
            self.apply_layout()
        view = self.view
        if len(rows) <= self.CHUNK:
            view.set_model(None)
            app = self.store.append
            for r in rows:
                app(r)
            view.set_model(self.store)
            self._fill_done(old_names)
        else:
            # TreeView (fixed-height) copes with live inserts; IconView re-lays-out every tile per insert,
            # so for it we fill detached and attach once at the end. Either way the main loop stays free.
            self.loading = True
            detached = view is self.icons
            if detached:
                view.set_model(None)
            self._fill_chunk(gen, rows, 0, old_names, detached)
        return False

    def _fill_chunk(self, gen, rows, i, old_names, detached):
        if gen != self.gen:
            if detached:
                self.view.set_model(self.store)
            return False
        app = self.store.append
        for r in rows[i:i + self.CHUNK]:
            app(r)
        i += self.CHUNK
        if i < len(rows):
            self.win.on_tab_loaded(self)     # keep the "N items" count moving
            GLib.idle_add(self._fill_chunk, gen, rows, i, old_names, detached, priority=GLib.PRIORITY_DEFAULT_IDLE)
        else:
            if detached:
                self.view.set_model(self.store)
            self.loading = False
            self._fill_done(old_names)
        return False

    def _fill_done(self, old_names):
        if self.pc_mode:
            self.pcview.set_entries(self.entries, self.cfg["single_click"])
            self.stack.set_visible_child_name("pc")
        for rec in reversed(NAV_LOG):
            if rec["path"] == self.path and rec["shown"] is None:
                rec["shown"] = time.perf_counter()
                rec["stalls_during"] = len(WATCHDOG.records) - rec["stalls_before"]
                break
        if old_names:
            self.select_names(old_names, scroll=False)
        self.win.on_tab_loaded(self)

    def resort(self):
        """Re-sort what is already loaded (no disk access); sorting happens off the main thread."""
        if self.search or self.path in (THISPC, QUICK) or not self.entries:
            self.load()
            return
        self.gen += 1
        gen = self.gen
        ents, cfg = list(self.entries), self.cfg
        old = [e.name for e in self.selected_entries()]

        def work():
            res = sort_entries(ents, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
            GLib.idle_add(self._fill, gen, res, "", old)
        threading.Thread(target=work, daemon=True).start()

    def _watch(self):
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
        if self.path in VIRTUAL and self.path != TRASH:
            return
        try:
            f = Gio.File.new_for_uri(self.path) if "://" in self.path else Gio.File.new_for_path(self.path)
            self.monitor = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect("changed", self._on_changed)
        except GLib.Error:
            self.monitor = None

    def _on_changed(self, mon, f, other, ev):
        if self.reload_pending:
            return
        self.reload_pending = GLib.timeout_add(400, self._reload_now)

    def _reload_now(self):
        self.reload_pending = 0
        if not self.search:
            self.load()
        return False

    # ---- search
    def start_search(self, text: str):
        self.stop_search()
        self.search_text = text
        root = self.path if self.path not in VIRTUAL else HOME
        self.entries = []
        self.store.clear()
        self._index = None
        self.search = SearchOp(root, text, self.cfg["search_subfolders"], self.cfg["search_contents"], self.cfg["show_hidden"], self._search_batch)
        self.rebuild_columns()
        self._search_seen = set()
        if self.cfg.get("index_enabled") and INDEX.loaded and self.cfg["search_subfolders"]:
            hits = INDEX.search(text, root)
            if hits:
                batch = []
                for p, is_dir, size, mtime in hits:
                    if not self.cfg["show_hidden"] and os.path.basename(p).startswith("."):
                        continue
                    e = Entry()
                    e.name, e.path, e.is_dir = os.path.basename(p), p, bool(is_dir)
                    e.size = None if is_dir else int(size)
                    e.mtime = float(mtime)
                    e.location = os.path.dirname(p)
                    if is_dir:
                        e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
                    else:
                        e.content_type, e.type, e.icon = _ct_info(e.name)
                    batch.append(e)
                self._search_batch(batch, False)             # instant results from the index
        self.search.start()
        self.win.on_tab_loaded(self)

    def _search_batch(self, batch: List[Entry], done: bool):
        if not self.search:
            return False
        seen = getattr(self, "_search_seen", set())
        for e in batch:
            if e.path in seen:
                continue                      # already shown from the index
            seen.add(e.path)
            self.entries.append(e)
            self.store.append([e, False, None, None])
        self._index = None
        self.win.on_tab_loaded(self)
        if done:
            GLib.timeout_add(250, lambda: (self.win._update_status(), False)[1])     # thread has exited by then → wheel stops
        return False

    def stop_search(self):
        if self.search:
            self.search.cancel.set()
            self.search = None
            self.search_text = ""
            self.rebuild_columns()


# --------------------------------------------------------------------------- a notebook page: one or two panes
class Page(Gtk.Box):
    def __init__(self, win: "MainWindow", path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(False)
        self.pack_start(self.paned, True, True, 0)
        self.panes: List[FolderView] = []
        self.active: Optional[FolderView] = None
        self.label = Gtk.Label(label="", width_chars=14, max_width_chars=30, ellipsize=Pango.EllipsizeMode.MIDDLE, xalign=0)
        self.add_pane(path)
        self.active = self.panes[0]

    def add_pane(self, path: str) -> FolderView:
        fv = FolderView(self.win, path)
        fv.page = self
        self.panes.append(fv)
        if len(self.panes) == 1:
            self.paned.pack1(fv, True, False)
        else:
            self.paned.pack2(fv, True, False)
        fv.show_all()
        self._update_headers()
        if path not in VIRTUAL:
            self.win._bump_frequent(path)
        fv.load()
        return fv

    @property
    def split(self) -> bool:
        return len(self.panes) > 1

    def do_split(self, path: Optional[str] = None):
        if self.split:
            return self.panes[1]
        base = self.active or self.panes[0]
        p = path or (base.path if base.path not in VIRTUAL else HOME)
        fv = self.add_pane(p)
        GLib.idle_add(lambda: (self.paned.set_position(self.paned.get_allocated_width() // 2), fv.view.grab_focus()) and False)
        return fv

    def unsplit(self, pane: Optional[FolderView] = None):
        if not self.split:
            return
        pane = pane or self.panes[1]
        pane.stop_search()
        if pane.monitor:
            pane.monitor.cancel()
        self.panes.remove(pane)
        self.paned.remove(pane)
        keep = self.panes[0]
        self.paned.remove(keep)
        self.paned.pack1(keep, True, False)
        self.active = keep
        self._update_headers()
        keep.view.grab_focus()
        self.win.set_active_pane(keep)

    def set_active(self, fv: FolderView):
        if fv in self.panes and fv is not self.active:
            self.active = fv
            self._update_headers()

    def _update_headers(self):
        for fv in self.panes:
            fv.header_box.set_visible(self.split)
            fv.header.set_visible(self.split)
            fv.header_label.set_visible(self.split)
            ctx = fv.header.get_style_context()
            sctx = fv.stack.get_style_context()
            muted = self.split and fv is not self.active
            if not muted:
                ctx.add_class("active")
                sctx.remove_class("inactive-pane")
            else:
                ctx.remove_class("active")
                sctx.add_class("inactive-pane")
            if fv.muted != muted:
                fv.muted = muted
                fv.icons.muted = muted
                fv.icons.queue_draw()
                fv.tree.queue_draw()
            fv.header_label.set_text(fv.path if fv.path not in VIRTUAL else self.win.display_path(fv.path))
        self.update_label()

    def update_label(self):
        fv = self.active or self.panes[0]
        self.label.set_text(fv.short_title() if not self.split else " | ".join(p.short_title() for p in self.panes))
        self.label.set_tooltip_text(" | ".join(p.path for p in self.panes))
        for p in self.panes:
            p.header_label.set_text(p.path if p.path not in VIRTUAL else self.win.display_path(p.path))


# --------------------------------------------------------------------------- menu construction helpers
def _menu_item(label: str, cb: Optional[Callable], accel: str = "", check: Optional[bool] = None, radio_group=None,
               sensitive: bool = True, submenu: Optional[Gtk.Menu] = None):
    if label == "-":
        return Gtk.SeparatorMenuItem()
    if radio_group is not None:
        item = Gtk.RadioMenuItem.new_with_mnemonic_from_widget(radio_group[0] if radio_group else None, label)
        radio_group.append(item)
        item.set_active(bool(check))
    elif check is not None:
        item = Gtk.CheckMenuItem.new_with_mnemonic(label)
        item.set_active(check)
    else:
        item = Gtk.MenuItem.new_with_mnemonic(label)
    if accel:
        child = item.get_child()
        if isinstance(child, Gtk.AccelLabel):
            key, mods = Gtk.accelerator_parse(accel)
            child.set_accel(key, mods)
    if submenu is not None:
        item.set_submenu(submenu)
    if cb is not None:
        item.connect("activate", lambda it: cb() if not isinstance(it, (Gtk.CheckMenuItem,)) or it.get_active() or not isinstance(it, Gtk.RadioMenuItem) else None)
    item.set_sensitive(sensitive)
    return item


def build_menu(spec) -> Gtk.Menu:
    """spec: list of dicts/tuples -> Gtk.Menu."""
    menu = Gtk.Menu()
    for s in spec:
        if s == "-":
            menu.append(Gtk.SeparatorMenuItem())
            continue
        if isinstance(s, tuple):
            s = {"label": s[0], "cb": s[1] if len(s) > 1 else None, "accel": s[2] if len(s) > 2 else ""}
        sub = build_menu(s["sub"]) if "sub" in s else None
        menu.append(_menu_item(s["label"], s.get("cb"), s.get("accel", ""), s.get("check"), s.get("radio"),
                               s.get("sensitive", True), sub))
    menu.show_all()
    return menu


def _short_drive_label(label: str) -> str:
    """'KIOXIA_2TB (/mnt/KIOXIA_2TB)' -> 'KIOXIA_2TB'; the full path is shown as a tooltip instead."""
    if label.endswith(")") and " (" in label:
        return label[:label.rindex(" (")]
    return label


# --------------------------------------------------------------------------- main window
class MainWindow(Gtk.ApplicationWindow):
    MENU_KEYS = {"file": "File", "home": "Home", "share": "Share", "view": "View", "tools": "Tools", "help": "Help"}

    def __init__(self, app: "ExplorerApp", path: Optional[str]):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.cfg = app.cfg
        self.clipboard: Dict[str, object] = {"mode": "", "paths": []}
        self.undo_stack: List[Tuple[str, object]] = []
        self.editing = False
        self.status_timer = 0
        self.set_default_size(*self.cfg["window"])
        self.set_icon_name("system-file-manager")
        self.accel = Gtk.AccelGroup()
        self.add_accel_group(self.accel)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)
        self.menubar = Gtk.MenuBar()
        vbox.pack_start(self.menubar, False, False, 0)
        self._build_menubar()
        vbox.pack_start(self._build_toolbar(), False, False, 0)
        vbox.pack_start(self._build_commandbar(), False, False, 0)
        self.notebook = Gtk.Notebook(scrollable=True)
        self.notebook.set_show_border(False)
        self.notebook.set_group_name("kestrel-tabs")
        self.notebook.connect("switch-page", self._on_switch_page)
        self.notebook.connect("page-removed", self._on_page_removed)
        self.notebook.connect("page-added", self._on_page_added)
        self.notebook.connect("create-window", self._on_create_window)
        newtab = Gtk.Button.new_from_icon_name("tab-new-symbolic", Gtk.IconSize.MENU)
        newtab.set_relief(Gtk.ReliefStyle.NONE)
        newtab.set_tooltip_text("New tab (Ctrl+T)")
        newtab.connect("clicked", lambda b: self.new_tab(self.tab.path if self.tab.path not in VIRTUAL else HOME))
        newtab.show()
        self.notebook.set_action_widget(newtab, Gtk.PackType.END)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(self.paned, True, True, 0)
        self.nav_box = self._build_nav()
        self.paned.pack1(self.nav_box, False, False)
        self.right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.right.pack1(self.notebook, True, False)
        self.side = self._build_side()
        self.right.pack2(self.side, False, False)
        self.paned.pack2(self.right, True, False)
        self.paned.set_position(self.cfg["nav_width"])
        self._watch_volumes()
        # The side pane (preview + quick view) is freely resizable, like the nav pane; both splitters are
        # remembered: side_width is the pane's width, side_split the preview/quick-view divider.
        self._side_restored = False
        self.right.connect("size-allocate", self._restore_side_width)
        self.right.connect("notify::position", lambda p, _: self._remember_side_width())
        self.side_paned.connect("notify::position", lambda p, _: self._remember_side_split())

        self.status = Gtk.Label(xalign=0, margin_start=8, margin_end=8, margin_top=3, margin_bottom=3, ellipsize=Pango.EllipsizeMode.END)
        sb = Gtk.Box()
        sb.get_style_context().add_class("statusbar")
        # bottom-left activity wheel: spins while a folder is being read (network shares, big folders) or
        # a search is running; hidden otherwise so the bar stays quiet
        self.spinner = Gtk.Spinner(margin_start=8, no_show_all=True)
        self.spinner.set_size_request(14, 14)
        sb.pack_start(self.spinner, False, False, 0)
        sb.pack_start(self.status, True, True, 0)
        for lay, icon, tip in (("details", "view-list-symbolic", "Details"), ("large", "view-grid-symbolic", "Large icons")):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda b, l=lay: self.set_layout(l))
            sb.pack_end(b, False, False, 0)
        vbox.pack_start(sb, False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_button)
        self.connect("delete-event", self._on_close)

        self.show_all()
        bench("window")
        self._bench_listed = False
        if path != "":   # "" = empty window that will receive a dragged tab
            start = path or {"quick": QUICK, "pc": THISPC, "home": HOME}.get(self.cfg["open_to"], HOME)
            self.new_tab(start)
        self._apply_panes()

    # ------------------------------------------------------------------ toolbar
    def _build_toolbar(self):
        tb = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=4, margin_bottom=2)
        tb.get_style_context().add_class("toolbar-row")
        self.btn_back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.btn_fwd = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        self.btn_up = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        for b, cb, tip in ((self.btn_back, self.go_back, "Back (Alt+Left)"), (self.btn_fwd, self.go_forward, "Forward (Alt+Right)"),
                           (self.btn_up, self.go_up, "Up (Alt+Up)")):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda w, c=cb: c())
            tb.pack_start(b, False, False, 0)
        self.btn_back.connect("button-press-event", self._history_popup)
        # path bar: breadcrumbs or entry
        self.path_stack = Gtk.Stack()
        self.crumbs = Gtk.Box()
        self.crumbs.get_style_context().add_class("linked")
        crumb_frame = Gtk.EventBox()
        crumb_frame.add(self.crumbs)
        crumb_frame.connect("button-press-event", lambda w, e: self.edit_address() if e.button == 1 else None)
        self.path_stack.add_named(crumb_frame, "crumbs")
        self.addr = Gtk.Entry()
        self.addr.connect("activate", self._on_address)
        self.addr.connect("focus-out-event", lambda w, e: self.path_stack.set_visible_child_name("crumbs"))
        self.addr.connect("key-press-event", self._on_address_key)
        self.path_stack.add_named(self.addr, "entry")
        frame = Gtk.Frame()
        frame.get_style_context().add_class("pathbar")
        frame.add(self.path_stack)
        tb.pack_start(frame, True, True, 0)
        self.searchbox = Gtk.SearchEntry(width_chars=22)
        self.searchbox.set_placeholder_text("Search")
        self.searchbox.connect("activate", lambda e: self.start_search(e.get_text()))
        self.searchbox.connect("stop-search", lambda e: self.clear_search())
        tb.pack_start(self.searchbox, False, False, 0)
        return tb

    def _history_popup(self, btn, event):
        if event.button == 3:
            t = self.tab
            if not t.back:
                return True
            m = Gtk.Menu()
            for i, p in enumerate(reversed(t.back[-15:])):
                it = Gtk.MenuItem(label=self.display_path(p))
                it.connect("activate", lambda it, n=i + 1: self.go_back(n))
                m.append(it)
            m.attach_to_widget(btn, None)
            m.show_all()
            m.popup_at_pointer(event)
            return True
        return False

    def _refresh_crumbs(self):
        t = self.tab
        if t is None:
            return
        for c in self.crumbs.get_children():
            self.crumbs.remove(c)
        for label, p in self._crumb_list(t.path):
            b = Gtk.Button(label=label)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.connect("clicked", lambda w, pp=p: self.navigate(pp))
            b.connect("button-press-event", lambda w, e, pp=p: (self.new_tab(pp) or True) if e.button == 2 else False)
            self.crumbs.pack_start(b, False, False, 0)
            arrow = Gtk.Label(label="›")
            arrow.get_style_context().add_class("dim-label")
            self.crumbs.pack_start(arrow, False, False, 2)
        self.crumbs.show_all()
        self.searchbox.set_placeholder_text(f"Search {self.display_path(t.path)}")
        if not t.search:
            self.searchbox.set_text("")

    def _crumb_list(self, path: str) -> List[Tuple[str, str]]:
        if path in VIRTUAL:
            return [(self.display_path(path), path)]
        out = [("This PC", THISPC)]
        dr = demo_drive_root(path)
        if dr is not None:
            out.append(dr)
            rest, acc = path[len(dr[1]):].strip("/"), dr[1]
        elif path == HOME or path.startswith(HOME + "/"):
            out.append((os.path.basename(HOME), HOME))
            rest, acc = path[len(HOME):].strip("/"), HOME
        else:
            out.append(("/", "/"))
            rest, acc = path.strip("/"), "/"
        if rest:
            for part in rest.split("/"):
                acc = os.path.join(acc, part)
                out.append((part, acc))
        return out

    def edit_address(self):
        t = self.tab
        self.addr.set_text("" if t.path in VIRTUAL else pretty_path(t.path))
        self.path_stack.set_visible_child_name("entry")
        self.addr.grab_focus()
        self.addr.select_region(0, -1)

    def _on_address_key(self, entry, event):
        if event.keyval == Gdk.KEY_Escape:
            self.path_stack.set_visible_child_name("crumbs")
            self.tab.view.grab_focus()
            return True
        if event.keyval == Gdk.KEY_Tab:
            v = os.path.expanduser(entry.get_text())
            d, b = os.path.split(v)
            try:
                cands = sorted(n for n in os.listdir(d or "/") if n.startswith(b) and os.path.isdir(os.path.join(d or "/", n)))
            except OSError:
                cands = []
            if cands:
                pre = os.path.commonprefix(cands)
                entry.set_text(os.path.join(d, pre) + ("/" if len(cands) == 1 else ""))
                entry.set_position(-1)
            return True
        return False

    def _on_address(self, entry):
        v = entry.get_text().strip()
        self.path_stack.set_visible_child_name("crumbs")
        if not v:
            return
        low = v.lower()
        if low in ("this pc", "pc", "computer"):
            self.navigate(THISPC)
        elif low in ("recycle bin", "trash", "trash:///", "trash://"):
            self.navigate(TRASH)
        elif low in ("quick access", "quick"):
            self.navigate(QUICK)
        elif low in ("network", "net://", "network:///"):
            self.navigate(NETWORK)
        elif "://" in v and not v.startswith("file://"):
            self.flash(f"Connecting to {v}…")
            mount_uri(v, self, lambda p, err: message(self, "Connect", err, Gtk.MessageType.ERROR) if err else self.navigate(p if p and os.path.isdir(p) else NETWORK))
        else:
            p = os.path.abspath(os.path.expanduser(os.path.expandvars(v)))
            if os.path.isdir(p):
                self.navigate(p)
            elif os.path.isfile(p):
                self.navigate(os.path.dirname(p))
                GLib.timeout_add(300, lambda: self.tab.select_names([os.path.basename(p)]) or False)
            else:
                message(self, APP_NAME, f"Can't find '{v}'. Check the spelling and try again.", Gtk.MessageType.ERROR)
        self.tab.view.grab_focus()

    # ------------------------------------------------------------------ command bar (Windows 11 style)
    def _cmd_button(self, icon: str, label: str, cb: Callable, tip: str = "", menu_cb: Optional[Callable] = None):
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.get_style_context().add_class("cmd")
        box = Gtk.Box(spacing=6)
        box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        if label:
            box.pack_start(Gtk.Label(label=label), False, False, 0)
        if menu_cb:
            box.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.MENU), False, False, 0)
        b.add(box)
        b.set_tooltip_text(tip or label)
        b.set_focus_on_click(False)
        if menu_cb:
            b.connect("clicked", lambda w: self._popup_under(w, build_menu(menu_cb())))
        else:
            b.connect("clicked", lambda w: cb())
        return b

    def _popup_under(self, widget, menu: Gtk.Menu):
        menu.attach_to_widget(widget, None)
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _build_commandbar(self):
        bar = Gtk.Box(spacing=2)
        bar.get_style_context().add_class("commandbar")
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        bar.set_margin_bottom(4)
        sep = lambda: Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
        bar.pack_start(self._cmd_button("list-add-symbolic", "New", None, "New folder or item", lambda: self._new_item_spec_with_folder()), False, False, 0)
        bar.pack_start(sep(), False, False, 0)
        for icon, lab, cb, tip in (("edit-cut-symbolic", "", lambda: self.copy_sel("cut"), "Cut (Ctrl+X)"),
                                   ("edit-copy-symbolic", "", lambda: self.copy_sel("copy"), "Copy (Ctrl+C)"),
                                   ("edit-paste-symbolic", "", lambda: self.paste(), "Paste (Ctrl+V)"),
                                   ("document-edit-symbolic", "", lambda: self.tab.start_rename(), "Rename (F2)"),
                                   ("user-trash-symbolic", "", lambda: self.delete(), "Delete (Del)")):
            bar.pack_start(self._cmd_button(icon, lab, cb, tip), False, False, 0)
        bar.pack_start(self._cmd_button("emblem-shared-symbolic", "", None, "Share", lambda: self.share_spec()), False, False, 0)
        bar.pack_start(sep(), False, False, 0)
        bar.pack_start(self._cmd_button("view-sort-descending-symbolic", "Sort", None, "Sort by", lambda: self.menu_spec("view")[6]["sub"]), False, False, 0)
        bar.pack_start(self._cmd_button("view-grid-symbolic", "View", None, "Layout", lambda: self.menu_spec("view")[5]["sub"] + ["-"] + self.menu_spec("view")[11:14]), False, False, 0)
        bar.pack_start(self._cmd_button("view-dual-symbolic", "Split", self.toggle_split, "Show two folders side by side (Ctrl+Shift+S)"), False, False, 0)
        bar.pack_start(self._cmd_button("view-more-symbolic", "", None, "More", lambda: self.menu_spec("home")), False, False, 0)
        bar.pack_end(self._cmd_button("sidebar-show-right-symbolic", "", lambda: self.toggle_cfg("details_pane"), "Details pane (Alt+Shift+P)"), False, False, 0)
        bar.pack_end(self._cmd_button("image-x-generic-symbolic", "", lambda: self.toggle_cfg("preview_pane"), "Preview pane (Alt+P)"), False, False, 0)
        return bar

    def _new_item_spec_with_folder(self):
        return [("_Folder", self.new_folder, "<Control><Shift>n"), "-"] + self._new_item_spec()[1:]

    # ------------------------------------------------------------------ navigation pane
    def _build_nav(self):
        self.nav_store = Gtk.TreeStore(GObject.TYPE_PYOBJECT)
        tv = Gtk.TreeView(model=self.nav_store, headers_visible=False, activate_on_single_click=True)
        tv.set_enable_tree_lines(False)
        tv.set_level_indentation(6)
        self.nav_tree = tv
        col = Gtk.TreeViewColumn("")
        tv.set_show_expanders(False)          # we draw our own twirly so rows without one can sit flush left
        tv.set_level_indentation(16)
        arrow = Gtk.CellRendererPixbuf()
        arrow.set_fixed_size(16, -1)
        pix = Gtk.CellRendererPixbuf()
        txt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col.pack_start(arrow, False)
        col.pack_start(pix, False)
        col.pack_start(txt, True)
        col.set_cell_data_func(arrow, self._nav_arrow, None)
        col.set_cell_data_func(pix, self._nav_icon, None)
        col.set_cell_data_func(txt, self._nav_text, None)
        enable_row_hover(tv)
        enable_soft_grid(tv, columns=False)
        tv.append_column(col)
        tv.set_tooltip_column(-1)
        tv.set_has_tooltip(True)
        tv.connect("query-tooltip", self._nav_tooltip)
        tv.get_selection().set_select_function(lambda s, m, p, cur, d: bool(m[p][0]) and m[p][0].kind not in ("spacer", "info"), None)
        tv.connect("test-expand-row", self._nav_expand)
        tv.connect("row-expanded", lambda t, it, p: t.queue_draw())
        tv.connect("row-collapsed", lambda t, it, p: t.queue_draw())
        tv.connect("row-activated", self._nav_activate)
        tv.connect("button-press-event", self._nav_button)
        tv.connect("key-press-event", self._nav_key)
        tv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tv.connect("drag-data-received", self._nav_drop)
        self.rebuild_nav()
        sw = Gtk.ScrolledWindow(hexpand=False, vexpand=True)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        sw.get_style_context().add_class("navpane")
        return sw

    def rebuild_nav(self):
        self.nav_store.clear()
        PATHS.on_change = lambda: (self._fill_recent(self.nav_recent), self._fill_quick(self.nav_quick), self.tab is not None and self.tab.path in (QUICK, RECENT) and self.tab.load())
        self.nav_recent = self.nav_store.append(None, [NavNode("Recent", RECENT, Gio.ThemedIcon.new("document-open-recent"), True, "root")])
        self._fill_recent(self.nav_recent)
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        self.nav_quick = self.nav_store.append(None, [NavNode("Quick access", QUICK, Gio.ThemedIcon.new("starred"), True, "root")])
        self._fill_quick(self.nav_quick)
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        pc = self.nav_store.append(None, [NavNode("This PC", THISPC, Gio.ThemedIcon.new("computer"), True, "root")])
        for label, p, icon in xdg_user_dirs():
            it = self.nav_store.append(pc, [NavNode(label, p, Gio.ThemedIcon.new(icon))])
            self.nav_store.append(it, [None])
        for label, p, icon, k in drives():
            if k == "network":
                continue
            it = self.nav_store.append(pc, [NavNode(_short_drive_label(label), p, icon, True, "removable" if k == "removable" else "drive")])
            self.nav_store.append(it, [None])
        for label, vol, vicon in volumes_unmounted():
            n = NavNode(f"{label}  (not mounted)", None, vicon, False, "volume")
            n.volume = vol
            self.nav_store.append(pc, [n])
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        net = self.nav_store.append(None, [NavNode("Network", NETWORK, Gio.ThemedIcon.new("network-workgroup"), True, "root")])
        nets = drives("network")
        for label, p, icon, k in nets:
            it = self.nav_store.append(net, [NavNode(_short_drive_label(label), p, icon, True, "network")])
            self.nav_store.append(it, [None])
        if not nets:
            self.nav_store.append(net, [NavNode("No network drives", None, Gio.ThemedIcon.new("network-offline"), False, "info")])
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        self.nav_store.append(None, [NavNode("Recycle Bin", TRASH, Gio.ThemedIcon.new("user-trash"), False, "root")])
        self.nav_tree.expand_row(self.nav_store.get_path(net), False)
        self.nav_tree.expand_row(self.nav_store.get_path(pc), False)
        self.nav_tree.expand_row(self.nav_store.get_path(self.nav_quick), False)

    def _fill_recent(self, parent_iter):
        child = self.nav_store.iter_children(parent_iter)
        while child:
            nxt = self.nav_store.iter_next(child)
            self.nav_store.remove(child)
            child = nxt
        shown = 0
        for p in self.cfg["recent"]:
            if PATHS.exists(p):
                self.nav_store.append(parent_iter, [NavNode(os.path.basename(p), p, Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), False, "file")])
                shown += 1
                if shown >= 12:
                    break
        if not shown:
            self.nav_store.append(parent_iter, [NavNode("No recent files", None, Gio.ThemedIcon.new("document-open-recent-symbolic"), False, "info")])

    def _fill_quick(self, parent_iter):
        child = self.nav_store.iter_children(parent_iter)
        while child:
            nxt = self.nav_store.iter_next(child)
            self.nav_store.remove(child)
            child = nxt
        for p in self.cfg["pinned"]:
            if PATHS.isdir(p):
                it = self.nav_store.append(parent_iter, [NavNode(self.display_path(p), p, Gio.ThemedIcon.new("folder"), True, "pinned")])
                self.nav_store.append(it, [None])
        if self.cfg["show_frequent"]:
            for p in self.frequent_folders()[:6]:
                if p not in self.cfg["pinned"]:
                    it = self.nav_store.append(parent_iter, [NavNode(self.display_path(p), p, Gio.ThemedIcon.new("folder"), True, "frequent")])
                    self.nav_store.append(it, [None])

    def _nav_tooltip(self, tv, x, y, keyboard, tip):
        ok, mx, my, model, path, it = tv.get_tooltip_context(x, y, keyboard)
        if not ok:
            return False
        n = model[it][0]
        if not n or not n.path or n.path in VIRTUAL:
            return False
        text = n.path
        if n.kind in ("drive", "removable"):
            hit = disk_for_mount(n.path)
            if hit:
                d, part = hit
                text += f"\n{part['name']}{(' · ' + part['fstype']) if part['fstype'] else ''} on {d['model']} ({d['tran'] or 'internal'}, {fmt_size(d['size'])})"
        tip.set_text(text)
        tv.set_tooltip_row(tip, path)
        return True

    def _nav_arrow(self, col, cell, model, it, data=None):
        n = model[it][0]
        hover_bg(self.nav_tree, cell, model, it)
        if n is None or n.kind == "spacer" or not n.expandable:
            cell.set_property("visible", False)
            return
        cell.set_property("visible", True)
        cell.set_property("icon-name", "pan-down-symbolic" if self.nav_tree.row_expanded(model.get_path(it)) else "pan-end-symbolic")

    def _nav_icon(self, col, cell, model, it, data=None):
        n = model[it][0]
        if n is not None and n.kind == "spacer":
            cell.set_property("pixbuf", None)
            cell.set_property("cell-background-set", False)
            cell.set_property("height", 8)
            return
        cell.set_property("height", -1)
        hover_bg(self.nav_tree, cell, model, it)
        cell.set_property("xpad", 4 if (n is not None and n.kind == "root" and not n.expandable) else 2)
        cell.set_property("pixbuf", ICONS.pixbuf(n.icon, 16) if n and n.icon else None)

    def _nav_text(self, col, cell, model, it, data=None):
        hover_bg(self.nav_tree, cell, model, it)
        n = model[it][0]
        if n is None or n.kind == "spacer":
            cell.set_property("markup", "")
            cell.set_property("height", 8 if n is not None else -1)
            cell.set_property("cell-background-set", False)
            return
        cell.set_property("height", -1)
        if n.kind == "root":
            cell.set_property("markup", f"<b>{GLib.markup_escape_text(n.label)}</b>")
        elif n.kind == "info":
            cell.set_property("markup", f"<span foreground='#888'>{GLib.markup_escape_text(n.label)}</span>")
        else:
            cell.set_property("markup", GLib.markup_escape_text(n.label))

    def _nav_expand(self, tv, it, path):
        node = self.nav_store[it][0]
        if node is None or node.loaded or not node.path or node.path in VIRTUAL:
            return False
        node.loaded = True
        # Add the "Loading…" row BEFORE removing the placeholder: if the node is ever childless while GTK is
        # deciding whether it can expand, the expansion is dropped and the twirly needs a second click.
        self.nav_store.append(it, [NavNode("Loading…", None, Gio.ThemedIcon.new("content-loading-symbolic"), False, "info")])
        child = self.nav_store.iter_children(it)
        while child:
            nxt = self.nav_store.iter_next(child)
            if self.nav_store[child][0] is None:
                self.nav_store.remove(child)
            child = nxt
        row = Gtk.TreeRowReference.new(self.nav_store, self.nav_store.get_path(it))
        show_hidden, p = self.cfg["show_hidden"], node.path

        def work():
            names = []
            try:
                with os.scandir(p) as sc:
                    names = [(d.name, d.path) for d in sc if d.is_dir() and (show_hidden or not d.name.startswith("."))]
                names.sort(key=lambda t: t[0].lower())
            except OSError:
                pass
            GLib.idle_add(done, names[:1000])

        def done(names):
            if not row.valid():
                return False
            parent = self.nav_store.get_iter(row.get_path())
            for name, path_ in names:
                c = self.nav_store.append(parent, [NavNode(name, path_, Gio.ThemedIcon.new("folder"))])
                self.nav_store.append(c, [None])
            # now drop the "Loading…" row (last, so the branch never becomes empty)
            child = self.nav_store.iter_children(parent)
            while child:
                nxt = self.nav_store.iter_next(child)
                n = self.nav_store[child][0]
                if n is not None and n.kind == "info":
                    if names:
                        self.nav_store.remove(child)
                    else:
                        n.label = "No subfolders"
                        self.nav_store.row_changed(self.nav_store.get_path(child), child)
                child = nxt
            return False
        threading.Thread(target=work, daemon=True).start()
        return False

    def _nav_node_at(self, event):
        hit = self.nav_tree.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return None, None
        return self.nav_store[hit[0]][0], hit[0]

    def _nav_activate(self, tv, path, col):
        node = self.nav_store[path][0]
        if node and node.kind == "volume":
            self.mount_volume(node.volume)
            return
        if node and node.path:
            if node.kind == "file":
                return                     # Recent files: single click previews, double click opens (see _nav_button)
            self.navigate(node.path)       # expanding is only done by the twirly, like Explorer

    def _nav_button(self, tv, event):
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            node, _ = self._nav_node_at(event)
            if node and node.kind == "file" and node.path:
                self.open_file(entry_for_path(node.path))
                return True
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            hit = tv.get_path_at_pos(int(event.x), int(event.y))
            if hit:
                path, col, cx, cy = hit
                node = self.nav_store[path][0]
                if node and node.kind == "file" and node.path:
                    # single click on a Recent file: select it in the nav pane and show it in Preview / Quick view
                    tv.get_selection().select_path(path)
                    if self.tab is not None:
                        self.tab.select_none()
                    self._nav_preview = entry_for_path(node.path)
                    self._update_side()
                    return True
                if node and node.expandable:
                    # click on our twirly (the first 16 px past this row's indentation) toggles expansion
                    indent = (path.get_depth() - 1) * 16
                    if cx < indent + 18:
                        if tv.row_expanded(path):
                            tv.collapse_row(path)
                        else:
                            tv.expand_row(path, False)
                        return True
        if event.button == 3:
            node, path = self._nav_node_at(event)
            if node and (node.path or node.kind == "volume"):
                tv.get_selection().select_path(path)
                self.nav_context_menu(node, path, event)
            return True
        if event.button == 2:
            node, _ = self._nav_node_at(event)
            if node and node.path:
                self.new_tab(node.path)
            return True
        if event.button in (8, 9):
            (self.go_back if event.button == 8 else self.go_forward)()
            return True
        return False

    def _nav_key(self, tv, event):
        if event.keyval == Gdk.KEY_Delete:
            model, it = tv.get_selection().get_selected()
            node = model[it][0] if it else None
            if node and node.kind == "pinned":
                self.unpin(node.path)
            return True
        return False

    def _nav_drop(self, tv, ctx, x, y, sel, info, t):
        uris = sel.get_uris()
        hit = tv.get_dest_row_at_pos(x, y)
        node = self.nav_store[hit[0]][0] if hit else None
        if not uris or not node or not node.path or node.path in VIRTUAL:
            if node and node.path == TRASH and uris:
                paths = [Gio.File.new_for_uri(u).get_path() for u in uris]
                self.run_op("trash", [p for p in paths if p], None)
                Gtk.drag_finish(ctx, True, False, t)
                return
            Gtk.drag_finish(ctx, False, False, t)
            return
        paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        kind = decide_drop(paths, node.path, tv)
        ok = bool(kind) and self.transfer(paths, node.path, kind)
        Gtk.drag_finish(ctx, ok, False, t)

    def nav_context_menu(self, node: NavNode, path, event):
        p = node.path
        if node.kind == "volume":                      # a removable volume that isn't mounted yet
            spec = [("_Mount", lambda: self.mount_volume(node.volume))]
            menu = build_menu(spec)
            menu.attach_to_widget(self.nav_tree, None)
            menu.popup_at_pointer(event)
            return
        pinned = p in self.cfg["pinned"]
        spec = [("_Open", lambda: self.navigate(p)), ("Open in new _tab", lambda: self.new_tab(p)),
                ("Open in new _window", lambda: self.app.new_window(p))]
        if node.kind == "removable":
            spec += ["-", ("_Eject", lambda: self.eject(p)), ("_Format…", lambda: self.format_drive(p, node.label))]
        if p not in VIRTUAL:
            spec += [("Open in _terminal", lambda: self.open_terminal(p)), "-",
                     ("_Unpin from Quick access" if pinned else "_Pin to Quick access", (lambda: self.unpin(p)) if pinned else (lambda: self.pin(p))),
                     "-", ("P_roperties", lambda: self.show_properties([entry_for_path(p)]))]
        elif p == TRASH:
            spec += ["-", ("_Empty Recycle Bin", self.empty_trash)]
        elif p == NETWORK:
            spec += ["-", ("_Connect to server…", self.connect_to_server), ("_Refresh", lambda: (self.rebuild_nav(), self.reload_all()))]
        if node.kind == "network":
            spec += [("_Disconnect (unmount)", lambda: self.unmount(p))]
        menu = build_menu(spec)
        menu.attach_to_widget(self.nav_tree, None)
        menu.popup_at_pointer(event)

    # ------------------------------------------------------------------ network
    def net_entries(self) -> List[Entry]:
        out = []
        drive_entries = []
        for label, p, icon, k in drives("network"):
            e = entry_for_path(p, label)
            e.icon = icon
            e.section = "Network drives"
            e.location = _mount_source(p) or p
            e.type = "…"
            out.append(e)
            drive_entries.append(e)
        t = self.tab
        # free space in a thread (a dead share would hang the window); browse the LAN in a thread too
        def work():
            for e in drive_entries:
                try:
                    u = disk_usage(e.path)
                    e.usage = (u.total - u.free, u.total)
                    e.type = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                except OSError:
                    e.type = "unavailable"
            found = network_browse()
            GLib.idle_add(lambda: self._net_browse_done(t, found) or False)
        threading.Thread(target=work, daemon=True).start()
        return out

    def _net_browse_done(self, t: FolderView, found: List[Entry]):
        if t.path != NETWORK or t.search:
            return
        existing = {e.uri for e in t.entries}
        new = [e for e in found if e.uri not in existing]
        if new:
            t._fill(t.gen, list(t.entries) + new, "", [e.name for e in t.selected_entries()])
        else:
            touch_all(t.store)
            self.on_tab_loaded(t)

    def connect_to_server(self):
        uri = ask_text(self, "Connect to Server", "Server address (e.g. smb://server/share, sftp://user@host/path, ftp://host, dav://host/dav, nfs://server/export):", "smb://")
        if not uri or uri.strip() in ("smb://", ""):
            return
        uri = uri.strip()
        self.flash(f"Connecting to {uri}…")

        def done(path, err):
            if err:
                message(self, "Connect to Server", err, Gtk.MessageType.ERROR)
                return
            self.rebuild_nav()
            if path and os.path.isdir(path):
                self.navigate(path)
            else:
                self.flash(f"Connected: {uri}")
                self.navigate(NETWORK)
        mount_uri(uri, self, done)

    # ------------------------------------------------------------------ removable drives
    def _watch_volumes(self):
        """Live drive detection: udisks/GVfs tells us when something is plugged in, mounted, ejected or pulled.
        Refresh the nav pane and any This PC view — no manual reload needed."""
        vm = Gio.VolumeMonitor.get()
        self._vm = vm                                   # keep a reference: signals die with the object

        def changed(*_):
            invalidate_mounts()
            if getattr(self, "_vm_timer", None):
                GLib.source_remove(self._vm_timer)
            self._vm_timer = GLib.timeout_add(400, self._drives_changed)    # coalesce the burst of signals
        for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added", "volume-removed", "volume-changed", "drive-connected", "drive-disconnected"):
            vm.connect(sig, changed)

    def _drives_changed(self):
        self._vm_timer = None
        self.rebuild_nav()
        for fv in self._tabs():
            if fv.path in (THISPC, NETWORK):
                fv.load()
            elif fv.path not in VIRTUAL and not os.path.isdir(fv.path):
                self.flash(f"{self.display_path(fv.path)} is no longer available")
                self.navigate(THISPC, tab=fv)
        return False

    def eject(self, path: str):
        label = self.display_path(path)
        self.flash(f"Ejecting {label}…")

        def done(err):
            if err:
                message(self, "Eject", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"{label} can now be removed safely")
                for fv in self._tabs():
                    if fv.path == path or fv.path.startswith(path.rstrip("/") + "/"):
                        self.navigate(THISPC, tab=fv)
        eject_or_unmount(path, self, done)

    def mount_volume(self, vol):
        self.flash(f"Mounting {vol.get_name()}…")

        def done(path, err):
            if err:
                message(self, "Mount", err, Gtk.MessageType.ERROR)
            elif path:
                self.navigate(path)
        mount_volume(vol, self, done)

    def format_drive(self, path: str, label: str):
        """Format a removable drive through UDisks2 (polkit prompt, no sudo). Confirms twice: the dialog itself and
        an explicit 'type the label' check for anything bigger than 64 GB, since a wrong target here is fatal."""
        dev = block_device_for(path)
        if not dev:
            message(self, "Format", "Couldn't find the block device for this drive.", Gtk.MessageType.ERROR)
            return
        try:
            total = disk_usage(path).total
        except OSError:
            total = 0
        d = Gtk.Dialog(title=f"Format {label}", transient_for=self, modal=True)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Format", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        box = d.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)
        warn = Gtk.Label(xalign=0, wrap=True)
        warn.set_markup(f"<b>Everything on {GLib.markup_escape_text(label)} ({dev}, {fmt_size(total)}) will be erased.</b>\nThis cannot be undone.")
        box.pack_start(warn, False, False, 0)
        box.pack_start(Gtk.Label(label="File system:", xalign=0), False, False, 0)
        combo = Gtk.ComboBoxText()
        avail = [(k, desc) for k, desc, _pkg in FORMAT_CHOICES if mkfs_available(k)]
        for k, desc in avail:
            combo.append(k, desc)
        if avail:
            combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        missing = [f"{desc.split(' — ')[0]} (dnf install {pkg})" for k, desc, pkg in FORMAT_CHOICES if not mkfs_available(k)]
        if missing:
            ml = Gtk.Label(xalign=0, wrap=True)
            ml.set_markup("<small>Not available until installed: " + GLib.markup_escape_text(", ".join(missing)) + "</small>")
            box.pack_start(ml, False, False, 0)
        box.pack_start(Gtk.Label(label="Volume label:", xalign=0), False, False, 0)
        name = Gtk.Entry(text=re.sub(r"[^A-Za-z0-9_ -]", "", label)[:32] or "USB", activates_default=True)
        box.pack_start(name, False, False, 0)
        quick = Gtk.CheckButton(label="Quick format (don't overwrite existing data)", active=True)
        box.pack_start(quick, False, False, 0)
        if total > 64 << 30:
            box.pack_start(Gtk.Label(label=f"This drive is large. Type its current name ({label}) to confirm:", xalign=0, wrap=True), False, False, 0)
            confirm = Gtk.Entry()
            box.pack_start(confirm, False, False, 0)
        else:
            confirm = None
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        while True:
            r = d.run()
            if r != Gtk.ResponseType.OK:
                d.destroy()
                return
            if not avail:
                message(self, "Format", "No mkfs tool is installed for any supported file system.", Gtk.MessageType.ERROR)
                continue
            if confirm is not None and confirm.get_text().strip() != label.strip():
                message(self, "Format", "The name doesn't match — nothing was done.", Gtk.MessageType.WARNING)
                continue
            break
        fstype, vol_label, erase = combo.get_active_id(), name.get_text().strip(), not quick.get_active()
        d.destroy()
        prog = Gtk.Dialog(title="Formatting", transient_for=self, modal=True)
        pb = Gtk.ProgressBar(text=f"Formatting {dev} as {fstype}…", show_text=True, margin=16)
        prog.get_content_area().pack_start(pb, False, False, 0)
        prog.set_default_size(380, -1)
        prog.show_all()
        pulse = GLib.timeout_add(100, lambda: (pb.pulse(), True)[1])

        def done(err):
            GLib.source_remove(pulse)
            prog.destroy()
            invalidate_mounts()
            if err:
                message(self, "Format failed", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"{dev} formatted as {fstype}" + (f" ({vol_label})" if vol_label else ""))
            self._drives_changed()
        # UDisks needs the filesystem unmounted first; it does that itself when asked via Format on a mounted
        # block, but doing it explicitly gives a clearer error when a file is still open on the stick
        def after_unmount(uerr):
            if uerr and "not mounted" not in uerr.lower():
                GLib.source_remove(pulse)
                prog.destroy()
                message(self, "Format", f"Couldn't unmount first: {uerr}", Gtk.MessageType.ERROR)
                return
            udisks_format(dev, fstype, vol_label, erase, done)
        unmount_path(path, self, after_unmount)

    def unmount(self, path: str):
        def done(err):
            if err:
                message(self, "Disconnect", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"Disconnected {self.display_path(path)}")
                for fv in self._tabs():
                    if fv.path == path or fv.path.startswith(path.rstrip("/") + "/"):
                        self.navigate(NETWORK, tab=fv)
                self.rebuild_nav()
                self.reload_all()
        unmount_path(path, self, done)

    def open_network_entry(self, e: Entry) -> bool:
        """Open a server/share seen through GVfs: mount it first if needed, then browse its FUSE path."""
        if not e.uri or e.uri.startswith("file://"):
            return False
        g = Gio.File.new_for_uri(e.uri)
        p = g.get_path()
        if p and os.path.isdir(p):
            self.navigate(p)
            return True
        self.flash(f"Connecting to {e.name}…")

        def done(path, err):
            if err:
                message(self, "Network", err, Gtk.MessageType.ERROR)
            elif path and os.path.isdir(path):
                self.navigate(path)
            else:
                # not browsable as a folder (e.g. a server list) — enumerate it in place
                self.flash(f"{e.name}: not browsable as a folder from here")
        mount_uri(e.uri, self, done)
        return True

    # ------------------------------------------------------------------ side pane (preview / details)
    def _build_side(self):
        """Right-hand side: Preview on top, a Quick-view properties card below, in a resizable vertical paned."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("sidepane")
        self.side_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.side_paned.set_wide_handle(False)
        outer.pack_start(self.side_paned, True, True, 0)

        # ---- preview
        pv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        self.preview_box = pv
        self.preview_caption = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.preview_caption.get_style_context().add_class("dim-label")
        pv.pack_start(self.preview_caption, False, False, 0)
        self.preview_stack = Gtk.Stack()
        self.preview_stack.set_homogeneous(False)           # minimum = the visible child's, not the biggest child's
        self.preview_stack.set_hhomogeneous(False)
        self.preview_stack.set_vhomogeneous(False)
        # Transitions are done by snapshot: when a new preview lands, a picture of what the pane showed a
        # moment ago is laid over it and faded out (see _show_preview). That gives every file type — images,
        # text, folders, archives, GL/software 3D — the same old→new crossfade with no grey in between,
        # which neither GtkStack's crossfade (child → child only) nor opacity fades (bleed the background
        # through) can do for a widget whose content is swapped in place.
        self.preview_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.preview_overlay = Gtk.Overlay()
        self._snap_surface = None
        self._snap_alpha = 0.0
        # always shown (and therefore realised and allocated from the start): a widget first shown at the
        # moment of a fade is not allocated until the next layout pass and misses the first frames
        self.snap_area = Gtk.DrawingArea()
        self.snap_area.set_has_window(False)        # no GdkWindow of its own → it can never intercept input
        self.snap_area.set_can_focus(False)
        self.snap_area.set_app_paintable(True)
        self.snap_area.connect("draw", self._draw_snapshot)
        self.preview_overlay.add_overlay(self.snap_area)
        self.preview_overlay.set_overlay_pass_through(self.snap_area, True)
        self.preview_image = FitImage()
        if GtkSource is not None:
            self.preview_text = GtkSource.View.new_with_buffer(GtkSource.Buffer())
            self.preview_text.set_show_line_numbers(True)
            self.preview_text.set_highlight_current_line(True)
            self.preview_text.set_tab_width(4)
            self.preview_text.set_insert_spaces_instead_of_tabs(False)
            self.preview_text.set_auto_indent(True)
            self.preview_text.set_monospace(True)
            self.preview_text.set_right_margin_position(100)
            self._lang_mgr = GtkSource.LanguageManager.get_default()
            self._style_mgr = GtkSource.StyleSchemeManager.get_default()
        else:
            self.preview_text = Gtk.TextView(monospace=True)
        self.preview_text.set_editable(False)
        self.preview_text.set_cursor_visible(False)
        self.preview_text.set_wrap_mode(Gtk.WrapMode.NONE)
        self.preview_text.set_left_margin(6)
        self.preview_text.connect("key-press-event", self._edit_key)
        tsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        tsw.add(self.preview_text)
        # quick-edit toolbar: Edit toggle · find · prev/next · Save · Revert
        tb = Gtk.Box(spacing=4)
        tb.get_style_context().add_class("edit-toolbar")
        self.edit_toggle = Gtk.ToggleButton(label="Edit")
        self.edit_toggle.set_tooltip_text("Edit this text file in place (Ctrl+S saves)")
        self.edit_toggle.connect("toggled", self._edit_toggled)
        tb.pack_start(self.edit_toggle, False, False, 0)
        self.edit_badge = Gtk.Label(label="READ-ONLY")
        self.edit_badge.get_style_context().add_class("edit-badge")
        tb.pack_start(self.edit_badge, False, False, 2)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Find in file", width_chars=14, hexpand=True)
        self.find_entry.connect("search-changed", lambda e: self._find(1, from_start=True))
        self.find_entry.connect("activate", lambda e: self._find(1))
        self.find_entry.connect("key-press-event", lambda w, ev: (self._find(-1) or True) if ev.keyval == Gdk.KEY_Return and ev.state & Gdk.ModifierType.SHIFT_MASK else False)
        tb.pack_start(self.find_entry, True, True, 0)
        for icon, d, tip in (("go-up-symbolic", -1, "Previous match (Shift+Enter)"), ("go-down-symbolic", 1, "Next match (Enter)")):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda w, dd=d: self._find(dd))
            tb.pack_start(b, False, False, 0)
        self.find_count = Gtk.Label()
        self.find_count.get_style_context().add_class("dim-label")
        tb.pack_start(self.find_count, False, False, 4)
        self.btn_save = Gtk.Button(label="Save", sensitive=False)
        self.btn_save.set_tooltip_text("Save (Ctrl+S)")
        self.btn_save.connect("clicked", lambda b: self.save_edit())
        self.btn_revert = Gtk.Button(label="Revert", sensitive=False)
        self.btn_revert.connect("clicked", lambda b: self.revert_edit())
        tb.pack_end(self.btn_save, False, False, 0)
        tb.pack_end(self.btn_revert, False, False, 0)
        psw = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        psw.pack_start(clip_width(tb), False, False, 0)
        psw.pack_start(tsw, True, True, 0)
        self.edit_path: Optional[str] = None
        self.edit_entry: Optional[Entry] = None
        self.edit_dirty = False
        self.edit_full = False
        buf = self.preview_text.get_buffer()
        self.find_tag = buf.create_tag("find", background="#f5c542", foreground="#000000")
        self.find_cur_tag = buf.create_tag("findcur", background="#ff8c00", foreground="#000000")
        buf.connect("changed", self._edit_changed)
        self.preview_info = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.preview_info.get_style_context().add_class("dim-label")
        self.preview_big_icon = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        self.preview_icon_img = FitImage(128)
        self.preview_icon_img.set_size_request(-1, 48)
        self.preview_icon_lab = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.preview_icon_lab.get_style_context().add_class("dim-label")
        self.preview_big_icon.pack_start(self.preview_icon_img, True, True, 0)
        self.preview_big_icon.pack_start(self.preview_icon_lab, False, False, 0)
        self.preview_folder = Gtk.ListStore(GObject.TYPE_PYOBJECT, str)     # entry, size text
        ftv = Gtk.TreeView(model=self.preview_folder, headers_visible=False)
        ftv.set_activate_on_single_click(False)
        fcol = Gtk.TreeViewColumn("")
        fpix, ftxt = Gtk.CellRendererPixbuf(), Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        # Icon themes hand back pixbufs of slightly different heights for different MIME types (and some
        # 16 px requests come back 20–24 px), which made rows uneven. Fix the renderer's box so every row is
        # the same height regardless of the icon it holds.
        fpix.set_fixed_size(22, 22)
        ftxt.set_fixed_size(-1, 24)
        fsize_fixed = 24
        fcol.pack_start(fpix, False)
        fcol.pack_start(ftxt, True)
        fcol.set_expand(True)
        fcol.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        fcol.set_cell_data_func(fpix, lambda c, cell, m, it, d=None: cell.set_property("pixbuf", ICONS.pixbuf(m[it][0].icon, 16)), None)
        fcol.set_cell_data_func(ftxt, lambda c, cell, m, it, d=None: cell.set_property("text", m[it][0].name), None)
        ftv.append_column(fcol)
        fsize = Gtk.CellRendererText(xalign=1.0)
        fsize.set_property("foreground", "#888888")
        fsize.set_fixed_size(-1, fsize_fixed)
        scol = Gtk.TreeViewColumn("", fsize, text=1)
        scol.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        scol.set_fixed_width(90)
        ftv.append_column(scol)
        ftv.set_fixed_height_mode(True)
        ftv.connect("row-activated", lambda t, p, c: self.activate_entry(self.preview_folder[p][0]))
        fsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        fsw.add(ftv)
        self.model_view = None            # created on the first model preview (GL context setup is ~300 ms)
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.model_box = mbox
        mbar = Gtk.Box(spacing=6)
        self.model_count = Gtk.Label(xalign=0, hexpand=True)
        self.model_count.get_style_context().add_class("dim-label")
        mbar.pack_start(self.model_count, True, True, 4)
        self.model_full_toggle = Gtk.ToggleButton(label="All triangles")
        self.model_full_toggle.set_tooltip_text("Override the 500,000-face preview cap and load every triangle.\n"
                                                "Heavy meshes will orbit slowly — this is a software renderer.")
        self.model_full_toggle.get_style_context().add_class("flat")
        self.model_full_toggle.connect("toggled", self._toggle_model_full)
        mbar.pack_end(self.model_full_toggle, False, False, 0)
        mbox.pack_start(clip_width(mbar), False, False, 0)
        self.model_slot = Gtk.Box()
        mbox.pack_start(self.model_slot, True, True, 0)
        mhint = Gtk.Label(label="LMB orbit · MMB pan · Alt+RMB or wheel zoom · double-click reset · W wireframe · S flat/smooth", ellipsize=Pango.EllipsizeMode.END)
        mhint.get_style_context().add_class("dim-label")
        mbox.pack_start(mhint, False, False, 0)
        self.preview_stack.add_named(mbox, "model")
        self.preview_stack.add_named(self.preview_image, "image")
        self.video = VideoPreview() if gst_sink_available() else None
        if self.video is not None:
            self.video.sink_widget.set_size_request(-1, 0)      # no minimum: the pane decides
        if self.video is not None:
            self.preview_stack.add_named(self.video, "video")
        self.preview_stack.add_named(psw, "text")
        self.preview_stack.add_named(self.preview_info, "info")
        self.preview_stack.add_named(self.preview_big_icon, "icon")
        self.preview_stack.add_named(fsw, "folder")
        self.preview_overlay.add(self.preview_stack)
        pv.pack_start(self.preview_overlay, True, True, 0)
        # shrink=True on both halves: the divider can be dragged until either the preview or the quick view
        # is almost closed, regardless of what the current content would like as its minimum size
        self.side_paned.pack1(pv, True, False)

        # ---- quick view (properties card)
        qv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.details_box = qv
        head = Gtk.Box(spacing=8)
        self.qv_icon = Gtk.Image()
        head.pack_start(self.qv_icon, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.details_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, selectable=True)
        self.qv_subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.qv_subtitle.get_style_context().add_class("dim-label")
        tb.pack_start(self.details_title, False, False, 0)
        tb.pack_start(self.qv_subtitle, False, False, 0)
        head.pack_start(tb, True, True, 0)
        qv.pack_start(head, False, False, 0)
        self.details_grid = Gtk.Grid(column_spacing=10, row_spacing=3)
        qsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        qsw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        qsw.add(self.details_grid)
        qv.pack_start(qsw, True, True, 0)
        act = Gtk.Box(spacing=4)
        act.get_style_context().add_class("linked")
        for lab, cb in (("Open", lambda: self.activate_entry(self.tab.current_entry() if self.tab else None)),
                        ("Open with…", self.open_with), ("Properties…", self.properties)):
            b = Gtk.Button(label=lab)
            b.connect("clicked", lambda w, c=cb: c())
            act.pack_start(b, True, True, 0)
        self.qv_actions = act
        qv.pack_end(act, False, False, 0)
        self.side_paned.pack2(qv, False, False)
        outer.set_size_request(180, -1)                       # floor only; the splitter sets the real width
        return outer

    def _restore_side_width(self, paned, alloc):
        if self._side_restored or alloc.width < 300 or not self.side.get_visible():
            return
        self._side_restored = True
        width = int(self.cfg.get("side_width") or self.cfg.get("preview_width", 300))
        width = max(180, min(width, alloc.width - 300))
        paned.set_position(alloc.width - width)

    def _remember_side_width(self):
        if not self._side_restored or not self.side.get_visible():
            return
        w = self.right.get_allocated_width() - self.right.get_position()
        if w >= 180:
            self.cfg["side_width"] = w

    def _remember_side_split(self):
        if getattr(self, "_split_restored", False) and self.cfg["preview_pane"] and self.cfg["details_pane"]:
            pos = self.side_paned.get_position()
            if pos > 40:
                self.cfg["side_split"] = pos

    def _apply_panes(self):
        self.nav_box.set_visible(self.cfg["nav_pane"])
        show_side = self.cfg["preview_pane"] or self.cfg["details_pane"]
        self.side.set_visible(show_side)
        self.preview_box.set_visible(self.cfg["preview_pane"])
        self.details_box.set_visible(self.cfg["details_pane"])
        if self.cfg["preview_pane"] and self.cfg["details_pane"]:
            def place():
                h = self.side_paned.get_allocated_height()
                saved = int(self.cfg.get("side_split") or 0)
                self.side_paned.set_position(saved if 40 < saved < h - 40 else int(h * 0.55))
                self._split_restored = True
                return False
            GLib.idle_add(place)
        self._side_restored = False          # re-apply the remembered width when the pane (re)appears
        self._update_side()

    def _update_side(self):
        if not (self.cfg["preview_pane"] or self.cfg["details_pane"]):
            return
        t = self.tab
        if t is None:
            return
        sel = t.selected_entries()
        if not sel and getattr(self, "_nav_preview", None) is not None:
            sel = [self._nav_preview]          # a Recent file picked in the nav pane with a single click
        e = sel[0] if sel else None
        self._side_gen = getattr(self, "_side_gen", 0) + 1
        self._take_snapshot()
        # Whatever the pane was computing for the previous item is now moot: stop the folder-size walks at
        # once. Left running they compete with the next folder listing for the disk / network share, which is
        # what made navigating away from a selected folder feel sticky.
        for attr in ("_pv_cancel", "_qv_cancel"):
            ev = getattr(self, attr, None)
            if ev is not None:
                ev.set()
                setattr(self, attr, None)
        if self.cfg["preview_pane"]:
            self._update_preview(e)
        if self.cfg["details_pane"]:
            if self.editing_pinned and self.edit_entry is not None and (e is None or e.path != self.edit_path):
                sel = [self.edit_entry]        # keep the edited file's details on screen while editing
            self._update_details(sel)

    # ---- preview
    def _update_preview(self, e: Optional[Entry]):
        gen = self._side_gen
        # unsaved quick-edit: selecting something else (or nothing) asks first; "Keep editing" stays put
        if self.editing_pinned and (e is None or e.path != self.edit_path):
            if self.edit_dirty:
                # Never open a modal dialog from inside a selection/button handler: the view would miss the
                # button release and think a drag is in progress. Defer it to the next main-loop turn.
                if not getattr(self, "_prompt_pending", False):
                    self._prompt_pending = True
                    GLib.timeout_add(60, self._deferred_prompt)
            elif e is not None:
                self.edit_toggle.set_active(False)      # clean editor, new file chosen: just leave edit mode
                self._update_preview(e)
                return
            self._show_edit_caption()
            self._show_preview("text")
            return
        if e is not None and e.path == self.edit_path and self.editing_pinned:
            self._show_edit_caption()
            self._show_preview("text")      # same file re-selected: keep the edits
            return
        # Re-selecting the item already on screen (same path, size and mtime) keeps the finished preview:
        # re-decoding a model or a big image on every click made double-clicks feel laggy.
        key = (e.path, e.size, e.mtime) if e is not None else None
        shown = self.preview_stack.get_visible_child_name()
        if key is not None and key == getattr(self, "_preview_key", None) and shown not in ("info", None):
            self._drop_snapshot()               # nothing changes: let the held snapshot go at once
            return
        self._preview_key = key
        if self.video is not None and shown == "video":
            self.video.stop()                   # leaving the player: release the decoder, stop the sound
        self._set_caption("")
        if e is None:
            # The selection is empty for an instant between clicks and during navigation; showing the empty
            # state right away flashes grey between two previews. Only show it if nothing follows within 200 ms.
            self._placeholder("Select a file to preview", gen, 200)
            return
        if e.is_dir:
            if e.path:
                # listing happens in a thread: a slow network folder must not freeze the window; the pane keeps
                # the previous item until the listing lands (placeholder only if that takes > 250 ms)
                self._set_caption("Listing " + e.name + "…")
                # the listing itself only starts after the 250 ms double-click debounce below, so the
                # placeholder must come well after that — otherwise every folder shows grey for one frame and
                # the list pops in on top of it instead of crossfading from the previous item
                self._placeholder("Listing…", gen, 900)
                fpath, show_hidden = e.path, self.cfg["show_hidden"]
                # debounce: the first click of a double-click selects the folder — only start reading it if
                # the selection is still current 250 ms later, so navigating never has to compete with a
                # preview scan of the very folder it is opening

                def flist():
                    try:
                        allkids = list_folder(fpath, show_hidden)
                    except Exception:
                        allkids = None
                    GLib.idle_add(fdone, allkids)

                def fdone(allkids):
                    if gen != self._side_gen:
                        return False
                    if allkids is None:
                        self.preview_folder.clear()
                        self._set_caption(e.name)
                        self._preview_as_icon(e, "")
                        return False
                    kids = sort_entries(allkids, "name", False, True)[:300]
                    self.preview_folder.clear()
                    nd = sum(1 for k in allkids if k.is_dir)
                    files_bytes = sum(k.size or 0 for k in allkids if not k.is_dir)
                    self._set_caption(f"{len(allkids) - nd} files, {nd} folders  ·  {fmt_size(files_bytes)} in files" + ("  ·  showing first 300" if len(allkids) > 300 else ""))
                    for k in kids:
                        self.preview_folder.append([k, "" if k.is_dir else fmt_size(k.size, self.cfg["size_in_bytes"])])
                    self._show_preview("folder")             # content arrived: fade it in
                    self._preview_dir_sizes(gen, [k for k in kids if k.is_dir], fpath)
                    return False
                GLib.timeout_add(250, lambda: (gen == self._side_gen and threading.Thread(target=flist, daemon=True).start(), False)[1])
                return
            self.preview_folder.clear()
            self._set_caption(e.name)
            self._preview_as_icon(e, "")
            return
        self._set_caption(f"{e.name}  ·  {e.type}  ·  {fmt_size(e.size, self.cfg['size_in_bytes'])}")
        ct = e.content_type or ""
        if self.video is not None and e.path and (ct.startswith(("video/", "audio/")) or e.ext in (".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".m4v", ".ogv", ".flac", ".ogg", ".mp3", ".wav")):
            self.video.load(e.path)
            self.video.on_ready = lambda: gen == self._side_gen and self._show_preview("video")
            self._placeholder("Loading video…", gen)
            path = e.path

            def minfo():
                rows = describe_media(path)
                GLib.idle_add(lambda: (gen == self._side_gen and [self._qv_add(k, v) for k, v in rows], False)[1])
            threading.Thread(target=minfo, daemon=True).start()
            return
        w = max(120, self.side.get_allocated_width() - 24)
        h = max(120, self.preview_box.get_allocated_height() - 24)
        if ct.startswith("image/") and e.path:
            self._placeholder("Loading preview…", gen)
            path = e.path

            def work():
                try:
                    full = GdkPixbuf.Pixbuf.get_file_info(path)
                    dims = (full[1], full[2]) if full and full[0] else None
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, True)
                    pb = pb.apply_embedded_orientation() or pb
                except GLib.Error:
                    pb, dims = None, None
                if pb is None:
                    # GdkPixbuf has no loader for this one (EXR, IFF, HDR, DDS, PSD…): built-in decoders / tools
                    res = generate_preview(path, ct, w, h)
                    GLib.idle_add(self._extended_done, gen, e, res)
                    return
                GLib.idle_add(done, pb, dims)

            def done(pb, dims):
                if gen != self._side_gen:
                    return False
                self.preview_image.set_from_pixbuf(pb)
                self._show_preview("image")
                if dims:
                    e.extra_dims = dims
                    self._qv_set("Dimensions", f"{dims[0]} × {dims[1]} px")
                return False
            threading.Thread(target=work, daemon=True).start()
            return
        model_exts = (".obj", ".stl", ".ply", ".fbx", ".usd", ".usda", ".usdc", ".usdz", ".abc", ".gltf", ".glb", ".dae", ".blend", ".3ds", ".x3d", ".lwo", ".off")
        # text formats are parsed whole, so cap them; Alembic/USD/.blend readers only touch the first sample
        model_cap = (16 << 30) if e.ext in (".abc", ".usd", ".usdc", ".usdz", ".blend") else (600 << 20)
        if e.path and e.ext in model_exts and (e.size or 0) >= model_cap:
            self._preview_as_icon(e, f"Too large to preview ({fmt_size(e.size)})")
            return
        if e.path and e.ext in model_exts:
            self._load_model_preview(e, gen)
            return
        if e.path and (e.ext in PREVIEW_DECODERS or e.ext in SEVENZIP_EXTS or e.ext in (".obj", ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".whl",
                                                              ".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".dds", ".psd", ".dpx", ".cin", ".tx", ".tex", ".ptex")
                       or ct.startswith(("video/", "audio/", "application/pdf"))):
            self._placeholder("Loading preview…", gen)
            path = e.path
            threading.Thread(target=lambda: GLib.idle_add(self._extended_done, gen, e, generate_preview(path, ct, w, h)), daemon=True).start()
            return
        if (ct.startswith("text/") or ct in ("application/json", "application/xml", "application/x-shellscript", "application/javascript", "application/x-yaml", "application/x-desktop")) and e.path:
            try:
                with open(e.path, "rb") as f:
                    data = f.read(64 << 10)
                if b"\0" not in data:
                    self._load_text(e.path, data, (e.size or 0) <= 64 << 10)
                    self.edit_entry = e
                    self._show_preview("text")
                    return
            except OSError:
                pass
        self._preview_as_icon(e, "No preview available")

    def _preview_dir_sizes(self, gen, subdirs: List[Entry], parent: str):
        """Fill in subfolder sizes in the folder preview, in the background, with a sanity cap."""
        if not subdirs:
            return
        cancel = threading.Event()
        prev = getattr(self, "_pv_cancel", None)
        if prev:
            prev.set()
        self._pv_cancel = cancel
        paths = [d.path for d in subdirs]

        def work():
            results = {}
            for p in paths:
                if cancel.is_set() or gen != self._side_gen:
                    return
                f, d, b = dir_stats_cached(p, cancel)
                results[p] = b
                if len(results) % 5 == 0 or p == paths[-1]:
                    GLib.idle_add(apply, dict(results))

        def apply(res):
            if gen != self._side_gen:
                return False
            for row in self.preview_folder:
                k = row[0]
                if k.is_dir and k.path in res:
                    row[1] = fmt_size(res[k.path], self.cfg["size_in_bytes"])
                    total = sum(res.values())
            # caption: add folder total once everything is in
            if len(res) == len(paths):
                txt = self.preview_caption.get_text()
                if "  ·  total " not in txt:
                    try:
                        files_part = [x for x in txt.split("  ·  ") if x.endswith("in files")][0]
                        fb = sum(k.size or 0 for k in (r[0] for r in self.preview_folder) if not k.is_dir)
                    except IndexError:
                        fb = 0
                    self.preview_caption.set_text(txt + f"  ·  total {fmt_size(fb + sum(res.values()))}")
            return False
        threading.Thread(target=work, daemon=True).start()

    # ---- quick text edit
    def _load_text(self, path: str, data: bytes, complete: bool):
        """Put file text into the preview buffer (first 64 KB for viewing; the whole file once editing starts)."""
        if self.edit_dirty and self.edit_path and self.edit_path != path:
            if not self.prompt_save():
                return
        self.edit_path, self.edit_full, self.edit_dirty = path, complete, False
        self.edit_toggle.set_sensitive(True)
        self.edit_toggle.set_active(False)
        buf = self.preview_text.get_buffer()
        buf.handler_block_by_func(self._edit_changed)
        buf.set_text(data.decode("utf-8", "replace"))
        buf.handler_unblock_by_func(self._edit_changed)
        self._apply_syntax(path)
        self._update_edit_buttons()
        if self.find_entry.get_text():
            self._find(1, from_start=True)

    def _apply_syntax(self, path: str):
        """Pick a GtkSourceView language from the file name/content type and a colour scheme matching the theme."""
        if GtkSource is None:
            return
        buf = self.preview_text.get_buffer()
        ct, _ = Gio.content_type_guess(path, None)
        lang = self._lang_mgr.guess_language(path, ct)
        if lang is None and os.path.basename(path).lower() in ("makefile", "dockerfile", "cmakelists.txt"):
            lang = self._lang_mgr.get_language({"makefile": "makefile", "dockerfile": "dockerfile", "cmakelists.txt": "cmake"}[os.path.basename(path).lower()])
        buf.set_language(lang)
        buf.set_highlight_syntax(lang is not None)
        settings = Gtk.Settings.get_default()
        dark = bool(settings and settings.get_property("gtk-application-prefer-dark-theme"))
        if not dark and settings:
            dark = "dark" in (settings.get_property("gtk-theme-name") or "").lower()
        for name in (("Adwaita-dark", "oblivion", "cobalt", "classic") if dark else ("Adwaita", "classic", "tango")):
            scheme = self._style_mgr.get_scheme(name)
            if scheme is not None:
                buf.set_style_scheme(scheme)
                break

    def prompt_save(self) -> bool:
        """Ask Save / Discard / Keep editing for unsaved quick-edit changes. Returns True if it's OK to move on."""
        if not (self.edit_dirty and self.edit_path):
            return True
        if getattr(self, "_prompting", False):
            return False
        self._prompting = True
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              text=f"Save changes to {os.path.basename(self.edit_path)}?")
        d.format_secondary_text("Your edits in the preview pane haven't been saved.")
        d.add_buttons("_Discard", 2, "_Keep editing", Gtk.ResponseType.CANCEL, "_Save", 1)
        d.set_default_response(1)
        r = d.run()
        d.destroy()
        self._prompting = False
        if r == 1:
            self.save_edit()
            return not self.edit_dirty
        if r == 2:
            self.revert_edit()
            return True
        return False

    def _show_edit_caption(self):
        ee = self.edit_entry
        if ee is not None:
            self.preview_caption.set_text(f"{ee.name}  ·  {ee.type}  ·  {fmt_size(ee.size, self.cfg['size_in_bytes'])}" + ("  ·  (unsaved)" if self.edit_dirty else "  ·  editing"))

    @property
    def editing_pinned(self) -> bool:
        """True while the preview should stay on the file being edited (edit mode on, or unsaved changes)."""
        return bool(self.edit_path) and (self.edit_dirty or self.edit_toggle.get_active())

    def _deferred_prompt(self):
        self._prompt_pending = False
        if not self.edit_dirty:
            return False
        if self.prompt_save():
            self._update_side()                 # proceed with whatever is selected now
        else:
            # Keep editing: reselect the edited file if it's in this folder, keep the editor focused
            t = self.tab
            name = os.path.basename(self.edit_path or "")
            self._show_preview("text")
            if t is not None and os.path.dirname(self.edit_path) == t.path and any(x.name == name for x in t.entries):
                t.select_names([name], scroll=False)
            self.preview_text.grab_focus()
        return False

    def _edit_toggled(self, btn):
        on = btn.get_active()
        if not on and self.edit_dirty and not self.prompt_save():
            btn.set_active(True)
            return
        if on and self.edit_path and not self.edit_full:
            try:
                if os.path.getsize(self.edit_path) > 8 << 20:
                    message(self, "Edit", "Files over 8 MB are too big for the quick editor — use Open with…", Gtk.MessageType.INFO)
                    btn.set_active(False)
                    return
                with open(self.edit_path, "rb") as f:
                    data = f.read()
                buf = self.preview_text.get_buffer()
                buf.handler_block_by_func(self._edit_changed)
                buf.set_text(data.decode("utf-8", "replace"))
                buf.handler_unblock_by_func(self._edit_changed)
                self.edit_full = True
            except OSError as ex:
                message(self, "Edit", str(ex), Gtk.MessageType.ERROR)
                btn.set_active(False)
                return
        self.preview_text.set_editable(on)
        self.preview_text.set_cursor_visible(on)
        if on:
            self.preview_text.grab_focus()
        self._update_edit_buttons()
        self._update_edit_look()

    def _update_edit_look(self):
        """Make the editing state unmistakable: badge text, button label, coloured frame around the editor."""
        on = self.edit_toggle.get_active()
        ctx = self.preview_text.get_style_context()
        tctx = self.edit_toggle.get_style_context()
        bctx = self.edit_badge.get_style_context()
        for c in ("editing", "readonly"):
            ctx.remove_class(c)
        for c in ("suggested-action", "destructive-action"):
            tctx.remove_class(c)
        for c in ("badge-editing", "badge-dirty"):
            bctx.remove_class(c)
        if on:
            ctx.add_class("editing")
            tctx.add_class("suggested-action")
            self.edit_toggle.set_label("Done")
            if self.edit_dirty:
                self.edit_badge.set_text("EDITING · UNSAVED")
                bctx.add_class("badge-dirty")
            else:
                self.edit_badge.set_text("EDITING")
                bctx.add_class("badge-editing")
        else:
            ctx.add_class("readonly")
            self.edit_toggle.set_label("Edit")
            self.edit_badge.set_text("READ-ONLY")

    def _edit_changed(self, buf):
        if self.edit_toggle.get_active():
            self.edit_dirty = True
            self._update_edit_buttons()

    def _update_edit_buttons(self):
        self.btn_save.set_sensitive(self.edit_dirty)
        self.btn_revert.set_sensitive(self.edit_dirty)
        if hasattr(self, "edit_badge"):
            self._update_edit_look()
        if self.edit_path and self.edit_entry is not None and (self.edit_toggle.get_active() or self.edit_dirty):
            self._show_edit_caption()

    def save_edit(self):
        if not (self.edit_path and self.edit_dirty):
            return
        buf = self.preview_text.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        tmp = self.edit_path + ".kestrel-tmp"
        try:
            st = os.stat(self.edit_path)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, _stat.S_IMODE(st.st_mode))
            os.replace(tmp, self.edit_path)
            self.edit_dirty = False
            self._update_edit_buttons()
            self.flash(f"Saved {os.path.basename(self.edit_path)}")
        except OSError as ex:
            message(self, "Save", str(ex), Gtk.MessageType.ERROR)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def revert_edit(self):
        if not self.edit_path:
            return
        try:
            with open(self.edit_path, "rb") as f:
                data = f.read()
        except OSError as ex:
            message(self, "Revert", str(ex), Gtk.MessageType.ERROR)
            return
        buf = self.preview_text.get_buffer()
        buf.handler_block_by_func(self._edit_changed)
        buf.set_text(data.decode("utf-8", "replace"))
        buf.handler_unblock_by_func(self._edit_changed)
        self.edit_dirty = False
        self.edit_full = True
        self._update_edit_buttons()

    def _edit_key(self, tv, event):
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if mods & Gdk.ModifierType.CONTROL_MASK:
            if event.keyval in (Gdk.KEY_s, Gdk.KEY_S):
                self.save_edit()
                return True
            if event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.find_entry.grab_focus()
                return True
        if event.keyval == Gdk.KEY_Escape:
            self.tab.view.grab_focus()
            return True
        return False

    def _find(self, direction: int, from_start: bool = False):
        """Highlight all matches of the find box in the preview text and move to the next/previous one."""
        buf = self.preview_text.get_buffer()
        needle = self.find_entry.get_text()
        s, e = buf.get_start_iter(), buf.get_end_iter()
        buf.remove_tag(self.find_tag, s, e)
        buf.remove_tag(self.find_cur_tag, s, e)
        if not needle:
            self.find_count.set_text("")
            return
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE | Gtk.TextSearchFlags.TEXT_ONLY
        matches = []
        it = buf.get_start_iter()
        while True:
            hit = it.forward_search(needle, flags, None)
            if not hit:
                break
            matches.append(hit)
            buf.apply_tag(self.find_tag, hit[0], hit[1])
            it = hit[1]
        if not matches:
            self.find_count.set_text("0")
            return
        cur = buf.get_iter_at_mark(buf.get_insert())
        if from_start:
            idx = next((i for i, (a, b) in enumerate(matches) if a.compare(cur) >= 0), 0)
        elif direction > 0:
            idx = next((i for i, (a, b) in enumerate(matches) if a.compare(cur) > 0), 0)
        else:
            idx = next((i for i in range(len(matches) - 1, -1, -1) if matches[i][0].compare(cur) < 0), len(matches) - 1)
        a, b = matches[idx]
        buf.apply_tag(self.find_cur_tag, a, b)
        buf.place_cursor(a)
        self.preview_text.scroll_to_iter(a, 0.1, True, 0.0, 0.5)
        self.find_count.set_text(f"{idx + 1}/{len(matches)}")

    def _extended_done(self, gen, e: Entry, res: dict):
        """Main-thread half of generate_preview(): show the image / text and add info rows to the quick view."""
        if gen != self._side_gen:
            if res.get("png"):
                try:
                    os.remove(res["png"])
                except OSError:
                    pass
            return False
        for k, v in res.get("info", []):
            self._qv_add(k, v)
        if "raw" in res:
            img = res["raw"]
            pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(img.rgb), GdkPixbuf.Colorspace.RGB, False, 8, img.w, img.h, img.w * 3)
            self.preview_image.set_from_pixbuf(pb)
            self._show_preview("image")
        elif "png" in res:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(res["png"])
                self.preview_image.set_from_pixbuf(pb)
                self._show_preview("image")
            except GLib.Error:
                self._preview_as_icon(e, "No preview available")
            finally:
                try:
                    os.remove(res["png"])
                except OSError:
                    pass
        elif "text" in res:
            self.edit_toggle.set_sensitive(False)
            buf = self.preview_text.get_buffer()
            buf.handler_block_by_func(self._edit_changed)
            buf.set_text(res["text"])
            buf.handler_unblock_by_func(self._edit_changed)
            if GtkSource is not None:
                buf.set_language(None)
            self.edit_path = None
            self._show_preview("text")
        else:
            self._preview_as_icon(e, "No preview available")
        return False

    def _qv_add(self, key: str, value: str):
        """Append (or update) a row in the quick-view card."""
        labs = getattr(self, "_qv_labels", {})
        if key in labs:
            labs[key].set_text(value)
            return
        g = self.details_grid
        i = len(labs)
        kl = Gtk.Label(label=key, xalign=1, yalign=0)
        kl.get_style_context().add_class("dim-label")
        vl = Gtk.Label(label=value or "—", xalign=0, wrap=True, selectable=True, hexpand=True)
        vl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        vl.set_max_width_chars(34)
        g.attach(kl, 0, i, 1, 1)
        g.attach(vl, 1, i, 1, 1)
        labs[key] = vl
        self._qv_labels = labs
        g.show_all()

    def _make_model_view(self):
        """GLModelView when hardware acceleration is on and PyOpenGL imports, otherwise the software viewer."""
        if self.cfg.get("gl_preview", True) and HAVE_GL:
            v = GLModelView()
            v.on_error = self._gl_failed
            return v
        return ModelView()

    def _gl_failed(self, msg: str):
        """OpenGL viewer reported a failure: show it where the user is looking, drop to the software renderer."""
        self.flash("OpenGL preview failed; using the software renderer")
        if self.cfg.get("gl_preview", True):
            self.cfg["gl_preview"] = True
            self.toggle_gl_preview()          # flips to False and swaps in ModelView
        self.model_count.set_markup(f"<span foreground='#c0392b'>OpenGL failed: {GLib.markup_escape_text(msg[:160])}</span>")
        self.model_count.set_tooltip_text(msg)

    def _ensure_model_view(self):
        if self.model_view is None:
            self.model_view = self._make_model_view()
            self.model_slot.pack_start(self.model_view, True, True, 0)
            self.model_view.show()
        return self.model_view

    def toggle_gl_preview(self):
        """Preferences → Optional. Swaps the viewer widget in place and re-shows the model."""
        self.cfg["gl_preview"] = not self.cfg.get("gl_preview", True)
        save_config(self.cfg)
        old = self.model_view
        m = old.model if old is not None else None
        if old is not None:
            self.model_slot.remove(old)
        new = self._make_model_view()
        self.model_slot.pack_start(new, True, True, 0)
        new.show()
        self.model_view = new
        if m is not None:
            new.set_model(m)
        if self.cfg["gl_preview"] and not HAVE_GL:
            self.flash("PyOpenGL is not installed; using the software renderer")
        else:
            self.flash("3D preview: " + ("hardware accelerated (OpenGL, white)" if isinstance(new, GLModelView) else "software renderer (blue)"))

    def _load_model_preview(self, e: Entry, gen: int):
        """Decode a 3D file in the worker process and hand it to the ModelView. Honors the 'All triangles'
        override (no face cap, no drag thinning); otherwise meshes are decimated to 120k faces for speed."""
        full = self.model_full_toggle.get_active()
        self._placeholder("Loading model…" + (" (all triangles)" if full else ""), gen)
        path = e.path
        self._model_entry = e

        def mwork():
            try:
                m = run_heavy(load_model, path, sys.maxsize if full else 500000)
            except Exception as ex:
                m = {"error": str(ex)}
            GLib.idle_add(mdone, m)

        def mdone(m):
            if gen != self._side_gen:
                return False
            if not m or "error" in (m or {}):
                if e.ext == ".abc":
                    txt = describe_alembic(e.path)
                    if txt:
                        self._extended_done(gen, e, {"text": txt, "info": [("Alembic", "no PyAlembic found — showing hierarchy")]})
                        return False
                self._preview_as_icon(e, "Couldn't read this model" + (f"\n{m['error']}" if m else ""))
                return False
            for k, v in m["info"]:
                self._qv_add(k, v)
            mv = self._ensure_model_view()
            shown, total = len(m["faces"]), m.get("nfaces_total", len(m["faces"]))
            self.model_count.set_text(f"{shown:,} of {total:,} faces" if shown < total else f"{total:,} faces")
            if isinstance(mv, ModelView) and np is not None:
                # software renderer: fade in when the first frame arrives, not before (the widget would be blank)
                mv.on_first_frame = lambda: gen == self._side_gen and self._show_preview("model")
                mv.set_model(m)
                st = self.preview_stack.get_allocation()
                mv.prime(max(mv.get_allocated_width(), st.width), max(mv.get_allocated_height(), st.height - 60))
            else:
                mv.set_model(m)
                self._show_preview("model")
            return False
        threading.Thread(target=mwork, daemon=True).start()

    def _toggle_model_full(self, btn):
        e = getattr(self, "_model_entry", None)
        if e is None or self.preview_stack.get_visible_child_name() not in ("model", "info"):
            return
        self._side_gen += 1
        self._preview_key = None            # force a reload of the same file at the new detail level
        self._load_model_preview(e, self._side_gen)

    FADE_MS = 125

    def _set_caption(self, text: str):
        """Caption for the content that is about to be shown; applied when that content (or its placeholder)
        actually appears, so the caption never changes ahead of the picture under it."""
        self._pending_caption = text

    def _apply_caption(self):
        pc = getattr(self, "_pending_caption", None)
        if pc is not None:
            self.preview_caption.set_text(pc)
            self._pending_caption = None

    def _placeholder(self, text: str, gen: int, delay_ms: int = 250):
        """Show an 'info' placeholder only if the real content hasn't arrived after delay_ms. Until then the
        previous preview stays on screen, so quick loads go old → new in one fade, with no grey in between."""
        def due():
            if gen != self._side_gen or getattr(self, "_content_gen", -1) == gen:
                return False                      # superseded, or the real content already landed
            self.preview_info.set_text(text)
            self._apply_caption()
            self._show_preview("info")
            self._take_snapshot(hold=False)  # so the content that follows crossfades from the placeholder too
                                             # (no hold: the fade INTO the placeholder is still running)
            return False
        GLib.timeout_add(delay_ms, due)

    def _show_preview(self, name: str):
        """Switch the preview pane to child `name` with a ~⅛ s fade. The stack crossfades on its own when the
        child changes; when the same child is reused for a new file (image → image) we fade that child's
        opacity back in so every file switch reads as a soft cut rather than a pop."""
        stack = self.preview_stack
        child = stack.get_child_by_name(name)
        if child is None:
            return
        if name != "info":
            self._content_gen = self._side_gen          # real content for the current selection is on screen
            self._apply_caption()
        stack.set_visible_child_name(name)
        self._crossfade_from_snapshot()

    def _take_snapshot(self, hold: bool = True):
        """Render the preview pane as it is right now into an image (kept until the next content lands).
        With `hold` the image is also shown at once at full opacity, covering whatever the pane does while
        the next item loads."""
        if not self.cfg.get("preview_pane") or not self.preview_stack.get_realized():
            return
        a = self.preview_stack.get_allocation()
        if a.width < 2 or a.height < 2:
            return
        try:
            import cairo
            scale = self.get_scale_factor()
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, a.width * scale, a.height * scale)
            surf.set_device_scale(scale, scale)
            cr = cairo.Context(surf)
            # The snapshot must be OPAQUE or the new content shows through it from frame one (only the old
            # text/image would appear to fade). Most of our containers have no background of their own —
            # the colour comes from the toplevel — so paint the theme's window colour first, then any
            # container backgrounds, then the content.
            ok, bg = self.get_style_context().lookup_color("theme_bg_color")
            if ok:
                cr.set_source_rgb(bg.red, bg.green, bg.blue)
            else:
                cr.set_source_rgb(0.13, 0.13, 0.13)
            cr.paint()
            Gtk.render_background(self.get_style_context(), cr, 0, 0, a.width, a.height)
            for wdg in (self.side, self.preview_box, self.preview_stack):
                Gtk.render_background(wdg.get_style_context(), cr, 0, 0, a.width, a.height)
            self.preview_stack.draw(cr)
            self._snap_surface = surf
            if not hold:
                return
            # HOLD the snapshot on screen at full opacity right away. Whatever the pane does underneath while
            # the next item loads (blanking the video surface, clearing a list, a placeholder) stays hidden;
            # the fade-out starts only when the new content is in place — so every switch is old → new.
            self._fade_token = getattr(self, "_fade_token", 0) + 1
            self._snap_draw = surf
            self._snap_alpha = 1.0
            self.snap_area.queue_draw()
        except Exception as ex:
            bench(f"snapshot failed: {ex}")
            self._snap_surface = None

    def _drop_snapshot(self):
        """Discard a held snapshot without a fade (the content under it is unchanged)."""
        self._snap_surface = None
        self._fade_token = getattr(self, "_fade_token", 0) + 1
        if getattr(self, "_snap_draw", None) is not None:
            self._snap_draw = None
            self.snap_area.queue_draw()

    def _crossfade_from_snapshot(self):
        surf = self._snap_surface
        self._snap_surface = None
        if surf is None:
            return
        token = getattr(self, "_fade_token", 0) + 1
        self._fade_token = token
        self._snap_draw = surf
        self._snap_alpha = 1.0
        self.snap_area.queue_draw()
        t0 = time.monotonic()

        def tick():
            if self._fade_token != token:
                return False
            a = min(1.0, (time.monotonic() - t0) * 1000.0 / self.FADE_MS)
            self._snap_alpha = 1.0 - a * a * (3 - 2 * a)      # smoothstep out
            self.snap_area.queue_draw()
            if a >= 1.0:
                self._snap_draw = None
                self.snap_area.queue_draw()
                return False
            return True
        GLib.timeout_add(16, tick)

    def _draw_snapshot(self, w, cr):
        surf = getattr(self, "_snap_draw", None)
        if surf is None or self._snap_alpha <= 0:
            return False
        cr.set_source_surface(surf, 0, 0)
        cr.paint_with_alpha(self._snap_alpha)
        return True

    def _preview_as_icon(self, e: Entry, note: str):
        pb = ICONS.pixbuf(e.icon, 128)
        self.preview_icon_img.set_from_pixbuf(pb)
        self.preview_icon_lab.set_text(note)
        self._show_preview("icon")

    # ---- quick view
    def _qv_set(self, key: str, value: str):
        lab = getattr(self, "_qv_labels", {}).get(key)
        if lab is not None:
            lab.set_text(value)

    def _update_details(self, sel: List[Entry]):
        g = self.details_grid
        for c in g.get_children():
            g.remove(c)
        self._qv_labels = {}
        t = self.tab
        gen = self._side_gen
        self.qv_actions.set_sensitive(bool(sel))
        f_long = "%A, %B %d, %Y  %H:%M"
        if not sel:
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(Gio.ThemedIcon.new("folder"), 32))
            self.details_title.set_markup(f"<b>{GLib.markup_escape_text(self.display_path(t.path))}</b>")
            nd = sum(1 for e in t.entries if e.is_dir)
            self.qv_subtitle.set_text(f"{len(t.entries) - nd} files, {nd} folders")
            rows = [("Location", pretty_path(t.path) if t.path not in VIRTUAL else self.display_path(t.path))]
            if t.path not in VIRTUAL:
                rows.append(("Disk", "…"))
                tp = t.path

                def dwork():
                    try:
                        u = disk_usage(tp)
                        txt = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                    except OSError:
                        txt = "unavailable"
                    GLib.idle_add(lambda: (gen == self._side_gen and self._qv_set("Disk", txt), False)[1])
                threading.Thread(target=dwork, daemon=True).start()
        elif len(sel) > 1:
            nd = sum(1 for e in sel if e.is_dir)
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(Gio.ThemedIcon.new("edit-select-all"), 32))
            self.details_title.set_markup(f"<b>{len(sel)} items selected</b>")
            self.qv_subtitle.set_text(f"{len(sel) - nd} files, {nd} folders")
            total = sum(e.size or 0 for e in sel if not e.is_dir)
            rows = [("Size", fmt_size(total) + ("  (files only)" if nd else "")),
                    ("Location", pretty_path(os.path.dirname(sel[0].path)))]
            exts = sorted({e.ext for e in sel if e.ext})
            if exts:
                rows.append(("Types", ", ".join(exts[:8]) + ("…" if len(exts) > 8 else "")))
        else:
            e = sel[0]
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(e.icon, 32))
            self.details_title.set_markup(f"<b>{GLib.markup_escape_text(e.name)}</b>")
            self.qv_subtitle.set_text(e.type)
            rows = []
            if e.is_dir:
                rows.append(("Contains", "calculating…"))
                rows.append(("Size", "calculating…"))
            else:
                rows.append(("Size", f"{fmt_size(e.size)}  ({(e.size or 0):,} bytes)"))
            rows.append(("Location", e.location or pretty_path(os.path.dirname(e.path))))
            rows.append(("Modified", fmt_date(e.mtime, f_long)))
            if e.ctime:
                rows.append(("Created", fmt_date(e.ctime, f_long)))
            rows.append(("Accessed", fmt_date(e.atime, f_long)))
            rows.append(("Owner", e.owner))
            rows.append(("Permissions", (_stat.filemode(e.mode) + f"  ({oct(_stat.S_IMODE(e.mode))[2:]})") if e.mode else ""))
            if e.content_type:
                rows.append(("MIME type", e.content_type))
            if not e.is_dir:
                app = Gio.AppInfo.get_default_for_type(e.content_type, False) if e.content_type else None
                rows.append(("Opens with", app.get_display_name() if app else "—"))
            if (e.content_type or "").startswith("image/"):
                rows.append(("Dimensions", getattr(e, "extra_dims", None) and f"{e.extra_dims[0]} × {e.extra_dims[1]} px" or "…"))
            if (e.content_type or "").startswith("text/") and e.path and (e.size or 0) < 8 << 20:
                rows.append(("Lines", "…"))
            if e.is_link and e.path:
                try:
                    rows.append(("Link target", os.readlink(e.path)))
                except OSError:
                    pass
            if e.deleted:
                rows.append(("Deleted", fmt_date(e.deleted, f_long)))
            if script_runner(e.path or ""):
                rows.append(("Runnable", "yes"))
            # background facts: folder size / contents, text line count
            path = e.path
            if e.is_dir and path:
                cancel = threading.Event()
                self._qv_cancel = getattr(self, "_qv_cancel", None)
                if self._qv_cancel:
                    self._qv_cancel.set()
                self._qv_cancel = cancel

                def work():
                    f, d, b = dir_stats_cached(path, cancel)
                    if not cancel.is_set():
                        GLib.idle_add(lambda: (gen == self._side_gen and (self._qv_set("Contains", f"{f:,} files, {d:,} folders"), self._qv_set("Size", fmt_size(b) + f"  ({b:,} bytes)"))) and False)
                # same 250 ms debounce as the folder preview: a double-click must not start a size walk
                GLib.timeout_add(250, lambda: (gen == self._side_gen and not cancel.is_set() and threading.Thread(target=work, daemon=True).start(), False)[1])
            elif (e.content_type or "").startswith("text/") and path and (e.size or 0) < 8 << 20:
                def work_lines():
                    try:
                        with open(path, "rb") as fh:
                            n = sum(buf.count(b"\n") for buf in iter(lambda: fh.read(1 << 20), b""))
                    except OSError:
                        n = 0
                    GLib.idle_add(lambda: (gen == self._side_gen and self._qv_set("Lines", f"{n:,}")) and False)
                threading.Thread(target=work_lines, daemon=True).start()
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=1, yalign=0)
            kl.get_style_context().add_class("dim-label")
            vl = Gtk.Label(label=v or "—", xalign=0, wrap=True, selectable=True, hexpand=True)
            vl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            vl.set_max_width_chars(34)
            g.attach(kl, 0, i, 1, 1)
            g.attach(vl, 1, i, 1, 1)
            self._qv_labels[k] = vl
        g.show_all()

    # ------------------------------------------------------------------ tabs & panes
    @property
    def page(self) -> Optional["Page"]:
        n = self.notebook.get_current_page()
        return self.notebook.get_nth_page(n) if n >= 0 else None

    @property
    def tab(self) -> Optional[FolderView]:
        pg = self.page
        return (pg.active or pg.panes[0]) if pg else None

    def new_tab(self, path: str, switch: bool = True) -> FolderView:
        pg = Page(self, path)
        lab = Gtk.Box(spacing=2)
        lab.pack_start(pg.label, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.get_style_context().add_class("tab-close")
        close.connect("clicked", lambda b: pg.win.close_tab(pg))
        lab.pack_start(close, False, False, 0)
        lab.show_all()
        ev = Gtk.EventBox()
        ev.add(lab)
        ev.connect("button-press-event", lambda w, e: pg.win._on_tab_label_button(w, e, pg))
        ev.show_all()
        n = self.notebook.append_page(pg, ev)
        self.notebook.set_tab_reorderable(pg, True)
        self.notebook.set_tab_detachable(pg, True)
        pg.show_all()
        pg._update_headers()
        if switch:
            self.notebook.set_current_page(n)
        self._update_tabs_visible()
        return pg.panes[0]

    def _on_tab_label_button(self, w, event, pg):
        if event.button == 2:
            self.close_tab(pg)
            return True
        if event.button == 3:
            self.notebook.set_current_page(self.notebook.page_num(pg))
            spec = [("New _tab", lambda: self.new_tab(pg.active.path if pg.active.path not in VIRTUAL else HOME)),
                    ("_Duplicate tab", lambda: self.new_tab(pg.active.path)), "-",
                    ("Move tab to new _window", lambda: self.detach_tab(pg)),
                    ("Close split" if pg.split else "_Split view (two folders side by side)", lambda: pg.unsplit() if pg.split else pg.do_split()), "-",
                    ("Close other tabs", lambda: [self.close_tab(p) for p in self._pages() if p is not pg]),
                    ("_Close tab", lambda: self.close_tab(pg))]
            m = build_menu(spec)
            m.attach_to_widget(w, None)
            m.popup_at_pointer(event)
            return True
        return False

    def detach_tab(self, pg: "Page"):
        """Move a tab into a new window (same as dragging it out)."""
        if self.notebook.get_n_pages() == 1:
            return
        win = self.app.new_window("")
        tab_label = self.notebook.get_tab_label(pg)
        self.notebook.detach_tab(pg)
        n = win.notebook.append_page(pg, tab_label)
        win.notebook.set_tab_reorderable(pg, True)
        win.notebook.set_tab_detachable(pg, True)
        pg.win = win
        for fv in pg.panes:
            fv.win = win
        win.notebook.set_current_page(n)
        win.present()

    def _on_create_window(self, nb, page, x, y):
        win = self.app.new_window("")
        win.move(max(0, int(x) - 100), max(0, int(y) - 40))
        return win.notebook

    def _on_page_added(self, nb, child, n):
        if isinstance(child, Page):
            child.win = self
            for fv in child.panes:
                fv.win = self
        self._update_tabs_visible()

    def _on_page_removed(self, nb, child, n):
        self._update_tabs_visible()
        if nb.get_n_pages() == 0:
            GLib.idle_add(lambda: self.close() or False)

    def toggle_split(self):
        pg = self.page
        if pg:
            pg.unsplit() if pg.split else pg.do_split()

    def set_active_pane(self, fv: FolderView):
        pg = fv.page
        if pg is None or fv.win is not self:
            return
        if pg.active is not fv:
            pg.set_active(fv)
        self._refresh_crumbs()
        self._update_history_buttons()
        self._update_title()
        self._update_status()
        self._update_side()

    def close_tab(self, pg: Optional["Page"] = None):
        pg = pg or self.page
        if pg is None:
            return
        if self.notebook.get_n_pages() == 1:
            self.close()
            return
        for fv in pg.panes:
            fv.stop_search()
            if fv.monitor:
                fv.monitor.cancel()
        self.notebook.remove_page(self.notebook.page_num(pg))
        self._update_tabs_visible()

    def _pages(self) -> List["Page"]:
        return [self.notebook.get_nth_page(i) for i in range(self.notebook.get_n_pages())]

    def _update_tabs_visible(self):
        self.notebook.set_show_tabs(self.notebook.get_n_pages() > 1)

    def _on_switch_page(self, nb, page, n):
        GLib.idle_add(self._after_switch)

    def _after_switch(self):
        if self.tab is None:
            return False
        self._refresh_crumbs()
        self._update_history_buttons()
        self._update_title()
        self._update_status()
        self._update_side()
        self.tab.view.grab_focus()
        return False

    def on_tab_loaded(self, t: FolderView):
        if BENCH and not getattr(self, "_bench_listed", True) and not t.loading and not t.partial and t.entries:
            self._bench_listed = True
            bench(f"listed {len(t.entries)} items")
        if t.page:
            t.page.update_label()
        if t is self.tab:
            self._update_status()
            self._update_side()
            self._update_title()
            self._refresh_crumbs()

    def on_selection_changed(self, t: FolderView):
        self._nav_preview = None               # the folder view owns the side pane again
        if t is self.tab:
            self._update_status()
            self._update_side()

    def _update_title(self):
        t = self.tab
        if t is None:
            return
        self.set_title((pretty_path(t.path) if (self.cfg["full_path_title"] and t.path not in VIRTUAL) else t.title()) + " — " + APP_NAME)

    def _update_history_buttons(self):
        t = self.tab
        if t is None:
            return
        self.btn_back.set_sensitive(bool(t.back))
        self.btn_fwd.set_sensitive(bool(t.fwd))
        self.btn_up.set_sensitive(t.path != THISPC)

    def on_tab_busy(self, t: FolderView):
        """A listing just started in t: show the wheel right away (the status text itself is only
        refreshed when results arrive)."""
        if t is self.tab:
            self._set_spinner(True)

    SPINNER_DELAY_MS = 250        # only waits longer than this earn a wheel; quick folders never flash it

    def _set_spinner(self, on: bool):
        pending = getattr(self, "_spin_timer", None)
        if on:
            if self.spinner.get_visible() or pending:
                return
            self._spin_timer = GLib.timeout_add(self.SPINNER_DELAY_MS, self._spinner_due)
        else:
            if pending:
                GLib.source_remove(pending)
                self._spin_timer = None
            if self.spinner.get_visible():
                self.spinner.stop()
                self.spinner.hide()

    def _spinner_due(self):
        self._spin_timer = None
        t = self.tab
        if t is not None and (t.busy or t.loading or getattr(t, "partial", False) or (t.search and t.search.is_alive())):
            self.spinner.show()
            self.spinner.start()
        return False

    def _update_status(self):
        t = self.tab
        if t is None:
            return
        self._set_spinner(bool(t.busy or t.loading or getattr(t, "partial", False) or (t.search and t.search.is_alive())))
        n = len(t.entries)
        sel = t.selected_entries()
        s = f"{n} item{'s' if n != 1 else ''}"
        if sel:
            total = sum(e.size or 0 for e in sel if not e.is_dir)
            s += f"    {len(sel)} item{'s' if len(sel) != 1 else ''} selected" + (f"  {fmt_size(total)}" if total else "")
        if t.search and t.search.is_alive():
            s += "    Searching…"
        elif getattr(t, "partial", False) and getattr(t, "indexed_paint", False):
            s = f"{n} item{'s' if n != 1 else ''}    Verifying against the disk…"
        elif getattr(t, "partial", False):
            s = f"Reading folder…  {getattr(t, 'partial_count', 0):,} items so far"
        elif t.busy:
            s = f"Reading {self.display_path(t.path)}…"
        elif t.loading:
            s += "    Loading…"
        if self.clipboard["mode"]:
            s += f"    [{len(self.clipboard['paths'])} {self.clipboard['mode']}]"
        self.status.set_text(s)

    def flash(self, msg: str):
        self.status.set_text(msg)
        if self.status_timer:
            GLib.source_remove(self.status_timer)
        self.status_timer = GLib.timeout_add(3500, self._flash_end)

    def _flash_end(self):
        self.status_timer = 0
        self._update_status()
        return False

    # ------------------------------------------------------------------ navigation
    def navigate(self, path: str, record: bool = True, tab: Optional[FolderView] = None):
        self._nav_preview = None
        t = tab or self.tab
        if t.search:
            t.stop_search()
            self.searchbox.set_text("")
        if path not in VIRTUAL:
            path = os.path.abspath(path)
            # local paths are checked here (cheap); a network path is not stat'ed on the GTK thread — the
            # listing thread reports a missing folder in the view instead
            if not is_network_path(path) and not os.path.isdir(path):
                self.flash(f"Cannot open: {path}")
                return
        if t is None:
            return
        NAV_LOG.append({"path": path, "t0": time.perf_counter(), "listed": None, "shown": None, "n": 0,
                        "threads": threading.active_count(), "stalls_before": len(WATCHDOG.records)})
        del NAV_LOG[:-400]
        if record and t.path != path:
            t.back.append(t.path)
            t.fwd.clear()
        cur = t.selected_entries()
        if cur:
            t.remember[t.path] = cur[0].name
        old_virtual = t.path in VIRTUAL
        t.path = path
        if path not in VIRTUAL:
            self._bump_frequent(path)
        t.apply_layout()
        t.load(keep_selection=False)
        name = t.remember.get(path)
        if name:
            GLib.timeout_add(250, lambda: (t.select_names([name]) if t.path == path else None) or False)
        if t is self.tab:
            self._refresh_crumbs()
            self._update_history_buttons()
            self._update_title()
        if t.page:
            t.page.update_label()

    def _bump_frequent(self, path: str):
        fq = self.cfg["frequent"]
        fq[path] = fq.get(path, 0) + 1
        if len(fq) > 300:
            for p, _ in sorted(fq.items(), key=lambda kv: kv[1])[:100]:
                del fq[p]

    def frequent_folders(self) -> List[str]:
        return [p for p, _ in sorted(self.cfg["frequent"].items(), key=lambda kv: -kv[1]) if PATHS.isdir(p) and p != HOME]

    def go_back(self, n: int = 1):
        t = self.tab
        for _ in range(n):
            if not t.back:
                return
            t.fwd.append(t.path)
            target = t.back.pop()
            if _ == n - 1:
                self.navigate(target, record=False)
            else:
                t.path = target

    def go_forward(self):
        t = self.tab
        if t.fwd:
            t.back.append(t.path)
            self.navigate(t.fwd.pop(), record=False)

    def go_up(self):
        t = self.tab
        if t.path in VIRTUAL:
            if t.path != THISPC:
                self.navigate(THISPC)
            return
        parent = os.path.dirname(t.path.rstrip("/")) or "/"
        child = t.path
        if parent == t.path:
            self.navigate(THISPC)
        else:
            t.remember[parent] = os.path.basename(child)
            self.navigate(parent)

    def activate_entry(self, e: Optional[Entry]):
        if e is None:
            return
        t = self.tab
        if t.path == NETWORK and not e.path and e.uri:
            self.open_network_entry(e)
            return
        if getattr(e, "volume", None) is not None:
            self.mount_volume(e.volume)
            return
        if e.path and not e.uri and t.path not in VIRTUAL and not os.path.lexists(e.path):
            self.flash(f"{e.name} is no longer there")       # e.g. opened from an index paint before the disk answered
            t.load()
            return
        if e.is_dir:
            if self.cfg["browse_in_new_tab"] and t.path not in VIRTUAL:
                self.new_tab(e.path)
            else:
                self.navigate(e.path)
            return
        if t.path == TRASH:
            self.flash("Items in the Recycle Bin can't be opened. Restore it first.")
            return
        if self.cfg.get("archive_action", "extract") == "extract" and self.is_archive(e) and e.path and os.path.isdir(os.path.dirname(e.path)):
            self.extract_here(e)          # Explorer-style: an archive opens as a folder — here, by extracting it beside itself
            return
        self.open_file(e)

    def open_file(self, e: Entry):
        rec = self.cfg["recent"]
        if e.path in rec:
            rec.remove(e.path)
        rec.insert(0, e.path)
        del rec[30:]
        if getattr(self, "nav_recent", None) is not None:
            self._fill_recent(self.nav_recent)
        if not launch_default(e.uri or e.path, self):
            self.flash("No application is registered to open this file. Try Open with…")

    def open_with(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        d = Gtk.AppChooserDialog.new(self, Gtk.DialogFlags.MODAL, e.gfile)
        d.set_heading(f"How do you want to open {e.name}?")
        w = d.get_widget()
        w.set_show_other(True)
        w.set_show_default(True)
        if d.run() == Gtk.ResponseType.OK:
            app = d.get_app_info()
            if app:
                try:
                    app.launch([e.gfile], self.get_display().get_app_launch_context())
                except GLib.Error as ex:
                    message(self, "Open with", ex.message, Gtk.MessageType.ERROR)
        d.destroy()

    def open_terminal(self, cwd: Optional[str] = None):
        cwd = cwd or (self.tab.path if self.tab.path not in VIRTUAL else HOME)
        if not spawn_terminal(cwd):
            message(self, "Open terminal", "No terminal emulator was found. Set $TERMINAL or install one.", Gtk.MessageType.WARNING)

    def open_shell_here(self):
        """'Open command prompt here' — same as a terminal in this folder."""
        self.open_terminal()

    # ------------------------------------------------------------------ virtual folders
    def display_path(self, path: str) -> str:
        return {QUICK: "Quick access", THISPC: "This PC", TRASH: "Recycle Bin", NETWORK: "Network", RECENT: "Recent"}.get(path) or \
            ("Local Disk (/)" if path == "/" else (os.path.basename(HOME) if path == HOME else (os.path.basename(path.rstrip("/")) or path)))

    def display_name(self, e: Entry) -> str:
        n = e.name
        if self.cfg["hide_extensions"] and not e.is_dir and e.ext and os.path.splitext(n)[0]:
            n = os.path.splitext(n)[0]
        return n

    def cell_text(self, e: Entry, key: str) -> str:
        f = self.cfg["date_format"]
        if key == "modified":
            return fmt_date(e.mtime, f)
        if key == "created":
            return fmt_date(e.ctime, f)
        if key == "accessed":
            return fmt_date(e.atime, f)
        if key == "type":
            return e.type
        if key == "size":
            return fmt_size(e.size, self.cfg["size_in_bytes"])
        if key == "owner":
            return e.owner
        if key == "perms":
            return _stat.filemode(e.mode)[1:] if e.mode else ""
        if key == "location":
            return e.location
        if key == "deleted":
            return fmt_date(e.deleted, f)
        return ""

    def quick_entries(self) -> List[Entry]:
        out = []
        for p in self.cfg["pinned"]:
            if PATHS.isdir(p):
                e = entry_for_path(p)
                e.section = "Pinned folders"
                out.append(e)
        if self.cfg["show_frequent"]:
            for p in self.frequent_folders()[:8]:
                if p not in self.cfg["pinned"]:
                    e = entry_for_path(p)
                    e.section = "Frequent folders"
                    e.location = os.path.dirname(p)
                    out.append(e)
        if self.cfg["show_recent"]:
            for p in self.cfg["recent"][:20]:
                if PATHS.exists(p):
                    e = entry_for_path(p)
                    e.location = os.path.dirname(p)
                    e.section = "Recent files"
                    out.append(e)
        return out

    def recent_entries(self) -> List[Entry]:
        out = []
        for p in self.cfg["recent"]:
            if PATHS.exists(p):
                e = entry_for_path(p)
                e.location = os.path.dirname(p)
                e.section = "Recent files"
                out.append(e)
        return out

    def pc_entries(self) -> List[Entry]:
        out = []
        for label, p, icon in xdg_user_dirs():
            e = entry_for_path(p, label)
            e.icon = Gio.ThemedIcon.new(icon)
            e.section = "Folders"
            out.append(e)
        # Devices and drives, grouped by the PHYSICAL disk each mount lives on — Linux shows mounts, not disks,
        # so a section per disk ("Samsung SSD 980 PRO 2TB · NVME · 2.0 TB") makes it visible which paths share
        # hardware. Mounts lsblk can't place (network, FUSE, virtual) fall into their own sections.
        drive_entries = []
        by_disk: Dict[str, List[Entry]] = {}
        loose: List[Entry] = []
        nets: List[Entry] = []
        disks = physical_disks()
        for label, p, icon, k in drives():
            e = entry_for_path(p, label)
            e.icon = icon
            e.drive_kind = k
            if k == "network":
                e.section = "Network locations"
                e.type = f"Network drive  ·  {_mount_source(p) or p}"
                nets.append(e)
                continue
            e.type = "…"
            hit = disk_for_mount(p)
            if hit:
                d, part = hit
                e.section = f"{d['model']}  ·  {d['tran'] or 'internal'}  ·  {fmt_size(d['size'])}" + ("  ·  removable" if d["removable"] else "")
                e.location = f"{part['name']}" + (f"  {part['fstype']}" if part["fstype"] else "")
                by_disk.setdefault(d["name"], []).append(e)
            else:
                e.section = "Other mounts"
                loose.append(e)
            drive_entries.append(e)
        # unmounted partitions: shown greyed under their disk as information, not as drives (dual-boot rule);
        # removable ones are offered to mount
        mountable = {vol.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE): (label, vol, vicon) for label, vol, vicon in volumes_unmounted()}
        for d in disks:
            sec_entries = by_disk.get(d["name"])
            extra = []
            for part in d["parts"]:
                if part["mountpoints"]:
                    continue
                dev = "/dev/" + part["name"]
                if dev in mountable:
                    label, vol, vicon = mountable[dev]
                    e = Entry()
                    e.name, e.is_dir, e.icon = label, True, vicon
                    e.type = "Not mounted"
                    e.volume, e.drive_kind = vol, "volume"
                    e.location = part["name"]
                    extra.append(e)
                elif part["fstype"] and part["fstype"] not in ("swap", "LVM2_member", "crypto_LUKS", "linux_raid_member"):
                    e = Entry()
                    e.name = f"{part['label'] or part['name']}  (not mounted)"
                    e.is_dir = False
                    e.icon = Gio.ThemedIcon.new("drive-harddisk-symbolic")
                    e.type = f"{part['fstype']} · {fmt_size(part['size'])}"
                    e.drive_kind = "unmounted"
                    e.location = part["name"]
                    extra.append(e)
            if sec_entries or extra:
                sec = f"{d['model']}  ·  {d['tran'] or 'internal'}  ·  {fmt_size(d['size'])}" + ("  ·  removable" if d["removable"] else "")
                for e in extra:
                    e.section = sec
                out.extend((sec_entries or []) + extra)
        out.extend(loose)
        out.extend(nets)
        # free-space queries can hang on a dead network mount: never do them on the main thread
        t = self.tab

        def work():
            for e in drive_entries:
                try:
                    u = disk_usage(e.path)
                    e.usage = (u.total - u.free, u.total)
                    e.type = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                except OSError:
                    e.type = "unavailable"
            GLib.idle_add(lambda: (t and t.path == THISPC and (touch_all(t.store), t.pcview.refresh())) or False)
        threading.Thread(target=work, daemon=True).start()
        return out

    # ------------------------------------------------------------------ view settings
    def sort_by(self, key: str):
        if self.cfg["sort_key"] == key:
            self.cfg["sort_desc"] = not self.cfg["sort_desc"]
        else:
            self.cfg["sort_key"], self.cfg["sort_desc"] = key, False
        for t in self._tabs():
            t.rebuild_columns()
            t.resort()

    def set_sort_desc(self, desc: bool):
        self.cfg["sort_desc"] = desc
        for t in self._tabs():
            t.resort()

    def set_group(self, g: str):
        self.cfg["group_by"] = g
        for t in self._tabs():
            t.resort()

    def set_layout(self, lay: str):
        """Changes the active pane only (so split panes can differ); also becomes the default for new panes."""
        self.cfg["layout"] = lay
        t = self.tab
        if t is not None:
            t.layout = lay
            t.apply_layout()
            t.resort()

    def toggle_column(self, key: str):
        cols = self.cfg["columns"]
        if key in cols:
            cols.remove(key)
        else:
            cols.append(key)
        for t in self._tabs():
            t.rebuild_columns()

    def toggle_cfg(self, key: str):
        self.cfg[key] = not self.cfg[key]
        self.apply_cfg()

    def apply_cfg(self):
        want_gl = bool(self.cfg.get("gl_preview", True) and HAVE_GL)
        have_gl = isinstance(self.model_view, GLModelView) if self.model_view is not None else want_gl
        if want_gl != have_gl:
            self.cfg["gl_preview"] = not self.cfg["gl_preview"]
            self.toggle_gl_preview()         # flips it back and swaps the viewer in place
        self._apply_panes()
        for t in self._tabs():
            t.apply_layout()
        self.rebuild_nav()
        self.reload_all()
        self._update_title()
        save_config(self.cfg)

    def _tabs(self) -> List[FolderView]:
        return [fv for pg in self._pages() for fv in pg.panes]

    def reload_all(self):
        for t in self._tabs():
            if not t.search:
                t.load()

    # ------------------------------------------------------------------ search
    def start_search(self, text: str):
        text = text.strip()
        if not text:
            self.clear_search()
            return
        t = self.tab
        t.start_search(text)
        t.apply_layout()
        self._update_title()
        self.on_tab_loaded(t)

    def clear_search(self):
        t = self.tab
        if t.search:
            t.stop_search()
            t.load()
        self.searchbox.set_text("")
        self._update_title()
        self.on_tab_loaded(t)

    # ------------------------------------------------------------------ keys & mouse
    def _on_key(self, win, event):
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.TextView)) or self.editing:
            if self.propagate_key_event(event):
                return True
            return self.activate_key(event)
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if key == Gdk.KEY_F10 and mods == 0:
            self.menubar.select_first(True)
            return True
        return False

    def on_view_key(self, view, event, t: FolderView):
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift, alt = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK), bool(mods & Gdk.ModifierType.MOD1_MASK)
        if key == Gdk.KEY_BackSpace and not mods:
            self.go_back()
            return True
        if key == Gdk.KEY_Delete:
            self.delete(permanent=shift)
            return True
        if key == Gdk.KEY_F2 and not mods:
            t.start_rename()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and alt:
            self.properties()
            return True
        if key == Gdk.KEY_Menu or (key == Gdk.KEY_F10 and shift):
            self.context_menu(t, None)
            return True
        if key == Gdk.KEY_Escape:
            if t.search:
                self.clear_search()
            else:
                t.select_none()
            return True
        if key == Gdk.KEY_space and ctrl:
            p = cursor_path(view)
            if p is not None:
                if view is t.tree:
                    s = view.get_selection()
                    (s.unselect_path if s.path_is_selected(p) else s.select_path)(p)
                else:
                    (view.unselect_path if view.path_is_selected(p) else view.select_path)(p)
            return True
        if view is t.icons and not mods and event.string and event.string.isprintable():
            self._icon_typeahead(t, event.string)
            return True
        return False

    def _icon_typeahead(self, t: FolderView, ch: str):
        now = time.time()
        if now - getattr(self, "_ta_time", 0) > 1.0:
            self._ta = ""
        self._ta = getattr(self, "_ta", "") + ch
        self._ta_time = now
        q = self._ta.lower()
        cur = cursor_path(t.icons)
        start = (cur.get_indices()[0] + (0 if len(self._ta) > 1 else 1)) if cur else 0
        n = len(t.store)
        for off in range(n):
            i = (start + off) % n
            e = t.entry_at(Gtk.TreePath.new_from_indices([i]))
            if e and e.name.lower().startswith(q):
                p = Gtk.TreePath.new_from_indices([i])
                t.icons.unselect_all()
                t.icons.select_path(p)
                t.icons.set_cursor(p, None, False)
                t.icons.scroll_to_path(p, False, 0, 0)
                return

    def _on_button(self, win, event):
        if event.button == 8:
            self.go_back()
            return True
        if event.button == 9:
            self.go_forward()
            return True
        return False

    def _on_close(self, *a):
        if not self.prompt_save():
            return True        # keep the window open
        if getattr(self, "video", None) is not None:
            self.video.stop()
        self.cfg["window"] = list(self.get_size())
        self.cfg["nav_width"] = self.paned.get_position()
        self._remember_side_width()
        self._remember_side_split()
        save_config(self.cfg)
        return False


    # ------------------------------------------------------------------ menus
    def _build_menubar(self):
        for i, (key, title) in enumerate(self.MENU_KEYS.items()):
            item = Gtk.MenuItem.new_with_mnemonic("_" + title)
            menu = Gtk.Menu()
            item.set_submenu(menu)
            menu.connect("show", lambda m, k=key: self._fill_menu(m, k))
            self.menubar.append(item)
        self.menubar.show_all()
        # global accelerators (registered once; menu items are rebuilt dynamically)
        for accel, cb in (("<Control>t", lambda: self.new_tab(self.tab.path if self.tab.path not in VIRTUAL else HOME)),
                          ("<Control>w", lambda: self.close_tab()), ("<Control>n", lambda: self.app.new_window(self.tab.path)),
                          ("<Control>q", self.close), ("<Control>l", self.edit_address), ("F4", self.edit_address),
                          ("<Alt>d", self.edit_address), ("<Control>e", lambda: self.searchbox.grab_focus()),
                          ("<Control>f", lambda: self.searchbox.grab_focus()), ("F3", lambda: self.searchbox.grab_focus()),
                          ("F5", self.reload_all), ("<Control>r", self.reload_all), ("<Control><Shift>r", self.open_in_rv), ("F1", lambda: show_shortcuts(self)),
                          ("<Alt>Left", self.go_back), ("<Alt>Right", self.go_forward), ("<Alt>Up", self.go_up),
                          ("<Control>c", lambda: self.copy_sel("copy")), ("<Control>x", lambda: self.copy_sel("cut")),
                          ("<Control>v", lambda: self.paste()), ("<Control><Shift>c", self.copy_path),
                          ("<Control>a", lambda: self.tab.select_all()), ("<Control>z", self.undo),
                          ("<Control><Shift>n", self.new_folder), ("<Control>h", lambda: self.toggle_cfg("show_hidden")),
                          ("<Alt>p", lambda: self.toggle_cfg("preview_pane")), ("<Alt><Shift>p", lambda: self.toggle_cfg("details_pane")),
                          ("<Control>o", self.open_with), ("<Control>Tab", lambda: self.notebook.next_page()),
                          ("<Control><Shift>Tab", lambda: self.notebook.prev_page()), ("<Control>Page_Down", lambda: self.notebook.next_page()),
                          ("<Control>Page_Up", lambda: self.notebook.prev_page()), ("F11", self._toggle_fullscreen),
                          ("<Alt>Return", self.properties), ("<Control>j", lambda: self.open_terminal()),
                          ("<Control><Shift>s", self.toggle_split), ("<Control>F2", self.batch_rename)):
            key, mods = Gtk.accelerator_parse(accel)
            self.accel.connect(key, mods, Gtk.AccelFlags.VISIBLE, lambda g, o, k, m, c=cb: (c(), True)[1])
        for i in range(1, 10):
            key, mods = Gtk.accelerator_parse(f"<Control>{i}")
            self.accel.connect(key, mods, Gtk.AccelFlags.VISIBLE, lambda g, o, k, m, n=i: (self.notebook.set_current_page(n - 1), True)[1])

    def _toggle_fullscreen(self):
        st = self.get_window().get_state() if self.get_window() else 0
        if st & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fill_menu(self, menu: Gtk.Menu, key: str):
        for c in menu.get_children():
            menu.remove(c)
        for s in self.menu_spec(key):
            if s == "-":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if isinstance(s, tuple):
                s = {"label": s[0], "cb": s[1] if len(s) > 1 else None, "accel": s[2] if len(s) > 2 else ""}
            sub = build_menu(s["sub"]) if "sub" in s else None
            menu.append(_menu_item(s["label"], s.get("cb"), s.get("accel", ""), s.get("check"), s.get("radio"), s.get("sensitive", True), sub))
        menu.show_all()

    def menu_spec(self, key: str):
        cfg = self.cfg
        t = self.tab
        if t is None:
            return []
        sel = t.selected_entries()
        e = sel[0] if sel else None
        real = t.path not in VIRTUAL
        has = bool(sel) and t.path not in (QUICK, THISPC)
        can_paste = real and (bool(self.clipboard["paths"]) or self._external_clipboard_has_files())
        if key == "file":
            return [("New _tab", lambda: self.new_tab(t.path if real else HOME), "<Control>t"),
                    ("New _window", lambda: self.app.new_window(t.path if real else HOME), "<Control>n"), "-",
                    ("Open _terminal here", lambda: self.open_terminal(), "<Control>j"),
                    ("Connect to _server…", self.connect_to_server), "-",
                    ("_Preferences…", lambda: self.folder_options(0)),
                    ("_Help", lambda: show_shortcuts(self), "F1"), "-",
                    ("_Close tab", lambda: self.close_tab(), "<Control>w"), ("E_xit", self.close, "<Control>q")]
        if key == "home":
            target = e.path if (e and e.is_dir) else t.path
            pinned = target in cfg["pinned"]
            return [{"label": "_Unpin from Quick access" if pinned else "_Pin to Quick access",
                     "cb": (lambda: self.unpin(target)) if pinned else (lambda: self.pin(target)), "sensitive": real}, "-",
                    {"label": "_Copy", "cb": lambda: self.copy_sel("copy"), "accel": "<Control>c", "sensitive": has},
                    {"label": "_Paste", "cb": lambda: self.paste(), "accel": "<Control>v", "sensitive": can_paste},
                    {"label": "Cu_t", "cb": lambda: self.copy_sel("cut"), "accel": "<Control>x", "sensitive": has},
                    {"label": "Copy pat_h", "cb": self.copy_path, "accel": "<Control><Shift>c", "sensitive": has},
                    {"label": "Paste _shortcut", "cb": lambda: self.paste(shortcut=True), "sensitive": can_paste}, "-",
                    {"label": "_Move to…", "cb": lambda: self.move_or_copy_to("move"), "sensitive": has and real},
                    {"label": "Cop_y to…", "cb": lambda: self.move_or_copy_to("copy"), "sensitive": has and real},
                    {"label": "_Delete", "cb": lambda: self.delete(), "accel": "Delete", "sensitive": has},
                    {"label": "Delete permanently", "cb": lambda: self.delete(True), "accel": "<Shift>Delete", "sensitive": has},
                    {"label": "_Rename", "cb": lambda: t.start_rename(), "accel": "F2", "sensitive": has and real}, "-",
                    {"label": "New _folder", "cb": self.new_folder, "accel": "<Control><Shift>n", "sensitive": real},
                    {"label": "New _item", "sub": self._new_item_spec(), "sensitive": real}, "-",
                    ("P_roperties", self.properties, "<Alt>Return"),
                    {"label": "_Open", "cb": lambda: self.activate_entry(e), "accel": "Return", "sensitive": e is not None},
                    {"label": "Open _with…", "cb": self.open_with, "accel": "<Control>o", "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Edit", "cb": self.edit_file, "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Run in terminal", "cb": lambda: self.run_script(e, True), "sensitive": bool(e and not e.is_dir and script_runner(e.path))}, "-",
                    ("Select _all", lambda: t.select_all(), "<Control>a"), ("Select _none", lambda: t.select_none()),
                    ("_Invert selection", lambda: t.invert_selection())]
        if key == "share":
            return [{"label": "_Share", "sub": self.share_spec()},
                    {"label": "_Zip (compress)", "cb": self.zip_selection, "sensitive": has and real},
                    {"label": "E_xtract here", "cb": self.extract_here, "sensitive": self.is_archive(e)},
                    {"label": "Extract _to…", "cb": self.extract_all, "sensitive": self.is_archive(e)},
                    {"label": "_Print", "cb": self.print_files, "sensitive": has}]
        if key == "view":
            lay = t.layout
            layouts, sorts, groups, cols, lgroup = [], [], [], [], []
            for name, k in (("E_xtra large icons", "xlarge"), ("_Large icons", "large"), ("_Medium icons", "medium"),
                            ("_Small icons", "small"), ("L_ist", "list"), ("_Details", "details"), ("_Tiles", "tiles"), ("_Content", "content")):
                layouts.append({"label": name, "cb": lambda k=k: self.set_layout(k), "check": lay == k, "radio": lgroup})
            sgroup = []
            for name, k in (("_Name", "name"), ("Date _modified", "modified"), ("_Type", "type"), ("_Size", "size"),
                            ("Date _created", "created"), ("Date _accessed", "accessed"), ("_Owner", "owner")):
                sorts.append({"label": name, "cb": lambda k=k: (self.sort_by(k) if cfg["sort_key"] != k else None), "check": cfg["sort_key"] == k, "radio": sgroup})
            dgroup = []
            sorts += ["-", {"label": "_Ascending", "cb": lambda: self.set_sort_desc(False), "check": not cfg["sort_desc"], "radio": dgroup},
                      {"label": "_Descending", "cb": lambda: self.set_sort_desc(True), "check": cfg["sort_desc"], "radio": dgroup}]
            ggroup = []
            for name, k in (("(_None)", "none"), ("_Name", "name"), ("Date _modified", "modified"), ("_Type", "type"),
                            ("_Size", "size"), ("Date _created", "created"), ("_Owner", "owner")):
                groups.append({"label": name, "cb": lambda k=k: self.set_group(k), "check": cfg["group_by"] == k, "radio": ggroup})
            for name, k in (("Date _modified", "modified"), ("_Type", "type"), ("_Size", "size"), ("Date _created", "created"),
                            ("Date _accessed", "accessed"), ("_Owner", "owner"), ("_Permissions", "perms")):
                cols.append({"label": name, "cb": lambda k=k: self.toggle_column(k), "check": k in cfg["columns"]})
            return [{"label": "_Navigation pane", "cb": lambda: self.toggle_cfg("nav_pane"), "check": cfg["nav_pane"]},
                    {"label": "_Preview pane", "cb": lambda: self.toggle_cfg("preview_pane"), "accel": "<Alt>p", "check": cfg["preview_pane"]},
                    {"label": "_Details pane", "cb": lambda: self.toggle_cfg("details_pane"), "accel": "<Alt><Shift>p", "check": cfg["details_pane"]},
                    {"label": "Split _view (two folders)", "cb": self.toggle_split, "accel": "<Control><Shift>s", "check": bool(self.page and self.page.split)}, "-",
                    {"label": "_Layout", "sub": layouts}, {"label": "_Sort by", "sub": sorts}, {"label": "_Group by", "sub": groups},
                    {"label": "Add _columns", "sub": cols},
                    ("Size all columns to _fit", lambda: [c.queue_resize() for c in t.tree.get_columns()] and None), "-",
                    {"label": "Item check _boxes", "cb": lambda: self.toggle_cfg("checkboxes"), "check": cfg["checkboxes"]},
                    {"label": "File name _extensions", "cb": lambda: self.toggle_cfg("hide_extensions"), "check": not cfg["hide_extensions"]},
                    {"label": "_Hidden items", "cb": lambda: self.toggle_cfg("show_hidden"), "accel": "<Control>h", "check": cfg["show_hidden"]}, "-",
                    {"label": "Hide selected _items", "cb": self.hide_selected, "sensitive": has and real}]
        if key == "tools":
            multi = len(sel) >= 1 and real
            return [{"label": "_Batch rename…", "cb": self.batch_rename, "accel": "<Control>F2", "sensitive": multi},
                    {"label": "Copy SHA-256 c_hecksum", "cb": self.copy_checksum, "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Run in terminal", "cb": lambda: self.run_script(e, True), "sensitive": bool(e and not e.is_dir and script_runner(e.path))}, "-",
                    ("Empty Recycle _Bin", self.empty_trash), "-",
                    ("_Performance report…", self.performance_report),
                    {"label": "_Update file index now", "cb": self.index_now, "sensitive": bool(cfg.get("index_enabled"))},
                    ("_Optional tools and integrations…", lambda: self.folder_options(3)), "-",
                    ("_Preferences…", lambda: self.folder_options(0))]
        return [("_View help (keyboard shortcuts)", lambda: show_shortcuts(self), "F1"), ("_About Kestrel", lambda: show_about(self))]

    def _new_item_spec(self):
        return [("_Folder", lambda: self.new_item("folder")), ("_Shortcut", lambda: self.new_item("shortcut")),
                ("_Text Document", lambda: self.new_item("text")), ("_Markdown Document", lambda: self.new_item("markdown")),
                ("S_hell Script", lambda: self.new_item("script")), ("_Python Script", lambda: self.new_item("python"))]

    def context_menu(self, t: FolderView, event):
        sel = t.selected_entries()
        e = sel[0] if sel else None
        real = t.path not in VIRTUAL
        can_paste = real and (bool(self.clipboard["paths"]) or self._external_clipboard_has_files())
        spec = []
        if t.path == TRASH:
            if e:
                spec += [("_Restore", self.restore), ("_Delete permanently", lambda: self.delete()), "-", ("P_roperties", self.properties), "-"]
            spec += [("_Empty Recycle Bin", self.empty_trash), ("Re_fresh", self.reload_all)]
        elif e:
            pinned = e.path in self.cfg["pinned"]
            item = bool(e.path) and os.path.exists(e.path)   # the item itself is a real file/folder (also true in Quick access)
            spec += [("_Open", lambda: self.activate_entry(e))]
            if t.path == NETWORK and e.path:
                spec += [("_Disconnect (unmount)", lambda: self.unmount(e.path))]
            if t.path == THISPC and getattr(e, "drive_kind", "") == "removable" and e.path:
                spec += [("_Eject", lambda: self.eject(e.path)), ("_Format…", lambda: self.format_drive(e.path, e.name)), "-"]
            if t.path == THISPC and getattr(e, "volume", None) is not None:
                spec = [("_Mount", lambda: self.mount_volume(e.volume))]
            if e.is_dir and e.path:
                spec += [("Open in new _tab", lambda: self.new_tab(e.path)), ("Open in new _window", lambda: self.app.new_window(e.path)),
                         ("Open in t_erminal", lambda: self.open_terminal(e.path)),
                         ("_Unpin from Quick access" if pinned else "_Pin to Quick access", (lambda: self.unpin(e.path)) if pinned else (lambda: self.pin(e.path)))]
            else:
                spec += [("Open _with…", self.open_with), ("_Edit", self.edit_file)]
                if self.cfg.get("rv_enabled") and any((x.ext or "").lower() in RV_IMAGE_EXTS for x in sel if not x.is_dir):
                    spec += [("Open in _RV", self.open_in_rv, "<Control><Shift>r")]
                    if len(sel) == 1 and rv_sequence_for(e.path or ""):
                        spec += [("Open image se_quence in RV", lambda: self.open_in_rv(sequence=True))]
                if script_runner(e.path):
                    spec += ["-", ("_Run in terminal", lambda: self.run_script(e, True)),
                             ("Run (_no window)", lambda: self.run_script(e, False)), "-"]
                if self.is_archive(e):
                    spec += [("E_xtract here", lambda: self.extract_here(e)), ("Extract _to…", self.extract_all),
                             ("Open with _default app", lambda: self.open_file(e))]
            spec += ["-", {"label": "Se_nd to", "sub": [{"label": "_Compressed (zipped) folder", "cb": self.zip_selection, "sensitive": real},
                                                        {"label": "_Desktop (create shortcut)", "cb": lambda: self.send_to_desktop(e), "sensitive": item},
                                                        ("_Mail recipient", self.share_email)]}, "-",
                     {"label": "Cu_t", "cb": lambda: self.copy_sel("cut"), "sensitive": item}, {"label": "_Copy", "cb": lambda: self.copy_sel("copy"), "sensitive": item},
                     {"label": "_Paste", "cb": lambda: self.paste(), "sensitive": can_paste}, ("Copy as _path", self.copy_path), "-",
                     {"label": "Create _shortcut", "cb": lambda: self.shortcut_here(e), "sensitive": real},
                     {"label": "Unpin from Quick access" if t.path == QUICK and e.is_dir else "_Delete", "cb": lambda: self.delete(), "sensitive": item and t.path != THISPC},
                     {"label": "_Rename" if len(sel) == 1 else "_Batch rename…", "cb": lambda: t.start_rename(), "sensitive": real}, "-", ("P_roperties", self.properties)]
        elif t.path == NETWORK:
            spec += [("_Connect to server…", self.connect_to_server), ("Re_fresh", lambda: (self.rebuild_nav(), self.reload_all())), "-",
                     {"label": "_View", "sub": self.menu_spec("view")[5]["sub"]}]
        else:
            view_spec = self.menu_spec("view")
            spec += [{"label": "_View", "sub": view_spec[5]["sub"]}, {"label": "_Sort by", "sub": view_spec[6]["sub"]},
                     {"label": "_Group by", "sub": view_spec[7]["sub"]}, ("Re_fresh", self.reload_all), "-",
                     {"label": "_Paste", "cb": lambda: self.paste(), "sensitive": can_paste},
                     {"label": "Paste _shortcut", "cb": lambda: self.paste(shortcut=True), "sensitive": can_paste},
                     {"label": "_Undo", "cb": self.undo, "sensitive": bool(self.undo_stack)}, "-",
                     ("Open in _terminal", lambda: self.open_terminal()), {"label": "Ne_w", "sub": self._new_item_spec(), "sensitive": real}, "-",
                     ("P_roperties", lambda: self.show_properties([entry_for_path(t.path)]) if real else self.properties())]
        menu = build_menu(spec)
        menu.attach_to_widget(t.view, None)
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(t.view, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH_WEST, None)

    # ------------------------------------------------------------------ clipboard
    def copy_sel(self, mode: str):
        t = self.tab
        sel = t.selected_entries()
        if not sel or t.path == THISPC:
            return
        paths = [e.path for e in sel if e.path and os.path.exists(e.path)]
        if not paths:
            return
        old = list(self.clipboard["paths"])
        self.clipboard = {"mode": mode, "paths": paths}
        self._export_clipboard(mode, paths)
        for fv in self._tabs():
            fv.refresh_rows(set(old) | set(paths))
        self.flash(f"{len(paths)} item{'s' if len(paths) != 1 else ''} {'copied' if mode == 'copy' else 'cut'}")
        self._update_status()

    def _export_clipboard(self, mode: str, paths: List[str]):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
        targets = [Gtk.TargetEntry.new("x-special/gnome-copied-files", 0, 0), Gtk.TargetEntry.new("text/uri-list", 0, 1),
                   Gtk.TargetEntry.new("UTF8_STRING", 0, 2), Gtk.TargetEntry.new("text/plain", 0, 3)]

        def get(cb_, sd, info, data):
            if info == 0:
                sd.set(Gdk.Atom.intern("x-special/gnome-copied-files", False), 8, (mode + "\n" + "\n".join(uris)).encode())
            elif info == 1:
                sd.set_uris(uris)
            else:
                sd.set_text("\n".join(paths), -1)
        try:
            cb.set_with_data(targets, get, lambda c, d: None, None)
        except Exception:
            cb.set_text("\n".join(paths), -1)

    def _external_clipboard_has_files(self) -> bool:
        try:
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            return cb.wait_is_target_available(Gdk.Atom.intern("x-special/gnome-copied-files", False)) or \
                cb.wait_is_target_available(Gdk.Atom.intern("text/uri-list", False))
        except Exception:
            return False

    def _read_external_clipboard(self) -> Tuple[str, List[str]]:
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        try:
            sd = cb.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
            if sd:
                lines = bytes(sd.get_data()).decode("utf-8", "replace").split("\n")
                mode = "cut" if lines and lines[0].strip() == "cut" else "copy"
                paths = [Gio.File.new_for_uri(u).get_path() for u in lines[1:] if u.strip()]
                return mode, [p for p in paths if p]
            uris = cb.wait_for_uris()
            if uris:
                return "copy", [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        except Exception:
            pass
        return "", []

    def copy_path(self):
        sel = self.tab.selected_entries()
        if sel:
            set_clipboard_text("\n".join(f'"{e.path}"' if " " in e.path else e.path for e in sel))
            self.flash("Path copied: " + (sel[0].path if len(sel) == 1 else f"{len(sel)} paths"))

    def paste(self, dest: Optional[str] = None, shortcut: bool = False):
        t = self.tab
        dest = dest or t.path
        if dest in VIRTUAL:
            self.flash("Can't paste here.")
            return
        mode, paths = self.clipboard["mode"], list(self.clipboard["paths"])
        if not paths:
            mode, paths = self._read_external_clipboard()
        paths = [p for p in paths if os.path.lexists(p)]
        if not paths:
            self.flash("Nothing to paste.")
            return
        if shortcut:
            for p in paths:
                link = unique_name(os.path.join(dest, os.path.basename(p) + " - Shortcut"))
                try:
                    os.symlink(p, link)
                except OSError as ex:
                    message(self, "Create shortcut", str(ex), Gtk.MessageType.ERROR)
            return
        if self.transfer(paths, dest, mode or "copy") and mode == "cut":
            self.clipboard = {"mode": "", "paths": []}
            self._update_status()

    def ask_drop_action(self) -> Optional[bool]:
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION, text="Drop files")
        d.add_buttons("_Move here", 1, "_Copy here", 2, "_Cancel", Gtk.ResponseType.CANCEL)
        r = d.run()
        d.destroy()
        return {1: True, 2: False}.get(r)

    # ------------------------------------------------------------------ file operations
    def transfer(self, paths: List[str], dest: str, kind: str) -> bool:
        paths = [os.path.abspath(p) for p in paths if p]
        if kind == "move" and all(os.path.dirname(p) == dest for p in paths):
            self.flash("Source and destination are the same.")
            return False
        for p in paths:
            if os.path.isdir(p) and (dest == p or dest.startswith(p.rstrip("/") + "/")):
                message(self, "Interrupted Action", "The destination folder is a subfolder of the source folder.", Gtk.MessageType.ERROR)
                return False
        plan = self._resolve_conflicts(paths, dest, kind)
        if plan is None:
            return False
        self.run_op(kind, paths, dest, plan)
        return True

    def _resolve_conflicts(self, paths: List[str], dest: str, mode: str) -> Optional[Dict[str, str]]:
        plan: Dict[str, str] = {}
        apply_all: Optional[str] = None
        for p in paths:
            d = os.path.join(dest, os.path.basename(p.rstrip("/")))
            if not os.path.lexists(d):
                continue
            if mode == "copy" and os.path.abspath(p) == os.path.abspath(d):
                plan[p] = "keep"
                continue
            if apply_all:
                plan[p] = apply_all
                continue
            r, all_ = conflict_dialog(self, p, d)
            if r is None:
                return None
            plan[p] = r
            if all_:
                apply_all = r
        return plan

    def run_op(self, kind: str, paths: List[str], dest: Optional[str], plan: Optional[Dict[str, str]] = None):
        verb = {"copy": "Copying", "move": "Moving", "delete": "Deleting", "trash": "Recycling"}.get(kind, "Working on")
        title = f"{verb} {len(paths)} item{'s' if len(paths) != 1 else ''}" + (f" to {self.display_path(dest)}" if dest else "")
        op = FileOp(kind, paths, dest, plan or {}, self._op_done)
        self._start_op(op, title)

    def _start_op(self, op, title: str):
        op.title = title
        op.dialog = None
        op.start()
        GLib.timeout_add(120, self._maybe_show_progress, op)

    def _maybe_show_progress(self, op):
        if not op.finished and op.dialog is None:
            op.dialog = ProgressDialog(self, op, op.title)
        return False

    def _op_done(self, op: FileOp):
        if op.dialog is not None:
            op.dialog.destroy()
            op.dialog = None
        if op.errors:
            message(self, APP_NAME, "Some items could not be processed:\n" + "\n".join(op.errors[:10]), Gtk.MessageType.WARNING)
        if op.results and op.kind in ("move", "trash", "copy"):
            self.undo_stack.append((op.kind, op.results))
        if op.kind == "extract" and op.results and not op.cancel.is_cancelled():
            self.navigate(op.results[0][1])
        self.reload_all()
        self.flash(f"{op.title} — {'cancelled' if op.cancel.is_cancelled() else 'done'}")
        return False

    def delete(self, permanent: bool = False):
        t = self.tab
        sel = t.selected_entries()
        if not sel:
            return
        if t.path == QUICK:
            for e in sel:
                self.unpin(e.path)
            return
        if t.path == THISPC:
            return
        desc = f"'{sel[0].name}'" if len(sel) == 1 else f"these {len(sel)} items"
        if t.path == TRASH:
            if ask_yes_no(self, "Delete File", f"Are you sure you want to permanently delete {desc}?", True):
                for e in sel:
                    try:
                        self._delete_trash_item(e)
                    except GLib.Error as ex:
                        message(self, "Delete", ex.message, Gtk.MessageType.ERROR)
                self.reload_all()
            return
        paths = [e.path for e in sel if e.path]
        if permanent:
            if ask_yes_no(self, "Delete File", f"Are you sure you want to permanently delete {desc}?", True):
                self.run_op("delete", paths, None)
            return
        if self.cfg["confirm_delete"] and not ask_yes_no(self, "Delete File", f"Are you sure you want to move {desc} to the Recycle Bin?"):
            return
        self.run_op("trash", paths, None)

    @staticmethod
    def _delete_trash_item(e: Entry):
        f = e.gfile
        if e.is_dir:
            # GIO trash backend deletes whole directories only via the real files dir
            real = e.path
            if real and os.path.isdir(real):
                shutil.rmtree(real)
                info_file = os.path.join(os.path.dirname(os.path.dirname(real)), "info", e.name + ".trashinfo")
                try:
                    os.remove(info_file)
                except OSError:
                    pass
                return
        f.delete(None)

    def restore(self):
        t = self.tab
        if t.path != TRASH:
            return
        for e in t.selected_entries():
            if not e.orig:
                message(self, "Restore", f"No original location recorded for {e.name}.", Gtk.MessageType.WARNING)
                continue
            try:
                os.makedirs(os.path.dirname(e.orig), exist_ok=True)
                dest = unique_name(e.orig)
                e.gfile.move(Gio.File.new_for_path(dest), Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
                self.flash(f"Restored to {dest}")
            except GLib.Error as ex:
                message(self, "Restore", ex.message, Gtk.MessageType.ERROR)
        self.reload_all()

    def empty_trash(self):
        try:
            items = list_folder(TRASH, True)
        except GLib.Error:
            items = []
        if not items:
            self.flash("The Recycle Bin is empty.")
            return
        if ask_yes_no(self, "Delete Multiple Items", f"Are you sure you want to permanently delete these {len(items)} items?", True):
            for e in items:
                try:
                    self._delete_trash_item(e)
                except GLib.Error:
                    pass
            self.reload_all()

    def do_rename(self, old: str, new: str) -> bool:
        base = os.path.basename(new)
        if not base or "/" in base:
            message(self, "Rename", "A file name can't contain '/' or be empty.", Gtk.MessageType.ERROR)
            return False
        if os.path.lexists(new):
            message(self, "Rename File", "There is already a file with the same name in this location.", Gtk.MessageType.ERROR)
            return False
        try:
            os.rename(old, new)
        except OSError as ex:
            message(self, "Rename", str(ex), Gtk.MessageType.ERROR)
            return False
        self.undo_stack.append(("rename", (old, new)))
        t = self.tab
        t.load()
        GLib.timeout_add(250, lambda: t.select_names([base]) or False)
        return True

    def batch_rename(self):
        t = self.tab
        sel = [e for e in t.selected_entries() if e.path]
        if not sel or t.path in VIRTUAL:
            self.flash("Select the files to rename first.")
            return

        def done(plan):
            renamed, errors = apply_renames(plan)
            if renamed:
                self.undo_stack.append(("move", renamed))      # undo = move each back
                self.flash(f"Renamed {len(renamed)} item{'s' if len(renamed) != 1 else ''}")
            if errors:
                message(self, "Batch Rename", "Some items could not be renamed:\n" + "\n".join(errors[:10]), Gtk.MessageType.WARNING)
            t.load()
            GLib.timeout_add(300, lambda: t.select_names([os.path.basename(n) for _, n in renamed]) or False)
        BatchRenameDialog(self, sel, done)

    def preview_diagnostics(self):
        """Which converters / VFX Python modules Kestrel can find for previews."""
        d = Gtk.Dialog(title="Dependencies — status and diagnostics", transient_for=self, modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(720, 420)
        tv = Gtk.TextView(editable=False, monospace=True)
        tv.get_buffer().set_text("Checking… (mayapy can take a few seconds to start)")
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        d.get_content_area().pack_start(sw, True, True, 0)
        d.show_all()
        threading.Thread(target=lambda: GLib.idle_add(lambda: tv.get_buffer().set_text(preview_tool_report()) or False), daemon=True).start()
        d.run()
        d.destroy()

    def open_in_rv(self, sequence: bool = False):
        """Open the selected images / movies in OpenRV (Ctrl+Shift+R). With sequence=True, or when the single
        selected file is one frame of a numbered sequence and the others are not selected, RV gets the
        name.#.ext spec so it loads the whole sequence."""
        if not self.cfg.get("rv_enabled"):
            self.flash("OpenRV integration is off (Preferences → Optional)")
            return
        rv = find_rv(self.cfg.get("rv_path", ""))
        if not rv:
            self.flash("OpenRV not found (Preferences → Optional)")
            return
        sel = [x for x in self.tab.selected_entries() if not x.is_dir and x.path and (x.ext or "").lower() in RV_IMAGE_EXTS]
        if not sel:
            self.flash("Select images or movies to open in RV")
            return
        args = [x.path for x in sel]
        if sequence and len(sel) == 1:
            seq = rv_sequence_for(sel[0].path)
            if seq:
                args = [seq]
        cwd = os.path.dirname(args[0]) if os.path.isdir(os.path.dirname(args[0])) else HOME
        if run_detached([rv] + args, cwd):
            self.flash(f"Opening {len(args)} item{'s' if len(args) != 1 else ''} in RV")
        else:
            self.flash("Could not start RV")

    def index_now(self, full: bool = False):
        if not INDEX.loaded:
            threading.Thread(target=lambda: (INDEX.load(), GLib.idle_add(lambda: (self.index_now(full), False)[1])), daemon=True).start()
            return
        INDEX.on_status = lambda: self.flash(self.index_status_line())
        INDEX.crawl(self.cfg.get("index_roots", [HOME]), self.cfg.get("index_network", False), self.cfg.get("index_hidden", False), full)
        self.flash("Updating the file index in the background…")

    def index_status_line(self) -> str:
        st = INDEX.stats()
        if st["crawling"]:
            return f"Indexing… {INDEX.progress['dirs']:,} folders, {INDEX.progress['files']:,} items — {INDEX.progress['current']}"
        when = _dt.datetime.fromtimestamp(st["finished"]).strftime("%b %d %H:%M") if st["finished"] else "never"
        return f"Index: {st['files']:,} items in {st['dirs']:,} folders · {fmt_size(st['size'])} encrypted on disk · updated {when} · key in {st['key']}"

    def performance_report(self):
        """Tools → Performance report…: the stall watchdog's findings (always on), plus the cProfile summary
        when started with --profile. Refreshes while open."""
        d = Gtk.Dialog(title="Performance report", transient_for=self, modal=False)
        d.add_button("_Save to file…", 1)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(900, 600)
        tv = Gtk.TextView(editable=False, monospace=True)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        d.get_content_area().pack_start(sw, True, True, 0)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup("<small>Stalls are moments the interface thread was blocked for more than 120 ms; the stack shows what it was doing. "
                        "Run <tt>python3 kestrel.py --profile</tt> for a full cProfile of everything on the interface thread, "
                        "and <tt>python3 kestrel.py --selftest</tt> for timed unit/benchmark tests.</small>")
        d.get_content_area().pack_start(hint, False, False, 6)

        def refresh():
            if not d.get_visible():
                return False
            text = "==== recent navigations ====\n" + nav_report() + "\n\n==== interface stalls ====\n" + WATCHDOG.report()
            if PROFILE is not None:
                text += "\n\n==== cProfile (--profile) ====\n" + profiling_report(25)
            tv.get_buffer().set_text(text)
            return True
        refresh()
        GLib.timeout_add(2000, refresh)
        d.show_all()
        while True:
            r = d.run()
            if r == 1:
                out = os.path.join(GLib.get_user_cache_dir(), "kestrel", f"performance-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w") as f:
                    f.write(WATCHDOG.report() + ("\n\n" + profiling_report(60) if PROFILE is not None else ""))
                self.flash(f"Saved {out}")
                continue
            break
        d.destroy()

    def install_deps(self):
        """Preferences → Optional: tick the missing optional tools and run their install
        commands in a terminal (sudo prompts there, nothing runs silently)."""
        d = Gtk.Dialog(title="Install missing dependencies", transient_for=self, modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        copy_btn = d.add_button("_Copy commands", 1)
        run_btn = d.add_button("_Install selected in terminal", Gtk.ResponseType.OK)
        run_btn.get_style_context().add_class("suggested-action")
        d.set_default_size(680, 460)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Checking what's installed…", xalign=0), False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(grid)
        box.pack_start(sw, True, True, 0)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("<small>Commands run in a terminal window so you can see them and enter your sudo password. "
                        "EPEL is enabled automatically when needed. Restart Kestrel afterwards.</small>")
        box.pack_start(note, False, False, 0)
        checks: List[Tuple[Gtk.CheckButton, dict]] = []
        d.show_all()
        run_btn.set_sensitive(False)
        copy_btn.set_sensitive(False)

        def fill(deps):
            box.get_children()[0].set_text(f"{sum(1 for x in deps if x['found'])} of {len(deps)} optional tools found.")
            for i, dep in enumerate(deps):
                cb = Gtk.CheckButton()
                cb.set_active(dep["found"] is None)
                cb.set_sensitive(dep["found"] is None)
                grid.attach(cb, 0, i, 1, 1)
                name = Gtk.Label(xalign=0)
                name.set_markup(f"<b>{GLib.markup_escape_text(dep['name'])}</b>")
                grid.attach(name, 1, i, 1, 1)
                st = Gtk.Label(xalign=0)
                st.set_markup(f"<span foreground='#2e8b57'>installed</span>" if dep["found"] else "<span foreground='#c0392b'>missing</span>")
                grid.attach(st, 2, i, 1, 1)
                g = Gtk.Label(label=dep["gives"], xalign=0, wrap=True, hexpand=True)
                g.get_style_context().add_class("dim-label")
                grid.attach(g, 3, i, 1, 1)
                name.set_tooltip_text(dep["cmd"])
                checks.append((cb, dep))
            grid.show_all()
            run_btn.set_sensitive(True)
            copy_btn.set_sensitive(True)
            return False
        threading.Thread(target=lambda: GLib.idle_add(fill, optional_deps()), daemon=True).start()
        while True:
            resp = d.run()
            chosen = [dep for cb, dep in checks if cb.get_active() and dep["found"] is None]
            if resp == 1:
                set_clipboard_text("\n".join(dep["cmd"] for dep in chosen) or "# nothing selected")
                self.flash("Install commands copied")
                continue
            if resp == Gtk.ResponseType.OK and chosen:
                script = os.path.join(GLib.get_user_cache_dir(), "kestrel", "install-deps.sh")
                os.makedirs(os.path.dirname(script), exist_ok=True)
                with open(script, "w") as f:
                    f.write(install_script(chosen))
                os.chmod(script, 0o755)
                if not spawn_terminal(HOME, ["bash", script]):
                    message(self, "No terminal found", f"Run this script yourself:\n{script}", Gtk.MessageType.WARNING)
            break
        d.destroy()

    def copy_checksum(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        self.flash(f"Computing SHA-256 of {e.name}…")

        def work():
            import hashlib
            h = hashlib.sha256()
            try:
                with open(e.path, "rb") as f:
                    for buf in iter(lambda: f.read(1 << 20), b""):
                        h.update(buf)
                digest = h.hexdigest()
            except OSError as ex:
                GLib.idle_add(lambda: message(self, "Checksum", str(ex), Gtk.MessageType.ERROR) and False)
                return
            GLib.idle_add(lambda: (set_clipboard_text(digest), self.flash(f"SHA-256 copied: {digest[:16]}…")) and False)
        threading.Thread(target=work, daemon=True).start()

    def new_folder(self):
        self.new_item("folder")

    def new_item(self, kind: str):
        t = self.tab
        if t.path in VIRTUAL:
            self.flash("Can't create items here.")
            return
        if kind == "shortcut":
            target = ask_text(self, "Create Shortcut", "Type the location of the item:")
            if not target:
                return
            target = os.path.expanduser(target)
            name = ask_text(self, "Create Shortcut", "Type a name for this shortcut:", os.path.basename(target.rstrip("/")) + " - Shortcut")
            if name:
                try:
                    os.symlink(target, os.path.join(t.path, name))
                except OSError as ex:
                    message(self, "Create Shortcut", str(ex), Gtk.MessageType.ERROR)
            return
        default = {"folder": "New folder", "text": "New Text Document.txt", "markdown": "New Document.md",
                   "script": "New Script.sh", "python": "New Script.py"}.get(kind, "New file")
        path = os.path.join(t.path, default)
        n = 2
        while os.path.lexists(path):
            stem, ext = (default, "") if kind == "folder" else os.path.splitext(default)
            path = os.path.join(t.path, f"{stem} ({n}){ext}")
            n += 1
        try:
            if kind == "folder":
                os.mkdir(path)
            else:
                with open(path, "w") as f:
                    f.write({"script": "#!/bin/bash\n", "python": "#!/usr/bin/env python3\n"}.get(kind, ""))
                if kind in ("script", "python"):
                    os.chmod(path, 0o755)
        except OSError as ex:
            message(self, "New item", str(ex), Gtk.MessageType.ERROR)
            return
        self.undo_stack.append(("new", path))
        t.load()

        def then():
            t.select_names([os.path.basename(path)])
            t.start_rename()
            return False
        GLib.timeout_add(300, then)

    def undo(self):
        if not self.undo_stack:
            self.flash("Nothing to undo.")
            return
        kind, data = self.undo_stack.pop()
        try:
            if kind == "rename":
                old, new = data
                if os.path.lexists(new) and not os.path.lexists(old):
                    os.rename(new, old)
                    self.flash(f"Undo rename: {os.path.basename(old)}")
            elif kind == "move":
                n = 0
                for src, dst in data:
                    if os.path.lexists(dst) and not os.path.lexists(src):
                        os.makedirs(os.path.dirname(src), exist_ok=True)
                        shutil.move(dst, src)
                        n += 1
                self.flash(f"Undo move: {n} item(s) moved back")
            elif kind == "copy":
                if ask_yes_no(self, "Undo Copy", f"Delete the {len(data)} copied item(s)?", True):
                    for _, dst in data:
                        if os.path.lexists(dst):
                            shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
                    self.flash("Undo copy")
            elif kind == "trash":
                n = 0
                trashed = list_folder(TRASH, True)
                for src, name in data:
                    for e in trashed:
                        if e.orig == src and not os.path.lexists(src):
                            e.gfile.move(Gio.File.new_for_path(src), Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
                            n += 1
                            break
                self.flash(f"Undo delete: {n} item(s) restored")
            elif kind == "new":
                if os.path.lexists(data):
                    shutil.rmtree(data) if os.path.isdir(data) and not os.path.islink(data) else os.remove(data)
                    self.flash("Undo new item")
        except (OSError, GLib.Error) as ex:
            message(self, "Undo", str(ex), Gtk.MessageType.ERROR)
        self.reload_all()

    def move_or_copy_to(self, kind: str):
        sel = self.tab.selected_entries()
        if not sel or self.tab.path in VIRTUAL:
            return
        dest = pick_folder(self, f"{'Move' if kind == 'move' else 'Copy'} Items", self.tab.path)
        if dest:
            self.transfer([e.path for e in sel], dest, kind)

    def is_archive(self, e: Optional[Entry]) -> bool:
        return bool(e) and not e.is_dir and e.name.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz") + SEVENZIP_EXTS)

    def zip_selection(self):
        t = self.tab
        sel = t.selected_entries()
        if not sel or t.path in VIRTUAL:
            return
        d = Gtk.Dialog(title="Compress", transient_for=self, modal=True)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Create", Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_border_width(12)
        box.set_spacing(6)
        base = sel[0].name if len(sel) == 1 else self.display_path(t.path)
        stem = base if sel[0].is_dir or len(sel) > 1 else os.path.splitext(base)[0]
        box.pack_start(Gtk.Label(label="Archive name:", xalign=0), False, False, 0)
        name = Gtk.Entry(text=stem, activates_default=True)
        box.pack_start(name, False, False, 0)
        combo = Gtk.ComboBoxText()
        fmts = [("zip (Compressed folder)", "zip"), ("tar.gz", "gztar"), ("tar.xz", "xztar"), ("tar", "tar")]
        if sevenzip():
            fmts.insert(1, ("7z (7-Zip, best compression)", "7z"))
        for f, _ in fmts:
            combo.append_text(f)
        combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        r = d.run()
        fmt = fmts[combo.get_active()][1]
        stem = name.get_text().strip() or stem
        d.destroy()
        if r != Gtk.ResponseType.OK:
            return
        ext = {"zip": ".zip", "7z": ".7z", "gztar": ".tar.gz", "xztar": ".tar.xz", "tar": ".tar"}[fmt]
        out = unique_name(os.path.join(t.path, stem + ext))
        op = ArchiveOp("zip", [e.path for e in sel], out, fmt, self._op_done)
        self._start_op(op, f"Compressing {len(sel)} item{'s' if len(sel) != 1 else ''} to {os.path.basename(out)}")

    @staticmethod
    def archive_stem(name: str) -> str:
        low = name.lower()
        for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar"):
            if low.endswith(suf):
                return name[:-len(suf)]
        return os.path.splitext(name)[0]

    def extract_here(self, e: Optional[Entry] = None):
        """Extract into a new folder named after the archive, next to it, with the progress window, then open
        that folder. No questions asked (unique name on collision) — this is what double-click does."""
        e = e or self.tab.current_entry()
        if not self.is_archive(e) or not e.path:
            return
        dest = unique_name(os.path.join(os.path.dirname(e.path), self.archive_stem(e.name)))
        op = ArchiveOp("extract", [e.path], dest, "", self._op_done)
        self._start_op(op, f"Extracting {e.name}")

    def extract_all(self):
        t = self.tab
        e = t.current_entry()
        if not self.is_archive(e):
            return
        default = os.path.join(t.path, self.archive_stem(e.name))
        dest = ask_text(self, "Extract Compressed Folder", "Files will be extracted to this folder:", default)
        if not dest:
            return
        op = ArchiveOp("extract", [e.path], os.path.expanduser(dest), "", self._op_done)
        self._start_op(op, f"Extracting {e.name} to {self.display_path(dest)}")

    def pin(self, path: str):
        if path in VIRTUAL or not os.path.isdir(path):
            return
        if path not in self.cfg["pinned"]:
            self.cfg["pinned"].append(path)
        self._fill_quick(self.nav_quick)
        self.nav_tree.expand_row(self.nav_store.get_path(self.nav_quick), False)
        self.flash(f"Pinned {self.display_path(path)} to Quick access")
        if self.tab.path == QUICK:
            self.tab.load()

    def unpin(self, path: str):
        if path in self.cfg["pinned"]:
            self.cfg["pinned"].remove(path)
            self._fill_quick(self.nav_quick)
            self.flash("Unpinned from Quick access")
            if self.tab.path == QUICK:
                self.tab.load()

    def hide_selected(self):
        t = self.tab
        if t.path in VIRTUAL:
            return
        for e in t.selected_entries():
            new = os.path.join(os.path.dirname(e.path), e.name[1:] if e.name.startswith(".") else "." + e.name)
            if not os.path.lexists(new):
                try:
                    os.rename(e.path, new)
                except OSError as ex:
                    message(self, "Hide", str(ex), Gtk.MessageType.ERROR)
        t.load()

    def shortcut_here(self, e: Entry):
        link = unique_name(os.path.join(os.path.dirname(e.path), shortcut_name(e)))
        try:
            os.symlink(e.path, link)
        except OSError as ex:
            message(self, "Create shortcut", str(ex), Gtk.MessageType.ERROR)

    def send_to_desktop(self, e: Entry):
        desk = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP) or os.path.join(HOME, "Desktop")
        os.makedirs(desk, exist_ok=True)
        try:
            os.symlink(e.path, unique_name(os.path.join(desk, shortcut_name(e))))
            self.flash("Shortcut created on Desktop")
        except OSError as ex:
            message(self, "Send to Desktop", str(ex), Gtk.MessageType.ERROR)

    def share_email(self):
        sel = [e for e in self.tab.selected_entries() if not e.is_dir]
        if not sel:
            self.flash("Select one or more files to share.")
            return
        if shutil.which("xdg-email"):
            argv = ["xdg-email"]
            for e in sel:
                argv += ["--attach", e.path]
            subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            message(self, "Share", "No email client integration found (xdg-email).", Gtk.MessageType.WARNING)

    def share_spec(self):
        t = self.tab
        sel = t.selected_entries() if t else []
        has = bool(sel) and t.path not in VIRTUAL
        files_only = bool(sel) and all(not e.is_dir for e in sel)
        return [{"label": "Copy _path", "cb": self.copy_path, "sensitive": bool(sel)},
                {"label": "Copy _link (file:// URI)", "cb": lambda: set_clipboard_text("\n".join(e.gfile.get_uri() for e in sel)), "sensitive": bool(sel)},
                {"label": "Copy _to…", "cb": lambda: self.move_or_copy_to("copy"), "sensitive": has},
                {"label": "Compress to _zip…", "cb": self.zip_selection, "sensitive": has}, "-",
                {"label": "_Email as attachment…", "cb": self.share_email, "sensitive": files_only and bool(shutil.which("xdg-email"))},
                {"label": "_Print…", "cb": self.print_files, "sensitive": files_only},
                {"label": "Send to _Desktop (shortcut)", "cb": lambda: self.send_to_desktop(sel[0]), "sensitive": len(sel) == 1 and bool(sel[0].path) and os.path.exists(sel[0].path)}]

    def print_files(self):
        """Explorer-style Print: text and images go through the GTK print dialog; anything else is
        opened in its default application (which has its own Print)."""
        sel = [e for e in self.tab.selected_entries() if not e.is_dir]
        if not sel:
            self.flash("Select a file to print.")
            return
        for e in sel:
            ct = e.content_type or ""
            if ct.startswith("image/"):
                self._print_image(e)
            elif ct.startswith("text/") or ct in ("application/json", "application/xml", "application/x-shellscript", "application/javascript", "application/x-yaml"):
                self._print_text(e)
            elif ct == "application/pdf" and shutil.which("lp"):
                subprocess.Popen(["lp", e.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.flash(f"Sent {e.name} to the default printer")
            else:
                launch_default(e.path, self)
                self.flash(f"Opened {e.name} in its application")

    def _run_print(self, name: str, begin, draw):
        op = Gtk.PrintOperation()
        op.set_job_name(name)
        op.set_embed_page_setup(True)
        op.connect("begin-print", begin)
        op.connect("draw-page", draw)
        try:
            res = op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self)
        except GLib.Error as ex:
            message(self, "Print", ex.message, Gtk.MessageType.ERROR)
            return
        if res == Gtk.PrintOperationResult.ERROR:
            message(self, "Print", "Printing failed.", Gtk.MessageType.ERROR)

    def _print_text(self, e: Entry):
        try:
            with open(e.path, "rb") as f:
                text = f.read(4 << 20).decode("utf-8", "replace").expandtabs(4)
        except OSError as ex:
            message(self, "Print", str(ex), Gtk.MessageType.ERROR)
            return
        state = {"pages": [], "lines": text.splitlines() or [""], "lph": 60}

        def begin(op, ctx):
            layout = ctx.create_pango_layout()
            layout.set_font_description(Pango.FontDescription("Monospace 9"))
            layout.set_text("X", -1)
            _, lh = layout.get_pixel_size()
            lph = max(10, int((ctx.get_height() - 40) // (lh * 1.15)))
            state["lph"] = lph
            n = max(1, (len(state["lines"]) + lph - 1) // lph)
            op.set_n_pages(n)

        def draw(op, ctx, page):
            cr = ctx.get_cairo_context()
            layout = ctx.create_pango_layout()
            layout.set_font_description(Pango.FontDescription("Monospace 9"))
            layout.set_width(int(ctx.get_width() * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            lph = state["lph"]
            header = ctx.create_pango_layout()
            header.set_font_description(Pango.FontDescription("Sans 8"))
            header.set_text(f"{e.name}   —   page {page + 1}", -1)
            cr.move_to(0, 0)
            PangoCairo.show_layout(cr, header)
            layout.set_text("\n".join(state["lines"][page * lph:(page + 1) * lph]), -1)
            cr.move_to(0, 24)
            PangoCairo.show_layout(cr, layout)
        self._run_print(e.name, begin, draw)

    def _print_image(self, e: Entry):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(e.path)
            pb = pb.apply_embedded_orientation() or pb
        except GLib.Error as ex:
            message(self, "Print", ex.message, Gtk.MessageType.ERROR)
            return

        def begin(op, ctx):
            op.set_n_pages(1)

        def draw(op, ctx, page):
            cr = ctx.get_cairo_context()
            w, h = ctx.get_width(), ctx.get_height()
            scale = min(w / pb.get_width(), h / pb.get_height(), 1e9)
            cr.translate((w - pb.get_width() * scale) / 2, (h - pb.get_height() * scale) / 2)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
        self._run_print(e.name, begin, draw)

    def run_script(self, e: Entry, in_terminal: bool):
        cmd = script_runner(e.path)
        if not cmd:
            return
        cwd = os.path.dirname(e.path)
        if in_terminal:
            if not spawn_terminal(cwd, cmd):
                message(self, "Run", "No terminal emulator was found. Set $TERMINAL or install one.", Gtk.MessageType.WARNING)
        elif run_detached(cmd, cwd):
            self.flash(f"Started {e.name}")
        else:
            message(self, "Run", f"Couldn't start {e.name}.", Gtk.MessageType.ERROR)

    def edit_file(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        app = Gio.AppInfo.get_default_for_type("text/plain", False)
        try:
            if app:
                app.launch([e.gfile], self.get_display().get_app_launch_context())
            else:
                launch_default(e.path, self)
        except GLib.Error as ex:
            message(self, "Edit", ex.message, Gtk.MessageType.ERROR)

    # ------------------------------------------------------------------ dialogs
    def properties(self):
        t = self.tab
        sel = t.selected_entries()
        if not sel:
            if t.path in VIRTUAL:
                if t.path == TRASH:
                    try:
                        n = len(list_folder(TRASH, True))
                    except GLib.Error:
                        n = 0
                    message(self, "Recycle Bin Properties", f"Items: {n}")
                return
            sel = [entry_for_path(t.path)]
        self.show_properties(sel)

    def show_properties(self, sel: List[Entry]):
        sel = [e for e in sel if e.path]
        if not sel:
            return
        if len(sel) > 1:
            MultiPropertiesDialog(self, sel)
        else:
            PropertiesDialog(self, sel[0], self.cfg, self.do_rename)

    def folder_options(self, page: int = 0):
        OptionsDialog(self, self.cfg, self.apply_cfg, page)


# --------------------------------------------------------------------------- application
CSS = b"""
/* ---- Windows 11 "Fluent" flavour on top of the current GTK theme ---- */
menubar { padding: 0 4px; }
menubar > menuitem { padding: 4px 8px; border-radius: 4px; }
menubar > menuitem:hover { background: alpha(@theme_fg_color, 0.08); color: @theme_fg_color; box-shadow: none; }
menu menuitem:hover { background: alpha(@theme_selected_bg_color, 0.85); border-radius: 4px; }
.toolbar-row button:hover, .statusbar button:hover, notebook > header button:hover { background: alpha(@theme_fg_color, 0.08); }
.sidepane button:hover { background: alpha(@theme_fg_color, 0.08); }
.toolbar-row button { min-width: 28px; min-height: 28px; padding: 2px; border-radius: 6px; }
.pathbar { background: alpha(@theme_fg_color, 0.05); border: 1px solid alpha(@theme_fg_color, 0.14);
           border-radius: 6px; padding: 1px 4px; min-height: 30px; }
.pathbar > border { border-style: none; }        /* GtkFrame draws its own frame - keep only ours */
.pathbar entry { border: none; background: transparent; box-shadow: none; min-height: 26px; }
.toolbar-row entry.search, .toolbar-row searchentry { border: 1px solid alpha(@theme_fg_color, 0.14); border-radius: 6px;
           background: alpha(@theme_fg_color, 0.05); box-shadow: none; }
.toolbar-row entry.search:focus, .toolbar-row searchentry:focus { border-color: @theme_selected_bg_color; }
.pathbar:hover { background: alpha(@theme_fg_color, 0.08); }
.pathbar button { padding: 1px 8px; min-height: 24px; border-radius: 4px; border: none; background: none; box-shadow: none; }
.pathbar button:hover { background: alpha(@theme_fg_color, 0.1); }
.pathbar label.dim-label { padding: 0 1px; }
entry.search { border-radius: 6px; min-height: 30px; }
.commandbar { padding: 2px 0; }
.commandbar button.cmd { padding: 4px 10px; min-height: 30px; border-radius: 6px; }
.commandbar button.cmd:hover { background: alpha(@theme_fg_color, 0.08); }
.commandbar button.cmd label { font-size: 0.95em; }
.navpane treeview { padding: 2px 0; }
.navpane treeview:selected { border-radius: 4px; }
.navpane { background: alpha(@theme_fg_color, 0.025); }
paned > separator { min-width: 1px; min-height: 1px; background: alpha(@theme_fg_color, 0.12); background-clip: content-box;
                    margin: 0; border: none; box-shadow: none; }
treeview.view { padding: 1px 0; }
treeview.view header button { padding: 4px 8px; background: none; border-style: solid; border-color: alpha(@theme_fg_color, 0.12);
                               border-width: 0 1px 1px 0; border-radius: 0; }
treeview.view header button:last-child { border-right-width: 0; }
treeview.view header button:hover { background: alpha(@theme_fg_color, 0.06); }
treeview.view header button label { font-weight: 600; color: alpha(@theme_fg_color, 0.75); }
treeview.view:selected { background: alpha(@theme_selected_bg_color, 0.85); border-radius: 3px; }
iconview:selected { background: alpha(@theme_selected_bg_color, 0.8); border-radius: 6px; }
notebook > header { background: alpha(@theme_fg_color, 0.04); border: none; padding: 4px 4px 0 4px; }
notebook > header tab { padding: 4px 10px; margin: 0 2px; border: none; border-radius: 6px 6px 0 0; min-width: 120px; }
notebook > header tab:checked { background: @theme_base_color; }
notebook > header tab:hover:not(:checked) { background: alpha(@theme_fg_color, 0.06); }
notebook > header tab button.tab-close { min-width: 18px; min-height: 18px; padding: 0; border-radius: 4px; }
.pane-header { padding: 3px 4px; background: alpha(@theme_fg_color, 0.04); border-bottom: 2px solid alpha(@theme_fg_color, 0.1); }
.pane-header.active { background: alpha(@theme_selected_bg_color, 0.22); border-bottom: 2px solid @theme_selected_bg_color; }
.pane-header.active label { font-weight: bold; }
/* the inactive side is dimmed (darker in dark themes, greyer in light ones); the active side keeps the normal base */
.inactive-pane, .inactive-pane treeview.view, .inactive-pane .icongrid { background: shade(@theme_base_color, 0.92); }
.inactive-pane .pane-header label { color: alpha(@theme_fg_color, 0.6); }
.pane-header label { font-size: 0.9em; }
.sidepane { background: alpha(@theme_fg_color, 0.025); }
.sidepane textview, .sidepane textview text { background: alpha(@theme_fg_color, 0.03); }
.edit-toolbar button { padding: 2px 8px; min-height: 24px; }
.edit-badge { font-size: 0.75em; font-weight: bold; letter-spacing: 1px; padding: 1px 6px; border-radius: 3px;
              color: alpha(@theme_fg_color, 0.55); background: alpha(@theme_fg_color, 0.07); }
.edit-badge.badge-editing { color: #ffffff; background: #2b8a3e; }
.edit-badge.badge-dirty { color: #ffffff; background: #d9480f; }
textview.editing, textview.editing text { background: alpha(#2b8a3e, 0.06); }
.sidepane textview.editing { border: 2px solid #2b8a3e; border-radius: 4px; }
.sidepane textview.readonly { border: 2px solid transparent; }
.edit-toolbar entry { min-height: 24px; }
.statusbar { background: alpha(@theme_fg_color, 0.04); border-top: 1px solid alpha(@theme_fg_color, 0.08); }
.statusbar label { font-size: 0.92em; }
.statusbar button { padding: 0 4px; min-height: 22px; min-width: 22px; }
"""


class ExplorerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)
        self.cfg = load_config()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        WATCHDOG.start(os.path.join(GLib.get_user_cache_dir(), "kestrel", "stalls.log") if PROFILE is not None else None)
        GLib.timeout_add(400, lambda: (threading.Thread(target=warm_workers, daemon=True).start(), False)[1])
        GLib.timeout_add(1500, self._index_startup)

    def _index_startup(self):
        """Load the encrypted index off the main thread; then crawl (incrementally) if it is stale."""
        cfg = load_config()
        if not cfg.get("index_enabled"):
            return False

        def work():
            pin_worker()
            INDEX.load()
            age_h = (time.time() - INDEX.progress.get("finished", 0)) / 3600.0
            if age_h > float(cfg.get("index_interval_h", 6)):
                INDEX.crawl(cfg.get("index_roots", [HOME]), cfg.get("index_network", False), cfg.get("index_hidden", False))
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add_seconds(1800, self._index_tick)
        return False

    def _index_tick(self):
        cfg = load_config()
        if cfg.get("index_enabled") and INDEX.loaded and not INDEX.crawling:
            if (time.time() - INDEX.progress.get("finished", 0)) / 3600.0 > float(cfg.get("index_interval_h", 6)):
                INDEX.crawl(cfg.get("index_roots", [HOME]), cfg.get("index_network", False), cfg.get("index_hidden", False))
        return True
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        w = self.new_window(None)
        if STRESS is not None:
            drv = StressDriver(w, STRESS[0], STRESS[1], STRESS[2])
            GLib.timeout_add(1500, lambda: (drv.start(), False)[1])

    def do_open(self, files, n, hint):
        for f in files:
            p = f.get_path()
            self.new_window(p if p and os.path.isdir(p) else (os.path.dirname(p) if p else None))

    def new_window(self, path: Optional[str]):
        if path in VIRTUAL:
            path = HOME
        w = MainWindow(self, path)
        w.present()
        return w


def _probe_gl_child() -> int:
    """Runs in a subprocess with a given GDK_BACKEND: realise a GLArea in a window and report as JSON."""
    import json
    out = {"backend": os.environ.get("GDK_BACKEND", "(default)"), "ok": False, "attempts": [], "renderer": "", "error": ""}
    try:
        win = Gtk.Window(title="kestrel gl probe", default_width=64, default_height=64)
        area = Gtk.GLArea()
        area.set_has_depth_buffer(True)
        result = {}

        def create(a):
            ctx, label = gl_try_contexts(a.get_window(), out["attempts"])
            result["label"] = label
            return ctx
        area.connect("create-context", create)

        def render(a, ctx):
            try:
                out["renderer"] = (_GL.glGetString(_GL.GL_RENDERER) or b"").decode(errors="replace") + " / " + \
                                  (_GL.glGetString(_GL.GL_VERSION) or b"").decode(errors="replace")
                hdr = _gl_header(ctx)
                _GLshaders.compileProgram(_GLshaders.compileShader(hdr + _GL_VERT, _GL.GL_VERTEX_SHADER),
                                          _GLshaders.compileShader(hdr + _GL_FRAG, _GL.GL_FRAGMENT_SHADER))
                out["ok"] = True
                out["flavour"] = result.get("label")
            except Exception as ex:
                out["error"] = f"shader/render: {ex}"[:400]
            return True
        area.connect("render", render)
        win.add(area)
        win.show_all()
        t0 = time.time()
        while time.time() - t0 < 3 and not out["ok"] and not out["error"]:
            while Gtk.events_pending():
                Gtk.main_iteration()
            if area.get_error() is not None and not out["error"]:
                out["error"] = f"context: {area.get_error().message}"
            time.sleep(0.02)
        win.destroy()
    except Exception as ex:
        out["error"] = f"{ex}"[:400]
    print("KESTREL-GL " + json.dumps(out))
    return 0


def probe_gl(args: List[str]) -> int:
    """kestrel.py --probe-gl: system GL facts, then the GLArea test under each GDK backend. If the default
    backend fails but another works, that backend is saved to the config (--no-save to only report)."""
    import json
    if args and args[0] == "--child":
        return _probe_gl_child()
    print("Kestrel OpenGL probe")
    print("=" * 60)
    env = os.environ
    print(f"session:      XDG_SESSION_TYPE={env.get('XDG_SESSION_TYPE', '?')}  WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '-')}  DISPLAY={env.get('DISPLAY', '-')}")
    print(f"GDK env:      GDK_BACKEND={env.get('GDK_BACKEND', '-')}  GDK_GL={env.get('GDK_GL', '-')}  GDK_DEBUG={env.get('GDK_DEBUG', '-')}  LIBGL_ALWAYS_SOFTWARE={env.get('LIBGL_ALWAYS_SOFTWARE', '-')}")
    print(f"python:       {sys.executable}  PyOpenGL: {'yes' if HAVE_GL else 'NO (python3 -m pip install --user PyOpenGL)'}")
    print(f"GTK:          {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    try:
        gpu = subprocess.run(["sh", "-c", "lspci 2>/dev/null | grep -iE 'vga|3d|display'"], capture_output=True, text=True, timeout=10).stdout.strip()
        print("GPU:          " + (gpu.replace("\n", "\n              ") or "(lspci unavailable)"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for tool, argv in (("glxinfo", ["glxinfo", "-B"]), ("eglinfo", ["eglinfo", "-B"])):
        if shutil.which(tool):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                keep = [l.strip() for l in r.stdout.splitlines() if any(k in l for k in ("OpenGL renderer", "OpenGL version", "OpenGL core profile version", "Vendor", "EGL version", "Device:", "Accelerated"))]
                print(f"{tool + ':':<14}" + ("\n              ".join(keep[:6]) or (r.stderr.strip().splitlines() or ["no output"])[-1]))
            except (OSError, subprocess.TimeoutExpired) as ex:
                print(f"{tool + ':':<14}{ex}")
        else:
            print(f"{tool + ':':<14}not installed (sudo dnf install {'glx-utils' if tool == 'glxinfo' else 'egl-utils'})")
    verdicts = []
    try:
        r = subprocess.run(["sh", "-c", "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1"], capture_output=True, text=True, timeout=10)
        smi = r.stdout.strip()
        print("nvidia:       " + (smi.replace("\n", "\n              ") or "(no nvidia-smi)"))
        if "version mismatch" in smi.lower():
            verdicts.append("The NVIDIA kernel module and the user-space driver libraries are different versions (a driver\n"
                            "  update was installed but the old module is still loaded). Nothing can create a GL context until\n"
                            "  they match: REBOOT, then run this probe again. (Check with: cat /proc/driver/nvidia/version)")
        try:
            kmod = open("/proc/driver/nvidia/version").read().strip().splitlines()[0]
            print("nvidia kmod:  " + kmod)
        except OSError:
            pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    if shutil.which("glxinfo"):
        r = subprocess.run(["glxinfo", "-B"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or "OpenGL renderer" not in r.stdout:
            verdicts.append("glxinfo itself fails on this X display, so GLX is broken system-wide, not just in Kestrel:\n  " +
                            " | ".join((r.stderr.strip() + "\n" + r.stdout.strip()).strip().splitlines()[-3:]))
    if not HAVE_GL:
        print("\nPyOpenGL is missing, so the viewer cannot use the GPU at all. Install it and run the probe again.")
        return 1
    print("\nGLArea context test (what the viewer does), per GDK backend:")
    results = {}
    for backend in (None, "x11", "wayland"):
        if backend == "wayland" and not env.get("WAYLAND_DISPLAY"):
            continue
        if backend == "x11" and not env.get("DISPLAY"):
            continue
        cenv = dict(env)
        if backend:
            cenv["GDK_BACKEND"] = backend
        else:
            cenv.pop("GDK_BACKEND", None)
        try:
            r = subprocess.run([sys.executable, os.path.abspath(sys.argv[0]), "--probe-gl", "--child"], env=cenv, capture_output=True, text=True, timeout=30)
            line = [l for l in r.stdout.splitlines() if l.startswith("KESTREL-GL ")]
            res = json.loads(line[-1][11:]) if line else {"ok": False, "error": (r.stderr.strip().splitlines() or ["no output"])[-1][:300], "attempts": []}
        except (OSError, subprocess.TimeoutExpired, ValueError) as ex:
            res = {"ok": False, "error": str(ex), "attempts": []}
        results[backend] = res
        name = backend or "default"
        print(f"  {name:<8} {'OK  ' if res['ok'] else 'FAIL'}  {res.get('renderer') or res.get('error', '')}" + (f"  [{res.get('flavour')}]" if res.get("ok") else ""))
        for a in res.get("attempts", []):
            print(f"           - {a}")
    print()
    if results.get(None, {}).get("ok"):
        print("Hardware acceleration works with the default backend. Turn on Tools → Hardware-accelerated 3D preview.")
        return 0
    working = [b for b, r in results.items() if b and r.get("ok")]
    if working:
        b = working[0]
        print(f"The default backend fails but GDK_BACKEND={b} works.")
        if "--no-save" not in args:
            cfg = load_config()
            cfg["gdk_backend"] = b
            save_config(cfg)
            print(f"Saved gdk_backend={b} to the Kestrel config: the app will start with that backend from now on.")
        else:
            print(f"Run Kestrel as:  GDK_BACKEND={b} python3 kestrel.py   (or drop --no-save to make it permanent)")
        return 0
    if verdicts:
        print("Diagnosis:")
        for v in verdicts:
            print("* " + v)
        print()
    print("No backend could create a GL context. Common causes:")
    print("  - NVIDIA driver without GBM/EGL Wayland support: try the x11 result above, or update the driver")
    print("  - remote session / VNC / software-only GL: set LIBGL_ALWAYS_SOFTWARE=1 to use llvmpipe (slow but works)")
    print("  - Flatpak/snap sandboxes or missing mesa-libGL / mesa-libEGL packages: sudo dnf install mesa-libGL mesa-libEGL mesa-dri-drivers")
    return 1


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    if "--stress" in sys.argv[1:] or "--nav-bench" in sys.argv[1:]:
        args = sys.argv[1:]
        flag = "--stress" if "--stress" in args else "--nav-bench"
        i = args.index(flag)
        rest = []
        for a in args[i + 1:]:
            if a.startswith("--"):
                break
            rest.append(a)
        folder = " ".join(rest) if rest and os.path.isdir(" ".join(rest)) else None       # unquoted paths with spaces
        if rest and folder is None:
            print(f"stress: folder not found: {' '.join(rest)!r} — using a generated one", file=sys.stderr)
        cycles = int(args[args.index("--cycles") + 1]) if "--cycles" in args and args.index("--cycles") + 1 < len(args) else 1
        globals()["STRESS"] = (folder, cycles, "nav" if flag == "--nav-bench" else "stress")
        sys.argv = [sys.argv[0]]
    if "--preview-bench" in sys.argv[1:]:
        return run_preview_bench(big="--quick" not in sys.argv[1:])
    if "--selftest" in sys.argv[1:]:
        return run_selftest("--bench" in sys.argv[1:])
    if "--profile" in sys.argv[1:]:
        import cProfile
        sys.argv.remove("--profile")
        globals()["PROFILE"] = cProfile.Profile()
        PROFILE.enable()
        log = os.path.join(GLib.get_user_cache_dir(), "kestrel", "stalls.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        open(log, "w").close()
        print(f"kestrel: profiling — stalls are logged live to {log}; a summary prints at exit (Tools → Performance report… while running)", file=sys.stderr)
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-gl":
        return probe_gl(sys.argv[2:])
    if len(sys.argv) > 2 and sys.argv[1] == "--probe-model":
        # terminal self-test for the 3D preview chain: kestrel.py --probe-model file.abc
        print(preview_tool_report())
        print("-" * 60)
        globals()["BLENDER_VERBOSE"] = True
        t0 = time.time()
        m = load_model(" ".join(sys.argv[2:]))     # tolerate an unquoted path with spaces
        if m and "error" not in m:
            print(f"OK in {time.time() - t0:.1f}s: {len(m['verts'])} verts, {len(m['faces'])} faces, info={m['info']}")
            return 0
        print("FAILED:", (m or {}).get("error", "no result"))
        return 1
    if "--demo" in sys.argv[1:]:
        # kestrel --demo /path/to/KestrelDemo : a fictional machine for screenshots (see docs/DEMO.md)
        i = sys.argv.index("--demo")
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            root = sys.argv[i + 1]
            del sys.argv[i:i + 2]
        else:
            root = os.path.join(HOME, "KestrelDemo")
            del sys.argv[i]
        if not os.path.isdir(root):
            print(f"kestrel: demo folder not found: {root}", file=sys.stderr)
            return 2
        globals()["DEMO"] = demo_setup(root)
        globals()["HOME"] = DEMO["home"]
        if len(sys.argv) == 1:
            sys.argv.append(os.path.join(DEMO["root"], "Projects", "Hollowmere"))
    cfg0 = load_config()
    globals()["UI_CORES"] = int(cfg0.get("ui_cores", 2))
    pin_ui_thread()
    install_thread_pinning()
    sys.setswitchinterval(0.002)      # let the GTK thread grab the GIL back from Python workers within ~2 ms
    if cfg0.get("gdk_backend") and not os.environ.get("GDK_BACKEND"):
        os.environ["GDK_BACKEND"] = cfg0["gdk_backend"]       # chosen by --probe-gl; must be set before the display opens
    app = ExplorerApp()
    try:
        return app.run(sys.argv)
    finally:
        save_config(app.cfg)
        if PROFILE is not None:
            out = os.path.join(GLib.get_user_cache_dir(), "kestrel", "profile.prof")
            PROFILE.disable()
            PROFILE.dump_stats(out)
            print("\n" + WATCHDOG.report() + "\n\n" + profiling_report(40), file=sys.stderr)
            print(f"\nkestrel: full profile saved to {out}  (python3 -m pstats {out})", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
