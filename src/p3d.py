

# --------------------------------------------------------------------------- FBX / USD / Alembic loaders
def _fbx_binary(data: bytes):
    """Walk a binary FBX node tree and collect every Geometry's Vertices + PolygonVertexIndex."""
    version = struct.unpack_from("<I", data, 23)[0]
    big = version >= 7500
    hdr = struct.Struct("<QQQB") if big else struct.Struct("<IIIB")
    meshes = []
    pos = 27

    def read_prop(p):
        t = data[p:p + 1]
        p += 1
        if t in (b"Y",):
            return struct.unpack_from("<h", data, p)[0], p + 2
        if t == b"C":
            return data[p], p + 1
        if t == b"I":
            return struct.unpack_from("<i", data, p)[0], p + 4
        if t == b"F":
            return struct.unpack_from("<f", data, p)[0], p + 4
        if t == b"D":
            return struct.unpack_from("<d", data, p)[0], p + 8
        if t == b"L":
            return struct.unpack_from("<q", data, p)[0], p + 8
        if t in (b"f", b"d", b"l", b"i", b"b"):
            n, enc, clen = struct.unpack_from("<III", data, p)
            p += 12
            raw = data[p:p + clen]
            p += clen
            if enc == 1:
                raw = zlib.decompress(raw)
            fmt = {b"f": "f", b"d": "d", b"l": "q", b"i": "i", b"b": "b"}[t]
            import array
            arr = array.array(fmt)
            arr.frombytes(raw[:n * arr.itemsize])
            return arr, p
        if t in (b"S", b"R"):
            n = struct.unpack_from("<I", data, p)[0]
            return data[p + 4:p + 4 + n], p + 4 + n
        raise ValueError(f"unknown FBX property type {t!r}")

    def read_node(p, depth):
        end, nprops, plen, nlen = hdr.unpack_from(data, p)
        if end == 0:
            return None, p + hdr.size
        p += hdr.size
        name = data[p:p + nlen]
        p += nlen
        props = []
        pend = p + plen
        want = name in (b"Vertices", b"PolygonVertexIndex") or depth < 3
        while p < pend:
            v, p2 = read_prop(p) if want else (None, None)
            if not want:
                break
            props.append(v)
            p = p2
        p = pend
        children = {}
        if name in (b"Objects", b"Geometry") or depth == 0:
            while p < end:
                cname, cprops, p = read_node_full(p, depth + 1)
                if cname is None:
                    break
                if cname in (b"Geometry", b"Vertices", b"PolygonVertexIndex"):
                    children.setdefault(cname, []).append(cprops)
        if name == b"Geometry" and b"Vertices" in children and b"PolygonVertexIndex" in children:
            meshes.append((children[b"Vertices"][0][0], children[b"PolygonVertexIndex"][0][0]))
        return name, props, end

    def read_node_full(p, depth):
        r = read_node(p, depth)
        if r[0] is None:
            return None, None, r[1]
        return r

    while pos + hdr.size <= len(data):
        name, props, pos = read_node_full(pos, 0)
        if name is None:
            break
    return meshes


def _fbx_ascii(text: str):
    import re
    meshes = []
    for gm in re.finditer(r"Geometry:[^{]*\{(.*?)\n\t\}", text, re.S):
        body = gm.group(1)
        v = re.search(r"Vertices:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", body, re.S)
        i = re.search(r"PolygonVertexIndex:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", body, re.S)
        if v and i:
            verts = [float(x) for x in v.group(1).replace("\n", "").split(",") if x.strip()]
            idx = [int(x) for x in i.group(1).replace("\n", "").split(",") if x.strip()]
            meshes.append((verts, idx))
    return meshes


def load_fbx(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"Kaydara FBX Binary"):
        meshes = _fbx_binary(data)
    else:
        meshes = _fbx_ascii(data.decode("latin-1"))
    verts, faces = [], []
    for vflat, idx in meshes:
        base = len(verts)
        verts.extend((vflat[i], vflat[i + 1], vflat[i + 2]) for i in range(0, len(vflat) - 2, 3))
        poly = []
        for raw in idx:
            last = raw < 0
            poly.append((~raw if last else raw) + base)
            if last:
                for k in range(1, len(poly) - 1):
                    faces.append((poly[0], poly[k], poly[k + 1]))
                poly = []
    return verts, faces, len(meshes)


def load_usda_text(text: str):
    """Pull points / faceVertexIndices / faceVertexCounts out of every Mesh prim in a USDA file."""
    import re
    verts, faces = [], []
    nmesh = 0
    for m in re.finditer(r"def\s+Mesh\s+\"[^\"]*\"[^{]*\{", text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            c = text[i]
            depth += (c == "{") - (c == "}")
            i += 1
        body = text[start:i]
        p = re.search(r"point3f\[\]\s+points\s*=\s*\[(.*?)\]", body, re.S)
        fi = re.search(r"int\[\]\s+faceVertexIndices\s*=\s*\[(.*?)\]", body, re.S)
        fc = re.search(r"int\[\]\s+faceVertexCounts\s*=\s*\[(.*?)\]", body, re.S)
        if not (p and fi and fc):
            continue
        base = len(verts)
        pts = re.findall(r"\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)", p.group(1))
        verts.extend((float(a), float(b), float(c)) for a, b, c in pts)
        idx = [int(x) + base for x in fi.group(1).replace("\n", " ").split(",") if x.strip()]
        counts = [int(x) for x in fc.group(1).replace("\n", " ").split(",") if x.strip()]
        k = 0
        for n in counts:
            poly = idx[k:k + n]
            k += n
            for j in range(1, len(poly) - 1):
                faces.append((poly[0], poly[j], poly[j + 1]))
        nmesh += 1
    return verts, faces, nmesh


_MESH_SCRIPT = r'''
import sys
path = sys.argv[1]
def emit(verts, counts, idx, base):
    for v in verts: sys.stdout.write("v %r %r %r\n" % (float(v[0]), float(v[1]), float(v[2])))
    k = 0
    for n in counts:
        poly = [int(idx[k + j]) + base + 1 for j in range(int(n))]
        k += int(n)
        sys.stdout.write("f " + " ".join(map(str, poly)) + "\n")
    return base + len(verts)
if path.lower().endswith(".abc"):
    import alembic
    from alembic.Abc import IArchive
    from alembic.AbcGeom import IPolyMesh
    arch = IArchive(path)
    base = 0
    def walk(obj):
        global base
        if IPolyMesh.matches(obj.getMetaData()):
            samp = IPolyMesh(obj.getParent(), obj.getName()).getSchema().getValue()
            base = emit(samp.getPositions(), samp.getFaceCounts(), samp.getFaceIndices(), base)
        for i in range(obj.getNumChildren()): walk(obj.getChild(i))
    walk(arch.getTop())
else:
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.Open(path)
    base = 0
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            m = UsdGeom.Mesh(prim)
            xf = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            pts = [xf.Transform(p) for p in (m.GetPointsAttr().Get() or [])]
            base = emit(pts, m.GetFaceVertexCountsAttr().Get() or [], m.GetFaceVertexIndicesAttr().Get() or [], base)
'''


def alembic_format(path: str) -> str:
    """'ogawa' (modern, what Blender reads), 'hdf5' (pre-2014 legacy container) or 'unknown' from the file's magic bytes."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"
    if head.startswith(b"Ogawa"):
        return "ogawa"
    if head == b"\x89HDF\r\n\x1a\n":
        return "hdf5"
    return "unknown"


def load_abc_hdf5(path: str) -> Optional[Tuple[list, list, str]]:
    """Read a legacy HDF5-container Alembic with h5py (pip install --user h5py; the wheel bundles libhdf5, no
    compile). Alembic's HDF5 backend stores a property's first sample as the dataset '<prop>.smp0' inside the
    schema group (e.g. ABC/<obj>/<shape>/.prop/.geom/P.smp0), so a PolyMesh is any group holding 'P',
    '.faceCounts' and '.faceIndices' samples.
    The walk is name-based and structure-agnostic on purpose: it survives the schema nesting differences between
    Alembic 1.0–1.5 writers. Positions are object-local (no xform stack), which is fine for a preview."""
    try:
        import h5py  # type: ignore
    except ImportError:
        _vfx_err("h5py: not installed (python3 -m pip install --user h5py) — needed to read HDF5 Alembic without Maya")
        return None
    import re
    # first sample of a property is the dataset '<prop>.smp0'; later samples live under '<prop>.smpi/0001…'
    pat = re.compile(r"^(P|\.faceCounts|\.faceIndices)\.smp0$")
    meshes: dict = {}
    seen_names: List[str] = []
    try:
        with h5py.File(path, "r") as h5:
            def visit(name, obj):
                if not isinstance(obj, h5py.Dataset):
                    return
                base = name.rsplit("/", 1)[-1]
                if len(seen_names) < 40:
                    seen_names.append(name)
                mobj = pat.match(base)
                if not mobj:
                    return
                prop = mobj.group(1)
                parent = name.rsplit("/", 1)[0] if "/" in name else ""
                meshes.setdefault(parent, {})[prop] = (0, name)
            h5.visititems(visit)
            verts, faces = [], []
            for parent, props in meshes.items():
                if not all(k in props for k in ("P", ".faceCounts", ".faceIndices")):
                    continue
                import numpy as np
                P = np.asarray(h5[props["P"][1]][()])
                counts = np.asarray(h5[props[".faceCounts"][1]][()], dtype=np.int64)
                idx = np.asarray(h5[props[".faceIndices"][1]][()], dtype=np.int64)
                pts = P.reshape(-1, 3).astype(np.float64)
                base = sum(len(v) for v in verts)
                verts.append(pts)
                # vectorised fan triangulation: triangle t of a polygon is (v0, v_t, v_t+1)
                starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
                for t in range(1, int(counts.max()) - 1 if len(counts) else 0):
                    sel = counts > t + 1
                    s0 = starts[sel]
                    faces.append(np.stack((idx[s0], idx[s0 + t], idx[s0 + t + 1]), axis=1) + base)
            if verts:
                nm = sum(1 for p in meshes.values() if "P" in p)
                return (np.concatenate(verts), np.concatenate(faces) if faces else np.zeros((0, 3), dtype=np.int64),
                        f"h5py (HDF5 Alembic, {nm} mesh{'es' if nm != 1 else ''})")
    except Exception as ex:
        _vfx_err(f"h5py: {ex}")
        return None
    _vfx_err("h5py: opened the file but found no PolyMesh samples. Datasets seen: " + ", ".join(seen_names[:12]))
    return None


def _vfx_pythons(include_vendor: bool = False) -> List[str]:
    """Interpreters that might have PyAlembic / pxr: the running one, system python3, and — only on PATH, or
    when include_vendor is set for HDF5 Alembic that nothing open-source can read — Maya's mayapy / Houdini's hython."""
    import glob
    cands = [sys.executable, shutil.which("python3") or "", shutil.which("mayapy") or "", shutil.which("hython") or ""]
    if include_vendor:
        cands += sorted(glob.glob("/usr/autodesk/maya*/bin/mayapy") + glob.glob("/opt/autodesk/maya*/bin/mayapy") +
                        glob.glob("/opt/hfs*/bin/hython"), reverse=True)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and os.path.exists(c):
            seen.add(c)
            out.append(c)
    return out


LAST_VFX_ERROR = ""


def _vfx_err(msg: str):
    """Append one line to the importer error log (shown in the preview pane when every route fails)."""
    global LAST_VFX_ERROR
    LAST_VFX_ERROR = (LAST_VFX_ERROR + "\n" + msg).strip()


def load_via_vfx_python(path: str, include_vendor: bool = False) -> Optional[Tuple[list, list, str]]:
    """Export mesh data through any interpreter that can import alembic / pxr. Returns (verts, faces, interpreter)."""
    global LAST_VFX_ERROR
    errs = []
    for py in _vfx_pythons(include_vendor):
        if BLENDER_VERBOSE:
            print(f"  trying {py} …", flush=True)
        try:
            rc, out_lines, err_lines = _run_streaming([py, "-c", _MESH_SCRIPT, path], timeout=120, verbose=BLENDER_VERBOSE)
        except OSError as ex:
            errs.append(f"{py}: {ex}")
            continue
        r = subprocess.CompletedProcess([py], rc, "\n".join(out_lines), "\n".join(err_lines))
        if r.returncode != 0 or "v " not in r.stdout:
            tail = (r.stderr.strip().splitlines() or [f"exit {r.returncode}, no mesh output"])[-1]
            errs.append(f"{os.path.basename(py)}: {tail}")
            continue
        verts, faces = [], []
        for line in r.stdout.splitlines():
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = [int(x) - 1 for x in line.split()[1:]]
                for k in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[k], idx[k + 1]))
        return verts, faces, os.path.basename(py)
    for e_ in errs[-3:]:
        _vfx_err(e_)
    return None


def preview_tool_report() -> str:
    """Environment report for Preferences → Dependencies → Diagnostics. Deliberately does NOT repeat the
    installed/missing status or install commands — the checklist above it shows those. It reports what the
    checklist can't: versions, which binary was picked when several exist, the OpenGL context outcome, which
    Python modules are importable in which interpreter, and which GStreamer decoders are present."""
    L = []
    cpus = os.cpu_count() or 0
    try:
        ui = sorted(os.sched_getaffinity(threading.get_native_id()))
        aff = f"interface thread on CPU {ui[0]}–{ui[-1]}" if len(ui) < cpus else "interface thread not pinned"
    except Exception:
        aff = "affinity unavailable"
    L.append("Runtime")
    L.append(f"  python     {sys.version.split()[0]}  ({sys.executable})")
    L.append(f"  numpy      {np.__version__ if np is not None else '— (pure-Python fallbacks in use)'}")
    L.append(f"  GTK        {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}   GtkSourceView {'yes' if GtkSource is not None else 'no'}")
    L.append(f"  session    {os.environ.get('XDG_SESSION_TYPE', '?')}, GDK_BACKEND={os.environ.get('GDK_BACKEND', 'auto')}   CPUs {cpus}, {aff}")
    L.append("")
    L.append("OpenGL")
    if HAVE_GL:
        try:
            import OpenGL  # type: ignore
            L.append(f"  PyOpenGL   {OpenGL.__version__}")
        except Exception:
            L.append("  PyOpenGL   importable")
        L.append("  context    " + (GL_STATUS[0] if GL_STATUS else "not created yet"))
    else:
        L.append("  PyOpenGL   not importable in this interpreter")
    L.append("")
    L.append("3D importers")
    bl = _find_blender()
    L.append(f"  blender    {bl[0] if bl else '—'}")
    L.append(f"  assimp     {shutil.which('assimp') or '—'}")
    usd = shutil.which("usdcat")
    L.append(f"  USD        {usd or ('pxr (Python module)' if _has_py_module('pxr') else '—')}")
    L.append(f"  7-Zip      {sevenzip() or '—'}")
    L.append("")
    L.append("Image / video / document converters")
    im = shutil.which("magick") or shutil.which("convert")
    L.append(f"  ImageMagick {im + ('  (IM7)' if im and im.endswith('magick') else '  (IM6)') if im else '—'}")
    for t in ("oiiotool", "imgcvt", "ffmpeg", "ffprobe", "pdftoppm"):
        L.append(f"  {t:<11} {shutil.which(t) or '—'}")
    L.append("")
    L.append("GStreamer")
    if HAVE_GST:
        try:
            L.append(f"  version    {Gst.version_string()}")
        except Exception:
            pass
        have = [n for n in ("gtksink",) + H264_DECODERS + ("avdec_h265", "vp8dec", "vp9dec", "av1dec", "dav1ddec", "avdec_prores", "theoradec", "jpegdec")
                if Gst.ElementFactory.find(n)]
        L.append("  elements   " + (", ".join(have) or "none of the ones Kestrel looks for"))
        dec, why = gst_h264_status()
        L.append("  H.264      " + (dec if dec else "NONE — " + why))
        if dec and dec.startswith("nv") and not Gst.ElementFactory.find("avdec_h264"):
            L.append("  note       decoding on the NVIDIA GPU; libav has no avdec_h264 because EL9's ffmpeg-free omits H.264 — that's expected")
        try:
            reg = Gst.Registry.get()
            for name in ("libav", "openh264", "ugly", "good", "bad", "gtk", "nvcodec", "va", "vaapi"):
                pl = reg.find_plugin(name)
                if pl is not None:
                    L.append(f"  plugin     {name:<9} {pl.get_version():<10} {len(reg.get_feature_list_by_plugin(name))} elements   {pl.get_filename()}")
        except Exception:
            pass
    else:
        L.append(f"  not importable: {GST_ERROR or 'unknown error'}")
        L.append("  needs the Gst-1.0 typelib (package gstreamer1) visible to this Python's gi")
    L.append("")
    L.append("Python modules (numpy: fast paths · h5py: HDF5 Alembic · pxr: USD · OpenGL: GPU preview)")
    seen, interps = set(), []
    for py in _vfx_pythons():
        rp = os.path.realpath(py)
        if rp not in seen:                       # /usr/bin/python and python3 are usually the same binary
            seen.add(rp)
            interps.append(py)
    for py in interps:
        mods = []
        for mod in ("numpy", "h5py", "pxr", "OpenGL"):
            try:
                r = subprocess.run([py, "-c", f"import {mod}; print(getattr({mod}, '__version__', 'ok'))"], capture_output=True, text=True, timeout=60)
                mods.append(f"{mod} {r.stdout.strip()[:12]}" if r.returncode == 0 else f"{mod} —")
            except (OSError, subprocess.TimeoutExpired):
                mods.append(f"{mod} ?")
        L.append(f"  {py}\n      " + "   ".join(mods))
    return "\n".join(L)


# --------------------------------------------------------------------------- open-source converters: Blender, assimp
_BLENDER_SCRIPT = r"""
import bpy, sys, traceback
try:
    argv = sys.argv[sys.argv.index("--") + 1:]
    path = argv[0]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = path.lower().rsplit(".", 1)[-1]
    def first(*names):
        for n in names:
            op = bpy.ops
            try:
                for part in n.split("."):
                    op = getattr(op, part)
                return op
            except AttributeError:
                continue
        return None
    if ext == "abc":
        res = bpy.ops.wm.alembic_import(filepath=path, as_background_job=False)
    elif ext in ("usd", "usda", "usdc", "usdz"):
        res = bpy.ops.wm.usd_import(filepath=path)
    elif ext == "fbx":
        res = bpy.ops.import_scene.fbx(filepath=path)
    elif ext in ("gltf", "glb"):
        res = bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "dae":
        res = bpy.ops.wm.collada_import(filepath=path)
    elif ext == "blend":
        res = bpy.ops.wm.open_mainfile(filepath=path)
    elif ext == "stl":
        res = (first("wm.stl_import") or bpy.ops.import_mesh.stl)(filepath=path)
    elif ext == "ply":
        res = (first("wm.ply_import") or bpy.ops.import_mesh.ply)(filepath=path)
    elif ext == "obj":
        res = (first("wm.obj_import") or bpy.ops.import_scene.obj)(filepath=path)
    elif ext == "x3d":
        res = bpy.ops.import_scene.x3d(filepath=path)
    else:
        res = "unsupported"
    print("KESTREL import:", res)
    dg = bpy.context.evaluated_depsgraph_get()
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    print("KESTREL meshes:", len(meshes), "objects:", len(bpy.data.objects))
    out = []
    base = 0
    for ob in meshes:
        try:
            me = ob.evaluated_get(dg).to_mesh()
        except Exception:
            me = ob.data
        mw = ob.matrix_world
        for v in me.vertices:
            co = mw @ v.co
            out.append("v %r %r %r" % (co.x, co.y, co.z))
        for pg in me.polygons:
            out.append("f " + " ".join(str(i + base + 1) for i in pg.vertices))
        base += len(me.vertices)
    sys.stdout.write("KESTREL-BEGIN\n" + "\n".join(out) + "\nKESTREL-END\n")
    sys.stdout.flush()
except Exception:
    traceback.print_exc(file=sys.stdout)
"""

BLENDER_EXTS = (".abc", ".usd", ".usda", ".usdc", ".usdz", ".fbx", ".gltf", ".glb", ".dae", ".blend", ".x3d", ".stl", ".ply", ".obj")
ASSIMP_EXTS = (".fbx", ".dae", ".gltf", ".glb", ".3ds", ".x3d", ".lwo", ".ms3d", ".md5mesh", ".ase", ".ifc", ".off")


_BLENDER_CACHE: Optional[Tuple[str, List[str]]] = None


def _find_blender() -> Optional[Tuple[str, List[str]]]:
    """Locate Blender → (description, argv prefix). Checks PATH, /opt-style unpacked builds, Flatpak and snap.
    A Flatpak Blender runs in its own sandbox with a private /tmp, so it is launched through `flatpak run`
    with --filesystem grants added per call (see load_via_blender)."""
    global _BLENDER_CACHE
    if _BLENDER_CACHE is not None:
        return _BLENDER_CACHE
    import glob
    found = None
    p = shutil.which("blender")
    if p:
        found = (p, [p])
    if not found:
        for c in sorted(glob.glob("/opt/blender*/blender") + glob.glob("/usr/local/blender*/blender") +
                        glob.glob(os.path.expanduser("~/blender*/blender")) + glob.glob(os.path.expanduser("~/Applications/blender*/blender")), reverse=True):
            if os.access(c, os.X_OK):
                found = (c, [c])
                break
    if not found and shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "info", "org.blender.Blender"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                found = ("flatpak org.blender.Blender", ["flatpak", "run", "org.blender.Blender"])
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not found and os.path.exists("/snap/bin/blender"):
        found = ("/snap/bin/blender", ["/snap/bin/blender"])
    _BLENDER_CACHE = found
    return found


BLENDER_VERBOSE = False      # --probe-model sets this: echo Blender's chatter live


def _run_streaming(argv: List[str], timeout: float, verbose: bool = False) -> Tuple[int, List[str], List[str]]:
    """Run argv in its own process group, collecting stdout lines; on timeout kill the WHOLE group.
    subprocess.run(timeout=) only kills the direct child — with `flatpak run` that leaves bwrap/Blender alive
    holding the pipe, and the caller blocks forever on EOF. Returns (rc, stdout_lines, stderr_lines)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                            start_new_session=True, stdin=subprocess.DEVNULL)
    out, err = [], []
    t0 = time.time()

    def pump(stream, sink, echo):
        for line in stream:
            sink.append(line.rstrip("\n"))
            if echo and verbose and not line.startswith(("v ", "f ")):
                print(f"  [{time.time() - t0:5.1f}s] {line.rstrip()}", flush=True)
    th_o = threading.Thread(target=pump, args=(proc.stdout, out, True), daemon=True)
    th_e = threading.Thread(target=pump, args=(proc.stderr, err, True), daemon=True)
    th_o.start()
    th_e.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        rc = -9
        err.append(f"timed out after {timeout:.0f}s (process group killed)")
    th_o.join(timeout=5)
    th_e.join(timeout=5)
    return rc, out, err


def load_via_blender(path: str) -> Optional[Tuple[list, list, str]]:
    """Headless Blender import → vertices/faces (applies modifiers, world transforms).
    The export script is passed with --python-expr and the mesh comes back on stdout, so nothing depends on a
    shared filesystem — important for the Flatpak build, whose sandbox has a private /tmp."""
    bl = _find_blender()
    if not bl:
        _vfx_err("Blender: not found (sudo dnf install blender, or flatpak install flathub org.blender.Blender)")
        return None
    argv = list(bl[1])
    path = os.path.abspath(path)
    if argv[0] == "flatpak":
        argv[2:2] = [f"--filesystem={os.path.dirname(path)}:ro"]      # the model's folder, whatever the app's defaults are
    argv += ["-b", "--factory-startup", "-noaudio", "--python-exit-code", "3", "--python-expr", _BLENDER_SCRIPT, "--", path]
    if BLENDER_VERBOSE:
        print("  running:", " ".join(shlex.quote(a) if a is not _BLENDER_SCRIPT else "<export script>" for a in argv), flush=True)
    try:
        rc, out, err = _run_streaming(argv, timeout=90, verbose=BLENDER_VERBOSE)
    except OSError as ex:
        _vfx_err(f"Blender: {ex}")
        return None
    if "KESTREL-BEGIN" in out and "KESTREL-END" in out:
        body = out[out.index("KESTREL-BEGIN") + 1:out.index("KESTREL-END")]
        verts, faces = _parse_obj_lines(body)
        if verts:
            return verts, faces, "Blender"
    interesting = [l.strip() for l in out + err
                   if l.strip() and not l.startswith(("v ", "f ")) and ("KESTREL" in l or "rror" in l or "Traceback" in l or "arning" in l or "annot" in l or "timed out" in l)]
    _vfx_err("Blender: " + (" | ".join(x[:160] for x in interesting[-4:]) or f"exit {rc}, no geometry; tail: " + " | ".join((out + err)[-3:])))
    return None


def load_via_assimp(path: str) -> Optional[Tuple[list, list, str]]:
    """assimp CLI export → OBJ text → vertices/faces."""
    global LAST_VFX_ERROR
    a = shutil.which("assimp")
    if not a:
        _vfx_err("assimp: not found (sudo dnf install assimp)")
        return None
    out = os.path.join(tempfile.gettempdir(), f"kestrel-assimp-{os.getpid()}-{abs(hash(path)) & 0xffffff:x}.obj")
    try:
        r = subprocess.run([a, "export", path, out, "-ptv", "-tri"], capture_output=True, text=True, timeout=120)
        if os.path.exists(out):
            verts, faces = _parse_obj_text(out)
            if verts:
                return verts, faces, "assimp"
        _vfx_err(f"assimp: {(r.stderr.strip().splitlines() or ['no output'])[-1][:200]}")
    except (OSError, subprocess.TimeoutExpired) as ex:
        _vfx_err(f"assimp: {ex}")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return None


def _parse_obj_text(path: str) -> Tuple[list, list]:
    with open(path, "rb") as f:
        return _parse_obj_lines(f)


def _parse_obj_lines(lines) -> Tuple[list, list]:
    """Minimal OBJ reader for converter output: 'v x y z' and 'f a b c …' lines (str or bytes); faces fan-triangulated."""
    verts, faces = [], []
    for line in lines:
        if isinstance(line, str):
            line = line.encode()
        if line.startswith(b"v "):
            p = line.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith(b"f "):
            idx = []
            for tok in line.split()[1:]:
                s = tok.split(b"/")[0]
                if s:
                    i = int(s)
                    idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):
                faces.append((idx[0], idx[k], idx[k + 1]))
    return verts, faces


GL_STATUS: List[str] = []       # last OpenGL viewer status line (set by GLModelView), for the diagnostics report


# --------------------------------------------------------------------------- optional dependencies
def _has_py_module(name: str) -> bool:
    try:
        r = subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def optional_deps() -> List[dict]:
    """Everything Kestrel can use but doesn't require, with a check and the Rocky 9 install command.
    Each entry: {key, name, gives, found: str|None, cmd: str}. The commands are plain shell so the user sees
    exactly what runs (Preferences → Optional opens them in a terminal)."""
    bl = _find_blender()
    flatpak = shutil.which("flatpak")
    deps = [
        dict(key="blender", name="Blender", gives="Alembic, USD, FBX, glTF, Collada, .blend model previews",
             found=bl[0] if bl else None,
             cmd=("flatpak install -y flathub org.blender.Blender" if flatpak else
                  "sudo dnf install -y flatpak && flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo && "
                  "flatpak install -y flathub org.blender.Blender")),
        dict(key="assimp", name="assimp", gives="FBX, DAE, glTF, 3DS, X3D model previews",
             found=shutil.which("assimp"), cmd="sudo dnf install -y assimp"),
        dict(key="oiiotool", name="OpenImageIO tools", gives="EXR (all codecs), DPX, TIFF, 16-bit IFF, .tx previews",
             found=shutil.which("oiiotool"), cmd="sudo dnf install -y OpenImageIO-utils"),
        dict(key="magick", name="ImageMagick", gives="DDS, XCF, TGA, SVG and other image formats",
             found=shutil.which("magick") or shutil.which("convert"), cmd="sudo dnf install -y ImageMagick"),
        dict(key="ffmpeg", name="ffmpeg (free build)", gives="video frame thumbnails and media info",
             found=shutil.which("ffmpeg"), cmd="sudo dnf install -y ffmpeg-free"),
        dict(key="pdftoppm", name="poppler-utils", gives="PDF first-page previews",
             found=shutil.which("pdftoppm"), cmd="sudo dnf install -y poppler-utils"),
        dict(key="gtksourceview", name="GtkSourceView", gives="syntax colouring + line numbers in the quick editor",
             found="loaded" if GtkSource is not None else None, cmd="sudo dnf install -y gtksourceview4"),
        dict(key="pyopengl", name="PyOpenGL (Python)", gives="hardware-accelerated 3D preview",
             found="OpenGL importable" if HAVE_GL else None, cmd="python3 -m pip install --user PyOpenGL"),
        dict(key="h5py", name="h5py (Python)", gives="HDF5 (legacy) Alembic previews",
             found="h5py importable" if _has_py_module("h5py") else None, cmd="python3 -m pip install --user h5py"),
        dict(key="usd", name="usd-core (Python)", gives="USD previews",
             found="pxr importable" if _has_py_module("pxr") else None, cmd="python3 -m pip install --user usd-core"),
        dict(key="gst", name="GStreamer (Python bindings)", gives="video / audio playback engine" + (f" — import failed: {GST_ERROR}" if GST_ERROR else ""),
             found=(Gst.version_string() if HAVE_GST else None),
             cmd="sudo dnf install -y gstreamer1 gstreamer1-plugins-base python3-gobject"),
        dict(key="gstgtk", name="GStreamer GTK sink", gives="video player in the preview pane",
             found="gtksink" if gst_sink_available(refresh=True) else None, cmd="sudo dnf install -y gstreamer1-plugins-good gstreamer1-plugins-good-gtk"),
        dict(key="gstcodecs", name="GStreamer codecs (RPM Fusion)",
             gives="H.264 / H.265 / ProRes / AAC playback" + (f" — {gst_h264_status()[1]}" if HAVE_GST and not gst_h264_status()[0] else ""),
             found=gst_h264_status()[0],
             cmd="sudo dnf install -y https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-9.noarch.rpm && sudo dnf install -y gstreamer1-plugins-ugly gstreamer1-libav"),
        dict(key="rv", name="OpenRV", gives="Open in RV (right-click, Ctrl+Shift+R)",
             found=find_rv(load_config().get("rv_path", "")), cmd="echo 'Download OpenRV from https://github.com/AcademySoftwareFoundation/OpenRV/releases and unpack it under /opt or ~/, then set the path in Preferences → Optional'"),
        dict(key="7z", name="7-Zip (p7zip)", gives=".7z, .rar, .cab, .iso archives: preview, extract and create",
             found=sevenzip(), cmd="sudo dnf install -y p7zip p7zip-plugins"),
        dict(key="xdotool", name="xdotool", gives="fm-bench.py benchmark script only",
             found=shutil.which("xdotool"), cmd="sudo dnf install -y xdotool"),
    ]
    return deps


def install_script(deps: List[dict]) -> str:
    """Shell script that installs the given deps one after another, continuing past failures and summarising."""
    lines = ["#!/bin/bash", "# Kestrel optional dependencies — review before running", "set -u",
             "if ! rpm -q epel-release >/dev/null 2>&1; then echo '== enabling EPEL (needed for most of these)'; sudo dnf install -y epel-release; fi",
             "fail=()"]
    for d in deps:
        lines += [f"echo; echo '== {d['name']}: {d['gives']}'", f"if ! ( {d['cmd']} ); then fail+=('{d['name']}'); fi"]
    lines += ["echo", "if [ ${#fail[@]} -eq 0 ]; then echo 'All done. Restart Kestrel to pick up the new tools.';",
              "else echo \"Failed: ${fail[*]}\"; fi"]
    return "\n".join(lines) + "\n"


def describe_alembic(path: str) -> Optional[str]:
    """Hierarchy listing via abctree / abcls when PyAlembic isn't around."""
    for tool in ("abctree", "abcls"):
        t = shutil.which(tool)
        if t:
            try:
                r = subprocess.run([t, path], capture_output=True, text=True, timeout=15)
                if r.stdout.strip():
                    return r.stdout[:20000]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None
