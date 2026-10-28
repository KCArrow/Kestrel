#!/usr/bin/env python3
# Kestrel File Browser — Copyright (c) 2026 Kalib Arrowsmith. https://github.com/KCArrow/kestrel
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Required Notice: Copyright Kalib Arrowsmith (https://github.com/KCArrow/kestrel)
# Free for individuals and noncommercial organizations; companies and studios need a commercial
# licence — see COMMERCIAL-LICENSE.md in the repository.
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

import bisect
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


RECENT_LOG = __import__("collections").deque(maxlen=200)   # last events, included in error reports


def bench(label: str):
    RECENT_LOG.append(f"{time.strftime('%H:%M:%S')}  {label}")
    if BENCH:
        sys.stderr.write(f"bench: {label} {int((time.time() - _T0) * 1000)} ms\n")
        sys.stderr.flush()


APP_ID = "io.github.kestrel_files"
APP_NAME = "Kestrel"
APP_FULL_NAME = "Kestrel File Browser"
VERSION = "1.1"
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
    "confirm_delete": True, "checkboxes": False, "gl_preview": True, "seq_fps": 24, "blend_view": "breakdown", "gdk_backend": "", "rv_enabled": False, "rv_path": "", "ui_cores": 2, "archive_action": "extract", "index_enabled": False, "index_roots": [HOME], "index_network": False, "index_hidden": False, "index_interval_h": 6, "nav_pane": True, "preview_pane": False, "details_pane": False,
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


def looks_runnable(e) -> bool:
    """script_runner() without opening the file — for menus, which are built while they pop up and must not
    wait on a network share. Uses the name, the mode bits and the content type the listing already has."""
    if e is None or e.is_dir or not e.path:
        return False
    if e.ext in (".py", ".sh", ".bash") or (e.mode & 0o111):
        return True
    return (e.content_type or "") in ("application/x-shellscript", "text/x-python", "text/x-python3",
                                      "application/x-executable", "application/x-sharedlib")


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
