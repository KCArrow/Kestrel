# Kestrel File Browser

A fast, Windows-Explorer-style file manager for the Linux desktop, built for Rocky Linux 9
and VFX/animation workstations.

Single-file GTK 3 application (`kestrel.py`, ~13k lines), **no pip dependencies**: it needs only
`python3`, `python3-gobject` and `gtk3`, which every GNOME install already has. Everything else
(video, 3D formats, EXR tools, archives, OpenRV…) is optional and detected at runtime.

```bash
sudo dnf install python3-gobject gtk3     # Rocky / RHEL / Fedora (usually already present)
./install.sh                              # ~/.local/bin/kestrel + app-menu entry
kestrel ~/Pictures
```

Or unzip the portable build and run `./kestrel` — nothing is installed (see [Portable build](#portable-build)).

## Screenshots

<!-- Taken in demo mode (docs/DEMO.md) — nothing here is a real machine. -->
![Kestrel main window](docs/screenshots/hero.png)

| This PC | 3D preview | Video preview |
|---|---|---|
| ![](docs/screenshots/this-pc.png) | ![](docs/screenshots/preview-3d.png) | ![](docs/screenshots/preview-video.png) |

| EXR / PSD preview | Split view | Preferences → Optional |
|---|---|---|
| ![](docs/screenshots/preview-exr.png) | ![](docs/screenshots/split-view.png) | ![](docs/screenshots/preferences-optional.png) |

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Portable build](#portable-build)
- [Optional tools](#optional-tools)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Command-line flags](#command-line-flags)
- [Configuration and data locations](#configuration-and-data-locations)
- [Building from source](#building-from-source)
- [Tests and diagnostics](#tests-and-diagnostics)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)
- [License](#license)

---

## Features

**Explorer layout.** Menu bar (File / Home / Share / View / Tools / Help), Windows 11-style command
bar, breadcrumb address bar with path typing and Tab completion, tabs (drag a tab out into its own
window, drag it back), split view (two folders side by side, Ctrl+Shift+S), navigation pane (Quick
access with pinned/frequent/recent, This PC, Recycle Bin), details pane, preview pane.

**Views.** Extra large → small icons, List, Details, Tiles, Content; sort and group by any column;
column chooser; item check boxes; file name extensions; hidden items. Details view uses fixed-height
rows so 100k-file folders scroll like 100-file ones.

**File operations.** Background copy / move / delete / rename / new with a progress dialog (bytes,
speed, remaining, pause, cancel) and Replace / Skip / Keep-both conflicts. Clipboard and
drag-and-drop interoperate with Nautilus and other GTK apps. Undo (Ctrl+Z). Real freedesktop
Recycle Bin. Folders auto-refresh when changed on disk. Batch rename (Ctrl+F2) with patterns,
numbering and regex. SHA-256 to clipboard. Run scripts in a terminal.

**Archives.** Zip / tar.gz / tar.xz / tar natively; `.7z`, `.rar`, `.cab`, `.iso` and more through
7-Zip when installed. Double-clicking an archive extracts it (configurable); Extract here / Extract
to…; progress with pause.

**Preview pane.** Images (GdkPixbuf formats plus built-in decoders for **EXR**, **Radiance HDR**,
**Maya IFF**, **Photoshop PSD/PSB**), text with syntax highlighting and an inline quick editor,
PDF, folder contents with sizes, archive listings, media info, **video and audio playback**
(GStreamer), and a **3D model previewer** for OBJ, STL, PLY, FBX, glTF, Alembic (`.abc`, both Ogawa
and legacy HDF5), USD and `.blend` — software rasteriser (multi-process, 5 M-triangle meshes in
under a second) or OpenGL. Smooth shading, orbit/zoom, wireframe, "all triangles" override. Every
preview switch crossfades in ⅛ s; preview and details panes are resizable and remembered.

**Removable drives.** Live detection (no restart), USB badge on removable media, Eject and Format
(UDisks2, no root prompt in the app), physical-disk grouping on This PC so you can see which
disk holds which mounts. Other OS partitions on a dual-boot machine stay hidden until explicitly
mounted.

**Optional encrypted file index.** Speeds up navigation and search on slow/network storage. The
disk is always the truth — the index only gives a head start, every folder is re-read as you
visit it, and search results are verified. Stored AES-256 encrypted with HMAC under
`~/.cache/kestrel`, key in the keyring (libsecret) or a 0600 key file.

**Snappy by design.** The GTK thread never touches the disk or network: listings, stats,
thumbnails, previews, searches and sizes run in threads or worker processes. Optional CPU parking
pins the interface to its own cores so a heavy preview can't make the window stutter. A stall
watchdog and freeze dumps are built in (see [Tests and diagnostics](#tests-and-diagnostics)).

**OpenRV integration (optional).** Right-click → *Open in RV* or **Ctrl+Shift+R** sends images,
sequences and movies to RV when it's detected.

---

## Requirements

| | |
|---|---|
| OS | Linux with GTK 3 (developed and tested on Rocky Linux 9; also runs on Fedora, Ubuntu, Arch/CachyOS) |
| Python | 3.9 or newer (Rocky 9 ships 3.9; 3.11/3.12 also work) |
| Required packages | `python3-gobject` (PyGObject), `gtk3` |
| Optional | `python3-numpy` (much faster 3D/EXR previews), see [Optional tools](#optional-tools) |

```bash
# Rocky / RHEL / Fedora
sudo dnf install python3-gobject gtk3 python3-numpy
# Debian / Ubuntu
sudo apt install python3-gi gir1.2-gtk-3.0 python3-numpy
# Arch / CachyOS
sudo pacman -S python-gobject gtk3 python-numpy
```

---

## Install

```bash
git clone https://github.com/KCArrow/kestrel.git
cd kestrel
./install.sh
```

`install.sh` copies `kestrel.py` to `~/.local/bin/kestrel` and adds a desktop entry. No root needed.

- Make it the default folder handler: `xdg-mime default kestrel.desktop inode/directory`
- Uninstall: `./install.sh --uninstall` (settings in `~/.config/kestrel` are kept)
- Run without installing: `python3 kestrel.py [folder…]`

---

## Portable build

Download `kestrel-<version>-portable.zip` from the Releases page (or build it with
`./make-portable.sh`, output in `dist/`). Unzip anywhere:

```
kestrel-1.0-portable/
├── kestrel            # launcher: ./kestrel [folder]
├── kestrel.py         # the application
├── install.sh         # optional: install into ~/.local/bin later
├── PORTABLE.txt
├── README.md  LICENSE  docs/  packaging/
```

`./kestrel` runs it in place. Create a `config/` folder next to the launcher to make it fully
self-contained (settings, cache and the file index are then kept there instead of
`~/.config` / `~/.cache`). The host still needs `python3`, `python3-gobject` and `gtk3`; that is
the only way to ship a GTK app without bundling a desktop stack.

---

## Optional tools

Open **Preferences → Optional** inside Kestrel. It lists every optional dependency, shows which are
found, lets you toggle OpenGL / OpenRV / CPU parking / indexing, and offers a generated install
script for whatever is missing.

| Capability | Needs | Rocky 9 package / source |
|---|---|---|
| Video & audio preview | GStreamer + gtksink + codecs | `gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free gstreamer1-libav` (RPM Fusion for H.264) |
| Fast 3D / EXR | numpy | `python3-numpy` |
| FBX, glTF, DAE, PLY… | Blender (any install incl. Flatpak `org.blender.Blender`) or `assimp` | `flatpak install org.blender.Blender` / `assimp` |
| Alembic `.abc` (Ogawa), USD, `.blend` | Blender | as above; `pip install usd-core` also works for USD |
| Alembic `.abc` (legacy HDF5) | h5py | `python3-h5py` |
| OpenGL preview | PyOpenGL | `python3-pyopengl` |
| EXR/DPX/TX conversions | OpenImageIO `oiiotool`, ImageMagick | `OpenImageIO-utils ImageMagick` |
| Thumbnails for movies, extra image formats | `ffmpeg` | RPM Fusion |
| PDF preview | `pdftoppm` | `poppler-utils` |
| Text editor highlighting | GtkSourceView 4 | `gtksourceview4` |
| `.7z .rar .cab .iso …` | 7-Zip | `p7zip p7zip-plugins` or `7zip` |
| Open in RV | OpenRV on PATH or picked in Preferences | https://github.com/AcademySoftwareFoundation/OpenRV |
| Index key in keyring | libsecret | `libsecret-tools` (otherwise a 0600 key file is used) |
| Eject / Format | UDisks2 | `udisks2` (standard on GNOME) |

Nothing here is required; Kestrel degrades gracefully and tells you in the preview pane what
would enable a given format.

---

## Keyboard shortcuts

Press **F1** in the app for the full list.

| | |
|---|---|
| Enter / Backspace / Alt+← → ↑ | open / back / forward / up |
| Ctrl+L, F4, Alt+D | edit path |
| Ctrl+E, Ctrl+F, F3 | search |
| Ctrl+T / Ctrl+W / Ctrl+Tab / Ctrl+1…9 | tabs |
| Ctrl+N | new window |
| Ctrl+Shift+S | split view |
| Ctrl+C / X / V, Ctrl+Shift+C | copy / cut / paste / copy path |
| Ctrl+Z | undo |
| F2, Ctrl+F2 | rename, batch rename |
| Delete, Shift+Delete | recycle, delete permanently |
| Ctrl+Shift+N | new folder |
| Ctrl+A, Ctrl+Space | select all, toggle selection |
| Ctrl+H | hidden items |
| Alt+P, Alt+Shift+P | preview pane, details pane |
| Alt+Enter | properties |
| Ctrl+O | open with… |
| Ctrl+Shift+R | open in RV (when enabled) |
| F5, F11, Ctrl+Q | refresh, full screen, quit |

---

## Command-line flags

```
kestrel [folder …]                 open one tab per folder (default: home)
kestrel --demo [KestrelDemo]       fictional machine for screenshots/demos — see docs/DEMO.md
kestrel --selftest [--bench]       unit/benchmark tests of the non-GUI machinery
kestrel --preview-bench [--quick] [--keep]
                                   synthesize large files of every preview type, time each decoder
kestrel --stress [folder] [--cycles N]
                                   drive the real window through the folder: navigate, select, preview,
                                   rename, zip… and report stalls
kestrel --nav-bench folder         open every subfolder, report listing/paint times and stalls
kestrel --profile                  run normally with cProfile on the UI thread (Tools → Performance report)
kestrel --probe-gl [--no-save]     test OpenGL context creation under each GDK backend
kestrel --probe-model file         run the 3D import chain on one file and report
```

---

## Configuration and data locations

| Path | Contents |
|---|---|
| `~/.config/kestrel/config.json` | all settings (readable JSON; delete to reset) |
| `~/.config/kestrel/index.key` | file-index key (only when no keyring is available; mode 0600) |
| `~/.cache/kestrel/index.enc` | encrypted file index |
| `~/.cache/kestrel/freeze-*.txt` | thread dumps written automatically if the UI freezes > 4 s |
| `~/.cache/kestrel/gl-probe.txt` | last `--probe-gl` result |

A legacy `~/.config/explorer-gtk/config.json` is migrated automatically on first start.

---

## Building from source

`kestrel.py` is generated by concatenating the parts in `src/` (kept separate only to keep
editing manageable; they share one module namespace):

```bash
./build.sh            # src/p*.py → kestrel.py, then py_compile as a syntax check
./make-portable.sh    # → dist/kestrel-<version>-portable.zip
```

Part order and contents are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Edit the parts, never `kestrel.py` directly.

---

## Tests and diagnostics

```bash
python3 kestrel.py --selftest             # on a desktop with GTK installed
python3 tests/headless.py --selftest      # no display, no GTK: real GLib/Gio, widgets stubbed (CI)
python3 kestrel.py --preview-bench        # decoder timings on synthetic large files
python3 kestrel.py --nav-bench /mnt/nas   # navigation latency on a slow share
python3 kestrel.py --stress ~/Projects    # robustness: random realistic use with stall reporting
```

While running, **Tools → Performance report…** shows the stall watchdog's log (any main-loop stall
over 120 ms with the Python stack it was stuck in), and a freeze longer than 4 s writes every
thread's stack to `~/.cache/kestrel/freeze-*.txt`. See [docs/TESTING.md](docs/TESTING.md).

---

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for: GStreamer "missing" / no video,
OpenGL context failures (including the NVIDIA driver/kernel mismatch after an update), Flatpak
Blender not detected, Alembic files that no importer reads, slow network folders, index questions,
and how to report a freeze.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — source layout, threading model, preview pipeline, index design
- [docs/TESTING.md](docs/TESTING.md) — every test/bench flag, what it measures, how to read the output
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- [docs/DEMO.md](docs/DEMO.md) — `--demo` mode and the KestrelDemo folder for screenshots
- [CHANGELOG.md](CHANGELOG.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT — see [LICENSE](LICENSE).
