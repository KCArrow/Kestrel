# Changelog

## 1.0 — 2026-10-25

First public release.

- Windows-Explorer layout: menus, command bar, tabs, split view, navigation / preview / details panes,
  all icon and list views, grouping, column chooser
- Background file operations with progress, conflicts, pause, undo; freedesktop trash; batch rename
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
- Preferences dialog with General / View / Search / Optional tabs and dependency diagnostics

Known issues

- Video → video preview switches can still cut instead of crossfading on some setups.
