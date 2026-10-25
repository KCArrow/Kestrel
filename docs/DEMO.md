# Demo mode

`kestrel --demo /path/to/KestrelDemo` starts Kestrel on a **fictional machine** so you can take
screenshots or give demos without exposing your own files, drives or user name.

```bash
unzip kestrel-demo.zip -d ~/          # → ~/KestrelDemo
kestrel --demo ~/KestrelDemo          # or: python3 kestrel.py --demo ~/KestrelDemo
```

What is faked while the flag is on (everything else is the real application):

| | Real | Demo |
|---|---|---|
| Drives in This PC / nav pane | your mounts | Local Disk (/), Projects, Archive, KINGSTON (USB, removable), studio (NFS share) |
| Physical disks (This PC sections, tooltips) | `lsblk` | Samsung 980 PRO · WD Black SN850X · Seagate IronWolf (with an unmounted Windows partition, for the dual-boot rule) · Kingston DataTraveler |
| Free space bars | `statvfs` | fixed fictional numbers |
| User folders (Desktop, Documents, …) | XDG dirs | `KestrelDemo/Home/*` |
| File owner in Details / Properties | your user name | `artist` |
| Quick access pins, Frequent, Recent | your config | pre-filled with demo paths |
| Settings | `~/.config/kestrel/config.json` | fresh defaults, **never read or written** |

Not faked: the Recycle Bin (it is your real trash — empty it or avoid that page), window title bar,
theme, and the file index (leave it off in demo mode).

## The demo folder

```
KestrelDemo/
├── System/      fake root filesystem (etc/os-release, fstab, …)               → "Local Disk (/)"
├── Projects/    Hollowmere/ short-film project + Saltmarsh_Spot_S2/            → "Projects"
├── Archive/     four archived shows                                            → "Archive"
├── KINGSTON/    client review stick                                            → USB drive
├── studio_nas/  library/hdri, textures, software, shared, users                 → network share
└── Home/        Desktop Documents Downloads Music Pictures Videos
```

`Projects/Hollowmere` holds real, previewable content: PNG/JPG, 1080p EXR frames, Radiance HDR,
Maya IFF, a PSD, two high-poly OBJ sculpts (489k and 216k triangles) and an STL, an H.264 playblast
and a turntable movie, PDF, zip, CSV/MD/TXT/JSON/Python. Files elsewhere are mostly realistic
names with placeholder contents.

## Screenshot checklist

| File | Where |
|---|---|
| `hero.png` | `Projects/Hollowmere/assets/creatures/bogwraith/model/`, Details view, select the `.obj`, preview + details panes on |
| `this-pc.png` | **This PC** — four disk sections, USB badge, "Windows (not mounted)" greyed, network location |
| `preview-3d.png` | `assets/props/lantern/model/lantern_chain_v002.obj` |
| `preview-exr.png` | `renders/sh010/beauty/sh010_beauty_v004.1001.exr` or `reference/concept/heron_colorkeys.psd` |
| `preview-video.png` | `shots/sh010/playblast/sh010_anim_latest.mp4` |
| `split-view.png` | Ctrl+Shift+S: `renders/turntable/heron_turntable_v02/` and `reference/concept/` |
| `quick-access.png` | Quick access page (pins, frequent, recent all pre-filled) |
| `search.png` | search `heron` or `*.exr` from `Projects/` |
| `preferences-optional.png` | Preferences → Optional |
| `batch-rename.png` | select the turntable PNG sequence → Ctrl+F2 |

Take them at 100 % scale, roughly 1600×1000, with a clean GTK theme. Save into `docs/screenshots/`
and reference them from README.md.
