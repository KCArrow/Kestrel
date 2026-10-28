

# =========================================================================== scene object & texture previews
# Picking an object in a Blender or Maya breakdown shows its geometry in a small 3D view; picking a texture
# shows the image. Geometry is read straight from the scene file (no Blender / Maya): Blender meshes from the
# Mesh data-block (MVert/MPoly/MLoop up to 3.x, attribute layers from 3.5 on), Maya ASCII meshes from their
# stored .vt/.ed/.fc data, and meshes built only by construction history from the primitive that started it
# (polyCube, polySphere…). Modifiers / deformers / later history steps are not applied.

def scene_tile_path(path: str) -> str:
    """A texture path as written (UDIM / <UVTILE> / frame tokens) → the first file on disk that matches."""
    if not path or os.path.exists(path):
        return path
    tok = re.compile(r"<UDIM>|<udim>|<UVTILE>|<uvtile>|\$\{?UDIM\}?|%\(UDIM\)d|<f\d?>|%0?\d*d|#+|\$F\d?|_MAPID_", re.I)
    if tok.search(path):
        hits = sorted(glob.glob(tok.sub("*", path)))
        if hits:
            return hits[0]
    return path


SCENE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".hdr", ".bmp", ".gif", ".psd", ".tx",
                    ".tex", ".dds", ".webp", ".iff", ".dpx", ".cin", ".pic", ".sgi", ".rgb", ".jp2", ".ktx2", ".ktx")


def _rot_matrix(rx: float, ry: float, rz: float, order: str = "xyz"):
    """Euler angles (radians) → 3×3, applied in `order` (first letter first), column vectors."""
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    R = {"x": np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]),
         "y": np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]),
         "z": np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])}
    return R[order[2]] @ R[order[1]] @ R[order[0]]


def _trs(t, r3, s):
    M = np.eye(4)
    M[:3, :3] = r3 @ np.diag(s)
    M[:3, 3] = t
    return M


def _quat_matrix(w, x, y, z):
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _fan_triangles(offsets, corner_verts):
    """Polygon offsets (n+1) + corner → vertex indices → triangle fans (int32 N×3)."""
    offs = np.asarray(offsets, np.int64)
    cv = np.asarray(corner_verts, np.int64)
    sizes = offs[1:] - offs[:-1]
    ntri = np.maximum(sizes - 2, 0)
    tot = int(ntri.sum())
    if tot == 0:
        return np.zeros((0, 3), np.int32)
    start = np.repeat(offs[:-1], ntri)
    k = np.arange(tot) - np.repeat(np.cumsum(ntri) - ntri, ntri) + 1
    ok = (start + k + 1) < len(cv)
    start, k = start[ok], k[ok]
    return np.stack([cv[start], cv[start + k], cv[start + k + 1]], 1).astype(np.int32)


# ---- Blender
_BLEND_ROT_ORDERS = {1: "xyz", 2: "xzy", 3: "yxz", 4: "yzx", 5: "zxy", 6: "zyx"}


def _blend_struct_array(bf, ptr: int, struct_name: str, fields: Tuple[str, ...], owner: int = 0):
    """Rows of a DATA block holding an array of `struct_name`: {field: numpy column} (fixed offsets)."""
    data = bf.read_data(ptr, owner) if ptr else None
    st = bf.structs.get(struct_name)
    size = bf.tlen.get(struct_name, 0)
    if not data or not st or not size:
        return None
    n = len(data) // size
    raw = np.frombuffer(data[:n * size], np.uint8).reshape(n, size)
    out = {}
    for name in fields:
        f = st.get(name)
        if f is None:
            return None
        off, ftype, rawname = f
        dt = {"float": "f4", "int": "i4", "uint": "u4", "short": "i2", "ushort": "u2", "char": "i1", "uchar": "u1",
              "int32_t": "i4", "uint32_t": "u4"}.get(ftype)
        if dt is None:
            return None
        cnt = 1
        for m in re.findall(r"\[(\d+)\]", rawname):
            cnt *= int(m)
        isz = np.dtype(dt).itemsize
        col = np.ascontiguousarray(raw[:, off:off + isz * cnt]).view(bf.e + dt)
        out[name] = col.reshape(n, cnt) if cnt > 1 else col.reshape(n)
    return out


def _blend_cd_layers(bf, me_st: str, me_data: bytes, cd: str, owner: int = 0) -> Dict[str, Tuple[int, int]]:
    """CustomData layers of a Mesh (vdata / ldata / pdata / fdata): name → (type, data pointer)."""
    lp = bf.field(me_st, me_data, f"{cd}.layers")
    if not lp:
        return {}
    data = bf.read_data(lp, owner)
    size = bf.tlen.get("CustomDataLayer", 0)
    if not data or not size:
        return {}
    out = {}
    for i in range(len(data) // size):
        chunk = data[i * size:(i + 1) * size]
        nm = bf.field("CustomDataLayer", chunk, "name") or ""
        typ = bf.field("CustomDataLayer", chunk, "type")
        dp = bf.field("CustomDataLayer", chunk, "data") or 0
        out.setdefault(nm or f"#type{typ}", (typ, dp))
        out.setdefault(f"#type{typ}", (typ, dp))
    return out


def _blend_attr_storage(bf, st: str, d: bytes, owner: int = 0) -> Dict[str, int]:
    """Blender 5.x generic attributes (Mesh.attribute_storage): name → pointer to the raw array."""
    p = bf.field(st, d, "attribute_storage.dna_attributes")
    n = bf.field(st, d, "attribute_storage.dna_attributes_num") or 0
    raw = bf.read_data(p, owner) if p else None
    size = bf.tlen.get("Attribute", 0)
    out: Dict[str, int] = {}
    if not raw or not size:
        return out
    for i in range(min(n, len(raw) // size)):
        c = raw[i * size:(i + 1) * size]
        nm = bf.read_data(bf.field("Attribute", c, "name") or 0, owner) or b""
        nm = nm.split(b"\0")[0].decode("utf-8", "replace")
        dp = bf.field("Attribute", c, "data") or 0
        if bf.field("Attribute", c, "storage_type") == 0:              # AttributeArray {data, sharing, size}
            arr = bf.read_data(dp, owner) if dp else None
            dp = bf.field("AttributeArray", arr, "data") if arr else 0
        else:                                                         # AttributeSingle: one value
            continue
        if nm and dp:
            out[nm] = dp
    return out


def blend_mesh_arrays(bf, me_ptr: int):
    """(V float N×3 in object space, F int M×3) of a Mesh data-block, or None."""
    b = bf.block(me_ptr)
    if b is None or b[3] is None:
        return None
    st, d = bf.sname(b[1]), b[3]
    fnum = lambda *names: next((v for v in (bf.field(st, d, n) for n in names) if v is not None), None)
    nv = fnum("totvert", "verts_num") or 0
    npoly = fnum("totpoly", "faces_num") or 0
    if nv <= 0:
        return None
    vlay = _blend_cd_layers(bf, st, d, "vdata", me_ptr)
    attrs = _blend_attr_storage(bf, st, d, me_ptr)
    for nm in ("position", ".corner_vert"):
        if nm in attrs:
            vlay.setdefault(nm, (None, attrs[nm]))
    V = None
    if "position" in vlay:                                            # 3.5+
        raw = bf.read_data(vlay["position"][1], me_ptr)
        if raw and len(raw) >= nv * 12:
            V = np.frombuffer(raw[:nv * 12], bf.e + "f4").reshape(nv, 3)
    if V is None:
        mv = bf.field(st, d, "mvert")
        arr = _blend_struct_array(bf, mv, "MVert", ("co",), me_ptr) if mv else None
        if arr is None and "#type0" in vlay:                         # CD_MVERT layer
            arr = _blend_struct_array(bf, vlay["#type0"][1], "MVert", ("co",), me_ptr)
        if arr is not None:
            V = arr["co"][:nv].astype(np.float32)
    if V is None:
        return None
    F = None
    if npoly:
        offp = bf.field(st, d, "face_offset_indices") or bf.field(st, d, "poly_offset_indices")
        offs = None
        if offp:
            raw = bf.read_data(offp, me_ptr)
            if raw and len(raw) >= (npoly + 1) * 4:
                offs = np.frombuffer(raw[:(npoly + 1) * 4], bf.e + "i4")
        if offs is None:
            mp = bf.field(st, d, "mpoly")
            pdl = _blend_cd_layers(bf, st, d, "pdata", me_ptr)
            arr = _blend_struct_array(bf, mp, "MPoly", ("loopstart", "totloop"), me_ptr) if mp else None
            if arr is None and "#type25" in pdl:
                arr = _blend_struct_array(bf, pdl["#type25"][1], "MPoly", ("loopstart", "totloop"), me_ptr)
            if arr is not None:
                ls, tl = arr["loopstart"][:npoly].astype(np.int64), arr["totloop"][:npoly].astype(np.int64)
                order = np.argsort(ls, kind="stable")
                if np.all(np.diff(ls[order]) >= 0) and np.array_equal(ls[order][1:], (ls + tl)[order][:-1]):
                    offs = np.concatenate([ls[order], [ls[order][-1] + tl[order][-1]]])
                else:                                                # gaps: emit per polygon
                    cv = _blend_corner_verts(bf, st, d, me_ptr)
                    if cv is not None:
                        tris = [_fan_triangles([s, s + t], cv) for s, t in zip(ls, tl)]
                        F = np.concatenate(tris) if tris else None
        if offs is not None and F is None:
            cv = _blend_corner_verts(bf, st, d, me_ptr)
            if cv is not None:
                F = _fan_triangles(offs, cv)
    if F is None:                                                    # pre-2.63 tessellated faces
        mf = bf.field(st, d, "mface")
        arr = _blend_struct_array(bf, mf, "MFace", ("v1", "v2", "v3", "v4"), me_ptr) if mf else None
        if arr is not None:
            v1, v2, v3, v4 = (arr[k].astype(np.int64) for k in ("v1", "v2", "v3", "v4"))
            quad = v4 != 0
            F = np.concatenate([np.stack([v1, v2, v3], 1), np.stack([v1[quad], v3[quad], v4[quad]], 1)]).astype(np.int32)
    if F is None or not len(F):
        return V, np.zeros((0, 3), np.int32)
    F = F[(F >= 0).all(1) & (F < nv).all(1)]
    return V, F


def _blend_corner_verts(bf, st: str, d: bytes, me_ptr: int = 0):
    nl = next((v for v in (bf.field(st, d, n) for n in ("totloop", "corners_num")) if v is not None), 0)
    ll = _blend_cd_layers(bf, st, d, "ldata", me_ptr)
    attrs = _blend_attr_storage(bf, st, d, me_ptr)
    if ".corner_vert" in attrs:
        ll.setdefault(".corner_vert", (None, attrs[".corner_vert"]))
    if ".corner_vert" in ll:                                          # 3.6+
        raw = bf.read_data(ll[".corner_vert"][1], me_ptr)
        if raw and len(raw) >= nl * 4:
            return np.frombuffer(raw[:nl * 4], bf.e + "i4")
    ml = bf.field(st, d, "mloop")
    arr = _blend_struct_array(bf, ml, "MLoop", ("v",), me_ptr) if ml else None
    if arr is None and "#type26" in ll:
        arr = _blend_struct_array(bf, ll["#type26"][1], "MLoop", ("v",), me_ptr)
    return arr["v"][:nl] if arr is not None else None


def _blend_object_matrix(bf, b) -> "np.ndarray":
    """World matrix of an Object block: the saved one when the file has it, else composed from
    loc / rot / scale (+ deltas) and the parent chain."""
    st, d = bf.sname(b[1]), b[3]
    for nm in ("object_to_world", "obmat"):
        if nm in bf.structs.get(st, {}):
            vals = [bf.field(st, d, nm, index=i) for i in range(16)]
            if all(v is not None for v in vals) and any(abs(v) > 1e-12 for v in vals[:15]):
                return np.array(vals, np.float64).reshape(4, 4).T          # stored row-major [col][row]
    return _blend_compose(bf, b, 0)


def _blend_compose(bf, b, depth: int):
    st, d = bf.sname(b[1]), b[3]
    g = lambda nm, i, dflt: (lambda v: dflt if v is None else v)(bf.field(st, d, nm, index=i))
    loc = np.array([g("loc", i, 0.0) + g("dloc", i, 0.0) for i in range(3)])
    scl = np.array([g("size", i, 1.0) * (g("dscale", i, 1.0) or 1.0) for i in range(3)])
    mode = g("rotmode", 0, 1)
    if mode == 0:
        R = _quat_matrix(*[g("quat", i, 1.0 if i == 0 else 0.0) for i in range(4)])
    elif mode == -1:                                                   # axis-angle
        ax = np.array([g("rotAxis", i, 0.0) for i in range(3)])
        ang = g("rotAngle", 0, 0.0)
        n = np.linalg.norm(ax) or 1.0
        h = ang / 2
        R = _quat_matrix(math.cos(h), *(ax / n * math.sin(h)))
    else:
        R = _rot_matrix(g("rot", 0, 0.0), g("rot", 1, 0.0), g("rot", 2, 0.0), _BLEND_ROT_ORDERS.get(mode, "xyz"))
    M = _trs(loc, R, scl)
    par = bf.field(st, d, "parent") or 0
    pb = bf.block(par) if par and depth < 64 else None
    if pb is not None and pb[3] is not None:
        pinv = [bf.field(st, d, "parentinv", index=i) for i in range(16)]
        P = np.array(pinv, np.float64).reshape(4, 4).T if all(v is not None for v in pinv) else np.eye(4)
        B = np.eye(4)
        if bf.field(st, d, "partype") == 7:                           # PARBONE: parented to a bone's tail
            B = _blend_bone_matrix(bf, pb, bf.field(st, d, "parsubstr") or "")
        M = _blend_object_matrix(bf, pb) @ B @ P @ M
    return M


def _blend_bone_matrix(bf, arm_b, bone_name: str):
    """Armature-space matrix of a bone's tail (posed when the file saved a pose, else rest)."""
    st, d = bf.sname(arm_b[1]), arm_b[3]
    pose = bf.field(st, d, "pose") or 0
    pd = bf.read_data(pose, arm_b[2]) if pose else None
    if not pd:
        return np.eye(4)
    ps = "bPose"
    first = bf.field(ps, pd, "chanbase.first")
    seen = 0
    while first and seen < 5000:
        seen += 1
        cd = bf.read_data(first, arm_b[2])
        if not cd:
            break
        if (bf.field("bPoseChannel", cd, "name") or "") == bone_name:
            M = None
            vals = [bf.field("bPoseChannel", cd, "pose_mat", index=i) for i in range(16)]
            if all(v is not None for v in vals) and any(abs(v) > 1e-12 for v in vals):
                M = np.array(vals, np.float64).reshape(4, 4).T
            length = 0.0
            bp = bf.field("bPoseChannel", cd, "bone") or 0
            bd = bf.read_data(bp, 0) if bp else None
            if bd:
                length = bf.field("Bone", bd, "length") or 0.0
                if M is None:
                    am = [bf.field("Bone", bd, "arm_mat", index=i) for i in range(16)]
                    if all(v is not None for v in am):
                        M = np.array(am, np.float64).reshape(4, 4).T
            if M is None:
                return np.eye(4)
            T = np.eye(4)
            T[1, 3] = length
            return M @ T
        first = bf.field("bPoseChannel", cd, "next") or 0
    return np.eye(4)


def _blend_collection_objects(bf, gr_ptr: int, depth: int = 0) -> list:
    """Objects in a collection (Group before 2.8) and its child collections."""
    g = bf.block(gr_ptr)
    if g is None or g[3] is None or depth > 16:
        return []
    st = bf.sname(g[1])
    out = []
    first = bf.field(st, g[3], "gobject.first")
    for link in (bf.listbase(first) if first else []):
        op = bf.field(bf.sname(link[1]), link[3], "ob")
        if op:
            out.append(op)
    first = bf.field(st, g[3], "children.first")
    for link in (bf.listbase(first) if first else []):
        cp = bf.field(bf.sname(link[1]), link[3], "collection")
        if cp:
            out.extend(_blend_collection_objects(bf, cp, depth + 1))
    return list(dict.fromkeys(out))


def _blend_instance_collection(bf, b) -> int:
    st, d = bf.sname(b[1]), b[3]
    return bf.field(st, d, "instance_collection") or bf.field(st, d, "dup_group") or 0


def _blend_linked_collection(bf, gr_ptr: int, path: str, libs: Dict[str, object]):
    """A collection linked from another .blend: (that file's BlendFile, its collection address) or None."""
    g = bf.block(gr_ptr)
    gd = (g[3] if g is not None and g[3] is not None else bf.read_data(gr_ptr)) if g is not None else None
    if not gd:
        return None
    g = g[:3] + (gd,) + tuple(g[4:])
    gst = bf.sname(g[1])
    placeholder = gst == "ID"                                         # linked IDs are written as bare ID blocks
    lp = (bf.field("ID", g[3], "lib") if placeholder else bf.id_lib(g)) or 0
    lb = bf.block(lp) if lp else None
    if lb is None or lb[3] is None:
        return None
    raw = bf.field(bf.sname(lb[1]), lb[3], "filepath") or bf.field(bf.sname(lb[1]), lb[3], "name") or ""
    full, ok = _blend_path(raw, path)
    if not ok:
        return None
    if full not in libs:
        try:
            libs[full] = BlendFile(full)
        except Exception:
            libs[full] = None
    lbf = libs[full]
    if lbf is None:
        return None
    name = ((bf.field("ID", g[3], "name") or "")[2:]) if placeholder else bf.id_name(g)
    hit = next((x for x in lbf.blocks if x[5] == "GR" and x[3] is not None and lbf.id_name(x) == name and not lbf.id_lib(x)), None)
    return (lbf, full, hit[2]) if hit else None


def blend_scene_objects(bf, sc_ptr: int) -> list:
    """Objects of one scene (2.8+ collections, 2.7x base list), minus the ones hidden in the viewport."""
    sb = bf.block(sc_ptr)
    if sb is None or sb[3] is None:
        return []
    st = bf.sname(sb[1])
    mc = bf.field(st, sb[3], "master_collection")
    if mc:
        members = _blend_collection_objects(bf, mc)
    else:
        members = []
        first = bf.field(st, sb[3], "base.first")
        for link in (bf.listbase(first) if first else []):
            op = bf.field(bf.sname(link[1]), link[3], "object")
            if op:
                members.append(op)
    out = []
    for op in members:
        b = bf.block(op)
        if b is None or b[3] is None:
            continue
        os_ = bf.sname(b[1])
        hidden = (bf.field(os_, b[3], "visibility_flag") or 0) & 1 or (bf.field(os_, b[3], "restrictflag") or 0) & 1
        if not hidden:
            out.append(op)
    return out


def blend_object_model_arrays(path: str, ob_ptr):
    """Geometry of a Blender object and everything parented under it, in world space, Y-up. Empties that
    instance a collection show the collection's objects at the empty (nested instances, and collections
    linked from other .blend files, too)."""
    bf0 = _cached_scene(path, BlendFile, "blend")
    files = {path: bf0}
    obs_of: Dict[str, dict] = {}

    def obs_in(fpath):
        if fpath not in obs_of:
            f = files[fpath]
            obs_of[fpath] = {b[2]: b for b in f.blocks if b[5] == "OB" and b[3] is not None}
        return obs_of[fpath]
    obs = obs_in(path)
    if isinstance(ob_ptr, str) and ob_ptr.startswith("scene="):       # a whole scene
        seen = [p for p in blend_scene_objects(bf0, int(ob_ptr[6:])) if p in obs]
        if not seen:
            raise ValueError("this scene has no visible objects")
        ob_ptr = seen[0]
    else:
        ob_ptr = int(ob_ptr)
        if ob_ptr not in obs:
            raise ValueError("object not found in the file")
        kids: Dict[int, list] = {}
        for p, b in obs.items():
            par = bf0.field(bf0.sname(b[1]), b[3], "parent") or 0
            if par in obs:
                kids.setdefault(par, []).append(p)
        todo, seen = [ob_ptr], []
        while todo and len(seen) < 5000:
            p = todo.pop()
            if p in seen:
                continue
            seen.append(p)
            todo.extend(kids.get(p, []))
    Vs, Fs, kinds = [], [], []
    n_off, skipped, inst, missing_libs = 0, 0, 0, 0
    mesh_cache: Dict[Tuple[str, int], object] = {}

    def add(fpath, p, M_extra, depth):
        nonlocal n_off, skipped, inst, missing_libs
        bf = files[fpath]
        b = obs_in(fpath).get(p)
        if b is None or depth > 6 or len(Vs) > 20000:
            return
        st, d = bf.sname(b[1]), b[3]
        M = M_extra @ _blend_object_matrix(bf, b)
        if bf.field(st, d, "type") == 1:                              # OB_MESH
            me = bf.field(st, d, "data") or 0
            if (fpath, me) not in mesh_cache:
                mesh_cache[(fpath, me)] = blend_mesh_arrays(bf, me)
            arr = mesh_cache[(fpath, me)]
            if arr is None:
                skipped += 1
            else:
                V, F = arr
                Vs.append((np.c_[V.astype(np.float64), np.ones(len(V))] @ M.T)[:, :3])
                Fs.append(F.astype(np.int64) + n_off)
                n_off += len(V)
                kinds.append(bf.id_name(b))
        gr = _blend_instance_collection(bf, b)
        if not gr:
            return
        inst += 1
        gfile, gptr = fpath, gr
        members = _blend_collection_objects(bf, gr)
        if not members:                                               # linked from a library file?
            linked = _blend_linked_collection(bf, gr, fpath, {k: v for k, v in files.items()})
            if linked is None:
                missing_libs += 1
                return
            lbf, lpath, lptr = linked
            files[lpath] = lbf
            gfile, gptr = lpath, lptr
            members = _blend_collection_objects(lbf, lptr)
        gbf = files[gfile]
        g = gbf.block(gptr)
        off = [0.0, 0.0, 0.0]
        if g is not None and g[3] is not None:
            gs = gbf.sname(g[1])
            off = [gbf.field(gs, g[3], "instance_offset", index=i) or gbf.field(gs, g[3], "dupli_ofs", index=i) or 0.0 for i in range(3)]
        T = np.eye(4)
        T[:3, 3] = -np.array(off, np.float64)
        for q in members:
            add(gfile, q, M @ T, depth + 1)

    for p in seen:
        add(path, p, np.eye(4), 0)
    if not Vs:
        t = bf0.field(bf0.sname(obs[ob_ptr][1]), obs[ob_ptr][3], "type")
        if missing_libs:
            raise ValueError("this instances a collection from a linked .blend that isn't on this machine")
        raise ValueError(f"no mesh geometry here ({_BLEND_OB_TYPES.get(t, 'object')})" if not skipped else
                         "the mesh data couldn't be read from this file")
    V = np.concatenate(Vs)
    V = np.stack([V[:, 0], V[:, 2], -V[:, 1]], 1)                     # Blender Z-up → viewer Y-up
    notes = ["Base mesh: modifiers (subdivision, mirror, arrays…) aren't applied."]
    if inst:
        notes.append(f"{inst} collection instance{'s' if inst != 1 else ''} shown in place.")
    if missing_libs:
        notes.append(f"{missing_libs} instance{'s' if missing_libs != 1 else ''} of linked collections not found on this machine.")
    return V.astype(np.float32), np.concatenate(Fs).astype(np.int32), kinds, notes


# ---- Maya ASCII
_MAYA_PRIMS = {"polyCube", "polySphere", "polyCylinder", "polyPlane", "polyCone", "polyTorus", "polyPyramid"}
_MAYA_ROT_ORDERS = {0: "xyz", 1: "yzx", 2: "zxy", 3: "xzy", 4: "yxz", 5: "zyx"}
_RE_MA_SETATTR = re.compile(r'setAttr\s+((?:-\w+(?:\s+(?!["-])\S+)?\s+)*)"([^"]+)"\s*(?:-type\s+"(\w+)"\s*)?(.*)', re.S)


def _maya_ascii_sections(path: str):
    """Top-level statements grouped by node, keyed by DAG path ('|grp|pCube1'; history nodes '|polyCube1'):
    {path: {type, name, parent path, body (setAttr text)}}, connections resolved to (src path.attr, dst
    path.attr), and the resolver (duplicate short names are told apart by their parents)."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    nodes: Dict[str, dict] = {}
    conns: list = []
    dag = MayaDagPaths()
    starts = [m.start() for m in re.finditer(r"^(?=[A-Za-z])", text, re.M)]
    starts.append(len(text))
    raw_conns = []
    for a, b in zip(starts, starts[1:]):
        chunk = text[a:b]
        if chunk.startswith("createNode "):
            head = chunk.split(";", 1)[0]
            fl, pos = _mel_flags(head)
            name = fl.get("-n", "")
            full = dag.add(name, fl.get("-p", ""))
            nodes[full] = {"type": pos[0] if pos else "?", "parent": dag.parent_of(full),
                           "body": chunk[len(head) + 1:], "name": name.split("|")[-1]}
        elif chunk.startswith("connectAttr "):
            strs = _RE_MEL_STR.findall(chunk.split(";", 1)[0])
            if len(strs) >= 2:
                raw_conns.append((strs[0], strs[1]))
        elif chunk.startswith("parent ") and " -add" in chunk.split(";", 1)[0]:
            # instancing: parent -s -nc -r -add "|pCube1|pCubeShape1" "pCube2";
            strs = _RE_MEL_STR.findall(chunk.split(";", 1)[0])
            if len(strs) >= 2:
                child, par = dag.resolve(strs[0]), dag.resolve(strs[1])
                if child and par:
                    nodes.setdefault("\0instances", {"type": "", "body": "", "name": "", "parent": "", "list": []})["list"].append((child, par))
    for src, dst in raw_conns:
        sn, _, sa = src.partition(".")
        dn, _, da = dst.partition(".")
        sp, dp = dag.resolve(sn), dag.resolve(dn)
        if sp and dp:
            conns.append((f"{sp}.{sa}", f"{dp}.{da}"))
    return nodes, conns, dag


def _mb_mesh_text(data: bytes) -> str:
    """A Maya binary MESH chunk ('ci' cached mesh) as the equivalent .ma setAttr text (.vt / .ed / .fc), so the
    ASCII code path reads both. Layout (big-endian): float count + xyz floats; edge-index count + vertex pairs;
    face-edge count + entries (bit 31 = edge used reversed, bits 29-30 = last edge of the face)."""
    nul = data.find(b"\0")
    if nul < 0:
        return ""
    b = data[nul + 2:]
    try:
        nfl = struct.unpack_from(">I", b, 0)[0]
        p = 4
        V = np.frombuffer(b, ">f4", nfl, p).reshape(-1, 3)
        p += nfl * 4
        ne = struct.unpack_from(">I", b, p)[0]
        E = np.frombuffer(b, ">u4", ne, p + 4).reshape(-1, 2)
        p += 4 + ne * 4
        nfe = struct.unpack_from(">I", b, p)[0]
        FE = np.frombuffer(b, ">u4", nfe, p + 4).astype(np.int64)
    except (struct.error, ValueError):
        return ""
    if not len(V):
        return ""
    if (len(E) and int(E.max()) >= len(V)) or (len(FE) and int((FE & 0x1FFFFFFF).max()) >= max(len(E), 1)):
        return ""                                                     # not the layout we know: leave it to Maya
    out = [f'setAttr ".vt[0:{len(V) - 1}]" ' + " ".join(f"{x:.7g}" for x in V.ravel())]
    if len(E):
        out.append(f'setAttr ".ed[0:{len(E) - 1}]" ' + " ".join(f"{a} {c} 1" for a, c in E))
    if len(FE):
        idx = FE & 0x1FFFFFFF
        val = np.where((FE >> 31) & 1, -(idx + 1), idx)
        last = np.nonzero((FE >> 29) & 3)[0]
        faces, start = [], 0
        for e in last:
            loop = val[start:e + 1]
            faces.append(f"f {len(loop)} " + " ".join(str(int(x)) for x in loop))
            start = e + 1
        if faces:
            out.append(f'setAttr ".fc[0:{len(faces) - 1}]" -type "polyFaces" ' + " ".join(faces))
    return ";\n".join(out) + ";\n"


def _maya_binary_sections(path: str):
    """Maya binary (.mb) scene in the same shape as _maya_ascii_sections: nodes keyed by DAG path with their
    attributes rendered as setAttr text, resolved connections, the resolver."""
    with open(path, "rb") as f:
        d = f.read()
    w = 8 if d[:4] == b"FOR8" else 4
    nodes: Dict[str, dict] = {}
    dag = MayaDagPaths()
    raw_conns = []
    cur = None
    cur_type = ""
    for kind, tag, *rest in _iff_chunks(d, 0, len(d), w):
        if kind == "group":
            t = tag.decode("latin-1") if all(32 < c < 127 for c in tag) else ""
            cur_type = _MAYA_TYPE_TAGS.get(t, t or "?")
            cur = None
            continue
        data = rest[0]
        t = tag.decode("latin-1")
        if t == "CREA":
            parts = data[1:].split(b"\0")
            name = parts[0].decode("utf-8", "replace") if parts else ""
            parent = ""
            if len(parts) > 1 and parts[1] and all(32 <= c < 127 for c in parts[1]):
                cand = parts[1].decode("ascii")
                if dag.resolve(cand) or cand.startswith("|"):
                    parent = cand
            full = dag.add(name, parent)
            cur = nodes[full] = {"type": cur_type, "parent": dag.parent_of(full), "name": name.split("|")[-1], "parts": []}
            continue
        if t == "SLCT":
            cur = None
            continue
        if t == "CWFL":
            ps = data[1:].split(b"\0")
            if len(ps) >= 2:
                raw_conns.append((ps[0].decode("latin-1").lstrip(":"), ps[1].decode("latin-1").lstrip(":")))
            continue
        if cur is None:
            continue
        if t == "MESH":
            cur["parts"].append(_mb_mesh_text(data))
            continue
        if t in ("DBLE", "DBL2", "DBL3", "DBL4", "FLT2", "FLT3", "FLTE", "FLT4", "FLGS"):
            nul = data.find(b"\0")
            if nul <= 0 or nul > 256:
                continue
            attr = data[:nul].decode("latin-1")
            payload = data[nul + 2:]
            if t == "FLGS":
                if len(payload) != 4 or "[" in attr:
                    continue
                vals = [struct.unpack(">i", payload)[0]]
            else:
                fmt = "f" if t.startswith("FLT") else "d"
                n = len(payload) // (4 if fmt == "f" else 8)
                if not n:
                    continue
                vals = np.frombuffer(payload, ">" + ("f4" if fmt == "f" else "f8"), n)
            cur["parts"].append(f'setAttr ".{attr}" ' + " ".join(f"{float(v):.10g}" for v in vals) + ";\n")
    for nd in nodes.values():
        nd["body"] = "".join(nd.pop("parts"))
    conns = []
    for src, dst in raw_conns:
        sn, _, sa = src.partition(".")
        dn, _, da = dst.partition(".")
        sp, dp = dag.resolve(sn), dag.resolve(dn)
        if sp and dp:
            conns.append((f"{sp}.{sa}", f"{dp}.{da}"))
    return nodes, conns, dag


def _ma_setattrs(body: str):
    """(attr, type, value text) for each setAttr in a node body."""
    for stmt in body.split(";"):
        s = stmt.strip()
        if not s.startswith("setAttr"):
            continue
        m = _RE_MA_SETATTR.match(s)
        if m:
            yield m.group(2), m.group(3) or "", m.group(4)


def _ma_numbers(txt: str):
    vals = txt.replace("yes", "1").replace("no", "0").split()
    try:
        return np.array([float(v) for v in vals], np.float64)
    except ValueError:
        return np.array([float(v) for v in vals if re.match(r"^-?[\d.]+(?:e[-+]?\d+)?$", v, re.I)], np.float64)


def _ma_indexed(attrs, short: str, width: int) -> Optional["np.ndarray"]:
    """Gather ".vt[0:7]" style ranged setAttrs into one array of rows."""
    rows: Dict[int, np.ndarray] = {}
    hi = -1
    for a, typ, val in attrs:
        m = re.match(r"^\." + short + r"\[(\d+)(?::(\d+))?\]$", a)
        if not m:
            continue
        lo_i = int(m.group(1))
        hi_i = int(m.group(2) or m.group(1))
        try:
            nums = np.array(val.split(), np.float64)
        except ValueError:
            nums = _ma_numbers(val)
        n = hi_i - lo_i + 1
        if len(nums) < n * width:
            n = len(nums) // width
        rows[lo_i] = nums[:n * width].reshape(n, width)
        hi = max(hi, lo_i + n - 1)
    if hi < 0:
        return None
    out = np.zeros((hi + 1, width))
    for lo_i, arr in rows.items():
        out[lo_i:lo_i + len(arr)] = arr
    return out


def _ma_faces(attrs, ed) -> Optional[Tuple["np.ndarray", "np.ndarray"]]:
    """polyFaces "f n e…" records → (offsets, corner vertices). Holes ("h") are dropped."""
    loops: list = []
    for a, typ, val in attrs:
        if not re.match(r"^\.fc\[", a):
            continue
        toks = val.split()
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if t == "f" and i + 1 < n:
                k = int(toks[i + 1])
                loops.append([int(x) for x in toks[i + 2:i + 2 + k]])
                i += 2 + k
            elif t in ("h",) and i + 1 < n:
                k = int(toks[i + 1])
                i += 2 + k
            elif t in ("mu", "mf") and i + 2 < n:
                k = int(toks[i + 2])
                i += 3 + k
            elif t == "mc" and i + 2 < n:
                k = int(toks[i + 2])
                i += 3 + k
            elif t == "fc" and i + 1 < n:
                k = int(toks[i + 1])
                i += 2 + k
            else:
                i += 1
    if not loops or ed is None:
        return None
    e0, e1 = ed[:, 0].astype(np.int64), ed[:, 1].astype(np.int64)
    sizes = np.array([len(l) for l in loops], np.int64)
    flat = np.array([x for l in loops for x in l], np.int64)
    neg = flat < 0
    idx = np.where(neg, -flat - 1, flat)
    idx = np.clip(idx, 0, len(e0) - 1)
    cv = np.where(neg, e1[idx], e0[idx])
    offs = np.concatenate([[0], np.cumsum(sizes)])
    return offs, cv


def _ma_attr_vec(attrs, name: str, dflt):
    for a, typ, val in attrs:
        if a == name:
            v = _ma_numbers(val)
            if len(v) >= len(dflt):
                return v[:len(dflt)]
    return np.array(dflt, np.float64)


def _ma_attr_num(attrs, names, dflt: float) -> float:
    for a, typ, val in attrs:
        if a in names:
            v = _ma_numbers(val)
            if len(v):
                return float(v[0])
    return dflt


def maya_primitive(kind: str, attrs) -> Tuple["np.ndarray", "np.ndarray"]:
    """The mesh a poly-primitive creator node makes (polyCube, polySphere…) from its saved attributes."""
    A = lambda names, d: _ma_attr_num(attrs, tuple("." + n for n in names), d)
    if kind == "polyCube":
        w, h, d = A(("w", "width"), 1.0), A(("h", "height"), 1.0), A(("d", "depth"), 1.0)
        V = np.array([[x, y, z] for x in (-.5, .5) for y in (-.5, .5) for z in (-.5, .5)]) * [w, h, d]
        F = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                      [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
        return V, F
    if kind == "polyPyramid":
        w = A(("w", "sideLength"), 1.0)
        ns_ = max(3, int(A(("ns", "numberOfSides"), 4)))
        r = w / (2 * math.sin(math.pi / ns_))
        h = w * math.sqrt(max(0.0, 1 - 1 / (4 * math.sin(math.pi / ns_) ** 2)))
        ph = np.arange(ns_) * 2 * math.pi / ns_ + (math.pi / ns_ if ns_ == 4 else 0)
        V = np.concatenate([np.stack([r * np.cos(ph), np.full(ns_, -h / 2), r * np.sin(ph)], 1), [[0, h / 2, 0], [0, -h / 2, 0]]])
        i = np.arange(ns_)
        j = (i + 1) % ns_
        F = np.concatenate([np.stack([i, np.full(ns_, ns_), j], 1), np.stack([j, np.full(ns_, ns_ + 1), i], 1)])
        return V, F
    if kind == "polyPlane":
        w, h = A(("w", "width"), 1.0), A(("h", "height"), 1.0)
        sw, sh = max(1, int(A(("sw", "subdivisionsWidth"), 10))), max(1, int(A(("sh", "subdivisionsHeight"), 10)))
        xs, zs = np.linspace(-w / 2, w / 2, sw + 1), np.linspace(-h / 2, h / 2, sh + 1)
        X, Z = np.meshgrid(xs, zs)
        V = np.stack([X.ravel(), np.zeros(X.size), Z.ravel()], 1)
        return V, _grid_faces(sh, sw)
    if kind in ("polySphere", "polyTorus"):
        sa = max(3, int(A(("sa", "subdivisionsAxis", "subdivisionsX"), 20)))
        sh = max(2, int(A(("sh", "subdivisionsHeight", "subdivisionsY"), 20)))
        if kind == "polySphere":
            r = A(("r", "radius"), 1.0)
            th = np.linspace(0, math.pi, sh + 1)[:, None]
            ph = np.linspace(0, 2 * math.pi, sa + 1)[None, :]
            V = np.stack([r * np.sin(th) * np.cos(ph), r * np.cos(th) * np.ones_like(ph), r * np.sin(th) * np.sin(ph)], -1)
        else:
            r, sr = A(("r", "radius"), 1.0), A(("sr", "sectionRadius"), 0.5)
            u = np.linspace(0, 2 * math.pi, sa + 1)[None, :]
            v = np.linspace(0, 2 * math.pi, sh + 1)[:, None]
            V = np.stack([(r + sr * np.cos(v)) * np.cos(u), sr * np.sin(v) * np.ones_like(u), (r + sr * np.cos(v)) * np.sin(u)], -1)
        return V.reshape(-1, 3), _grid_faces(sh, sa)
    if kind in ("polyCylinder", "polyCone"):
        r, h = A(("r", "radius"), 1.0), A(("h", "height"), 2.0)
        sa = max(3, int(A(("sa", "subdivisionsAxis", "subdivisionsX"), 20)))
        ph = np.linspace(0, 2 * math.pi, sa, endpoint=False)
        top_r = 0.0 if kind == "polyCone" else r
        ring_b = np.stack([r * np.cos(ph), np.full(sa, -h / 2), r * np.sin(ph)], 1)
        ring_t = np.stack([top_r * np.cos(ph), np.full(sa, h / 2), top_r * np.sin(ph)], 1)
        V = np.concatenate([ring_b, ring_t, [[0, -h / 2, 0], [0, h / 2, 0]]])
        i = np.arange(sa)
        j = (i + 1) % sa
        side = np.concatenate([np.stack([i, j, j + sa], 1), np.stack([i, j + sa, i + sa], 1)])
        caps = np.concatenate([np.stack([np.full(sa, 2 * sa), j, i], 1), np.stack([np.full(sa, 2 * sa + 1), i + sa, j + sa], 1)])
        return V, np.concatenate([side, caps])
    raise ValueError(kind)


def _grid_faces(rows: int, cols: int):
    r, c = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    a = (r * (cols + 1) + c).ravel()
    b, d = a + 1, a + cols + 1
    return np.concatenate([np.stack([a, d, b], 1), np.stack([b, d, d + 1], 1)])


# deformers: a scene that has them is a rig, where only Maya's evaluation is exact
_MAYA_DEFORMERS = {"skinCluster", "blendShape", "cluster", "wire", "ffd", "nonLinear", "deltaMush", "wrap",
                   "proximityWrap", "lattice", "sculpt", "jiggle", "softMod", "shrinkWrap", "tension", "morph",
                   "solidify", "textureDeformer", "curveWarp", "transferAttributes"}
# history nodes that don't move points (selection bookkeeping, empty tweak nodes)
_MAYA_HARMLESS_HISTORY = {"groupParts", "groupId", "tweak", "objectSet", "dagPose"}
_MAYA_PASS_INPUTS = re.compile(r"^(?:ip|ig|i|inMesh|inputPolymesh|inputGeometry|ip\[0\]\.ig|input\[0\]\.inputGeometry)$")


def _maya_mesh_geometry(nodes, conns_into, key: str, depth: int = 0, ctx=None, trace: Optional[list] = None):
    """(V, F, note) for a mesh shape: its stored data, else follow its history to a stored mesh or a primitive."""
    nd = nodes[key]
    attrs = list(_ma_setattrs(nd["body"]))
    vt = _ma_indexed(attrs, "vt", 3)
    if vt is not None and len(vt):
        pt = _ma_indexed(attrs, "pt", 3)
        if pt is not None and len(pt):                                # Maya draws stored points + tweaks
            n = min(len(pt), len(vt))
            vt[:n] += pt[:n]
        ed = _ma_indexed(attrs, "ed", 3)
        fc = _ma_faces(attrs, ed)
        F = _fan_triangles(*fc) if fc else np.zeros((0, 3), np.int32)
        return vt, F, None
    src = conns_into.get((key, "i")) or conns_into.get((key, "inMesh"))
    return _maya_geometry_from(nodes, conns_into, src, depth + 1, trace if trace is not None else [], ctx) if src else None


def _conns_of(conns_into, node: str) -> list:
    """Incoming connections of one node as (attr, source) — indexed once per scene."""
    idx = conns_into.get(("", "__by_node__"))
    if idx is None:
        idx = {}
        for (n, a), s in conns_into.items():
            idx.setdefault(n, []).append((a, s))
        conns_into[("", "__by_node__")] = idx
    return idx.get(node, [])


def _maya_geometry_from(nodes, conns_into, src: str, depth: int, skipped: list, ctx=None):
    """Geometry arriving through a history connection 'node.attr' (primitive, mesh output, polyUnite, or any
    poly / deformer node, which is passed through to its own input)."""
    while src and depth < 96:
        depth += 1
        node, _, attr = src.partition(".")
        up = nodes.get(node)
        if up is None:
            return None
        t = up["type"]
        if t in _MAYA_PRIMS:
            V, F = maya_primitive(t, list(_ma_setattrs(up["body"])))
            note = f"Built by construction history: shown as its {t} ({up['name']})" + \
                   (f" without {', '.join(dict.fromkeys(skipped[:6]))}" if skipped else "")
            return V, F, note
        if t == "mesh":
            g = _maya_mesh_geometry(nodes, conns_into, node, depth, ctx, skipped)
            if g is not None and attr.startswith("w") and ctx is not None and up["parent"] in nodes:
                M = ctx(up["parent"])                                  # worldMesh: bake that shape's transform
                g = ((np.c_[g[0], np.ones(len(g[0]))] @ M.T)[:, :3], g[1], g[2])
            return g
        if t == "polyUnite":                                           # combined meshes (Mesh → Combine)
            parts, off = [], 0
            ins = sorted(((int(m.group(1)), s) for a, s in _conns_of(conns_into, node)
                          for m in [re.match(r"^ip\[(\d+)\]$", a)] if m))
            Vs, Fs, notes = [], [], []
            for _, s2 in ins:
                g = _maya_geometry_from(nodes, conns_into, s2, depth, list(skipped), ctx)
                if g is None:
                    continue
                Vs.append(g[0])
                Fs.append(np.asarray(g[1], np.int64) + off)
                off += len(g[0])
                if g[2]:
                    notes.append(g[2])
            if not Vs:
                return None
            return np.concatenate(Vs), np.concatenate(Fs), (notes[0] if notes else None)
        skipped.append(t)
        src = next((s for a, s in _conns_of(conns_into, node) if _MAYA_PASS_INPUTS.match(a)), None)
    return None


def _maya_xforms(nodes):
    """(local, world) matrix functions for the nodes of a parsed Maya ASCII scene."""
    xf_cache: Dict[str, np.ndarray] = {}

    def local(k):
        """Maya's transform matrix with pivots (column vectors):
        T · RT · RP · R · RA · RP⁻¹ · ST · SP · SH · S · SP⁻¹   (joints: T · JO · R · RA · S)."""
        nd = nodes[k]
        if nd["type"] not in ("transform", "joint", "lodGroup", "dagContainer"):
            return np.eye(4)
        at = list(_ma_setattrs(nd["body"]))
        vec = lambda name, d: _ma_attr_vec(at, name, d)
        tr = lambda v: (lambda M: (M.__setitem__((slice(0, 3), 3), v), M)[1])(np.eye(4))
        r4 = lambda R3: (lambda M: (M.__setitem__((slice(0, 3), slice(0, 3)), R3), M)[1])(np.eye(4))
        t = vec(".t", (0.0, 0.0, 0.0))
        r = np.radians(vec(".r", (0.0, 0.0, 0.0)))
        sc = vec(".s", (1.0, 1.0, 1.0))
        ro = _MAYA_ROT_ORDERS.get(int(_ma_attr_num(at, (".ro",), 0)), "xyz")
        ra = np.radians(vec(".ra", (0.0, 0.0, 0.0)))
        R = r4(_rot_matrix(*r, ro)) @ r4(_rot_matrix(*ra))
        S = np.diag([sc[0], sc[1], sc[2], 1.0])
        if nd["type"] == "joint":
            jo = np.radians(vec(".jo", (0.0, 0.0, 0.0)))
            return tr(t) @ r4(_rot_matrix(*jo)) @ R @ S
        rp, sp = vec(".rp", (0.0, 0.0, 0.0)), vec(".sp", (0.0, 0.0, 0.0))
        rpt, spt = vec(".rpt", (0.0, 0.0, 0.0)), vec(".spt", (0.0, 0.0, 0.0))
        sh = vec(".sh", (0.0, 0.0, 0.0))
        SH = np.eye(4)
        SH[0, 1], SH[0, 2], SH[1, 2] = sh[0], sh[1], sh[2]
        return tr(t) @ tr(rpt) @ tr(rp) @ R @ tr(-rp) @ tr(spt) @ tr(sp) @ SH @ S @ tr(-sp)

    def world(k, depth=0):
        if k in xf_cache:
            return xf_cache[k]
        M = local(k)
        p = nodes[k]["parent"]
        if p and p in nodes and depth < 128 and not re.search(r'"\.(?:it|inheritsTransform)"\s+(?:no|0|false)\b', nodes[k]["body"]):
            M = world(p, depth + 1) @ M
        xf_cache[k] = M
        return M
    return local, world


_SCENE_CACHE: Dict[str, tuple] = {}


def _cached_scene(path: str, loader, kind: str = "native"):
    """The last parsed scene of each kind, kept in the process so picking object after object in one (big)
    file doesn't re-read it every time. Keyed by path + size + mtime; one scene per kind at a time."""
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return loader(path)
    hit = _SCENE_CACHE.get(kind)
    if hit and hit[0] == key:
        return hit[1]
    _SCENE_CACHE.pop(kind, None)
    val = loader(path)
    _SCENE_CACHE[kind] = (key, val)
    return val


def maya_object_model_arrays(path: str, node: str, stats: Optional[dict] = None):
    """Geometry of a Maya transform and everything under it (Maya ASCII), world space. `node` is the DAG path
    the breakdown gave the row (a bare name works when it is unique). Hidden children are left out."""
    with open(path, "rb") as f:
        binary = f.read(4) in (b"FOR4", b"FOR8")
    nodes, conns, dag = _cached_scene(path, _maya_binary_sections if binary else _maya_ascii_sections)
    whole = node == "*"                                               # the whole scene: every visible top-level node
    if whole:
        root = "\0scene"
        tops = [k for k, nd in nodes.items() if nd.get("type") in ("transform", "joint", "lodGroup") and not nd["parent"]
                and nd["name"] not in _MAYA_DEFAULT_CAMS and k != "\0instances"]
        nodes = dict(nodes)
        nodes[root] = {"type": "scene", "parent": "", "name": "scene", "body": ""}
    else:
        root = node if node in nodes else dag.resolve(node)
    if not root or root not in nodes:
        raise ValueError("node not found in the file")
    conns_into: Dict[Tuple[str, str], str] = {}
    for s_, d in conns:
        n_, _, attr = d.partition(".")
        conns_into[(n_, attr)] = s_
    kids: Dict[str, list] = {}
    for k, nd in nodes.items():
        if nd["parent"]:
            kids.setdefault(nd["parent"], []).append(k)
    if whole:
        kids[root] = tops
    local, world = _maya_xforms(nodes)
    for child, par in nodes.get("\0instances", {}).get("list", []):
        kids.setdefault(par, []).append(child)                         # instanced shapes show under every parent
    hidden = lambda k: bool(re.search(r'setAttr\s+(?:-\w+\s+\w+\s+)*"\.(?:v|visibility)"\s+(?:no|0|false)\b', nodes[k]["body"]))
    no_inherit = lambda k: bool(re.search(r'"\.(?:it|inheritsTransform)"\s+(?:no|0|false)\b', nodes[k]["body"]))
    # walk down from the picked node carrying the world matrix, so an instance gets the matrix of the path
    # it is reached through (the same shape under two transforms shows twice)
    par0 = nodes[root]["parent"]
    start = (world(par0) if par0 in nodes and not no_inherit(root) else np.eye(4)) @ local(root)
    todo, visits, guard = [(root, start, 0)], [], 0
    while todo and guard < 200000:
        guard += 1
        k, M, depth = todo.pop()
        if depth > 256:
            continue
        if k != root and hidden(k):                                    # hidden below the picked node: skip
            continue
        visits.append((k, M))
        for c in kids.get(k, []):
            if c in nodes:
                Mc = (np.eye(4) if no_inherit(c) else M) @ local(c) if nodes[c]["type"] != "mesh" else M
                todo.append((c, Mc, depth + 1))
    Vs, Fs, off, notes, names = [], [], 0, [], []
    unread = approx = 0
    geo_cache: Dict[str, tuple] = {}
    for k, M in visits:
        nd = nodes[k]
        if nd["type"] != "mesh":
            continue
        at = nd["body"]
        if re.search(r'"\.io"\s+(?:yes|1)\b', at):                    # intermediate (orig) shapes
            continue
        if k not in geo_cache:
            trace: list = []
            geo_cache[k] = _maya_mesh_geometry(nodes, conns_into, k, ctx=world, trace=trace)
            if any(t not in _MAYA_HARMLESS_HISTORY for t in trace):
                approx += 1                                           # deformers / modelling steps not applied
        g = geo_cache[k]
        if g is None:
            unread += 1
            continue
        V, F, note = g
        if note and note not in notes:
            notes.append(note)
        Vs.append((np.c_[V, np.ones(len(V))] @ M.T)[:, :3])
        Fs.append(np.asarray(F, np.int64) + off)
        off += len(V)
        names.append(nd["name"])
    if stats is not None:
        stats["unread"] = unread
        stats["approx"] = approx
        stats["rigged"] = any(nd.get("type") in _MAYA_DEFORMERS for nd in nodes.values())
    if not Vs:
        raise ValueError("no mesh geometry stored for this node" + (" (its meshes come from history Kestrel can't rebuild)" if unread else ""))
    if unread:
        notes.append(f"{unread} mesh{'es' if unread > 1 else ''} built by history Kestrel can't rebuild left out.")
    notes.append("Deformers, smooth preview and history steps after the primitive aren't applied.")
    return np.concatenate(Vs).astype(np.float32), np.concatenate(Fs).astype(np.int32), names, notes


# ---- Maya binary (.mb): geometry comes from Maya itself (mayapy), once per file, then cached
_MAYA_GEO_SCRIPT = r'''
import sys, struct, array, traceback
try:
    path, out = sys.argv[1], sys.argv[2]
    import maya.standalone
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds
    import maya.api.OpenMaya as om
    cmds.file(path, open=True, force=True, ignoreVersion=True, prompt=False)
    seen = set()
    with open(out + ".part", "wb") as f:
        f.write(b"KSG1")
        it = om.MItDag(om.MItDag.kDepthFirst, om.MFn.kMesh)
        while not it.isDone():
            for dp in om.MDagPath.getAllPathsTo(it.currentItem()):    # every instance has its own path
                name = dp.fullPathName()
                if name in seen:
                    continue
                seen.add(name)
                try:
                    if om.MFnDagNode(dp).isIntermediateObject:
                        continue
                    mesh = om.MFnMesh(dp)
                    pts = mesh.getPoints(om.MSpace.kWorld)
                    counts, ids = mesh.getTriangles()
                except Exception:
                    continue
                V = array.array("f")
                for p in pts:
                    V.extend((p.x, p.y, p.z))
                F = array.array("i", ids)
                b = name.encode("utf-8")
                f.write(struct.pack("<I", len(b)) + b + struct.pack("<BII", 1 if dp.isVisible() else 0, len(V) // 3, len(F) // 3))
                f.write(V.tobytes())
                f.write(F.tobytes())
            it.next()
    import os
    os.replace(out + ".part", out)
    print("KESTREL-OK")
except Exception:
    traceback.print_exc(file=sys.stdout)
    print("KESTREL-FAIL")
'''


def _scene_geo_cache_file(path: str) -> str:
    try:
        root = GLib.get_user_cache_dir()
    except Exception:
        root = os.path.expanduser("~/.cache")
    st = os.stat(path)
    import hashlib
    h = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:24]
    return os.path.join(root, "kestrel", "scene-geo", h + ".ksg")


_MB_FAILED: Dict[str, str] = {}


def maya_mb_cached(path: str) -> bool:
    try:
        return os.path.exists(_scene_geo_cache_file(path))
    except OSError:
        return False


def maya_mb_meshes(path: str):
    """[(dag path of the shape, visible, V, F)] of every mesh in a Maya binary scene, read by mayapy (the
    file's real result: history, deformers and instances evaluated, world space). The first look at a file
    starts Maya (tens of seconds, a licence); the result is cached under ~/.cache/kestrel/scene-geo."""
    cache = _scene_geo_cache_file(path)
    if cache in _MB_FAILED:                                           # don't boot Maya again for a file it couldn't open
        raise ValueError(_MB_FAILED[cache])
    if not os.path.exists(cache):
        mp = _find_mayapy()
        if not mp:
            raise ValueError("Maya binary geometry needs Maya: mayapy wasn't found (PATH, $MAYA_LOCATION or "
                             "/usr/autodesk/maya*). Save as .ma to preview without Maya.")
        import tempfile
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="kestrel-mb-")
        script = os.path.join(tmp, "geo.py")
        try:
            with open(script, "w") as f:
                f.write(_MAYA_GEO_SCRIPT)
            rc, so, se = _run_streaming([mp, script, os.path.abspath(path), cache], timeout=300)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if "KESTREL-OK" not in so or not os.path.exists(cache):
            tail = [l for l in so + se if l.strip() and ("rror" in l or "icense" in l or "timed out" in l)][-2:]
            _MB_FAILED[cache] = "Maya couldn't open the scene" + (": " + " | ".join(x.strip()[:160] for x in tail) if tail else f" (exit {rc})")
            raise ValueError(_MB_FAILED[cache])
    out = []
    with open(cache, "rb") as f:
        if f.read(4) != b"KSG1":
            raise ValueError("bad geometry cache")
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            n = struct.unpack("<I", h)[0]
            name = f.read(n).decode("utf-8", "replace")
            vis, nv, nf = struct.unpack("<BII", f.read(9))
            V = np.frombuffer(f.read(nv * 12), np.float32).reshape(-1, 3)
            F = np.frombuffer(f.read(nf * 12), np.int32).reshape(-1, 3)
            out.append((name, bool(vis), V, F))
    return out


def maya_scene_maya_arrays(path: str, node: str):
    """Meshes under one DAG path of a .mb (from the Maya-made cache); hidden ones only when all are hidden."""
    meshes = _cached_scene(path, maya_mb_meshes, "maya")
    if node == "*":
        hits = list(meshes)
    else:
        node = node.rstrip("|")
        pref = node + "|"
        short = node.split("|")[-1]
        hits = [m for m in meshes if m[0].startswith(pref)]
        if not hits:                                                  # partial paths: match by the node's name
            hits = [m for m in meshes if f"|{short}|" in m[0] + "|"]
    if not hits:
        raise ValueError("no mesh under this node")
    vis = [m for m in hits if m[1]]
    use = vis or hits
    Vs, Fs, off = [], [], 0
    for name, _, V, F in use:
        Vs.append(V)
        Fs.append(F.astype(np.int64) + off)
        off += len(V)
    notes = ["Read by Maya: history, deformers and instances are evaluated as the scene opens."]
    return np.concatenate(Vs), np.concatenate(Fs).astype(np.int32), [m[0].split("|")[-1] for m in use], notes


def scene_object_model(path: str, key: str, max_faces: int = 400000, maya: str = "never") -> dict:
    """'object:<id>' row of a Blender / Maya breakdown → a 3D-preview model dict.
    Maya scenes are read natively first (maya="never", safe in the decoder worker). When some meshes there
    can't be rebuilt (long modelling history, unknown nodes) and Maya is installed, the result carries
    "maya_could_help"; the window then asks again with maya="only", which reads the scene with mayapy (in a
    thread: it's a subprocess) and caches every mesh."""
    if np is None:
        return {"error": "numpy is needed for object previews"}
    ident = key.split(":", 1)[1]
    stats: dict = {}
    via_maya = False
    try:
        if path.lower().endswith(".blend"):
            V, F, names, notes = blend_object_model_arrays(path, ident)
        elif maya == "only":
            V, F, names, notes = maya_scene_maya_arrays(path, ident)
            via_maya = True
        elif maya_mb_cached(path):
            # Maya has already evaluated this file once: its result is exact and already on disk, so every
            # pick uses it (otherwise children of a rig would show Kestrel's approximation while the whole
            # scene showed Maya's)
            try:
                V, F, names, notes = maya_scene_maya_arrays(path, ident)
                via_maya = True
            except Exception:
                V, F, names, notes = maya_object_model_arrays(path, ident, stats)
        else:
            try:
                V, F, names, notes = maya_object_model_arrays(path, ident, stats)
            except ValueError as ex:
                if _find_mayapy() and "not found" not in str(ex):
                    return {"error": str(ex), "maya_could_help": True}
                raise
    except Exception as ex:
        return {"error": str(ex) or type(ex).__name__}
    if not len(F):
        return {"error": "the mesh has no faces"}
    m = _finish_model_numpy(V, F, max_faces, len(F), [])
    m["notes"] = notes
    m["label"] = f"{len(names)} mesh{'es' if len(names) != 1 else ''} · {len(F):,} triangles"
    if via_maya:
        m["via_maya"] = True
    elif (stats.get("unread") or stats.get("approx") or stats.get("rigged")) and _find_mayapy():
        m["maya_could_help"] = True                                   # Maya can evaluate what was skipped
    return m
