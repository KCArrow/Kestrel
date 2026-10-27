

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

    def stop(self):
        """Quitting: tear-down is allowed to take a moment and must not be reported as a freeze."""
        self._running = False

    def _watch(self):
        stall_start = None
        samples: dict = {}
        dumped = False
        while self._running:
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
            try:
                CRASH.freeze_report(path)
            except NameError:
                pass
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
def write_test_vdb(path: str):
    """A minimal valid .vdb (format 224, zip + active-mask compression): one fog grid named 'density' with five
    active voxels in one leaf at index (8..12, 16, 24), voxel size 0.5, translated by (10, 20, 30)."""
    S = lambda t: struct.pack("<I", len(t.encode())) + t.encode()
    out = bytearray(struct.pack("<qIIIB", 0x56444220, 224, 8, 1, 1) + b"0" * 36 + struct.pack("<Ii", 0, 1))
    out += S("density") + S("Tree_float_5_4_3") + S("")
    gpos = len(out) + 24
    grid = bytearray(struct.pack("<I", 3))                                     # COMPRESS_ZIP | COMPRESS_ACTIVE_MASK
    grid += struct.pack("<I", 1) + S("class") + S("string") + S("fog volume")
    grid += S("UniformScaleTranslateMap") + struct.pack("<18d", 10, 20, 30, *([0.5] * 15))
    bit0 = lambda nbytes: b"\x01" + bytes(nbytes - 1)
    no_vals = b"\x00" + struct.pack("<q", 0)                                 # metadata 0, zero active values
    grid += struct.pack("<IfII", 1, 0.0, 0, 1) + struct.pack("<3i", 0, 0, 0)
    grid += bit0(4096) + bytes(4096) + no_vals                                # upper: child 0
    leaf_n = (1 << 8) | (2 << 4) | 3                                          # lower-node offset → leaf origin (8, 16, 24)
    lm = bytearray(512)
    lm[leaf_n >> 3] |= 1 << (leaf_n & 7)
    grid += bytes(lm) + bytes(512) + no_vals                                  # lower at (0,0,0): one leaf
    mask = bytearray(64)
    for x in range(5):
        n = x << 6                                                            # voxel (x, 0, 0) in the leaf
        mask[n >> 3] |= 1 << (n & 7)
    grid += mask                                                              # leaf topology
    block = gpos + len(grid)
    z = zlib.compress(struct.pack("<5f", 0.2, 0.4, 0.6, 0.8, 1.0))
    grid += mask + b"\x00" + struct.pack("<q", len(z)) + z                    # leaf buffer
    out += struct.pack("<qqq", gpos, block, gpos + len(grid)) + grid
    with open(path, "wb") as f:
        f.write(out)


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

        @timed("crash reporter")
        def test_crash_reporter(self):
            d = os.path.join(self.tmp, "reports")
            cr = CrashReporter(d)
            os.makedirs(d, exist_ok=True)
            # a previous run that died with a fault trace, and one that just vanished
            dead = 2 ** 22 + 12345
            for pid, fault in ((dead, "Fatal Python error: Segmentation fault\n  File x"), (dead + 1, "")):
                with open(os.path.join(d, f"session-{pid}.json"), "w") as f:
                    json.dump({"pid": pid, "started": time.time() - 60, "version": VERSION}, f)
                with open(os.path.join(d, f"fault-{pid}.txt"), "w") as f:
                    f.write(fault)
            prev = cr._check_previous()
            self.assertEqual(len(prev), 1)
            self.assertIn("crashed", cr.summary_of(prev[0]))
            self.assertFalse(any(n.startswith(("session-", "fault-")) for n in os.listdir(d)))
            self.assertEqual(len(cr.pending()), 1)          # the traceless 'ended' one isn't pending
            # runtime exception: reported once per failure site, home folder scrubbed
            def boom():
                raise ValueError(os.path.join(os.path.expanduser("~"), "secret", "file.exr"))
            paths = []
            for _ in range(2):
                try:
                    boom()
                except ValueError:
                    paths.append(cr.record(*sys.exc_info()))
            self.assertIsNotNone(paths[0])
            self.assertIsNone(paths[1])
            with open(paths[0]) as f:
                txt = f.read()
            self.assertIn("ValueError", txt)
            self.assertIn("~/secret/file.exr", txt)
            if os.path.expanduser("~") not in ("/", "/root"):
                self.assertNotIn(os.path.expanduser("~"), txt)
            cr.archive(paths[0], "sent")
            self.assertEqual(len(cr.pending()), 1)

        @timed("houdini cache render (points + volume)")
        def test_cache_render(self):
            self.assertEqual(houdini_kind("a.0001.bgeo.sc"), "geo")
            self.assertEqual(houdini_kind("smoke.vdb"), "vdb")
            self.assertEqual(houdini_kind("x.obj"), "")
            if np is None:
                self.skipTest("numpy not installed")
            rng = np.random.default_rng(3)
            P = rng.normal(size=(50000, 3)).astype(np.float32)
            g = np.stack(np.meshgrid(*[np.linspace(-1, 1, 24)] * 3, indexing="ij"), -1).reshape(-1, 3)
            d = np.clip(1 - np.linalg.norm(g, axis=1), 0, None)
            m = _finish_cache_numpy({"P": P, "Cd": np.abs(P) % 1, "vP": g[d > 0] + 4, "vD": d[d > 0], "vS": [2 / 23],
                                     "info": [("Points", "50,000")], "source": "test"}, 500000)
            self.assertEqual(len(m["points"]), 50000)
            self.assertTrue(len(m["vpts"]) > 1000)
            self.assertLessEqual(float(np.abs(np.concatenate([m["points"], m["vpts"]])).max()), 0.5001)   # one shared unit box
            EX = {k: m[k] for k in ("points", "pcolor", "psize", "vpts", "vden", "vsize") if k in m}
            r = software_render(m["verts"], m["faces"], m["normals"], m["vnormals"], (200, 150, 0.5, 0.3, 1.0, 0, 0, True, False, (30, 30, 30), (1, 1, 1)), EX)
            self.assertIsNotNone(r)
            img = np.frombuffer(r[2], np.uint8).reshape(150, 200, 4)
            self.assertGreater(int((img[:, :, :3].astype(int).sum(2) != 90).sum()), 2000)     # something was drawn

        @timed("built-in VDB reader")
        @timed("GIF info")
        def test_gif_info(self):
            # hand-built 2×1 GIF89a: global palette, NETSCAPE loop forever, two frames of 50 ms and 0 ms
            pal = bytes([0, 0, 0, 255, 255, 255])
            frame = lambda d: (b"\x21\xf9\x04\x00" + struct.pack("<H", d) + b"\x00\x00"
                               + b"\x2c" + struct.pack("<HHHH", 0, 0, 2, 1) + b"\x00" + b"\x02\x02\x44\x01\x00")
            gif = (b"GIF89a" + struct.pack("<HH", 2, 1) + b"\x80\x00\x00" + pal
                   + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00" + frame(5) + frame(0) + b"\x3b")
            p = os.path.join(self.tmp, "anim.gif")
            with open(p, "wb") as f:
                f.write(gif)
            self.assertEqual(gif_info(p), {"w": 2, "h": 1, "frames": 2, "ms": 150, "loop": 0})
            with open(p, "wb") as f:
                f.write(gif[:-12])                                    # truncated mid-frame: still answers
            self.assertEqual(gif_info(p)["frames"], 1)
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            self.assertIsNone(gif_info(p))

        @timed("shader & IES previews")
        def test_shader_previews(self):
            j = lambda n: os.path.join(self.tmp, n)
            # MaterialX: one standard_surface with a texture that exists
            open(j("albedo.png"), "w").close()
            with open(j("red.mtlx"), "w") as f:
                f.write('<?xml version="1.0"?>\n<materialx version="1.38">\n'
                        '  <image name="tex" type="color3"><input name="file" type="filename" value="albedo.png"/></image>\n'
                        '  <standard_surface name="SR_red" type="surfaceshader">\n'
                        '    <input name="base_color" type="color3" value="0.8, 0.1, 0.1"/>\n'
                        '    <input name="specular_roughness" type="float" value="0.3"/>\n'
                        '    <input name="metalness" type="float" value="1"/>\n'
                        '  </standard_surface>\n'
                        '  <surfacematerial name="M_red" type="material"><input name="surfaceshader" type="surfaceshader" nodename="SR_red"/></surfacematerial>\n'
                        '</materialx>\n')
            self.assertEqual(shader_preview_kind(j("red.mtlx")), "materialx")
            r = shader_preview(j("red.mtlx"))
            self.assertNotIn("error", r, r.get("error"))
            m = r["materials"][0]
            self.assertAlmostEqual(m["base_color"][0], 0.8, 3)
            self.assertEqual(m.get("metallic"), 1.0)
            self.assertEqual(r["code"][1], "xml")
            mats = {n[0]: n for n in r["tree"]}["Materials"][2]
            self.assertTrue(mats[0][3].startswith("material:"))
            # Unity .mat (YAML) vs a MATLAB .mat that only shares the extension
            with open(j("Brick.mat"), "w") as f:
                f.write("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\nMaterial:\n  m_Name: Brick\n"
                        "  m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}\n"
                        "  m_SavedProperties:\n    m_Floats:\n    - _Metallic: 0\n    - _Glossiness: 0.25\n"
                        "    m_Colors:\n    - _Color: {r: 0.6, g: 0.3, b: 0.2, a: 1}\n")
            with open(j("data.mat"), "wb") as f:
                f.write(b"MATLAB 5.0 MAT-file" + b"\0" * 64)
            self.assertEqual(shader_preview_kind(j("Brick.mat")), "unity-mat")
            self.assertEqual(shader_preview_kind(j("data.mat")), "")
            r = shader_preview(j("Brick.mat"))
            self.assertNotIn("error", r, r.get("error"))
            self.assertAlmostEqual(r["materials"][0]["base_color"][1], 0.0732, 3)        # Unity stores sRGB
            # GLSL: Shadertoy and glslsandbox styles are runnable, a vertex shader isn't; F# .fs isn't a shader
            toy = "void mainImage(out vec4 c, in vec2 p) { c = vec4(p / iResolution.xy, 0.5 + 0.5 * sin(iTime), 1.0); }\n"
            box = "uniform float time;\nuniform vec2 resolution;\nvoid main() { gl_FragColor = vec4(gl_FragCoord.xy / resolution, 0.0, 1.0); }\n"
            vert = "#version 330\nlayout(location = 0) in vec3 P;\nuniform mat4 mvp;\nvoid main() { gl_Position = mvp * vec4(P, 1.0); }\n"
            self.assertEqual(glsl_style(toy), "shadertoy")
            self.assertEqual(glsl_style(box), "sandbox")
            self.assertEqual(glsl_style(vert), "")
            with open(j("wave.frag"), "w") as f:
                f.write(toy)
            with open(j("Program.fs"), "w") as f:
                f.write("module Program\nopen System\n[<EntryPoint>]\nlet main argv = printfn \"hi\"; 0\n")
            self.assertEqual(shader_preview_kind(j("Program.fs")), "")
            r = shader_preview(j("wave.frag"))
            self.assertEqual(r["glsl"]["style"], "shadertoy")
            frag, _ = glsl_build_fragment(toy, "shadertoy", "#version 150 core\n")
            self.assertIn("mainImage(c, gl_FragCoord.xy)", frag)
            self.assertIn("#line 0", frag)                                 # GLSL 1.50 numbers the next line N+1
            self.assertIn("#line 1", glsl_build_fragment(box, "sandbox", "#version 300 es\nprecision highp float;\n")[0])
            self.assertEqual(glsl_error_text(RuntimeError("Shader compile failure (0): b'0:3(2): error: syntax error\\n'", [], 0)),
                             "0:3(2): error: syntax error")
            # IES: a small symmetric downlight
            with open(j("down.ies"), "w") as f:
                f.write("IESNA:LM-63-2002\n[MANUFAC] Test\nTILT=NONE\n1 1000 1 5 1 1 2 0.1 0.1 0\n1 1 50\n"
                        "0 22.5 45 67.5 90\n0\n800 700 400 100 0\n")
            self.assertEqual(shader_preview_kind(j("down.ies")), "ies")
            if np is not None:
                ies = IESProfile(j("down.ies"))
                self.assertEqual(len(ies.v), 5)
                self.assertAlmostEqual(float(ies.cd.max()), 800.0)
                r = shader_preview(j("down.ies"))
                self.assertNotIn("error", r, r.get("error"))
                img = r["images"][0][1]
                self.assertEqual(len(img.rgb), img.w * img.h * 3)
                ball = render_material_ball(dict(MATERIAL_DEFAULTS, base_color=(0.8, 0.1, 0.1)), size=64)
                px = np.frombuffer(ball.rgb, np.uint8).reshape(ball.h, ball.w, 3)
                c = px[ball.h // 2, ball.w // 2].astype(int)
                self.assertGreater(c[0], c[1] + 20)                       # the ball is red where it faces us

        def test_vdb_reader(self):
            self.assertEqual(_lz4_block_decode(bytes([0x35]) + b"abc" + bytes([3, 0]), 12), b"abcabcabcabc")
            if np is None:
                self.skipTest("numpy not installed")
            path = os.path.join(self.tmp, "fog.vdb")
            write_test_vdb(path)
            got = load_vdb_native(path)
            self.assertIsNotNone(got, LAST_VFX_ERROR)
            self.assertEqual(len(got["vD"]), 5)
            # voxel (i, j, k) → translate + 0.5 · ijk; the leaf sits at index (8, 16, 24)
            self.assertTrue(np.allclose(sorted(got["vP"][:, 0]), [10 + 0.5 * x for x in (8, 9, 10, 11, 12)]))
            self.assertAlmostEqual(float(got["vD"].max()), 1.0, places=5)
            # sequences: a later frame loaded with the first frame's norm lands in the same box
            a = load_model(path)
            b = load_model(path, 500000, a["norm"], 1000)
            self.assertEqual(a["norm"], b["norm"])
            self.assertTrue(np.allclose(a["vpts"], b["vpts"]))
            import types
            E = lambda n: types.SimpleNamespace(name=n, path="/s/" + n, is_dir=False)
            seq = MainWindow._cache_selection([E("Tornado.ember"), E("smoke.10.vdb"), E("smoke.9.vdb"), E("smoke.100.vdb"), E("a.bgeo.sc")])
            self.assertEqual([x.name for x in seq], ["smoke.9.vdb", "smoke.10.vdb", "smoke.100.vdb"])
            self.assertIsNone(MainWindow._cache_selection([E("a.vdb"), E("b.txt")]))

        @timed("scene breakdowns (Unity, Maya)")
        def test_scene_breakdowns(self):
            # a tiny Unity project: scene with a parent/child, a light, a script and a prefab instance
            proj = os.path.join(self.tmp, "UnityProj")
            for d in ("Assets/Scripts", "Assets/Prefabs", "ProjectSettings"):
                os.makedirs(os.path.join(proj, d), exist_ok=True)
            with open(os.path.join(proj, "ProjectSettings", "ProjectVersion.txt"), "w") as f:
                f.write("m_EditorVersion: 2022.3.10f1\n")
            for rel, g in (("Assets/Scripts/Spin.cs", "a" * 32), ("Assets/Prefabs/Tree.prefab", "b" * 32)):
                open(os.path.join(proj, rel), "w").close()
                with open(os.path.join(proj, rel + ".meta"), "w") as f:
                    f.write(f"fileFormatVersion: 2\nguid: {g}\n")
            scene = os.path.join(proj, "Assets", "Main.unity")
            yaml_lines = [
                "%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:",
                "--- !u!1 &100", "GameObject:", "  m_Name: World", "  m_IsActive: 1", "  m_Component:",
                "  - component: {fileID: 101}",
                "--- !u!4 &101", "Transform:", "  m_GameObject: {fileID: 100}", "  m_Children:", "  - {fileID: 201}",
                "  m_Father: {fileID: 0}",
                "--- !u!1 &200", "GameObject:", "  m_Name: Sun", "  m_Component:", "  - component: {fileID: 201}",
                "  - component: {fileID: 202}", "  - component: {fileID: 203}",
                "--- !u!4 &201", "Transform:", "  m_GameObject: {fileID: 200}", "  m_Children: []", "  m_Father: {fileID: 101}",
                "--- !u!108 &202", "Light:", "  m_GameObject: {fileID: 200}", "  m_Type: 1",
                "--- !u!114 &203", "MonoBehaviour:", "  m_GameObject: {fileID: 200}",
                "  m_Script: {fileID: 11500000, guid: " + "a" * 32 + ", type: 3}",
                "--- !u!1001 &300", "PrefabInstance:", "  m_Modification:", "    m_TransformParent: {fileID: 0}",
                "    m_Modifications:", "    - target: {fileID: 5, guid: " + "b" * 32 + ",", "        type: 3}",
                "      propertyPath: m_Name", "      value: Oak", "      objectReference: {fileID: 0}",
                "  m_SourcePrefab: {fileID: 100100000, guid: " + "b" * 32 + ", type: 3}",
                "--- !u!114 &400", "MonoBehaviour:", "  m_GameObject: {fileID: 100}",
                "  m_Script: {fileID: 11500000, guid: " + "c" * 32 + ", type: 3}"]
            with open(scene, "w") as f:
                f.write("\n".join(yaml_lines) + "\n")
            self.assertEqual(scene_breakdown_kind(scene), "unity")
            r = scene_breakdown(scene)
            self.assertNotIn("error", r, r.get("error"))
            self.assertIn(("Unity", "2022.3.10f1"), r["info"])
            tree = {n[0]: n for n in r["tree"]}
            hier = tree["Hierarchy"][2]
            self.assertEqual([n[0] for n in hier], ["World", "Oak"])
            self.assertEqual(hier[0][2][0][0], "Sun")
            self.assertIn("Light (Directional)", hier[0][2][0][1])
            self.assertIn("Spin", hier[0][2][0][1])
            refs = {g[0]: g for g in tree["References"][2]}
            self.assertTrue(refs["Scripts"][2][0][3].endswith(os.path.join("Assets", "Scripts", "Spin.cs")))
            self.assertIs(refs["Scripts"][2][0][4], True)
            self.assertIn("Unresolved (missing or in an unindexed package)", refs)       # guid ccc... has no asset
            # Maya ASCII: hierarchy, a reference, a found and a missing texture, a plug-in
            tex = os.path.join(self.tmp, "wood.png")
            open(tex, "w").close()
            ma = os.path.join(self.tmp, "shot.ma")
            ma_lines = [
                "//Maya ASCII 2024 scene",
                'file -rdi 1 -ns "chr" -rfn "chrRN" -typ "mayaAscii"', '\t\t"' + self.tmp + '/char.ma";',
                'requires maya "2024";', 'requires "mtoa" "5.3.0";',
                "currentUnit -l centimeter -a degree -t film;", 'fileInfo "product" "Maya 2024";',
                'createNode transform -n "grp";', 'createNode transform -n "box" -p "grp";',
                'createNode mesh -n "boxShape" -p "box";',
                'createNode file -n "file1";', '\tsetAttr ".ftn" -type "string" "' + tex + '";',
                'createNode file -n "file2";', '\tsetAttr ".ftn" -type "string" "/no/such/dir/rust.<UDIM>.exr";']
            with open(ma, "w") as f:
                f.write("\n".join(ma_lines) + "\n")
            self.assertEqual(scene_breakdown_kind(ma), "maya")
            r = scene_breakdown(ma)
            self.assertNotIn("error", r, r.get("error"))
            tree = {n[0]: n for n in r["tree"]}
            self.assertEqual(tree["Outliner"][2][0][0], "grp")
            self.assertEqual(tree["Outliner"][2][0][2][0][0], "box")
            refs = {g[0]: g for g in tree["References"][2]}
            self.assertEqual(refs["Scene references"][2][0][0], "chr")
            self.assertIs(refs["Scene references"][2][0][4], False)                    # char.ma doesn't exist
            texs = {n[0]: n[4] for n in refs["Textures"][2]}
            self.assertEqual(texs, {"wood.png": True, "rust.<UDIM>.exr": False})
            self.assertIn("mtoa", [n[0] for n in tree["Plug-ins required"][2]])
            self.assertIn("rust.<UDIM>.exr", breakdown_text(r))
            # files that only share an extension
            matlab = os.path.join(self.tmp, "data.mat")
            with open(matlab, "wb") as f:
                f.write(b"MATLAB 5.0 MAT-file")
            self.assertEqual(scene_breakdown_kind(matlab), "")

        @timed("file clipboard formats")
        def test_file_clipboard(self):
            fc = FileClipboard()
            img = os.path.join(self.tmp, "clip pic.jpg")
            txt = os.path.join(self.tmp, "notes.txt")
            for p in (img, txt):
                with open(p, "wb") as f:
                    f.write(b"\xff\xd8\xff" + b"0" * 64)
            t1 = fc.targets_for([img])
            self.assertEqual(t1[:2], ["image/png", "image/jpeg"])
            self.assertIn("text/uri-list", t1)
            t2 = fc.targets_for([img, txt])
            self.assertNotIn("image/png", t2)
            self.assertEqual(t2[0], "x-special/gnome-copied-files")
            fc.mode, fc.paths = "cut", [img, txt]
            fc.uris = [Gio.File.new_for_path(p).get_uri() for p in fc.paths]
            fc.image_path = img
            ul = fc.payload("text/uri-list").decode()
            self.assertTrue(ul.endswith("\r\n") and ul.count("\r\n") == 2)
            self.assertIn("clip%20pic.jpg", ul)
            g = fc.payload("x-special/gnome-copied-files").decode().split("\n")
            self.assertEqual(g[0], "cut")
            self.assertEqual(len(g), 3)
            self.assertEqual(fc.payload("image/jpeg")[:3], b"\xff\xd8\xff")

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
            # frame offset keeps padding; overlapping shift (1001..1003 → 1002..1004) goes through the two-phase rename
            self.assertEqual(offset_number("shot.1001", 60), ("shot.1061", ""))
            self.assertEqual(offset_number("f0009", 3), ("f0012", ""))
            self.assertEqual(offset_number("sh010_v004.0999", 2), ("sh010_v004.1001", ""))
            self.assertEqual(offset_number("sh010_v004.1001", 5, "first"), ("sh015_v004.1001", ""))
            self.assertEqual(offset_number("a.0003", -5)[1], "frame would be negative")
            self.assertEqual(offset_number("nonum", 5)[1], "no number to offset")
            seq = [os.path.join(d, f"img.{f}.exr") for f in (1001, 1002, 1003)]
            for p_ in seq:
                open(p_, "w").close()
            plan = [(p_, os.path.join(d, "img." + offset_number(os.path.basename(p_)[:-4], 1)[0].split(".")[1] + ".exr")) for p_ in seq]
            done, errors = apply_renames(plan)
            self.assertFalse(errors)
            self.assertEqual(sorted(os.listdir(d))[-3:], ["img.1002.exr", "img.1003.exr", "img.1004.exr"])
            # repath into a folder that doesn't exist yet
            self.assertEqual(resolve_dest_dir(os.path.join(d, "x.exr"), "../{parent}_v2"), os.path.normpath(os.path.join(d, "..", "ren_v2")))
            target = resolve_dest_dir(os.path.join(d, "a.txt"), "../moved/{ext}")
            done, errors = apply_renames([(os.path.join(d, "a.txt"), os.path.join(target, "a_renamed.txt"))])
            self.assertFalse(errors)
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "moved", "txt", "a_renamed.txt")))

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


# --------------------------------------------------------------------------- DCC integration test (Houdini / Maya)
_DCC_HOU_GEN = r'''
import sys, os, traceback
out = sys.argv[1]
import hou
def step(name, fn):
    try:
        fn()
        print("GEN-OK", name)
    except Exception:
        print("GEN-FAIL", name, traceback.format_exc().strip().splitlines()[-1])
geo = hou.node("/obj").createNode("geo", "kestrel_gen")
for c in geo.children():
    c.destroy()
sph = geo.createNode("sphere")
for k, v in (("type", "polymesh"), ("rows", 48), ("cols", 64)):
    try:
        sph.parm(k).set(v)
    except Exception:
        pass
def mesh():
    sph.geometry().saveToFile(os.path.join(out, "hou_mesh.bgeo.sc"))
def particles():
    sc = geo.createNode("scatter")
    sc.setFirstInput(sph)
    for k in ("npts", "forcetotal"):
        try:
            sc.parm(k).set(200000 if k == "npts" else 1)
        except Exception:
            pass
    w = geo.createNode("attribwrangle")
    w.setFirstInput(sc)
    w.parm("snippet").set("@Cd = relbbox(0, @P);")
    g = w.geometry()
    g.saveToFile(os.path.join(out, "hou_particles.bgeo.sc"))
    print("GEN-INFO particles", len(g.points()))
def vdb(fog):
    v = geo.createNode("vdbfrompolygons")
    v.setFirstInput(sph)
    for k, val in (("voxelsize", 0.02), ("builddistance", 0 if fog else 1), ("buildfog", 1 if fog else 0)):
        try:
            v.parm(k).set(val)
        except Exception:
            pass
    g = v.geometry()
    if v.errors():
        raise RuntimeError("; ".join(v.errors()))
    g.saveToFile(os.path.join(out, "hou_fog.vdb" if fog else "hou_sdf.vdb"))
def abc():
    r = geo.createNode("rop_alembic")
    r.setFirstInput(sph)
    r.parm("filename").set(os.path.join(out, "hou_mesh.abc"))
    r.parm("trange").set(0)
    r.render()
step("mesh .bgeo.sc", mesh)
step("particles .bgeo.sc", particles)
step("fog .vdb", lambda: vdb(True))
step("sdf .vdb", lambda: vdb(False))
step("alembic .abc", abc)
'''

_DCC_MAYA_GEN = r'''
import sys, os, traceback
out = sys.argv[1]
import maya.standalone
maya.standalone.initialize(name="python")
import maya.cmds as cmds, maya.mel as mel
for p in ("fbxmaya", "AbcExport", "mayaUsdPlugin", "objExport"):
    try:
        cmds.loadPlugin(p, quiet=True)
    except Exception:
        pass
s = cmds.polySphere(sx=64, sy=48, r=1)[0]
cmds.polyCube(w=1, h=0.4, d=1.4)
cmds.move(0, -1.2, 0)
def step(name, fn):
    try:
        fn()
        print("GEN-OK", name)
    except Exception:
        print("GEN-FAIL", name, traceback.format_exc().strip().splitlines()[-1])
step("fbx", lambda: (cmds.select(all=True), mel.eval('FBXExport -f "%s" -s' % os.path.join(out, "maya_mesh.fbx").replace("\\", "/"))))
step("abc", lambda: cmds.AbcExport(j="-frameRange 1 1 -file %s" % os.path.join(out, "maya_mesh.abc")))
def usd():
    p = os.path.join(out, "maya_mesh.usd")
    if hasattr(cmds, "mayaUSDExport"):
        cmds.mayaUSDExport(file=p)
    else:
        try:
            cmds.loadPlugin("mayaUsdPlugin")
        except Exception as ex:
            raise RuntimeError("Maya USD plugin (mayaUsdPlugin) is not available to mayapy: %s" % ex)
        cmds.file(p, force=True, type="USD Export", exportAll=True)
step("usd", usd)
step("obj", lambda: cmds.file(os.path.join(out, "maya_mesh.obj"), force=True, options="groups=1;ptgroups=1;materials=0;smoothing=1;normals=1", type="OBJexport", exportAll=True))
'''


def _png_write(path: str, w: int, h: int, rgb: bytes):
    import zlib
    raw = b"".join(b"\0" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def run_dcc_test(extra: List[str]) -> int:
    """kestrel.py --dcc-test [files…]: generate test caches with the installed Houdini and Maya, read every one
    back through each route Kestrel has (hython, gconvert, mayapy, Houdini-for-Alembic), render a frame of each
    and write ~/kestrel-dcc-test/report.txt + sheet.png. Extra files given on the command line are tested too."""
    global LAST_VFX_ERROR
    out = os.path.join(HOME, "kestrel-dcc-test")
    os.makedirs(out, exist_ok=True)
    rep: List[str] = []

    def log(s=""):
        print(s, flush=True)
        rep.append(s)
    hou = _find_houdini()
    mp = _find_mayapy()
    log(f"Kestrel {VERSION} DCC test   {time.strftime('%Y-%m-%d %H:%M')}")
    log(f"python {sys.version.split()[0]}  numpy {np.__version__ if np is not None else 'MISSING'}")
    log(f"hython   {hou['hython'] or '—'}   (Houdini {hou['version'] or '?'})")
    log(f"gconvert {hou['gconvert'] or '—'}")
    log(f"mayapy   {mp or '—'}")
    bl = _find_blender()
    log(f"blender  {bl[0] if bl else '—'}")
    if np is None:
        log("numpy is required for Houdini caches — install python3-numpy and re-run.")
    import tempfile
    log("")
    log("== generating test files")
    for tool, script, label in ((hou["hython"], _DCC_HOU_GEN, "Houdini"), (mp, _DCC_MAYA_GEN, "Maya")):
        if not tool:
            log(f"  {label}: not found, skipped")
            continue
        sp = os.path.join(tempfile.gettempdir(), f"kestrel-gen-{label.lower()}.py")
        with open(sp, "w") as f:
            f.write(script)
        t0 = time.time()
        rc, so, se = _run_streaming([tool, sp, out], timeout=400)
        gen = [l for l in so if l.startswith("GEN-")]
        log(f"  {label} ({time.time() - t0:.0f}s, exit {rc}):")
        for l in gen:
            log("    " + l)
        if not gen:
            log("    no output; last lines: " + " | ".join(x[:160] for x in (so + se)[-6:]))
    cases = []
    for n, routes in (("hou_mesh.bgeo.sc", ("houdini", "gconvert")), ("hou_particles.bgeo.sc", ("houdini", "gconvert")),
                      ("hou_fog.vdb", ("houdini", "openvdb", "blender")), ("hou_sdf.vdb", ("houdini", "openvdb", "blender")),
                      ("hou_mesh.abc", ("houdini-abc", "maya", "blender")),
                      ("maya_mesh.fbx", ("maya", "blender")), ("maya_mesh.abc", ("maya", "houdini-abc", "blender")),
                      ("maya_mesh.usd", ("maya", "blender")), ("maya_mesh.obj", ("maya", "builtin"))):
        p = os.path.join(out, n)
        if os.path.exists(p):
            cases += [(p, r) for r in routes]
    for p in extra:
        if os.path.exists(p):
            cases.append((os.path.abspath(p), "auto"))
    log("")
    log("== reading back")
    tiles = []
    for p, route in cases:
        LAST_VFX_ERROR = ""
        t0 = time.time()
        res, err = None, ""
        try:
            if route == "auto":
                m = load_model(p, 500000)
            elif route == "builtin":
                m = load_model(p, 500000)
            elif route in ("houdini", "gconvert", "openvdb"):
                got = {"houdini": load_via_houdini, "gconvert": load_via_gconvert, "openvdb": load_vdb_openvdb}[route](p)
                m = _finish_cache_numpy(got, 500000) if got else None
            else:
                fn = {"maya": load_via_maya, "blender": load_via_blender, "houdini-abc": load_via_houdini_mesh}[route]
                got = fn(p)
                m = _finish_model_numpy(got[0], got[1], 500000, len(got[1]), [("Source", got[2])]) if got and np is not None else None
            if m and "error" in m:
                err, m = m["error"], None
            res = m
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
        dt = time.time() - t0
        name = f"{os.path.basename(p)} via {route}"
        if res is None:
            log(f"  FAIL  {name:<40} {dt:5.1f}s  {(err or LAST_VFX_ERROR or 'no result')[:300]}")
            continue
        parts = []
        if len(res["faces"]):
            parts.append(f"{len(res['faces']):,} tris")
        if "points" in res:
            parts.append(f"{len(res['points']):,} points" + (" +Cd" if "pcolor" in res else ""))
        if "vpts" in res:
            parts.append(f"{len(res['vpts']):,} voxels")
        log(f"  OK    {name:<40} {dt:5.1f}s  {', '.join(parts) or 'EMPTY'}")
        for k, v in res.get("info", []):
            if k in ("Volumes", "Point attributes", "Source", "Houdini"):
                log(f"          {k}: {v}")
        if np is not None:
            EX = {k: res[k] for k in ("points", "pcolor", "psize", "vpts", "vden", "vsize") if k in res} or None
            V = np.asarray(res["verts"], np.float32).reshape(-1, 3)
            F = np.asarray(res["faces"], np.int32).reshape(-1, 3)
            N = np.asarray(res["normals"], np.float32).reshape(-1, 3) if len(res.get("normals", [])) else None
            VN = np.asarray(res["vnormals"], np.float32).reshape(-1, 3) if len(res.get("vnormals", [])) else None
            fr = software_render(V, F, N, VN, (320, 240, 0.6, 0.35, 1.0, 0, 0, True, False, (40, 40, 44), (1, 1, 1)), EX)
            if fr:
                rgb = np.frombuffer(fr[2], np.uint8).reshape(240, 320, 4)[:, :, [2, 1, 0]]
                tiles.append((name, rgb))
    if tiles and np is not None:
        cols = 3
        rows = (len(tiles) + cols - 1) // cols
        sheet = np.full((rows * 244, cols * 324, 3), 20, np.uint8)
        for i, (_, rgb) in enumerate(tiles):
            r, c = divmod(i, cols)
            sheet[r * 244 + 2:r * 244 + 242, c * 324 + 2:c * 324 + 322] = rgb
        _png_write(os.path.join(out, "sheet.png"), sheet.shape[1], sheet.shape[0], sheet.tobytes())
        log("")
        log("contact sheet, left→right, top→bottom: " + "; ".join(n for n, _ in tiles))
    with open(os.path.join(out, "report.txt"), "w") as f:
        f.write(CrashReporter.sanitize("\n".join(rep) + "\n"))
    print(f"\nWrote {out}/report.txt and sheet.png")
    return 0
