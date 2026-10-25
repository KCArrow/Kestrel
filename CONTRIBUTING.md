# Contributing

## Setup

```bash
git clone https://github.com/<you>/kestrel.git && cd kestrel
sudo dnf install python3-gobject gtk3 python3-numpy      # or the apt/pacman equivalents in README
./build.sh && python3 kestrel.py
```

## Workflow

1. Edit the parts in `src/` — never `kestrel.py`, it is generated. `docs/ARCHITECTURE.md` says what
   lives where and the order the parts are concatenated in.
2. `./build.sh` (also byte-compiles as a syntax check).
3. `python3 tests/headless.py --selftest` must pass; run `python3 kestrel.py --preview-bench --quick`
   if you touched decoders or the model pipeline, and `--stress` for anything in the window code.
4. Commit `src/` **and** the rebuilt `kestrel.py` together, so the single-file download on the
   default branch always matches the source.

## Rules of the house

- The GTK main thread never touches the disk or the network. Anything that can block goes to a
  thread or the `run_heavy` pool and reports back with `GLib.idle_add` + a generation check.
- No pip dependencies for the core. New capabilities are optional, detected at runtime, listed in
  `optional_deps()` (p3d) so they show up in Preferences → Optional with an install hint, and the
  app must degrade gracefully (a message in the preview pane, never an exception dialog).
- Every GStreamer call goes through `VideoPreview._ops`. Every new preview type gets its row in
  `run_preview_bench` and a unit test in `run_selftest`.
- UI strings are terse: labels, not explanations. Rocky 9 is the baseline: Python 3.9 syntax, GTK 3.24.
- Keep `--selftest` fast (< 10 s without `--bench`).

## Licensing of contributions

Kestrel is licensed under the PolyForm Noncommercial License with commercial licences sold
separately (see COMMERCIAL-LICENSE.md). So that this remains possible, by submitting a pull
request you agree that your contribution is provided under the same terms **and** that you grant
the project author a perpetual, irrevocable, royalty-free licence to use, modify and relicense it,
including under commercial terms. You keep the copyright to your work. If you can't agree to that,
please open an issue describing the change instead of a PR.

## Releasing

```bash
# bump VERSION in src/p1.py, update CHANGELOG.md
./build.sh && ./make-portable.sh         # dist/kestrel-<ver>-portable.zip
git tag v<ver> && git push --tags        # the release workflow attaches the zip
```
