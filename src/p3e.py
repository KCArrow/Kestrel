
import io  # noqa: E402  (scene breakdowns: in-memory decompressed .blend)


# =========================================================================== scene breakdowns (p3e)
# What's inside a scene file, read without opening the application: Unreal (.umap / .uasset / .uproject),
# Unity (.unity / .prefab / .mat / .unitypackage), Maya (.ma / .mb) and Blender (.blend). Each reader returns
#   {"kind": str, "info": [(key, value)], "tree": [node], "notes": [str]}
# where node = [label, detail, [children], path, exists] — shown as an expandable tree in the preview pane.
# References carry the file's full path and whether it exists on this machine (None: not a local file).

def _bn(label: str, detail: str = "", children: Optional[list] = None, path: str = "", exists=None) -> list:
    return [label, detail, children or [], path, exists]


def _bn_file(label: str, detail: str, path: str) -> list:
    """A referenced file: path shown in its own column, flagged when it doesn't exist."""
    ok = None
    if path and (os.path.isabs(path) or path.startswith("~")):
        try:
            ok = os.path.exists(os.path.expanduser(path))
        except (OSError, ValueError):
            ok = None
    return _bn(label, detail, None, path, ok)


def _refs_summary(nodes: list) -> Tuple[int, int]:
    """(files, missing) over a reference tree."""
    n = miss = 0
    for x in nodes:
        if x[3]:
            n += 1
            miss += x[4] is False
        a, b = _refs_summary(x[2])
        n += a
        miss += b
    return n, miss


def _bn_counts(counter: dict, limit: int = 60, fmt=None) -> list:
    """Counter → nodes 'Name   ×N', biggest first."""
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]).lower()))
    out = [_bn(fmt(k) if fmt else str(k), f"×{v:,}") for k, v in items[:limit]]
    if len(items) > limit:
        out.append(_bn(f"… {len(items) - limit:,} more", ""))
    return out


def _bn_capped(nodes: list, limit: int, what: str = "more") -> list:
    if len(nodes) <= limit:
        return nodes
    return nodes[:limit] + [_bn(f"… {len(nodes) - limit:,} {what}", "")]


# --------------------------------------------------------------------------- Unreal Engine packages
UE_TAG = 0x9E2A83C1
_UE_PKG_FILTER_EDITOR_ONLY = 0x80000000


class UEPackage:
    """Header of an Unreal package (.uasset / .umap): summary, name table, import and export maps.
    Covers UE 4.0 – 5.4+ (legacy file versions -2 … -9). Export data is not deserialised, except for a
    best-effort scan of actor labels and outliner folders (editor-only tagged properties)."""

    def __init__(self, path: str, data: Optional[bytes] = None):
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        self.path, self.d = path, data
        self.p = 0
        self._summary()
        self._names()
        self._imports()
        self._exports()

    # ---- primitives
    def _i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def _u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def _i64(self):
        v = struct.unpack_from("<q", self.d, self.p)[0]
        self.p += 8
        return v

    def _fstring(self) -> str:
        n = self._i32()
        if n == 0:
            return ""
        if n < 0:
            n = -n
            if n > 1 << 20:
                raise ValueError("bad FString")
            s = self.d[self.p:self.p + 2 * n].decode("utf-16-le", "replace")
            self.p += 2 * n
        else:
            if n > 1 << 20:
                raise ValueError("bad FString")
            s = self.d[self.p:self.p + n].decode("latin-1")
            self.p += n
        return s.rstrip("\x00")

    def _engine_version(self) -> str:
        major, minor, patch = struct.unpack_from("<HHH", self.d, self.p)
        self.p += 6
        cl = self._u32()
        branch = self._fstring()
        if major == 0 and minor == 0:
            return ""
        return f"{major}.{minor}.{patch}" + (f" ({branch})" if branch else "") + (f" CL {cl}" if cl else "")

    # ---- summary
    def _summary(self):
        if self._u32() != UE_TAG:
            raise ValueError("not an Unreal package")
        self.legacy = self._i32()
        if self.legacy >= 0 or self.legacy < -9:
            raise ValueError(f"unsupported package version {self.legacy}")
        if self.legacy != -4:
            self._i32()                                       # LegacyUE3Version
        self.ue4 = self._i32()
        self.ue5 = self._i32() if self.legacy <= -8 else 0
        self._i32()                                           # licensee version
        if self.ue5 >= 1016:                                  # PACKAGE_SAVED_HASH (UE 5.4+): hash + header size
            self.p += 20                                      # come before the custom versions
            self._i32()
        if self.legacy <= -2:
            n = self._i32()
            if not 0 <= n < 10000:
                raise ValueError("bad custom version count")
            if self.legacy == -2:
                self.p += 8 * n
            elif self.legacy >= -5 + 1:                       # -3, -4: guid + version + friendly name
                for _ in range(n):
                    self.p += 20
                    self._fstring()
            else:
                self.p += 20 * n
        if self.ue5 < 1016:
            self._i32()                                       # TotalHeaderSize
        self.folder = self._fstring()                         # PackageName (UE5) / FolderName
        self.flags = self._u32()
        editor = not (self.flags & _UE_PKG_FILTER_EDITOR_ONLY)
        self.name_count, self.name_offset = self._i32(), self._i32()
        if self.ue5 >= 1008:                                  # ADD_SOFTOBJECTPATH_LIST
            self._i32(), self._i32()
        if editor and self.ue4 >= 516:                        # ADDED_PACKAGE_SUMMARY_LOCALIZATION_ID
            self._fstring()
        if self.ue4 >= 459:                                   # SERIALIZE_TEXT_IN_PACKAGES
            self._i32(), self._i32()
        self.export_count, self.export_offset = self._i32(), self._i32()
        self.import_count, self.import_offset = self._i32(), self._i32()
        self.depends_offset = self._i32()
        if self.ue4 >= 384:                                   # ADD_STRING_ASSET_REFERENCES_MAP
            self._i32(), self._i32()
        if self.ue4 >= 510:                                   # ADDED_SEARCHABLE_NAMES
            self._i32()
        self.thumb_offset = self._i32()                       # ThumbnailTableOffset
        if self.ue5 < 1016:
            self.p += 16                                      # Guid
        if editor and self.ue4 >= 518:                        # ADDED_PACKAGE_OWNER
            self.p += 16
            if self.ue4 < 520:
                self.p += 16
        self.engine = ""
        gen_end = self._find_generations(self.p)
        if gen_end is not None:
            self.p = gen_end
            try:
                if self.ue4 >= 336:                           # ENGINE_VERSION_OBJECT
                    self.engine = self._engine_version()
                    if self.ue4 >= 444:
                        compat = self._engine_version()
                        if not self.engine:
                            self.engine = compat
                else:
                    cl = self._i32()
                    self.engine = f"CL {cl}" if cl else ""
            except Exception:
                self.engine = ""
        for nm, v in (("names", self.name_count), ("imports", self.import_count), ("exports", self.export_count)):
            if not 0 <= v < 10_000_000:
                raise ValueError(f"bad {nm} count")

    def _find_generations(self, guess: int) -> Optional[int]:
        """End of the generations table, which precedes the saved-by engine version. Its last entry repeats
        the package's export and name counts, so it can be found even when newer engine versions add summary
        fields this reader doesn't know about: try the computed position, then scan nearby."""
        d = self.d
        want = (self.export_count, self.name_count)
        for q in [guess] + list(range(max(0, guess - 64), min(len(d) - 12, guess + 512))):
            if q + 4 > len(d):
                continue
            c = struct.unpack_from("<i", d, q)[0]
            if 1 <= c <= 64 and q + 4 + 8 * c <= len(d):
                if struct.unpack_from("<ii", d, q + 4 + 8 * (c - 1)) == want:
                    return q + 4 + 8 * c
        return None

    # ---- tables
    def _names(self):
        self.p = self.name_offset
        hashes = self.ue4 >= 504                              # NAME_HASHES_SERIALIZED
        names = []
        for _ in range(self.name_count):
            names.append(self._fstring())
            if hashes:
                self.p += 4
        self.names = names

    def name(self, idx: int, num: int = 0) -> str:
        if not 0 <= idx < len(self.names):
            return f"?{idx}"
        s = self.names[idx]
        return f"{s}_{num - 1}" if num > 0 else s

    def _fname(self) -> str:
        i, n = struct.unpack_from("<ii", self.d, self.p)
        self.p += 8
        return self.name(i, n)

    def _imports(self):
        self.p = self.import_offset
        editor = not (self.flags & _UE_PKG_FILTER_EDITOR_ONLY)
        imps = []
        for _ in range(self.import_count):
            cpkg, cname = self._fname(), self._fname()
            outer = self._i32()
            oname = self._fname()
            if self.ue4 >= 520 and editor:                    # NON_OUTER_PACKAGE_IMPORT
                self.p += 8
            if self.ue5 >= 1003:                              # OPTIONAL_RESOURCES
                self.p += 4
            imps.append((cpkg, cname, outer, oname))
        self.imports = imps

    def _export_stride(self) -> int:
        s = 4 + 4 + (4 if self.ue4 >= 508 else 0) + 4 + 8 + 4
        s += 16 if self.ue4 >= 511 else 8                     # 64-bit serial size / offset
        s += 12                                               # forced export, not for client / server
        if self.ue5 < 1005:
            s += 16                                           # package guid
        if self.ue5 >= 1006:
            s += 4                                            # is inherited instance
        s += 4                                                # package flags
        if self.ue4 >= 365:
            s += 4
        if self.ue4 >= 485:
            s += 4                                            # bIsAsset
        if self.ue5 >= 1003:
            s += 4
        if self.ue4 >= 507:
            s += 20                                           # preload dependencies
        if self.ue5 >= 1010:
            s += 16                                           # script serialization offsets
        return s

    def _exports(self):
        self.p = self.export_offset
        stride = self._export_stride()
        exps = []
        for i in range(self.export_count):
            base = self.export_offset + i * stride
            self.p = base
            cls = self._i32()
            sup = self._i32()
            if self.ue4 >= 508:
                self._i32()                                   # template
            outer = self._i32()
            oname = self._fname()
            self._u32()                                       # object flags
            if self.ue4 >= 511:
                size, off = self._i64(), self._i64()
            else:
                size, off = self._i32(), self._i32()
            is_asset = False
            if self.ue4 >= 485:
                q = base + stride - (20 if self.ue4 >= 507 else 0) - (16 if self.ue5 >= 1010 else 0) \
                    - (4 if self.ue5 >= 1003 else 0) - 4
                is_asset = struct.unpack_from("<i", self.d, q)[0] == 1
            exps.append((cls, outer, oname, size, off, is_asset, sup))
        # sanity: indices in range (a wrong stride shows up immediately)
        ni, ne = len(self.imports), len(exps)
        bad = sum(1 for c, o, *_ in exps if not (-ni <= c <= ne and -ni <= o <= ne))
        if exps and bad > len(exps) // 10:
            raise ValueError("export map not understood")
        self.exports = exps

    def thumbnails(self) -> list:
        """Editor thumbnails stored in the package: [(class, object path, image bytes, is_jpeg, w, h)].
        PNG in UE4, PNG or JPEG in UE5 (a negative height marks JPEG)."""
        out = []
        off = getattr(self, "thumb_offset", 0)
        if not off or off + 4 > len(self.d):
            return out
        try:
            self.p = off
            n = self._i32()
            if not 0 < n < 10000:
                return out
            entries = []
            for _ in range(n):
                cls = self._fstring()
                obj = self._fstring()
                entries.append((cls, obj, self._i32()))
            for cls, obj, foff in entries:
                if not 0 < foff < len(self.d) - 12:
                    continue
                w, h, size = struct.unpack_from("<iii", self.d, foff)
                if w <= 0 or size <= 0 or foff + 12 + size > len(self.d):
                    continue
                out.append((cls, obj, self.d[foff + 12:foff + 12 + size], h < 0, w, abs(h)))
        except (struct.error, ValueError):
            pass
        return out

    # ---- object helpers
    def obj_name(self, ref: int) -> str:
        if ref < 0 and -ref - 1 < len(self.imports):
            return self.imports[-ref - 1][3]
        if ref > 0 and ref - 1 < len(self.exports):
            return self.exports[ref - 1][2]
        return ""

    def class_of(self, ex) -> str:
        return self.obj_name(ex[0]) or "Object"

    def import_package(self, i: int) -> str:
        """Outermost package of an import (e.g. /Game/Meshes/SM_Chair)."""
        seen = 0
        while 0 <= i < len(self.imports) and seen < 64:
            outer = self.imports[i][2]
            if outer >= 0:
                return self.imports[i][3]
            i = -outer - 1
            seen += 1
        return ""

    def referenced_assets(self) -> Dict[str, str]:
        """{'/Game/Path/Asset': 'StaticMesh', …} for imports of objects inside /Game (and plugin) packages."""
        out = {}
        for i, (cpkg, cname, outer, oname) in enumerate(self.imports):
            if cname == "Package" or outer >= 0:
                continue
            pkg = self.import_package(i)
            if pkg.startswith("/Script/") or pkg.startswith("/Engine/Transient") or not pkg.startswith("/"):
                continue
            if self.imports[-outer - 1][1] == "Package":     # direct child of the package: the asset itself
                out.setdefault(pkg, cname)
        return out

    def property_value_near(self, start: int, end: int, prop: str, kind: str):
        """Best-effort read of one tagged property inside export data: finds the FName of `prop` and returns
        the FString (kind='str') or FName (kind='name') value that follows within a few dozen bytes."""
        try:
            idx = self.names.index(prop)
        except ValueError:
            return None
        pat = struct.pack("<ii", idx, 0)
        j = self.d.find(pat, start, end)
        if j < 0:
            return None
        k = j + 8
        lim = min(end, k + 64)
        if kind == "str":
            while k + 4 < lim:
                n = struct.unpack_from("<i", self.d, k)[0]
                if 1 < n < 512 and k + 4 + n <= end and self.d[k + 4 + n - 1] == 0:
                    raw = self.d[k + 4:k + 4 + n - 1]
                    if raw and all(32 <= c < 127 or c >= 160 for c in raw):
                        return raw.decode("latin-1")
                elif -512 < n < -1 and k + 4 - 2 * n <= end:
                    try:
                        s = self.d[k + 4:k + 4 - 2 * n].decode("utf-16-le").rstrip("\x00")
                        if s and s.isprintable():
                            return s
                    except UnicodeDecodeError:
                        pass
                k += 1
            return None
        skip = {"NameProperty", "None", prop}
        while k + 8 <= lim:
            ni, nn = struct.unpack_from("<ii", self.d, k)
            if 0 <= ni < len(self.names) and 0 <= nn < 1000 and self.names[ni] not in skip and k - j >= 16:
                v = self.name(ni, nn)
                if v and "/" not in v[:1]:
                    return v
            k += 1
        return None


_UE_ACTOR_HINTS = ("Actor", "Light", "Volume", "Camera", "Character", "Pawn", "Fog", "Atmosphere", "Landscape",
                   "Sky", "Cloud", "Brush", "Emitter", "Decal", "Note", "PlayerStart", "Reflection", "Sound")


def _ue_content_root(path: str) -> Optional[str]:
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(40):
        if os.path.basename(d) == "Content":
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd
    return None


def _ue_external_actors(map_path: str, limit_files: int = 4000, limit_s: float = 6.0):
    """World Partition / one-file-per-actor: actors live in Content/__ExternalActors__/<map path>/…"""
    root = _ue_content_root(map_path)
    if not root:
        return None
    rel = os.path.splitext(os.path.relpath(map_path, root))[0]
    folder = os.path.join(root, "__ExternalActors__", rel)
    if not os.path.isdir(folder):
        return None
    files = []
    for dp, dn, fn in os.walk(folder):
        files += [os.path.join(dp, f) for f in fn if f.endswith(".uasset")]
    t0 = time.monotonic()
    counts: Dict[str, int] = {}
    actors = []
    read = 0
    for f in files[:limit_files]:
        if time.monotonic() - t0 > limit_s:
            break
        try:
            pk = UEPackage(f)
        except Exception:
            continue
        read += 1
        for ex in pk.exports:
            if ex[1] == 0 or pk.obj_name(ex[1]) == "PersistentLevel":
                cls = pk.class_of(ex)
                if cls in ("Level", "Package", "MetaData", "World"):
                    continue
                if ex[1] != 0 or any(h in cls for h in _UE_ACTOR_HINTS) or cls.endswith("_C"):
                    counts[cls] = counts.get(cls, 0) + 1
                    lab = pk.property_value_near(ex[4], ex[4] + ex[3], "ActorLabel", "str") if ex[3] < 1 << 20 else None
                    fold = pk.property_value_near(ex[4], ex[4] + ex[3], "FolderPath", "name") if ex[3] < 1 << 20 else None
                    actors.append((lab or ex[2], cls, fold))
                    break
    return {"files": len(files), "read": read, "counts": counts, "actors": actors}


def _ue_outliner(actors: list, limit: int = 1500) -> list:
    """[(label, class, folder)] → folder tree like the World Outliner."""
    root: dict = {}
    for lab, cls, fold in actors[:limit]:
        node = root
        for part in [p for p in (fold or "").split("/") if p and p != "None"]:
            node = node.setdefault(("folder", part), {})
        node.setdefault(("actor", f"{lab}\x00{cls}"), {})

    def build(d):
        out = []
        for (kind, key), sub in sorted(d.items(), key=lambda kv: (kv[0][0] != "folder", kv[0][1].lower())):
            if kind == "folder":
                out.append(_bn(key, "folder", build(sub)))
            else:
                lab, cls = key.split("\x00")
                out.append(_bn(lab, _ue_class_label(cls)))
        return out
    nodes = build(root)
    if len(actors) > limit:
        nodes.append(_bn(f"… {len(actors) - limit:,} more actors", ""))
    return nodes


def _ue_class_label(cls: str) -> str:
    return f"{cls[:-2]} (Blueprint)" if cls.endswith("_C") else cls


def _ue_package_file(pkg: str, content: Optional[str]) -> str:
    """/Game/Maps/Foo → <project>/Content/Maps/Foo.umap|.uasset; plugin mounts → Plugins/**/<Name>/Content."""
    if not content or not pkg.startswith("/"):
        return ""
    parts = pkg.strip("/").split("/", 1)
    if len(parts) < 2:
        return ""
    mount, rest = parts
    if mount == "Game":
        base = content
    elif mount in ("Engine", "Script", "Temp", "Memory"):
        return ""
    else:
        base = _ue_plugin_content(os.path.dirname(content), mount)
        if not base:
            return ""
    stem = os.path.join(base, rest)
    for ext in (".uasset", ".umap"):
        if os.path.exists(stem + ext):
            return stem + ext
    return stem + ".uasset"


_UE_PLUGINS: Dict[str, Dict[str, str]] = {}


def _ue_plugin_content(project: str, mount: str) -> str:
    cache = _UE_PLUGINS.get(project)
    if cache is None:
        cache = {}
        plug = os.path.join(project, "Plugins")
        for dp, dn, fn in os.walk(plug):
            for f in fn:
                if f.endswith(".uplugin"):
                    cache[os.path.splitext(f)[0]] = os.path.join(dp, "Content")
            if dp.count(os.sep) - plug.count(os.sep) > 4:
                dn[:] = []
        _UE_PLUGINS[project] = cache
    return cache.get(mount, "")


def _ue_assets_tree(assets: Dict[str, str], content: Optional[str], limit: int = 400) -> list:
    by_type: Dict[str, list] = {}
    for p, t in assets.items():
        by_type.setdefault(t, []).append(p)
    out = []
    for t, ps in sorted(by_type.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        ps.sort(key=str.lower)
        items = []
        for p in ps:
            f = _ue_package_file(p, content)
            items.append(_bn_file(os.path.basename(p), p, f) if f else
                         _bn(os.path.basename(p), p + ("  · engine content" if p.startswith("/Engine/") else "")))
        out.append(_bn(t, f"×{len(ps):,}", _bn_capped(items, limit)))
    return out


def breakdown_unreal(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".uproject":
        return _breakdown_uproject(path)
    pk = UEPackage(path)
    info = [("Engine", pk.engine or f"UE{'5' if pk.ue5 else '4'} package version {pk.ue5 or pk.ue4}")]
    if pk.flags & _UE_PKG_FILTER_EDITOR_ONLY:
        info.append(("Cooked", "yes (editor data stripped)"))
    tree, notes = [], []
    level_idx = next((i + 1 for i, ex in enumerate(pk.exports) if pk.class_of(ex) == "Level"), 0)
    assets = pk.referenced_assets()
    if ext == ".umap" or level_idx:
        kind = "Unreal level"
        actors, counts = [], {}
        for ex in pk.exports:
            if level_idx and ex[1] == level_idx:
                cls = pk.class_of(ex)
                if cls in ("Model", "BrushComponent", "Polys", "LevelScriptBlueprint", "NavigationSystemV1"):
                    continue
                if cls.endswith("Component") or cls in ("Function", "Package"):
                    continue
                counts[cls] = counts.get(cls, 0) + 1
                lab = fold = None
                if ex[3] < 1 << 20:
                    lab = pk.property_value_near(ex[4], ex[4] + ex[3], "ActorLabel", "str")
                    fold = pk.property_value_near(ex[4], ex[4] + ex[3], "FolderPath", "name")
                actors.append((lab or ex[2], cls, fold))
        wp = "WorldPartition" in pk.names
        ext_act = _ue_external_actors(path)
        if ext_act and ext_act["files"]:
            for k, v in ext_act["counts"].items():
                counts[k] = counts.get(k, 0) + v
            actors += ext_act["actors"]
            info.append(("World Partition", f"{ext_act['files']:,} external actor files"))
            if ext_act["read"] < ext_act["files"]:
                notes.append(f"Read {ext_act['read']:,} of {ext_act['files']:,} external actor files (time limit).")
        elif wp:
            info.append(("World Partition", "yes"))
        info.append(("Actors", f"{sum(counts.values()):,}"))
        tree.append(_bn("Outliner", f"{len(actors):,} actors", _ue_outliner(actors)))
        tree.append(_bn("Actors by class", f"{len(counts):,} classes", _bn_counts(counts, fmt=_ue_class_label)))
    else:
        main = next((ex for ex in pk.exports if ex[5]), None) or \
            next((ex for ex in pk.exports if ex[1] == 0), None)
        cls = pk.class_of(main) if main else "?"
        kind = f"Unreal {cls}"
        info.append(("Asset class", cls))
        if cls in ("Blueprint", "WidgetBlueprint", "AnimBlueprint"):
            gen = next((i + 1 for i, ex in enumerate(pk.exports) if ex[2].endswith("_C") and ex[1] == 0), 0)
            funcs = sorted({ex[2] for ex in pk.exports if pk.class_of(ex) == "Function" and ex[1] == gen
                            and not ex[2].startswith(("ExecuteUbergraph", "bpv__", "Default__"))}, key=str.lower)
            comps = {}
            for ex in pk.exports:
                c = pk.class_of(ex)
                if c.endswith("Component") and "Default__" not in ex[2]:
                    comps[c] = comps.get(c, 0) + 1
            parent = pk.obj_name(pk.exports[gen - 1][6]) if gen else ""
            if parent:
                info.append(("Parent class", parent))
            info.append(("Functions", f"{len(funcs):,}"))
            tree.append(_bn("Functions & events", f"{len(funcs):,}", [_bn(f) for f in funcs]))
            if comps:
                tree.append(_bn("Components", f"{sum(comps.values()):,}", _bn_counts(comps)))
        objs: Dict[str, int] = {}
        for ex in pk.exports:
            c = pk.class_of(ex)
            objs[c] = objs.get(c, 0) + 1
        tree.append(_bn("Objects in this package", f"{len(pk.exports):,}", _bn_counts(objs)))
    content = _ue_content_root(path)
    refs = _ue_assets_tree(assets, content)
    own = (pk.folder or "").rstrip("/")
    # Soft references: /Game paths in the name table that aren't hard imports. FNames keep a trailing _N as a
    # separate number, so the bare string of a numbered hard import (SM_Frame for SM_Frame_1) is skipped.
    hard = {imp[3] for imp in pk.imports if imp[1] == "Package"}
    numbered = {re.sub(r"_\d+$", "", h) for h in hard if re.search(r"_\d+$", h)}
    soft = sorted({n for n in pk.names if n.startswith("/Game/") and n != own and not n.startswith(own + ".")
                   and n.split(".")[0] not in hard and n.split(".")[0] not in numbered}, key=str.lower)
    if soft:
        items = []
        for n in soft:
            pkgp = n.split(".")[0].split(":")[0]
            f = _ue_package_file(pkgp, content)
            items.append(_bn_file(os.path.basename(pkgp), n, f) if f else _bn(os.path.basename(pkgp), n))
        refs.append(_bn("Soft references", f"×{len(soft):,}", _bn_capped(items, 400)))
    nref, nmiss = _refs_summary(refs)
    tree.append(_bn("References", f"{len(assets) + len(soft):,} assets" + (f" · {nmiss} missing" if nmiss else ""), refs))
    if nmiss:
        info.append(("Missing references", f"{nmiss:,}"))
    scripts = sorted({imp[3] for imp in pk.imports if imp[1] == "Package" and imp[3].startswith("/Script/")})
    if scripts:
        tree.append(_bn("Engine & plugin modules", f"{len(scripts):,}", [_bn(s[len('/Script/'):], "") for s in scripts]))
    info.append(("Package", f"{len(pk.names):,} names · {len(pk.imports):,} imports · {len(pk.exports):,} exports"))
    images = []
    for cls, obj, data, is_jpeg, w, h in pk.thumbnails()[:1]:
        img = decode_image_bytes(data)
        if img is not None:
            images.append((f"Editor thumbnail ({cls})", img))
    return {"kind": kind, "info": info, "tree": tree, "notes": notes, "images": images}


def _breakdown_uproject(path: str) -> dict:
    with open(path, "rb") as f:
        j = json.loads(f.read().decode("utf-8-sig"))
    info = [("Engine", str(j.get("EngineAssociation") or "source build / unknown"))]
    if j.get("Category"):
        info.append(("Category", j["Category"]))
    if j.get("Description"):
        info.append(("Description", j["Description"]))
    tree = []
    mods = j.get("Modules") or []
    if mods:
        tree.append(_bn("C++ modules", f"{len(mods)}", [_bn(m.get("Name", "?"), f"{m.get('Type', '')} · {m.get('LoadingPhase', 'Default')}") for m in mods]))
    plugs = j.get("Plugins") or []
    if plugs:
        on = [p for p in plugs if p.get("Enabled", True)]
        off = [p for p in plugs if not p.get("Enabled", True)]
        tree.append(_bn("Plugins enabled", f"{len(on)}", [_bn(p.get("Name", "?")) for p in sorted(on, key=lambda p: p.get("Name", "").lower())]))
        if off:
            tree.append(_bn("Plugins disabled", f"{len(off)}", [_bn(p.get("Name", "?")) for p in off]))
    if j.get("TargetPlatforms"):
        tree.append(_bn("Target platforms", "", [_bn(t) for t in j["TargetPlatforms"]]))
    content = os.path.join(os.path.dirname(path), "Content")
    if os.path.isdir(content):
        maps, n_assets, t0 = [], 0, time.monotonic()
        for dp, dn, fn in os.walk(content):
            dn[:] = [d for d in dn if d not in ("__ExternalActors__", "__ExternalObjects__", "Collections", "Developers")]
            for f in fn:
                if f.endswith(".umap"):
                    maps.append(os.path.relpath(os.path.join(dp, f), content))
                elif f.endswith(".uasset"):
                    n_assets += 1
            if time.monotonic() - t0 > 3:
                break
        info.append(("Content", f"{len(maps):,} maps · {n_assets:,} assets"))
        tree.append(_bn("Maps", f"{len(maps):,}", _bn_capped([_bn(os.path.basename(m), os.path.dirname(m)) for m in sorted(maps, key=str.lower)], 300)))
    return {"kind": "Unreal project", "info": info, "tree": tree, "notes": []}


# --------------------------------------------------------------------------- Unity (text-serialised YAML)
UNITY_CLASSES = {
    1: "GameObject", 4: "Transform", 8: "Behaviour", 12: "ParticleAnimator", 20: "Camera", 21: "Material",
    23: "MeshRenderer", 25: "Renderer", 33: "MeshFilter", 43: "Mesh", 48: "Shader", 50: "Rigidbody2D",
    54: "Rigidbody", 56: "Collider", 58: "CircleCollider2D", 59: "HingeJoint", 60: "PolygonCollider2D",
    61: "BoxCollider2D", 64: "MeshCollider", 65: "BoxCollider", 68: "EdgeCollider2D", 70: "CapsuleCollider2D",
    81: "AudioListener", 82: "AudioSource", 95: "Animator", 96: "TrailRenderer", 102: "TextMesh",
    104: "RenderSettings", 108: "Light", 111: "Animation", 114: "MonoBehaviour", 119: "Projector",
    120: "LineRenderer", 135: "SphereCollider", 136: "CapsuleCollider", 137: "SkinnedMeshRenderer",
    143: "CharacterController", 146: "WheelCollider", 153: "ConfigurableJoint", 154: "TerrainCollider",
    157: "LightmapSettings", 164: "AudioReverbFilter", 192: "OcclusionArea", 193: "Tree", 195: "NavMeshAgent",
    196: "NavMeshSettings", 198: "ParticleSystem", 199: "ParticleSystemRenderer", 205: "LODGroup",
    208: "NavMeshObstacle", 212: "SpriteRenderer", 215: "ReflectionProbe", 218: "Terrain", 220: "LightProbeGroup",
    222: "CanvasRenderer", 223: "Canvas", 224: "RectTransform", 225: "CanvasGroup", 226: "BillboardRenderer",
    258: "LightProbes", 290: "AssetBundleManifest", 310: "UnityConnectSettings", 320: "PlayableDirector",
    328: "VideoPlayer", 330: "SpriteMask", 331: "SpriteShapeRenderer", 483693784: "TilemapRenderer",
    1001: "PrefabInstance", 1660057539: "SceneRoots", 1839735485: "Tilemap", 29: "OcclusionCullingSettings",
    19719996: "TilemapCollider2D", 156049354: "Grid", 1953259897: "TerrainLayer", 2083052967: "VisualEffect",
    73398921: "VFXRenderer", 310525001: "VisualEffectAsset", 850595691: "LightingSettings",
}
_UNITY_SKIP_COMPONENTS = ("Transform", "RectTransform")
_UNITY_BUILTIN_GUIDS = {"0000000000000000e000000000000000": "Unity built-in extra resources",
                        "0000000000000000f000000000000000": "Unity default resources",
                        "0000000000000000d000000000000000": "Unity built-in resources"}
_RE_FILEID = re.compile(r"fileID: (-?\d+)")
_RE_GUID = re.compile(r"guid: ([0-9a-f]{32})")
_RE_REF = re.compile(r"\b(\w+): \{fileID: (-?\d+)(?:, guid: ([0-9a-f]{32}))?")


def unity_project_root(path: str) -> Optional[str]:
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(40):
        if os.path.isdir(os.path.join(d, "Assets")) and os.path.isdir(os.path.join(d, "ProjectSettings")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd
    return None


def unity_version(root: Optional[str]) -> str:
    if not root:
        return ""
    try:
        with open(os.path.join(root, "ProjectSettings", "ProjectVersion.txt")) as f:
            for line in f:
                if line.startswith("m_EditorVersion:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


_UNITY_GUIDS: Dict[str, Tuple[float, Dict[str, str], bool]] = {}


def unity_guid_index(root: str, limit_s: float = 8.0) -> Tuple[Dict[str, str], bool]:
    """{guid: path relative to the project} from every .meta in Assets/, Packages/ and the package cache.
    Cached per project for a minute (the worker process lives on, so browsing a project stays fast).
    Returns (index, complete)."""
    hit = _UNITY_GUIDS.get(root)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1], hit[2]
    idx: Dict[str, str] = {}
    t0 = time.monotonic()
    complete = True
    for top in ("Assets", "Packages", os.path.join("Library", "PackageCache")):
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            for f in fn:
                if not f.endswith(".meta"):
                    continue
                try:
                    with open(os.path.join(dp, f), "rb") as fh:
                        head = fh.read(160)
                except OSError:
                    continue
                m = re.search(rb"guid: ([0-9a-f]{32})", head)
                if m:
                    idx[m.group(1).decode()] = os.path.relpath(os.path.join(dp, f[:-5]), root)
            if time.monotonic() - t0 > limit_s:
                complete = False
                break
        if not complete:
            break
    _UNITY_GUIDS[root] = (time.monotonic(), idx, complete)
    return idx, complete


def _unity_docs(path: str):
    """Yield (class_id, file_id, stripped, type_name, lines) for each YAML document. Flow mappings that wrap
    onto continuation lines are joined back into one line."""
    cid = fid = None
    stripped = False
    lines: list = []
    tname = ""
    pending = ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if pending:
                pending += " " + line.strip()
                if pending.count("{") <= pending.count("}"):
                    lines.append(pending)
                    pending = ""
                continue
            if line.startswith("--- !u!"):
                if cid is not None:
                    yield cid, fid, stripped, tname, lines
                head = line[7:].split()
                try:
                    cid = int(head[0])
                    fid = int(head[1].lstrip("&")) if len(head) > 1 else 0
                except (ValueError, IndexError):
                    cid, fid = -1, 0
                stripped = "stripped" in head
                lines, tname = [], ""
                continue
            if cid is None:
                continue
            if not tname and line and not line.startswith(" "):
                tname = line.rstrip(":")
                continue
            if "{" in line and line.count("{") > line.count("}"):
                pending = line
                continue
            lines.append(line)
    if cid is not None:
        yield cid, fid, stripped, tname, lines


def _unity_top(lines: list) -> Tuple[Dict[str, str], Dict[str, list]]:
    """Indent-2 keys of a document: scalars/flow values, and block lists of their '- ' items."""
    top: Dict[str, str] = {}
    lists: Dict[str, list] = {}
    cur = None
    for line in lines:
        if line.startswith("  - ") and cur is not None:
            lists.setdefault(cur, []).append(line[4:])
            continue
        if line.startswith("  ") and not line.startswith("   ") and ":" in line:
            k, _, v = line[2:].partition(":")
            v = v.strip()
            top[k] = v
            cur = k if not v else None
        elif not line.startswith("    "):
            cur = None
    return top, lists


def _fid(v: str) -> int:
    m = _RE_FILEID.search(v or "")
    return int(m.group(1)) if m else 0


def _guid(v: str) -> str:
    m = _RE_GUID.search(v or "")
    return m.group(1) if m else ""


def breakdown_unity(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".unitypackage":
        return _breakdown_unitypackage(path)
    with open(path, "rb") as f:
        head = f.read(64)
    root = unity_project_root(path)
    ver = unity_version(root)
    info = [("Unity", ver or "unknown (no ProjectSettings found above this file)")]
    if not head.startswith(b"%YAML"):
        return {"kind": "Unity asset (binary)", "info": info, "tree": [],
                "notes": ["This file is binary-serialised. Set Project Settings → Editor → Asset Serialization "
                          "to Force Text for a breakdown."]}
    guids, complete = unity_guid_index(root) if root else ({}, False)

    def asset(g: str) -> str:
        if g in _UNITY_BUILTIN_GUIDS:
            return _UNITY_BUILTIN_GUIDS[g]
        return guids.get(g, "")

    gos, xforms, prefabs, comps = {}, {}, {}, {}
    order: list = []
    roots_order: list = []
    refs: Dict[str, Dict[str, int]] = {}          # guid -> {field: count}
    settings: Dict[str, str] = {}
    big = os.path.getsize(path) > 300 << 20
    class_counts: Dict[str, int] = {}
    for cid, fid, stripped, tname, lines in _unity_docs(path):
        cname = UNITY_CLASSES.get(cid, tname or f"Class {cid}")
        class_counts[cname] = class_counts.get(cname, 0) + 1
        if big:
            continue
        for line in lines:
            if "fileID" not in line:                           # inline mesh / terrain data: long hex runs
                continue
            for m in _RE_REF.finditer(line):
                if m.group(3) and m.group(1) not in ("target", "m_CorrespondingSourceObject", "m_PrefabAsset"):
                    fld = refs.setdefault(m.group(3), {})
                    fld[m.group(1)] = fld.get(m.group(1), 0) + 1
            if line.startswith("  - {fileID") and "guid:" in line:          # list items (m_Materials …)
                g = _guid(line)
                if g:
                    fld = refs.setdefault(g, {})
                    fld["list"] = fld.get("list", 0) + 1
        top, lists = _unity_top(lines)
        if cid == 1:
            comp_ids = [_fid(x) for x in lists.get("m_Component", [])]
            gos[fid] = {"name": top.get("m_Name", ""), "active": top.get("m_IsActive", "1") != "0", "comps": comp_ids}
        elif cid in (4, 224):
            xforms[fid] = {"go": _fid(top.get("m_GameObject", "")), "father": _fid(top.get("m_Father", "")),
                           "children": [_fid(x) for x in lists.get("m_Children", [])],
                           "order": int(top.get("m_RootOrder", "0") or 0) if top.get("m_RootOrder", "").lstrip("-").isdigit() else 0,
                           "stripped": stripped, "prefab": _fid(top.get("m_PrefabInstance", "") or top.get("m_PrefabInternal", ""))}
            order.append(fid)
        elif cid == 1001:
            text = "\n".join(lines)
            parent = re.search(r"m_TransformParent: \{fileID: (-?\d+)", text)
            src = re.search(r"m_(?:SourcePrefab|ParentPrefab): \{fileID: -?\d+, guid: ([0-9a-f]{32})", text)
            nm = re.search(r"propertyPath: m_Name\s*\n\s*value: (.*)", text)
            act = re.search(r"propertyPath: m_IsActive\s*\n\s*value: (\d)", text)
            prefabs[fid] = {"parent": int(parent.group(1)) if parent else 0, "src": src.group(1) if src else "",
                            "name": (nm.group(1).strip() if nm else ""), "active": not (act and act.group(1) == "0")}
        elif cid == 1660057539:
            roots_order = [_fid(x) for x in lists.get("m_Roots", [])]
        elif cid == 104:
            settings["Skybox"] = asset(_guid(top.get("m_SkyboxMaterial", ""))) or ("none" if _fid(top.get("m_SkyboxMaterial", "")) == 0 else "built-in")
            settings["Fog"] = "on" if top.get("m_Fog") == "1" else "off"
            settings["Ambient"] = {"0": "skybox", "1": "gradient", "3": "colour", "4": "custom"}.get(top.get("m_AmbientMode", ""), top.get("m_AmbientMode", ""))
        elif cid == 157:
            lda = asset(_guid(top.get("m_LightingDataAsset", "")))
            settings["Baked lighting"] = lda or "not baked"
            ls = asset(_guid(top.get("m_LightingSettings", "")))
            if ls:
                settings["Lighting settings"] = ls
        elif cid not in (29, 196):
            go = _fid(top.get("m_GameObject", ""))
            if go:
                label = cname
                if cid == 114:
                    g = _guid(top.get("m_Script", ""))
                    sp = asset(g)
                    label = ("Built-in script" if sp.startswith("Unity ") else os.path.splitext(os.path.basename(sp))[0]) if sp \
                        else (f"Script {g[:8]}…" if g else "MonoBehaviour")
                elif cid == 108:
                    t = top.get("m_Type", "")
                    label = "Light (" + {"0": "Spot", "1": "Directional", "2": "Point", "3": "Area", "4": "Disc"}.get(t, t) + ")"
                comps[fid] = (go, label, top.get("m_Enabled", "1") != "0")

    # ---- hierarchy
    kids: Dict[int, list] = {}
    for p_id, p in prefabs.items():
        kids.setdefault(p["parent"], []).append(("prefab", p_id))
    for t_id, t in xforms.items():
        if not t["stripped"] and t["father"] not in xforms and t["father"] != 0:
            kids.setdefault(t["father"], []).append(("xform", t_id))
    prefab_root_xf = {t["prefab"]: t_id for t_id, t in xforms.items() if t["stripped"] and t["prefab"]}
    comp_by_go: Dict[int, list] = {}
    for c_id, (go, label, en) in comps.items():
        comp_by_go.setdefault(go, []).append(label + ("" if en else " (off)"))
    n_nodes = [0]
    LIMIT = 4000

    def prefab_node(p_id):
        p = prefabs[p_id]
        sp = asset(p["src"])
        name = p["name"] or (os.path.splitext(os.path.basename(sp))[0] if sp else "Prefab")
        n_nodes[0] += 1
        sub = []
        xt = prefab_root_xf.get(p_id)
        for kind, k in kids.get(xt, []) if xt else []:
            sub.append(node(kind, k))
        for t_id, t in xforms.items():
            if xt and t["father"] == xt and not t["stripped"]:
                sub.append(xform_node(t_id))
        return _bn(name + ("" if p["active"] else "  (inactive)"), "prefab · " + (sp or (f"missing prefab {p['src'][:8]}…" if p["src"] else "")), sub)

    def xform_node(t_id):
        t = xforms[t_id]
        if t["stripped"] and t["prefab"] in prefabs:
            return prefab_node(t["prefab"])
        g = gos.get(t["go"], {})
        n_nodes[0] += 1
        detail = ", ".join(comp_by_go.get(t["go"], []))
        if g and not g.get("active", True):
            detail = ("inactive · " + detail) if detail else "inactive"
        sub = []
        if n_nodes[0] < LIMIT:
            for c in t["children"]:
                if c in xforms:
                    sub.append(xform_node(c))
            for kind, k in kids.get(t_id, []):
                if kind == "prefab":
                    sub.append(prefab_node(k))
        return _bn(g.get("name", "") or "(unnamed)", detail, sub)

    def node(kind, k):
        return prefab_node(k) if kind == "prefab" else xform_node(k)

    root_items = []
    if roots_order:
        for r in roots_order:
            if r in xforms:
                root_items.append(xform_node(r))
            elif r in prefabs:
                root_items.append(prefab_node(r))
    else:
        rts = [t_id for t_id in order if xforms[t_id]["father"] == 0 and not xforms[t_id]["stripped"]]
        rts.sort(key=lambda t_id: xforms[t_id]["order"])
        root_items = [xform_node(t) for t in rts] + [prefab_node(p) for p, v in prefabs.items() if v["parent"] == 0]

    # ---- summary
    kind = {".unity": "Unity scene", ".prefab": "Unity prefab", ".mat": "Unity material"}.get(ext, "Unity asset")
    tree, notes = [], []
    n_lights = sum(1 for _, l, _ in comps.values() if l.startswith("Light ("))
    n_cams = sum(1 for _, l, _ in comps.values() if l == "Camera")
    if gos or prefabs:
        info += [("GameObjects", f"{len(gos):,}"), ("Prefab instances", f"{len(prefabs):,}")]
        if n_lights or n_cams:
            info.append(("Lights / cameras", f"{n_lights} / {n_cams}"))
        tree.append(_bn("Hierarchy", f"{len(root_items):,} roots", root_items))
        cc: Dict[str, int] = {}
        for _, l, _ in comps.values():
            cc[l] = cc.get(l, 0) + 1
        tree.append(_bn("Components", f"{sum(cc.values()):,}", _bn_counts(cc)))
        pc: Dict[str, int] = {}
        for p in prefabs.values():
            k = asset(p["src"]) or (f"missing ({p['src'][:8]}…)" if p["src"] else "?")
            pc[k] = pc.get(k, 0) + 1
        if pc:
            tree.append(_bn("Prefabs used", f"{len(pc):,}", _bn_counts(pc)))
    else:
        tree.append(_bn("Objects", f"{sum(class_counts.values()):,}", _bn_counts(class_counts)))
    if settings:
        tree.append(_bn("Lighting & environment", "", [_bn(k, v) for k, v in settings.items()]))
    tree.append(_unity_refs_tree(refs, asset, root))
    if root and not complete:
        notes.append("The project is large: only part of it was indexed, some references may show as unresolved.")
    if not root:
        notes.append("No Unity project found above this file, so referenced assets can't be resolved to paths.")
    if big:
        notes.append("Very large file: object counts only.")
    return {"kind": kind, "info": info, "tree": tree, "notes": notes}


_UNITY_REF_GROUPS = (("Scripts", (".cs",)), ("Prefabs", (".prefab",)), ("Materials", (".mat",)),
                     ("Meshes & models", (".fbx", ".obj", ".blend", ".dae", ".3ds", ".max", ".ma", ".mb", ".asset", ".mesh")),
                     ("Textures", (".png", ".jpg", ".jpeg", ".tga", ".psd", ".exr", ".hdr", ".tif", ".tiff", ".bmp", ".gif", ".renderTexture")),
                     ("Shaders", (".shader", ".shadergraph", ".compute", ".hlsl", ".cginc")),
                     ("Audio", (".wav", ".mp3", ".ogg", ".aif", ".aiff", ".flac")),
                     ("Animation", (".anim", ".controller", ".overrideController", ".playable", ".mask")),
                     ("Scenes", (".unity",)), ("Video", (".mp4", ".mov", ".webm")))


def _unity_refs_tree(refs: Dict[str, Dict[str, int]], asset, root: Optional[str]) -> list:
    groups: Dict[str, list] = {}
    missing = []
    for g, fields in refs.items():
        p = asset(g)
        n = sum(fields.values())
        if not p:
            missing.append(_bn(f"GUID {g}", f"×{n} · not found in the project ({', '.join(sorted(fields))})"))
            continue
        ext = os.path.splitext(p)[1].lower()
        grp = next((name for name, exts in _UNITY_REF_GROUPS if ext in exts), "Built-in" if p.startswith("Unity ") else "Other")
        if p.startswith("Unity "):
            groups.setdefault(grp, []).append(_bn(p, f"×{n}"))
        else:
            groups.setdefault(grp, []).append(_bn_file(os.path.basename(p), f"×{n}", os.path.join(root, p) if root else p))
    out = []
    order = [n for n, _ in _UNITY_REF_GROUPS] + ["Other", "Built-in"]
    for name in order:
        if name in groups:
            items = sorted(groups[name], key=lambda x: x[0].lower())
            out.append(_bn(name, f"{len(items):,}", _bn_capped(items, 500)))
    if missing:
        out.append(_bn("Unresolved (missing or in an unindexed package)", f"{len(missing):,}", _bn_capped(missing, 300)))
    total = sum(len(v) for v in groups.values()) + len(missing)
    _, nmiss = _refs_summary(out)
    nmiss += len(missing)
    return _bn("References", f"{total:,} assets" + (f" · {nmiss} missing" if nmiss else ""), out)


def _breakdown_unitypackage(path: str) -> dict:
    import tarfile
    names: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf:
            parts = m.name.lstrip("./").split("/")
            if len(parts) != 2:
                continue
            g, leaf = parts
            if leaf == "pathname":
                f = tf.extractfile(m)
                if f:
                    names[g] = f.read(4096).decode("utf-8", "replace").splitlines()[0].strip()
            elif leaf == "asset":
                sizes[g] = m.size
    tree_d: dict = {}
    for g, p in names.items():
        node = tree_d
        parts = p.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = sizes.get(g, -1)

    def build(d):
        out = []
        for k in sorted(d, key=lambda k: (not k.endswith("/"), k.lower())):
            v = d[k]
            if isinstance(v, dict):
                out.append(_bn(k.rstrip("/"), "folder", build(v)))
            else:
                out.append(_bn(k, fmt_size(v) if v >= 0 else "folder"))
        return out
    total = sum(s for s in sizes.values())
    return {"kind": "Unity package", "info": [("Assets", f"{len(sizes):,}"), ("Unpacked size", fmt_size(total))],
            "tree": [_bn("Contents", f"{len(names):,} entries", build(tree_d))], "notes": []}


# --------------------------------------------------------------------------- Maya (.ma ASCII and .mb binary)
# Node types whose attributes hold file paths: (attribute short names, label for the References section).
_MAYA_PATH_ATTRS = {
    "file": ((".ftn", ".fileTextureName"), "Textures"),
    "psdFileTex": ((".ftn", ".fileTextureName"), "Textures"),
    "aiImage": ((".filename", ".fn"), "Textures"),
    "PxrTexture": ((".filename",), "Textures"), "PxrMultiTexture": ((".filename0", ".filename"), "Textures"),
    "PxrNormalMap": ((".filename",), "Textures"), "PxrBump": ((".filename",), "Textures"),
    "PxrPtexture": ((".filename",), "Textures"), "PxrDomeLight": ((".lightColorMap",), "Textures"),
    "RedshiftNormalMap": ((".tex0",), "Textures"), "RedshiftDomeLight": ((".tex0",), "Textures"),
    "VRayBitmap": ((".fn", ".file"), "Textures"),
    "imagePlane": ((".imn", ".imageName"), "Image planes"),
    "aiStandIn": ((".dso",), "Arnold stand-ins"), "aiVolume": ((".filename", ".fn"), "Volumes / caches"),
    "RedshiftVolumeShape": ((".fileName",), "Volumes / caches"), "RedshiftProxyMesh": ((".fileName",), "Arnold stand-ins"),
    "AlembicNode": ((".fn", ".abc_File"), "Alembic caches"),
    "gpuCache": ((".cfn", ".cacheFileName"), "GPU caches"),
    "mayaUsdProxyShape": ((".fp", ".filePath"), "USD"), "pxrUsdProxyShape": ((".fp", ".filePath"), "USD"),
    "pxrUsdReferenceAssembly": ((".fp", ".filePath"), "USD"),
    "audio": ((".f", ".filename"), "Audio"),
    "cacheFile": ((".cp", ".cachePath"), "Volumes / caches"),
    "VRayMesh": ((".fn", ".fileName"), "Arnold stand-ins"), "VRayVolumeGrid": ((".if", ".inFile"), "Volumes / caches"),
    "xgmPalette": ((".xfn", ".xgFileName"), "XGen"), "xgmSplineDescription": ((".cacheFileName",), "XGen"),
    "bifrostGraphShape": ((".cacheFileName",), "Volumes / caches"),
    "RenderManArchive": ((".filename",), "Arnold stand-ins"),
}
_MAYA_PATH_EXTS = (".exr", ".tx", ".tex", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".tga", ".hdr", ".psd", ".bmp",
                   ".abc", ".vdb", ".usd", ".usda", ".usdc", ".usdz", ".ass", ".ass.gz", ".bgeo", ".bgeo.sc",
                   ".ma", ".mb", ".fbx", ".obj", ".rs", ".vrmesh", ".rib", ".wav", ".aif", ".aiff", ".mov", ".mp4",
                   ".mcx", ".mcc", ".xml", ".ptx", ".ies", ".ocio", ".lut", ".cube", ".xgen", ".ptex")
_MAYA_LIGHTS = ("directionalLight", "pointLight", "spotLight", "areaLight", "ambientLight", "volumeLight",
                "aiAreaLight", "aiSkyDomeLight", "aiMeshLight", "aiPhotometricLight", "aiLightPortal",
                "PxrRectLight", "PxrDomeLight", "PxrDiskLight", "PxrDistantLight", "PxrSphereLight", "PxrCylinderLight",
                "PxrMeshLight", "PxrEnvDayLight", "RedshiftPhysicalLight", "RedshiftDomeLight", "RedshiftIESLight",
                "RedshiftPortalLight", "RedshiftSunLight", "VRayLightRectShape", "VRayLightDomeShape", "VRayLightSphereShape")
_MAYA_MATERIAL_HINTS = ("lambert", "blinn", "phong", "phongE", "anisotropic", "surfaceShader", "standardSurface",
                        "openPBRSurface", "aiStandardSurface", "aiStandardHair", "aiFlat", "aiToon", "aiLambert",
                        "aiShadowMatte", "aiMixShader", "aiCarPaint", "aiStandardVolume", "PxrSurface", "PxrDisney",
                        "PxrDisneyBsdf", "PxrLayerSurface", "PxrMarschnerHair", "PxrVolume", "PxrConstant",
                        "RedshiftMaterial", "RedshiftStandardMaterial", "RedshiftSprite", "VRayMtl", "usdPreviewSurface",
                        "StingrayPBS", "rampShader", "layeredShader", "useBackground", "hairPhysicalShader")
_MAYA_DEFAULT_CAMS = {"persp", "top", "front", "side", "bottom", "back", "left"}
# 4-character node type tags of Maya binary files (built-in types; plugin nodes use numeric ids)
_MAYA_TYPE_TAGS = {"XFRM": "transform", "DMSH": "mesh", "DCAM": "camera", "SPTL": "spotLight", "RLLK": "lightLinker",
                   "DPLM": "displayLayerManager", "DSPL": "displayLayer", "RNLM": "renderLayerManager",
                   "RNDL": "renderLayer", "PCUB": "polyCube", "RBLN": "blinn", "SHAD": "shadingEngine",
                   "DMTI": "materialInfo", "SCRP": "script", "JONT": "joint", "RLAM": "lambert", "RPHO": "phong",
                   "DIRL": "directionalLight", "PNTL": "pointLight", "ARLT": "areaLight", "AMBL": "ambientLight",
                   "RTFT": "file", "PL2T": "place2dTexture", "GRPI": "groupId", "GRPP": "groupParts",
                   "NCRV": "nurbsCurve", "NSRF": "nurbsSurface", "LCTR": "locator", "REFN": "reference",
                   "OBJS": "objectSet", "SKCL": "skinCluster", "BLND": "blendShape", "TWEK": "tweak",
                   "DGPS": "dagPose", "ANCL": "animCurveTL", "ANCA": "animCurveTA", "ANCU": "animCurveTU",
                   "PSPH": "polySphere", "PPLN": "polyPlane", "PCYL": "polyCylinder", "IPLN": "imagePlane",
                   "AUDI": "audio", "CNST": "constraint", "PACN": "parentConstraint", "UNIT": "unitConversion",
                   "SHDE": "shadingEngine", "BPCE": "bump2d", "RMPT": "ramp", "CNOS": "nodeGraphEditorInfo",
                   "JOIN": "joint", "GPID": "groupId", "OBST": "objectSet"}


def _maya_workspace(path: str) -> Optional[str]:
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(12):
        if os.path.exists(os.path.join(d, "workspace.mel")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def resolve_scene_path(p: str, scene: str, roots=()) -> Tuple[str, Optional[bool]]:
    """A path written in a scene → (absolute path, exists?). Expands ~ and $VARS, tries the scene's folder,
    the project/workspace roots and Windows drive letters as given; UDIM / frame tokens
    (<UDIM>, <UVTILE>, %04d, ####, $F4, <f>) are checked with a glob."""
    raw = p.strip().strip('"')
    if not raw:
        return raw, None
    q = os.path.expanduser(os.path.expandvars(raw.replace("\\", "/")))
    cands = [q] if os.path.isabs(q) else [os.path.join(r, q) for r in (os.path.dirname(scene),) + tuple(roots) if r]
    if re.match(r"^[A-Za-z]:/", q):
        cands = [q]                                            # a Windows path: can't exist here, report it as is
    tok = re.compile(r"<UDIM>|<udim>|<UVTILE>|<uvtile>|<f\d?>|%0?\d*d|#+|\$F\d?|<frame>|_MAPID_|\*", re.I)
    for c in cands:
        if tok.search(c):
            pat = tok.sub("*", c)
            try:
                if glob.glob(pat):
                    return c, True
            except Exception:
                pass
        elif os.path.exists(c):
            return c, True
    # Written on another machine or OS (C:/proj/..., /mnt/other/...): look for the same file name next to the
    # scene and in the project's usual folders before calling it missing.
    base = os.path.basename(q)
    if base and not tok.search(base):
        for r in (os.path.dirname(scene),) + tuple(roots):
            for sub in ("", "scenes", "sourceimages", "images", "textures", "tex", "cache", "data", "assets", "renderData/images"):
                c = os.path.join(r, sub, base)
                if r and os.path.exists(c):
                    return c, True
    unknown = re.match(r"^[A-Za-z]:/", q) or re.search(r"<(MAYA_RESOURCES|[A-Z_]+_ROOT)>|\$\{?[A-Z_]+", q)
    return (cands[0] if cands else q), (None if unknown else False)


def _ref_nodes(found: Dict[str, Dict[str, list]], scene: str, roots=()) -> Tuple[list, int, int]:
    """{group: {path: [owner names]}} → reference tree, files, missing."""
    out, total, miss = [], 0, 0
    for grp in sorted(found, key=lambda g: (g != "Scene references", g)):
        items = []
        for raw, owners in sorted(found[grp].items(), key=lambda kv: kv[0].lower()):
            full, ok = resolve_scene_path(raw, scene, roots)
            total += 1
            miss += ok is False
            owner = ", ".join(sorted(set(owners))[:3]) + (f" +{len(set(owners)) - 3}" if len(set(owners)) > 3 else "")
            node = _bn(os.path.basename(raw.replace("\\", "/")) or raw, owner, None, full, ok)
            if full != raw and not os.path.isabs(raw):
                node[1] = (owner + " · " if owner else "") + f"as written: {raw}"
            items.append(node)
        out.append(_bn(grp, f"{len(items):,}" + (f" · {sum(1 for i in items if i[4] is False)} missing" if any(i[4] is False for i in items) else ""),
                       _bn_capped(items, 800)))
    return out, total, miss


class _MayaScene:
    def __init__(self):
        self.nodes: list = []                  # (type, name, parent)
        self.info: Dict[str, str] = {}
        self.requires: Dict[str, str] = {}
        self.units: Dict[str, str] = {}
        self.refs: list = []                   # (path, namespace, refnode, deferred)
        self.found: Dict[str, Dict[str, list]] = {}
        self.render: Dict[str, str] = {}
        self.version = ""
        self.attrs: Dict[str, Dict[str, str]] = {}   # shader nodes: attribute → value text
        self.conns: list = []                         # (src "node.attr", dst "node.attr")
        self.types: Dict[str, str] = {}
        self.instances: list = []                     # (shape, extra parent) from `parent -add`

    def add_path(self, group: str, path: str, owner: str):
        if path and len(path) < 4096:
            self.found.setdefault(group, {}).setdefault(path, []).append(owner)


_RE_MEL_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _mel_flags(stmt: str) -> Tuple[Dict[str, str], list]:
    """Split a MEL command into -flag values and positional (quoted) arguments."""
    toks = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', stmt)
    flags, pos = {}, []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("-") and not t[1:2].isdigit():
            if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
                flags[t] = toks[i + 1].strip('"')
                i += 2
                continue
            flags[t] = ""
        else:
            pos.append(t.strip('"'))
        i += 1
    return flags, pos


class MayaDagPaths:
    """Full DAG paths for Maya nodes as they are created. `-p` / connection names are the shortest unique
    partial path at save time, so 'grp2|pCube1' picks the right one of two 'pCube1's."""

    def __init__(self):
        self.by_short: Dict[str, list] = {}

    def resolve(self, ref: str) -> str:
        ref = (ref or "").strip()
        if not ref:
            return ""
        short = ref.split("|")[-1]
        cands = self.by_short.get(short) or self.by_short.get(short.lstrip(":")) or []
        if not cands:
            return ""
        if "|" in ref:
            if ref.startswith("|"):
                return ref if ref in cands else next((c for c in cands if c == ref), cands[-1])
            suf = "|" + ref
            hits = [c for c in cands if c.endswith(suf)]
            if hits:
                return hits[-1]
        return cands[-1]

    def add(self, name: str, parent: str) -> str:
        short = name.split("|")[-1]
        pfull = self.resolve(parent) if parent else ""
        full = f"{pfull}|{short}" if pfull or parent else f"|{short}"
        if parent and not pfull:
            full = "|" + parent.lstrip("|") + "|" + short             # parent not created (yet): keep as written
        self.by_short.setdefault(short, []).append(full)
        return full

    @staticmethod
    def parent_of(full: str) -> str:
        return full.rsplit("|", 1)[0] if full.count("|") > 1 else ""


def _parse_maya_ascii(path: str) -> _MayaScene:
    sc = _MayaScene()
    cur_type = cur_name = ""
    selected = ""
    stmt = ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first = f.readline()
        m = re.match(r"//Maya ASCII (\S+) scene", first)
        sc.version = m.group(1) if m else ""
        for line in f:
            if stmt:
                stmt += " " + line.strip()
                if not stmt.rstrip().endswith(";"):
                    continue
            else:
                c0 = line[:1]
                if c0 == "\t":
                    s = line.lstrip()
                    if not s.startswith("setAttr") or ('-type "string"' not in s and cur_type not in _MAYA_SHADER_TYPES):
                        continue
                elif c0 not in ("c", "f", "r", "s", "p"):
                    continue
                stmt = line.strip()
                if not stmt.endswith(";"):
                    continue
            s, stmt = stmt.rstrip().rstrip(";").rstrip(), ""
            if s.startswith("createNode "):
                fl, pos = _mel_flags(s)
                cur_type = pos[0] if pos else "?"
                cur_name = fl.get("-n", "")
                selected = ""
                sc.nodes.append((cur_type, cur_name, fl.get("-p", "")))
                sc.types[cur_name] = cur_type
            elif s.startswith("connectAttr "):
                strs = _RE_MEL_STR.findall(s)
                if len(strs) >= 2:
                    sc.conns.append((strs[0], strs[1]))
            elif s.startswith("parent ") and " -add" in s:
                strs = _RE_MEL_STR.findall(s)                     # instancing: parent -s -nc -r -add "shape" "xform"
                if len(strs) >= 2:
                    sc.instances.append((strs[0], strs[1]))
            elif s.startswith("setAttr ") and cur_type in _MAYA_SHADER_TYPES and not selected:
                mm = re.match(r'setAttr\s+(?:-\w+\s+\S+\s+)*"(\.[^"]+)"\s*(?:-type\s+"\w+"\s*)?(.*)$', s)
                if mm:
                    sc.attrs.setdefault(cur_name, {})[mm.group(1)] = mm.group(2).strip()
                strs = _RE_MEL_STR.findall(s)
                if len(strs) >= 3 and _MAYA_PATH_ATTRS.get(cur_type) and strs[0] in _MAYA_PATH_ATTRS[cur_type][0]:
                    sc.add_path(_MAYA_PATH_ATTRS[cur_type][1], strs[-1], cur_name)
            elif s.startswith("setAttr "):
                strs = _RE_MEL_STR.findall(s)
                if len(strs) < 3:
                    continue
                attr, value = strs[0], strs[-1].replace('\\"', '"')
                owner_t = selected or cur_type
                owner = selected or cur_name
                if owner_t == "defaultRenderGlobals" and attr in (".ren", ".currentRenderer"):
                    sc.render["Renderer"] = value
                elif owner_t == "sceneConfigurationScriptNode" or (cur_name == "sceneConfigurationScriptNode" and attr == ".b"):
                    pm = re.search(r"playbackOptions\s+-min\s+(\S+)\s+-max\s+(\S+)", value)
                    if pm:
                        sc.render["Playback range"] = f"{pm.group(1)} – {pm.group(2)}"
                    continue
                spec = _MAYA_PATH_ATTRS.get(owner_t)
                if spec and attr in spec[0]:
                    sc.add_path(spec[1], value, owner)
                elif owner_t not in ("camera", "script") and ("/" in value or "\\" in value):
                    low = value.lower()
                    if low.endswith(_MAYA_PATH_EXTS) and "\n" not in value and len(value) < 1024:
                        sc.add_path("Other files", value, f"{owner}{attr}")
            elif s.startswith("file "):
                fl, pos = _mel_flags(s)
                if ("-r" in fl or "-rdi" in fl) and pos:
                    sc.refs.append((pos[-1], fl.get("-ns", ""), fl.get("-rfn", ""), fl.get("-dr", "") == "1",
                                    fl.get("-rdi", "") if "-rdi" in fl else "", fl.get("-typ", "")))
            elif s.startswith("requires "):
                strs = _RE_MEL_STR.findall(s)
                if s.startswith("requires maya"):
                    sc.version = sc.version or (strs[0] if strs else "")
                elif len(strs) >= 2:
                    sc.requires[strs[0]] = strs[1]
            elif s.startswith("fileInfo "):
                strs = _RE_MEL_STR.findall(s)
                if len(strs) >= 2:
                    sc.info[strs[0]] = strs[1]
            elif s.startswith("currentUnit "):
                fl, _ = _mel_flags(s)
                sc.units = {"Linear": fl.get("-l", ""), "Angular": fl.get("-a", ""), "Time": fl.get("-t", "")}
            elif s.startswith("select "):
                fl, pos = _mel_flags(s)
                if pos:
                    selected = pos[0].lstrip(":")
                    cur_type = ""
                    cur_name = selected
    return sc


def _iff_chunks(d: bytes, p: int, end: int, w: int):
    """Yield (depth-first) ('group', form, start, end) and ('chunk', tag, data) for Maya IFF."""
    hdr = 8 if w == 4 else 16
    stack = [(p, end)]
    while stack:
        p, end = stack.pop()
        while p + hdr <= end:
            tag = d[p:p + 4]
            size = struct.unpack_from(">I", d, p + 4)[0] if w == 4 else struct.unpack_from(">Q", d, p + 8)[0]
            data = p + hdr
            if size > len(d) - data:
                return
            if tag[:3] in (b"FOR", b"LIS", b"CAT"):
                yield ("group", d[data:data + 4], data + 4, data + size)
                stack.append((data + size + ((-size) % 4 if w == 4 else 0), end))
                p, end = data + 4, data + size
                continue
            yield ("chunk", tag, d[data:data + size])
            p = data + size + ((-size) % w)


def _parse_maya_binary(path: str) -> _MayaScene:
    with open(path, "rb") as f:
        d = f.read()
    w = 8 if d[:4] == b"FOR8" else 4
    sc = _MayaScene()
    cur_type = cur_name = ""
    selected = ""
    known: set = set()
    for kind, tag, *rest in _iff_chunks(d, 0, len(d), w):
        if kind == "group":
            selected = ""
            cur_type = _MAYA_TYPE_TAGS.get(tag.decode("latin-1"), "") if tag.isascii() and tag.decode("latin-1").isprintable() else ""
            if not cur_type:
                cur_type = tag.decode("latin-1") if all(32 < c < 127 for c in tag) else f"0x{tag.hex()}"
            cur_name = ""
            continue
        data = rest[0]
        t = tag.decode("latin-1")
        if t == "CREA":
            flag = data[0] if data else 0
            parts = data[1:].split(b"\0")
            cur_name = parts[0].decode("utf-8", "replace") if parts else ""
            parent = ""
            # The second string is the parent when it names a node already written (Maya writes parents
            # first); the flag bits that say so differ between FOR4 and FOR8 files.
            if len(parts) > 1 and parts[1] and all(32 <= c < 127 for c in parts[1]):
                cand = parts[1].decode("ascii")
                if cand.split("|")[-1] in known:
                    parent = cand
            known.add(cur_name)
            sc.nodes.append((cur_type, cur_name, parent))
            sc.types[cur_name] = cur_type
        elif t == "SLCT":
            selected = data.split(b"\0")[0].decode("latin-1").lstrip(":")
            cur_type, cur_name = "", selected
        elif t == "STR ":
            nul = data.find(b"\0")
            if nul < 0:
                continue
            attr = "." + data[:nul].decode("latin-1")
            value = data[nul + 2:].split(b"\0")[0].decode("utf-8", "replace")
            owner_t = selected or cur_type
            owner = selected or cur_name
            if owner_t == "defaultRenderGlobals" and attr in (".ren", ".currentRenderer"):
                sc.render["Renderer"] = value
            elif cur_name == "sceneConfigurationScriptNode" and attr == ".b":
                pm = re.search(r"playbackOptions\s+-min\s+(\S+)\s+-max\s+(\S+)", value)
                if pm:
                    sc.render["Playback range"] = f"{pm.group(1)} – {pm.group(2)}"
            else:
                spec = _MAYA_PATH_ATTRS.get(owner_t)
                if spec and attr in spec[0]:
                    sc.add_path(spec[1], value, owner)
                elif owner_t not in ("camera", "script") and ("/" in value or "\\" in value) \
                        and value.lower().endswith(_MAYA_PATH_EXTS) and len(value) < 1024:
                    sc.add_path("Other files", value, f"{owner}{attr}")
        elif t in ("FLT3", "DBL3", "DBLE", "FLT2", "FLTE", "DBL2") and cur_name and not selected:
            nul = data.find(b"\0")
            if 0 < nul < 64:
                attr = "." + data[:nul].decode("latin-1")
                body = data[nul + 2:]
                fmt = ">" + ("f" if t.startswith("FLT") else "d")
                n = len(body) // (4 if t.startswith("FLT") else 8)
                if 0 < n <= 4:
                    vals = struct.unpack(fmt + fmt[1] * (n - 1), body[:n * (4 if t.startswith("FLT") else 8)])
                    sc.attrs.setdefault(cur_name, {})[attr] = " ".join(f"{v:g}" for v in vals)
        elif t == "CWFL":
            parts = data[1:].split(b"\0")
            if len(parts) >= 2:
                sc.conns.append((parts[0].decode("latin-1"), parts[1].decode("latin-1")))
        elif t == "FINF":
            parts = data.split(b"\0")
            if len(parts) >= 2:
                sc.info[parts[0].decode("latin-1")] = parts[1].decode("utf-8", "replace")
        elif t == "PLUG":
            parts = data.split(b"\0")
            if len(parts) >= 2 and parts[0]:
                sc.requires[parts[0].decode("latin-1")] = parts[1].decode("latin-1")
        elif t == "VERS" and not sc.version:
            sc.version = data.decode("latin-1").strip("\0")
        elif t in ("LUNI", "AUNI", "TUNI"):
            sc.units[{"LUNI": "Linear", "AUNI": "Angular", "TUNI": "Time"}[t]] = data.decode("latin-1").strip("\0")
        elif t in ("FREF", "RREF", "FRDI"):
            strs = [x.decode("utf-8", "replace") for x in data.split(b"\0") if x]
            pth = next((x for x in strs if re.search(r"\.(ma|mb|abc|usd[ac]?|fbx)$", x, re.I) or "/" in x), "")
            ns = next((x for x in strs if x != pth and not x.endswith("RN") and "/" not in x and len(x) < 128), "")
            rfn = next((x for x in strs if x.endswith("RN")), "")
            if pth:
                sc.refs.append((pth, ns, rfn, False, "", ""))
    return sc


_MAYA_SHADER_TYPES = set(_MAYA_MATERIAL_HINTS) | {"PxrSurface", "PxrDisneyBsdf", "RedshiftMaterial", "RedshiftStandardMaterial",
                                                 "VRayMtl", "usdPreviewSurface", "pxrUsdPreviewSurface", "standardSurface",
                                                 "openPBRSurface"}
_MAYA_ALIASES = {
    "base_color": (".bc", ".baseColor", ".base_color", ".c", ".color", ".dc", ".diffuseColor", ".diffuse_color"),
    "base_weight": (".b", ".base", ".baseWeight", ".base_weight", ".diffuseGain"),
    "metallic": (".m", ".metalness", ".metallic", ".baseMetalness", ".base_metalness", ".refl_metalness", ".mtl"),
    "roughness": (".sr", ".specularRoughness", ".specular_roughness", ".roughness", ".refl_roughness", ".rgh"),
    "specular": (".s", ".specular"),
    "coat": (".coat", ".coatWeight", ".coat_weight", ".clearcoat", ".cc"),
    "coat_roughness": (".coatRoughness", ".coat_roughness", ".clearcoatRoughness", ".ccr"),
    "_em_w": (".e", ".emission", ".emissionWeight", ".emission_weight"),
    "_em_c": (".ec", ".emissionColor", ".emission_color", ".emissiveColor", ".ic", ".incandescence", ".glowColor", ".emitColor"),
    "_opacity3": (".o", ".opacity", ".opc"),
    "_transparency": (".it", ".transparency"),
    "transmission": (".t", ".transmission", ".transmissionWeight", ".transmission_weight"),
    "normal": (".n", ".normalCamera", ".geometryNormal", ".normal"),
    "_gloss": (".reflectionGlossiness",),
    "_cosine_power": (".cp", ".cosinePower"),
    "_eccentricity": (".eccentricity",),
}
_MAYA_DEFAULTS = {"lambert": {"base_color": (0.5, 0.5, 0.5), "roughness": 0.9, "specular": 0.0},
                  "blinn": {"base_color": (0.5, 0.5, 0.5), "roughness": 0.35},
                  "phong": {"base_color": (0.5, 0.5, 0.5), "roughness": 0.3},
                  "standardSurface": {"base_color": (0.8, 0.8, 0.8), "roughness": 0.2},
                  "aiStandardSurface": {"base_color": (0.8, 0.8, 0.8), "roughness": 0.2},
                  "openPBRSurface": {"base_color": (0.8, 0.8, 0.8), "roughness": 0.3},
                  "usdPreviewSurface": {"base_color": (0.18, 0.18, 0.18), "roughness": 0.5},
                  "pxrUsdPreviewSurface": {"base_color": (0.18, 0.18, 0.18), "roughness": 0.5}}


def maya_materials(sc: "_MayaScene", path: str, roots=()) -> list:
    """Shader nodes → material dicts: values from setAttr, textures from file nodes connected upstream."""
    src_of: Dict[str, list] = {}                                  # dst node → [(dst attr, src node)]
    for src, dst in sc.conns:
        sn, _, sa = src.partition(".")
        dn, _, da = dst.partition(".")
        src_of.setdefault(dn.lstrip(":"), []).append(("." + da, sn.lstrip(":")))
    files: Dict[str, str] = {}
    for grp, paths in sc.found.items():
        for p, owners in paths.items():
            for o in owners:
                files.setdefault(o, p)
    shaders = [n for t, n, p in sc.nodes if t in _MAYA_SHADER_TYPES and n not in ("lambert1", "standardSurface1", "particleCloud1", "shaderGlow1")]
    # binary files: plug-in shader types are numeric — anything feeding a shading engine is a material
    for src, dst in sc.conns:
        sn, _, sa = src.partition(".")
        dn, _, da = dst.partition(".")
        if da in ("ss", "surfaceShader") and sc.types.get(dn, "") in ("shadingEngine", "SHAD", "") and sn not in shaders:
            shaders.append(sn)

    def upstream_files(node, depth=0, seen=None):
        seen = set() if seen is None else seen
        if depth > 8 or node in seen:
            return []
        seen.add(node)
        out = [files[node]] if node in files else []
        for _a, s in src_of.get(node, []):
            out += upstream_files(s, depth + 1, seen)
        return out
    want = {"normal": r"norm|nrm|_n\.|bump", "roughness": r"rough|gloss", "metallic": r"metal",
            "base_color": r"col|albedo|diff|base", "_em_c": r"emis|emit|incan|glow", "_opacity3": r"opac|alpha|mask"}
    out = []
    for name in shaders:
        typ = sc.types.get(name, "?")
        a = sc.attrs.get(name, {})
        fam = "lambert" if typ in ("lambert", "surfaceShader", "useBackground", "rampShader", "layeredShader") else \
            "blinn" if typ in ("blinn", "anisotropic") else "phong" if typ.startswith("phong") else typ
        m = dict(_MAYA_DEFAULTS.get(fam, {"base_color": (0.8, 0.8, 0.8), "roughness": 0.4}))
        m["name"] = name
        rows = []
        em_w, em_c = None, None
        conn = {da: s for da, s in src_of.get(name, [])}
        for key, aliases in _MAYA_ALIASES.items():
            if fam == "blinn" and key == "_em_c":
                aliases = (".ic", ".incandescence")
            if fam == "blinn" and key == "_eccentricity":
                aliases = (".ec", ".eccentricity")
            hit = next((al for al in aliases if al in conn), None)
            if hit:
                fs = upstream_files(conn[hit])
                pat = want.get(key)
                pick = next((f for f in fs if pat and re.search(pat, os.path.basename(f).lower())), fs[0] if fs and key != "normal" else None)
                full, ok = resolve_scene_path(pick, path, roots) if pick else ("", None)
                rows.append(_bn(hit.lstrip("."), f"← {conn[hit]}", None, full, ok))
                tk = {"base_color": "base_color_tex", "metallic": "metallic_tex", "roughness": "roughness_tex",
                      "normal": "normal_tex", "_em_c": "emission_tex", "_opacity3": "opacity_tex"}.get(key)
                if pick and tk:
                    m[tk] = full
                    if key == "base_color":
                        m["base_color"] = (1.0, 1.0, 1.0)
                continue
            hit = next((al for al in aliases if al in a), None)
            if not hit:
                continue
            raw = a[hit]
            vals = _floats(raw)
            if not vals:
                continue
            rows.append(_bn(hit.lstrip("."), " ".join(f"{v:.3g}" for v in vals[:4])))
            if key in ("base_color",):
                m[key] = _col3(vals) or m.get(key)
            elif key == "_em_c":
                em_c = _col3(vals)
            elif key == "_em_w":
                em_w = vals[0]
            elif key == "_opacity3":
                m["opacity"] = sum(vals[:3]) / max(1, len(vals[:3]))
            elif key == "_transparency":
                m["opacity"] = 1 - sum(vals[:3]) / max(1, len(vals[:3]))
            elif key == "_gloss":
                m["roughness"] = 1 - vals[0]
            elif key == "_cosine_power":
                m["roughness"] = math.sqrt(2.0 / (vals[0] + 2.0))
            elif key == "_eccentricity":
                m["roughness"] = min(1.0, vals[0] * 1.2)
            elif key != "normal":
                m[key] = vals[0]
        if em_c and any(em_c):
            w = 1.0 if em_w is None else em_w
            if fam in ("standardSurface", "aiStandardSurface") and em_w is None:
                w = 0.0                                          # emission weight defaults to 0 there
            if w > 0:
                m["emission"] = tuple(c * w for c in em_c)
        m["_type"] = typ
        m["_rows"] = rows
        out.append(m)
    return out


def breakdown_maya(path: str) -> dict:
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:4] in (b"FOR4", b"FOR8"):
        sc = _parse_maya_binary(path)
        kind = "Maya binary scene"
    else:
        sc = _parse_maya_ascii(path)
        kind = "Maya ASCII scene"
    ws = _maya_workspace(path)
    roots = tuple(r for r in (ws, os.environ.get("MAYA_PROJECT", "")) if r)
    info = [("Maya", sc.info.get("product") or (f"Maya {sc.version}" if sc.version else "unknown"))]
    if sc.info.get("cutIdentifier"):
        info.append(("Build", sc.info["cutIdentifier"]))
    if sc.units:
        info.append(("Units", " · ".join(v for v in (sc.units.get("Linear"), sc.units.get("Angular"), sc.units.get("Time")) if v)))
    for k, v in sc.render.items():
        info.append((k, v))
    if ws:
        info.append(("Project", ws))
    types: Dict[str, int] = {}
    for t, n, p in sc.nodes:
        types[t] = types.get(t, 0) + 1
    # outliner: DAG nodes (anything with a parent, plus transforms / joints at the root), keyed by DAG path so
    # two nodes with the same short name land under their own parents
    by_path: Dict[str, dict] = {}
    roots_l: list = []
    shapes: Dict[str, list] = {}
    dagp = MayaDagPaths()
    for t, n, p in sc.nodes:
        if not n:
            continue
        short = n.split("|")[-1]
        full = dagp.add(short, p)
        is_xform = t in ("transform", "joint", "lodGroup", "ikHandle", "dagContainer", "aimConstraint",
                         "parentConstraint", "pointConstraint", "orientConstraint", "scaleConstraint")
        if p and not is_xform:
            shapes.setdefault(MayaDagPaths.parent_of(full), []).append(t)
            continue
        if not is_xform and not p:
            continue
        rec = {"name": short, "path": full, "type": t, "kids": []}
        par = MayaDagPaths.parent_of(full)
        if p and par in by_path:
            by_path[par]["kids"].append(rec)
        else:
            roots_l.append(rec)
        by_path[full] = rec
    n_out = [0]

    ascii_scene = kind == "Maya ASCII scene"
    for child, par in sc.instances:
        pfull = dagp.resolve(par)
        ctype = sc.types.get(child.split("|")[-1], "")
        if pfull and ctype:
            shapes.setdefault(pfull, []).append(ctype + " (instance)" if ctype == "mesh" else ctype)

    def build(rec):
        n_out[0] += 1
        sh = shapes.get(rec["path"], [])
        detail = ", ".join(sorted(set(sh))) if sh else ("" if rec["type"] == "transform" else rec["type"])
        kids = [build(k) for k in rec["kids"]] if n_out[0] < 6000 else [_bn("…", "")]
        # rows with a mesh (here or below) can be picked to preview it in 3D (ASCII scenes only)
        geo = any(t.startswith("mesh") for t in sh) or any(k[3].startswith("object:") for k in kids)
        return _bn(rec["name"], detail, kids, f"object:{rec['path']}" if geo else "")
    outl = [build(r) for r in roots_l if r["name"] not in _MAYA_DEFAULT_CAMS]
    cams = sum(1 for t, n, p in sc.nodes if t == "camera" and n.replace("Shape", "") not in _MAYA_DEFAULT_CAMS)
    lights = sum(1 for t, n, p in sc.nodes if t in _MAYA_LIGHTS)
    meshes = types.get("mesh", 0)
    info.append(("Contents", f"{meshes:,} meshes · {lights} lights · {cams} cameras · {len(sc.refs and {r[0] for r in sc.refs}):,} references"))
    tree = [_bn("Outliner", f"{len(outl):,} top-level", outl,
                "object:*" if any(n[3].startswith("object:") for n in outl) else "")]
    # references (scene files) first
    found = dict(sc.found)
    seen = set()
    refs_tree = []
    for pth, ns, rfn, deferred, rdi, typ in sc.refs:
        key = (pth, ns)
        if key in seen:
            continue
        seen.add(key)
        full, ok = resolve_scene_path(pth, path, roots)
        det = ("unloaded · " if deferred else "") + (rfn or "")
        if ok and os.path.normpath(full) != os.path.normpath(os.path.expanduser(pth)):
            det += f" · found here; written as {pth}"
        refs_tree.append(_bn(ns or os.path.basename(pth), det, None, full, ok))
    other, nfiles, nmiss = _ref_nodes(found, path, roots)
    nmiss += sum(1 for r in refs_tree if r[4] is False)
    ref_children = ([_bn("Scene references", f"{len(refs_tree)}", refs_tree)] if refs_tree else []) + other
    tree.append(_bn("References", f"{nfiles + len(refs_tree):,} files" + (f" · {nmiss} missing" if nmiss else ""), ref_children))
    if nmiss:
        info.append(("Missing files", f"{nmiss:,}"))
    try:
        materials = sorted(maya_materials(sc, path, roots), key=lambda m: m["name"].lower())
    except Exception:
        materials = []
    if materials:
        mnodes = []
        for i, m in enumerate(materials):
            rows = m.pop("_rows", [])
            mnodes.append(_bn(m["name"], m.pop("_type", ""), rows, f"material:{i}", None))
        tree.append(_bn("Materials", f"{len(materials):,}", _bn_capped(mnodes, 400)))
    lt = [(n, t) for t, n, p in sc.nodes if t in _MAYA_LIGHTS]
    if lt or cams:
        tree.append(_bn("Lights & cameras", f"{len(lt)} lights · {cams} cameras",
                        [_bn(n, t) for n, t in lt] + [_bn(n, "camera") for t, n, p in sc.nodes if t == "camera" and n.replace("Shape", "") not in _MAYA_DEFAULT_CAMS]))
    anim = sum(c for t, c in types.items() if t.startswith("animCurve"))
    layers = [n for t, n, p in sc.nodes if t in ("renderLayer", "renderSetupLayer") and n != "defaultRenderLayer"]
    misc = []
    if anim:
        misc.append(_bn("Animation curves", f"{anim:,}"))
    if layers:
        misc.append(_bn("Render layers", f"{len(layers)}", [_bn(n) for n in layers]))
    if misc:
        tree.append(_bn("Animation & render layers", "", misc))
    if sc.requires:
        tree.append(_bn("Plug-ins required", f"{len(sc.requires)}", [_bn(k, v) for k, v in sorted(sc.requires.items(), key=lambda kv: kv[0].lower())]))
    tree.append(_bn("Node types", f"{len(sc.nodes):,} nodes", _bn_counts(types)))
    if sc.info:
        tree.append(_bn("File info", "", [_bn(k, v.replace("\\n", " ").strip()) for k, v in sc.info.items()]))
    notes = []
    return {"kind": kind, "info": info, "tree": tree, "notes": notes, "materials": materials}


# --------------------------------------------------------------------------- Blender (.blend)
# The file is a list of blocks (old memory address + struct type) and ends with "DNA1", the schema of every
# struct in that Blender version, so field offsets are looked up by name rather than hard-coded: files from
# 2.7x to 5.x read with the same code. gzip (old) and zstd (3.0+) compressed files are supported.
_BLEND_ID_CODES = {"OB": "Objects", "ME": "Meshes", "MA": "Materials", "TE": "Textures", "IM": "Images",
                   "LA": "Lights", "CA": "Cameras", "WO": "Worlds", "SC": "Scenes", "GR": "Collections",
                   "AC": "Actions", "NT": "Node groups", "LI": "Libraries", "AR": "Armatures", "CU": "Curves",
                   "CV": "Curves (hair)", "PT": "Point clouds", "VO": "Volumes", "MB": "Metaballs", "LT": "Lattices",
                   "SO": "Sounds", "MC": "Movie clips", "VF": "Fonts", "TX": "Texts", "CF": "Cache files",
                   "PA": "Particle settings", "BR": "Brushes", "GD": "Grease pencil (legacy)", "GP": "Grease pencil",
                   "LP": "Light probes", "SP": "Speakers", "KE": "Shape keys", "WS": "Workspaces", "LS": "Line styles",
                   "PL": "Palettes", "PC": "Paint curves", "MS": "Masks", "WM": "Window managers", "SN": "Screens",
                   "SR": "Screens", "CN": "Camera tracks", "ID": "Linked placeholders"}
_BLEND_OB_TYPES = {0: "Empty", 1: "Mesh", 2: "Curve", 3: "Surface", 4: "Text", 5: "Metaball", 10: "Light",
                   11: "Camera", 12: "Speaker", 13: "Light probe", 22: "Lattice", 25: "Armature",
                   26: "Grease pencil", 27: "Hair curves", 28: "Point cloud", 29: "Volume", 30: "Grease pencil"}
_BLEND_KEEP_ID = {"OB", "SC", "GR", "LI", "IM", "ME", "CA", "LA", "MA", "SO", "MC", "VF", "CF", "VO", "WO", "TX", "NT"}


def _blend_open(path: str):
    """→ (file-like with .read(n)) over the decompressed .blend, plus a description of the compression."""
    f = open(path, "rb")
    head = f.read(4)
    f.seek(0)
    if head[:2] == b"\x1f\x8b":
        import gzip
        return gzip.GzipFile(fileobj=f), "gzip"
    if head == b"\x28\xb5\x2f\xfd":
        f.close()
        return io.BytesIO(_zstd_decompress_file(path)), "zstd"
    return f, ""


def _zstd_decompress_file(path: str, cap: int = 4 << 30) -> bytes:
    try:
        import zstandard                                             # python3-zstandard, if installed
        with open(path, "rb") as f:
            return zstandard.ZstdDecompressor().stream_reader(f).read()
    except ImportError:
        pass
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("zstd") or "libzstd.so.1"
    try:
        z = ctypes.CDLL(name)
    except OSError:
        z = None
    if z is None:
        out = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=120)
        if out.returncode != 0:
            raise ValueError("zstd-compressed .blend: install zstd")
        return out.stdout

    class Buf(ctypes.Structure):
        _fields_ = [("ptr", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]
    z.ZSTD_createDStream.restype = ctypes.c_void_p
    z.ZSTD_decompressStream.restype = ctypes.c_size_t
    z.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(Buf), ctypes.POINTER(Buf)]
    z.ZSTD_isError.argtypes = [ctypes.c_size_t]
    z.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    ds = z.ZSTD_createDStream()
    z.ZSTD_initDStream(ctypes.c_void_p(ds))
    with open(path, "rb") as f:
        src = f.read()
    sbuf = ctypes.create_string_buffer(src, len(src))
    inb = Buf(ctypes.cast(sbuf, ctypes.c_void_p), len(src), 0)
    chunk = 1 << 22
    obuf = ctypes.create_string_buffer(chunk)
    parts, total = [], 0
    try:
        while inb.pos < inb.size:
            outb = Buf(ctypes.cast(obuf, ctypes.c_void_p), chunk, 0)
            r = z.ZSTD_decompressStream(ctypes.c_void_p(ds), ctypes.byref(outb), ctypes.byref(inb))
            if z.ZSTD_isError(r):
                raise ValueError("corrupt zstd data")
            parts.append(obuf.raw[:outb.pos])
            total += outb.pos
            if total > cap:
                raise ValueError("decompressed .blend is too large to preview")
            if r == 0 and inb.pos >= inb.size:
                break
        # drain any buffered output
        while True:
            outb = Buf(ctypes.cast(obuf, ctypes.c_void_p), chunk, 0)
            r = z.ZSTD_decompressStream(ctypes.c_void_p(ds), ctypes.byref(outb), ctypes.byref(inb))
            if outb.pos == 0 or z.ZSTD_isError(r):
                break
            parts.append(obuf.raw[:outb.pos])
    finally:
        z.ZSTD_freeDStream(ctypes.c_void_p(ds))
    return b"".join(parts)


class BlendFile:
    """Blocks + SDNA of a .blend. Keeps ID blocks of interest and small DATA blocks (list links)."""

    def __init__(self, path: str):
        self.path = path
        f, self.compression = _blend_open(path)
        self._mem = f.getvalue() if isinstance(f, io.BytesIO) else None   # zstd: keep for later block reads
        self.skipped: Dict[int, Tuple[int, int, int, int]] = {}     # old address → (offset, length, sdna, nr)
        self.thumb = None
        try:
            self._read(f)
        finally:
            f.close()

    def _read(self, f):
        head = f.read(12)
        if head[:7] != b"BLENDER":
            raise ValueError("not a .blend file")
        if head[7:9] == b"17":                                    # Blender 5.0+ header: BLENDER17-01v0500
            rest = f.read(5)
            self.ptr, self.le = 8, True
            self.version = (head[11:12] + rest).decode("latin-1")[-4:]
            self.new_format = True
        else:
            self.ptr = 8 if head[7:8] == b"-" else 4
            self.le = head[8:9] == b"v"
            self.version = head[9:12].decode("latin-1")
            self.new_format = False
        e = "<" if self.le else ">"
        P = "Q" if self.ptr == 8 else "I"
        self.e, self.P = e, P
        hsz = 32 if self.new_format else (16 + self.ptr)
        self.blocks = []                      # (code, sdna, old, data or None, nr)
        self._order: list = []                # old address of every block, in file order
        self._id_pos: Dict[int, int] = {}     # ID block address → index in _order
        self._at: Dict[int, list] = {}        # address → indices in _order (usually one)
        self._payload: Dict[int, tuple] = {}  # index in _order → ("k", block index) | ("s", offset, length, sdna, nr)
        self.by_old: Dict[int, int] = {}
        self.counts: Dict[str, int] = {}
        self.linked: Dict[str, int] = {}
        dna = None
        while True:
            h = f.read(hsz)
            if len(h) < hsz:
                break
            if self.new_format:
                code = h[:4]
                sdna, old, ln, nr = struct.unpack(e + "iQqq", h[4:32])
            else:
                code = h[:4]
                ln, old, sdna, nr = struct.unpack(e + "i" + P + "ii", h[4:hsz])
            if code == b"ENDB":
                break
            c2 = code[:2].decode("latin-1") if code[2:4] == b"\0\0" else ""
            # Blender 5 writes per-ID "stable" addresses that can repeat across IDs: remember every block's
            # place so a pointer can be resolved within the ID that owns it (read_data(owner=…))
            self._order.append(old)
            if c2:
                self._id_pos[old] = len(self._order) - 1
            self._at.setdefault(old, []).append(len(self._order) - 1)
            # ID blocks we report on, the schema, and small DATA blocks (list links, embedded collections);
            # geometry and pixel data are skipped without being read
            keep = (code == b"DNA1") or (c2 in _BLEND_KEEP_ID) or (code == b"DATA" and ln <= 4096)
            if code == b"TEST":                                   # file browser thumbnail
                self.thumb = f.read(ln)
                continue
            if keep:
                data = f.read(ln)
            else:
                data = None
                try:
                    self.skipped[old] = (f.tell(), ln, sdna, nr)
                    self._payload[len(self._order) - 1] = ("s", f.tell(), ln, sdna, nr)
                except (OSError, AttributeError):
                    pass
                f.seek(ln, 1)
            if c2:
                self.counts[c2] = self.counts.get(c2, 0) + 1
            if code == b"DNA1":
                dna = data
                continue
            self.by_old[old] = len(self.blocks)
            if data is not None:
                self._payload[len(self._order) - 1] = ("k", len(self.blocks))
            self.blocks.append((code, sdna, old, data, nr, c2))
        if dna is None:
            raise ValueError("no DNA1 block")
        self._dna(dna)

    def _dna(self, d: bytes):
        e = self.e
        p = 4                                                    # SDNA
        assert d[p:p + 4] == b"NAME"
        n = struct.unpack_from(e + "i", d, p + 4)[0]
        p += 8
        names = []
        for _ in range(n):
            q = d.index(b"\0", p)
            names.append(d[p:q].decode("latin-1"))
            p = q + 1
        p = (p + 3) & ~3
        assert d[p:p + 4] == b"TYPE"
        n = struct.unpack_from(e + "i", d, p + 4)[0]
        p += 8
        types = []
        for _ in range(n):
            q = d.index(b"\0", p)
            types.append(d[p:q].decode("latin-1"))
            p = q + 1
        p = (p + 3) & ~3
        assert d[p:p + 4] == b"TLEN"
        p += 4
        tlen = list(struct.unpack_from(e + f"{len(types)}h", d, p))
        p += 2 * len(types)
        p = (p + 3) & ~3
        assert d[p:p + 4] == b"STRC"
        n = struct.unpack_from(e + "i", d, p + 4)[0]
        p += 8
        self.structs: Dict[str, Dict[str, Tuple[int, str, str]]] = {}
        self.struct_names: list = []
        for _ in range(n):
            t, nf = struct.unpack_from(e + "hh", d, p)
            p += 4
            fields = {}
            off = 0
            for _ in range(nf):
                ft, fn = struct.unpack_from(e + "hh", d, p)
                p += 4
                nm = names[fn]
                size = self._field_size(nm, tlen[ft])
                key = re.sub(r"\[.*", "", nm.lstrip("*")).strip("()*")
                fields[key] = (off, types[ft], nm)
                off += size
            self.structs[types[t]] = fields
            self.struct_names.append(types[t])
        self.tlen = dict(zip(types, tlen))

    def _field_size(self, name: str, tsize: int) -> int:
        mult = 1
        for m in re.findall(r"\[(\d+)\]", name):
            mult *= int(m)
        if name.startswith("*") or name.startswith("(*"):
            return self.ptr * mult
        return tsize * mult

    # ---- field access
    def read_data(self, old: int, owner: int = 0) -> Optional[bytes]:
        """Data of any block by its old address, re-reading big blocks that were skipped on the first pass.
        `owner` (an ID block's address) picks the copy that belongs to that ID when addresses repeat."""
        hits = self._at.get(old, ())
        if owner and len(hits) > 1 and owner in self._id_pos:
            start = self._id_pos[owner]
            if not hasattr(self, "_id_sorted"):
                self._id_sorted = sorted(self._id_pos.values())
            j = bisect.bisect_right(self._id_sorted, start)
            end = self._id_sorted[j] if j < len(self._id_sorted) else len(self._order)
            idx = next((i for i in hits if start < i < end), None)
            pl = self._payload.get(idx) if idx is not None else None
            if pl and pl[0] == "k":
                return self.blocks[pl[1]][3]
            if pl and pl[0] == "s":
                return self._read_span(pl[1], pl[2])
        b = self.block(old)
        if b is not None and b[3] is not None:
            return b[3]
        pos = self.skipped.get(old)
        if pos is None:
            return None
        return self._read_span(pos[0], pos[1])

    def _read_span(self, offset: int, length: int) -> bytes:
        if self._mem is not None:
            return self._mem[offset:offset + length]
        f, _ = _blend_open(self.path)
        try:
            f.seek(offset)
            return f.read(length)
        finally:
            f.close()

    def block_nr(self, old: int) -> int:
        """Element count of a block (kept or skipped): array length for DATA blocks."""
        b = self.block(old)
        if b is not None:
            return b[4]
        s = self.skipped.get(old)
        return s[3] if s else 0

    def field(self, struct_name: str, data: bytes, path: str, kind: str = "auto", index: int = 0):
        """Value of a (possibly nested 'r.engine') field of struct_name inside data, or None. index picks an
        element of an array field (w[2], rect[2], value[4] …)."""
        off = 0
        st = struct_name
        parts = path.split(".")
        for i, part in enumerate(parts):
            f = self.structs.get(st, {}).get(part)
            if f is None:
                return None
            o, ftype, raw = f
            off += o
            if i < len(parts) - 1:
                st = ftype
                continue
            if off >= len(data):
                return None
            if raw.startswith("*") or raw.startswith("(*"):
                off += index * self.ptr
                if off + self.ptr > len(data):
                    return None
                return struct.unpack_from(self.e + self.P, data, off)[0]
            if ftype == "char" and "[" in raw:
                n = int(re.findall(r"\[(\d+)\]", raw)[0])
                s = data[off:off + n].split(b"\0")[0]
                return s.decode("utf-8", "replace")
            fmt = {"short": "h", "ushort": "H", "int": "i", "uint": "I", "float": "f", "char": "b", "uchar": "B",
                   "int64_t": "q", "uint64_t": "Q", "double": "d", "int8_t": "b", "uint8_t": "B", "int16_t": "h",
                   "int32_t": "i", "uint32_t": "I"}.get(ftype)
            if not fmt:
                return None
            off += index * struct.calcsize(fmt)
            if off + struct.calcsize(fmt) > len(data):
                return None
            return struct.unpack_from(self.e + fmt, data, off)[0]
        return None

    def sname(self, sdna: int) -> str:
        return self.struct_names[sdna] if 0 <= sdna < len(self.struct_names) else ""

    def block(self, old: int):
        i = self.by_old.get(old)
        return self.blocks[i] if i is not None else None

    def id_name(self, code_block) -> str:
        code, sdna, old, data, nr, c2 = code_block
        st = self.sname(sdna)
        n = self.field(st, data, "id.name") if data else None
        return (n or "")[2:]

    def id_lib(self, b) -> int:
        st = self.sname(b[1])
        return self.field(st, b[3], "id.lib") or 0

    def listbase(self, first: int, limit: int = 200000):
        """Blocks of a ListBase starting at its 'first' pointer (following 'next')."""
        seen = 0
        while first and seen < limit:
            b = self.block(first)
            if b is None or b[3] is None:
                return
            yield b
            seen += 1
            first = struct.unpack_from(self.e + self.P, b[3], 0)[0]     # Link.next is the first field


def _blend_rgba_image(data: bytes, w: int, h: int) -> Optional["RawImage"]:
    """Bottom-up RGBA (Blender previews, the file thumbnail) → RawImage over a neutral backdrop."""
    if not data or w <= 0 or h <= 0 or len(data) < w * h * 4 or np is None:
        return None
    a = np.frombuffer(data[:w * h * 4], np.uint8).reshape(h, w, 4)[::-1].astype(np.float32) / 255.0
    bg = np.array([0.16, 0.16, 0.17], np.float32)
    rgb = a[..., :3] * a[..., 3:4] + bg * (1 - a[..., 3:4])
    return RawImage(w, h, (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes())


def blend_thumbnail(bf: "BlendFile") -> Optional["RawImage"]:
    t = bf.thumb
    if not t or len(t) < 8:
        return None
    w, h = struct.unpack_from(bf.e + "ii", t, 0)
    return _blend_rgba_image(t[8:], w, h) if 0 < w <= 4096 and 0 < h <= 4096 else None


def blend_id_preview(bf: "BlendFile", idb) -> Optional["RawImage"]:
    """The preview render Blender stored with an ID (material balls, object / world previews)."""
    st = bf.sname(idb[1])
    pp = bf.field(st, idb[3], "id.preview") or bf.field(st, idb[3], "preview")
    owner = idb[2]
    pd = bf.read_data(pp, owner) if pp else None                  # addresses can repeat across IDs (3.x, 5.x)
    pb = bf.block(pp) if pp else None
    if pb is None or pd is None:
        return None
    pb = (pb[0], pb[1], pb[2], pd) + tuple(pb[4:])
    ps = bf.sname(pb[1]) or "PreviewImage"
    for i in (1, 0):                                             # 1 = large preview, 0 = icon
        w, h = bf.field(ps, pb[3], "w", index=i), bf.field(ps, pb[3], "h", index=i)
        rp = bf.field(ps, pb[3], "rect", index=i)
        if w and h and rp:
            img = _blend_rgba_image(bf.read_data(rp, owner) or b"", w, h)
            if img is not None:
                return img
    return None


_PRINCIPLED_IN = {"Base Color": "base_color", "Metallic": "metallic", "Roughness": "roughness", "Alpha": "opacity",
                  "Normal": "normal", "Coat Weight": "coat", "Clearcoat": "coat", "Coat Roughness": "coat_roughness",
                  "Clearcoat Roughness": "coat_roughness", "Emission Color": "_em_c", "Emission": "_em_c",
                  "Emission Strength": "_em_w", "Transmission Weight": "transmission", "Transmission": "transmission",
                  "Specular IOR Level": "specular_lvl", "Specular": "specular_lvl"}


def blend_materials(bf: "BlendFile", blend_path: str) -> list:
    """Every material: Principled BSDF values and image textures (followed through links, normal-map nodes
    and simple colour nodes), else the viewport display colour; plus Blender's own stored preview render."""
    out = []
    for mb in [b for b in bf.blocks if b[5] == "MA" and b[3] is not None]:
        st = bf.sname(mb[1])
        name = bf.id_name(mb)
        m = {"name": name}
        r, g, b_ = bf.field(st, mb[3], "r"), bf.field(st, mb[3], "g"), bf.field(st, mb[3], "b")
        if r is not None:
            m["base_color"] = (r, g, b_)
        for fld, key in (("metallic", "metallic"), ("roughness", "roughness")):
            v = bf.field(st, mb[3], fld)
            if v is not None:
                m[key] = v
        rows = []
        ntp = bf.field(st, mb[3], "nodetree")
        nt = bf.block(ntp) if ntp else None
        if nt is not None and nt[3] is not None and bf.field(st, mb[3], "use_nodes") != 0:
            ns = bf.sname(nt[1])
            nodes = {}
            first = bf.field(ns, nt[3], "nodes.first")
            for nb in bf.listbase(first or 0):
                nodes[nb[2]] = nb
            links = {}
            first = bf.field(ns, nt[3], "links.first")
            for lb in bf.listbase(first or 0):
                ls = bf.sname(lb[1])
                links[bf.field(ls, lb[3], "tosock")] = (bf.field(ls, lb[3], "fromnode"), bf.field(ls, lb[3], "fromsock"))

            def idname(nb):
                return bf.field(bf.sname(nb[1]), nb[3], "idname") or ""

            def sockets(nb, which="inputs"):
                res = []
                fp = bf.field(bf.sname(nb[1]), nb[3], which + ".first")
                for sb in bf.listbase(fp or 0):
                    ss = bf.sname(sb[1])
                    res.append((bf.field(ss, sb[3], "identifier") or bf.field(ss, sb[3], "name") or "",
                                bf.field(ss, sb[3], "name") or "", sb, bf.field(ss, sb[3], "default_value")))
                return res

            def image_of(nb, depth=0):
                if nb is None or depth > 6:
                    return None
                if idname(nb) in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment"):
                    ip = bf.field(bf.sname(nb[1]), nb[3], "id")
                    ib = bf.block(ip) if ip else None
                    if ib is not None and ib[3] is not None:
                        ist = bf.sname(ib[1])
                        raw = bf.field(ist, ib[3], "filepath") or bf.field(ist, ib[3], "name") or ""
                        if raw:
                            return _blend_path(raw, blend_path)[0]
                    return None
                for ident, nm, sb, dv in sockets(nb):
                    src = links.get(sb[2])
                    if src:
                        hit = image_of(nodes.get(src[0]), depth + 1)
                        if hit:
                            return hit
                return None
            pr = next((nb for nb in nodes.values() if idname(nb) == "ShaderNodeBsdfPrincipled"), None)
            if pr is not None:
                em_c, em_w = None, None
                for ident, nm, sb, dv in sockets(pr):
                    key = _PRINCIPLED_IN.get(nm) or _PRINCIPLED_IN.get(ident)
                    if not key:
                        continue
                    src = links.get(sb[2])
                    if src:
                        img = image_of(nodes.get(src[0]))
                        rows.append(_bn(nm, "← " + (idname(nodes.get(src[0])) if nodes.get(src[0]) else "?").replace("ShaderNode", ""),
                                        None, img or "", (os.path.exists(img) if img else None)))
                        if img:
                            tk = {"base_color": "base_color_tex", "metallic": "metallic_tex", "roughness": "roughness_tex",
                                  "normal": "normal_tex", "_em_c": "emission_tex", "opacity": "opacity_tex"}.get(key)
                            if tk:
                                m[tk] = img
                                if key == "base_color":
                                    m["base_color"] = (1.0, 1.0, 1.0)
                        continue
                    dvb = bf.block(dv) if dv else None
                    if dvb is None or dvb[3] is None:
                        continue
                    vs = bf.sname(dvb[1])
                    if vs.endswith("RGBA"):
                        val = tuple(bf.field(vs, dvb[3], "value", index=i) or 0.0 for i in range(3))
                        rows.append(_bn(nm, ", ".join(f"{c:.3g}" for c in val)))
                    elif vs.endswith("Float"):
                        val = bf.field(vs, dvb[3], "value")
                        rows.append(_bn(nm, f"{val:.3g}" if val is not None else ""))
                    else:
                        continue
                    if key == "_em_c":
                        em_c = val
                    elif key == "_em_w":
                        em_w = val
                    elif key == "specular_lvl":
                        m["specular"] = (val or 0.5) * 2
                    elif key == "base_color":
                        m["base_color"] = val
                    elif key != "normal" and isinstance(val, float):
                        m[key] = val
                if em_c and (em_w or 0) > 0:
                    m["emission"] = tuple(c * em_w for c in em_c)
        m["_rows"] = rows
        m["_preview"] = blend_id_preview(bf, mb)
        out.append(m)
    return out


def _blend_path(raw: str, blend: str) -> Tuple[str, Optional[bool]]:
    if raw.startswith("//"):
        raw = os.path.join(os.path.dirname(blend), raw[2:])
    return resolve_scene_path(raw, blend)


def breakdown_blend(path: str) -> dict:
    bf = BlendFile(path)
    v = bf.version.lstrip("0")
    ver = (f"{v[0]}.{int(v[1:])}" if len(v) == 3 else f"{v[:-2]}.{int(v[-2:])}") if v.isdigit() else bf.version
    info = [("Saved with", f"Blender {ver}")]
    if bf.compression:
        info.append(("Compression", bf.compression))
    ids = {}
    for b in bf.blocks:
        if b[5] and b[3] is not None:
            ids.setdefault(b[5], []).append(b)
    libs = {b[2]: b for b in ids.get("LI", [])}

    def lib_of(b) -> str:
        lp = bf.id_lib(b)
        if lp and lp in libs:
            lb = libs[lp]
            st = bf.sname(lb[1])
            return bf.field(st, lb[3], "filepath") or bf.field(st, lb[3], "name") or "library"
        return ""
    # objects
    obs = {}
    for b in ids.get("OB", []):
        st = bf.sname(b[1])
        t = bf.field(st, b[3], "type")
        data_ptr = bf.field(st, b[3], "data") or 0
        db = bf.block(data_ptr)
        obs[b[2]] = {"name": bf.id_name(b), "type": _BLEND_OB_TYPES.get(t, f"type {t}"),
                     "is_mesh": t == 1 or bool(bf.field(st, b[3], "instance_collection") or bf.field(st, b[3], "dup_group")),
                     "parent": bf.field(st, b[3], "parent") or 0,
                     "data": bf.id_name(db) if db and db[5] else "", "lib": lib_of(b)}
    kids: Dict[int, list] = {}
    for o_ptr, o in obs.items():
        if o["parent"] in obs:
            kids.setdefault(o["parent"], []).append(o_ptr)
    n_nodes = [0]

    def ob_node(o_ptr, depth=0):
        o = obs[o_ptr]
        n_nodes[0] += 1
        det = o["type"] + (f" · {o['data']}" if o["data"] and o["data"] != o["name"] else "")
        if o["lib"]:
            det += f" · linked from {os.path.basename(o['lib'])}"
        sub = [ob_node(k, depth + 1) for k in sorted(kids.get(o_ptr, []), key=lambda k: obs[k]["name"].lower())] \
            if depth < 64 and n_nodes[0] < 6000 else []
        # rows with geometry (here or below) can be picked to preview it in 3D
        geo = o["is_mesh"] or any(n[3].startswith("object:") for n in sub)
        return _bn(o["name"], det, sub, f"object:{o_ptr}" if geo else "")
    # collections
    cols = {b[2]: b for b in ids.get("GR", [])}
    tree, notes = [], []
    scenes = ids.get("SC", [])

    def col_node(cb, label=None, depth=0):
        st = bf.sname(cb[1])
        name = label or bf.id_name(cb)
        children, obj_nodes = [], []
        first = bf.field(st, cb[3], "children.first")
        if first:
            for link in bf.listbase(first):
                cp = bf.field(bf.sname(link[1]), link[3], "collection")
                if cp in cols and depth < 32:
                    children.append(col_node(cols[cp], depth=depth + 1))
        first = bf.field(st, cb[3], "gobject.first")
        members = []
        if first:
            for link in bf.listbase(first):
                op = bf.field(bf.sname(link[1]), link[3], "ob")
                if op in obs:
                    members.append(op)
        mset = set(members)
        for op in sorted(members, key=lambda k: obs[k]["name"].lower()):
            if obs[op]["parent"] not in mset:
                obj_nodes.append(ob_node(op))
        lib = lib_of(cb) if cb[5] else ""
        return _bn(name, (f"{len(members):,} objects" if members else "") + (f" · linked from {os.path.basename(lib)}" if lib else ""),
                   children + obj_nodes)
    scene_nodes = []
    render = {}
    for sb in scenes:
        st = bf.sname(sb[1])
        sname = bf.id_name(sb)
        eng = bf.field(st, sb[3], "r.engine") or ""
        sf, ef = bf.field(st, sb[3], "r.sfra"), bf.field(st, sb[3], "r.efra")
        fps = bf.field(st, sb[3], "r.frs_sec")
        fpsb = bf.field(st, sb[3], "r.frs_sec_base") or 1.0
        rx, ry = bf.field(st, sb[3], "r.xsch"), bf.field(st, sb[3], "r.ysch")
        pct = bf.field(st, sb[3], "r.size")
        cam = bf.block(bf.field(st, sb[3], "camera") or 0)
        world = bf.block(bf.field(st, sb[3], "world") or 0)
        rows = []
        if eng:
            rows.append(_bn("Render engine", {"BLENDER_EEVEE": "EEVEE (legacy)", "BLENDER_EEVEE_NEXT": "EEVEE",
                                              "CYCLES": "Cycles", "BLENDER_WORKBENCH": "Workbench"}.get(eng, eng)))
        if sf is not None:
            rows.append(_bn("Frame range", f"{sf} – {ef}" + (f" @ {fps / fpsb:.3g} fps" if fps else "")))
        if rx:
            rows.append(_bn("Resolution", f"{rx} × {ry}" + (f" ({pct}%)" if pct and pct != 100 else "")))
        if cam:
            rows.append(_bn("Camera", bf.id_name(cam)))
        if world:
            rows.append(_bn("World", bf.id_name(world)))
        if not render:
            render = {r[0]: r[1] for r in rows}
        mc_ptr = bf.field(st, sb[3], "master_collection")
        if mc_ptr and bf.block(mc_ptr) and bf.block(mc_ptr)[3]:
            content = col_node(bf.block(mc_ptr), label="Scene Collection")[2]
        else:                                                     # 2.7x: scene.base list
            content = []
            first = bf.field(st, sb[3], "base.first")
            if first:
                members = []
                for link in bf.listbase(first):
                    op = bf.field(bf.sname(link[1]), link[3], "object")
                    if op in obs:
                        members.append(op)
                mset = set(members)
                content = [ob_node(op) for op in sorted(members, key=lambda k: obs[k]["name"].lower()) if obs[op]["parent"] not in mset]
        scene_nodes.append(_bn(sname, f"{eng and render.get('Render engine', '')}".strip(" ·"), [_bn("Settings", "", rows)] + content,
                               f"object:scene={sb[2]}" if any(o["is_mesh"] for o in obs.values()) else ""))
    for k in ("Render engine", "Frame range", "Resolution"):
        if k in render:
            info.append((k, render[k]))
    # mesh totals
    faces = 0
    for b in ids.get("ME", []):
        st = bf.sname(b[1])
        for fld in ("faces_num", "totpoly", "totface"):
            vv = bf.field(st, b[3], fld)
            if vv:
                faces += vv
                break
    if faces:
        info.append(("Faces", f"{faces:,} in {len(ids.get('ME', [])):,} meshes"))
    info.append(("Objects", f"{len(obs):,}"))
    tree.append(_bn("Scenes", f"{len(scene_nodes)}", scene_nodes))
    # references
    found: Dict[str, list] = {}

    def add(group, raw, owner, extra=""):
        if raw:
            found.setdefault(group, []).append((raw, owner, extra))
    for b in ids.get("LI", []):
        st = bf.sname(b[1])
        add("Linked libraries", bf.field(st, b[3], "filepath") or bf.field(st, b[3], "name") or "", bf.id_name(b))
    for b in ids.get("IM", []):
        st = bf.sname(b[1])
        src = bf.field(st, b[3], "source")
        if src in (4, 5):                                         # generated / viewer
            continue
        packed = bool(bf.field(st, b[3], "packedfile"))
        if bf.field(st, b[3], "packedfiles.first"):
            packed = True
        raw = bf.field(st, b[3], "filepath") or bf.field(st, b[3], "name") or ""
        add("Images", raw, bf.id_name(b), "packed in .blend" if packed else ("UDIM tiles" if src == 6 else ("sequence" if src == 2 else ("movie" if src == 3 else ""))))
    for code, grp, fld in (("SO", "Sounds", "filepath"), ("MC", "Movie clips", "filepath"), ("VF", "Fonts", "filepath"),
                           ("CF", "Caches (Alembic / USD)", "filepath"), ("VO", "Volumes (VDB)", "filepath"),
                           ("TX", "Texts", "filepath")):
        for b in ids.get(code, []):
            st = bf.sname(b[1])
            raw = bf.field(st, b[3], fld) or bf.field(st, b[3], "name") or ""
            if code == "VF" and raw == "<builtin>":
                continue
            packed = bool(bf.field(st, b[3], "packedfile"))
            add(grp, raw, bf.id_name(b), "packed in .blend" if packed else "")
    ref_groups, nfiles, nmiss = [], 0, 0
    for grp, items in found.items():
        nodes = []
        for raw, owner, extra in sorted(items, key=lambda x: x[0].lower()):
            if extra == "packed in .blend":
                nodes.append(_bn(os.path.basename(raw.replace("\\", "/")) or owner, f"{owner} · packed in .blend", None, raw, None))
                nfiles += 1
                continue
            full, ok = _blend_path(raw, path)
            if extra == "UDIM tiles" and not ok and "<UDIM>" not in full:
                ok2 = glob.glob(re.sub(r"\.1\d\d\d\.", ".*.", full))
                ok = True if ok2 else ok
            nfiles += 1
            nmiss += ok is False
            det = owner + (f" · {extra}" if extra else "")
            if raw.startswith("//"):
                det += f" · {raw}"
            nodes.append(_bn(os.path.basename(raw.replace("\\", "/")) or owner, det, None, full, ok))
        miss = sum(1 for n in nodes if n[4] is False)
        ref_groups.append(_bn(grp, f"{len(nodes):,}" + (f" · {miss} missing" if miss else ""), _bn_capped(nodes, 800)))
    order = ["Linked libraries", "Images", "Caches (Alembic / USD)", "Volumes (VDB)", "Sounds", "Movie clips", "Fonts", "Texts"]
    ref_groups.sort(key=lambda n: order.index(n[0]) if n[0] in order else 99)
    tree.append(_bn("References", f"{nfiles:,} files" + (f" · {nmiss} missing" if nmiss else ""), ref_groups))
    if nmiss:
        info.append(("Missing files", f"{nmiss:,}"))
    images, materials = [], []
    th = blend_thumbnail(bf)
    if th is not None:
        images.append(("File thumbnail", th))
    try:
        materials = blend_materials(bf, path)
    except Exception as ex:
        notes.append(f"Materials couldn't be read in detail ({type(ex).__name__}).")
    if materials:
        mat_nodes = []
        for i, m in enumerate(sorted(materials, key=lambda m: m["name"].lower())):
            rows = m.pop("_rows", [])
            prv = m.pop("_preview", None)
            if prv is not None:
                m["_image"] = prv
            mat_nodes.append(_bn(m["name"], ("stored preview · " if prv is not None else "") + f"{len(rows)} inputs" if rows else
                                 ("stored preview" if prv is not None else "viewport colour"), rows, f"material:{i}", None))
        materials = sorted(materials, key=lambda m: m["name"].lower())
        tree.append(_bn("Materials", f"{len(materials):,}", _bn_capped(mat_nodes, 500)))
    counts = {_BLEND_ID_CODES.get(c, c): n for c, n in bf.counts.items()
              if c not in ("WM", "SN", "SR", "WS", "BR", "PL", "PC", "LS")}
    tree.append(_bn("Data-blocks", f"{sum(counts.values()):,}", _bn_counts(counts)))
    return {"kind": "Blender file", "info": info, "tree": tree, "notes": notes, "images": images, "materials": materials}


# --------------------------------------------------------------------------- dispatch + text export
SCENE_BREAKDOWN_EXTS = {".umap": "unreal", ".uasset": "unreal", ".uproject": "unreal",
                        ".unity": "unity", ".prefab": "unity", ".unitypackage": "unity",
                        ".asset": "unity-yaml", ".controller": "unity-yaml", ".overrideController": "unity-yaml",
                        ".ma": "maya", ".mb": "maya", ".blend": "blender"}


def scene_breakdown_kind(path: str) -> str:
    """'unreal' / 'unity' / 'maya' / 'blender' if Kestrel can break this file down, else ''. Extensions shared
    with other programs (.mat is also MATLAB) are only claimed when the file is Unity YAML."""
    ext = os.path.splitext(path)[1]
    k = SCENE_BREAKDOWN_EXTS.get(ext) or SCENE_BREAKDOWN_EXTS.get(ext.lower(), "")
    if k == "unity-yaml":
        try:
            with open(path, "rb") as f:
                return "unity" if f.read(5) == b"%YAML" else ""
        except OSError:
            return ""
    if k == "maya" and ext.lower() == ".ma":
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            return "maya" if (head.startswith(b"//Maya") or b"requires maya" in head or b"createNode " in head) else ""
        except OSError:
            return ""
    return k


def scene_breakdown(path: str) -> dict:
    """Run in the heavy worker: {kind, info, tree, notes} or {error}."""
    t0 = time.monotonic()
    k = scene_breakdown_kind(path)
    fn = {"unreal": breakdown_unreal, "unity": breakdown_unity, "maya": breakdown_maya, "blender": breakdown_blend}.get(k)
    if fn is None:
        return {"error": "not a scene file Kestrel can read"}
    try:
        if os.path.getsize(path) < 16:
            return {"error": "the file is empty"}
        res = fn(path)
    except struct.error:
        return {"error": "the file is truncated or not in a format Kestrel understands"}
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}
    res["seconds"] = time.monotonic() - t0
    bench(f"breakdown {k}: {os.path.basename(path)} {res['seconds']:.2f}s")
    return res


def breakdown_text(res: dict, title: str = "") -> str:
    """Plain-text version of a breakdown (Copy as text)."""
    lines = [title or res.get("kind", "")]
    for k, v in res.get("info", []):
        lines.append(f"  {k}: {v}")
    for n in res.get("notes", []):
        lines.append(f"  note: {n}")

    def walk(nodes, depth):
        for n in nodes:
            extra = []
            if n[1]:
                extra.append(n[1])
            if n[3] and not n[3].startswith(("material:", "object:")):
                extra.append(n[3] + ("  [MISSING]" if n[4] is False else ""))
            lines.append("  " * depth + "- " + n[0] + (f"   ({' · '.join(extra)})" if extra else ""))
            walk(n[2], depth + 1)
    lines.append("")
    walk(res.get("tree", []), 0)
    return "\n".join(lines) + "\n"
