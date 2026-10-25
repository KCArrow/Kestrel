

# --------------------------------------------------------------------------- Batch rename (PowerRename-style)
class BatchRenameDialog(Gtk.Dialog):
    """Rename many files at once with a live preview.

    Operations are applied in this order to each file's *name* (the extension is handled separately so
    'IMG_001.jpg' keeps '.jpg'):  find/replace (plain or regex)  →  prefix / suffix  →  numbering pattern
    →  case change.  The preview updates on every keystroke; Apply performs the renames in two phases
    (via temporary names) so swaps and shifts like 1→2, 2→3 never collide."""

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

        # ---- preview
        self.store = Gtk.ListStore(str, str, str)       # old, new, status
        tv = Gtk.TreeView(model=self.store)
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

        for w in (self.find, self.replace, self.prefix, self.suffix, self.pattern):
            w.connect("changed", lambda *_: self.refresh())
        for w in (self.regex, self.icase, self.incl_ext, self.only_matching):
            w.connect("toggled", lambda *_: self.refresh())
        for w in (self.start, self.step, self.digits):
            w.connect("value-changed", lambda *_: self.refresh())
        for w in (self.case, self.order):
            w.connect("changed", lambda *_: self.refresh())
        self.connect("response", self._response)
        self.plan: List[Tuple[str, str]] = []
        self.show_all()
        self.refresh()

    # ---- computing the new names
    def _ordered(self) -> List[Entry]:
        key = self.order.get_active_id()
        if key == "selection":
            return list(self.entries)
        if key == "modified":
            return sorted(self.entries, key=lambda e: (e.mtime, e.name.lower()))
        if key == "size":
            return sorted(self.entries, key=lambda e: (e.size or 0, e.name.lower()))
        return sorted(self.entries, key=lambda e: _natural_key(e.name))

    def _new_name(self, e: Entry, idx: int, counter: int) -> Optional[str]:
        """Apply the operations; None = leave this file alone."""
        import re
        if e.is_dir or not self.incl_ext.get_active():
            stem, ext = (e.name, "") if e.is_dir else os.path.splitext(e.name)
        else:
            stem, ext = e.name, ""
        find = self.find.get_text()
        matched = True
        if find:
            flags = re.IGNORECASE if self.icase.get_active() else 0
            try:
                pat = re.compile(find if self.regex.get_active() else re.escape(find), flags)
            except re.error:
                return None
            matched = bool(pat.search(stem))
            if matched:
                try:
                    stem = pat.sub(self.replace.get_text(), stem)
                except re.error:
                    return None
        if self.only_matching.get_active() and not matched:
            return None
        stem = self.prefix.get_text() + stem + self.suffix.get_text()
        pattern = self.pattern.get_text()
        if pattern:
            n = str(counter).zfill(int(self.digits.get_value()))
            date = fmt_date(e.mtime, "%Y-%m-%d") if e.mtime else ""
            stem = (pattern.replace("{n}", n).replace("{name}", stem).replace("{ext}", ext.lstrip("."))
                    .replace("{date}", date).replace("{i}", str(idx + 1)))
        case = self.case.get_active_id()
        if case == "lower":
            stem = stem.lower()
        elif case == "upper":
            stem = stem.upper()
        elif case == "title":
            stem = stem.title()
        elif case == "sentence":
            stem = stem[:1].upper() + stem[1:].lower()
        new = stem + ext
        return new if new and new != e.name else None

    def refresh(self):
        self.store.clear()
        self.plan = []
        counter = int(self.start.get_value())
        step = int(self.step.get_value())
        targets: Dict[str, str] = {}
        problems = 0
        for idx, e in enumerate(self._ordered()):
            new = self._new_name(e, idx, counter)
            status = ""
            if new is not None:
                counter += step
                dest = os.path.join(os.path.dirname(e.path), new)
                if "/" in new or new in (".", ".."):
                    status, new = "invalid name", None
                elif dest in targets:
                    status = "duplicate target"
                elif os.path.lexists(dest) and dest not in {x.path for x in self.entries}:
                    status = "already exists"
                if status:
                    problems += 1
                else:
                    targets[dest] = e.path
                    self.plan.append((e.path, dest))
            self.store.append([e.name, new or "", status or ("" if new else "—")])
        self.summary.set_text(f"{len(self.plan)} of {len(self.entries)} items will be renamed" + (f"   ·   {problems} problem{'s' if problems != 1 else ''} (shown in red)" if problems else ""))
        self.set_response_sensitive(Gtk.ResponseType.OK, bool(self.plan) and not problems)

    def _response(self, d, r):
        if r == Gtk.ResponseType.OK and self.plan:
            self.on_done(list(self.plan))
        self.destroy()


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
            os.rename(tmp, dst)
            done.append((src, dst))
        except OSError as ex:
            errors.append(f"{os.path.basename(src)}: {ex}")
            try:
                os.rename(tmp, src)      # put it back
            except OSError:
                errors.append(f"{os.path.basename(src)}: left as {os.path.basename(tmp)}")
    return done, errors
