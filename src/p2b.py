

# --------------------------------------------------------------------------- Batch rename (PowerRename-style)
class BatchRenameDialog(Gtk.Dialog):
    """Rename many files at once with a live preview.

    Operations are applied in this order to each file's *name* (the extension is handled separately so
    'IMG_001.jpg' keeps '.jpg'):  frame offset  →  find/replace (plain or regex)  →  prefix / suffix  →
    numbering pattern  →  case change; then the optional repath moves the result into another folder.
    The preview is debounced (recomputed ~80 ms after typing stops) and never touches the disk per file:
    destination folders are listed once and cached. Apply renames in two phases (via temporary names) so
    swaps and shifts like 1001→1061 over an overlapping range never collide."""

    CASE_OPTIONS = [("keep", "Keep case"), ("lower", "lowercase"), ("upper", "UPPERCASE"), ("title", "Title Case"), ("sentence", "Sentence case")]

    def __init__(self, parent, entries: List[Entry], on_done: Callable[[List[Tuple[str, str]]], None]):
        super().__init__(title="Batch Rename", transient_for=parent, modal=True)
        self.entries = [e for e in entries if e.path]
        self.on_done = on_done
        self.set_default_size(780, 560)
        self.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)

        # ---- controls
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        box.pack_start(grid, False, False, 0)
        r = 0
        grid.attach(Gtk.Label(label="Find:", xalign=1), 0, r, 1, 1)
        self.find = Gtk.Entry(hexpand=True, placeholder_text="text or regular expression")
        grid.attach(self.find, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="Replace with:", xalign=1), 2, r, 1, 1)
        self.replace = Gtk.Entry(hexpand=True, placeholder_text="use \\1 … for regex groups")
        grid.attach(self.replace, 3, r, 1, 1)
        r += 1
        opts = Gtk.Box(spacing=12)
        self.regex = Gtk.CheckButton(label="Regular expression")
        self.icase = Gtk.CheckButton(label="Ignore case")
        self.incl_ext = Gtk.CheckButton(label="Include extension")
        for w in (self.regex, self.icase, self.incl_ext):
            opts.pack_start(w, False, False, 0)
        grid.attach(opts, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Prefix:", xalign=1), 0, r, 1, 1)
        self.prefix = Gtk.Entry(hexpand=True)
        grid.attach(self.prefix, 1, r, 1, 1)
        grid.attach(Gtk.Label(label="Suffix:", xalign=1), 2, r, 1, 1)
        self.suffix = Gtk.Entry(hexpand=True, placeholder_text="added before the extension")
        grid.attach(self.suffix, 3, r, 1, 1)
        r += 1
        grid.attach(Gtk.Label(label="Numbering:", xalign=1), 0, r, 1, 1)
        nb = Gtk.Box(spacing=6)
        self.pattern = Gtk.Entry(hexpand=True, placeholder_text="e.g.  Scene_{n}   or   {name}_{n}   (empty = off)")
        self.pattern.set_tooltip_text("{n} = counter, {name} = current name, {ext} = extension, {date} = file date, {i} = position")
        nb.pack_start(self.pattern, True, True, 0)
        nb.pack_start(Gtk.Label(label="start"), False, False, 0)
        self.start = Gtk.SpinButton.new_with_range(0, 999999, 1)
        self.start.set_value(1)
        nb.pack_start(self.start, False, False, 0)
        nb.pack_start(Gtk.Label(label="step"), False, False, 0)
        self.step = Gtk.SpinButton.new_with_range(1, 1000, 1)
        self.step.set_value(1)
        nb.pack_start(self.step, False, False, 0)
        nb.pack_start(Gtk.Label(label="digits"), False, False, 0)
        self.digits = Gtk.SpinButton.new_with_range(1, 10, 1)
        self.digits.set_value(max(2, len(str(len(self.entries)))))
        nb.pack_start(self.digits, False, False, 0)
        grid.attach(nb, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Case:", xalign=1), 0, r, 1, 1)
        cb = Gtk.Box(spacing=12)
        self.case = Gtk.ComboBoxText()
        for key, lab in self.CASE_OPTIONS:
            self.case.append(key, lab)
        self.case.set_active_id("keep")
        cb.pack_start(self.case, False, False, 0)
        self.order = Gtk.ComboBoxText()
        for key, lab in (("name", "Number by name"), ("modified", "Number by date modified"), ("size", "Number by size"), ("selection", "Number by selection order")):
            self.order.append(key, lab)
        self.order.set_active_id("name")
        cb.pack_start(self.order, False, False, 0)
        self.only_matching = Gtk.CheckButton(label="Only rename items that match Find")
        cb.pack_start(self.only_matching, False, False, 0)
        grid.attach(cb, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Frame offset:", xalign=1), 0, r, 1, 1)
        fb = Gtk.Box(spacing=6)
        self.offset = Gtk.SpinButton.new_with_range(-999999, 999999, 1)
        self.offset.set_value(0)
        self.offset.set_tooltip_text("Adds this to the frame number in each name, keeping its padding: "
                                     "shot.1001.exr with offset 60 → shot.1061.exr (0 = off)")
        fb.pack_start(self.offset, False, False, 0)
        fb.pack_start(Gtk.Label(label="applied to the"), False, False, 0)
        self.offset_which = Gtk.ComboBoxText()
        self.offset_which.append("last", "last number in the name")
        self.offset_which.append("first", "first number in the name")
        self.offset_which.set_active_id("last")
        fb.pack_start(self.offset_which, False, False, 0)
        grid.attach(fb, 1, r, 3, 1)
        r += 1
        grid.attach(Gtk.Label(label="Move to:", xalign=1), 0, r, 1, 1)
        mb = Gtk.Box(spacing=6)
        self.dest = Gtk.Entry(hexpand=True, placeholder_text="empty = rename in place   ·   e.g.  ../v004   /mnt/show/{parent}_fixed")
        self.dest.set_tooltip_text("Repath: move the renamed files into this folder (absolute, or relative to each file's folder).\n"
                                   "{parent} = the file's folder name, {dir} = its full folder path, {ext} = its extension")
        mb.pack_start(self.dest, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._browse_dest)
        mb.pack_start(browse, False, False, 0)
        self.mkdirs = Gtk.CheckButton(label="Create missing folders")
        self.mkdirs.set_active(True)
        mb.pack_start(self.mkdirs, False, False, 0)
        grid.attach(mb, 1, r, 3, 1)

        # ---- preview
        self.store = Gtk.ListStore(str, str, str)       # old, new, status
        tv = Gtk.TreeView(model=self.store)
        self.tv = tv
        for i, title in enumerate(("Original", "New name", "")):
            rend = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
            col = Gtk.TreeViewColumn(title, rend, text=i)
            col.set_resizable(True)
            col.set_expand(i < 2)
            if i == 2:
                rend.set_property("foreground", "#d04040")
            tv.append_column(col)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.get_style_context().add_class("dim-label")
        box.pack_start(self.summary, False, False, 0)

        self._paths = {e.path for e in self.entries}
        self._dir_cache: Dict[str, Optional[set]] = {}      # destination folder → names in it (None = missing)
        self._order_key, self._order_list = None, []
        self._timer = 0
        for w in (self.find, self.replace, self.prefix, self.suffix, self.pattern, self.dest):
            w.connect("changed", lambda *_: self._schedule())
        for w in (self.regex, self.icase, self.incl_ext, self.only_matching, self.mkdirs):
            w.connect("toggled", lambda *_: self._schedule())
        for w in (self.start, self.step, self.digits, self.offset):
            w.connect("value-changed", lambda *_: self._schedule())
        for w in (self.case, self.order, self.offset_which):
            w.connect("changed", lambda *_: self._schedule())
        self.connect("response", self._response)
        self.connect("destroy", lambda *_: self._timer and GLib.source_remove(self._timer))
        self.plan: List[Tuple[str, str]] = []
        self.show_all()
        self.refresh()

    def _schedule(self):
        """Typing re-plans once the keystrokes pause (80 ms), not on every key."""
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(80, self._fire)

    def _fire(self):
        self._timer = 0
        self.refresh()
        return False

    def _browse_dest(self, *a):
        start = resolve_dest_dir(self.entries[0].path, self.dest.get_text().strip()) if (self.entries and self.dest.get_text().strip()) else \
            (os.path.dirname(self.entries[0].path) if self.entries else HOME)
        p = pick_folder(self, "Move renamed files to", start)
        if p:
            self.dest.set_text(p)

    def _names_in(self, folder: str) -> Optional[set]:
        """Names in a destination folder, listed once per dialog (None if it doesn't exist)."""
        if folder not in self._dir_cache:
            try:
                self._dir_cache[folder] = set(os.listdir(folder))
            except OSError:
                self._dir_cache[folder] = None
        return self._dir_cache[folder]

    # ---- computing the new names
    def _ordered(self) -> List[Entry]:
        key = self.order.get_active_id()
        if key == self._order_key:
            return self._order_list
        self._order_key = key
        self._order_list = self._compute_order(key)
        return self._order_list

    def _compute_order(self, key) -> List[Entry]:
        if key == "selection":
            return list(self.entries)
        if key == "modified":
            return sorted(self.entries, key=lambda e: (e.mtime, e.name.lower()))
        if key == "size":
            return sorted(self.entries, key=lambda e: (e.size or 0, e.name.lower()))
        return sorted(self.entries, key=lambda e: _natural_key(e.name))

    def _params(self) -> dict:
        """Read every control ONCE per refresh (reading GTK widgets per file was most of the old cost)."""
        import re
        find = self.find.get_text()
        pat, bad = None, False
        if find:
            try:
                pat = re.compile(find if self.regex.get_active() else re.escape(find), re.IGNORECASE if self.icase.get_active() else 0)
            except re.error:
                bad = True
        return {"pat": pat, "bad_regex": bad, "repl": self.replace.get_text() if self.regex.get_active() else self.replace.get_text().replace("\\", "\\\\"),
                "incl_ext": self.incl_ext.get_active(), "only": self.only_matching.get_active(),
                "prefix": self.prefix.get_text(), "suffix": self.suffix.get_text(), "pattern": self.pattern.get_text(),
                "digits": int(self.digits.get_value()), "case": self.case.get_active_id(),
                "offset": int(self.offset.get_value()), "which": self.offset_which.get_active_id() or "last",
                "dest": self.dest.get_text().strip(), "mkdirs": self.mkdirs.get_active()}

    def _new_name(self, e: Entry, idx: int, counter: int, P: dict) -> Tuple[Optional[str], str]:
        """(new name, problem). new None = leave this file alone; the name may equal the old one (repath only)."""
        if e.is_dir or not P["incl_ext"]:
            stem, ext = (e.name, "") if e.is_dir else os.path.splitext(e.name)
        else:
            stem, ext = e.name, ""
        if P["offset"]:
            stem, err = offset_number(stem, P["offset"], P["which"])
            if err:
                return None, err
        matched = True
        pat = P["pat"]
        if pat is not None:
            matched = bool(pat.search(stem))
            if matched:
                try:
                    stem = pat.sub(P["repl"], stem)
                except Exception:
                    return None, "bad replacement"
        if P["only"] and not matched:
            return None, ""
        stem = P["prefix"] + stem + P["suffix"]
        if P["pattern"]:
            n = str(counter).zfill(P["digits"])
            date = fmt_date(e.mtime, "%Y-%m-%d") if (e.mtime and "{date}" in P["pattern"]) else ""
            stem = (P["pattern"].replace("{n}", n).replace("{name}", stem).replace("{ext}", ext.lstrip("."))
                    .replace("{date}", date).replace("{i}", str(idx + 1)))
        case = P["case"]
        if case == "lower":
            stem = stem.lower()
        elif case == "upper":
            stem = stem.upper()
        elif case == "title":
            stem = stem.title()
        elif case == "sentence":
            stem = stem[:1].upper() + stem[1:].lower()
        return stem + ext, ""

    def refresh(self):
        P = self._params()
        if P["bad_regex"]:
            self.summary.set_text("The Find expression isn't a valid regular expression.")
            self.set_response_sensitive(Gtk.ResponseType.OK, False)
            return
        counter = int(self.start.get_value())
        step = int(self.step.get_value())
        targets: Dict[str, str] = {}
        problems = 0
        rows = []
        self.plan = []
        here = os.path.dirname(self.entries[0].path) if self.entries else ""
        for idx, e in enumerate(self._ordered()):
            new, status = self._new_name(e, idx, counter, P)
            shown = new or ""
            if new is not None:
                counter += step
                src_dir = os.path.dirname(e.path)
                ddir = resolve_dest_dir(e.path, P["dest"]) if P["dest"] else src_dir
                dest = os.path.join(ddir, new)
                if ddir != src_dir:
                    shown = os.path.relpath(dest, here) if dest.startswith(here + os.sep) else dest
                if dest == e.path:
                    new, shown = None, ""                    # nothing changes for this one
                elif "/" in new or new in (".", ".."):
                    status, new = "invalid name", None
                elif dest in targets:
                    status = "duplicate target"
                else:
                    names = self._names_in(ddir)
                    if names is None and not P["mkdirs"]:
                        status = "folder missing"
                    elif names is not None and new in names and dest not in self._paths:
                        status = "already exists"
                if not status and new is not None:
                    targets[dest] = e.path
                    self.plan.append((e.path, dest))
            if status:
                problems += 1
            rows.append([e.name, shown, status or ("" if shown else "—")])
        # refill detached from the view: one relayout instead of one per row
        self.tv.set_model(None)
        self.store.clear()
        for row in rows:
            self.store.append(row)
        self.tv.set_model(self.store)
        moving = sum(1 for s_, d_ in self.plan if os.path.dirname(s_) != os.path.dirname(d_))
        self.summary.set_text(f"{len(self.plan)} of {len(self.entries)} items will be renamed" + (f" ({moving} moved)" if moving else "")
                              + (f"   ·   {problems} problem{'s' if problems != 1 else ''} (shown in red)" if problems else ""))
        self.set_response_sensitive(Gtk.ResponseType.OK, bool(self.plan) and not problems)

    def _response(self, d, r):
        if r == Gtk.ResponseType.OK and self.plan:
            self.on_done(list(self.plan))
        self.destroy()


def offset_number(stem: str, offset: int, which: str = "last") -> Tuple[str, str]:
    """Add `offset` to the last (or first) run of digits in `stem`, keeping zero padding:
    ('shot.1001', 60) → 'shot.1061';  ('f0009', 3) → 'f0012';  ('a.0999', 2) → 'a.1001'.
    Returns (stem, problem); problem is set when there's no number or the result would be negative."""
    import re
    runs = list(re.finditer(r"\d+", stem))
    if not runs:
        return stem, "no number to offset"
    m = runs[-1] if which != "first" else runs[0]
    v = int(m.group()) + offset
    if v < 0:
        return stem, "frame would be negative"
    digits = str(v).zfill(len(m.group()))
    return stem[:m.start()] + digits + stem[m.end():], ""


def resolve_dest_dir(path: str, text: str) -> str:
    """Repath target for one file: tokens {parent} {dir} {ext}, ~, and paths relative to the file's folder."""
    src_dir = os.path.dirname(path)
    t = (text.replace("{parent}", os.path.basename(src_dir)).replace("{dir}", src_dir)
         .replace("{ext}", os.path.splitext(path)[1].lstrip(".")))
    t = os.path.expanduser(t)
    if not os.path.isabs(t):
        t = os.path.join(src_dir, t)
    return os.path.normpath(t)


def _natural_key(s: str):
    """'file2' < 'file10' — sort numbers inside names numerically."""
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def apply_renames(plan: List[Tuple[str, str]]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Rename in two phases through temporary names so A→B, B→A style plans work.
    Returns (done [(old, new)], errors)."""
    import uuid
    done, errors, temps = [], [], []
    for src, dst in plan:
        tmp = os.path.join(os.path.dirname(src), f".kestrel-rename-{uuid.uuid4().hex}")
        try:
            os.rename(src, tmp)
            temps.append((tmp, src, dst))
        except OSError as ex:
            errors.append(f"{os.path.basename(src)}: {ex}")
    for tmp, src, dst in temps:
        try:
            if os.path.lexists(dst):
                raise OSError("target exists")
            os.makedirs(os.path.dirname(dst), exist_ok=True)          # repath into a new folder
            try:
                os.rename(tmp, dst)
            except OSError as ex:
                if ex.errno != 18:                                    # EXDEV: another filesystem → copy + delete
                    raise
                shutil.move(tmp, dst)
            done.append((src, dst))
        except OSError as ex:
            errors.append(f"{os.path.basename(src)}: {ex}")
            try:
                os.rename(tmp, src)      # put it back
            except OSError:
                errors.append(f"{os.path.basename(src)}: left as {os.path.basename(tmp)}")
    return done, errors
