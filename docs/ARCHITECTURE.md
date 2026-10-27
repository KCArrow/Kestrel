# Architecture

Kestrel is one Python module. For editing it is split into parts under `src/`, which `build.sh`
concatenates **in this exact order** into `kestrel.py`. The parts are not packages: they share one
namespace, so a part may use anything defined in an earlier part, and a class defined later
(e.g. `MainWindow` in p4) may reference helpers from any earlier part at call time.

| Part | Lines | What lives there |
|---|---|---|
| `p1.py`  | ~1500 | imports, `VERSION`, `DEFAULT_CONFIG`, config load/save, formatting, `Entry` and directory listing (`list_folder`, shallow first paint, parallel stats on network mounts), sorting/grouping, drives and removable media (sysfs classification, UDisks2 eject/format, physical-disk grouping), `FileOp` background file operations, archive operations (`ArchiveOp`, 7-Zip), OpenRV detection |
| `p1b.py` | ~900  | image decoders written in pure Python/numpy (EXR, Radiance HDR, Maya IFF, PSD/PSB), tone-mapping, external converter fallbacks, information previews (archive listing, media info), `PathCache` (non-blocking exists/isdir), CPU parking, the `run_heavy` process pool, `generate_preview` |
| `p1c.py` | ~950  | diagnostics: `StallWatchdog` (main-loop stall detection, freeze dumps), navigation log, `run_selftest`, synthetic file generators and `run_preview_bench`, `StressDriver` (GUI stress and nav benchmarks) |
| `p1d.py` | ~350  | encrypted file index: `IndexKey` (keyring or key file), `FileIndex` (crawl, lookup, search, encrypt-then-MAC storage) |
| `p1e.py` | ~400  | crash detection and error reports: `CrashReporter` (exception hooks, faulthandler + per-process session markers for hard crashes, report files, sanitising, pre-filled GitHub issues), `CRASH` |
| `p2.py`  | ~850  | dialogs: message/ask helpers, `ProgressDialog`, Properties, `OptionsDialog` (Preferences: General / View / Search / Optional), shortcuts, about |
| `p2b.py` | ~250  | Batch rename dialog and `apply_renames` |
| `p2c.py` | ~90   | `ReportDialog` (review / edit / send a report), `open_report` |
| `p3b.py` | ~850  | custom widgets: `FitImage`, row hover, soft grid lines, virtualised `IconGrid`, This PC tile view (`PCView`, `DriveBar`) |
| `p3c.py` | ~1400 | 3D preview: `load_model` + `_finish_model_numpy` (triangulation, decimation, normals), software rasteriser (`_rasterize`, `software_render`) and its `RenderWorker` process, `ModelView` (cairo) and `GLModelView` (Gtk.GLArea), GStreamer `VideoPreview` |
| `p3e.py` | ~2050 | scene breakdowns: `UEPackage` (Unreal package summary / names / imports / exports), Unity YAML + `.meta` GUID index, Maya ASCII statements + IFF (`FOR4`/`FOR8`) chunks, `BlendFile` (blocks + SDNA, gzip/zstd), `scene_breakdown`, `breakdown_text` |
| `p3f.py` | ~1500 | shader / material / IES previews: `render_material_ball` (numpy GGX), readers for MaterialX, OSL/OSO, GLSL/HLSL, Unity `.mat`/`.shader`/`.shadergraph`, `IESProfile` + `ies_preview`, `shader_preview`, `material_ball_for`; live GLSL: `GLSLRunner` (no GTK) and `GLSLView` (Gtk.GLArea) |
| `p3d.py` | ~1300 | built-in VDB reader, Houdini / Maya bridges, 3D importers for FBX / USD / Alembic (HDF5 via h5py), Blender and assimp bridges, `optional_deps`, `install_script`, `preview_tool_report`, GL status |
| `p3.py`  | ~1050 | `IconCache` (icons + thumbnails), `NavNode`, `FolderView` (one folder tab: listing, search, selection, drag-and-drop, views), `Page` (one or two `FolderView`s for split view) |
| `p4.py`  | ~3000 | `MainWindow`: menus/command bar, navigation pane, tabs, address bar, side pane (preview + details), preview routing and crossfades, cache-sequence player, drive watching, status bar and spinner, window state |
| `p5.py`  | ~1400 | `MainWindow` continued: context menus, actions (zip, extract, RV, checksum, run script, batch rename…), index scheduling, `ExplorerApp` (Gtk.Application), `probe_gl`, `main()` and all command-line flags |

Keep this order when adding a part; `build.sh` holds the list.

## Threading model

The GTK main thread owns widgets and nothing else. Everything that can block runs elsewhere and
reports back with `GLib.idle_add`, carrying a *generation counter* so stale results are dropped:

| Work | Where | Notes |
|---|---|---|
| directory listing, stats | worker thread | shallow names first on network mounts, 32-wide stats; the index can paint first, the disk read always follows ("Verifying against the disk…") |
| pushing rows into views | main thread | 1,500-row slices between frames |
| sort / group | worker thread on cached listing | no disk access |
| copy / move / delete / archive | worker thread (`FileOp`, `ArchiveOp`) | pausable (SIGSTOP for 7z), cancellable, progress dialog |
| search | worker thread | index hits first, deduplicated against disk results |
| thumbnails, image decode, PDF, media info, SHA-256, folder sizes | worker threads | cancelled via `threading.Event` when the selection changes |
| heavy decoders (EXR, PSD, model load) | `run_heavy` process pool | forked early (`warm_workers`) so a fork never happens under load; 240 s timeout |
| 3D rasterising | `RenderWorker` process (singleton) | ordered request queue; frames returned as bytes and painted by cairo |
| video | GStreamer, every call on `VideoPreview._ops` thread | never query/seek the pipeline from the GTK thread (that was a 4 s freeze) |
| path existence (`PathCache`) | thread with cached answers | nav pane never stats a dead mount on the GTK thread |
| drive changes | `Gio.VolumeMonitor` signals | no polling |
| index crawl | one low-priority thread | debounced saves |

CPU parking (`Preferences → Optional → Performance`): `os.sched_setaffinity` pins the UI thread to
the first N cores and every worker thread/process to the rest; `sys.setswitchinterval(0.002)` keeps
the GIL handoff quick.

## Preview pipeline

`MainWindow._update_side` → `_update_preview(entry)`:

1. Take a **snapshot** of the pane (cairo, opaque: theme background + content) and hold it in an
   overlay (`snap_area`, a windowless `DrawingArea`, so input passes through to 3D/video).
2. Route by type: folder (debounced 250 ms, listing in a thread), video/audio, image (GdkPixbuf or
   `PREVIEW_DECODERS`), text/editor, PDF, archive listing, 3D model (`_load_model_preview`), or icon
   + info rows.
3. A placeholder ("Loading…") appears only if nothing lands within 250 ms (900 ms for folders,
   200 ms for an empty selection) so quick switches never flash grey.
4. When the content is in place, `_show_preview(name)` switches the `Gtk.Stack` child instantly
   and `_crossfade_from_snapshot()` fades the held snapshot out over `FADE_MS = 125`.
   Re-selecting the same item (`_preview_key`) drops the snapshot without re-decoding.

### 3D models

`load_model(path)` → fast numpy paths for OBJ/STL/PLY; FBX/USD text parsers; Alembic: HDF5 via
h5py, Ogawa via Blender; anything else via Blender (`_BLENDER_SCRIPT`, stdout transport,
Flatpak-aware), assimp, Maya (`mayapy`, `_MAYA_SCRIPT`, binary triangle dump) or Houdini.
Houdini caches (`houdini_kind`: `.bgeo*`, `.geo*`, `.vdb`) go through `load_houdini_cache`. VDBs first try
`load_vdb_native`, a built-in reader for float grids (`read_vdb_grids` → `_vdb_transform` →
`_VdbFloatTree`: root → 32³ → 16³ → 8³ leaf topology, then leaf buffers vectorised with numpy; zip, blosc
with a ctypes/python-blosc or pure-Python LZ4/BloscLZ decoder, active-value masks, half floats). Dense
volumes are averaged into 2ⁿ³ cells down to ~1.5M samples. Otherwise: hython
runs `_HOU_SCRIPT` (File SOP → Unpack; polygons saved as OBJ, particles + `Cd` and sampled VDB /
native volume voxels saved as `.npz`; level sets become surface points), then gconvert, then for
VDBs the OpenVDB Python module or Blender's volume-to-mesh. `_finish_cache_numpy` puts mesh,
points and voxels in one unit box; `render_cache` draws them: mesh + point splats into a shared
z-buffer (points shaded from screen-space depth slopes), then voxel densities in front of it are
summed per pixel and composited as smoke. In OpenGL, `GLScene` draws the same thing on the GPU: mesh,
particles as sphere-shaded sprites, and volumes in two offscreen passes — opaque scene into an RGBA8
texture + depth, each voxel splatted as its exact pixel footprint (box filter) into an R32F/R16F texture
with additive blending, depth-tested against the opaque scene — then one full-screen pass applies the
same `1 − exp(−k·Σdensity)` smoke curve. Exposure is measured once per model (read back from the
first frame) so orbiting and sequence playback don't pump. `GLScene` has no GTK dependency.
Two or more selected caches show a *Preview as sequence* button; pressing it starts the sequence
player (`MainWindow._load_model_sequence`) for that selection: frames
decode in `seq_pool()` worker processes, all with the first frame's `norm` (box, density scale,
coarsening step), within a memory budget window ahead of the playhead. `_finish_model_numpy` builds float32/int32 arrays, fixes winding by
signed volume, computes smooth vertex normals and, above the face cap (500k default, "All
triangles" overrides), decimates by vertex clustering. Software rendering runs in `RenderWorker`
(z-buffer, Gouraud, sub-pixel splat lane). `GLModelView` negotiates a context across GL/GLES
versions (`gl_try_contexts`) and renders white-shaded.

## Scene breakdowns (`p3e.py`)

`scene_breakdown(path)` runs in the `run_heavy` worker and returns `{kind, info, tree, notes}`; tree nodes are
`[label, detail, children, path, exists]`, shown by the side pane's `breakdown` page (a filtered `TreeStore`).
Every reader works from the file alone:

- **Unreal**: package summary for legacy versions −2 … −9 (UE 4.0 – 5.6); the generations table is found by its
  content (last entry = export/name counts) so the saved-by engine version reads even when newer versions add
  summary fields. Levels: exports whose outer is `PersistentLevel`; labels/folders by scanning the export's
  tagged properties for `ActorLabel` / `FolderPath`.
- **Unity**: documents split on `--- !u!<class> &<id>`; flow mappings that wrap are re-joined; the hierarchy is
  rebuilt from Transform `m_Father`/`m_Children`, `SceneRoots`, and PrefabInstance `m_TransformParent`.
- **Maya**: ASCII by MEL statement (`createNode`, `setAttr … -type "string"`, `file -r`, `requires`, `fileInfo`);
  binary by IFF chunk (`CREA` name/parent, `STR ` attributes, `FINF`, `PLUG`). Path attributes per node type
  (`_MAYA_PATH_ATTRS`) plus any string ending in a known file extension.
- **Blender**: block headers (old 20/24-byte and 5.x 32-byte), struct offsets from the file's own SDNA, so
  `Object.parent`, `Scene.r.engine`, `Image.filepath` etc. are found by name in every version.

Breakdowns may also carry `images` (`[(label, RawImage)]`: Blender `TEST` thumbnail, Unreal package thumbnail)
and `materials` (dicts in `MATERIAL_DEFAULTS` form). Material rows have the path `material:<i>`; the page keeps
that in a hidden column and renders `material_ball_for(materials[i])` in the worker when the row is selected.

## Shader, material and IES previews (`p3f.py`)

`shader_preview(path)` returns the breakdown shape plus `code` (`(text, language)`) and `glsl` (`{source,
style}`) and is shown on the same `breakdown` page: a picture on top (ball / light pattern / `GLSLView`), the tree
or the code view below. Every reader reduces a material to one dict (linear base colour, metallic, roughness,
specular, coat, emission, opacity, transmission, texture paths); `render_material_ball` shades a sphere with GGX
specular under a procedural studio environment, ACES-tonemapped, 2× supersampled.

Live GLSL wraps the user's code: Shadertoy sources get the `iTime` / `iResolution` / `iMouse` / `iChannel0-3`
uniforms and a `main()` that calls `mainImage`; glslsandbox sources get `gl_FragColor` mapped to an output. A
`#line` directive (0 for GLSL < 3.30, 1 otherwise) keeps compile errors on the user's line numbers. The view
animates through a tick callback only while mapped and while the window is active.

## Encrypted index (`p1d.py`)

- Storage: `~/.cache/kestrel/index.enc` = AES-256-CBC via `openssl enc -pbkdf2` (key over an fd,
  never argv) + HMAC-SHA256, encrypt-then-MAC. Key in the keyring through `secret-tool`, else a
  0600 file in `~/.config/kestrel`.
- Contents: `dir → (mtime, [(name, is_dir, size, mtime)])` for configured roots, excluding
  system prefixes unless a root is inside them; network and hidden entries are opt-in.
- Rule: **the disk is the truth.** Index data paints first and is replaced by the real listing on
  every visit; `update_dir` refreshes the index from what was read. Search merges index hits and
  live results with deduplication. The index can be wiped from Preferences at any time.

## Diagnostics (`p1c.py`)

`StallWatchdog` heartbeats the main loop from a thread; a gap > 120 ms is logged with the UI
thread's Python stack, > 4 s dumps every thread (`faulthandler`) to `~/.cache/kestrel/freeze-*.txt`.
`NAV_LOG` records each navigation (listing time, paint time, source: index/disk). `--profile` wraps
the main loop in cProfile. `StressDriver` drives the real window (navigate, select, preview,
rename, zip, undo) and reports stalls per step; `--nav-bench` visits every subfolder of a path.

## Crash and error reports (`p1e.py`, `p2c.py`)

| Trouble | How it is caught | What the user sees |
|---|---|---|
| unhandled Python exception (GTK callback, worker thread) | `sys.excepthook`, `threading.excepthook` | error bar in the active window, once per distinct failure site |
| hard crash (segfault, abort, fatal signal) | `faulthandler` writes every thread's stack to `reports/fault-<pid>.txt`; `reports/session-<pid>.json` exists while the process runs and is removed on a clean quit | at the next start, a bar: "Kestrel crashed…" |
| freeze > 4 s | `StallWatchdog.dump_freeze` (p1c) | bar when the interface recovers; if the user killed it, at the next start |
| process killed (OOM, SIGKILL, power loss) | stale session marker without a fault trace | recorded as `ended`, not prompted (nothing to diagnose) |

SIGTERM, SIGHUP and SIGINT quit the application cleanly (`GLib.unix_signal_add`) so a logout isn't
mistaken for a crash. Several Kestrel processes can run at once; markers are per pid and a marker
whose pid is still alive is left alone.

Reports are plain text: summary, environment (versions, distro, desktop, RAM, a few settings),
traceback / stacks, watchdog stalls this session and the last 40 `bench()` events (`RECENT_LOG`).
`CrashReporter.sanitize` replaces `$HOME` with `~` and the user name with `<user>`. Reporting is
always user-initiated: *Report on GitHub…* opens a new issue in the browser with title and body
pre-filled (truncated at 6000 characters to fit the URL; *Copy* has the full text). The issue URL
is `REPORT_ISSUES_URL` at the top of `p1e.py`.

## Configuration

`~/.config/kestrel/config.json`, keys in `DEFAULT_CONFIG` (p1). Saved on every change; sizes
(`nav_width`, `preview_width`, `side_width`, `side_split`, `window`) saved on close.
