

# =========================================================================== shader & material previews (p3f)
# A material read from any format becomes one plain dict (MATERIAL_DEFAULTS keys, colours in linear RGB,
# textures as file paths or (path, channel, invert) tuples) and is rendered as a "material ball": a sphere in a
# small studio (key, fill and rim light plus a procedural softbox environment), GGX specular, optional clear
# coat, normal map, emission, opacity / transmission. numpy only, so it works without OpenGL.

MATERIAL_DEFAULTS = {
    "name": "", "base_color": (0.8, 0.8, 0.8), "base_weight": 1.0, "base_color_tex": None,
    "metallic": 0.0, "metallic_tex": None, "roughness": 0.5, "roughness_tex": None,
    "specular": 1.0, "normal_tex": None, "normal_strength": 1.0,
    "emission": (0.0, 0.0, 0.0), "emission_tex": None, "opacity": 1.0, "opacity_tex": None,
    "transmission": 0.0, "coat": 0.0, "coat_roughness": 0.1, "coat_color": (1.0, 1.0, 1.0), "coat_color_tex": None,
    "tiling": (1.0, 1.0),
}

_TEX_CACHE: Dict[Tuple[str, int], object] = {}


def _srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin1(v: float) -> float:
    """One sRGB-encoded channel → linear (no numpy needed)."""
    v = float(v)
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def load_texture_array(path: str, max_size: int = 512):
    """Image file → float32 H×W×4 in 0..1 (as stored: colour maps are still sRGB-encoded). UDIM / <UVTILE>
    paths use the first tile. None when it can't be read."""
    if not path:
        return None
    p = path
    if re.search(r"<UDIM>|<udim>|<UVTILE>|\$\{?UDIM\}?|%\(UDIM\)d", p):
        hits = sorted(glob.glob(re.sub(r"<UDIM>|<udim>|<UVTILE>|\$\{?UDIM\}?|%\(UDIM\)d", "*", p)))
        if not hits:
            return None
        p = hits[0]
    key = (p, max_size)
    if key in _TEX_CACHE:
        return _TEX_CACHE[key]
    arr = None
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext in PREVIEW_DECODERS:
            raw = PREVIEW_DECODERS[ext](p, max_size, max_size)
            if raw is not None:
                a = np.frombuffer(raw.rgb, np.uint8).reshape(raw.h, raw.w, 3).astype(np.float32) / 255.0
                arr = np.concatenate([a, np.ones((raw.h, raw.w, 1), np.float32)], 2)
        if arr is None:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(p, max_size, max_size, True)
                w, h, n, rs = pb.get_width(), pb.get_height(), pb.get_n_channels(), pb.get_rowstride()
                buf = np.frombuffer(pb.get_pixels(), np.uint8)
                a = np.lib.stride_tricks.as_strided(buf, (h, w, n), (rs, n, 1)).astype(np.float32) / 255.0
                if n == 3:
                    a = np.concatenate([a, np.ones((h, w, 1), np.float32)], 2)
                arr = a
            except Exception:
                try:                                              # development / tests without GdkPixbuf
                    from PIL import Image as _PILImage
                    im = _PILImage.open(p).convert("RGBA")
                    im.thumbnail((max_size, max_size))
                    arr = np.asarray(im, np.float32) / 255.0
                except Exception:
                    arr = None
    except Exception:
        arr = None
    if len(_TEX_CACHE) > 48:
        _TEX_CACHE.clear()
    _TEX_CACHE[key] = arr
    return arr


def _tex_sample(arr, u, v):
    """Bilinear, wrapping. u, v arrays in texture repeats."""
    h, w = arr.shape[:2]
    x = (u % 1.0) * w - 0.5
    y = (1.0 - (v % 1.0)) * h - 0.5
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[..., None].astype(np.float32)
    fy = (y - y0)[..., None].astype(np.float32)
    x0 %= w
    y0 %= h
    x1 = (x0 + 1) % w
    y1 = (y0 + 1) % h
    a = arr[y0, x0] * (1 - fx) + arr[y0, x1] * fx
    b = arr[y1, x0] * (1 - fx) + arr[y1, x1] * fx
    return a * (1 - fy) + b * fy


def _norm(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-8)


def _studio_env(d):
    """Procedural studio environment seen along directions d (…×3): graded backdrop, a big softbox above
    the key side, a strip light behind for rims. Linear radiance."""
    y = d[..., 1:2]
    sky = 0.18 + 0.32 * np.clip(y * 0.5 + 0.5, 0, 1)
    env = np.concatenate([sky * 0.95, sky * 0.97, sky * 1.0], -1)
    floor = np.clip((0.05 - y) / 0.35, 0, 1) ** 1.5                     # soft horizon, no hard line
    env = env * (1 - floor) + np.array([0.11, 0.105, 0.10], np.float32) * floor
    key = _norm(np.array([-0.55, 0.65, 0.55], np.float32))
    c = np.clip((d * key).sum(-1, keepdims=True), 0, 1)
    env = env + 9.0 * np.clip((c - 0.93) / 0.07, 0, 1) ** 1.5           # softbox
    rim = _norm(np.array([0.6, 0.25, -0.75], np.float32))
    c2 = np.clip((d * rim).sum(-1, keepdims=True), 0, 1)
    env = env + 4.0 * np.clip((c2 - 0.96) / 0.04, 0, 1)                   # strip light
    return env.astype(np.float32)


def _env_avg(n):
    """Cosine-blurred version of the studio (diffuse irradiance / very rough reflections)."""
    y = n[..., 1:2]
    up = np.clip(y * 0.5 + 0.5, 0, 1)
    base = 0.12 + 0.30 * up
    key = _norm(np.array([-0.55, 0.65, 0.55], np.float32))
    k = np.clip((n * key).sum(-1, keepdims=True), 0, 1)
    rim = _norm(np.array([0.6, 0.25, -0.75], np.float32))
    r = np.clip((n * rim).sum(-1, keepdims=True), 0, 1)
    v = base + 0.55 * k ** 2 + 0.18 * r ** 3
    return np.concatenate([v * 0.97, v * 0.98, v], -1).astype(np.float32)


def _env_brdf(nv, rough):
    """Karis' analytic approximation of the split-sum environment BRDF → (scale, bias) for F0."""
    c0 = np.array([-1.0, -0.0275, -0.572, 0.022], np.float32)
    c1 = np.array([1.0, 0.0425, 1.04, -0.04], np.float32)
    r = rough[..., None] * c0 + c1
    a004 = np.minimum(r[..., 0] * r[..., 0], np.exp2(-9.28 * nv)) * r[..., 0] + r[..., 1]
    return a004 * -1.04 + r[..., 2], a004 * 1.04 + r[..., 3]


def _ggx_direct(n, v, l, rough, f0):
    h = _norm(v + l)
    nl = np.clip((n * l).sum(-1), 0, 1)
    nv = np.clip((n * v).sum(-1), 1e-4, 1)
    nh = np.clip((n * h).sum(-1), 0, 1)
    vh = np.clip((v * h).sum(-1), 0, 1)
    a = np.maximum(rough * rough, 2e-3)
    a2 = a * a
    d = a2 / (math.pi * (nh * nh * (a2 - 1) + 1) ** 2)
    k = (rough + 1) ** 2 / 8
    g = (nv / (nv * (1 - k) + k)) * (nl / (nl * (1 - k) + k))
    f = f0 + (1 - f0) * ((1 - vh) ** 5)[..., None]
    spec = (d * g / (4 * nv * np.maximum(nl, 1e-4)))[..., None] * f
    return spec, nl


def render_material_ball(mat: dict, size: int = 360, yaw: float = 0.45, tilt: float = 0.25) -> RawImage:
    """Material dict → RawImage of a shaded sphere (2× supersampled)."""
    m = dict(MATERIAL_DEFAULTS)
    m.update({k: v for k, v in mat.items() if v is not None})
    S = int(size) * 2
    f32 = np.float32
    ys, xs = np.mgrid[0:S, 0:S].astype(f32)
    px = (xs + 0.5) / S * 2 - 1
    py = 1 - (ys + 0.5) / S * 2
    R = 0.78
    cx, cy = 0.0, 0.08
    sx, sy = (px - cx) / R, (py - cy) / R
    rr = sx * sx + sy * sy
    inside = rr < 1.0
    # backdrop: gradient + contact shadow
    bg_t = np.clip(py * 0.5 + 0.5, 0, 1)[..., None]
    bg = (np.array([0.055, 0.055, 0.06], f32) * (1 - bg_t) + np.array([0.20, 0.205, 0.215], f32) * bg_t)
    sh = np.exp(-(((px - cx) / 0.85) ** 2 + ((py - (cy - R - 0.02)) / 0.11) ** 2) * 2.2)[..., None]
    bg = bg * (1 - 0.65 * sh)
    out = bg.copy()
    idx = np.nonzero(inside)
    if len(idx[0]):
        x, y = sx[idx], sy[idx]
        z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
        n_view = np.stack([x, y, z], -1).astype(f32)
        v = np.array([0, 0, 1], f32)[None, :].repeat(len(x), 0)
        # object space (spin + tilt) for UVs
        cyw, syw, ct, st = math.cos(yaw), math.sin(yaw), math.cos(tilt), math.sin(tilt)
        oy = n_view[:, 1] * ct - n_view[:, 2] * st
        oz0 = n_view[:, 1] * st + n_view[:, 2] * ct
        ox = n_view[:, 0] * cyw - oz0 * syw
        oz = n_view[:, 0] * syw + oz0 * cyw
        phi = np.arctan2(ox, oz)
        u = (phi / (2 * math.pi) + 0.5) * 2.0 * float(m["tiling"][0])
        vv = (np.arcsin(np.clip(oy, -1, 1)) / math.pi + 0.5) * float(m["tiling"][1])

        def tex(key, srgb=False):
            spec = m.get(key)
            if not spec:
                return None
            path, ch, inv = (spec, None, False) if isinstance(spec, str) else (tuple(spec) + (None, False))[:3]
            arr = load_texture_array(path)
            if arr is None:
                return None
            s = _tex_sample(arr, u, vv)
            if ch is not None:
                c = s[:, "rgba".index(ch) if isinstance(ch, str) else int(ch)]
                c = 1 - c if inv else c
                return c
            rgb = s[:, :3]
            return _srgb_to_linear(rgb) if srgb else rgb

        base = np.array(m["base_color"], f32)[None, :] * float(m["base_weight"])
        t = tex("base_color_tex", True)
        if t is not None:
            base = base * t if t.ndim == 2 else base * t[:, None]
        base = np.ascontiguousarray(np.broadcast_to(base, (len(x), 3)), dtype=f32)
        metal = np.full(len(x), float(m["metallic"]), f32)
        t = tex("metallic_tex")
        if t is not None:
            metal = (metal * t) if t.ndim == 1 else metal * t[:, 2]
        rough = np.full(len(x), float(m["roughness"]), f32)
        t = tex("roughness_tex")
        if t is not None:
            rough = (t if t.ndim == 1 else t[:, 1]) * (1.0 if float(m["roughness"]) <= 0 else 1.0)
        rough = np.clip(rough, 0.03, 1.0)
        n = n_view
        t = tex("normal_tex")
        if t is not None:
            # tangent along +u in object space, brought back to view space
            tx_o = np.stack([np.cos(phi), np.zeros_like(phi), -np.sin(phi)], -1)
            tx_v = np.stack([tx_o[:, 0] * cyw + tx_o[:, 2] * syw, np.zeros_like(phi), -tx_o[:, 0] * syw + tx_o[:, 2] * cyw], -1)
            ty = tx_v[:, 1] * ct + tx_v[:, 2] * st
            tz = -tx_v[:, 1] * st + tx_v[:, 2] * ct
            T = _norm(np.stack([tx_v[:, 0], ty, tz], -1))
            B = _norm(np.cross(n, T))
            nm = (t * 2 - 1) * np.array([m["normal_strength"], m["normal_strength"], 1], f32)
            n = _norm(T * nm[:, 0:1] + B * nm[:, 1:2] + n * nm[:, 2:3])
        nv = np.clip((n * v).sum(-1), 1e-4, 1)
        spec_lvl = 0.08 * float(m["specular"]) * 0.5
        f0 = spec_lvl * (1 - metal[:, None]) + base * metal[:, None]
        diffuse_col = base * (1 - metal[:, None])
        col = np.zeros_like(base)
        for ldir, lcol in (((-0.55, 0.65, 0.55), (1.9, 1.82, 1.7)), ((0.7, 0.15, 0.6), (0.3, 0.33, 0.4)),
                           ((0.6, 0.25, -0.75), (1.3, 1.3, 1.4))):
            l = _norm(np.array(ldir, f32))[None, :].repeat(len(x), 0)
            spec, nl = _ggx_direct(n, v, l, rough, f0)
            col += (diffuse_col / math.pi * nl[:, None] + spec * nl[:, None]) * np.array(lcol, f32)
        # environment: diffuse irradiance + split-sum specular
        col += diffuse_col * _env_avg(n) * 0.55
        refl = _norm(2 * nv[:, None] * n - v)
        sharp = _studio_env(refl)
        blur = _env_avg(refl)
        mixr = np.clip(rough * rough * 1.6, 0, 1)[:, None]
        env_spec = sharp * (1 - mixr) + blur * mixr
        a, b = _env_brdf(nv, rough)
        col += env_spec * (f0 * a[:, None] + b[:, None])
        # clear coat
        coat = float(m["coat"])
        if coat > 0:
            cc = np.array(m["coat_color"], f32)[None, :]
            t = tex("coat_color_tex", True)
            if t is not None:
                cc = t
            col = col * (cc * coat + (1 - coat))
            cr = np.full(len(x), max(0.03, float(m["coat_roughness"])), f32)
            f0c = np.full((len(x), 3), 0.04, f32)
            cspec = np.zeros_like(col)
            for ldir, lcol in (((-0.55, 0.65, 0.55), (2.6, 2.5, 2.35)), ((0.6, 0.25, -0.75), (1.6, 1.6, 1.7))):
                l = _norm(np.array(ldir, f32))[None, :].repeat(len(x), 0)
                s_, nl = _ggx_direct(n_view, v, l, cr, f0c)
                cspec += s_ * nl[:, None] * np.array(lcol, f32)
            nvc = np.clip(z, 1e-4, 1)
            refl_c = _norm(2 * nvc[:, None] * n_view - v)
            a, b = _env_brdf(nvc, cr)
            fc = 0.04 * a + b
            mc = np.clip(cr * cr * 1.6, 0, 1)[:, None]
            col = col * (1 - coat * fc[:, None]) + coat * (cspec + (_studio_env(refl_c) * (1 - mc) + _env_avg(refl_c) * mc) * fc[:, None])
        # emission
        em = np.array(m["emission"], f32)[None, :]
        t = tex("emission_tex", True)
        if t is not None:
            em = em * t if np.any(em) else t
        col += em
        # transmission / opacity: see the backdrop through the ball (bent a little for glass)
        trans = float(m["transmission"])
        op = np.full(len(x), float(m["opacity"]), f32)
        t = tex("opacity_tex")
        if t is not None:
            op = op * (t if t.ndim == 1 else t[:, 0])
        # glass bends the backdrop upside down (a ball lens); plain transparency shows it straight
        behind_y = np.clip(((cy + y * R * (-0.8 if trans > 0 else 1.0)) * 0.5 + 0.5), 0, 1)[:, None]
        behind = np.array([0.055, 0.055, 0.06], f32) * (1 - behind_y) + np.array([0.20, 0.205, 0.215], f32) * behind_y
        fres = (0.04 + 0.96 * (1 - nv) ** 5)[:, None]
        if trans > 0:
            tint = base if np.any(base) else np.ones_like(base)
            caustic = np.exp(-((x + 0.15) ** 2 + (y + 0.45) ** 2) * 6.0)[:, None] * 0.35
            glass = (behind * 1.6 + caustic) * tint * (1 - fres) + env_spec * fres * 1.4
            col = col * (1 - trans) + (glass + env_spec * (f0 * a[:, None] + b[:, None]) * 0.5) * trans
        if np.any(op < 1):
            o = op[:, None]
            col = col * o + behind * (1 - o) + env_spec * fres * (1 - o)
        out[idx] = col
    # filmic tone map (ACES approx) + sRGB, then 2×2 downsample
    a_, b_, c_, d_, e_ = 2.51, 0.03, 2.43, 0.59, 0.14
    x_ = out * 1.0
    tm = np.clip((x_ * (a_ * x_ + b_)) / (x_ * (c_ * x_ + d_) + e_), 0, 1)
    srgb = np.where(tm <= 0.0031308, tm * 12.92, 1.055 * np.power(tm, 1 / 2.4) - 0.055)
    small = srgb.reshape(S // 2, 2, S // 2, 2, 3).mean(axis=(1, 3))
    rgb = (np.clip(small, 0, 1) * 255 + 0.5).astype(np.uint8)
    return RawImage(S // 2, S // 2, rgb.tobytes())


# --------------------------------------------------------------------------- material / shader readers
# Each returns a preview dict: {"kind", "info", "tree", "notes", "materials": [material dict],
# "code": (text, language) or None, "glsl": {"source", "style"} for live-renderable fragment shaders,
# "images": [(label, RawImage)] for previews stored in the file itself}.

def _mat_from(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


def _floats(s: str) -> list:
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s or "")]


def _col3(s, default=None):
    f = _floats(s) if isinstance(s, str) else list(s or [])
    if len(f) >= 3:
        return (f[0], f[1], f[2])
    if len(f) == 1:
        return (f[0],) * 3
    return default


def _tex_node(label: str, path: str, owner: str = "", base: str = "") -> list:
    full, ok = resolve_scene_path(path, base or path) if path else ("", None)
    return _bn(label, owner, None, full or path, ok)


def _mat_summary_nodes(m: dict) -> list:
    """Material dict → breakdown rows (values and textures)."""
    rows = []
    for key, label in (("base_color", "Base colour"), ("base_weight", "Base weight"), ("metallic", "Metalness"),
                       ("roughness", "Roughness"), ("specular", "Specular"), ("coat", "Coat"),
                       ("coat_roughness", "Coat roughness"), ("emission", "Emission"), ("opacity", "Opacity"),
                       ("transmission", "Transmission")):
        if key in m:
            v = m[key]
            txt = ", ".join(f"{c:.3g}" for c in v) if isinstance(v, (tuple, list)) else f"{v:.3g}"
            rows.append(_bn(label, txt))
    for key, label in (("base_color_tex", "Base colour map"), ("metallic_tex", "Metalness map"),
                       ("roughness_tex", "Roughness map"), ("normal_tex", "Normal map"), ("emission_tex", "Emission map"),
                       ("opacity_tex", "Opacity map"), ("coat_color_tex", "Coat colour map")):
        t = m.get(key)
        if t:
            p = t if isinstance(t, str) else t[0]
            extra = "" if isinstance(t, str) else f"channel {t[1]}" + (" inverted" if len(t) > 2 and t[2] else "")
            ok = os.path.exists(p) if p else None
            if p and re.search(r"<UDIM>|<UVTILE>", p):
                ok = bool(glob.glob(re.sub(r"<UDIM>|<UVTILE>", "*", p)))
            rows.append(_bn(label, extra or os.path.basename(p), None, p, ok))
    return rows


# ---- MaterialX
_MTLX_MAP = {
    "standard_surface": {"base": "base_weight", "base_color": "base_color", "metalness": "metallic",
                         "specular_roughness": "roughness", "specular": "specular", "coat": "coat",
                         "coat_color": "coat_color", "coat_roughness": "coat_roughness", "emission": "_em_w",
                         "emission_color": "_em_c", "opacity": "opacity", "transmission": "transmission",
                         "transmission_color": "_tr_c", "normal": "normal"},
    "open_pbr_surface": {"base_weight": "base_weight", "base_color": "base_color", "base_metalness": "metallic",
                         "specular_roughness": "roughness", "specular_weight": "specular", "coat_weight": "coat",
                         "coat_color": "coat_color", "coat_roughness": "coat_roughness",
                         "emission_luminance": "_em_w_nits", "emission_color": "_em_c", "geometry_opacity": "opacity",
                         "transmission_weight": "transmission", "transmission_color": "_tr_c",
                         "geometry_normal": "normal"},
    "UsdPreviewSurface": {"diffuseColor": "base_color", "metallic": "metallic", "roughness": "roughness",
                          "emissiveColor": "emission", "opacity": "opacity", "normal": "normal",
                          "clearcoat": "coat", "clearcoatRoughness": "coat_roughness"},
    "gltf_pbr": {"base_color": "base_color", "metallic": "metallic", "roughness": "roughness", "emissive": "_em_c",
                 "emissive_strength": "_em_w", "alpha": "opacity", "transmission": "transmission",
                 "clearcoat": "coat", "clearcoat_roughness": "coat_roughness", "normal": "normal"},
    "disney_principled": {"basecolor": "base_color", "baseColor": "base_color", "metallic": "metallic",
                          "roughness": "roughness", "clearcoat": "coat", "specular": "specular"},
}


def _mtlx_preview(path: str) -> dict:
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as ex:
        raise ValueError(f"not readable as MaterialX XML ({ex})")
    base_dir = os.path.dirname(os.path.abspath(path))
    nodes: Dict[str, object] = {}
    prefix_of: Dict[int, str] = {}
    parent_of: Dict[int, object] = {}

    def walk(el, prefix):
        pre = el.get("fileprefix", prefix) if el.get("fileprefix") is not None else prefix
        for ch in el:
            parent_of[id(ch)] = el
            prefix_of[id(ch)] = ch.get("fileprefix", pre) if ch.get("fileprefix") is not None else pre
            if ch.get("name"):
                nodes.setdefault(ch.get("name"), ch)
                scoped = (el.get("name") or "") + "/" + ch.get("name")
                nodes[scoped] = ch
            walk(ch, prefix_of[id(ch)])
    prefix_of[id(root)] = root.get("fileprefix", "")
    walk(root, prefix_of[id(root)])
    textures: Dict[str, list] = {}

    def images_upstream(node, depth=0, seen=None):
        """Every image file feeding a node (depth-first, nearest first)."""
        seen = set() if seen is None else seen
        if node is None or depth > 12 or id(node) in seen:
            return []
        seen.add(id(node))
        out = []
        for inp in node.findall("input"):
            if inp.get("type") == "filename" and inp.get("value"):
                pre = prefix_of.get(id(inp), "") or prefix_of.get(id(node), "")
                p = os.path.normpath(os.path.join(base_dir, pre + inp.get("value")))
                textures.setdefault(p, []).append(node.get("name", ""))
                out.append(p)
        for inp in node.findall("input"):
            up = upstream(inp, node)
            if up is not None:
                out += images_upstream(up, depth + 1, seen)
        return out

    _want = {"normal": r"norm|nrm|_n\.|bump", "roughness": r"rough|gloss", "metallic": r"metal",
             "base_color": r"col|albedo|diff|base|basecolor", "_em_c": r"emis|emit", "opacity": r"opac|alpha|mask",
             "coat_color": r"col|albedo|diff"}

    def file_of(node, key=None):
        """The image a shader input most likely shows: the upstream file whose name fits the input."""
        files = images_upstream(node)
        if not files:
            return None
        pat = _want.get(key or "")
        if pat:
            best = [f for f in files if re.search(pat, os.path.basename(f).lower())]
            if best:
                return best[0]
            if key == "normal":
                return None                                   # a colour map isn't a normal map
        return files[0]

    def upstream(inp, owner):
        if inp.get("nodegraph"):
            ng = nodes.get(inp.get("nodegraph"))
            if ng is None:
                return None
            outs = ng.findall("output")
            out = next((o for o in outs if o.get("name") == inp.get("output", "out")), None)
            if out is None:
                out = outs[0] if outs else None
            if out is None:
                return None
            hit = nodes.get(f"{ng.get('name')}/{out.get('nodename')}")
            return hit if hit is not None else nodes.get(out.get("nodename") or "")
        if inp.get("nodename"):
            scope = parent_of.get(id(owner))
            scoped = f"{scope.get('name')}/{inp.get('nodename')}" if scope is not None and scope.get("name") else ""
            hit = nodes.get(scoped) if scoped else None
            return hit if hit is not None else nodes.get(inp.get("nodename"))
        if inp.get("interfacename"):
            return None
        return None
    mats, tree_mats = [], []
    for sm in root.iter("surfacematerial"):
        si = next((i for i in sm.findall("input") if i.get("name") == "surfaceshader"), None)
        sh = upstream(si, sm) if si is not None else None
        if sh is None:
            continue
        kind = sh.tag
        mp = _MTLX_MAP.get(kind, {})
        m = {"name": sm.get("name", "")}
        rows = []
        em_w, em_c = None, None
        for inp in sh.findall("input"):
            nm = inp.get("name")
            key = mp.get(nm)
            up = upstream(inp, sh)
            if up is not None:
                f = file_of(up, key)
                rows.append(_bn(nm, f"← {up.get('name')} ({up.tag})", None, f or "", (os.path.exists(f) if f else None)))
                if f and key:
                    tk = {"base_color": "base_color_tex", "metallic": "metallic_tex", "roughness": "roughness_tex",
                          "normal": "normal_tex", "_em_c": "emission_tex", "emission": "emission_tex",
                          "opacity": "opacity_tex", "coat_color": "coat_color_tex"}.get(key)
                    if tk:
                        m[tk] = f
                        if key == "base_color":
                            m.setdefault("base_color", (1.0, 1.0, 1.0))
                continue
            val = inp.get("value", "")
            rows.append(_bn(nm, val))
            if not key or not val:
                continue
            if key in ("base_color", "coat_color", "emission"):
                m[key] = _col3(val, (0.8, 0.8, 0.8))
            elif key == "_em_c":
                em_c = _col3(val)
            elif key == "_tr_c":
                m["_tr_c"] = _col3(val)
            elif key == "_em_w":
                em_w = (_floats(val) or [0])[0]
            elif key == "_em_w_nits":
                em_w = (_floats(val) or [0])[0] / 1000.0
            elif key == "opacity":
                f = _floats(val)
                m["opacity"] = sum(f) / len(f) if f else 1.0
            elif key != "normal":
                f = _floats(val)
                if f:
                    m[key] = f[0]
        tr_c = m.pop("_tr_c", None)
        if tr_c and m.get("transmission", 0) > 0 and "base_color_tex" not in m:
            m["base_color"] = tr_c                              # glass / liquids: show the transmission tint
        if em_c or em_w:
            c = em_c or (1.0, 1.0, 1.0)
            w = 1.0 if em_w is None else em_w
            m["emission"] = (c[0] * w, c[1] * w, c[2] * w)
        mats.append(m)
        tree_mats.append(_bn(m["name"], f"{kind}", [_bn("Inputs", f"{len(rows)}", rows)] + ([_bn("Ball preview uses", "", _mat_summary_nodes(m))]),
                             f"material:{len(mats) - 1}"))
    counts: Dict[str, int] = {}
    for el in root.iter():
        if el.get("name") and el.tag not in ("input", "output"):
            counts[el.tag] = counts.get(el.tag, 0) + 1
    tex_nodes = [_bn(os.path.basename(p), ", ".join(sorted(set(o)))[:80], None, p, os.path.exists(p)) for p, o in sorted(textures.items())]
    miss = sum(1 for t in tex_nodes if t[4] is False)
    tree = [_bn("Materials", f"{len(mats)}", tree_mats),
            _bn("References", f"{len(tex_nodes)} textures" + (f" · {miss} missing" if miss else ""), [_bn("Textures", f"{len(tex_nodes)}", tex_nodes)] if tex_nodes else []),
            _bn("Nodes", f"{sum(counts.values())}", _bn_counts(counts))]
    info = [("MaterialX", root.get("version", "?")), ("Materials", f"{len(mats)}")]
    if root.get("colorspace"):
        info.append(("Colour space", root.get("colorspace")))
    return {"kind": "MaterialX document", "info": info, "tree": tree, "notes": [] if mats else
            ["No surface material in this document (a node definition or test graph): nothing to put on the ball."],
            "materials": mats, "code": None}


# ---- OSL source and compiled .oso
_RE_OSL_HEAD = re.compile(r"\b(shader|surface|displacement|volume|light)\s+(\w+)\s*(?:\[\[.*?\]\]\s*)?\(", re.S)


def _strip_c_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _osl_params(src: str):
    """→ (shader type, name, [(output?, type, name, default, metadata)])"""
    clean = _strip_c_comments(src)
    m = _RE_OSL_HEAD.search(clean)
    if not m:
        return None, None, []
    i = m.end()
    depth, j = 1, i
    while j < len(clean) and depth:
        depth += {"(": 1, ")": -1}.get(clean[j], 0)
        j += 1
    body = clean[i:j - 1]
    params, cur, d = [], "", 0
    for ch in body:                                          # split on top-level commas
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        if ch == "," and d == 0:
            params.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        params.append(cur)
    out = []
    for p in params:
        meta = " ".join(re.findall(r"\[\[(.*?)\]\]", p, re.S))
        p2 = re.sub(r"\[\[.*?\]\]", "", p, flags=re.S).strip()
        mm = re.match(r"(output\s+)?(\w+(?:\s*\[\s*\d*\s*\])?)\s+(\w+)\s*(?:\[\s*\d*\s*\])?\s*(?:=\s*(.*))?$", p2, re.S)
        if mm:
            out.append((bool(mm.group(1)), mm.group(2), mm.group(3), (mm.group(4) or "").strip(), meta.strip()))
    return m.group(1), m.group(2), out


def _osl_guess_material(params, base: str) -> dict:
    m = {}
    for is_out, typ, name, default, meta in params:
        if is_out:
            continue
        low = name.lower()
        if typ == "color" and "base_color" not in m and re.search(r"^cd$|diffuse|base|albedo|colou?r", low) and "spec" not in low:
            m["base_color"] = _col3(default, None) or m.get("base_color")
        elif typ == "float" and re.search(r"rough", low) and "roughness" not in m:
            f = _floats(default)
            if f:
                m["roughness"] = f[0]
        elif typ == "float" and re.search(r"metal", low):
            f = _floats(default)
            if f:
                m["metallic"] = f[0]
        elif typ == "string" and re.search(r"tex|file|map|image", low) and default.strip('"'):
            p = default.strip().strip('"')
            if p and "base_color_tex" not in m:
                m["base_color_tex"] = resolve_scene_path(p, base)[0]
    return {k: v for k, v in m.items() if v is not None}


def _osl_preview(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    if path.lower().endswith(".oso"):
        return _oso_preview(path, src)
    stype, name, params = _osl_params(src)
    def _meta(meta):
        h = re.search(r'string\s+help\s*=\s*"([^"]*)"', meta)
        return " ".join((h.group(1) if h else meta).split())[:90]
    rows = [_bn(nm, f"{'output ' if o else ''}{t}" + (f" = {' '.join(d.split())}" if d else "") + (f"  · {_meta(meta)}" if meta else ""))
            for o, t, nm, d, meta in params]
    incs = re.findall(r'#include\s+[<"]([^>"]+)[>"]', src)
    m = _osl_guess_material(params, path)
    tree = [_bn("Parameters", f"{len(params)}", rows)]
    if incs:
        tree.append(_bn("Includes", f"{len(incs)}", [_bn(i) for i in incs]))
    closures = sorted(set(re.findall(r"\b(diffuse|oren_nayar|phong|ward|microfacet\w*|reflection|refraction|transparent|emission|background|translucent|subsurface|holdout|debug|principled\w*|conductor_bsdf|dielectric_bsdf|generalized_schlick_bsdf|sheen_bsdf|burley_diffuse_bsdf|oren_nayar_diffuse_bsdf)\s*\(", src)))
    if closures:
        tree.append(_bn("Closures used", f"{len(closures)}", [_bn(c) for c in closures]))
    notes = ["The ball is a guess from the parameter defaults (colour / roughness / metal / texture names); "
             "OSL itself isn't executed."] if m else []
    return {"kind": f"OSL {stype or 'shader'}", "info": [("Shader", name or "?"), ("Type", stype or "?"),
                                                           ("Parameters", f"{len(params)}")],
            "tree": tree, "notes": notes, "materials": [m] if m else [], "code": (src, "c")}


def _oso_preview(path: str, src: str) -> dict:
    params, stype, name, ver = [], "", "", ""
    for line in src.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if not parts:
            continue
        if parts[0] == "OpenShadingLanguage":
            ver = parts[1] if len(parts) > 1 else ""
        elif parts[0] in ("shader", "surface", "displacement", "volume", "light"):
            stype, name = parts[0], parts[1] if len(parts) > 1 else ""
        elif parts[0] in ("param", "oparam") and len(parts) >= 3:
            vals = [p for p in parts[3:] if p and not p.startswith("%")]
            params.append((parts[0] == "oparam", parts[1], parts[2], " ".join(vals[:4]), ""))
        elif parts[0] == "code":
            break
    rows = [_bn(nm, f"{'output ' if o else ''}{t}" + (f" = {d}" if d else "")) for o, t, nm, d, _ in params]
    m = _osl_guess_material(params, path)
    return {"kind": f"Compiled OSL {stype or 'shader'}", "info": [("Shader", name), ("OSL", ver), ("Parameters", f"{len(params)}")],
            "tree": [_bn("Parameters", f"{len(params)}", rows)], "notes": [], "materials": [m] if m else [], "code": (src, "c")}


# ---- GLSL / HLSL source
_GLSL_EXTS = (".glsl", ".frag", ".fs", ".fsh", ".vert", ".vs", ".vsh", ".geom", ".gs", ".comp", ".tesc", ".tese", ".shader_glsl")
_HLSL_EXTS = (".hlsl", ".fx", ".fxh", ".hlsli", ".cg", ".cginc", ".usf", ".ush", ".ps", ".compute")


def glsl_style(src: str) -> str:
    """'shadertoy' (mainImage), 'sandbox' (main + gl_FragColor / out vec4 with time/resolution uniforms),
    or '' when the source isn't a stand-alone fragment shader."""
    clean = _strip_c_comments(src)
    if re.search(r"\bvoid\s+mainImage\s*\(\s*out\s+vec4", clean):
        return "shadertoy"
    if re.search(r"\bvoid\s+main\s*\(", clean) and (("gl_FragColor" in clean) or re.search(r"\bout\s+vec4\s+\w+\s*;", clean)) \
            and "gl_Position" not in clean and "layout(local_size" not in clean.replace(" ", ""):
        return "sandbox"
    return ""


def _shader_source_preview(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read(4 << 20)
    ext = os.path.splitext(path)[1].lower()
    clean = _strip_c_comments(src)
    hlsl = ext in _HLSL_EXTS or bool(re.search(r"\b(cbuffer|SV_Target|Texture2D|SamplerState|float4x4)\b", clean))
    tree, info = [], []
    unis = re.findall(r"\buniform\s+(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+(\w+)\s*(\[[^\]]*\])?\s*(?:=\s*([^;]+))?;", clean)
    if unis:
        tree.append(_bn("Uniforms", f"{len(unis)}", [_bn(n + (a or ""), t + (f" = {d.strip()}" if d else "")) for t, n, a, d in unis]))
    ins = re.findall(r"\b(?:in|varying|attribute)\s+(?:(?:flat|smooth|noperspective|lowp|mediump|highp)\s+)*(\w+)\s+(\w+)\s*;", clean)
    if ins:
        tree.append(_bn("Inputs", f"{len(ins)}", [_bn(n, t) for t, n in ins]))
    if hlsl:
        cbs = re.findall(r"\bcbuffer\s+(\w+)", clean)
        texs = re.findall(r"\b(Texture\w*|TextureCube\w*|sampler\w*|SamplerState)\s+(\w+)", clean)
        if cbs:
            tree.append(_bn("Constant buffers", f"{len(cbs)}", [_bn(c) for c in cbs]))
        if texs:
            tree.append(_bn("Textures & samplers", f"{len(texs)}", [_bn(n, t) for t, n in texs]))
    funcs = re.findall(r"^\s*(?:\w+\s+)+?(\w+)\s*\([^;{}]*\)\s*\{", clean, re.M)
    funcs = [f for f in funcs if f not in ("if", "for", "while", "switch")]
    if funcs:
        tree.append(_bn("Functions", f"{len(funcs)}", [_bn(f) for f in funcs]))
    incs = re.findall(r'#\s*include\s+[<"]([^>"]+)[>"]', src)
    if incs:
        nodes = []
        for i in incs:
            full = os.path.normpath(os.path.join(os.path.dirname(path), i))
            nodes.append(_bn(os.path.basename(i), i, None, full, os.path.exists(full)))
        tree.append(_bn("References", f"{len(incs)} includes", nodes))
    ver = re.search(r"#version\s+(\d+(?:\s+es)?)", src)
    if ver:
        info.append(("GLSL", ver.group(1)))
    style = "" if hlsl else glsl_style(src)
    kind = ("HLSL" if hlsl else "GLSL") + " shader"
    if style:
        info.append(("Runs live", "Shadertoy style" if style == "shadertoy" else "fragment shader"))
    elif not hlsl:
        stage = {".vert": "vertex", ".vs": "vertex", ".vsh": "vertex", ".geom": "geometry", ".gs": "geometry",
                 ".comp": "compute", ".tesc": "tessellation control", ".tese": "tessellation evaluation"}.get(ext)
        if stage:
            info.append(("Stage", stage))
    notes = []
    if hlsl:
        notes.append("HLSL is shown as source; only GLSL fragment shaders can run live here.")
    return {"kind": kind, "info": info, "tree": tree, "notes": notes, "materials": [], "code": (src, "glsl" if not hlsl else "c"),
            "glsl": {"source": src, "style": style} if style else None}


# ---- Unity: .mat (material), .shader (ShaderLab), .shadergraph
def _unity_mat_preview(path: str) -> dict:
    root = unity_project_root(path)
    guids, _ = unity_guid_index(root) if root else ({}, False)
    text = open(path, "r", encoding="utf-8", errors="replace").read()

    def asset(g):
        return _UNITY_BUILTIN_GUIDS.get(g) or guids.get(g, "")
    name = (re.search(r"^[ \t]*m_Name:[ \t]*(.*)$", text, re.M) or [None, ""])[1].strip() or os.path.splitext(os.path.basename(path))[0]
    sh = re.search(r"m_Shader:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-f]{32}))?", text)
    shader = ""
    if sh:
        g = sh.group(2) or ""
        if g in _UNITY_BUILTIN_GUIDS:
            shader = {"46": "Standard", "45": "Standard (Specular setup)", "7": "Legacy/Diffuse", "10703": "Sprites/Default",
                      "10750": "Unlit/Texture", "10720": "UI/Default"}.get(sh.group(1), f"built-in shader {sh.group(1)}")
        elif g:
            sp = guids.get(g, "")
            shader = os.path.basename(sp) if sp else f"shader {g[:8]}… (in a package that isn't installed here)"
    texs, floats, cols = {}, {}, {}
    sec = None
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s in ("m_TexEnvs:", "m_Floats:", "m_Colors:", "m_Ints:"):
            sec = s[:-1]
            continue
        if sec and re.match(r"^\s{4}\w", line) and not s.startswith("-"):
            sec = None
        if sec == "m_TexEnvs":
            mm = re.match(r"-\s*(\w+):\s*$", s)
            if mm:
                cur = mm.group(1)
                continue
            mm = re.match(r"m_Texture:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-f]{32}))?", s)
            if mm and cur and mm.group(2):
                texs[cur] = asset(mm.group(2)) or f"missing GUID {mm.group(2)}"
            mm = re.match(r"m_Scale:\s*\{x:\s*([-\d.e]+),\s*y:\s*([-\d.e]+)", s)
            if mm and cur in texs:
                texs[cur + "@scale"] = (float(mm.group(1)), float(mm.group(2)))
        elif sec in ("m_Floats", "m_Ints"):
            mm = re.match(r"-\s*(\w+):\s*([-\d.eE]+)", s)
            if mm:
                floats[mm.group(1)] = float(mm.group(2))
        elif sec == "m_Colors":
            mm = re.match(r"-\s*(\w+):\s*\{r:\s*([-\d.eE]+),\s*g:\s*([-\d.eE]+),\s*b:\s*([-\d.eE]+),\s*a:\s*([-\d.eE]+)", s)
            if mm:
                cols[mm.group(1)] = tuple(float(mm.group(i)) for i in range(2, 6))

    def full(rel):
        return os.path.join(root, rel) if root and rel and not rel.startswith(("Unity ", "missing")) else rel

    def usable(rel):
        return bool(rel) and not rel.startswith(("Unity ", "missing"))
    m = {"name": name}
    c = cols.get("_BaseColor") or cols.get("_Color")
    if c:
        m["base_color"] = tuple(_lin1(v) for v in c[:3])
        if c[3] < 0.999 and (floats.get("_Surface", 0) == 1 or floats.get("_Mode", 0) in (2, 3)):
            m["opacity"] = c[3]
    t = texs.get("_BaseMap") or texs.get("_MainTex") or texs.get("_BaseColorMap")
    if usable(t):
        m["base_color_tex"] = full(t)
        sc = texs.get(("_BaseMap" if "_BaseMap" in texs else "_MainTex") + "@scale")
        if sc:
            m["tiling"] = sc
    if "_Metallic" in floats:
        m["metallic"] = floats["_Metallic"]
    sm = floats.get("_Smoothness", floats.get("_Glossiness"))
    if sm is not None:
        m["roughness"] = 1 - sm
    mg = texs.get("_MetallicGlossMap") or texs.get("_MaskMap")
    if usable(mg):
        m["metallic_tex"] = (full(mg), "r", False)
        m["roughness_tex"] = (full(mg), "a", True)
        m["metallic"] = 1.0
    nm = texs.get("_BumpMap") or texs.get("_NormalMap")
    if usable(nm):
        m["normal_tex"] = full(nm)
    ec = cols.get("_EmissionColor")
    kw = " ".join(re.findall(r"^[ \t]*m_(?:ShaderKeywords|ValidKeywords):[ \t]*(.*)$", text, re.M))
    kw += " " + " ".join(re.findall(r"^[ \t]*-[ \t]*(_EMISSION)\b", text, re.M))
    if ec and any(v > 0.001 for v in ec[:3]) and ("_EMISSION" in kw or floats.get("_EmissionEnabled") == 1):
        m["emission"] = ec[:3]
    em = texs.get("_EmissionMap")
    if usable(em) and "emission" in m:
        m["emission_tex"] = full(em)
    tex_rows = []
    for k, v in sorted(texs.items()):
        if k.endswith("@scale"):
            continue
        p = full(v)
        tex_rows.append(_bn(k, os.path.basename(v), None, p if not v.startswith(("Unity ", "missing")) else "",
                            os.path.exists(p) if not v.startswith(("Unity ", "missing")) else (False if v.startswith("missing") else None)))
    miss = sum(1 for r in tex_rows if r[4] is False)
    tree = [_bn("Shader", shader or "?"),
            _bn("References", f"{len(tex_rows)} textures" + (f" · {miss} missing" if miss else ""), [_bn("Textures", f"{len(tex_rows)}", tex_rows)] if tex_rows else []),
            _bn("Colours", f"{len(cols)}", [_bn(k, ", ".join(f"{x:.3g}" for x in v)) for k, v in sorted(cols.items())]),
            _bn("Floats", f"{len(floats)}", [_bn(k, f"{v:g}") for k, v in sorted(floats.items())]),
            _bn("Ball preview uses", "", _mat_summary_nodes(m))]
    return {"kind": "Unity material", "info": [("Unity", unity_version(root) or "?"), ("Shader", shader or "?")],
            "tree": tree, "notes": [] if root else ["No Unity project found above this file: texture GUIDs can't be resolved."],
            "materials": [m], "code": None}


def _shaderlab_preview(path: str) -> dict:
    src = open(path, "r", encoding="utf-8", errors="replace").read()
    clean = _strip_c_comments(src)
    name = (re.search(r'Shader\s+"([^"]+)"', clean) or [None, "?"])[1]
    props = re.findall(r'(\[[^\]]*\]\s*)*(\w+)\s*\(\s*"([^"]*)"\s*,\s*([\w]+(?:\s*\([^)]*\))?)\s*\)\s*=\s*([^\n]+)', clean)
    rows = [_bn(n, f"{t} = {d.strip()}" + (f"  · {lab}" if lab and lab != n else "")) for _, n, lab, t, d in props]
    subs = len(re.findall(r"\bSubShader\b", clean))
    passes = re.findall(r"\bPass\s*\{(?:[^{}]|\{[^{}]*\})*?Name\s+\"([^\"]+)\"", clean)
    pragmas = sorted(set(re.findall(r"#pragma\s+(\w+\s*\w*)", clean)))
    tags = sorted(set(re.findall(r'"RenderPipeline"\s*=\s*"(\w+)"', clean)))
    m = {}
    for _, n, lab, t, d in props:
        if n in ("_Color", "_BaseColor") and t.startswith("Color"):
            c = _col3(d)
            if c:
                m["base_color"] = tuple(_lin1(v) for v in c)
        elif n == "_Metallic":
            f = _floats(d)
            m["metallic"] = f[0] if f else 0
        elif n in ("_Glossiness", "_Smoothness"):
            f = _floats(d)
            if f:
                m["roughness"] = 1 - f[0]
    tree = [_bn("Properties", f"{len(rows)}", rows), _bn("Sub-shaders", f"{subs}"),
            _bn("Named passes", f"{len(passes)}", [_bn(p) for p in passes])]
    if pragmas:
        tree.append(_bn("Pragmas", f"{len(pragmas)}", [_bn(p) for p in pragmas]))
    incs = re.findall(r'#include\s+"([^"]+)"', src)
    if incs:
        tree.append(_bn("Includes", f"{len(incs)}", [_bn(os.path.basename(i), i) for i in incs]))
    info = [("Shader", name), ("Properties", f"{len(rows)}")]
    if tags:
        info.append(("Pipeline", ", ".join(tags)))
    return {"kind": "Unity shader (ShaderLab)", "info": info, "tree": tree, "notes": [],
            "materials": [m] if m else [], "code": (src, "c")}


def _shadergraph_preview(path: str) -> dict:
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    objs = []
    dec = json.JSONDecoder()
    i = 0
    while i < len(raw):
        while i < len(raw) and raw[i] in " \r\n\t":
            i += 1
        if i >= len(raw):
            break
        try:
            o, j = dec.raw_decode(raw, i)
        except ValueError:
            break
        objs.append(o)
        i = j
    props, node_types, targets = [], {}, []
    for o in objs:
        t = o.get("m_Type", "")
        if t.endswith("ShaderProperty"):
            props.append((o.get("m_Name", "?"), t.split(".")[-1].replace("ShaderProperty", ""), o.get("m_Value")))
        elif t.endswith("Node") or ".Nodes." in t:
            nt = t.split(".")[-1]
            node_types[nt] = node_types.get(nt, 0) + 1
        elif "Target" in t and "SubTarget" not in t:
            targets.append(t.split(".")[-1])
        for sp in o.get("m_SerializedProperties", []):                      # pre-2020 graphs
            try:
                d = json.loads(sp.get("JSONnodeData", "{}"))
                props.append((d.get("m_Name", "?"), sp.get("typeInfo", {}).get("fullName", "").split(".")[-1].replace("ShaderProperty", ""), d.get("m_Value")))
            except ValueError:
                pass
        for sn in o.get("m_SerializableNodes", []):
            nt = sn.get("typeInfo", {}).get("fullName", "?").split(".")[-1]
            node_types[nt] = node_types.get(nt, 0) + 1

    def fmt(v):
        if isinstance(v, dict):
            if "r" in v:
                return ", ".join(f"{v.get(k, 0):.3g}" for k in "rgba")
            if "x" in v:
                return ", ".join(f"{v.get(k, 0):.3g}" for k in "xyzw" if k in v)
            if "m_SerializedTexture" in v or "m_SerializedTexture" in json.dumps(v):
                return "texture"
            return ""
        return f"{v}" if v is not None else ""
    tree = [_bn("Properties", f"{len(props)}", [_bn(n, f"{t}" + (f" = {fmt(v)}" if fmt(v) else "")) for n, t, v in props]),
            _bn("Nodes", f"{sum(node_types.values())}", _bn_counts(node_types))]
    if targets:
        tree.append(_bn("Targets", "", [_bn(t) for t in targets]))
    return {"kind": "Unity Shader Graph", "info": [("Properties", f"{len(props)}"), ("Nodes", f"{sum(node_types.values())}")]
            + ([("Targets", ", ".join(targets))] if targets else []), "tree": tree, "notes": [], "materials": [], "code": None}


# --------------------------------------------------------------------------- IES photometric files (IESNA LM-63)
class IESProfile:
    """Candela table of an .ies file: vertical angles (0 = straight down), horizontal angles, candela[h][v]
    (multiplier and ballast factor applied), keywords, lumens, watts and the luminous-opening size."""

    def __init__(self, path: str):
        with open(path, "r", encoding="latin-1", errors="replace") as f:
            text = f.read()
        lines = text.splitlines()
        self.header = lines[0].strip() if lines else ""
        self.keywords: Dict[str, str] = {}
        i = 0
        last = None
        while i < len(lines):
            ln = lines[i].strip()
            if ln.upper().startswith("TILT"):
                break
            m = re.match(r"\[(\w+)\]\s*(.*)", ln)
            if m:
                k = m.group(1).upper()
                if k == "MORE" and last:
                    self.keywords[last] += " " + m.group(2).strip()
                else:
                    self.keywords[k] = (self.keywords.get(k, "") + " " + m.group(2).strip()).strip()
                    last = k
            i += 1
        if i >= len(lines):
            raise ValueError("no TILT line: not an IES file")
        tilt = lines[i].split("=", 1)[1].strip().upper() if "=" in lines[i] else "NONE"
        nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", " ".join(lines[i + 1:]))]
        p = 0
        if tilt == "INCLUDE":
            p += 1                                              # lamp-to-luminaire geometry
            n = int(nums[p])
            p += 1 + 2 * n                                      # angles + multiplying factors
        self.tilt = tilt
        (self.n_lamps, self.lumens_per_lamp, mult, nv, nh, self.photometric_type, self.units,
         self.width, self.length, self.height) = nums[p:p + 10]
        p += 10
        ballast, _, self.watts = nums[p:p + 3]
        p += 3
        nv, nh = int(nv), int(nh)
        if not (0 < nv < 2000 and 0 < nh < 2000):
            raise ValueError("implausible angle counts")
        self.v = np.array(nums[p:p + nv], np.float64)
        p += nv
        self.h = np.array(nums[p:p + nh], np.float64)
        p += nh
        cd = np.array(nums[p:p + nv * nh], np.float64)
        if len(cd) < nv * nh:
            raise ValueError("candela table is truncated")
        self.cd = cd.reshape(nh, nv) * mult * (ballast or 1.0)
        self.multiplier = mult

    # ---- lookups
    def candela(self, vert, horiz):
        """Bilinear candela for arrays of vertical (0..180, 0 = down) and horizontal (0..360) angles, with the
        file's symmetry expanded (single plane, quadrant 0–90, bilateral 0–180, full 0–360)."""
        h = np.mod(horiz, 360.0)
        hmax = self.h[-1]
        if len(self.h) == 1:
            h = np.zeros_like(h)
        elif hmax <= 90.0 + 1e-6:
            h = np.where(h > 180, 360 - h, h)
            h = np.where(h > 90, 180 - h, h)
        elif hmax <= 180.0 + 1e-6:
            h = np.where(h > 180, 360 - h, h)
        if self.h[0] >= 90.0 - 1e-6 and hmax <= 270 + 1e-6 and len(self.h) > 1:       # 90–270 (type B/C variants)
            h = np.clip(h, self.h[0], hmax)
        v = np.clip(vert, self.v[0], self.v[-1])
        vi = np.clip(np.searchsorted(self.v, v) - 1, 0, len(self.v) - 2) if len(self.v) > 1 else np.zeros_like(v, int)
        if len(self.v) > 1:
            v0, v1 = self.v[vi], self.v[vi + 1]
            fv = np.where(v1 > v0, (v - v0) / np.maximum(v1 - v0, 1e-9), 0.0)
        else:
            fv = np.zeros_like(v)
        out_v = np.clip(vert, 0, 180)
        outside = (out_v < self.v[0]) | (out_v > self.v[-1])
        if len(self.h) > 1:
            hi = np.clip(np.searchsorted(self.h, h) - 1, 0, len(self.h) - 2)
            h0, h1 = self.h[hi], self.h[hi + 1]
            fh = np.where(h1 > h0, (h - h0) / np.maximum(h1 - h0, 1e-9), 0.0)
        else:
            hi = np.zeros_like(v, int)
            fh = np.zeros_like(v)
        c = self.cd
        if len(self.v) > 1:
            a = c[hi, vi] * (1 - fv) + c[hi, vi + 1] * fv
            b = c[np.minimum(hi + 1, len(self.h) - 1), vi] * (1 - fv) + c[np.minimum(hi + 1, len(self.h) - 1), vi + 1] * fv
        else:
            a = c[hi, 0]
            b = c[np.minimum(hi + 1, len(self.h) - 1), 0]
        val = a * (1 - fh) + b * fh
        return np.where(outside, 0.0, val)

    def summary(self) -> dict:
        peak = float(self.cd.max()) if self.cd.size else 0.0
        # beam (50 %) and field (10 %) angles in the C0 plane, as full cone angles
        prof = self.cd[0]
        beam = field = None
        if peak > 0 and len(self.v) > 1 and self.v[0] <= 0.5:
            down = self.v <= 90.0
            pk = prof[down].max() if down.any() else 0
            for frac, name in ((0.5, "beam"), (0.1, "field")):
                idx = np.nonzero((prof < frac * pk) & down)[0]
                if len(idx) and pk > 0 and prof[0] >= 0.5 * pk:   # only for lights whose peak points down
                    ang = 2 * float(self.v[idx[0]])
                    if name == "beam":
                        beam = ang
                    else:
                        field = ang
        # luminous flux of the table (sum of candela × solid angle), for files that state -1 lumens
        vv = np.radians(np.clip(self.v, 0, 180))
        dv = np.gradient(vv) if len(vv) > 1 else np.array([math.pi])
        if len(self.h) > 1:
            span = np.radians(self.h[-1] - self.h[0])
            rep = 2 * math.pi / max(span, 1e-6) if span > 0 else 1.0
        else:
            rep = 1.0
        mean_cd = self.cd.mean(axis=0)
        flux = float((mean_cd * np.sin(vv) * dv).sum() * 2 * math.pi) if len(vv) > 1 else 0.0
        return {"peak": peak, "beam": beam, "field": field, "flux": flux,
                "symmetry": ("rotationally symmetric" if len(self.h) == 1 else
                             "quadrant symmetric" if self.h[-1] <= 90 else
                             "bilateral" if self.h[-1] <= 180 else "full 360°")}


def _ies_wall(prof: IESProfile, size: int = 300):
    """Light hitting a wall: a fixture 0.25 units in front of the wall near the top, aimed down."""
    S = size
    ys, xs = np.mgrid[0:S, 0:S].astype(np.float64)
    wx = (xs + 0.5) / S * 4 - 2                                  # wall 4 × 4 units
    wy = 0.6 - (ys + 0.5) / S * 4
    d = 0.35
    vx, vy, vz = wx, wy, np.full_like(wx, -d)                   # light at origin, wall plane z = -d
    r2 = vx * vx + vy * vy + vz * vz
    r = np.sqrt(r2)
    vert = np.degrees(np.arccos(np.clip(-vy / r, -1, 1)))         # 0 = straight down
    horiz = np.degrees(np.arctan2(vz, vx)) % 360                  # C-plane around the vertical axis
    cd = prof.candela(vert, horiz)
    e = cd * (d / r) / r2                                         # cosine law on the wall / inverse square
    e = e / max(float(np.percentile(e, 99.7)), 1e-9)
    t = 1 - np.exp(-2.2 * e)
    warm = np.array([1.0, 0.86, 0.66])
    base = np.array([0.045, 0.045, 0.05])
    img = base + t[..., None] * warm * 0.95
    floor = (wy < -3.0)
    img[floor] *= 0.6
    srgb = np.where(img <= 0.0031308, img * 12.92, 1.055 * np.power(np.clip(img, 0, 1), 1 / 2.4) - 0.055)
    return (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def _draw_line(img, x0, y0, x1, y1, col, width=1.2):
    """Anti-aliased polyline segment on an H×W×3 float image (distance-based coverage)."""
    h, w = img.shape[:2]
    xa, xb = int(max(0, min(x0, x1) - width - 1)), int(min(w - 1, max(x0, x1) + width + 1))
    ya, yb = int(max(0, min(y0, y1) - width - 1)), int(min(h - 1, max(y0, y1) + width + 1))
    if xb < xa or yb < ya:
        return
    ys, xs = np.mgrid[ya:yb + 1, xa:xb + 1].astype(np.float64)
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / L2, 0, 1) if L2 > 0 else np.zeros_like(xs)
    dist = np.hypot(xs - (x0 + t * dx), ys - (y0 + t * dy))
    a = np.clip(width / 2 + 0.5 - dist, 0, 1)[..., None]
    img[ya:yb + 1, xa:xb + 1] = img[ya:yb + 1, xa:xb + 1] * (1 - a) + np.array(col) * a


def _ies_polar(prof: IESProfile, size: int = 300):
    """Polar candela diagram: C0–C180 plane (orange) and C90–C270 plane (blue), down = 0°."""
    S = size
    img = np.empty((S, S, 3), np.float64)
    img[:] = (0.075, 0.075, 0.085)
    cx, cy, R = S / 2, S / 2, S * 0.44
    grid = (0.22, 0.22, 0.25)
    for k in (0.25, 0.5, 0.75, 1.0):                             # rings
        n = 96
        for i in range(n):
            a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            _draw_line(img, cx + R * k * math.cos(a0), cy + R * k * math.sin(a0), cx + R * k * math.cos(a1), cy + R * k * math.sin(a1), grid, 1.0)
    for deg in range(0, 360, 30):                                # spokes
        a = math.radians(deg)
        _draw_line(img, cx, cy, cx + R * math.cos(a), cy + R * math.sin(a), grid, 1.0)
    peak = max(float(prof.cd.max()), 1e-9)
    angles = np.linspace(0, 180, 181)
    for h_left, h_right, col in ((180, 0, (1.0, 0.62, 0.25)), (270, 90, (0.35, 0.65, 1.0))):
        for side, hh in ((1, h_right), (-1, h_left)):
            cd = prof.candela(angles, np.full_like(angles, hh)) / peak
            pts = [(cx + side * R * c * math.sin(math.radians(a)), cy + R * c * math.cos(math.radians(a))) for a, c in zip(angles, cd)]
            for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
                _draw_line(img, x0, y0, x1, y1, col, 1.8)
    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)


def ies_preview(path: str) -> dict:
    prof = IESProfile(path)
    s = prof.summary()
    wall = _ies_wall(prof, 300)
    polar = _ies_polar(prof, 300)
    gap = np.full((300, 8, 3), 30, np.uint8)
    both = np.concatenate([wall, gap, polar], 1)
    img = RawImage(both.shape[1], both.shape[0], np.ascontiguousarray(both).tobytes())
    kw = prof.keywords
    lumens = prof.lumens_per_lamp * prof.n_lamps
    info = [("Standard", prof.header[:40])]
    for k, label in (("MANUFAC", "Manufacturer"), ("LUMCAT", "Catalogue no."), ("LUMINAIRE", "Luminaire"), ("LAMP", "Lamp"),
                     ("LAMPCAT", "Lamp catalogue")):
        if kw.get(k):
            info.append((label, kw[k][:80]))
    info.append(("Lumens", f"{lumens:,.0f}" if lumens > 0 else f"absolute photometry (≈{s['flux']:,.0f} lm from the table)"))
    if prof.watts:
        info.append(("Input power", f"{prof.watts:g} W" + (f" · {lumens / prof.watts:.0f} lm/W" if lumens > 0 and prof.watts else "")))
    info.append(("Peak intensity", f"{s['peak']:,.0f} cd"))
    if s["beam"]:
        info.append(("Beam / field angle", f"{s['beam']:.0f}° / {s['field']:.0f}°" if s["field"] else f"{s['beam']:.0f}°"))
    info.append(("Distribution", s["symmetry"] + {1: " · type C", 2: " · type B", 3: " · type A"}.get(int(prof.photometric_type), "")))
    tree = [_bn("Keywords", f"{len(kw)}", [_bn(k, v) for k, v in kw.items()]),
            _bn("Photometry", "", [_bn("Vertical angles", f"{len(prof.v)} · {prof.v[0]:g}° – {prof.v[-1]:g}°"),
                                   _bn("Horizontal angles", f"{len(prof.h)} · {prof.h[0]:g}° – {prof.h[-1]:g}°"),
                                   _bn("Candela multiplier", f"{prof.multiplier:g}"),
                                   _bn("Luminous opening", f"{prof.width:g} × {prof.length:g} × {prof.height:g} {'ft' if int(prof.units) == 1 else 'm'}"),
                                   _bn("Tilt", prof.tilt)])]
    return {"kind": "IES light profile", "info": info, "tree": tree,
            "notes": ["Left: the light on a wall from a fixture just in front of it. Right: candela in the C0–C180 (orange) and C90–C270 (blue) planes, 0° pointing down."],
            "images": [("Light pattern · polar diagram", img)], "materials": [], "code": None}


# --------------------------------------------------------------------------- image bytes, dispatch
def decode_image_bytes(data: bytes) -> Optional["RawImage"]:
    """PNG / JPEG bytes (thumbnails stored inside files) → RawImage."""
    if not data:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        w, h, n, rs = pb.get_width(), pb.get_height(), pb.get_n_channels(), pb.get_rowstride()
        buf = np.frombuffer(pb.get_pixels(), np.uint8)
        a = np.lib.stride_tricks.as_strided(buf, (h, w, n), (rs, n, 1))
        rgb = a[..., :3].astype(np.float32)
        if n == 4:                                            # over the preview's dark backdrop
            al = a[..., 3:4].astype(np.float32) / 255.0
            rgb = rgb * al + 42.0 * (1 - al)
        return RawImage(w, h, np.ascontiguousarray(rgb.astype(np.uint8)).tobytes())
    except Exception:
        try:
            from PIL import Image as _PILImage
            im = _PILImage.open(io.BytesIO(data)).convert("RGB")
            return RawImage(im.width, im.height, im.tobytes())
        except Exception:
            return None


SHADER_PREVIEW_EXTS = {".mtlx": "materialx", ".osl": "osl", ".oso": "osl", ".shader": "unity-shader",
                       ".shadergraph": "unity-graph", ".shadersubgraph": "unity-graph", ".mat": "unity-mat", ".ies": "ies"}
SHADER_PREVIEW_EXTS.update({e: "source" for e in _GLSL_EXTS + _HLSL_EXTS})


def shader_preview_kind(path: str) -> str:
    """What the shader / material / light-profile preview would show for this file ('' = not ours)."""
    ext = os.path.splitext(path)[1].lower()
    k = SHADER_PREVIEW_EXTS.get(ext, "")
    if k in ("unity-mat", "unity-shader", "ies", "source"):
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return ""
        if k == "unity-mat" and not head.startswith(b"%YAML"):
            return ""                                         # MATLAB .mat and friends
        if k == "unity-shader" and b"Shader" not in head:
            return ""
        if k == "ies" and b"TILT" not in head.upper() and not head.upper().startswith(b"IESNA"):
            return ""
        if k == "source" and (b"\0" in head or head.lstrip().startswith(b"%!")):
            return ""                                         # compiled blobs, PostScript (.ps)
        if k == "source" and not re.search(rb"\b(?:[biud]?vec[234]|mat[234]|float[234]|half[234]|fixed[234]|uniform|gl_\w+|SV_\w+|cbuffer|"
                                           rb"Texture2D|sampler\w*|layout\s*\()|#\s*(?:version|pragma|include|define)", head):
            return ""                                         # F# (.fs), Apps Script (.gs)… that only share the extension
    return k


def shader_preview(path: str) -> dict:
    """Worker entry point: breakdown + material ball / light pattern / code / live GLSL for one file."""
    t0 = time.monotonic()
    k = shader_preview_kind(path)
    fn = {"materialx": _mtlx_preview, "osl": _osl_preview, "unity-shader": _shaderlab_preview,
          "unity-graph": _shadergraph_preview, "unity-mat": _unity_mat_preview, "ies": ies_preview,
          "source": _shader_source_preview}.get(k)
    if fn is None:
        return {"error": "not a shader or material file Kestrel can read"}
    try:
        res = fn(path)
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}
    res.setdefault("images", [])
    if not res.get("code") and k in ("materialx", "unity-mat", "unity-graph", "ies"):
        try:
            if os.path.getsize(path) <= (2 << 20):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    res["code"] = (f.read(), {"materialx": "xml", "unity-mat": "yaml", "unity-graph": "json"}.get(k, ""))
        except OSError:
            pass
    if res.get("materials") and not res["images"] and np is not None:
        try:
            res["images"].append((res["materials"][0].get("name") or "Material", render_material_ball(res["materials"][0])))
        except Exception as ex:
            res.setdefault("notes", []).append(f"The material ball couldn't be drawn ({type(ex).__name__}: {ex}).")
    res["seconds"] = time.monotonic() - t0
    return res


def material_ball_for(mat: dict) -> Optional["RawImage"]:
    """Worker entry point for a material picked in a scene breakdown: its stored preview, else a rendered ball."""
    if mat.get("_image") is not None:
        return mat["_image"]
    return render_material_ball(mat) if np is not None else None


# --------------------------------------------------------------------------- live GLSL (Shadertoy / glslsandbox)
_GLSL_VS = """
out vec2 _uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    _uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
_SHADERTOY_HEAD = """
uniform vec3 iResolution;
uniform float iTime;
uniform float iTimeDelta;
uniform float iFrameRate;
uniform int iFrame;
uniform vec4 iMouse;
uniform vec4 iDate;
uniform float iSampleRate;
uniform vec3 iChannelResolution[4];
uniform float iChannelTime[4];
uniform sampler2D iChannel0;
uniform sampler2D iChannel1;
uniform sampler2D iChannel2;
uniform sampler2D iChannel3;
in vec2 _uv;
out vec4 _kestrelOut;
#define texture2D texture
"""
_SHADERTOY_TAIL = """
void main() {
    vec4 c = vec4(0.0, 0.0, 0.0, 1.0);
    mainImage(c, gl_FragCoord.xy);
    _kestrelOut = vec4(c.rgb, 1.0);
}
"""


def glsl_build_fragment(src: str, style: str, header: str) -> Tuple[str, int]:
    """User source → complete fragment shader for this context, plus how many lines were added before the
    user's first line. A `#line 1` directive keeps compile-error line numbers matching the user's file."""
    body = re.sub(r"^[ \t]*#[ \t]*version[^\n]*", "", src, count=1, flags=re.M)
    if style == "shadertoy":
        pre = header + _SHADERTOY_HEAD
        tail = _SHADERTOY_TAIL
    else:
        pre = header + "in vec2 _uv;\n#define texture2D texture\n#define varying in\n"
        if not re.search(r"^\s*(?:layout\s*\([^)]*\)\s*)?out\s+(?:(?:lowp|mediump|highp)\s+)?vec4\s+\w+\s*;", _strip_c_comments(body), re.M):
            pre += "out vec4 _kestrelOut;\n#define gl_FragColor _kestrelOut\n"
        tail = ""
    # GLSL < 3.30 numbers the line after "#line N" as N+1; ES and 3.30+ as N.
    line = "#line 1\n" if (" es" in header or re.search(r"#version\s+(3[3-9]\d|[4-9]\d\d)", header)) else "#line 0\n"
    return pre + line + body + "\n" + tail, pre.count("\n") + 1


def glsl_error_text(ex) -> str:
    """PyOpenGL's ShaderCompilationError → just the driver's info log, one message per line."""
    msg = str(ex.args[0]) if getattr(ex, "args", None) else str(ex)
    m = re.search(r"""b(['"])(.*)\1""", msg, re.S)
    log = m.group(2).replace("\\n", "\n").replace("\\t", "  ").replace("\\'", "'") if m else msg
    return "\n".join(l.rstrip() for l in log.strip().splitlines() if l.strip())[:3000]


class GLSLRunner:
    """Compiles and draws one fragment shader full-screen (any GL context, no GTK). Uniforms are set by
    name: Shadertoy (iTime, iResolution, iMouse, iFrame, iDate, iChannel0-3) and glslsandbox / Book of
    Shaders (time, resolution, mouse, u_time, u_resolution, u_mouse)."""

    def __init__(self, header: str):
        self.hdr = header
        self.prog = None
        self.error = ""
        self.vao = _GL.glGenVertexArrays(1)
        self.tex = None
        self.uniforms: Dict[str, Tuple[int, int]] = {}

    def compile(self, src: str, style: str) -> bool:
        self.free_program()
        frag, offset = glsl_build_fragment(src, style, self.hdr)
        try:
            vs = _GLshaders.compileShader(self.hdr + _GLSL_VS, _GL.GL_VERTEX_SHADER)
            fs = _GLshaders.compileShader(frag, _GL.GL_FRAGMENT_SHADER)
            self.prog = _GLshaders.compileProgram(vs, fs)
        except Exception as ex:
            self.error = glsl_error_text(ex)
            self.prog = None
            return False
        self.error = ""
        self.uniforms = {}
        try:
            n = _GL.glGetProgramiv(self.prog, _GL.GL_ACTIVE_UNIFORMS)
            for i in range(int(n)):
                name, size, typ = _GL.glGetActiveUniform(self.prog, i)
                name = name.decode() if isinstance(name, bytes) else str(name)
                self.uniforms[name.split("[")[0]] = (int(typ), _GL.glGetUniformLocation(self.prog, name.split("[")[0]))
        except Exception:
            for nm in ("iTime", "iResolution", "iMouse", "iFrame", "iDate", "iTimeDelta", "iFrameRate", "time", "resolution",
                       "mouse", "u_time", "u_resolution", "u_mouse", "iChannel0", "iChannel1", "iChannel2", "iChannel3"):
                loc = _GL.glGetUniformLocation(self.prog, nm)
                if loc >= 0:
                    self.uniforms[nm] = (0, loc)
        return True

    def _noise_texture(self):
        if self.tex is None:
            rng = np.random.default_rng(7)
            data = np.ascontiguousarray(rng.integers(0, 256, (256, 256, 4), dtype=np.uint8))
            self.tex = _GL.glGenTextures(1)
            _GL.glBindTexture(_GL.GL_TEXTURE_2D, self.tex)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_MIN_FILTER, _GL.GL_LINEAR)
            _GL.glTexParameteri(_GL.GL_TEXTURE_2D, _GL.GL_TEXTURE_MAG_FILTER, _GL.GL_LINEAR)
            _GL.glTexImage2D(_GL.GL_TEXTURE_2D, 0, _GL.GL_RGBA8, 256, 256, 0, _GL.GL_RGBA, _GL.GL_UNSIGNED_BYTE, data)
        return self.tex

    def draw(self, W: int, H: int, t: float, frame: int, mouse=(0.0, 0.0, 0.0, 0.0), target_fb: int = 0):
        _GL.glBindFramebuffer(_GL.GL_FRAMEBUFFER, target_fb)
        _GL.glViewport(0, 0, W, H)
        _GL.glDisable(_GL.GL_DEPTH_TEST)
        _GL.glDisable(_GL.GL_BLEND)
        if self.prog is None:
            _GL.glClearColor(0.08, 0.08, 0.09, 1.0)
            _GL.glClear(_GL.GL_COLOR_BUFFER_BIT)
            return
        _GL.glUseProgram(self.prog)
        u = self.uniforms
        GL_FLOAT_VEC2, GL_FLOAT_VEC3, GL_FLOAT_VEC4 = 0x8B50, 0x8B51, 0x8B52

        def setf(names, *vals):
            for nm in names:
                if nm in u:
                    typ, loc = u[nm]
                    n = {GL_FLOAT_VEC2: 2, GL_FLOAT_VEC3: 3, GL_FLOAT_VEC4: 4}.get(typ, len(vals))
                    v = (list(vals) + [0.0, 0.0, 0.0])[:n]
                    (_GL.glUniform1f, _GL.glUniform2f, _GL.glUniform3f, _GL.glUniform4f)[n - 1](loc, *v)
        setf(("iTime", "time", "u_time"), t)
        setf(("iTimeDelta",), 1 / 60.0)
        setf(("iFrameRate",), 60.0)
        setf(("iResolution",), float(W), float(H), 1.0)
        setf(("resolution", "u_resolution"), float(W), float(H))
        setf(("iMouse",), *mouse)
        setf(("mouse", "u_mouse"), mouse[0] / max(W, 1), mouse[1] / max(H, 1))
        lt = time.localtime()
        setf(("iDate",), float(lt.tm_year), float(lt.tm_mon - 1), float(lt.tm_mday), float(lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec))
        if "iFrame" in u:
            _GL.glUniform1i(u["iFrame"][1], int(frame))
        tex = None
        for i in range(4):
            if f"iChannel{i}" in u:
                tex = tex or self._noise_texture()
                _GL.glActiveTexture(_GL.GL_TEXTURE0 + i)
                _GL.glBindTexture(_GL.GL_TEXTURE_2D, tex)
                _GL.glUniform1i(u[f"iChannel{i}"][1], i)
        _GL.glBindVertexArray(self.vao)
        _GL.glDrawArrays(_GL.GL_TRIANGLES, 0, 3)
        _GL.glBindVertexArray(0)
        _GL.glUseProgram(0)
        _GL.glActiveTexture(_GL.GL_TEXTURE0)

    def free_program(self):
        if self.prog:
            try:
                _GL.glDeleteProgram(self.prog)
            except Exception:
                pass
        self.prog = None

    def free(self):
        self.free_program()
        try:
            _GL.glDeleteVertexArrays(1, [self.vao])
            if self.tex:
                _GL.glDeleteTextures(1, [self.tex])
        except Exception:
            pass


class GLSLView(Gtk.GLArea):
    """Runs a fragment shader live in the preview pane. Animates while visible and the window is active;
    click (or drag) feeds iMouse; double-click restarts the clock."""

    def __init__(self):
        super().__init__()
        self.set_auto_render(False)
        self.set_has_depth_buffer(False)
        self.runner: Optional[GLSLRunner] = None
        self.src, self.style = "", ""
        self.dirty = False
        self.t0 = time.monotonic()
        self.frame = 0
        self.playing = True
        self.mouse = [0.0, 0.0, 0.0, 0.0]
        self.on_status = None                # callable(error text or "") set by the window
        self.flavour = ""
        self._tick = None
        self.connect("create-context", self._create_context)
        self.connect("realize", self._realize)
        self.connect("unrealize", self._unrealize)
        self.connect("render", self._render)
        self.connect("map", lambda w: self._start() if self.playing else None)
        self.connect("unmap", lambda w: self._stop())
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)

    def _create_context(self, area):
        ctx, label = gl_try_contexts(self.get_window(), [])
        self.flavour = label or ""
        return ctx

    def _realize(self, w):
        self.make_current()
        if self.get_error() is not None or self.get_context() is None:
            if self.on_status:
                GLib.idle_add(lambda: (self.on_status("OpenGL isn't available here, so the shader can't run live."), False)[1])
            return
        try:
            self.runner = GLSLRunner(_gl_header(self.get_context()))
        except Exception as ex:
            if self.on_status:
                msg = f"OpenGL error: {ex}"
                GLib.idle_add(lambda: (self.on_status(msg), False)[1])
            return
        self.dirty = bool(self.src)
        self._start()

    def _unrealize(self, w):
        self._stop()
        self.make_current()
        if self.runner:
            self.runner.free()
        self.runner = None

    def load(self, src: str, style: str):
        self.src, self.style = src, style
        self.t0 = time.monotonic()
        self.frame = 0
        self.dirty = True
        self.playing = True
        self.queue_render()
        self._start()

    def set_playing(self, on: bool):
        self.playing = on
        if on:
            self.t0 = time.monotonic() - getattr(self, "_paused_at", 0.0)
            self._start()
        else:
            self._paused_at = time.monotonic() - self.t0
            self._stop()

    def _start(self):
        if self._tick is None and self.get_realized():
            self._tick = self.add_tick_callback(self._on_tick)

    def _stop(self):
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None

    def _on_tick(self, widget, clock):
        top = self.get_toplevel()
        active = top.is_active() if isinstance(top, Gtk.Window) else True
        if self.playing and active and self.get_mapped():
            self.queue_render()
        return True

    def _render(self, area, ctx):
        if self.runner is None:
            return True
        if self.dirty:
            self.dirty = False
            ok = self.runner.compile(self.src, self.style)
            if self.on_status:
                self.on_status("" if ok else self.runner.error)
        scale = self.get_scale_factor()
        W, H = max(1, self.get_allocated_width() * scale), max(1, self.get_allocated_height() * scale)
        fb = int(np.asarray(_GL.glGetIntegerv(_GL.GL_DRAW_FRAMEBUFFER_BINDING)).reshape(-1)[0])
        t = (time.monotonic() - self.t0) if self.playing else getattr(self, "_paused_at", 0.0)
        self.runner.draw(W, H, t, self.frame, tuple(self.mouse), fb)
        self.frame += 1
        return True

    def _xy(self, event):
        s = self.get_scale_factor()
        return event.x * s, (self.get_allocated_height() - event.y) * s

    def _press(self, w, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.t0 = time.monotonic()
            self.frame = 0
            return True
        x, y = self._xy(event)
        self.mouse = [x, y, x, y]
        self.queue_render()
        return True

    def _release(self, w, event):
        self.mouse[2], self.mouse[3] = -abs(self.mouse[2]), -abs(self.mouse[3])
        return True

    def _motion(self, w, event):
        if event.state & Gdk.ModifierType.BUTTON1_MASK:
            x, y = self._xy(event)
            self.mouse[0], self.mouse[1] = x, y
            self.queue_render()
        return True
