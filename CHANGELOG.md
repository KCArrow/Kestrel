# Changelog

## 1.1 — 2026-10-27

**Houdini caches and VDBs**
- Houdini caches in the 3D preview: `.bgeo`/`.bgeo.sc`/`.geo` meshes and particles, `.vdb` volumes
  (smoke) and level sets (surfaces), via Houdini when installed; VDBs also via OpenVDB or Blender
- Built-in VDB reader: fog volumes preview as smoke without Houdini (Houdini, EmberGen, Blender VDBs;
  blosc/zip, half-float), level sets as surfaces; EmberGen/Blender Z-up volumes stood upright
- OpenGL draws Houdini caches too: particles as shaded sprites and volumes as smoke on the GPU
- Cache sequences: select several `.vdb` / `.bgeo` frames and press *Preview as sequence* for a time
  slider with playback (Space, ← →, adjustable fps); frames decode in the background and share one box
  and brightness; playback pauses when the window loses focus

**Scene breakdowns**
- Scene breakdowns in the preview pane for Unreal (.umap/.uasset/.uproject, UE 4.0–5.6), Unity (.unity/.prefab/
  .unitypackage), Maya (.ma/.mb) and Blender (.blend, 2.7x–5.x): outliner, contents, settings and every
  referenced file with its path and a missing-file check; filter, copy as text, double-click to reveal a file
- Blender and Unreal breakdowns show the file's saved thumbnail; Maya and Blender materials render on a
  material ball when picked

**Scene objects in 3D**
- Pick an object in a Blender (2.7x–5.x) or Maya (.ma / .mb) outliner to see its geometry in a 3D view, with
  everything parented under it, in place — read from the file itself, no Maya or Blender needed. Pick a
  texture to see the image (first UDIM tile)
- When a Maya scene has meshes Kestrel can't rebuild (long modelling history) or deformers it doesn't
  apply (skinning, blend shapes), and Maya is installed, the preview shows Kestrel's version at once and then
  Maya's own evaluation of the scene (read once per file with mayapy, cached); once Maya has read a file,
  every pick in it uses Maya's result. Rigs (skinning, blend shapes…) always get Maya's evaluation
- Pick the Outliner heading (Maya) or a scene (Blender) to see everything visible at once
- Fixed: Maya meshes moved or sculpted with vertex tweaks were drawn without them (bodies too small, parts
  in the wrong place)
- Instances: Maya instanced shapes and transforms show under every parent; Blender collection instances
  show in place, including collections linked from other .blend files
- World positions checked against the files' own data: Blender objects parented to bones follow the bone;
  Maya transforms honour inheritsTransform
- Maya object previews place rigged / grouped meshes correctly: rotate and scale pivots, shear, rotate axis,
  duplicate short names resolved by DAG path, hidden children left out; big scenes are parsed once and
  then each pick is quick
- Blender 5 files: data that shares an address across data-blocks is now read from the right block (stored
  material previews could show the wrong picture)

**Shaders, materials and lights**
- Shader and material previews: MaterialX, OSL (.osl/.oso), Unity .mat / .shader / .shadergraph on a material
  ball (GGX, clearcoat, normal maps, transmission, textures incl. UDIM); live Shadertoy / glslsandbox GLSL on
  the GPU with compile errors on the right line; GLSL / HLSL breakdown and highlighted code view
- IES photometric files (LM-63-1995/2002/2019): light pattern on a wall, polar candela plot, beam / field
  angles, lumens and wattage

**Other**
- Maya (`mayapy`) as a fallback importer for FBX, Alembic and USD when Blender isn't available
- Batch rename: frame-number offsets (padding kept, overlapping ranges safe), repathing into another
  folder (created if missing, works across drives), and a much faster live preview
- Home folder listed first under This PC in the navigation pane; Browse… buttons use Kestrel's own folder
  picker instead of the GTK/Nautilus chooser
- Animated GIFs (and animated WebP) play in the preview pane; click to pause. Frame count, length and
  loop setting in the details
- Address bar keeps a faint outline (matching the search box) so it's clear where to click to type a path
- Fixed: a double-click right after opening a folder was ignored, and Shift-click after going back or
  reloading selected only one item

## 1.0 — 2026-10-25

First public release. Licensed under PolyForm Noncommercial 1.0.0 (free for individuals and
nonprofits; commercial licences available — see COMMERCIAL-LICENSE.md).

- Windows-Explorer layout: menus, command bar, tabs, split view, navigation / preview / details panes,
  all icon and list views, grouping, column chooser
- Background file operations with progress, conflicts, pause, undo; freedesktop trash; batch rename
- System clipboard: copied files paste into other apps — browsers (Teams, Gmail, web chat), Discord,
  Slack, Nautilus; a single copied picture also pastes as an image
- Archives: zip/tar natively, 7z/rar/cab/iso via 7-Zip; extract on double-click
- Preview: images incl. EXR/HDR/IFF/PSD decoders, text with quick editor, PDF, folder contents,
  video/audio (GStreamer), 3D models (OBJ/STL/PLY/FBX/glTF/Alembic incl. HDF5/USD/.blend) with a
  multi-process software rasteriser or OpenGL; ⅛ s crossfades; resizable, remembered panes
- Removable drives: live detection, USB badge, eject, format (UDisks2), physical-disk grouping,
  dual-boot partitions hidden until mounted; Windows-style This PC tile view
- Optional encrypted file index (AES-256 + HMAC, keyring-backed) for navigation and search head start
- CPU parking, stall watchdog with freeze dumps, `--selftest`, `--preview-bench`, `--stress`,
  `--nav-bench`, `--profile`, `--probe-gl`, `--probe-model`
- OpenRV integration (Ctrl+Shift+R)
- Crash and error reporting: exceptions, hard crashes and freezes become reports you can review,
  edit and file as a GitHub issue (Help → Report a problem…)
- `--demo` mode: fictional drives, disks, user folders and config for screenshots (docs/DEMO.md)
- Preferences dialog with General / View / Search / Optional tabs and dependency diagnostics

Known issues

- Video → video preview switches can still cut instead of crossfading on some setups.
