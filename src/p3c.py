

# --------------------------------------------------------------------------- lightweight 3D model preview
def _read_obj_numpy(data: bytes):
    """OBJ text → (V float64 N×3, F int64 M×3) in bulk with numpy: pull the 'v' and 'f' lines out, drop the
    /vt/vn parts, fan-triangulate n-gons vectorised. (V empty if the file has no usable vertices.)"""
    vlines = [l for l in data.split(b"\n") if l[:2] == b"v "]
    try:
        V = np.array(b" ".join(l[2:].strip() for l in vlines).split(), dtype=np.float64)
        V = V.reshape(len(vlines), -1)[:, :3] if len(vlines) else np.zeros((0, 3))
    except ValueError:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    if not len(V):
        return V, np.zeros((0, 3), dtype=np.int64)
    flines = [l[2:].strip() for l in data.split(b"\n") if l[:2] == b"f "]
    nvert = len(V)
    if not flines:
        return V, np.zeros((0, 3), dtype=np.int64)
    blob = re.sub(rb"[ \t]+", b" ", re.sub(rb"/\S*", b"", b"\n".join(flines))).strip()
    rows = blob.split(b"\n")
    counts = np.fromiter((r.count(b" ") + 1 for r in rows), dtype=np.int64, count=len(rows))
    toks = blob.split()
    idx = np.array(toks).astype(np.int64) if toks else np.zeros(0, dtype=np.int64)
    idx = np.where(idx > 0, idx - 1, nvert + idx)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    tris = []
    for t in range(1, int(counts.max()) - 1 if len(counts) else 0):
        sel = counts > t + 1
        s0 = starts[sel]
        tris.append(np.stack((idx[s0], idx[s0 + t], idx[s0 + t + 1]), axis=1))
    F = np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)
    return V, F


def load_model(path: str, max_faces: int = 500000, norm=None, max_voxels: Optional[int] = None) -> Optional[dict]:
    """Parse OBJ / STL (ascii+binary) / PLY (ascii) into {verts: [(x,y,z)], faces: [(i,j,k)], info: [...]}.
    Big meshes are decimated by skipping faces so the preview stays interactive. For Houdini caches, `norm`
    (from a previous frame's result) keeps every frame of a sequence in the same box and brightness."""
    global LAST_VFX_ERROR
    hk = houdini_kind(os.path.basename(path))
    if hk:
        return load_houdini_cache(path, hk, max_faces, norm, max_voxels)
    ext = os.path.splitext(path)[1].lower()
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    nfaces_total = 0
    extra: List[Tuple[str, str]] = []
    if ext == ".obj" and np is None:
        ext = ".obj-slow"
    if ext == ".obj":
        with open(path, "rb") as f:
            V, F = _read_obj_numpy(f.read())
        if len(V):
            verts, faces = V, F
            nfaces_total = len(faces)
        else:
            ext = ".obj-slow"
    if ext == ".obj-slow":
        with open(path, "rb") as f:
            for line in f:
                if line.startswith(b"v "):
                    p = line.split()
                    if len(p) >= 4:
                        verts.append((float(p[1]), float(p[2]), float(p[3])))
                elif line.startswith(b"f "):
                    idx = []
                    for tok in line.split()[1:]:
                        s = tok.split(b"/")[0]
                        if s:
                            i = int(s)
                            idx.append(i - 1 if i > 0 else len(verts) + i)
                    for k in range(1, len(idx) - 1):          # fan-triangulate n-gons
                        nfaces_total += 1
                        faces.append((idx[0], idx[k], idx[k + 1]))
    elif ext == ".stl":
        with open(path, "rb") as f:
            head = f.read(84)
            f.seek(0)
            if head[:5].lower() == b"solid" and b"facet" in f.read(4096):
                f.seek(0)
                cur = []
                for line in f:
                    t = line.split()
                    if len(t) == 4 and t[0] == b"vertex":
                        cur.append((float(t[1]), float(t[2]), float(t[3])))
                        if len(cur) == 3:
                            base = len(verts)
                            verts.extend(cur)
                            faces.append((base, base + 1, base + 2))
                            nfaces_total += 1
                            cur = []
            elif np is not None:
                # binary STL in one numpy read: 50-byte records (normal, 3 vertices, attribute)
                f.seek(80)
                n = struct.unpack("<I", f.read(4))[0]
                nfaces_total = n
                dt = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
                arr = np.fromfile(f, dtype=dt, count=n)
                step = max(1, len(arr) // max_faces)
                tri = arr["v"][::step]
                verts = tri.reshape(-1, 3)
                faces = np.arange(len(verts), dtype=np.int64).reshape(-1, 3)
            else:
                f.seek(80)
                n = struct.unpack("<I", f.read(4))[0]
                nfaces_total = n
                step = max(1, n // max_faces)
                rec = struct.Struct("<12fH")
                for i in range(n):
                    data = f.read(50)
                    if len(data) < 50:
                        break
                    if i % step:
                        continue
                    v = rec.unpack(data)
                    base = len(verts)
                    verts.extend([(v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])])
                    faces.append((base, base + 1, base + 2))
    elif ext == ".ply":
        with open(path, "rb") as f:
            nv = nf = 0
            props = []
            fmt = "ascii"
            while True:
                line = f.readline().decode("latin-1").strip()
                if line.startswith("format"):
                    fmt = line.split()[1]
                elif line.startswith("element vertex"):
                    nv = int(line.split()[2])
                    section = "v"
                elif line.startswith("element face"):
                    nf = int(line.split()[2])
                    section = "f"
                elif line.startswith("property") and section == "v":
                    props.append(line.split()[2])
                elif line == "end_header" or not line:
                    break
            if fmt != "ascii":
                return None
            xi, yi, zi = props.index("x"), props.index("y"), props.index("z")
            for _ in range(nv):
                t = f.readline().split()
                verts.append((float(t[xi]), float(t[yi]), float(t[zi])))
            nfaces_total = nf
            for _ in range(nf):
                t = f.readline().split()
                k = int(t[0])
                idx = [int(x) for x in t[1:1 + k]]
                for j in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[j], idx[j + 1]))
    elif ext == ".fbx":
        try:
            verts, faces, nm = load_fbx(path)
        except Exception:
            verts, faces, nm = [], [], 0
        extra = [("Meshes", str(nm)), ("Source", "FBX " + ("binary" if open(path, "rb").read(18) == b"Kaydara FBX Binary" else "ASCII"))]
        if not verts:
            for loader in (load_via_assimp, load_via_blender, load_via_maya):
                got = loader(path)
                if got:
                    verts, faces = got[0], got[1]
                    extra = [("Source", f"via {got[2]}")]
                    break
            if not verts:
                return {"error": "Native FBX parser found no mesh. " + (LAST_VFX_ERROR or "")}
        nfaces_total = len(faces)
    elif ext in (".usd", ".usda", ".usdc", ".usdz", ".abc", ".gltf", ".glb", ".dae", ".blend", ".3ds", ".x3d", ".lwo", ".off"):
        verts, faces, src = [], [], ""
        if ext == ".usda":
            with open(path, "r", errors="ignore") as f:
                verts, faces, nm = load_usda_text(f.read())
            src = f"USDA text, {nm} mesh{'es' if nm != 1 else ''}"
        if not verts and ext in (".usd", ".usdc", ".usdz") and shutil.which("usdcat"):
            try:
                r = subprocess.run(["usdcat", "--flatten", path], capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    verts, faces, nm = load_usda_text(r.stdout)
                    src = f"usdcat, {nm} mesh{'es' if nm != 1 else ''}"
            except (OSError, subprocess.TimeoutExpired):
                pass
        # open-source routes first: Blender (abc/usd/gltf/dae/blend…), assimp (fbx/dae/gltf/3ds…); vendor Pythons last
        LAST_VFX_ERROR = ""
        abc_fmt = alembic_format(path) if ext == ".abc" else ""
        if abc_fmt == "hdf5":
            # Legacy HDF5 Alembic: Blender and every current open-source reader only speak Ogawa.
            _vfx_err("Legacy HDF5 Alembic (pre-2014 container): Blender only reads Ogawa archives.")
            # open-source first: h5py reads the HDF5 container directly; vendor PyAlembic only as a last resort
            loaders = [load_abc_hdf5]          # (Maya's PyAlembic rejects HDF5 too: "Unsupported core type")
        else:
            # Blender first; Maya for studios that don't deploy Blender; Houdini reads Alembic too
            loaders = [load_via_blender, load_via_assimp]
            if ext in (".abc", ".usd", ".usda", ".usdc", ".usdz", ".obj", ".dae"):
                loaders.append(load_via_maya)
            if ext == ".abc":
                loaders.append(load_via_houdini_mesh)
            loaders.append(load_via_vfx_python)
        for loader in loaders:
            if len(verts):
                break
            got = loader(path)
            if got:
                verts, faces, src = got[0], got[1], f"via {got[2]}"
        if not len(verts):
            return {"error": LAST_VFX_ERROR or "No importer produced any geometry."}
        nfaces_total = len(faces)
        extra = [("Source", src)] + ([("Alembic", {"ogawa": "Ogawa", "hdf5": "HDF5 (legacy)"}.get(abc_fmt, "unknown container"))] if ext == ".abc" else [])
    elif ext not in (".obj", ".obj-slow"):
        return None
    if not len(verts):
        return None
    if np is not None:
        return _finish_model_numpy(verts, faces, max_faces, nfaces_total, extra)
    nverts_total = len(verts)
    if len(faces) > max_faces:
        step = len(faces) // max_faces + 1
        faces = faces[::step]
        # keep only the vertices the surviving faces reference: a 3.7M-vertex cache would otherwise be
        # normalised, pickled back from the worker process and transformed every frame for nothing
        used = sorted({i for f in faces for i in f})
        remap = {old: new for new, old in enumerate(used)}
        verts = [verts[i] for i in used]
        faces = [(remap[i], remap[j], remap[k]) for i, j, k in faces]
    # normalize into a unit box around the origin
    xs, ys, zs = (sorted(c) for c in zip(*verts))
    cx, cy, cz = (xs[0] + xs[-1]) / 2, (ys[0] + ys[-1]) / 2, (zs[0] + zs[-1]) / 2
    scale = max(xs[-1] - xs[0], ys[-1] - ys[0], zs[-1] - zs[0], 1e-9)
    verts = [((x - cx) / scale, (y - cy) / scale, (z - cz) / scale) for x, y, z in verts]
    info = [("Vertices", f"{nverts_total:,}" + (f" (showing {len(verts):,})" if len(verts) < nverts_total else "")), ("Faces", f"{nfaces_total:,}" + (f" (showing {len(faces):,})" if len(faces) < nfaces_total else "")),
            ("Bounds", f"{xs[-1] - xs[0]:.3g} × {ys[-1] - ys[0]:.3g} × {zs[-1] - zs[0]:.3g}")] + extra
    # face normals once, in model space (rotated per frame with 6 multiplies instead of a cross product)
    normals = []
    for i, j, k in faces:
        ax, ay, az = verts[i]
        bx, by, bz = verts[j]
        cx, cy, cz = verts[k]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        normals.append((nx / ln, ny / ln, nz / ln))
    return {"verts": verts, "faces": faces, "normals": normals, "info": info, "nfaces_total": nfaces_total}


def _finish_model_numpy(verts, faces, max_faces: int, nfaces_total: int, extra: list, bounds=None) -> dict:
    """Vectorised post-processing: decimate, drop unused vertices, normalise to a unit box, face + vertex
    normals. verts/faces may be Python lists or numpy arrays. Decimation is by VERTEX CLUSTERING (snap
    vertices to a grid, merge, drop collapsed faces) rather than by skipping faces: skipping leaves a
    swiss-cheese surface where back faces show through the holes, clustering gives a closed low-res proxy
    like a LOD. Everything here is sized for multi-million-face meshes: no np.unique on faces (sort-bound),
    bincount instead of add.at, float32/int32 outputs so the worker→UI pickle stays small."""
    V = np.asarray(verts, dtype=np.float32).reshape(-1, 3)
    F = np.asarray(faces, dtype=np.int32).reshape(-1, 3)
    nverts_total = len(V)
    lo, hi = bounds if bounds is not None else (V.min(axis=0), V.max(axis=0))
    size = (hi - lo).astype(np.float64)
    scale = max(float(size.max()), 1e-9)
    V = ((V - (lo + hi) / 2) / scale).astype(np.float32)   # unit box around the origin, longest side = 1
    decim_note = ""
    if len(F) > max_faces:
        # face count scales ~ grid² for a surface: start from that estimate, then correct at most 3 times
        grid = int(max(16, min(400, 256 * math.sqrt(max_faces / len(F)) * 1.15)))
        best = None
        for _ in range(4):
            q = np.floor((V + 0.5) * grid).astype(np.int64).clip(0, grid - 1)
            key = (q[:, 0] * grid + q[:, 1]) * grid + q[:, 2]
            uniq, inv = np.unique(key, return_inverse=True)
            Fq = inv[F].astype(np.int64)
            keep = (Fq[:, 0] != Fq[:, 1]) & (Fq[:, 1] != Fq[:, 2]) & (Fq[:, 0] != Fq[:, 2])
            Fq = Fq[keep]
            if len(Fq):
                srt = np.sort(Fq, axis=1)
                nc = len(uniq)
                if nc < (1 << 21):
                    packed = (srt[:, 0] << 42) | (srt[:, 1] << 21) | srt[:, 2]
                    _, first = np.unique(packed, return_index=True)
                else:
                    _, first = np.unique(srt.view([("a", np.int64), ("b", np.int64), ("c", np.int64)]).ravel(), return_index=True)
                Fq = Fq[np.sort(first)]                         # dedupe, keep original winding
            best = (grid, uniq, inv, Fq)
            if len(Fq) <= max_faces and len(Fq) >= max_faces * 0.6:
                break
            if len(Fq) <= max_faces and grid >= 400:
                break
            grid = int(max(16, min(400, grid * math.sqrt(max_faces / max(len(Fq), 1)) * 0.98)))
            if grid == best[0]:
                break
        grid, uniq, inv, Fq = best
        if len(Fq) > max_faces:                                  # still over (pathological): fall back to a strict grid
            Fq = Fq[:max_faces]
        # cluster position = mean of the member vertices (smoother than the cell centre)
        counts = np.bincount(inv, minlength=len(uniq)).astype(np.float32)
        Vc = np.empty((len(uniq), 3), dtype=np.float32)
        for c in range(3):
            Vc[:, c] = np.bincount(inv, weights=V[:, c], minlength=len(uniq)) / counts
        V, F = Vc, Fq.astype(np.int32)
        decim_note = f" (showing {len(F):,} after clustering at {grid}³)"
    # drop vertices no face references (mask + cumsum: np.unique would sort 3×faces ints)
    used = np.zeros(len(V), dtype=bool)
    used[F] = True
    remap = np.cumsum(used, dtype=np.int32) - 1
    V = V[used]
    F = remap[F]
    if len(F):
        A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        # Winding convention differs by source (Alembic/Maya export clockwise, OBJ/STL counter-clockwise).
        # The signed volume (divergence theorem) tells which way this mesh faces; flip if negative so that
        # face normals point outwards for the flat-shading path.
        signed_vol = float(np.einsum("ij,ij->i", A.astype(np.float64), np.cross(B, C).astype(np.float64)).sum())
        if signed_vol < 0:
            F = np.ascontiguousarray(F[:, [0, 2, 1]])
            B, C = C, B
        N = np.cross(B - A, C - A).astype(np.float32)
        ln = np.linalg.norm(N, axis=1)
        ln[ln == 0] = 1.0
        N /= ln[:, None]
        # vertex normals = normalised sum of the adjacent unit face normals → smooth shading
        VN = np.zeros_like(V)
        for col in range(3):
            idx = F[:, col]
            for c in range(3):
                VN[:, c] += np.bincount(idx, weights=N[:, c], minlength=len(V)).astype(np.float32)
        vl = np.linalg.norm(VN, axis=1)
        vl[vl == 0] = 1.0
        VN /= vl[:, None]
    else:
        N = np.zeros((0, 3), dtype=np.float32)
        VN = np.zeros((0, 3), dtype=np.float32)
    info = [("Vertices", f"{nverts_total:,}" + (f" (showing {len(V):,})" if len(V) < nverts_total else "")),
            ("Faces", f"{nfaces_total:,}" + decim_note),
            ("Bounds", f"{size[0]:.3g} × {size[1]:.3g} × {size[2]:.3g}")] + extra
    return {"verts": V, "faces": F, "normals": N, "vnormals": VN, "info": info, "nfaces_total": nfaces_total}



def load_houdini_cache(path: str, kind: str, max_faces: int, norm=None, max_voxels: Optional[int] = None) -> Optional[dict]:
    """.bgeo/.geo/.vdb → model dict with any of: mesh, particles (points + Cd), volume voxels.
    Order: VDB: built-in reader (fast, no licence) → Houdini (hython) → gconvert (.geo/.bgeo) →
    for VDB: OpenVDB Python module → Blender (volume → mesh)."""
    global LAST_VFX_ERROR
    LAST_VFX_ERROR = ""
    if np is None:
        return {"error": "Houdini caches need numpy (sudo dnf install python3-numpy)."}
    mv = max_voxels or VDB_MAX_VOXELS
    got = load_vdb_native(path, mv, norm[3] if norm is not None and len(norm) > 3 else None) if kind == "vdb" else None
    if got is None:
        got = load_via_houdini(path, max_voxels=mv)
    if got is None and kind == "geo":
        got = load_via_gconvert(path)
    if got is None and kind == "vdb":
        got = load_vdb_openvdb(path)
        if got is None:
            b = load_via_blender(path)
            if b:
                got = {"V": b[0], "F": b[1], "info": [("Volume", "shown as a surface (Blender volume → mesh)")], "source": "Blender"}
    if got is None:
        hint = "" if _find_houdini()["hython"] else " Install Houdini (hython) to preview Houdini caches."
        return {"error": (LAST_VFX_ERROR or "No reader for this cache.") + hint}
    return _finish_cache_numpy(got, max_faces, norm=norm)


def _finish_cache_numpy(got: dict, max_faces: int, max_points: int = 2_000_000, norm=None) -> dict:
    """Normalise mesh / points / volume voxels into ONE unit box (so they stay registered), then finish the mesh
    part with the usual pipeline. Returns the model dict plus 'points', 'pcolor', 'vpts', 'vden', 'vsize', and
    'norm' = (centre, scale, density max): pass it back for the next frame of a sequence so the box and the
    smoke brightness stay put while it plays."""
    parts = [np.asarray(got[k], dtype=np.float64).reshape(-1, 3) for k in ("V", "P", "vP") if k in got and len(got[k])]
    if not parts:
        return {"error": "The cache contains no points, polygons or volumes."}
    allp = np.concatenate([p[:: max(1, len(p) // 200000)] for p in parts] + [p.min(0, keepdims=True) for p in parts] + [p.max(0, keepdims=True) for p in parts])
    lo, hi = allp.min(axis=0), allp.max(axis=0)
    size = hi - lo
    if norm is not None:
        centre, scale, dmax_ref = np.asarray(norm[0], dtype=np.float64), float(norm[1]), norm[2]
    else:
        centre, scale, dmax_ref = (lo + hi) / 2, max(float(size.max()), 1e-9), None
    to_box = lambda A: ((np.asarray(A, dtype=np.float64).reshape(-1, 3) - centre) / scale).astype(np.float32)
    extra = list(got.get("info", [])) + [("Source", got.get("source", ""))]
    if "F" in got and len(got["F"]):
        half = np.full(3, scale / 2)
        m = _finish_model_numpy(got["V"], got["F"], max_faces, len(got["F"]), [],
                                bounds=((centre - half).astype(np.float32), (centre + half).astype(np.float32)))
        info = [r for r in m["info"] if r[0] in ("Vertices", "Faces")]
    else:
        m = {"verts": np.zeros((0, 3), np.float32), "faces": np.zeros((0, 3), np.int32), "normals": np.zeros((0, 3), np.float32),
             "vnormals": np.zeros((0, 3), np.float32), "nfaces_total": 0}
        info = []
    if "P" in got and len(got["P"]):
        P = to_box(got["P"])
        step = max(1, len(P) // max_points)
        m["points"] = np.ascontiguousarray(P[::step])
        if "Cd" in got and len(got["Cd"]) == len(P):
            m["pcolor"] = np.clip(np.asarray(got["Cd"], dtype=np.float32)[::step], 0, 1)
        if "pS" in got:
            m["psize"] = float(np.asarray(got["pS"]).ravel()[0]) * step / scale
    if "vP" in got and len(got["vP"]):
        m["vpts"] = to_box(got["vP"])
        d = np.asarray(got["vD"], dtype=np.float32).ravel()
        dmax = float(dmax_ref) if dmax_ref else max(float(d.max()), 1e-9)
        m["vden"] = d / dmax
        m["vsize"] = float(np.asarray(got.get("vS", [scale / 100])).ravel()[0]) / scale
    m["info"] = info + [("Bounds", f"{size[0]:.3g} × {size[1]:.3g} × {size[2]:.3g}")] + [r for r in extra if r[1]]
    vd = got.get("vD")
    m["norm"] = (tuple(float(x) for x in centre), scale,
                 dmax_ref or (max(float(np.max(vd)), 1e-9) if vd is not None and len(vd) else None),
                 norm[3] if norm is not None and len(norm) > 3 else got.get("vStep"))
    return m


def _project(V, W, H, yaw, pitch, zoom, panx, pany):
    """Camera used by every software path: per-point screen x/y, view depth (larger = nearer) and the
    perspective factor (screen pixels per model unit at that point)."""
    f32 = np.float32
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    k16 = min(W, H) * 0.8 * zoom * 1.6
    ox, oy = W / 2 + panx, H / 2 + pany
    x, y, z = V[:, 0], V[:, 1], V[:, 2]
    xr = x * f32(cy_) + z * f32(sy_)
    zr = -x * f32(sy_) + z * f32(cy_)
    yp = y * f32(cp) - zr * f32(sp)
    depth = y * f32(sp) + zr * f32(cp)
    wdiv = f32(k16) / (f32(2.6) - depth)
    return f32(ox) + xr * wdiv, f32(oy) - yp * wdiv, depth, wdiv


def render_cache(V, F, N, VN, EX, params):
    """Frame for a model with particles and/or volume voxels (mesh optional). Opaque pass (mesh triangles and
    point splats) into a shared z-buffer, then the volume as smoke: voxel densities in front of the opaque
    surface are summed per pixel and composited with an auto-exposed absorption curve."""
    W, H, yaw, pitch, zoom, panx, pany, smooth, wire, bg_rgb, fg_rgb = params
    if W < 2 or H < 2:
        return None
    f32 = np.float32
    n = W * H
    zbuf = np.full(n, -np.inf, dtype=f32)
    col = np.empty((n, 3), dtype=f32)
    col[:] = np.array(bg_rgb, dtype=f32) / f32(255)
    drew = False
    if F is not None and len(F):
        px, py, depth, _ = _project(V, W, H, yaw, pitch, zoom, panx, pany)
        cy_, sy_, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        NN = VN if (smooth and VN is not None and len(VN)) else N
        nx1 = NN[:, 0] * f32(cy_) + NN[:, 2] * f32(sy_)
        nz1 = -NN[:, 0] * f32(sy_) + NN[:, 2] * f32(cy_)
        ny2 = NN[:, 1] * f32(cp) - nz1 * f32(sp)
        nz2 = NN[:, 1] * f32(sp) + nz1 * f32(cp)
        sh = f32(0.35) + f32(0.65) * np.minimum(np.abs(nx1 * f32(0.4) + ny2 * f32(0.7) + nz2 * f32(0.6)), f32(1.0))
        vsh, fsh = (sh, None) if NN is VN else (None, sh)
        bufs = _rasterize(W, H, px, py, depth, F[:, 0], F[:, 1], F[:, 2], vsh, fsh, bg_rgb, buffers=True)
        if bufs is not None:
            zb, sb, hit = bufs
            zbuf = zb
            s_ = sb[hit]
            col[hit, 0] = np.clip(0.42 * s_ + 0.1, 0, 1)
            col[hit, 1] = np.clip(0.55 * s_ + 0.1, 0, 1)
            col[hit, 2] = np.clip(0.72 * s_ + 0.12, 0, 1)
            drew = bool(hit.any())
    P = EX.get("points")
    if P is not None and len(P):
        px, py, depth, wdiv_p = _project(P, W, H, yaw, pitch, zoom, panx, pany)
        ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        idx = np.nonzero(ok)[0]
        if len(idx):
            cue = (f32(0.55) + f32(0.45) * np.clip((depth[idx] + f32(0.6)) / f32(1.2), 0, 1))[:, None]
            pc = EX.get("pcolor")
            c = (pc[idx] if pc is not None else np.array([0.62, 0.75, 0.92], dtype=f32)[None, :]) * cue
            if EX.get("psize"):                                      # known spacing (level-set voxels): close the gaps
                r = int(np.clip(round(float(EX["psize"]) * float(np.median(wdiv_p[idx])) * 1.15), 1, 12))
            else:
                r = 1 if len(idx) > 300_000 else 2                   # 2×2 splats when sparse, so points read
            before = zbuf.copy()
            for dx in range(r):
                for dy in range(r):
                    sx = np.clip(px[idx].astype(np.int32) + dx, 0, W - 1)
                    sy = np.clip(py[idx].astype(np.int32) + dy, 0, H - 1)
                    pix = sy * W + sx
                    dz = depth[idx]
                    np.maximum.at(zbuf, pix, dz)
                    win = dz >= zbuf[pix]
                    col[pix[win]] = c[win] if c.shape[0] == len(idx) else c[0]
            # Shade the splats from the depth buffer they just wrote (normals from screen-space depth slopes):
            # dense particle sheets and level-set surfaces read as 3D forms instead of flat cut-outs.
            mine = (zbuf > before).reshape(H, W)
            if mine.any():
                z = np.where(mine, zbuf.reshape(H, W), np.nan)
                gx = np.zeros_like(z)
                gy = np.zeros_like(z)
                gx[:, 1:-1] = (z[:, 2:] - z[:, :-2]) * 0.5
                gy[1:-1, :] = (z[2:, :] - z[:-2, :]) * 0.5
                gx = np.nan_to_num(gx, nan=0.0)
                gy = np.nan_to_num(gy, nan=0.0)
                k = float(np.median(wdiv_p[idx])) if len(idx) else 1.0
                nx, ny = -gx * k, gy * k
                ln = np.sqrt(nx * nx + ny * ny + 1.0)
                lam = np.clip((nx * 0.4 + ny * 0.7 + 0.6) / ln, 0, 1)
                shade = (0.45 + 0.55 * lam).astype(f32).reshape(-1)
                m1 = mine.reshape(-1)
                col[m1] *= shade[m1, None]
            drew = True
    VP = EX.get("vpts")
    if VP is not None and len(VP):
        px, py, depth, wdiv = _project(VP, W, H, yaw, pitch, zoom, panx, pany)
        dens = EX["vden"]
        rad = int(np.clip(round(float(EX.get("vsize", 0.01)) * float(np.median(wdiv))), 1, 4))
        acc = np.zeros(n, dtype=np.float64)
        ok = (px >= -rad - 1) & (px < W + rad) & (py >= -rad - 1) & (py < H + rad)
        idx = np.nonzero(ok)[0]
        if len(idx):
            # Bilinear splats: each voxel spreads over the pixels it straddles by its sub-pixel position, so a
            # regular voxel grid seen straight on doesn't alias into dark seams (moiré) between pixel columns.
            fx = px[idx] - f32(0.5) - f32(rad // 2)
            fy = py[idx] - f32(0.5) - f32(rad // 2)
            x0 = np.floor(fx)
            y0 = np.floor(fy)
            tx, ty = fx - x0, fy - y0
            x0 = x0.astype(np.int32)
            y0 = y0.astype(np.int32)
            dz = depth[idx]
            w0 = (dens[idx] / (rad * rad)).astype(np.float64)
            for cx, cy, wb in ((0, 0, (1 - tx) * (1 - ty)), (1, 0, tx * (1 - ty)), (0, 1, (1 - tx) * ty), (1, 1, tx * ty)):
                wc = w0 * wb
                for dx in range(rad):
                    for dy in range(rad):
                        sx = x0 + (cx + dx)
                        sy = y0 + (cy + dy)
                        inb = (sx >= 0) & (sx < W) & (sy >= 0) & (sy < H)
                        pix = sy[inb] * W + sx[inb]
                        front = dz[inb] > zbuf[pix]                      # occluded by the mesh / particles?
                        acc += np.bincount(pix[front], weights=wc[inb][front], minlength=n)
            nz = acc[acc > 0]
            if len(nz):
                ref = float(np.percentile(nz, 97)) or 1.0
                a = (1.0 - np.exp(-2.5 * acc / ref)).astype(f32)[:, None]
                smoke = (f32(0.94) - f32(0.30) * a) * np.array([0.97, 0.98, 1.0], dtype=f32)[None, :]
                col = col * (f32(1) - a) + smoke * a
                drew = True
    if not drew:
        return None
    out = np.empty((n, 4), dtype=np.uint8)
    rgb8 = np.clip(col * 255, 0, 255).astype(np.uint8)
    out[:, 2], out[:, 1], out[:, 0], out[:, 3] = rgb8[:, 0], rgb8[:, 1], rgb8[:, 2], 255
    return W, H, out.tobytes()


def software_render(V, F, N, VN, params, EX=None):
    """Render one frame of a mesh with the numpy rasteriser. params = (W, H, yaw, pitch, zoom, panx, pany,
    smooth, wire, bg_rgb, fg_rgb). Returns (W, H, BGRX bytes) or None. Runs in the render worker process.
    EX (particles / volume voxels from Houdini caches) switches to render_cache."""
    if EX and (not params[8] or F is None or not len(F)):
        return render_cache(V, F, N, VN, EX, params)
    W, H, yaw, pitch, zoom, panx, pany, smooth, wire, bg_rgb, fg_rgb = params
    if W < 2 or H < 2 or F is None or not len(F):
        return None
    f32 = np.float32
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    k16 = min(W, H) * 0.8 * zoom * 1.6
    ox, oy = W / 2 + panx, H / 2 + pany
    x, y, z = V[:, 0], V[:, 1], V[:, 2]
    xr = x * f32(cy_) + z * f32(sy_)
    zr = -x * f32(sy_) + z * f32(cy_)
    yp = y * f32(cp) - zr * f32(sp)
    depth = y * f32(sp) + zr * f32(cp)
    wdiv = f32(k16) / (f32(2.6) - depth)
    px = f32(ox) + xr * wdiv
    py = f32(oy) - yp * wdiv
    i, j, k = F[:, 0], F[:, 1], F[:, 2]
    if wire:
        return _wire_render(W, H, px, py, i, j, k, bg_rgb, fg_rgb)

    def lit(NN):
        nx1 = NN[:, 0] * f32(cy_) + NN[:, 2] * f32(sy_)
        nz1 = -NN[:, 0] * f32(sy_) + NN[:, 2] * f32(cy_)
        ny2 = NN[:, 1] * f32(cp) - nz1 * f32(sp)
        nz2 = NN[:, 1] * f32(sp) + nz1 * f32(cp)
        d = np.abs(nx1 * f32(0.4) + ny2 * f32(0.7) + nz2 * f32(0.6))      # two-sided lighting
        return f32(0.35) + f32(0.65) * np.minimum(d, f32(1.0))
    if smooth and VN is not None and len(VN):
        vshade, fshade = lit(VN), None
    else:
        vshade = None
        fshade = lit(N) if N is not None and len(N) else np.full(len(F), 1.0, dtype=f32)
    rgb = _rasterize(W, H, px, py, depth, i, j, k, vshade, fshade, bg_rgb)
    if rgb is None:
        return None
    return W, H, rgb.tobytes()


def _wire_render(W, H, px, py, i, j, k, bg_rgb, fg_rgb):
    """Wireframe as point splats along every edge (no per-line Cairo calls, so it scales to millions of
    edges): each edge is sampled every pixel of its length and the samples are scattered into the image."""
    f32 = np.float32
    out = np.empty((H * W, 4), dtype=np.uint8)
    out[:, 2], out[:, 1], out[:, 0], out[:, 3] = bg_rgb[0], bg_rgb[1], bg_rgb[2], 255
    col = (int(fg_rgb[2] * 255 * 0.55 + bg_rgb[2] * 0.45), int(fg_rgb[1] * 255 * 0.55 + bg_rgb[1] * 0.45), int(fg_rgb[0] * 255 * 0.55 + bg_rgb[0] * 0.45))
    for a_i, b_i in ((i, j), (j, k), (k, i)):
        ax, ay, bx, by = px[a_i], py[a_i], px[b_i], py[b_i]
        on = ((np.maximum(ax, bx) >= 0) & (np.minimum(ax, bx) < W) & (np.maximum(ay, by) >= 0) & (np.minimum(ay, by) < H))
        ax, ay, bx, by = ax[on], ay[on], bx[on], by[on]
        n = np.maximum(1, np.ceil(np.maximum(np.abs(bx - ax), np.abs(by - ay)))).astype(np.int64)
        # cap the work for absurd zoom levels
        total = int(n.sum())
        if total > 30_000_000:
            keep = np.random.default_rng(0).random(len(n)) < 30_000_000 / total
            ax, ay, bx, by, n = ax[keep], ay[keep], bx[keep], by[keep], n[keep]
        rep = np.repeat(np.arange(len(n)), n)
        local = (np.arange(len(rep)) - np.repeat(np.cumsum(n) - n, n)).astype(f32)
        t = local / np.repeat(n, n).astype(f32)
        xs = (ax[rep] + (bx[rep] - ax[rep]) * t).astype(np.int32)
        ys = (ay[rep] + (by[rep] - ay[rep]) * t).astype(np.int32)
        ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        pix = ys[ok] * W + xs[ok]
        out[pix, 2], out[pix, 1], out[pix, 0] = col[2], col[1], col[0]
    return W, H, out.reshape(H, W, 4).tobytes()


def _rasterize(W, H, px, py, depth, i, j, k, vshade, fshade, bg_rgb, buffers=False):
    """Vectorised triangle rasteriser with a z-buffer, in numpy.

    px/py/depth are per-VERTEX screen coordinates and view depth (larger = nearer); i/j/k index them per
    face; vshade is a per-vertex shade (Gouraud) or None, in which case fshade is the per-face shade.

    Two lanes, chosen per triangle by its screen bounding box:
      * tiny (spans at most 1 px — the overwhelming majority on a multi-million-face mesh at preview size):
        splatted as a single pixel at the centroid with the mean corner shade. Edge tests on sub-pixel
        triangles cost a lot and change nothing visible.
      * everything else: the bounding box is expanded into candidate pixels (np.repeat), the three edge
        functions decide which are inside, depth and shade are interpolated barycentrically.
    np.maximum.at resolves the nearest fragment per pixel (fast path in numpy ≥ 1.25). Large lanes are
    chunked so the candidate arrays stay around ~2M entries (float32) whatever the mesh.
    Returns a BGRX uint8 buffer (cairo RGB24) or None if nothing is on screen."""
    f32 = np.float32
    ax, ay, bx, by, cx, cy = px[i], py[i], px[j], py[j], px[k], py[k]
    da, db, dc = depth[i], depth[j], depth[k]
    if vshade is not None:
        sa, sb, sc = vshade[i], vshade[j], vshade[k]
    else:
        sa = sb = sc = fshade.astype(f32)
    minx = np.minimum(np.minimum(ax, bx), cx)
    maxx = np.maximum(np.maximum(ax, bx), cx)
    miny = np.minimum(np.minimum(ay, by), cy)
    maxy = np.maximum(np.maximum(ay, by), cy)
    onscreen = (maxx >= 0) & (minx < W) & (maxy >= 0) & (miny < H)
    tiny = onscreen & ((maxx - minx) <= 1.0) & ((maxy - miny) <= 1.0)
    big = onscreen & ~tiny
    zbuf = np.full(W * H, -np.inf, dtype=f32)
    sbuf = np.zeros(W * H, dtype=f32)

    # ---- lane 1: splats
    if tiny.any():
        t = np.nonzero(tiny)[0]
        sx = np.clip(((ax[t] + bx[t] + cx[t]) * f32(1 / 3)).astype(np.int32), 0, W - 1)
        sy = np.clip(((ay[t] + by[t] + cy[t]) * f32(1 / 3)).astype(np.int32), 0, H - 1)
        pix = sy * W + sx
        zc = (da[t] + db[t] + dc[t]) * f32(1 / 3)
        np.maximum.at(zbuf, pix, zc)
        win = zc >= zbuf[pix]
        sbuf[pix[win]] = ((sa[t] + sb[t] + sc[t]) * f32(1 / 3))[win]

    # ---- lane 2: real rasterisation for triangles that cover pixels
    if big.any():
        x0 = np.clip(np.floor(minx), 0, W).astype(np.int32)
        x1 = np.clip(np.ceil(maxx) + 1, 0, W).astype(np.int32)
        y0 = np.clip(np.floor(miny), 0, H).astype(np.int32)
        y1 = np.clip(np.ceil(maxy) + 1, 0, H).astype(np.int32)
        bw, bh = x1 - x0, y1 - y0
        area2 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        ok = big & (bw > 0) & (bh > 0) & (np.abs(area2) > 1e-6)
        idx_all = np.nonzero(ok)[0]
        counts_all = bw[idx_all].astype(np.int64) * bh[idx_all]
        if len(idx_all):
            csum = np.cumsum(counts_all)
            bounds = np.searchsorted(csum, np.arange(0, csum[-1] + 2_000_000, 2_000_000))
            bounds = np.unique(np.concatenate((bounds, [len(idx_all)])))
            for s0, s1 in zip(bounds[:-1], bounds[1:]):
                t = idx_all[s0:s1]
                n = counts_all[s0:s1]
                total = int(n.sum())
                if not len(t) or total == 0:
                    continue
                tri = np.repeat(np.arange(len(t), dtype=np.int32), n)
                local = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(n) - n, n)
                w_rep = np.repeat(bw[t], n)
                pxs = np.repeat(x0[t], n) + (local % w_rep).astype(np.int32)
                pys = np.repeat(y0[t], n) + (local // w_rep).astype(np.int32)
                fx = pxs.astype(f32) + f32(0.5)
                fy = pys.astype(f32) + f32(0.5)
                tt = t[tri]
                A_x, A_y, B_x, B_y, C_x, C_y = ax[tt], ay[tt], bx[tt], by[tt], cx[tt], cy[tt]
                w0 = (B_x - fx) * (C_y - fy) - (B_y - fy) * (C_x - fx)      # weight of A
                w1 = (C_x - fx) * (A_y - fy) - (C_y - fy) * (A_x - fx)      # weight of B
                w2 = (A_x - fx) * (B_y - fy) - (A_y - fy) * (B_x - fx)      # weight of C
                inside = ((w0 >= 0) & (w1 >= 0) & (w2 >= 0)) | ((w0 <= 0) & (w1 <= 0) & (w2 <= 0))
                if not inside.any():
                    continue
                sel = np.nonzero(inside)[0]
                tt = tt[sel]
                inv_a = f32(1.0) / area2[tt]
                w0s, w1s, w2s = w0[sel] * inv_a, w1[sel] * inv_a, w2[sel] * inv_a
                zz = w0s * da[tt] + w1s * db[tt] + w2s * dc[tt]
                pix = pys[sel] * W + pxs[sel]
                np.maximum.at(zbuf, pix, zz)
                win = zz >= zbuf[pix]
                tw = tt[win]
                sbuf[pix[win]] = w0s[win] * sa[tw] + w1s[win] * sb[tw] + w2s[win] * sc[tw]

    hit = zbuf > -np.inf
    if buffers:
        return zbuf, sbuf, hit
    if not hit.any():
        return None
    out = np.empty((H * W, 4), dtype=np.uint8)
    base_r, base_g, base_b = bg_rgb
    out[:, 2], out[:, 1], out[:, 0], out[:, 3] = base_r, base_g, base_b, 255
    sh = sbuf[hit]
    out[hit, 2] = np.clip((0.42 * sh + 0.1) * 255, 0, 255).astype(np.uint8)    # R
    out[hit, 1] = np.clip((0.55 * sh + 0.1) * 255, 0, 255).astype(np.uint8)    # G
    out[hit, 0] = np.clip((0.72 * sh + 0.12) * 255, 0, 255).astype(np.uint8)   # B
    return np.ascontiguousarray(out.reshape(H, W, 4))



def _render_worker_main(conn):
    """Render worker process: holds one model, answers ('frame', params) with the pixels."""
    pin_worker()
    V = F = N = VN = None
    EX = None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        kind = msg[0]
        if kind == "quit":
            return
        if kind == "model":
            V, F, N, VN = msg[1][:4]
            EX = msg[1][4] if len(msg[1]) > 4 else None
            continue
        if kind == "frame":
            try:
                res = software_render(V, F, N, VN, msg[1], EX)
            except Exception as ex:          # never let a bad frame kill the worker
                res = None
                print("kestrel: render failed —", ex, file=sys.stderr)
            try:
                conn.send((msg[1], res))
            except (OSError, ValueError):
                return


_RENDER_WORKER = None


def get_render_worker():
    """One render process for the whole app (created early by warm_workers, otherwise on first use)."""
    global _RENDER_WORKER
    if _RENDER_WORKER is None or not _RENDER_WORKER.proc.is_alive():
        _RENDER_WORKER = RenderWorker()
    return _RENDER_WORKER


class RenderWorker:
    """Owns the render process and the result thread; results are delivered on the GTK thread via idle_add."""

    def __init__(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_render_worker_main, args=(child,), daemon=True)
        self.proc.start()
        child.close()
        self._cb = None
        # ONE sender thread with a queue: messages must reach the worker in the order they were issued
        # (model before the frame that needs it). Separate per-message threads raced, and a frame request
        # overtaking its model made the worker answer with no image — the software viewer then never showed.
        import queue
        self._q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    def _sender(self):
        while True:
            msg = self._q.get()
            try:
                self.conn.send(msg)
            except (OSError, ValueError):
                return

    def set_model(self, V, F, N, VN, EX=None):
        self._q.put(("model", (V, F, N, VN, EX)))      # a 5M-face model takes ~0.3 s to pipe across, off-thread

    def render(self, params, cb):
        self._cb = cb
        self._q.put(("frame", params))

    def _reader(self):
        while True:
            try:
                params, res = self.conn.recv()
            except (EOFError, OSError):
                return
            cb = self._cb
            if cb:
                GLib.idle_add(lambda: (cb(params, res), False)[1])

    def close(self):
        try:
            self._q.put(("quit",))
        except Exception:
            pass
        try:
            self.proc.join(timeout=1)
            if self.proc.is_alive():
                self.proc.kill()
        except Exception:
            pass


class ModelNav:
    """Maya-style navigation shared by the software (ModelView) and OpenGL (GLModelView) previewers:
    LMB orbit, MMB pan, Alt+RMB drag-zoom, wheel zoom, double-click reset, W wireframe, S flat/smooth."""

    def _nav_init(self):
        self.model: Optional[dict] = None
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        self.pan = [0.0, 0.0]
        self.wire = False
        self.smooth = True
        self.thin = False
        self.drag = None            # (x, y, mode) mode: orbit | pan | zoom
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", lambda w, e: (setattr(self, "drag", None), self.queue_draw()) and False)
        self.connect("motion-notify-event", self._motion)
        self.connect("scroll-event", self._scroll)
        self.connect("key-press-event", self._key)

    def _reset_view(self):
        self.yaw, self.pitch, self.zoom = 0.6, 0.35, 1.0
        self.pan = [0.0, 0.0]

    def _press(self, w, event):
        """Maya-style navigation: LMB (or Alt+LMB) orbit, MMB (or Alt+MMB) pan, Alt+RMB zoom-drag."""
        self.grab_focus()
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self._reset_view()
            self.queue_draw()
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if event.button == 2:
            mode = "pan"
        elif event.button == 3 and alt:
            mode = "zoom"
        elif event.button == 1:
            mode = "orbit"
        else:
            return False
        self.drag = (event.x, event.y, mode)
        return True

    def _motion(self, w, event):
        if not self.drag:
            return False
        x0, y0, mode = self.drag
        dx, dy = event.x - x0, event.y - y0
        if mode == "orbit":
            self.yaw += dx * 0.01
            self.pitch = max(-1.55, min(1.55, self.pitch + dy * 0.01))
        elif mode == "pan":
            self.pan[0] += dx
            self.pan[1] += dy
        elif mode == "zoom":
            self.zoom = max(0.2, min(8.0, self.zoom * (1.0 + (dx - dy) * 0.005)))
        self.drag = (event.x, event.y, mode)
        self.queue_draw()
        return True

    def _scroll(self, w, event):
        d = 1.0
        if event.direction == Gdk.ScrollDirection.UP:
            d = 1.15
        elif event.direction == Gdk.ScrollDirection.DOWN:
            d = 1 / 1.15
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            d = 1 / 1.15 if event.delta_y > 0 else 1.15
        self.zoom = max(0.2, min(8.0, self.zoom * d))
        self.queue_draw()
        return True

    def _key(self, w, event):
        if event.keyval in (Gdk.KEY_w, Gdk.KEY_W):
            self.wire = not self.wire
            self.queue_draw()
            return True
        if event.keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.smooth = not self.smooth
            self.queue_draw()
            return True
        return False


class ModelView(ModelNav, Gtk.DrawingArea):
    """Software previewer drawn with Cairo + numpy: z-buffer rasteriser (see _rasterize), Gouraud shading,
    wireframe on W. Used when hardware acceleration is off or PyOpenGL is missing."""

    def __init__(self):
        super().__init__()
        self._nav_init()
        self._worker = None
        self._frame = None            # (w, h, BGRX bytes) last rendered frame
        self._frame_params = None
        self._inflight = False
        self._want = None
        self.on_first_frame = None       # one-shot: the window fades the pane in when the first frame lands
        self._V = self._F = self._N = self._VN = None
        self.connect("draw", self._draw)
        # (the render process is shared and lives for the whole session; nothing to tear down per view)

    def set_model(self, model: Optional[dict], keep_view: bool = False):
        self.model = model
        if not keep_view:
            self._reset_view()
        self._V = self._F = self._N = self._VN = None
        if model and np is not None:
            # numpy copies for the vectorised path: projection, culling, sorting and lighting become array
            # ops, leaving only the Cairo path building in Python
            self._V = np.asarray(model["verts"], dtype=np.float32).reshape(-1, 3)
            self._F = np.asarray(model["faces"], dtype=np.int32).reshape(-1, 3)
            self._N = np.asarray(model["normals"], dtype=np.float32).reshape(-1, 3) if model.get("normals") is not None and len(model["normals"]) else None
            self._VN = np.asarray(model["vnormals"], dtype=np.float32).reshape(-1, 3) if model.get("vnormals") is not None and len(model["vnormals"]) else None
            EX = {k: model[k] for k in ("points", "pcolor", "psize", "vpts", "vden", "vsize") if k in model} or None
            self._worker = get_render_worker()
            self._worker.set_model(self._V, self._F, self._N, self._VN, EX)
        # Keep the previous frame on screen until the new model's first frame lands (model → model would
        # otherwise blank the viewport for the whole load, which reads as a flash before the crossfade).
        self._frame_params = None
        self._want = None
        self._awaiting_first = not keep_view
        if model is None:
            self._frame = None
        self.queue_draw()

    def _draw(self, w, cr):
        """Paint the latest frame from the render worker; if the camera moved, ask for a new one. The GTK
        thread never rasterises: the worker process does, on the non-reserved cores, and posts the pixels back.
        Until the new frame lands the previous one stays on screen, so orbiting is never blocked."""
        a = self.get_allocation()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.04)
        cr.rectangle(0, 0, a.width, a.height)
        cr.fill()
        if not self.model:
            return False
        if self._V is None:
            self._draw_python(cr, a, fg, 1)         # no numpy: synchronous fallback
            return False
        params = self._params(a.width, a.height)
        if self._frame is not None:
            fw, fh, buf = self._frame
            try:
                import cairo
                surf = cairo.ImageSurface.create_for_data(memoryview(buf), cairo.FORMAT_RGB24, fw, fh, fw * 4)
                if (fw, fh) != (a.width, a.height):
                    cr.save()
                    cr.scale(a.width / fw, a.height / fh)
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint()
                    cr.restore()
                else:
                    cr.set_source_surface(surf, 0, 0)
                    cr.paint()
            except Exception as ex:
                bench(f"paint failed: {ex}")
        if params != self._frame_params:
            self._request(params)
        return False

    def _params(self, w: int, h: int):
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        return (w, h, round(self.yaw, 4), round(self.pitch, 4), round(self.zoom, 4),
                round(self.pan[0]), round(self.pan[1]), self.smooth, self.wire,
                self._bg_rgb(fg), (fg.red, fg.green, fg.blue))

    def prime(self, w: int, h: int):
        """Request the first frame before the widget is on screen (a hidden stack child is never drawn, so
        _draw alone would never ask). w/h: the size it will be shown at."""
        if self._V is not None and getattr(self, "_awaiting_first", False) and w > 1 and h > 1:
            self._request(self._params(w, h))

    def _request(self, params):
        """Ask the worker for a frame; while one is in flight just remember the newest wish (coalescing)."""
        if self._inflight:
            self._want = params
            return
        self._want = None
        self._inflight = True
        self._worker.render(params, self._on_frame)

    def _on_frame(self, params, frame):
        self._inflight = False
        first = getattr(self, "_awaiting_first", False) and frame is not None
        if first:
            self._awaiting_first = False
        if frame is not None:
            w, h, buf = frame
            # the pipe delivers immutable bytes; cairo.ImageSurface.create_for_data needs a WRITABLE buffer
            # (it raised TypeError and the viewport stayed blank)
            self._frame = (w, h, bytearray(buf))
            self._frame_params = params
        if first and self.on_first_frame:
            cb, self.on_first_frame = self.on_first_frame, None
            cb()
        if self._want is not None and self._want != params:
            self._request(self._want)
        self.queue_draw()

    def _camera(self, a):
        cy_, sy_ = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        s = min(a.width, a.height) * 0.8 * self.zoom
        ox, oy = a.width / 2 + self.pan[0], a.height / 2 + self.pan[1]
        return cy_, sy_, cp, sp, s * 1.6, ox, oy

    def _bg_rgb(self, fg):
        """The viewport background as 8-bit RGB: the theme's window colour with the 4 % fg tint we paint over it."""
        try:
            ok, bgc = self.get_style_context().lookup_color("theme_bg_color")
            if not ok:
                raise ValueError
            r, g, b = bgc.red, bgc.green, bgc.blue
        except Exception:
            r, g, b = (0.13, 0.13, 0.13) if fg.red > 0.5 else (0.98, 0.98, 0.98)
        mix = lambda c, f: int(round((c * 0.96 + f * 0.04) * 255))
        return mix(r, fg.red), mix(g, fg.green), mix(b, fg.blue)

    def _draw_python(self, cr, a, fg, step):
        """Pure-Python fallback (no numpy): same picture, ~20× slower on big meshes."""
        m = self.model
        cy_, sy_, cp, sp, k16, ox, oy = self._camera(a)
        faces, normals = m["faces"], m.get("normals")
        proj = [(ox + (x * cy_ + z * sy_) * k16 / (2.6 - (y * sp + (-x * sy_ + z * cy_) * cp)),
                 oy - (y * cp - (-x * sy_ + z * cy_) * sp) * k16 / (2.6 - (y * sp + (-x * sy_ + z * cy_) * cp)),
                 y * sp + (-x * sy_ + z * cy_) * cp) for x, y, z in m["verts"]]
        if self.wire:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.set_line_width(0.6)
            for fi in range(0, len(faces), step):
                i, j, k = faces[fi]
                p, q, r = proj[i], proj[j], proj[k]
                cr.move_to(p[0], p[1])
                cr.line_to(q[0], q[1])
                cr.line_to(r[0], r[1])
                cr.close_path()
            cr.stroke()
            return
        idx = range(0, len(faces), step)
        order = sorted(idx, key=lambda fi: proj[faces[fi][0]][2] + proj[faces[fi][1]][2] + proj[faces[fi][2]][2])
        lx, ly, lz = 0.4, 0.7, 0.6
        for fi in order:
            i, j, k = faces[fi]
            p, q, r = proj[i], proj[j], proj[k]
            if (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]) > 0:
                continue                                   # back face
            nx, ny, nz = normals[fi] if normals else (0.0, 0.0, 1.0)
            nx1, nz1 = nx * cy_ + nz * sy_, -nx * sy_ + nz * cy_
            ny2, nz2 = ny * cp - nz1 * sp, ny * sp + nz1 * cp
            d = nx1 * lx + ny2 * ly + nz2 * lz
            shade = 0.35 + 0.65 * (d if d > 0 else 0.0)
            cr.set_source_rgb(0.42 * shade + 0.1, 0.55 * shade + 0.1, 0.72 * shade + 0.12)
            cr.move_to(p[0], p[1])
            cr.line_to(q[0], q[1])
            cr.line_to(r[0], r[1])
            cr.close_path()
            cr.fill()


# --------------------------------------------------------------------------- OpenGL previewer (optional PyOpenGL)
try:
    from OpenGL import GL as _GL          # type: ignore
    from OpenGL.GL import shaders as _GLshaders   # type: ignore
    HAVE_GL = True
except Exception:                          # ImportError, or a broken libGL
    _GL = None
    HAVE_GL = False

# Context flavours tried in order; the shader header is picked to match (see _gl_header).
GL_ATTEMPTS = [("core 3.2", False, (3, 2)), ("core 3.0", False, (3, 0)), ("GLES 3.0", True, (3, 0)), ("legacy/default", False, None)]


def gl_try_contexts(gdk_window, log: Optional[list] = None):
    """Create a GdkGLContext on gdk_window, trying each flavour in GL_ATTEMPTS. Returns (ctx, label) or
    (None, None); each attempt's outcome is appended to log when given. Shared by GLModelView and
    `kestrel.py --probe-gl`, so the probe tests exactly what the viewer does."""
    for label, use_es, ver in GL_ATTEMPTS:
        try:
            ctx = gdk_window.create_gl_context()
            ctx.set_use_es(1 if use_es else 0)
            if ver:
                ctx.set_required_version(*ver)
            ctx.realize()
            if log is not None:
                v = ctx.get_version()
                log.append(f"{label}: OK (got {v[0]}.{v[1]}{' ES' if ctx.get_use_es() else ''})")
            return ctx, label
        except Exception as ex:
            if log is not None:
                log.append(f"{label}: {getattr(ex, 'message', ex)}")
    return None, None


def _gl_header(ctx) -> str:
    if ctx.get_use_es():
        return "#version 300 es\nprecision highp float;\n"
    v = ctx.get_version()
    return "#version 150 core\n" if (v[0], v[1]) >= (3, 2) else "#version 130\n"


_GL_VERT = """
in vec3 pos;
in vec3 nrm;
uniform mat4 mvp;
uniform mat4 mv;
uniform mat3 nmat;
out vec3 vN;
out vec3 vP;
void main() {
    gl_Position = mvp * vec4(pos, 1.0);
    vN = nmat * nrm;
    vP = (mv * vec4(pos, 1.0)).xyz;
}
"""
_GL_FRAG = """
in vec3 vN;
in vec3 vP;
uniform int smoothShade;
uniform int wire;
uniform vec3 wireColor;
out vec4 frag;
void main() {
    if (wire == 1) { frag = vec4(wireColor, 1.0); return; }
    vec3 n = (smoothShade == 1) ? normalize(vN) : normalize(cross(dFdx(vP), dFdy(vP)));
    float d = abs(dot(n, normalize(vec3(0.4, 0.7, 0.6))));     // two-sided
    float sh = 0.35 + 0.65 * min(d, 1.0);
    frag = vec4(vec3(0.08 + 0.90 * sh), 1.0);        // neutral white: tells the GL path from the blue software one
}
"""
# Particles: round sprites shaded as little spheres, sized like the software splats.
_GL_PTS_VERT = """
in vec3 pos;
in vec3 col;
uniform mat4 mvp;
uniform float pxPerUnit;     // screen pixels per model unit at distance 1 (framebuffer pixels)
uniform float psize;         // known point spacing in model units (level-set voxels), 0 = unknown
uniform float defSize;       // sprite size when the spacing is unknown
out vec3 vCol;
out float vCue;
void main() {
    gl_Position = mvp * vec4(pos, 1.0);
    float w = max(gl_Position.w, 1e-4);
    gl_PointSize = psize > 0.0 ? clamp(psize * pxPerUnit / w * 1.15, 1.0, 64.0) : defSize;
    vCue = 0.55 + 0.45 * clamp((2.6 - w + 0.6) / 1.2, 0.0, 1.0);    // nearer = brighter, as in software
    vCol = col;
}
"""
_GL_PTS_FRAG = """
in vec3 vCol;
in float vCue;
uniform float round_;
out vec4 frag;
void main() {
    float lam = 0.75;
    if (round_ > 0.5) {
        vec2 c = gl_PointCoord * 2.0 - 1.0;
        float r2 = dot(c, c);
        if (r2 > 1.0) discard;
        vec3 n = vec3(c.x, -c.y, sqrt(1.0 - r2));
        lam = clamp(dot(n, normalize(vec3(0.4, 0.7, 0.6))), 0.0, 1.0);
    }
    frag = vec4(vCol * vCue * (0.45 + 0.55 * lam), 1.0);
}
"""
# Volume voxels: each is splatted as its exact screen footprint (box filtered against the pixel grid), adding
# density x covered pixel area into a float target. A pixel's total is the density summed along its ray, the
# same quantity the software renderer accumulates, so both paths share the exposure curve.
_GL_VOX_VERT = """
in vec3 pos;
in float den;
uniform mat4 mvp;
uniform float pxPerUnit;
uniform float vsize;
out float vDen;
out float vS;
out float vSprite;
void main() {
    gl_Position = mvp * vec4(pos, 1.0);
    float s = vsize * pxPerUnit / max(gl_Position.w, 1e-4);   // voxel edge in pixels
    vS = s;
    vSprite = ceil(s) + 2.0;
    gl_PointSize = vSprite;
    vDen = den;
}
"""
_GL_VOX_FRAG = """
in float vDen;
in float vS;
in float vSprite;
out vec4 frag;
float cover(float x, float h) { return clamp(min(x + 0.5, h) - max(x - 0.5, -h), 0.0, 1.0); }
void main() {
    vec2 p = (gl_PointCoord - 0.5) * vSprite;                  // this pixel's centre relative to the voxel's
    float h = vS * 0.5;
    frag = vec4(vDen * cover(p.x, h) * cover(p.y, h), 0.0, 0.0, 0.0);
}
"""
# Final pass: opaque scene + smoke from the accumulated densities (1 - exp(-k·sum), auto-exposed).
_GL_COMP_VERT = """
out vec2 uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
_GL_COMP_FRAG = """
in vec2 uv;
uniform sampler2D opaqueTex;
uniform sampler2D accumTex;
uniform float k;
out vec4 frag;
void main() {
    vec3 c = texture(opaqueTex, uv).rgb;
    float a = 1.0 - exp(-k * texture(accumTex, uv).r);
    vec3 smoke = (0.94 - 0.30 * a) * vec3(0.97, 0.98, 1.0);
    frag = vec4(mix(c, smoke, a), 1.0);
}
"""


def gl_matrices(w: int, h: int, yaw: float, pitch: float, zoom: float, pan):
    """Row-major numpy (upload with transpose=True): mvp, mv, normal matrix, and pixels per model unit at
    distance 1 (the software path's k16). Same camera as _project: yaw → pitch → distance 2.6, pan in pixels."""
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    Ry = np.array([[cy_, 0, sy_, 0], [0, 1, 0, 0], [-sy_, 0, cy_, 0], [0, 0, 0, 1]], dtype=np.float64)
    Rx = np.array([[1, 0, 0, 0], [0, cp, -sp, 0], [0, sp, cp, 0], [0, 0, 0, 1]], dtype=np.float64)
    T = np.eye(4)
    T[2, 3] = -2.6
    mv = T @ Rx @ Ry
    s = min(w, h) * 0.8 * zoom * 1.6
    fx, fy = s / (w / 2), s / (h / 2)
    near, far = 0.05, 20.0
    P = np.array([[fx, 0, 0, 0], [0, fy, 0, 0],
                  [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                  [0, 0, -1, 0]], dtype=np.float64)
    Pan = np.eye(4)
    Pan[0, 3] = pan[0] * 2.0 / w
    Pan[1, 3] = -pan[1] * 2.0 / h
    mvp = Pan @ P @ mv
    return mvp.astype(np.float32), mv.astype(np.float32), (Rx @ Ry)[:3, :3].astype(np.float32), s


class GLScene:
    """Everything the OpenGL previewer draws, independent of GTK (so it can be tested on any GL context):
    a mesh (one glDrawElements), particles (sphere-shaded sprites) and volumes. Volumes go through an
    offscreen pass: opaque scene into an RGBA8 texture + depth, voxel densities added into a float texture
    (depth-tested against the opaque scene), then one full-screen pass composites smoke over it.
    Methods must be called with the context current."""

    def __init__(self, header: str):
        self.hdr = header
        self.es = "300 es" in header
        self.mesh_prog = self._program(_GL_VERT, _GL_FRAG)
        self.pts_prog = self.vox_prog = self.comp_prog = None
        self.vao = _GL.glGenVertexArrays(1)
        self.vbo = _GL.glGenBuffers(1)
        self.ebo = _GL.glGenBuffers(1)
        self.pvao = self.pvbo = self.vvao = self.vvbo = self.evao = None
        self.fbo_a = self.fbo_b = self.tex_a = self.tex_b = self.rb = None
        self.fb_size = (0, 0)
        self.nidx = self.npts = self.nvox = 0
        self.psize = 0.0
        self.vsize = 0.0
        self.k = None                       # smoke exposure, measured on the first frame of each model
        self.acc_fmt = ""

    def _program(self, vs: str, fs: str):
        return _GLshaders.compileProgram(_GLshaders.compileShader(self.hdr + vs, _GL.GL_VERTEX_SHADER),
                                         _GLshaders.compileShader(self.hdr + fs, _GL.GL_FRAGMENT_SHADER))

    def _attribs(self, prog, layout):
        """layout: [(name, components)] interleaved float32."""
        import ctypes
        stride = 4 * sum(n for _, n in layout)
        off = 0
        for name, n in layout:
            loc = _GL.glGetAttribLocation(prog, name)
            if loc >= 0:
                _GL.glEnableVertexAttribArray(loc)
                _GL.glVertexAttribPointer(loc, n, _GL.GL_FLOAT, _GL.GL_FALSE, stride, ctypes.c_void_p(off))
            off += 4 * n

    # ---- upload
    def upload(self, m: Optional[dict], keep_exposure: bool = False):
        self.nidx = self.npts = self.nvox = 0
        if not keep_exposure:
            self.k = None
        if not m:
            return
        if len(m.get("faces", ())):
            V = np.asarray(m["verts"], dtype=np.float32).reshape(-1, 3)
            F = np.asarray(m["faces"], dtype=np.uint32).reshape(-1, 3)
            VN = m.get("vnormals")
            if VN is None or not len(VN):
                VN = np.zeros_like(V)
                VN[:, 2] = 1.0
            data = np.ascontiguousarray(np.hstack((V, np.asarray(VN, dtype=np.float32).reshape(-1, 3))))
            _GL.glBindVertexArray(self.vao)
            _GL.glBindBuffer(_GL.GL_ARRAY_BUFFER, self.vbo)
            _GL.glBufferData(_GL.GL_ARRAY_BUFFER, data.nbytes, data, _GL.GL_STATIC_DRAW)
            _GL.glBindBuffer(_GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)
            idx = np.ascontiguousarray(F)
            _GL.glBufferData(_GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, _GL.GL_STATIC_DRAW)
            self._attribs(self.mesh_prog, [("pos", 3), ("nrm", 3)])
            _GL.glBindVertexArray(0)
            self.nidx = int(idx.size)
        P = m.get("points")
        if P is not None and len(P):
            if self.pts_prog is None:
                self.pts_prog = self._program(_GL_PTS_VERT, _GL_PTS_FRAG)
                self.pvao, self.pvbo = _GL.glGenVertexArrays(1), _GL.glGenBuffers(1)
            P = np.asarray(P, dtype=np.float32).reshape(-1, 3)
            C = m.get("pcolor")
            C = (np.asarray(C, dtype=np.float32).reshape(-1, 3) if C is not None and len(C) == len(P)
                 else np.broadcast_to(np.array([0.62, 0.75, 0.92], np.float32), P.shape))
            data = np.ascontiguousarray(np.hstack((P, C)))
            _GL.glBindVertexArray(self.pvao)
            _GL.glBindBuffer(_GL.GL_ARRAY_BUFFER, self.pvbo)
            _GL.glBufferData(_GL.GL_ARRAY_BUFFER, data.nbytes, data, _GL.GL_STATIC_DRAW)
            self._attribs(self.pts_prog, [("pos", 3), ("col", 3)])
            _GL.glBindVertexArray(0)
            self.npts = len(P)
            self.psize = float(m.get("psize") or 0.0)
        VP = m.get("vpts")
        if VP is not None and len(VP):
            if self.vox_prog is None:
                self.vox_prog = self._program(_GL_VOX_VERT, _GL_VOX_FRAG)
                self.comp_prog = self._program(_GL_COMP_VERT, _GL_COMP_FRAG)
                self.vvao, self.vvbo = _GL.glGenVertexArrays(1), _GL.glGenBuffers(1)
                self.evao = _GL.glGenVertexArrays(1)        # empty VAO for the full-screen triangle
            data = np.ascontiguousarray(np.hstack((np.asarray(VP, dtype=np.float32).reshape(-1, 3),
                                                   np.asarray(m["vden"], dtype=np.float32).reshape(-1, 1))))
            _GL.glBindVertexArray(self.vvao)
            _GL.glBindBuffer(_GL.GL_ARRAY_BUFFER, self.vvbo)
            _GL.glBufferData(_GL.GL_ARRAY_BUFFER, data.nbytes, data, _GL.GL_STATIC_DRAW)
            self._attribs(self.vox_prog, [("pos", 3), ("den", 1)])
            _GL.glBindVertexArray(0)
            self.nvox = len(data)
            self.vsize = float(m.get("vsize") or 0.01)

    # ---- offscreen targets for volumes
    def _targets(self, W: int, H: int):
        if self.fb_size == (W, H) and self.fbo_a:
            return
        self._free_targets()
        self.tex_a, self.tex_b = _GL.glGenTextures(1), _GL.glGenTextures(1)
        for t in (self.tex_a, self.tex_b):
            _GL.glBindTexture(_GL.GL_TEXTURE_2D, t)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_MIN_FILTER, _GL.GL_NEAREST)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_MAG_FILTER, _GL.GL_NEAREST)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_WRAP_S, _GL.GL_CLAMP_TO_EDGE)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_WRAP_T, _GL.GL_CLAMP_TO_EDGE)
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, self.tex_a)
        _GL.glTexImage2D(_GL.GL_TEXTURE_2D, 0, _GL.GL_RGBA8, W, H, 0, _GL.GL_RGBA, _GL.GL_UNSIGNED_BYTE, None)
        self.rb = _GL.glGenRenderbuffers(1)
        _GL.glBindRenderbuffer(_GL.GL_RENDERBUFFER, self.rb)
        _GL.glRenderbufferStorage(_GL.GL_RENDERBUFFER, _GL.GL_DEPTH_COMPONENT24, W, H)
        self.fbo_a, self.fbo_b = _GL.glGenFramebuffers(1), _GL.glGenFramebuffers(1)
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, self.fbo_a)
        _GL.glFramebufferTexture2D(_GL.GL_FRAMEBUFFER, _GL.GL_COLOR_ATTACHMENT0, _GL.GL_TEXTURE_2D, self.tex_a, 0)
        _GL.glFramebufferRenderbuffer(_GL.GL_FRAMEBUFFER, _GL.GL_DEPTH_ATTACHMENT, _GL.GL_RENDERBUFFER, self.rb)
        if _GL.glCheckFramebufferStatus(_GL.GL_FRAMEBUFFER) != _GL.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("offscreen colour target not supported")
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, self.fbo_b)
        _GL.glFramebufferRenderbuffer(_GL.GL_FRAMEBUFFER, _GL.GL_DEPTH_ATTACHMENT, _GL.GL_RENDERBUFFER, self.rb)
        for ifmt, typ, label in ((_GL.GL_R32F, _GL.GL_FLOAT, "R32F"), (_GL.GL_R16F, _GL.GL_HALF_FLOAT, "R16F")):
            _GL.glBindTexture(_GL.GL_TEXTURE_2D, self.tex_b)
            _GL.glTexImage2D(_GL.GL_TEXTURE_2D, 0, ifmt, W, H, 0, _GL.GL_RED, typ, None)
            _GL.glFramebufferTexture2D(_GL.GL_FRAMEBUFFER, _GL.GL_COLOR_ATTACHMENT0, _GL.GL_TEXTURE_2D, self.tex_b, 0)
            if _GL.glCheckFramebufferStatus(_GL.GL_FRAMEBUFFER) == _GL.GL_FRAMEBUFFER_COMPLETE:
                self.acc_fmt = label
                break
        else:
            raise RuntimeError("float render targets not supported (volumes need them)")
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, 0)
        self.fb_size = (W, H)

    def _free_targets(self):
        try:
            if self.fbo_a:
                _GL.glDeleteFramebuffers(2, [self.fbo_a, self.fbo_b])
            if self.tex_a:
                _GL.glDeleteTextures(2, [self.tex_a, self.tex_b])
            if self.rb:
                _GL.glDeleteRenderbuffers(1, [self.rb])
        except Exception:
            pass
        self.fbo_a = self.fbo_b = self.tex_a = self.tex_b = self.rb = None
        self.fb_size = (0, 0)

    def free(self):
        self._free_targets()
        try:
            _GL.glDeleteBuffers(1, [self.vbo])
            _GL.glDeleteBuffers(1, [self.ebo])
            for b in (self.pvbo, self.vvbo):
                if b:
                    _GL.glDeleteBuffers(1, [b])
            for a in (self.vao, self.pvao, self.vvao, self.evao):
                if a:
                    _GL.glDeleteVertexArrays(1, [a])
            for p in (self.mesh_prog, self.pts_prog, self.vox_prog, self.comp_prog):
                if p:
                    _GL.glDeleteProgram(p)
        except Exception:
            pass

    # ---- draw
    def _u(self, prog, name):
        return _GL.glGetUniformLocation(prog, name)

    def _draw_opaque(self, mvp, mv, nmat, ppu, smooth, wire, fg):
        _GL.glEnable(_GL.GL_DEPTH_TEST)
        _GL.glDepthMask(_GL.GL_TRUE)
        _GL.glDisable(_GL.GL_CULL_FACE)
        _GL.glDisable(_GL.GL_BLEND)
        if self.nidx:
            p = self.mesh_prog
            _GL.glUseProgram(p)
            _GL.glUniformMatrix4fv(self._u(p, "mvp"), 1, _GL.GL_TRUE, mvp)
            _GL.glUniformMatrix4fv(self._u(p, "mv"), 1, _GL.GL_TRUE, mv)
            _GL.glUniformMatrix3fv(self._u(p, "nmat"), 1, _GL.GL_TRUE, nmat)
            _GL.glUniform1i(self._u(p, "smoothShade"), 1 if smooth else 0)
            _GL.glUniform1i(self._u(p, "wire"), 1 if wire else 0)
            _GL.glUniform3f(self._u(p, "wireColor"), *fg)
            if not self.es:
                _GL.glPolygonMode(_GL.GL_FRONT_AND_BACK, _GL.GL_LINE if wire else _GL.GL_FILL)
            _GL.glBindVertexArray(self.vao)
            _GL.glDrawElements(_GL.GL_TRIANGLES, self.nidx, _GL.GL_UNSIGNED_INT, None)
            if not self.es:
                _GL.glPolygonMode(_GL.GL_FRONT_AND_BACK, _GL.GL_FILL)
        if self.npts:
            if not self.es:
                _GL.glEnable(_GL.GL_PROGRAM_POINT_SIZE)
            p = self.pts_prog
            _GL.glUseProgram(p)
            _GL.glUniformMatrix4fv(self._u(p, "mvp"), 1, _GL.GL_TRUE, mvp)
            _GL.glUniform1f(self._u(p, "pxPerUnit"), ppu)
            _GL.glUniform1f(self._u(p, "psize"), self.psize)
            _GL.glUniform1f(self._u(p, "defSize"), 1.5 if self.npts > 300_000 else 2.5)
            _GL.glUniform1f(self._u(p, "round_"), 1.0 if self.psize or self.npts <= 300_000 else 0.0)
            _GL.glBindVertexArray(self.pvao)
            _GL.glDrawArrays(_GL.GL_POINTS, 0, self.npts)
        _GL.glBindVertexArray(0)
        _GL.glUseProgram(0)

    def render(self, W: int, H: int, w: int, h: int, cam, bg, fg, smooth: bool, wire: bool, target_fb: int):
        """W×H framebuffer pixels (w×h logical). cam = (yaw, pitch, zoom, pan). target_fb = the framebuffer
        to finish in (GtkGLArea's own one is bound when the render signal fires, and is not 0)."""
        mvp, mv, nmat, k16 = gl_matrices(w, h, *cam)
        ppu = k16 * (W / float(w))                            # framebuffer pixels per unit at distance 1
        if not self.nvox:
            _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, target_fb)
            _GL.glViewport(0, 0, W, H)
            _GL.glClearColor(bg[0], bg[1], bg[2], 1.0)
            _GL.glClear(_GL.GL_COLOR_BUFFER_BIT | _GL.GL_DEPTH_BUFFER_BIT)
            self._draw_opaque(mvp, mv, nmat, ppu, smooth, wire, fg)
            return
        self._targets(W, H)
        _GL.glViewport(0, 0, W, H)
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, self.fbo_a)
        _GL.glClearColor(bg[0], bg[1], bg[2], 1.0)
        _GL.glClear(_GL.GL_COLOR_BUFFER_BIT | _GL.GL_DEPTH_BUFFER_BIT)
        self._draw_opaque(mvp, mv, nmat, ppu, smooth, wire, fg)
        # densities in front of the opaque scene, summed per pixel
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, self.fbo_b)
        _GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        _GL.glClear(_GL.GL_COLOR_BUFFER_BIT)
        _GL.glEnable(_GL.GL_DEPTH_TEST)
        _GL.glDepthMask(_GL.GL_FALSE)
        _GL.glEnable(_GL.GL_BLEND)
        _GL.glBlendFunc(_GL.GL_ONE, _GL.GL_ONE)
        if not self.es:
            _GL.glEnable(_GL.GL_PROGRAM_POINT_SIZE)
        p = self.vox_prog
        _GL.glUseProgram(p)
        _GL.glUniformMatrix4fv(self._u(p, "mvp"), 1, _GL.GL_TRUE, mvp)
        _GL.glUniform1f(self._u(p, "pxPerUnit"), ppu)
        _GL.glUniform1f(self._u(p, "vsize"), self.vsize)
        _GL.glBindVertexArray(self.vvao)
        _GL.glDrawArrays(_GL.GL_POINTS, 0, self.nvox)
        _GL.glDisable(_GL.GL_BLEND)
        _GL.glDepthMask(_GL.GL_TRUE)
        if self.k is None:
            self.k = self._exposure(W, H)
        # composite into the widget's framebuffer
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, target_fb)
        _GL.glViewport(0, 0, W, H)
        _GL.glDisable(_GL.GL_DEPTH_TEST)
        p = self.comp_prog
        _GL.glUseProgram(p)
        _GL.glActiveTexture(_GL.GL_TEXTURE0)
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, self.tex_a)
        _GL.glActiveTexture(_GL.GL_TEXTURE1)
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, self.tex_b)
        _GL.glUniform1i(self._u(p, "opaqueTex"), 0)
        _GL.glUniform1i(self._u(p, "accumTex"), 1)
        _GL.glUniform1f(self._u(p, "k"), self.k)
        _GL.glBindVertexArray(self.evao)
        _GL.glDrawArrays(_GL.GL_TRIANGLES, 0, 3)
        _GL.glBindVertexArray(0)
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, 0)
        _GL.glActiveTexture(_GL.GL_TEXTURE0)
        _GL.glBindTexture(_GL.GL_TEXTURE_2D, 0)
        _GL.glUseProgram(0)

    def _exposure(self, W: int, H: int) -> float:
        """Same auto-exposure as the software renderer (97th percentile of the summed densities), measured once
        per model from the first frame so orbiting doesn't pump the brightness."""
        try:
            _GL.glReadBuffer(_GL.GL_COLOR_ATTACHMENT0)
            raw = _GL.glReadPixels(0, 0, W, H, _GL.GL_RED, _GL.GL_FLOAT)
            acc = np.asarray(raw, dtype=np.float32).reshape(-1)
            nz = acc[acc > 0]
            ref = float(np.percentile(nz, 97)) if len(nz) else 1.0
        except Exception:
            ref = 8.0                                         # GLES drivers may refuse float readback
        return 2.5 / max(ref, 1e-6)


class GLModelView(ModelNav, Gtk.GLArea):
    """Hardware-accelerated previewer: geometry is uploaded to the GPU once and every frame is a few draw
    calls, so a 5M-triangle mesh or a million-voxel volume orbits at the display's refresh rate. Same camera
    maths as the software path (yaw → pitch → distance 2.6, pan in pixels), same shading, same keys.
    Drawing lives in GLScene."""

    def __init__(self):
        super().__init__()
        self._nav_init()
        self.set_has_depth_buffer(True)
        self.set_auto_render(True)
        self.gl_flavour = ""
        self.connect("create-context", self._create_context)
        self.scene: Optional[GLScene] = None
        self._dirty = False
        self.gl_info = ""
        self.gl_error: Optional[str] = None
        self.on_error = None                  # callable(msg) set by the window: shown in the UI, triggers fallback
        self.on_cache_failed = None           # callable(model, msg): particles/volumes failed, use software for them
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    def _fail(self, where: str, ex) -> None:
        """Record the first OpenGL failure (later ones are noise), log it, and tell the window."""
        if self.gl_error:
            return
        msg = f"{where}: {ex}".replace("\n", " ")[:400]
        self.gl_error = msg
        GL_STATUS[:] = [f"OpenGL FAILED ({self.gl_info or 'no context'}): {msg}"]
        print("kestrel: OpenGL preview failed —", msg, file=sys.stderr)
        if self.on_error:
            GLib.idle_add(lambda: (self.on_error(msg), False)[1])

    def _create_context(self, area):
        """GLArea asks us for a context: negotiate the best flavour the driver/compositor will give us."""
        log: list = []
        ctx, label = gl_try_contexts(self.get_window(), log)
        if ctx is None:
            self._fail("context", " | ".join(log) + "   (run: python3 kestrel.py --probe-gl)")
            return None
        self.gl_flavour = label
        return ctx

    # ---- lifecycle
    def _on_realize(self, w):
        try:
            self.make_current()
            err = self.get_error()
            if err is not None:
                if not self.gl_error:
                    self._fail("context", err.message if hasattr(err, "message") else err)
                return
            ctx = self.get_context()
            ver = ctx.get_version() if ctx else (0, 0)
            self.gl_info = f"GL {ver[0]}.{ver[1]}" + (" ES" if ctx and ctx.get_use_es() else "")
            try:
                self.gl_info += " — " + _GL.glGetString(_GL.GL_RENDERER).decode(errors="replace")
            except Exception:
                pass
            self.gl_info += f" [{self.gl_flavour}]"
            self.scene = GLScene(_gl_header(ctx))
            self._dirty = self.model is not None
            GL_STATUS[:] = [f"OpenGL OK: {self.gl_info}"]
        except Exception as ex:
            self._fail("init", ex)

    def _on_unrealize(self, w):
        self.make_current()
        if self.scene:
            self.scene.free()
        self.scene = None

    def set_model(self, model: Optional[dict], keep_view: bool = False):
        """keep_view: next frame of a sequence — same camera, same smoke exposure (no brightness pumping)."""
        self.model = model
        if not keep_view:
            self._reset_view()
        self._dirty = "keep" if keep_view else True
        self.queue_render()

    def queue_draw(self):             # ModelNav calls queue_draw(); GLArea wants queue_render()
        self.queue_render()

    def _cache_failed(self, ex):
        """Particles / volumes need features this driver lacks: hand this model to the software renderer
        (meshes keep using OpenGL)."""
        m = self.model
        msg = str(ex).replace("\n", " ")[:300]
        print("kestrel: OpenGL can't draw this cache, using the software renderer —", msg, file=sys.stderr)
        self.model = None
        self._dirty = True
        if self.on_cache_failed and m is not None:
            GLib.idle_add(lambda: (self.on_cache_failed(m, msg), False)[1])

    # ---- render
    def _on_render(self, area, ctx):
        try:
            self._render()
        except Exception as ex:
            if self.model is not None and any(k in self.model for k in ("points", "vpts")):
                self._cache_failed(ex)
            else:
                self._fail("render", ex)
        return True

    def _render(self):
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        try:
            ok, bgc = sc.lookup_color("theme_bg_color")
            r, g, b = (bgc.red, bgc.green, bgc.blue) if ok else ((0.13,) * 3 if fg.red > 0.5 else (0.98,) * 3)
        except Exception:
            r, g, b = (0.13,) * 3
        bg = (r * 0.96 + fg.red * 0.04, g * 0.96 + fg.green * 0.04, b * 0.96 + fg.blue * 0.04)
        target = int(np.asarray(_GL.glGetIntegerv(_GL.GL_DRAW_FRAMEBUFFER_BINDING)).reshape(-1)[0])
        if self.gl_error or not self.scene:
            _GL.glClearColor(bg[0], bg[1], bg[2], 1.0)
            _GL.glClear(_GL.GL_COLOR_BUFFER_BIT | _GL.GL_DEPTH_BUFFER_BIT)
            return
        if self._dirty:
            keep = self._dirty == "keep"
            self._dirty = False
            self.scene.upload(self.model, keep_exposure=keep)
        scale = self.get_scale_factor()
        w, h = max(1, self.get_allocated_width()), max(1, self.get_allocated_height())
        self.scene.render(w * scale, h * scale, w, h, (self.yaw, self.pitch, self.zoom, self.pan), bg,
                          (fg.red, fg.green, fg.blue), self.smooth, self.wire, target)


# --------------------------------------------------------------------------- video / audio playback (GStreamer)
GST_ERROR = ""
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore
    Gst.init(None)
    HAVE_GST = True
except Exception as _ex:                 # typelib missing (gstreamer1 / python3-gobject), or a broken plugin registry
    Gst = None
    HAVE_GST = False
    GST_ERROR = f"{_ex.__class__.__name__}: {_ex}"


def gst_sink_available(refresh: bool = False) -> bool:
    """gtksink lives in gstreamer1-plugins-good-gtk on Rocky; without it we can't embed video in the pane.
    refresh=True re-scans the plugin registry so a package installed while Kestrel runs is picked up."""
    if not HAVE_GST:
        return False
    if refresh:
        try:
            Gst.update_registry()
        except Exception:
            pass
    return Gst.ElementFactory.find("gtksink") is not None


H264_DECODERS = ("avdec_h264", "openh264dec", "nvh264dec", "nvh264sldec", "vah264dec", "vaapih264dec", "v4l2h264dec", "d3d11h264dec")


def gst_decoder(*names) -> Optional[str]:
    if not HAVE_GST:
        return None
    for n in names:
        if Gst.ElementFactory.find(n) is not None:
            return n
    return None


def gst_h264_status() -> Tuple[Optional[str], str]:
    """(decoder element name or None, explanation). Looks at every H.264 decoder GStreamer could offer
    (software libav/openh264, NVIDIA, VA-API, V4L2) and, when none is registered, says whether the libav
    plugin is installed-but-blacklisted (the usual cause: a libav plugin built against a different ffmpeg)."""
    if not HAVE_GST:
        return None, "GStreamer not importable"
    dec = gst_decoder(*H264_DECODERS)
    if dec:
        return dec, ""
    reg = Gst.Registry.get()
    pl = reg.find_plugin("libav")
    if pl is None:
        return None, "no libav plugin in the registry"
    # the plugin exists: does it provide any element?
    feats = [f.get_name() for f in reg.get_feature_list_by_plugin("libav")]
    if not feats:
        return None, (f"libav plugin found at {pl.get_filename()} but it loaded no elements — "
                      "it is probably blacklisted for an ffmpeg library mismatch: run  gst-inspect-1.0 libav  in a terminal to see the error, "
                      "then  rm -rf ~/.cache/gstreamer-1.0  and restart")
    return None, f"libav plugin loaded {len(feats)} elements but no H.264 decoder among them"


class VideoPreview(Gtk.Box):
    """In-pane player for the preview area: playbin → gtksink widget, with play/pause, a seek bar, time
    readout and mute. Opens paused on the first frame (no autoplay noise), Space toggles playback.
    Codec coverage is whatever GStreamer has: the free EPEL plugins do VP8/VP9/AV1/Theora/MJPEG; H.264/H.265
    and ProRes need gstreamer1-plugins-ugly + gstreamer1-libav from RPM Fusion (see Dependencies)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.player = Gst.ElementFactory.make("playbin", "kestrel-player")
        sink = Gst.ElementFactory.make("gtksink", None)
        self.player.set_property("video-sink", sink)
        self.sink_widget = sink.get_property("widget")
        self.sink_widget.set_size_request(-1, 160)
        # The sink widget keeps showing the LAST frame it ever drew — through a stop, and even on an audio-only
        # file where it never draws a new one. So it lives in a stack behind a blank and an audio card, and is
        # only shown once the new file has prerolled a video frame.
        self.screen = Gtk.Stack()
        self.screen.set_homogeneous(False)
        blank = Gtk.DrawingArea()
        blank.connect("draw", lambda w, cr: (cr.set_source_rgb(0, 0, 0), cr.paint(), False)[2])
        self.screen.add_named(blank, "blank")
        self.screen.add_named(self.sink_widget, "video")
        audio = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        audio.pack_start(Gtk.Image.new_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.DIALOG), False, False, 0)
        self.audio_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.audio_label.get_style_context().add_class("dim-label")
        audio.pack_start(self.audio_label, False, False, 0)
        self.screen.add_named(audio, "audio")
        self.screen.set_visible_child_name("blank")
        # The sink widget's NATURAL size is the video's pixel size (1920×1080 for HD). Let that propagate and
        # GTK keeps re-requesting the toplevel at that width — the window twitched sideways every frame. A
        # scrolled window with external policies swallows the natural size; the screen fills whatever the
        # pane gives it and the sink scales the picture inside.
        holder = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        holder.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.EXTERNAL)
        holder.add(self.screen)
        self.pack_start(holder, True, True, 0)
        bar = Gtk.Box(spacing=4)
        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self.play_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.play_btn.connect("clicked", lambda b: self.toggle())
        bar.pack_start(self.play_btn, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self._seeking = False
        self.scale.connect("button-press-event", lambda w, e: setattr(self, "_seeking", True) and False)
        self.scale.connect("button-release-event", self._seek_release)
        self.scale.connect("change-value", self._seek_change)
        bar.pack_start(self.scale, True, True, 0)
        self.time_lab = Gtk.Label(label="0:00 / 0:00")
        self.time_lab.get_style_context().add_class("dim-label")
        bar.pack_start(self.time_lab, False, False, 4)
        self.mute_btn = Gtk.ToggleButton()
        self.mute_btn.set_image(Gtk.Image.new_from_icon_name("audio-volume-high-symbolic", Gtk.IconSize.BUTTON))
        self.mute_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mute_btn.set_tooltip_text("Mute")
        self.mute_btn.connect("toggled", self._mute)
        bar.pack_start(self.mute_btn, False, False, 0)
        self.pack_start(clip_width(bar), False, False, 0)
        self.err_lab = Gtk.Label(xalign=0, wrap=True)
        self.err_lab.get_style_context().add_class("dim-label")
        self.pack_start(self.err_lab, False, False, 0)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", lambda b, m: self._on_eos())
        bus.connect("message::state-changed", self._on_state)
        self._timer = None
        self.playing = False
        self.on_ready = None             # called once the first frame is prerolled (window fades the pane in then)
        self._ready_sent = False
        # All pipeline state changes go through one queue thread, in order. set_state() can block for
        # hundreds of ms (decoder teardown), so it must not run on the GTK thread — but two free threads
        # (stop() then load()) could reorder and leave a fresh load stuck at NULL.
        import queue
        self._ops: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._op_loop, daemon=True, name="kestrel-video-state").start()
        self.set_can_focus(True)
        self.connect("key-press-event", self._key)

    def _op_loop(self):
        while True:
            fn = self._ops.get()
            try:
                fn()
            except Exception as ex:
                print("kestrel: video state change failed —", ex, file=sys.stderr)

    # ---- control
    def load(self, path: str):
        self.playing = False
        self.err_lab.set_text("")
        self._ready_sent = False
        self.screen.set_visible_child_name("blank")          # never show the previous file's frame
        self.audio_label.set_text(os.path.basename(path))
        uri = GLib.filename_to_uri(path, None)
        player = self.player

        def do_load():
            player.set_state(Gst.State.NULL)
            player.set_property("uri", uri)
            player.set_state(Gst.State.PAUSED)          # preroll: first frame, no sound
        self._ops.put(do_load)
        self._set_icon(False)
        if self._timer is None:
            self._timer = GLib.timeout_add(250, self._tick)

    def stop(self):
        player = self.player
        self._ops.put(lambda: player.set_state(Gst.State.NULL))
        self.screen.set_visible_child_name("blank")
        self.playing = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self._set_icon(False)

    def toggle(self):
        self.playing = not self.playing
        st = Gst.State.PLAYING if self.playing else Gst.State.PAUSED
        player = self.player
        self._ops.put(lambda: player.set_state(st))
        self._set_icon(self.playing)

    def _set_icon(self, playing: bool):
        self.play_btn.set_image(Gtk.Image.new_from_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic", Gtk.IconSize.BUTTON))

    def _mute(self, b):
        player, on = self.player, b.get_active()
        self._ops.put(lambda: player.set_property("mute", on))
        b.set_image(Gtk.Image.new_from_icon_name("audio-volume-muted-symbolic" if b.get_active() else "audio-volume-high-symbolic", Gtk.IconSize.BUTTON))

    def _key(self, w, event):
        if event.keyval == Gdk.KEY_space:
            self.toggle()
            return True
        return False

    # ---- seeking / clock
    # Every GStreamer call that can block — queries and seeks included, not just state changes — goes
    # through the op thread. query_duration() on a file that is still prerolling (a movie on a share)
    # blocks until the demuxer has the header; called from the GTK thread it froze the whole window.
    def _seek_change(self, scale, scroll, value):
        player = self.player
        v = max(0.0, min(value, 1000.0)) / 1000.0

        def do_seek():
            ok, dur = player.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(v * dur))
        self._ops.put(do_seek)
        return False

    def _seek_release(self, w, e):
        self._seeking = False
        return False

    @staticmethod
    def _fmt(ns: int) -> str:
        s = max(0, int(ns // Gst.SECOND))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _tick(self):
        """GTK-thread timer: only ENQUEUES a poll; the op thread talks to GStreamer and posts results back."""
        if getattr(self, "_poll_pending", False):
            return True
        self._poll_pending = True
        player = self.player

        def poll():
            try:
                _, cur, _pend = player.get_state(0)
                ok_p, pos = player.query_position(Gst.Format.TIME)
                ok_d, dur = player.query_duration(Gst.Format.TIME)
                nvid = player.get_property("n-video") if cur in (Gst.State.PAUSED, Gst.State.PLAYING) else -1
            except Exception:
                cur, ok_p, pos, ok_d, dur, nvid = None, False, 0, False, 0, -1
            GLib.idle_add(self._poll_done, cur, ok_p and ok_d and dur > 0, pos, dur, nvid)
        self._ops.put(poll)
        return True

    def _poll_done(self, cur, ok, pos, dur, nvid=-1):
        self._poll_pending = False
        if not self._ready_sent and cur in (Gst.State.PAUSED, Gst.State.PLAYING):
            self._mark_ready(nvid)
        if ok:
            if not self._seeking:
                self.scale.set_value(pos / dur * 1000.0)
            self.time_lab.set_text(f"{self._fmt(pos)} / {self._fmt(dur)}")
        return False

    # ---- bus
    def _mark_ready(self, nvid: int):
        """First usable state for the current file: reveal the video surface (or the audio card) and tell the
        window it can fade the pane in. The ONLY place that sets _ready_sent, so the screen can't stay blank."""
        if self._ready_sent:
            return
        self._ready_sent = True
        self.screen.set_visible_child_name("video" if nvid != 0 else "audio")
        if self.on_ready:
            self.on_ready()

    def _on_error(self, bus, msg):
        if not self._ready_sent:                              # show the pane anyway so the error is visible
            GLib.idle_add(lambda: (self._mark_ready(0), False)[1])
        err, dbg = msg.parse_error()
        text = err.message
        if "decoder" in text.lower() or "plugin" in text.lower() or "codec" in text.lower():
            text += "\nMissing codec."
        self.err_lab.set_text(text)
        self.stop()
        self.screen.set_visible_child_name("audio")      # card with the file name under the error

    def _on_eos(self):
        player = self.player
        self._ops.put(lambda: (player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0), player.set_state(Gst.State.PAUSED)))
        self.playing = False
        self._set_icon(False)

    def _on_state(self, bus, msg):
        if msg.src is self.player:
            old, new, pending = msg.parse_state_changed()
            if new == Gst.State.PAUSED and old == Gst.State.READY:
                self._tick()          # the poll it enqueues decides video-vs-audio and fires on_ready
