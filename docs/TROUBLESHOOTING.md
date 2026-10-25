# Troubleshooting

First stop: **Preferences → Optional** shows every optional tool, whether it was found, and a
diagnostics report you can copy. **Tools → Performance report…** shows stalls and timings.

## It won't start

- `ModuleNotFoundError: No module named 'gi'` → install PyGObject:
  `sudo dnf install python3-gobject gtk3` (Rocky/Fedora), `sudo apt install python3-gi gir1.2-gtk-3.0` (Debian/Ubuntu).
- `Namespace Gtk not available` → the GTK 3 typelib is missing: `gtk3` / `gir1.2-gtk-3.0`.
- Running `kestrel` prints "command not found" after `install.sh` → `~/.local/bin` is not on your
  PATH; log out/in or `export PATH="$HOME/.local/bin:$PATH"`.
- Wrong Python picked up → run `python3 kestrel.py` explicitly; Python 3.9+ is required.

## Video / audio preview

- *GStreamer: missing* → install the GStreamer GI bindings and plugins:
  `sudo dnf install python3-gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free`.
  On Rocky 9, `python3-gstreamer1` is what makes `from gi.repository import Gst` work; the plugins
  alone are not enough.
- *gtksink missing* → it is in `gstreamer1-plugins-good` (Rocky) / `gstreamer1.0-gtk3` (Debian).
- Movies show a black or static frame, audio only → no H.264 decoder. Enable RPM Fusion and install
  `gstreamer1-libav` (or `gstreamer1-plugins-ugly` / `openh264`). The Optional tab lists the
  decoders it found (`H264_DECODERS`).
- The diagnostics report lists each GStreamer element by its actual element name; a package being
  installed but the element still "missing" usually means the plugin was built without that feature
  (`gst-inspect-1.0 gtksink` to confirm).

## 3D previews

- "No importer could read this file" → see which route applies in **Preferences → Optional**:
  - `.obj .stl .ply`: built in (numpy makes them far faster).
  - `.fbx .gltf .glb .dae .3ds` and friends: Blender (any install, including Flatpak
    `org.blender.Blender`) or `assimp`.
  - `.abc`: Ogawa files need Blender; **legacy HDF5 Alembic** (older Maya exports) can't be read by
    Blender or current Alembic builds — Kestrel reads them directly with `h5py`
    (`sudo dnf install python3-h5py`). `kestrel --probe-model file.abc` tells you which kind it is.
  - `.usd .usda .usdc .usdz`: Blender, or `pip install --user usd-core`.
  - `.blend`: Blender.
- Flatpak Blender not detected → `flatpak info org.blender.Blender` must succeed for your user.
  Kestrel runs it with filesystem grants for the file's folder; a file on a path Flatpak can't see
  (some network mounts) will fail — copy it locally to test.
- Import hangs → the Blender bridge is killed after a timeout (process group), the preview reports
  it. Very large scenes can exceed it; raise the model face cap or use the software renderer.
- Model looks wrong (holes, inside-out) → "All triangles" in the preview context menu disables
  decimation; winding is fixed automatically, but meshes with mixed orientation will show dark
  faces under smooth shading.
- Slow software rendering → check numpy is installed (`python3 -c "import numpy"`); without it the
  pure-Python fallback is used. Enable CPU parking in Preferences → Optional so the window stays
  responsive while frames render.

## OpenGL

- "Unable to create GL context" → run `kestrel --probe-gl`. It tries x11 and wayland backends and
  every GL/GLES version, saves the one that works, and reports the system state.
- Probe says *NVML/driver version mismatch* → the NVIDIA kernel module and the user-space driver
  differ (happens after a driver update). **Reboot.**
- Under Wayland with NVIDIA, x11 (XWayland) is often the backend that works; the probe stores it as
  `gdk_backend` in the config and Kestrel sets `GDK_BACKEND` on startup.
- Models render white in GL mode by design (flat lighting only). Turn OpenGL off in Preferences →
  Optional to get the shaded software renderer back.

## Slow folders / freezes

- Navigation into a network folder is slow the first time: names appear instantly, sizes and
  dates fill in. If names don't appear instantly, run `kestrel --nav-bench /that/path` and attach
  the output to a report.
- A dead network mount (`/mnt/nas` with the NAS off) must not freeze the window; if it does,
  **Tools → Performance report…** will show which call stalled.
- The window froze completely → look in `~/.cache/kestrel/` for `freeze-*.txt`; it holds every
  thread's stack at the moment of the freeze. Attach it to an issue.
- The spinner bottom-left appears after 250 ms of waiting on a listing; it is informational, not
  a hang.

## File index

- Enabling: Preferences → Search → *File index*. Choose roots; network and hidden paths are
  opt-in. The first crawl runs in the background at low priority.
- "Will I ever see a stale listing?" No. Index data only paints first; every folder is re-read
  from disk as you enter it and the view is replaced by what the disk says ("Verifying against the
  disk…" in the status bar). Search hits are checked before being shown.
- Key storage: the keyring via `secret-tool` (install `libsecret-tools`); otherwise a 0600 key
  file at `~/.config/kestrel/index.key`. *Wipe index* in Preferences deletes the data and key.
- Index size: a few MB per 100k files, encrypted. It is excluded from itself.

## Drives

- A plugged-in USB stick doesn't show up → Kestrel listens to `Gio.VolumeMonitor`; if GVFS isn't
  running (minimal installs), start `gvfs-udisks2-volume-monitor` or install `gvfs`.
- Eject / Format are greyed out → needs `udisks2` and a polkit agent (standard on GNOME); the
  Format dialog lists only filesystems whose `mkfs` is installed (`exfatprogs`, `dosfstools`,
  `ntfs-3g`, `e2fsprogs`).
- A Windows/other-Linux partition is missing from This PC → intentional: internal non-removable
  partitions that aren't mounted are hidden. Mount it (Disks app, or `udisksctl mount -b /dev/sdX1`)
  and it appears.

## Archives

- `.7z / .rar / .cab / .iso` need 7-Zip: `sudo dnf install p7zip p7zip-plugins` (or `7zip` on
  newer distros). The extract-on-double-click behaviour is in Preferences → General → Archives.

## Reporting a problem

The quickest way: **Help → Report a problem…**, or *Review & report…* on the error bar after a crash.
The report already contains the version, distro, desktop, the stack trace and recent activity;
describe what you were doing at the top and press *Report on GitHub…*.
Reports live in `~/.cache/kestrel/reports/` if you'd rather attach the file yourself.

If you report by hand, include:

Kestrel version (Help → About), distro and desktop (Wayland or X11), the diagnostics
report from Preferences → Optional, and for freezes the newest `~/.cache/kestrel/freeze-*.txt`
or the Performance report. For a preview problem with a specific file type, the output of
`kestrel --probe-model <file>` (3D) or the file's `file --mime-type` result.
