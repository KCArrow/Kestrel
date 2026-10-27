

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
    hou = _find_houdini()
    L.append(f"  hython     {hou['hython'] or '—'}" + (f"   (Houdini {hou['version']})" if hou["version"] else ""))
    L.append(f"  gconvert   {hou['gconvert'] or '—'}")
    L.append(f"  mayapy     {_find_mayapy() or '—'}")
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
    elif ext == "vdb":
        # OpenVDB volume: Blender imports it as a Volume object; a Volume-to-Mesh modifier turns the main grid
        # into a surface (iso 0 for level sets, a low density threshold for fog) that the mesh export below handles
        res = bpy.ops.object.volume_import(filepath=path)
        for vo in [o for o in bpy.data.objects if o.type == "VOLUME"]:
            vo.data.grids.load()
            names = [g.name for g in vo.data.grids]
            grid = next((n for n in ("density", "surface", "sdf") if n in names), names[0] if names else "density")
            me = bpy.data.meshes.new("kestrel_v2m")
            mo = bpy.data.objects.new("kestrel_v2m", me)
            bpy.context.scene.collection.objects.link(mo)
            md = mo.modifiers.new("v2m", "VOLUME_TO_MESH")
            md.object = vo
            md.grid_name = grid
            md.threshold = 0.0 if grid in ("surface", "sdf") else 0.1
            print("KESTREL vdb grid:", grid, names)
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
            out.append("v %r %r %r" % (co.x, co.z, -co.y))      # Blender Z-up -> Y-up (Maya/Houdini/OBJ convention)
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
        dict(key="houdini", name="Houdini (hython / gconvert)", gives=".bgeo / .bgeo.sc / .geo and .vdb previews: meshes, particles, volumes; also reads Alembic",
             found=(_find_houdini()["hython"] or _find_houdini()["gconvert"]),
             cmd="echo 'Install Houdini from https://www.sidefx.com/download/ (it installs under /opt/hfs*; Kestrel finds it there or via $HFS)'"),
        dict(key="maya", name="Maya (mayapy)", gives="FBX, Alembic and USD previews when Blender isn't available",
             found=_find_mayapy(),
             cmd="echo 'Maya is installed by your studio / Autodesk installer under /usr/autodesk/maya*; Kestrel finds mayapy there or via $MAYA_LOCATION'"),
        dict(key="openvdb", name="OpenVDB (Python)", gives=".vdb volume previews without Houdini",
             found="importable" if (_has_py_module("pyopenvdb") or _has_py_module("openvdb")) else None,
             cmd="sudo dnf install -y python3-openvdb || echo 'python3-openvdb is not packaged for this release; Houdini or Blender also preview VDBs'"),
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


# --------------------------------------------------------------------------- Houdini caches (.bgeo / .geo / .vdb) and the Maya bridge
# Houdini geometry and VDB volumes are read through Houdini itself when it is installed (hython, then gconvert):
# that is the only reader that handles every variant (blosc-compressed .bgeo.sc, packed primitives, every VDB
# compression). Without Houdini: VDBs go through the optional `pyopenvdb` module or Blender (volume → mesh).
# Maya (mayapy) is a fallback for the formats Blender normally imports, for studios that don't deploy Blender.

HOUDINI_GEO_EXTS = (".bgeo.sc", ".geo.sc", ".bgeo.gz", ".geo.gz", ".bgeo.lzma", ".bgeo.bz2", ".bgeo", ".geo",
                    ".bhclassic", ".hclassic", ".bpoly", ".poly")


def houdini_kind(name: str) -> str:
    """'geo' for Houdini geometry files, 'vdb' for OpenVDB, '' otherwise (handles compound extensions)."""
    low = name.lower()
    if low.endswith(".vdb"):
        return "vdb"
    return "geo" if low.endswith(HOUDINI_GEO_EXTS) else ""


_HOUDINI_CACHE: Optional[dict] = None


def _find_houdini() -> dict:
    """{'hython': path|None, 'gconvert': path|None, 'version': str} — PATH, $HFS, then the newest /opt/hfs*."""
    global _HOUDINI_CACHE
    if _HOUDINI_CACHE is not None:
        return _HOUDINI_CACHE
    dirs = []
    if os.environ.get("HFS"):
        dirs.append(os.path.join(os.environ["HFS"], "bin"))

    def ver_key(p):
        m = re.search(r"hfs(\d+)\.(\d+)(?:\.(\d+))?", p)
        return tuple(int(x or 0) for x in m.groups()) if m else (0,)
    dirs += [os.path.join(d, "bin") for d in sorted(glob.glob("/opt/hfs*") + glob.glob(os.path.expanduser("~/hfs*")) +
                                                     glob.glob("/opt/sidefx/hfs*"), key=ver_key, reverse=True)]
    found = {"hython": shutil.which("hython"), "gconvert": shutil.which("gconvert"), "version": ""}
    for d in dirs:
        for tool in ("hython", "gconvert"):
            p = os.path.join(d, tool)
            if not found[tool] and os.access(p, os.X_OK):
                found[tool] = p
    src = found["hython"] or found["gconvert"] or ""
    m = re.search(r"hfs(\d+\.\d+(?:\.\d+)?)", src)
    found["version"] = m.group(1) if m else ""
    _HOUDINI_CACHE = found
    return found


_HOU_SCRIPT = r'''
import sys, json, math, traceback
try:
    import numpy as np
    import hou
    path, out, objout, maxpts, maxvox = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
    info = [["Houdini", hou.applicationVersionString()]]
    node = hou.node("/obj").createNode("geo", "kestrel_preview")
    for c in node.children():
        c.destroy()
    fsop = node.createNode("file")
    fsop.parm("file").set(path)
    unp = node.createNode("unpack")
    unp.setFirstInput(fsop)
    g = unp.geometry()
    if fsop.errors():
        raise RuntimeError("; ".join(fsop.errors()))
    npts = int(g.intrinsicValue("pointcount"))
    nprims = int(g.intrinsicValue("primitivecount"))
    npoly = g.countPrimType(hou.primType.Polygon)
    nvdb = g.countPrimType(hou.primType.VDB)
    nvol = g.countPrimType(hou.primType.Volume)
    info += [["Points", "{:,}".format(npts)], ["Primitives", "{:,}".format(nprims)]]
    attrs = [a.name() for a in g.pointAttribs()][:14]
    if attrs:
        info.append(["Point attributes", ", ".join(attrs)])
    arrays = {}
    if npoly:
        if npoly == nprims:
            g.saveToFile(objout)
        else:
            gp = hou.Geometry()
            gp.merge(g)
            gp.deletePrims([p for p in gp.prims() if p.type() != hou.primType.Polygon])
            gp.saveToFile(objout)
        arrays["has_obj"] = np.array([1])
    elif npts and not (nvdb or nvol):
        P = np.frombuffer(g.pointFloatAttribValuesAsString("P"), dtype=np.float32).reshape(-1, 3)
        step = max(1, int(math.ceil(len(P) / float(maxpts))))
        arrays["P"] = P[::step]
        if g.findPointAttrib("Cd") is not None:
            cd = np.frombuffer(g.pointFloatAttribValuesAsString("Cd"), dtype=np.float32)
            if cd.size == len(P) * 3:
                arrays["Cd"] = cd.reshape(-1, 3)[::step]
        if step > 1:
            info.append(["Shown", "every {}th point".format(step)])
    vols = []
    vP, vD, vS, sP, sS = [], [], [], [], []
    if nvdb or nvol:
        per = max(20000, maxvox // max(1, nvdb + nvol))
        namea = g.findPrimAttrib("name")
        for i, prim in enumerate(g.iterPrims()):
            if i > 200000:
                break
            t = prim.type()
            if t not in (hou.primType.VDB, hou.primType.Volume):
                continue
            name = prim.attribValue("name") if namea is not None else "volume"
            o = np.array(prim.indexToPos((0, 0, 0)))
            ex = np.array(prim.indexToPos((1, 0, 0))) - o
            ey = np.array(prim.indexToPos((0, 1, 0))) - o
            ez = np.array(prim.indexToPos((0, 0, 1))) - o
            vs = float(np.linalg.norm(ex))
            idx, val, sdf, desc = [], [], False, ""
            if t == hou.primType.VDB:
                sdf = bool(prim.isSDF())
                bb = prim.activeVoxelBoundingBox()
                mn, mx = bb.minvec(), bb.maxvec()
                x0, y0, z0 = int(mn[0]), int(mn[1]), int(mn[2])
                x1, y1, z1 = int(mx[0]), int(mx[1]), int(mx[2])
                nx, ny, nz = x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1
                act = int(prim.activeVoxelCount())
                desc = "{} VDB{}, {:,} active voxels, voxel {:.3g}".format(name, " (SDF)" if sdf else "", act, vs)
                if nx > 0 and ny > 0 and nz > 0:
                    step = max(1, int(math.ceil((nx * ny * nz / float(per)) ** (1 / 3.0))))
                    order = None
                    for z in range(z0, z1 + 1, step):
                        arr = np.array(prim.voxelRangeAsTuple(hou.BoundingBox(x0, y0, z, x1, y1, z)), dtype=np.float32)
                        if arr.size != nx * ny:
                            continue
                        if order is None and nx > 1 and ny > 1:
                            a1, ay1 = prim.voxel((x0 + 1, y0, z)), prim.voxel((x0, y0 + 1, z))
                            if a1 != ay1:
                                order = "x" if abs(arr[1] - a1) < 1e-9 else ("y" if abs(arr[1] - ay1) < 1e-9 else None)
                        a = arr.reshape(ny, nx) if order != "y" else arr.reshape(nx, ny).T
                        a = a[::step, ::step]
                        m = (np.abs(a) < vs * step * 0.9) if sdf else (a > 1e-4)
                        ys, xs = np.nonzero(m)
                        if len(xs):
                            idx.append(np.stack([x0 + xs * step, y0 + ys * step, np.full(len(xs), z)], 1))
                            val.append(np.ones(len(xs), np.float32) if sdf else a[m])
                    vs *= step
            else:
                rx, ry, rz = prim.resolution()
                desc = "{} volume {}×{}×{}".format(name, rx, ry, rz)
                if rx * ry * rz <= 96 * 1024 * 1024:
                    a = np.array(prim.allVoxels(), dtype=np.float32).reshape(rz, ry, rx)
                    step = max(1, int(math.ceil((rx * ry * rz / float(per)) ** (1 / 3.0))))
                    a = a[::step, ::step, ::step]
                    m = a > 1e-4
                    zs, ys, xs = np.nonzero(m)
                    if len(xs):
                        idx.append(np.stack([xs * step, ys * step, zs * step], 1))
                        val.append(a[m])
                    vs *= step
            vols.append(desc)
            if idx:
                I = np.concatenate(idx).astype(np.float64)
                V = np.concatenate(val).astype(np.float32)
                if not sdf and len(V):
                    keep = V > V.max() * 0.01
                    I, V = I[keep], V[keep]
                W = o + I[:, :1] * ex + I[:, 1:2] * ey + I[:, 2:3] * ez
                if sdf:
                    sP.append(W.astype(np.float32))       # level set: its narrow band is drawn as a surface
                    sS.append(vs)
                else:
                    vP.append(W.astype(np.float32))
                    vD.append(V)
                    vS.append(vs)
    if sP:
        S = np.concatenate(sP)
        if len(S) > maxpts:
            S = S[np.random.default_rng(0).choice(len(S), maxpts, replace=False)]
        arrays["P"] = np.concatenate([arrays["P"], S]) if "P" in arrays else S
        arrays["pS"] = np.array([max(sS)], np.float32)
        arrays.pop("Cd", None)
    if vols:
        info.append(["Volumes", " | ".join(vols[:6]) + (" …" if len(vols) > 6 else "")])
    if vP:
        P = np.concatenate(vP)
        D = np.concatenate(vD)
        if len(P) > maxvox:
            sel = np.random.default_rng(0).choice(len(P), maxvox, replace=False)
            P, D = P[sel], D[sel]
        arrays["vP"], arrays["vD"], arrays["vS"] = P, D, np.array([max(vS)], np.float32)
    arrays["info"] = np.array([json.dumps(info)])
    np.savez(out, **arrays)
    print("KESTREL-OK")
except Exception:
    traceback.print_exc(file=sys.stdout)
    print("KESTREL-FAIL")
'''


def load_via_houdini(path: str, max_points: int = 2_000_000, max_voxels: int = 1_500_000) -> Optional[dict]:
    """hython: Houdini reads the file (any .bgeo/.geo variant, packed prims unpacked, VDB and native volumes) and
    hands back polygons (as OBJ), particles and sampled volume voxels (as .npz). Needs numpy on our side."""
    h = _find_houdini()
    if not h["hython"]:
        _vfx_err("Houdini: hython not found (PATH, $HFS or /opt/hfs*)")
        return None
    if np is None:
        _vfx_err("Houdini caches need numpy (sudo dnf install python3-numpy)")
        return None
    import tempfile
    tmp = tempfile.mkdtemp(prefix="kestrel-hou-")
    script = os.path.join(tmp, "read.py")
    out, objout = os.path.join(tmp, "out.npz"), os.path.join(tmp, "mesh.obj")
    try:
        with open(script, "w") as f:
            f.write(_HOU_SCRIPT)
        rc, so, se = _run_streaming([h["hython"], script, os.path.abspath(path), out, objout, str(max_points), str(max_voxels)],
                                    timeout=200, verbose=BLENDER_VERBOSE)
        if "KESTREL-OK" not in so or not os.path.exists(out):
            tail = [l for l in so + se if l.strip() and ("rror" in l or "icense" in l or "timed out" in l)][-3:]
            _vfx_err("Houdini (hython): " + (" | ".join(x[:200] for x in tail) or f"exit {rc}"))
            return None
        z = np.load(out, allow_pickle=False)
        res = {k: z[k] for k in z.files}
        res["info"] = [tuple(r) for r in json.loads(str(res["info"][0]))]
        if "has_obj" in res and os.path.exists(objout):
            with open(objout, "rb") as f:
                res["V"], res["F"] = _read_obj_numpy(f.read())
        res["source"] = "Houdini " + (h["version"] or "")
        return res
    except Exception as ex:
        _vfx_err(f"Houdini: {ex}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_via_gconvert(path: str) -> Optional[dict]:
    """Houdini's gconvert (no hython / no licence available): geometry → OBJ. Polygons and points only."""
    h = _find_houdini()
    if not h["gconvert"] or np is None:
        return None
    import tempfile
    tmp = tempfile.mkdtemp(prefix="kestrel-gconv-")
    out = os.path.join(tmp, "mesh.obj")
    try:
        rc, so, se = _run_streaming([h["gconvert"], os.path.abspath(path), out], timeout=120)
        if not os.path.exists(out):
            _vfx_err("gconvert: " + (" | ".join((so + se)[-2:]) or f"exit {rc}"))
            return None
        with open(out, "rb") as f:
            V, F = _read_obj_numpy(f.read())
        res = {"info": [("Points", f"{len(V):,}")], "source": "Houdini gconvert"}
        if len(F):
            res["V"], res["F"] = V, F
        else:
            res["P"] = V
        return res
    except Exception as ex:
        _vfx_err(f"gconvert: {ex}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_vdb_openvdb(path: str, max_voxels: int = 1_500_000) -> Optional[dict]:
    """Optional `pyopenvdb` / `openvdb` Python module: sample the first fog/SDF float grid."""
    vdbmod = None
    for name in ("pyopenvdb", "openvdb"):
        try:
            vdbmod = __import__(name)
            break
        except ImportError:
            continue
    if vdbmod is None or np is None:
        return None
    try:
        grids, meta = vdbmod.readAll(path)
        g = next((x for x in grids if "Float" in type(x).__name__ or "float" in getattr(x, "valueTypeName", "")), None)
        if g is None:
            _vfx_err("OpenVDB: no float grid")
            return None
        (i0, j0, k0), (i1, j1, k1) = g.evalActiveVoxelBoundingBox()
        dims = (i1 - i0 + 1, j1 - j0 + 1, k1 - k0 + 1)
        if dims[0] * dims[1] * dims[2] > 256 * 1024 * 1024:
            _vfx_err("OpenVDB: grid too large for a dense preview")
            return None
        a = np.zeros(dims, dtype=np.float32)
        g.copyToArray(a, ijk=(i0, j0, k0))
        sdf = "LEVEL_SET" in str(getattr(g, "gridClass", "")).upper()
        step = max(1, int(math.ceil((a.size / float(max_voxels)) ** (1 / 3.0))))
        b = a[::step, ::step, ::step]
        try:
            vs = float(g.transform.voxelSize()[0])
            org = np.array(g.transform.indexToWorld((0, 0, 0)), dtype=np.float64)
        except Exception:
            vs, org = 1.0, np.zeros(3)
        m = (np.abs(b) < vs * step * 0.9) if sdf else (b > max(1e-4, float(b.max()) * 0.01))
        ii, jj, kk = np.nonzero(m)
        I = np.stack([i0 + ii * step, j0 + jj * step, k0 + kk * step], 1).astype(np.float64)
        W = org + I * vs                      # axis-aligned transform assumed (the common case)
        info = [("Volumes", f"{g.name or 'grid'} ({'SDF' if sdf else 'fog'}), {g.activeVoxelCount():,} active voxels")]
        if sdf:
            return {"P": W.astype(np.float32), "pS": np.array([vs * step], np.float32), "info": info, "source": "OpenVDB (Python)"}
        D = b[m][:len(W)]
        return {"vP": W.astype(np.float32), "vD": D.astype(np.float32), "vS": np.array([vs * step], np.float32),
                "info": [("Volumes", f"{g.name or 'grid'} ({'SDF' if sdf else 'fog'}), {g.activeVoxelCount():,} active voxels")],
                "source": "OpenVDB (Python)"}
    except Exception as ex:
        _vfx_err(f"OpenVDB: {ex}")
        return None


# ---- built-in OpenVDB reader: float grids (fog volumes and level sets) without Houdini or the OpenVDB library
# Implements the parts of the .vdb format Houdini, EmberGen, Blender and Maya write for scalar grids: file
# version 222+, Tree_float_5_4_3 (optionally _HalfFloat), zip / blosc / no compression, active-value
# masks, linear transforms. Blosc is decoded by python-blosc or the C library when either is found
# (Houdini ships one), otherwise by a small pure-Python LZ4 / BloscLZ decoder.

VDB_MAX_VOXELS = 1_500_000


def _lz4_block_decode(src: bytes, out_len: int) -> bytes:
    """LZ4 block format (no frame)."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:                                  # overlapping copy: repeat the period
            chunk = dst[start:]
            reps, rem = divmod(ml, off)
            dst += chunk * reps + chunk[:rem]
    if len(dst) != out_len:
        raise ValueError(f"lz4: {len(dst)} bytes, expected {out_len}")
    return bytes(dst)


def _blosclz_decode(src: bytes, out_len: int) -> bytes:
    """BloscLZ (FastLZ level 1 variant used by c-blosc 1.x)."""
    dst = bytearray()
    i, n = 0, len(src)
    ctrl = src[i] & 31
    i += 1
    while True:
        if ctrl >= 32:
            ln = (ctrl >> 5) - 1
            ofs = (ctrl & 31) << 8
            if ln == 7 - 1:
                while True:
                    c = src[i]
                    i += 1
                    ln += c
                    if c != 255:
                        break
            c = src[i]
            i += 1
            ln += 3
            ofs += c
            if c == 255 and ofs == (31 << 8) + 255:          # far distance
                ofs = (src[i] << 8) + src[i + 1] + 8191
                i += 2
            start = len(dst) - ofs - 1
            if ofs + 1 >= ln:
                dst += dst[start:start + ln]
            else:
                chunk = dst[start:]
                reps, rem = divmod(ln, ofs + 1)
                dst += chunk * reps + chunk[:rem]
        else:
            ctrl += 1
            dst += src[i:i + ctrl]
            i += ctrl
        if i >= n:
            break
        ctrl = src[i]
        i += 1
    if len(dst) != out_len:
        raise ValueError(f"blosclz: {len(dst)} bytes, expected {out_len}")
    return bytes(dst)


_BLOSC_C = None
_BLOSC_MODS = None
_LZ4_C = None


def _lz4_c():
    """ctypes LZ4_decompress_safe from the system liblz4 (installed on practically every Linux: systemd needs
    it), or False. ~50× faster than the pure-Python decoder."""
    global _LZ4_C
    if _LZ4_C is None:
        _LZ4_C = False
        import ctypes
        import ctypes.util
        for name in (ctypes.util.find_library("lz4"), "liblz4.so.1", "liblz4.so"):
            if not name:
                continue
            try:
                f = ctypes.CDLL(name).LZ4_decompress_safe
                f.restype = ctypes.c_int
                f.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
                _LZ4_C = (f, ctypes.create_string_buffer)
                break
            except (OSError, AttributeError):
                continue
    return _LZ4_C


def _blosc_mods():
    """(python-blosc module or None, lz4.block module or None), looked up once."""
    global _BLOSC_MODS
    if _BLOSC_MODS is None:
        mods = []
        for name in ("blosc", "lz4.block"):
            try:
                mods.append(__import__(name, fromlist=["x"]))
            except Exception:
                mods.append(None)
        _BLOSC_MODS = tuple(mods)
    return _BLOSC_MODS


def _blosc_c():
    """ctypes handle to a c-blosc library (system, Houdini's dsolib, Blender), or False."""
    global _BLOSC_C
    if _BLOSC_C is not None:
        return _BLOSC_C
    _BLOSC_C = False
    import ctypes
    import ctypes.util
    cands = [ctypes.util.find_library("blosc")]
    for pat in ("/opt/hfs*/dsolib/libblosc.so*", os.path.expandvars("$HFS/dsolib/libblosc.so*"),
                "/usr/lib64/libblosc.so*", "/usr/local/lib*/libblosc.so*"):
        cands += sorted(glob.glob(pat), reverse=True)
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
            f = lib.blosc_decompress_ctx
            f.restype = ctypes.c_int
            f.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
            _BLOSC_C = (f, ctypes)
            break
        except (OSError, AttributeError):
            continue
    return _BLOSC_C


def blosc_decompress(buf: bytes) -> bytes:
    """Decode one Blosc-1 chunk (the format OpenVDB writes)."""
    nbytes, blocksize, cbytes = struct.unpack_from("<III", buf, 4)
    pyblosc, lz4b = _blosc_mods()
    if pyblosc is not None:
        return pyblosc.decompress(buf)
    c = _blosc_c()
    if c:
        f, ctypes = c
        out = ctypes.create_string_buffer(nbytes)
        if f(buf, out, nbytes, 1) == nbytes:
            return out.raw
    flags, typesize = buf[2], buf[3]
    if flags & 0x02:                                               # stored (memcpy) chunk
        return bytes(buf[16:16 + nbytes])
    lz4c = _lz4_c()
    codec = flags >> 5
    if flags & 0x04:
        raise ValueError("blosc: bit-shuffle is not supported")
    nblocks = (nbytes + blocksize - 1) // blocksize
    bstarts = struct.unpack_from(f"<{nblocks}i", buf, 16)
    out = bytearray()
    for b in range(nblocks):
        bsize = min(blocksize, nbytes - b * blocksize)
        split = (not flags & 0x10) and typesize <= 16 and (bsize // typesize) >= 1 and (bsize % typesize == 0)
        nsplits = typesize if split else 1
        neblock = bsize // nsplits
        pos = bstarts[b]
        block = bytearray()
        for _ in range(nsplits):
            cb = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
            if cb == 0:
                block += bytes(neblock)
            elif cb < 0:
                block += bytes([(-cb) & 0xFF]) * neblock
            elif cb == neblock:
                block += buf[pos:pos + cb]
            else:
                data = bytes(buf[pos:pos + cb])
                if codec == 1:
                    if lz4b is not None:
                        block += lz4b.decompress(data, uncompressed_size=neblock)
                    elif lz4c:
                        out_ = lz4c[1](neblock)
                        if lz4c[0](data, out_, cb, neblock) != neblock:
                            raise ValueError("lz4: corrupt block")
                        block += out_.raw
                    else:
                        block += _lz4_block_decode(data, neblock)
                elif codec == 0:
                    block += _blosclz_decode(data, neblock)
                elif codec == 3:
                    block += zlib.decompress(data)
                elif codec == 4:
                    import zstandard
                    block += zstandard.ZstdDecompressor().decompress(data, max_output_size=neblock)
                else:
                    raise ValueError(f"blosc: codec {codec} not supported")
            pos += max(cb, 0)
        if flags & 0x01 and typesize > 1:                          # undo byte shuffle
            full = (bsize // typesize) * typesize
            arr = np.frombuffer(bytes(block[:full]), dtype=np.uint8).reshape(typesize, -1).T.reshape(-1)
            block = bytearray(arr.tobytes()) + block[full:]
        out += block
    return bytes(out)


class _VdbStream:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def read(self, n: int) -> bytes:
        b = self.d[self.p:self.p + n]
        if len(b) != n:
            raise ValueError("unexpected end of VDB file")
        self.p += n
        return b

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def i32(self):
        return struct.unpack("<i", self.read(4))[0]

    def i64(self):
        return struct.unpack("<q", self.read(8))[0]

    def string(self) -> str:
        return self.read(self.u32()).decode("utf-8", "replace")

    def meta(self) -> dict:
        out = {}
        for _ in range(self.u32()):
            name, typ = self.string(), self.string()
            raw = self.read(self.u32())
            try:
                if typ == "string":
                    out[name] = raw.decode("utf-8", "replace")
                elif typ == "bool":
                    out[name] = bool(raw[0])
                elif typ in ("int32", "int64", "float", "double") and len(raw) in (4, 8):
                    out[name] = struct.unpack({"int32": "<i", "int64": "<q", "float": "<f", "double": "<d"}[typ], raw)[0]
                elif typ == "vec3i":
                    out[name] = struct.unpack("<3i", raw)
                else:
                    out[name] = raw
            except Exception:
                out[name] = raw
        return out


def _vdb_transform(s: _VdbStream):
    """→ 4×4 row-vector matrix (world = [i j k 1] @ M)."""
    typ = s.string()
    d = lambda n: struct.unpack(f"<{n}d", s.read(8 * n))
    M = np.eye(4)
    if typ in ("ScaleMap", "UniformScaleMap"):
        sc = d(3)
        d(12)                                                # voxelSize, inverse, invScaleSqr, invTwiceScale
        M[0, 0], M[1, 1], M[2, 2] = sc
    elif typ == "TranslationMap":
        M[3, :3] = d(3)
    elif typ in ("ScaleTranslateMap", "UniformScaleTranslateMap"):
        tr, sc = d(3), d(3)
        d(12)
        M[0, 0], M[1, 1], M[2, 2] = sc
        M[3, :3] = tr
    elif typ in ("AffineMap", "UnitaryMap"):
        M = np.array(d(16)).reshape(4, 4)
    else:
        raise ValueError(f"transform {typ} not supported")
    return M


class _VdbFloatTree:
    """Reads topology + buffers of a Tree_float_5_4_3 grid into flat arrays of active voxels."""

    def __init__(self, s: _VdbStream, compression: int, half: bool):
        self.s, self.comp, self.half = s, compression, half
        self.vt = np.float16 if half else np.float32

    def _data(self, count: int):
        """readData: count values of the stored type, honouring the grid's compression."""
        s, isz = self.s, (2 if self.half else 4)
        if self.half and count < 1:                          # OpenVDB's HalfReader writes nothing for 0 values
            return np.zeros(0, dtype=self.vt)
        nbytes = count * isz
        if self.comp & 0x5:                                  # zip or blosc: size-prefixed
            n = s.i64()
            if n <= 0:
                raw = s.read(-n)
            else:
                blob = s.read(n)
                raw = blosc_decompress(blob) if self.comp & 0x4 else zlib.decompress(blob)
        else:
            raw = s.read(nbytes)
        return np.frombuffer(raw, dtype=self.vt, count=count)

    def _compressed_values(self, count: int, mask_on: int):
        """readCompressedValues → (values array, all_values_stored)."""
        s = self.s
        meta = s.read(1)[0]
        if meta in (2, 4, 5):
            s.read(4 if meta != 5 else 8)                    # inactive value(s), full precision
        if meta in (3, 4, 5):
            s.read(count // 8)                               # selection mask
        if self.comp & 0x2 and meta != 6:
            return self._data(mask_on), False
        return self._data(count), True

    @staticmethod
    def _mask(raw: bytes):
        return np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little").astype(bool)

    def read(self):
        s = self.s
        if s.u32() != 1:
            raise ValueError("multi-buffer trees are not supported")
        self.background = struct.unpack("<f", s.read(4))[0]
        ntiles, nchild = s.u32(), s.u32()
        s.read(ntiles * (12 + 4 + 1))
        # topology: depth-first, upper(32³) → lower(16³) → leaf masks
        self.leaf_origins, self.leaf_tmask = [], []
        tile_pts, tile_vals = [], []
        lower_offsets = np.array([(n >> 8, (n >> 4) & 15, n & 15) for n in range(4096)], dtype=np.int32)
        upper_offsets = None
        # each root child (upper node) is read in full, recursively, before the next one
        for _ in range(nchild):
            org = np.array(struct.unpack("<3i", s.read(12)), dtype=np.int64)
            cm, vm = self._mask(s.read(4096)), self._mask(s.read(4096))
            self._compressed_values(32768, int(vm.sum()))    # upper tiles (128³ each) — ignored
            if upper_offsets is None:
                n = np.arange(32768)
                upper_offsets = np.stack([n >> 10, (n >> 5) & 31, n & 31], 1)
            for ci in np.nonzero(cm)[0]:
                lorg = org + upper_offsets[ci] * 128
                lcm, lvm = self._mask(s.read(512)), self._mask(s.read(512))
                vals, allv = self._compressed_values(4096, int(lvm.sum()))
                if lvm.any():                                 # active 8³ tiles
                    tv = vals[lvm] if allv else vals
                    tile_pts.append(lorg + lower_offsets[lvm] * 8)
                    tile_vals.append(np.asarray(tv, dtype=np.float32))
                for li in np.nonzero(lcm)[0]:
                    self.leaf_origins.append(lorg + lower_offsets[li] * 8)
                    s.read(64)                                # leaf topology mask (repeated with the buffers)
        self.tile_pts = np.concatenate(tile_pts) if tile_pts else np.zeros((0, 3), np.int64)
        self.tile_vals = np.concatenate(tile_vals) if tile_vals else np.zeros(0, np.float32)
        return self

    def read_leaves(self, stride: int = 1):
        """Leaf buffers in topology order → (origins int32 L×3, masks uint8 L×64, active values float32 in
        (leaf, bit) order). Every `stride`-th leaf is decoded; the rest are skipped without decompressing.
        Hot loop: ~25k–500k leaves, so plain locals and offsets instead of stream objects."""
        data, p = self.s.d, self.s.p
        half, comp = self.half, self.comp
        isz = 2 if half else 4
        dt = np.float16 if half else np.float32
        sized = bool(comp & 0x5)
        maskc = bool(comp & 0x2)
        q64 = struct.Struct("<q").unpack_from
        zero64 = bytes(64)
        dec = blosc_decompress if comp & 0x4 else zlib.decompress
        masks, chunks, used = [], [], []
        for idx in range(len(self.leaf_origins)):
            m = data[p:p + 64]
            meta = data[p + 64]
            p += 65
            if meta in (2, 4, 5):
                p += 8 if meta == 5 else 4                    # inactive value(s): full precision even for half grids
            if meta in (3, 4, 5):
                p += 64                                       # selection mask
            allv = (not maskc) or meta == 6
            if half and not allv and m == zero64:
                continue                                      # no active values: OpenVDB wrote nothing at all
            if sized:
                n = q64(data, p)[0]
                p += 8
                if idx % stride:
                    p += abs(n)
                    continue
                if n <= 0:
                    raw = data[p:p - n]
                    p -= n
                else:
                    raw = dec(data[p:p + n])
                    p += n
            else:
                cnt = 512 if allv else bin(int.from_bytes(m, "little")).count("1")
                raw = data[p:p + cnt * isz]
                p += cnt * isz
                if idx % stride:
                    continue
            if allv:                                          # all 512 stored: keep the active ones
                raw = np.frombuffer(raw, dt)[self._mask(m)].tobytes()
            masks.append(m)
            chunks.append(raw)
            used.append(idx)
        self.s.p = p
        org = np.asarray(self.leaf_origins, dtype=np.int32).reshape(-1, 3)[np.asarray(used, dtype=np.int64)]
        mb = np.frombuffer(b"".join(masks), dtype=np.uint8).reshape(-1, 64)
        vals = np.frombuffer(b"".join(chunks), dtype=dt).astype(np.float32)
        return org, mb, vals

    def _skip_values(self, count: int, on: int):
        s = self.s
        meta = s.read(1)[0]
        if meta in (2, 4, 5):
            s.read(4 if meta != 5 else 8)
        if meta in (3, 4, 5):
            s.read(count // 8)
        n = on if (self.comp & 0x2 and meta != 6) else count
        if self.half and n < 1:
            return
        if self.comp & 0x5:
            k = s.i64()
            s.p += abs(k)
        else:
            s.p += n * (2 if self.half else 4)


def read_vdb_grids(path: str):
    """Header + grid descriptors of a .vdb: (data, [dict(name, type, pos, block, end)])."""
    with open(path, "rb") as f:
        data = f.read()                                       # caches are tens of MB; one read beats many seeks
    s = _VdbStream(data)
    if s.i64() != 0x56444220:
        raise ValueError("not an OpenVDB file")
    ver = s.u32()
    if ver < 222:
        raise ValueError(f"VDB file version {ver} is too old for the built-in reader")
    s.u32(), s.u32()
    has_offsets = s.read(1)[0]
    s.read(36)
    s.meta()
    grids = []
    for _ in range(s.i32()):
        name, typ = s.string(), s.string()
        s.string()                                            # instance parent
        pos, block, end = s.i64(), s.i64(), s.i64()
        grids.append({"name": name.split("\x1e")[0], "type": typ, "pos": pos, "block": block, "end": end})
        if not has_offsets:
            raise ValueError("VDB without grid offsets")
        s.p = end
    return data, grids


def _vdb_pick_step(mb, max_voxels: int) -> int:
    """Coarsening for a preview: the smallest of 1, 2, 4, 8 whose occupied cells (any active voxel among the
    step³ it averages) fit in max_voxels, with 30 % slack so a sequence doesn't drop a whole level of detail
    for one frame a little over. Leaf-aligned steps make coarsening a reshape, not a sort."""
    occ = {1: 0, 2: 0, 4: 0, 8: 0}
    for b0 in range(0, len(mb), 32768):
        bits = np.unpackbits(mb[b0:b0 + 32768], axis=1, bitorder="little").astype(bool).reshape(-1, 8, 8, 8)
        n = len(bits)
        occ[1] += int(bits.sum())
        for st in (2, 4, 8):
            d = 8 // st
            occ[st] += int(bits.reshape(n, d, st, d, st, d, st).any(axis=(2, 4, 6)).sum())
    return next((st for st in (1, 2, 4) if occ[st] <= max_voxels * 1.3), 8)


def _vdb_leaf_samples(org, mb, vals, step: int, keep):
    """Leaves → sample centres (index space, float64 N×3) and values, averaging step³ cells inside each leaf.
    keep(cell_values, active_bits) selects cells. Works in blocks of 32k leaves (64 MB) so memory stays flat."""
    counts = np.unpackbits(mb, axis=1, bitorder="little").sum(1, dtype=np.int64) if len(mb) else np.zeros(0, np.int64)
    starts = np.concatenate([np.zeros(1, np.int64), np.cumsum(counts)])
    d = 8 // step
    n_cells = d * d * d
    cidx = np.arange(n_cells)
    coff = np.stack([cidx // (d * d), (cidx // d) % d, cidx % d], 1).astype(np.float64) * step + (step - 1) / 2.0
    out_c, out_v = [], []
    for b0 in range(0, len(mb), 32768):
        b1 = min(len(mb), b0 + 32768)
        bits = np.unpackbits(mb[b0:b1], axis=1, bitorder="little").astype(bool)
        dense = np.zeros(bits.shape, np.float32)
        dense[bits] = vals[starts[b0]:starts[b1]]
        n = b1 - b0
        if step > 1:
            cv = dense.reshape(n, d, step, d, step, d, step).mean(axis=(2, 4, 6)).reshape(n, n_cells)
            cb = bits.reshape(n, d, step, d, step, d, step).any(axis=(2, 4, 6)).reshape(n, n_cells)
        else:
            cv, cb = dense, bits
        li, ci = np.nonzero(keep(cv, cb) & cb)
        out_c.append(org[b0 + li].astype(np.float64) + coff[ci])
        out_v.append(cv[li, ci])
    if not out_c:
        return np.zeros((0, 3)), np.zeros(0, np.float32)
    return np.concatenate(out_c), np.concatenate(out_v).astype(np.float32)


def load_vdb_native(path: str, max_voxels: int = VDB_MAX_VOXELS, step: Optional[int] = None) -> Optional[dict]:
    """Built-in reader: the density grid (or the first float grid) as smoke voxels, a level set as surface points.
    step: coarsening factor to use (a sequence keeps the first frame's, so detail doesn't flicker)."""
    if np is None:
        return None
    t0 = time.monotonic()
    try:
        data, grids = read_vdb_grids(path)
        floats = [g for g in grids if g["type"].startswith("Tree_float_5_4_3")]
        if not floats:
            _vfx_err("VDB: no float grid the built-in reader understands (" + ", ".join(g["type"] for g in grids) + ")")
            return None
        others = ", ".join(g["name"] for g in grids)
        metas = []
        for g in floats:
            s = _VdbStream(data, g["pos"])
            g["comp"] = s.u32()
            g["meta"] = s.meta()
            g["tf_pos"] = s.p
            metas.append(g)
        cls = lambda g: str(g["meta"].get("class", "")).lower()
        pick = (next((g for g in floats if g["name"].lower() == "density"), None)
                or next((g for g in floats if cls(g) != "level set"), None) or floats[0])
        sdf = cls(pick) == "level set"
        s = _VdbStream(data, pick["tf_pos"])
        M = _vdb_transform(s)
        tree = _VdbFloatTree(s, pick["comp"], pick["type"].endswith("_HalfFloat")).read()
        if s.p != pick["block"]:
            raise ValueError(f"topology ended at {s.p}, expected {pick['block']}")
        nleaves = len(tree.leaf_origins)
        est = int(pick["meta"].get("file_voxel_count", nleaves * 300))
        stride = 1
        blosc_slow = bool(pick["comp"] & 0x4) and not _blosc_c() and not _lz4_c() and not any(_blosc_mods())
        if blosc_slow and nleaves > 250_000:                   # pure-Python LZ4 (~10 µs a leaf): cap the decode time
            stride = int(math.ceil(nleaves / 250_000))
        org, mb, vals = tree.read_leaves(stride)
        if s.p != pick["end"]:
            raise ValueError(f"leaf data ended at {s.p}, expected {pick['end']}")
        if len(tree.tile_vals):                              # active 8³ tiles are leaf-sized blocks of one value
            org = np.concatenate([org, tree.tile_pts.astype(np.int32)])
            mb = np.concatenate([mb, np.full((len(tree.tile_vals), 64), 255, np.uint8)])
            vals = np.concatenate([vals, np.repeat(tree.tile_vals.astype(np.float32), 512)])
        vs = float(np.linalg.norm(M[0, :3]))
        # EmberGen and Blender write Z-up volumes; Kestrel's viewer (like Houdini and Maya) is Y-up
        tags = " ".join(f"{k} {v}" for g in metas for k, v in g["meta"].items() if not isinstance(v, bytes)).lower()
        zup = "embergen" in tags or "blender" in tags
        if zup:
            M = M @ np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
        info = [("Volumes", f"{pick['name']} ({'SDF' if sdf else 'fog'}), {est:,} active voxels"
                 + (f"; grids: {others}" if len(grids) > 1 else ""))]
        if sdf:
            centre, _ = _vdb_leaf_samples(org, mb, vals, 1, lambda d, bits: bits & (np.abs(d) < vs * 0.87))
            step = max(1, int(math.ceil((len(centre) / 2_000_000) ** 0.5)))
            centre = centre[::step]
            P = np.c_[centre, np.ones(len(centre))] @ M
            return {"P": P[:, :3].astype(np.float32), "pS": np.array([vs * math.sqrt(step)], np.float32), "info": info,
                    "source": "built-in VDB reader"}
        thr = 0.004 * (float(vals.max()) if len(vals) else 0.0)   # faint haze: invisible, but widens the framing
        step_fixed = int(step or 0)
        if step_fixed:
            step = 1 if step_fixed < 2 else 2 if step_fixed < 4 else 4 if step_fixed < 8 else 8
        else:
            step = _vdb_pick_step(mb, max_voxels)
        # a cell is kept when its average holds at least one voxel's worth of visible density
        centre, val = _vdb_leaf_samples(org, mb, vals, step, lambda d, bits: d > thr / step ** 3)
        P = np.c_[centre, np.ones(len(centre))] @ M
        if stride > 1:
            info.append(("Note", f"every {stride}th block read (install lz4-libs or python3-blosc for full detail)"))
        bench(f"vdb native: {pick['name']} {len(val):,} samples, {nleaves:,} leaves, stride {stride}, "
              f"{time.monotonic() - t0:.2f}s")
        return {"vP": P[:, :3].astype(np.float32), "vD": val.astype(np.float32), "vS": np.array([vs * step], np.float32),
                "vStep": step, "info": info, "source": "built-in VDB reader"}
    except Exception as ex:
        _vfx_err(f"VDB reader: {ex}")
        return None


# ---- Maya (mayapy) — fallback for FBX / Alembic / USD / OBJ when Blender isn't available
_MAYA_CACHE: Optional[str] = None


def _find_mayapy() -> Optional[str]:
    global _MAYA_CACHE
    if _MAYA_CACHE is not None:
        return _MAYA_CACHE or None
    c = [shutil.which("mayapy")]
    if os.environ.get("MAYA_LOCATION"):
        c.append(os.path.join(os.environ["MAYA_LOCATION"], "bin", "mayapy"))
    c += sorted(glob.glob("/usr/autodesk/maya*/bin/mayapy") + glob.glob("/opt/autodesk/maya*/bin/mayapy"), reverse=True)
    _MAYA_CACHE = next((p for p in c if p and os.access(p, os.X_OK)), "")
    return _MAYA_CACHE or None


_MAYA_SCRIPT = r'''
import sys, struct, array, traceback
try:
    path, out = sys.argv[1], sys.argv[2]
    import maya.standalone
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds
    import maya.api.OpenMaya as om
    for plug in ("fbxmaya", "AbcImport", "mayaUsdPlugin", "objExport"):
        try:
            cmds.loadPlugin(plug, quiet=True)
        except Exception:
            pass
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "abc":
        cmds.AbcImport(path, mode="import")
    elif ext == "fbx":
        cmds.file(path, i=True, type="FBX", ignoreVersion=True)
    elif ext in ("usd", "usda", "usdc", "usdz"):
        cmds.file(path, i=True, type="USD Import", ignoreVersion=True)
    else:
        cmds.file(path, i=True, ignoreVersion=True)
    V, F, base = array.array("f"), array.array("i"), 0
    it = om.MItDag(om.MItDag.kDepthFirst, om.MFn.kMesh)
    while not it.isDone():
        dp = it.getPath()
        if not om.MFnDagNode(dp).isIntermediateObject:
            fn = om.MFnMesh(dp)
            pts = fn.getPoints(om.MSpace.kWorld)
            for p in pts:
                V.extend((p.x, p.y, p.z))
            counts, ids = fn.getTriangles()
            F.extend([i + base for i in ids])
            base += len(pts)
        it.next()
    with open(out, "wb") as f:
        f.write(b"KSTM" + struct.pack("<QQ", len(V) // 3, len(F) // 3))
        f.write(V.tobytes())
        f.write(F.tobytes())
    print("KESTREL-OK")
except Exception:
    traceback.print_exc(file=sys.stdout)
    print("KESTREL-FAIL")
'''


def load_via_maya(path: str) -> Optional[Tuple[list, list, str]]:
    """mayapy standalone import → triangles. Slow to start (Maya boots and checks out a licence), so it's the
    last resort after Blender and assimp."""
    mp = _find_mayapy()
    if not mp:
        _vfx_err("Maya: mayapy not found (PATH, $MAYA_LOCATION or /usr/autodesk/maya*)")
        return None
    import tempfile
    tmp = tempfile.mkdtemp(prefix="kestrel-maya-")
    script, out = os.path.join(tmp, "read.py"), os.path.join(tmp, "mesh.bin")
    try:
        with open(script, "w") as f:
            f.write(_MAYA_SCRIPT)
        rc, so, se = _run_streaming([mp, script, os.path.abspath(path), out], timeout=200, verbose=BLENDER_VERBOSE)
        if "KESTREL-OK" not in so or not os.path.exists(out):
            tail = [l for l in so + se if l.strip() and ("rror" in l or "icense" in l or "timed out" in l)][-3:]
            _vfx_err("Maya: " + (" | ".join(x[:200] for x in tail) or f"exit {rc}"))
            return None
        with open(out, "rb") as f:
            head = f.read(20)
            if head[:4] != b"KSTM":
                return None
            nv, nf = struct.unpack("<QQ", head[4:20])
            if np is not None:
                V = np.frombuffer(f.read(nv * 12), dtype=np.float32).reshape(-1, 3)
                F = np.frombuffer(f.read(nf * 12), dtype=np.int32).reshape(-1, 3)
            else:
                import array
                va, fa = array.array("f"), array.array("i")
                va.frombytes(f.read(nv * 12))
                fa.frombytes(f.read(nf * 12))
                V = [tuple(va[i:i + 3]) for i in range(0, len(va), 3)]
                F = [tuple(fa[i:i + 3]) for i in range(0, len(fa), 3)]
        return (V, F, "Maya") if len(V) else None
    except Exception as ex:
        _vfx_err(f"Maya: {ex}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_via_houdini_mesh(path: str) -> Optional[Tuple[list, list, str]]:
    """Houdini as a mesh importer for Alembic (File SOP reads .abc; packed prims are unpacked)."""
    r = load_via_houdini(path)
    if r and "F" in r and len(r["F"]):
        return r["V"], r["F"], r["source"]
    return None
