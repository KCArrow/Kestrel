

# =========================================================================== extended previews
# Everything here runs on a worker thread and returns plain data (RGB bytes or text); the main thread
# turns it into a GdkPixbuf. Pure-Python decoders cover the VFX formats GdkPixbuf has no loader for
# (Maya IFF, OpenEXR, Radiance HDR); external tools, when installed, cover the long tail.

import struct
import zlib
import math
import tempfile

_HALF_TABLE: Optional[List[float]] = None


def _half_table() -> List[float]:
    """65536-entry lookup for IEEE half → float (struct supports 'e' since Python 3.6)."""
    global _HALF_TABLE
    if _HALF_TABLE is None:
        _HALF_TABLE = [struct.unpack("<e", struct.pack("<H", i))[0] for i in range(65536)]
    return _HALF_TABLE


class RawImage:
    """Decoded preview: 8-bit RGB bytes, row-major, plus metadata for the quick-view card."""

    def __init__(self, w: int, h: int, rgb: bytes, info: Optional[List[Tuple[str, str]]] = None):
        self.w, self.h, self.rgb, self.info = w, h, rgb, info or []


def _tonemap(samples: List[Tuple[float, float, float]], w: int, h: int, exposure: Optional[float] = None) -> bytes:
    """Linear float RGB → display sRGB-ish bytes with auto exposure (geometric-mean key) and gamma 2.2."""
    if exposure is None:
        lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in samples]
        pos = [l for l in lum if l > 1e-6]
        if pos:
            logmean = math.exp(sum(math.log(l) for l in pos[::max(1, len(pos) // 4000)]) / max(1, len(pos[::max(1, len(pos) // 4000)])))
            exposure = 0.18 / logmean if logmean > 0 else 1.0
            exposure = max(0.02, min(exposure, 64.0))
        else:
            exposure = 1.0
    inv = 1.0 / 2.2
    out = bytearray(w * h * 3)
    i = 0
    for r, g, b in samples:
        for c in (r, g, b):
            v = c * exposure
            v = v / (1.0 + v)                 # Reinhard, keeps highlights from clipping
            v = 0.0 if not (v > 0) else (1.0 if v >= 1 else v ** inv)      # 'not (v > 0)' also catches NaN
            out[i] = int(v * 255 + 0.5)
            i += 1
    return bytes(out)


def _tonemap_np(rgb, exposure: Optional[float] = None) -> bytes:
    """numpy version of _tonemap: rgb is an (N, 3) float array of linear values."""
    rgb = np.nan_to_num(rgb.astype(np.float32), nan=0.0, posinf=1e4, neginf=0.0)
    if exposure is None:
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        pos = lum[lum > 1e-6]
        if len(pos):
            logmean = float(np.exp(np.log(pos[::max(1, len(pos) // 20000)]).mean()))
            exposure = max(0.02, min(0.18 / logmean if logmean > 0 else 1.0, 64.0))
        else:
            exposure = 1.0
    v = rgb * np.float32(exposure)
    v = v / (1.0 + v)
    v = np.clip(v, 0.0, 1.0) ** np.float32(1 / 2.2)
    return (v * 255 + 0.5).astype(np.uint8).tobytes()


def _decode_exr_np(data, offsets, compression, lines_per, line_bytes, chan_off, rgb_names, sizes, w, h, ymin, ymax, stride, ow, oh, names):
    """Vectorised scanline EXR decode: per chunk, zlib/RLE → un-delta (cumsum) → interleave (reshape) → view
    the needed channels as float16/float32/uint32 → sample every `stride`-th column. ~50× the pure-Python path."""
    import zlib
    rows = []
    dt_for = {1: "<f2", 2: "<f4", 0: "<u4"}
    for bi, o in enumerate(offsets):
        y0 = struct.unpack_from("<i", data, o)[0]
        size = struct.unpack_from("<i", data, o + 4)[0]
        nlines = min(lines_per, ymax - y0 + 1)
        want = [k for k in range(nlines) if ((y0 - ymin) + k) % stride == 0]
        if not want:
            continue
        raw = data[o + 8:o + 8 + size]
        expected = nlines * line_bytes
        if size < expected and compression in (1, 2, 3):
            dec = zlib.decompress(raw) if compression in (2, 3) else _exr_rle(raw)
            d = np.frombuffer(dec, dtype=np.uint8).astype(np.int32)
            und = ((np.cumsum(d - 128) + 128) & 255).astype(np.uint8)       # un-delta
            n = len(und)
            half_n = (n + 1) // 2
            buf = np.empty(n, dtype=np.uint8)
            buf[0::2] = und[:half_n]
            buf[1::2] = und[half_n:half_n + n // 2]                            # interleave
        else:
            buf = np.frombuffer(raw, dtype=np.uint8)
        for k in want:
            base = k * line_bytes
            chans = []
            for cname in rgb_names:
                off, ptype = chan_off[cname]
                arr = np.frombuffer(buf[base + off: base + off + sizes[ptype] * w].tobytes(), dtype=dt_for[ptype])
                if ptype == 0:
                    arr = arr.astype(np.float32) / 4294967295.0
                chans.append(arr[::stride].astype(np.float32))
            rows.append(np.stack(chans, axis=1))
    if not rows:
        return None
    rgb = np.concatenate(rows)
    if len(rgb) != ow * oh:
        pad = np.zeros((max(0, ow * oh - len(rgb)), 3), dtype=np.float32)
        rgb = np.concatenate((rgb, pad))[:ow * oh]
    comp = {0: "uncompressed", 1: "RLE", 2: "ZIPS", 3: "ZIP"}[compression]
    return RawImage(ow, oh, _tonemap_np(rgb),
                    [("Format", f"OpenEXR {comp}, {len(names)} channel{'s' if len(names) != 1 else ''}"),
                     ("Dimensions", f"{w} × {h} px"), ("Channels", ", ".join(names[:12]) + ("…" if len(names) > 12 else ""))])


def _stride_for(w: int, h: int, max_w: int, max_h: int) -> int:
    return max(1, int(math.ceil(max(w / max_w, h / max_h))))


# --------------------------------------------------------------------------- Maya IFF (.iff, 8-bit RGB/RGBA, RLE or raw)
def decode_maya_iff(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"FOR4" or data[8:12] != b"CIMG":
        return None
    pos = 12
    end = 8 + struct.unpack(">I", data[4:8])[0]
    width = height = 0
    flags = bpc = tiles = compress = 0
    rgba_chunks = []

    def walk(pos, end):
        nonlocal width, height, flags, bpc, tiles, compress
        while pos + 8 <= end:
            cid, size = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
            body = pos + 8
            if cid == b"FOR4":
                walk(body + 4, body + size)
            elif cid == b"TBHD":
                width, height, _prn, _prd, flags, bpc, tiles, compress = struct.unpack(">IIHHIHHI", data[body:body + 24])
            elif cid == b"RGBA":
                rgba_chunks.append((body, size))
            pos = body + size + (size & 1) + ((4 - (size + (size & 1)) % 4) % 4 if (size % 4) else 0)
    walk(pos, end)
    if not width or not height or bpc != 0:
        return None            # 16-bit IFFs go to the external-tool path
    nch = 4 if (flags & 2) else 3
    stride = _stride_for(width, height, max_w, max_h)
    ow, oh = (width + stride - 1) // stride, (height + stride - 1) // stride
    out = bytearray(ow * oh * 3)
    for body, size in rgba_chunks:
        x1, y1, x2, y2 = struct.unpack(">HHHH", data[body:body + 8])
        tw, th = x2 - x1 + 1, y2 - y1 + 1
        raw = data[body + 8:body + size]
        npx = tw * th
        if len(raw) == npx * nch:
            # uncompressed: interleaved pixels, channel order B G R (A)
            planes = [raw[c::nch] for c in range(nch)]
            chans = [planes[2], planes[1], planes[0]]      # stored B,G,R(,A) → R,G,B
        else:
            # RLE: planes stored one after another, order A,B,G,R (or B,G,R)
            dec, p = [], 0
            for _c in range(nch):
                plane = bytearray()
                need = npx
                while len(plane) < need and p < len(raw):
                    b = raw[p]
                    p += 1
                    cnt = (b & 0x7F) + 1
                    if b & 0x80:
                        plane += bytes([raw[p]]) * cnt
                        p += 1
                    else:
                        plane += raw[p:p + cnt]
                        p += cnt
                dec.append(bytes(plane[:need]))
            chans = [dec[-1], dec[-2], dec[-3]]     # R, G, B
        rch, gch, bch = chans
        for ty in range(th):
            sy = y1 + ty                      # IFF rows run bottom-up
            if sy % stride:
                continue
            oy = (height - 1 - sy) // stride
            if oy < 0 or oy >= oh:
                continue
            row = ty * tw
            for tx in range(0, tw):
                sx = x1 + tx
                if sx % stride:
                    continue
                ox = sx // stride
                o = (oy * ow + ox) * 3
                i = row + tx
                out[o], out[o + 1], out[o + 2] = rch[i], gch[i], bch[i]
    return RawImage(ow, oh, bytes(out), [("Format", f"Maya IFF {'RGBA' if nch == 4 else 'RGB'} 8-bit{' RLE' if compress else ''}"),
                                         ("Dimensions", f"{width} × {height} px")])


# --------------------------------------------------------------------------- Radiance HDR (.hdr / .pic)
def decode_hdr(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"#?"):
        return None
    pos = data.find(b"\n\n")
    if pos < 0:
        return None
    header, pos = data[:pos], pos + 2
    nl = data.find(b"\n", pos)
    res = data[pos:nl].decode("ascii", "ignore").split()
    pos = nl + 1
    if len(res) != 4 or res[0] != "-Y" or res[2] != "+X":
        return None
    h, w = int(res[1]), int(res[3])
    stride = _stride_for(w, h, max_w, max_h)
    ow, oh = (w + stride - 1) // stride, (h + stride - 1) // stride
    samples: List[Tuple[float, float, float]] = []
    for y in range(h):
        if pos + 4 > len(data):
            break
        if data[pos] == 2 and data[pos + 1] == 2 and (data[pos + 2] << 8 | data[pos + 3]) == w and w >= 8 and w < 32768:
            pos += 4
            planes = []
            for _c in range(4):
                plane = bytearray()
                while len(plane) < w:
                    c = data[pos]
                    pos += 1
                    if c > 128:
                        plane += bytes([data[pos]]) * (c - 128)
                        pos += 1
                    else:
                        plane += data[pos:pos + c]
                        pos += c
                planes.append(plane)
            scan = [(planes[0][x], planes[1][x], planes[2][x], planes[3][x]) for x in range(0, w, stride)]
        else:
            scan = [tuple(data[pos + x * 4:pos + x * 4 + 4]) for x in range(0, w, stride)]
            pos += w * 4
        if y % stride == 0:
            for r, g, b, e in scan:
                if e:
                    f = math.ldexp(1.0, e - 136)
                    samples.append(((r + 0.5) * f, (g + 0.5) * f, (b + 0.5) * f))
                else:
                    samples.append((0.0, 0.0, 0.0))
    if len(samples) != ow * oh:
        samples = (samples + [(0.0, 0.0, 0.0)] * (ow * oh))[:ow * oh]
    return RawImage(ow, oh, _tonemap(samples, ow, oh), [("Format", "Radiance HDR (RGBE)"), ("Dimensions", f"{w} × {h} px")])


# --------------------------------------------------------------------------- OpenEXR (scanline; NONE / RLE / ZIPS / ZIP)
def decode_exr(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x76\x2f\x31\x01":
        return None
    version = struct.unpack("<I", data[4:8])[0]
    if version & 0x200 or version & 0x1000:          # tiled / multipart: external tools
        return None
    pos = 8
    channels, compression, dw = [], 0, None

    def cstr(p):
        e = data.index(b"\0", p)
        return data[p:e].decode("latin-1"), e + 1
    while True:
        name, pos = cstr(pos)
        if name == "":
            break
        typ, pos = cstr(pos)
        size = struct.unpack("<i", data[pos:pos + 4])[0]
        pos += 4
        body = data[pos:pos + size]
        pos += size
        if name == "channels" and typ == "chlist":
            p = 0
            while p < len(body) and body[p] != 0:
                e = body.index(b"\0", p)
                cname = body[p:e].decode("latin-1")
                ptype = struct.unpack("<i", body[e + 1:e + 5])[0]
                xs, ys = struct.unpack("<ii", body[e + 9:e + 17])
                channels.append((cname, ptype, xs, ys))
                p = e + 17
        elif name == "compression":
            compression = body[0]
        elif name == "dataWindow":
            dw = struct.unpack("<iiii", body)
    if dw is None or not channels:
        return None
    xmin, ymin, xmax, ymax = dw
    w, h = xmax - xmin + 1, ymax - ymin + 1
    if any(xs != 1 or ys != 1 for _, _, xs, ys in channels):
        return None
    lines_per = {0: 1, 1: 1, 2: 1, 3: 16}.get(compression)
    if lines_per is None:
        return None                                   # PIZ/PXR24/B44/DWA → external tools
    sizes = {0: 4, 1: 2, 2: 4}                        # UINT, HALF, FLOAT
    chan_order = sorted(channels, key=lambda c: c[0])  # EXR stores channels alphabetically
    names = [c[0] for c in chan_order]

    def pick(suffixes):
        for n in names:
            if n in suffixes or n.split(".")[-1] in suffixes:
                return n
        return None
    rn, gn, bn = pick(("R",)), pick(("G",)), pick(("B",))
    if rn is None:
        yn = pick(("Y",))
        if yn is None:
            rn = gn = bn = names[0]
        else:
            rn = gn = bn = yn
    gn, bn = gn or rn, bn or rn
    nblocks = (h + lines_per - 1) // lines_per
    offsets = struct.unpack("<%dQ" % nblocks, data[pos:pos + 8 * nblocks])
    stride = _stride_for(w, h, max_w, max_h)
    ow, oh = (w + stride - 1) // stride, (h + stride - 1) // stride
    half = _half_table()
    line_bytes = sum(sizes[t] * w for _, t, _, _ in chan_order)
    chan_off: Dict[str, Tuple[int, int]] = {}
    off = 0
    for cname, ptype, _, _ in chan_order:
        chan_off[cname] = (off, ptype)
        off += sizes[ptype] * w
    if np is not None:
        return _decode_exr_np(data, offsets, compression, lines_per, line_bytes, chan_off, (rn, gn, bn), sizes, w, h, ymin, ymax, stride, ow, oh, names)
    samples: List[Tuple[float, float, float]] = []

    def rd(buf, base, ptype, x):
        if ptype == 1:
            return half[buf[base + x * 2] | (buf[base + x * 2 + 1] << 8)]
        if ptype == 2:
            return struct.unpack_from("<f", buf, base + x * 4)[0]
        return struct.unpack_from("<I", buf, base + x * 4)[0] / 4294967295.0

    for bi, o in enumerate(offsets):
        y0 = struct.unpack_from("<i", data, o)[0]
        size = struct.unpack_from("<i", data, o + 4)[0]
        raw = data[o + 8:o + 8 + size]
        nlines = min(lines_per, ymax - y0 + 1)
        expected = nlines * line_bytes
        if any(((y0 - ymin) + k) % stride == 0 for k in range(nlines)):
            if compression in (2, 3):
                buf = _exr_unpredict(zlib.decompress(raw)) if size < expected else raw
            elif compression == 1:
                buf = _exr_unpredict(_exr_rle(raw)) if size < expected else raw
            else:
                buf = raw
            for k in range(nlines):
                sy = (y0 - ymin) + k
                if sy % stride:
                    continue
                base = k * line_bytes
                ro, rt = chan_off[rn]
                go, gt = chan_off[gn]
                bo, bt = chan_off[bn]
                for x in range(0, w, stride):
                    samples.append((rd(buf, base + ro, rt, x), rd(buf, base + go, gt, x), rd(buf, base + bo, bt, x)))
    if len(samples) != ow * oh:
        samples = (samples + [(0.0, 0.0, 0.0)] * (ow * oh))[:ow * oh]
    comp = {0: "uncompressed", 1: "RLE", 2: "ZIPS", 3: "ZIP"}[compression]
    return RawImage(ow, oh, _tonemap(samples, ow, oh),
                    [("Format", f"OpenEXR {comp}, {len(channels)} channel{'s' if len(channels) != 1 else ''}"),
                     ("Dimensions", f"{w} × {h} px"), ("Channels", ", ".join(names[:12]) + ("…" if len(names) > 12 else ""))])


def _exr_rle(src: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        i += 1
        if c > 127:                 # signed negative: literal run
            cnt = 256 - c
            out += src[i:i + cnt]
            i += cnt
        else:
            out += bytes([src[i]]) * (c + 1)
            i += 1
    return bytes(out)


def _exr_unpredict(buf: bytes) -> bytes:
    """Undo EXR's delta + interleave predictor used by ZIP/ZIPS/RLE."""
    # OpenEXR compresses as: reorder (split into even/odd byte halves) THEN delta-code. So we undo the delta
    # first, then interleave the two halves back together.
    b = bytearray(buf)
    for i in range(1, len(b)):
        b[i] = (b[i - 1] + b[i] - 128) & 0xFF
    half = (len(b) + 1) // 2
    out = bytearray(len(b))
    out[0::2] = b[:half][:len(out[0::2])]
    out[1::2] = b[half:half + len(out[1::2])]
    return bytes(out)


# --------------------------------------------------------------------------- external converters
# --------------------------------------------------------------------------- Photoshop PSD / PSB
def _packbits_line(data: bytes, pos: int, end: int, out_len: int) -> bytes:
    """Decode one PackBits-compressed scanline (data[pos:end]) to out_len bytes."""
    out = bytearray()
    i = pos
    while i < end and len(out) < out_len:
        n = data[i]
        i += 1
        if n < 128:
            out += data[i:i + n + 1]
            i += n + 1
        elif n > 128:
            out += data[i:i + 1] * (257 - n)
            i += 1
    return bytes(out[:out_len])


def decode_psd(path: str, max_w: int, max_h: int) -> Optional[RawImage]:
    """Photoshop PSD/PSB preview from the flattened composite that Photoshop stores at the end of the file
    (when 'Maximize Compatibility' was on, which is the default). Handles RGB / grayscale / CMYK / indexed /
    duotone / Lab-ish, 8- and 16-bit, raw or RLE. Only every k-th scanline is decoded for the preview size,
    which is what keeps a 10k×10k file under a second. If the composite is missing (compatibility off), the
    embedded JPEG thumbnail (image resource 1036) is used instead; failing that, the caller falls back to
    ImageMagick."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"8BPS":
        return None
    version = struct.unpack(">H", data[4:6])[0]
    psb = version == 2
    channels, height, width, depth, mode = struct.unpack(">HIIHH", data[12:26])
    mode_name = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK", 7: "Multichannel", 8: "Duotone", 9: "Lab"}.get(mode, str(mode))
    pos = 26
    cm_len = struct.unpack(">I", data[pos:pos + 4])[0]
    palette = data[pos + 4:pos + 4 + cm_len] if mode == 2 else b""
    pos += 4 + cm_len
    res_len = struct.unpack(">I", data[pos:pos + 4])[0]
    resources = data[pos + 4:pos + 4 + res_len]
    pos += 4 + res_len
    # image resources: count layers? we only want the thumbnail (1036/1033) as a fallback
    thumb = None
    r = 0
    while r + 12 <= len(resources):
        if resources[r:r + 4] != b"8BIM":
            break
        rid = struct.unpack(">H", resources[r + 4:r + 6])[0]
        nlen = resources[r + 6]
        r += 7 + nlen + ((nlen + 1) % 2)
        size = struct.unpack(">I", resources[r:r + 4])[0]
        r += 4
        if rid in (1036, 1033):
            thumb = resources[r + 28:r + size]          # 28-byte thumbnail header, then JPEG
        r += size + (size % 2)
    lm_len = struct.unpack(">Q" if psb else ">I", data[pos:pos + (8 if psb else 4)])[0]
    pos += (8 if psb else 4) + lm_len
    info = [("Dimensions", f"{width} × {height}"), ("Mode", f"{mode_name} {depth}-bit, {channels} channel{'s' if channels != 1 else ''}"),
            ("Format", "PSB (large document)" if psb else "PSD")]
    # count layers for the quick view (cheap: the layer info header)
    try:
        lp = pos - lm_len
        li_len = struct.unpack(">Q" if psb else ">I", data[lp:lp + (8 if psb else 4)])[0]
        if li_len:
            lp += 8 if psb else 4
            nlayers = abs(struct.unpack(">h", data[lp:lp + 2])[0])
            info.append(("Layers", str(nlayers)))
    except struct.error:
        pass
    if pos + 2 > len(data) or width == 0 or height == 0:
        return _psd_thumb(thumb, info)
    compression = struct.unpack(">H", data[pos:pos + 2])[0]
    pos += 2
    bps = depth // 8 if depth >= 8 else 1
    if depth not in (8, 16) and mode != 0:
        return _psd_thumb(thumb, info)
    # scanline sampling
    step = max(1, int(math.ceil(max(width / max(max_w, 1), height / max(max_h, 1)))))
    rows = list(range(0, height, step))
    cols = list(range(0, width, step))
    ow, oh = len(cols), len(rows)
    row_bytes = width * bps if depth >= 8 else (width + 7) // 8
    nch = channels
    planes: List[List[bytes]] = []
    if compression == 0:
        for c in range(nch):
            base = pos + c * row_bytes * height
            if base + row_bytes * height > len(data) + 1:
                return _psd_thumb(thumb, info)
            planes.append([data[base + y * row_bytes: base + (y + 1) * row_bytes] for y in rows])
    elif compression == 1:
        cnt_sz = 4 if psb else 2
        counts = struct.unpack(f">{nch * height}{'I' if psb else 'H'}", data[pos:pos + nch * height * cnt_sz])
        p = pos + nch * height * cnt_sz
        offsets = []
        for c in range(nch):
            for y in range(height):
                offsets.append(p)
                p += counts[c * height + y]
        for c in range(nch):
            lines = []
            for y in rows:
                k = c * height + y
                lines.append(_packbits_line(data, offsets[k], offsets[k] + counts[k], row_bytes))
            planes.append(lines)
    else:
        return _psd_thumb(thumb, info)      # ZIP-compressed composites are rare (PSB only)

    def px8(line: bytes, x: int) -> int:
        if depth == 16:
            return line[2 * x] if 2 * x < len(line) else 0
        return line[x] if x < len(line) else 0
    out = bytearray(ow * oh * 3)
    o = 0
    for ri, y in enumerate(rows):
        L = [pl[ri] for pl in planes]
        for x in cols:
            if mode == 3 and nch >= 3:
                out[o], out[o + 1], out[o + 2] = px8(L[0], x), px8(L[1], x), px8(L[2], x)
            elif mode == 4 and nch >= 4:
                c, m, yy, k = (px8(L[i], x) for i in range(4))          # Photoshop stores CMYK inverted
                out[o], out[o + 1], out[o + 2] = c * k // 255, m * k // 255, yy * k // 255
            elif mode == 2 and palette:
                idx = px8(L[0], x)
                out[o], out[o + 1], out[o + 2] = palette[idx], palette[256 + idx], palette[512 + idx]
            elif mode == 0:
                bit = (L[0][x // 8] >> (7 - x % 8)) & 1 if x // 8 < len(L[0]) else 0
                out[o] = out[o + 1] = out[o + 2] = 0 if bit else 255
            else:                                                       # grayscale, duotone, Lab (L only), multichannel
                g = px8(L[0], x)
                out[o] = out[o + 1] = out[o + 2] = g
            o += 3
    if nch == 4 and mode == 3:
        info.append(("Alpha", "yes"))
    if step > 1:
        info.append(("Preview", f"1/{step} of the composite"))
    return RawImage(ow, oh, bytes(out), info)


def _psd_thumb(thumb: Optional[bytes], info: list) -> Optional[RawImage]:
    """Fallback: decode the embedded JPEG thumbnail with GdkPixbuf (worker process has no display, which is fine)."""
    if not thumb:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        loader.write(thumb)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return None
        if pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0) if False else pb
        w, h, rs, nch = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_n_channels()
        raw = pb.get_pixels()
        rgb = bytearray(w * h * 3)
        for y in range(h):
            row = raw[y * rs: y * rs + w * nch]
            if nch == 3:
                rgb[y * w * 3:(y + 1) * w * 3] = row
            else:
                for x in range(w):
                    rgb[(y * w + x) * 3:(y * w + x) * 3 + 3] = row[x * nch:x * nch + 3]
        return RawImage(w, h, bytes(rgb), info + [("Preview", "embedded thumbnail (composite not saved — enable 'Maximize compatibility')")])
    except Exception:
        return None


def _tool(*names) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def convert_with_tools(path: str, ct: str, max_w: int, max_h: int) -> Optional[Tuple[str, str]]:
    """Try installed converters; returns (png_path, tool_name) or None. Caller deletes the png."""
    out = os.path.join(tempfile.gettempdir(), f"kestrel-preview-{os.getpid()}-{abs(hash(path)) & 0xffffff:x}.png")
    low = path.lower()
    attempts = []
    is_video = ct.startswith("video/") or low.endswith((".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".r3d"))
    if is_video:
        ff = _tool("ffmpeg")
        if ff:
            attempts.append((ff, [ff, "-v", "error", "-y", "-ss", "1", "-i", path, "-frames:v", "1",
                                  "-vf", f"scale='min({max_w},iw)':-2", out]))
    if low.endswith(".pdf"):
        pt = _tool("pdftoppm")
        if pt:
            attempts.append((pt, [pt, "-png", "-singlefile", "-r", "40", "-f", "1", "-l", "1", path, out[:-4]]))
    oiio = _tool("oiiotool")
    if oiio:
        attempts.append((oiio, [oiio, path, "--subimage", "0", "--ch", "R,G,B", "--resize", f"{max_w}x{max_h}", "--colorconvert", "linear", "sRGB", "-o", out]))
        attempts.append((oiio, [oiio, path, "--subimage", "0", "--resize", f"{max_w}x{max_h}", "-o", out]))
    im = _tool("magick", "convert")
    if im:
        attempts.append((im, [im, path + "[0]", "-resize", f"{max_w}x{max_h}", "-colorspace", "sRGB", "-alpha", "off", out]))
    imgcvt = _tool("imgcvt")           # ships with Maya: handles .iff, 16-bit, .tdi …
    if imgcvt:
        attempts.append((imgcvt, [imgcvt, "-t", "png", path, out]))
    for tool, argv in attempts:
        try:
            r = subprocess.run(argv, capture_output=True, timeout=20)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return out, os.path.basename(tool)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            os.remove(out)
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------- information previews (text)
def describe_archive(path: str) -> Optional[str]:
    import zipfile, tarfile
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                total = sum(i.file_size for i in infos)
                lines = [f"{len(infos)} entries · {fmt_size(total)} uncompressed · {fmt_size(os.path.getsize(path))} on disk", ""]
                lines += [f"{fmt_size(i.file_size):>10}   {i.filename}" for i in infos[:400]]
                if len(infos) > 400:
                    lines.append(f"… {len(infos) - 400} more")
                return "\n".join(lines)
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                members = []
                for m in t:
                    members.append(m)
                    if len(members) > 400:
                        break
                lines = [f"{'400+' if len(members) > 400 else len(members)} entries · {fmt_size(os.path.getsize(path))} on disk", ""]
                lines += [f"{fmt_size(m.size):>10}   {m.name}{'/' if m.isdir() else ''}" for m in members[:400]]
                return "\n".join(lines)
    except Exception as ex:
        return f"Could not read archive: {ex}"
    sz = sevenzip()
    if sz:
        try:
            # (no -ba: p7zip 16.02 on EL9 predates it; the archive's own header block is filtered out below)
            r = subprocess.run([sz, "l", "-slt", "--", path], capture_output=True, text=True, errors="replace", timeout=30)
            if r.returncode == 0 and "Path = " in r.stdout:
                entries, cur = [], {}
                body = r.stdout.split("\n----------\n", 1)[-1]        # everything after the header separator
                for line in body.splitlines():
                    if not line.strip():
                        if cur.get("Path"):
                            entries.append(cur)
                        cur = {}
                    elif " = " in line:
                        k, v = line.split(" = ", 1)
                        cur[k] = v
                if cur.get("Path"):
                    entries.append(cur)
                entries = [e for e in entries if not (e.get("Path") in (path, os.path.basename(path)) and "Type" in e)]
                total = sum(int(e.get("Size") or 0) for e in entries)
                lines = [f"{len(entries)} entries · {fmt_size(total)} uncompressed · {fmt_size(os.path.getsize(path))} on disk  (7-Zip)", ""]
                for e in entries[:400]:
                    is_dir = (e.get("Attributes") or "").startswith("D") or (e.get("Folder") == "+")
                    lines.append(f"{'' if is_dir else fmt_size(int(e.get('Size') or 0)):>10}   {e['Path']}{'/' if is_dir else ''}")
                if len(entries) > 400:
                    lines.append(f"… {len(entries) - 400} more")
                return "\n".join(lines)
            if r.returncode != 0:
                return f"7-Zip could not read this archive: {(r.stderr.strip().splitlines() or ['unknown error'])[-1]}"
        except (OSError, subprocess.TimeoutExpired) as ex:
            return f"Could not read archive: {ex}"
    elif path.lower().endswith(SEVENZIP_EXTS):
        return "This archive type needs 7-Zip to list and extract.\nTools → Dependencies (p7zip)"
    return None


def describe_media(path: str) -> List[Tuple[str, str]]:
    """Duration / resolution / codecs via ffprobe when available."""
    fp = _tool("ffprobe")
    if not fp:
        return []
    try:
        r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
                            "-of", "json", path], capture_output=True, text=True, timeout=10)
        j = json.loads(r.stdout or "{}")
    except Exception:
        return []
    rows = []
    f = j.get("format", {})
    if f.get("duration"):
        d = float(f["duration"])
        rows.append(("Duration", f"{int(d // 3600):d}:{int(d % 3600 // 60):02d}:{d % 60:05.2f}" if d >= 3600 else f"{int(d // 60)}:{d % 60:05.2f}"))
    if f.get("bit_rate"):
        rows.append(("Bit rate", f"{int(f['bit_rate']) // 1000} kb/s"))
    for s in j.get("streams", []):
        if s.get("codec_type") == "video":
            fr = s.get("r_frame_rate", "")
            try:
                a, b = fr.split("/")
                fr = f"{float(a) / float(b):.3g} fps"
            except Exception:
                pass
            rows.append(("Video", f"{s.get('codec_name', '?')} {s.get('width', '?')}×{s.get('height', '?')} {fr}"))
        elif s.get("codec_type") == "audio":
            rows.append(("Audio", f"{s.get('codec_name', '?')} {s.get('sample_rate', '?')} Hz {s.get('channels', '?')} ch"))
    return rows


def describe_obj(path: str) -> List[Tuple[str, str]]:
    v = vn = vt = fc = 0
    groups = 0
    try:
        with open(path, "rb") as f:
            for line in f.readlines(8 << 20):
                if line.startswith(b"v "):
                    v += 1
                elif line.startswith(b"f "):
                    fc += 1
                elif line.startswith(b"vn "):
                    vn += 1
                elif line.startswith(b"vt "):
                    vt += 1
                elif line.startswith((b"g ", b"o ")):
                    groups += 1
    except OSError:
        return []
    return [("Vertices", f"{v:,}"), ("Faces", f"{fc:,}"), ("Normals / UVs", f"{vn:,} / {vt:,}"), ("Groups", f"{groups:,}")]


_POOL = None


# --------------------------------------------------------------------------- non-blocking existence checks
class PathCache:
    """os.path.exists / isdir for paths that may live on a network share, without ever blocking the GTK
    thread. The first query of a path answers optimistically (True) and schedules a real stat on a worker
    thread; later queries return the cached verdict, re-checked at most every 30 s. A dead NFS/SMB mount
    costs the worker a hang, not the window."""

    def __init__(self):
        self._v: Dict[str, Tuple[bool, bool, float]] = {}        # path → (exists, isdir, checked_at)
        self._pending: set = set()
        self._lock = threading.Lock()
        self.on_change: Optional[Callable] = None               # called (idle) when a verdict flips

    def _schedule(self, path: str):
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)

        def work():
            pin_worker()
            try:
                st = os.stat(path)
                ex, isd = True, _stat.S_ISDIR(st.st_mode)
            except OSError:
                ex, isd = False, False
            with self._lock:
                old = self._v.get(path)
                self._v[path] = (ex, isd, time.monotonic())
                self._pending.discard(path)
            if old is not None and old[:2] != (ex, isd) and self.on_change:
                GLib.idle_add(lambda: (self.on_change(), False)[1])
            elif old is None and not ex and self.on_change:
                GLib.idle_add(lambda: (self.on_change(), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _get(self, path: str) -> Tuple[bool, bool]:
        with self._lock:
            v = self._v.get(path)
        if v is None:
            self._schedule(path)
            return True, True                  # optimistic until the worker answers
        if time.monotonic() - v[2] > 30:
            self._schedule(path)
        return v[0], v[1]

    def exists(self, path: str) -> bool:
        return self._get(path)[0]

    def isdir(self, path: str) -> bool:
        ex, isd = self._get(path)
        return ex and isd


PATHS = PathCache()


# --------------------------------------------------------------------------- CPU parking
# Preview work (decoders, model loading, software rendering, thumbnails) is kept off the cores the interface
# runs on, so a 5M-triangle render or a 100 MB EXR decode can never starve the GTK main loop. UI_CORES is set
# from the config at startup; with fewer than 4 CPUs nothing is pinned.
UI_CORES = 2


def _cpu_sets():
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None, None
    n = max(0, min(UI_CORES, len(cpus) - 2))
    if len(cpus) < 4 or n == 0:
        return None, None
    return set(cpus[:n]), set(cpus[n:])


def pin_ui_thread():
    """Call from the GTK main thread once: it lives on the reserved cores."""
    ui, _ = _cpu_sets()
    if ui:
        try:
            os.sched_setaffinity(threading.get_native_id(), ui)
        except (AttributeError, OSError):
            pass


def pin_worker():
    """Call at the start of any preview worker thread or process: everything except the reserved cores,
    and a lower scheduling priority so the interface wins ties on shared caches/memory bandwidth."""
    _, work = _cpu_sets()
    if work:
        try:
            os.sched_setaffinity(threading.get_native_id(), work)
        except (AttributeError, OSError):
            pass
    try:
        if os.getpid() != MAIN_PID:
            os.nice(5)
    except OSError:
        pass


MAIN_PID = os.getpid()


def install_thread_pinning():
    """New threads inherit the creating thread's CPU mask, so threads spawned by the (pinned) GTK thread
    would land on the reserved cores. Wrap threading.Thread so every Python worker thread moves itself to
    the worker cores as its first act."""
    _, work = _cpu_sets()
    if not work:
        return
    base = threading.Thread

    class PinnedThread(base):
        def run(self):
            pin_worker()
            super().run()
    threading.Thread = PinnedThread


def warm_workers():
    """Fork the decoder pool (and the 3D render worker) right after startup, while RSS is a few tens of MB.
    os.fork() copies the page tables with the GIL held, so forking later — with a 5M-face mesh and thumbnail
    caches resident — would freeze the interface for a noticeable fraction of a second."""
    try:
        run_heavy(int, 0)
    except Exception:
        pass
    try:
        get_render_worker()
    except Exception:
        pass


def run_heavy(fn, *args):
    """Run a CPU-bound decoder in a forked worker process (one at a time). Pure-Python decoding holds the
    GIL, which would make the GTK main loop stutter; a separate process keeps the window fluid. Falls back
    to an in-thread call if the pool can't be used."""
    global _POOL
    try:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("fork"), initializer=pin_worker)
        return _POOL.submit(fn, *args).result(timeout=240)
    except Exception:
        try:
            if _POOL is not None:
                _POOL.shutdown(wait=False)
        except Exception:
            pass
        _POOL = None
        return fn(*args)


def shutdown_workers():
    """Quit: kill the decoder pool and the render process instead of letting multiprocessing's exit handler
    join them — a worker busy with a big model or EXR made closing the window hang for seconds."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        try:
            for p in list(getattr(pool, "_processes", {}).values()):
                p.kill()
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    rw = globals().get("_RENDER_WORKER")
    if rw is not None:
        try:
            rw.proc.kill()
            rw.proc.join(timeout=0.5)
        except Exception:
            pass
    try:
        import multiprocessing
        for ch in multiprocessing.active_children():
            ch.kill()
    except Exception:
        pass


PREVIEW_DECODERS = {".iff": decode_maya_iff, ".tdi": decode_maya_iff, ".exr": decode_exr, ".hdr": decode_hdr, ".pic": decode_hdr, ".rgbe": decode_hdr,
                    ".psd": decode_psd, ".psb": decode_psd}


def generate_preview(path: str, ct: str, max_w: int, max_h: int) -> dict:
    """Worker-thread entry point. Returns {'raw': RawImage} | {'png': path, 'tool': name} | {'text': str} | {}
    always with an 'info' list for the quick-view card."""
    ext = os.path.splitext(path)[1].lower()
    info: List[Tuple[str, str]] = []
    try:
        if ext in PREVIEW_DECODERS and os.path.getsize(path) < 400 << 20:
            try:
                img = run_heavy(PREVIEW_DECODERS[ext], path, max_w, max_h)
                if img is not None:
                    return {"raw": img, "info": img.info}
            except Exception as ex:
                info.append(("Decoder", f"built-in failed ({ex.__class__.__name__}); trying tools"))
        if ext == ".obj":
            info += describe_obj(path)
        if ct.startswith(("video/", "audio/")) or ext in (".mov", ".mp4", ".mkv", ".mxf", ".wav", ".flac", ".mp3", ".aif", ".aiff"):
            info += describe_media(path)
        arch = describe_archive(path) if (ext in (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".whl") or path.lower().endswith(SEVENZIP_EXTS)) else None
        if arch:
            return {"text": arch, "info": info}
        conv = convert_with_tools(path, ct, max_w, max_h)
        if conv:
            png, tool = conv
            info.append(("Preview via", tool))
            return {"png": png, "tool": tool, "info": info}
    except Exception as ex:
        info.append(("Preview", f"failed: {ex}"))
    return {"info": info}
