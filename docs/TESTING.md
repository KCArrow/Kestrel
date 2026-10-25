# Testing and benchmarking

All tests are built into `kestrel.py`; there is no test framework to install.

## Unit / benchmark tests

```bash
python3 kestrel.py --selftest            # with GTK available
python3 tests/headless.py --selftest     # anywhere: no display, no GTK (CI, containers)
python3 kestrel.py --selftest --bench    # scaled-up data: 50k-file listings, 5M-face mesh, table output
```

Covers: listing/sorting/grouping, formatting, `unique_name`, `Entry` from dirents, the EXR / HDR /
IFF / PSD decoders (synthetic files, bit-exact numpy vs. pure-Python), tone-mapping, OBJ/STL
loading, `_finish_model_numpy` (triangulation, decimation, normals), the rasteriser, zip/7z
round-trips through `ArchiveOp`, archive descriptions, batch-rename planning, RV sequence
detection, drive classification helpers, the crash reporter (stale-session detection, de-duplication,
home-folder scrubbing), index encrypt/decrypt/HMAC round-trip and search.
Each line prints its wall time; a non-zero exit means a failure.

`tests/headless.py` stubs `Gtk`, `Gdk`, `GdkPixbuf`, `Pango`, `GtkSource` and `Gst` but uses the
real `GLib`/`Gio`, so content types, cancellables and file operations are exercised for real.
Requires `python3-gobject` (`python3-gi` on Debian/Ubuntu); numpy optional.

## Preview benchmark

```bash
python3 kestrel.py --preview-bench [--quick] [--keep]
```

Synthesises large files of every preview type (PNG, PSD, EXR, HDR, IFF, OBJ, STL, text, zip,
PDF) in a scratch folder, runs each through the exact decoder/model path the preview pane uses,
and prints load + render time per type. `--quick` uses smaller files, `--keep` leaves the scratch
folder for inspection. Use it after touching anything under `p1b.py` / `p3c.py`.

## GUI stress test

```bash
python3 kestrel.py --stress [folder] [--cycles N]
```

Opens the real window on `folder` (default: home) and drives it: navigate into random subfolders,
back/forward/up, select files of each type to trigger previews, open/close tabs, search, create
and rename and zip files in a scratch directory, undo. Dialog boxes are suppressed. At the end it
prints the stall watchdog's report: how many steps stalled the main loop, the worst stalls and
the Python stack each was stuck in. A healthy run reports ~1 % stalled steps or fewer, all short.

## Navigation benchmark

```bash
python3 kestrel.py --nav-bench /path/to/slow/share
```

Visits every subfolder of the path through the real window and prints per-folder listing time,
first-paint time, whether the index served the first paint, and stalls. This is the test for
network mounts (NFS / SMB / sshfs / sparsebundles) — a cold folder on a NAS should paint names
instantly and fill in sizes afterwards.

## Profiling a live session

```bash
python3 kestrel.py --profile
```

Then use the app normally and open **Tools → Performance report…**: the stall log (every main-loop
stall > 120 ms with its stack), navigation log, and a cProfile table of everything run on the UI
thread. Works without `--profile` too, minus the cProfile section.

If the window ever freezes for more than 4 s, a full thread dump is written automatically to
`~/.cache/kestrel/freeze-*.txt` — attach it to a bug report.

## OpenGL and 3D import probes

```bash
python3 kestrel.py --probe-gl            # system GL facts + GLArea context test under x11 and wayland
python3 kestrel.py --probe-model file.abc   # run the importer chain on one file, print what worked
```

`--probe-gl` saves the working backend to the config (`--no-save` to skip). It also detects the
common NVIDIA failure where the kernel module and user-space driver versions differ after an
update (fix: reboot).

## Continuous integration

`.github/workflows/ci.yml` runs `tests/headless.py --selftest` on Ubuntu with `python3-gi` and
numpy, and byte-compiles `kestrel.py` with Python 3.9 (Rocky 9's version) to catch syntax that
is too new.
