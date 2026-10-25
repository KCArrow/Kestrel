

# --------------------------------------------------------------------------- small dialog helpers
def message(parent, title: str, text: str, kind=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, secondary: str = "") -> int:
    if STRESS is not None:                 # stress run: a modal box would stall the script; log it instead
        print(f"stress: [message] {title}: {text}", file=sys.stderr, flush=True)
        return Gtk.ResponseType.OK
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind, buttons=buttons, text=title)
    if text:
        d.format_secondary_text(text)
    r = d.run()
    d.destroy()
    return r


def ask_yes_no(parent, title: str, text: str, default_no: bool = False) -> bool:
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                          buttons=Gtk.ButtonsType.YES_NO, text=title)
    d.format_secondary_text(text)
    d.set_default_response(Gtk.ResponseType.NO if default_no else Gtk.ResponseType.YES)
    r = d.run()
    d.destroy()
    return r == Gtk.ResponseType.YES


def ask_text(parent, title: str, label: str, value: str = "", select_stem: bool = False) -> Optional[str]:
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_OK", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    box = d.get_content_area()
    box.set_spacing(6)
    box.set_border_width(12)
    box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
    entry = Gtk.Entry(text=value, activates_default=True, width_chars=50)
    box.pack_start(entry, False, False, 0)
    d.show_all()
    if select_stem and value:
        stem = os.path.splitext(value)[0]
        entry.select_region(0, len(stem))
    r = d.run()
    txt = entry.get_text()
    d.destroy()
    return txt if r == Gtk.ResponseType.OK else None


def pick_folder(parent, title: str, start: str) -> Optional[str]:
    d = Gtk.FileChooserDialog(title=title, transient_for=parent, action=Gtk.FileChooserAction.SELECT_FOLDER)
    d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
    d.set_default_response(Gtk.ResponseType.OK)
    if start and os.path.isdir(start):
        d.set_current_folder(start)
    d.set_create_folders(True)
    r = d.run()
    p = d.get_filename()
    d.destroy()
    return p if r == Gtk.ResponseType.OK else None


def conflict_dialog(parent, src: str, dst: str) -> Tuple[Optional[str], bool]:
    """Returns (replace|skip|keep|None, apply_to_all)."""
    both_dirs = os.path.isdir(src) and os.path.isdir(dst)
    what = "folder" if both_dirs else "file"
    d = Gtk.Dialog(title="Replace or Skip Files", transient_for=parent, modal=True)
    d.add_buttons("_Replace", 1, "_Skip", 2, "_Keep both", 3, "_Cancel", Gtk.ResponseType.CANCEL)
    d.set_default_response(1)
    box = d.get_content_area()
    box.set_border_width(12)
    box.set_spacing(8)
    lab = Gtk.Label(xalign=0, wrap=True)
    lab.set_markup(f"The destination already has a {what} named <b>{GLib.markup_escape_text(os.path.basename(dst))}</b>."
                   + ("\nMerging folders replaces files with the same name." if both_dirs else ""))
    box.pack_start(lab, False, False, 0)
    grid = Gtk.Grid(column_spacing=12, row_spacing=4)
    for i, (title, p) in enumerate((("Source", src), ("Destination", dst))):
        try:
            st = os.stat(p)
            info = f"{'folder' if os.path.isdir(p) else fmt_size(st.st_size)}  ·  {fmt_date(st.st_mtime, '%Y-%m-%d %H:%M')}"
        except OSError:
            info = "?"
        grid.attach(Gtk.Label(label=title + ":", xalign=0), 0, i, 1, 1)
        grid.attach(Gtk.Label(label=info, xalign=0), 1, i, 1, 1)
    box.pack_start(grid, False, False, 0)
    chk = Gtk.CheckButton(label="Do this for all conflicts")
    box.pack_start(chk, False, False, 0)
    d.show_all()
    r = d.run()
    all_ = chk.get_active()
    d.destroy()
    return {1: "replace", 2: "skip", 3: "keep"}.get(r), all_


class ProgressDialog(Gtk.Window):
    """Windows 11-style file-operation window: percent, bar, speed graph, items/time remaining, Pause, Cancel."""

    def __init__(self, parent, op, title: str):
        super().__init__(title=title, transient_for=parent, type_hint=Gdk.WindowTypeHint.DIALOG)
        self.op = op
        self.set_default_size(520, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.last_t, self.last_done = time.time(), 0
        self.rate = 0.0
        self.expanded = True
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        self.add(v)
        self.head = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.head.set_markup(f"<span size='large'>{GLib.markup_escape_text(title)}</span>")
        v.pack_start(self.head, False, False, 0)
        self.pct = Gtk.Label(xalign=0)
        self.pct.set_markup("<span size='x-large' weight='bold'>0%</span> complete")
        v.pack_start(self.pct, False, False, 0)
        self.bar = Gtk.ProgressBar()
        v.pack_start(self.bar, False, False, 0)
        self.graph = Gtk.DrawingArea()
        self.graph.set_size_request(-1, 70)
        self.graph.connect("draw", self._draw_graph)
        v.pack_start(self.graph, False, False, 0)
        self.details = Gtk.Grid(column_spacing=12, row_spacing=3)
        self.rows = {}
        for i, key in enumerate(("Name", "Speed", "Items remaining", "Time remaining")):
            k = Gtk.Label(label=key + ":", xalign=0)
            k.get_style_context().add_class("dim-label")
            val = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
            self.details.attach(k, 0, i, 1, 1)
            self.details.attach(val, 1, i, 1, 1)
            self.rows[key] = val
        v.pack_start(self.details, False, False, 0)
        hb = Gtk.Box(spacing=6)
        self.more = Gtk.Button(label="Fewer details", relief=Gtk.ReliefStyle.NONE)
        self.more.connect("clicked", self._toggle)
        hb.pack_start(self.more, False, False, 0)
        self.btn_cancel = Gtk.Button(label="Cancel")
        self.btn_cancel.connect("clicked", lambda b: op.cancel.cancel())
        hb.pack_end(self.btn_cancel, False, False, 0)
        self.btn_pause = Gtk.Button(label="Pause")
        self.btn_pause.connect("clicked", self._pause)
        hb.pack_end(self.btn_pause, False, False, 0)
        v.pack_start(hb, False, False, 0)
        self.connect("delete-event", lambda w, e: (op.cancel.cancel(), True)[1])
        self.timer = GLib.timeout_add(150, self.tick)
        self.show_all()

    def _toggle(self, b):
        self.expanded = not self.expanded
        self.details.set_visible(self.expanded)
        self.graph.set_visible(self.expanded)
        self.more.set_label("Fewer details" if self.expanded else "More details")
        self.resize(520, 1)

    def _pause(self, b):
        op = self.op
        if op.paused.is_set():
            op.paused.clear()
            self.btn_pause.set_label("Pause")
        else:
            op.paused.set()
            self.btn_pause.set_label("Resume")

    def _draw_graph(self, w, cr):
        a = w.get_allocation()
        sc = w.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.06)
        cr.rectangle(0, 0, a.width, a.height)
        cr.fill()
        samples = self.op.speed_samples[-120:]
        if len(samples) < 2:
            return False
        mx = max(samples) or 1.0
        step = a.width / max(1, len(samples) - 1)
        cr.set_source_rgba(0.0, 0.47, 0.84, 0.35)
        cr.move_to(0, a.height)
        for i, sp in enumerate(samples):
            cr.line_to(i * step, a.height - (sp / mx) * (a.height - 6))
        cr.line_to((len(samples) - 1) * step, a.height)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(0.0, 0.47, 0.84, 1.0)
        cr.set_line_width(1.5)
        for i, sp in enumerate(samples):
            (cr.move_to if i == 0 else cr.line_to)(i * step, a.height - (sp / mx) * (a.height - 6))
        cr.stroke()
        return False

    def tick(self):
        op = self.op
        if op.finished:
            return False
        now = time.time()
        dt = now - self.last_t
        if dt >= 0.5:
            inst = (op.done - self.last_done) / dt
            self.rate = inst if self.rate == 0 else self.rate * 0.6 + inst * 0.4
            op.speed_samples.append(max(0.0, inst))
            self.last_t, self.last_done = now, op.done
            self.graph.queue_draw()
        paused = op.paused.is_set()
        if op.total:
            frac = min(1.0, op.done / op.total)
            self.bar.set_fraction(frac)
            self.pct.set_markup(f"<span size='x-large' weight='bold'>{int(frac * 100)}%</span> complete" + ("  (paused)" if paused else ""))
            remain_b = op.total - op.done
            self.rows["Time remaining"].set_text(_fmt_eta(remain_b / self.rate) if self.rate > 1 and not paused else ("Paused" if paused else "Calculating…"))
        else:
            self.bar.pulse()
            self.pct.set_markup("<span size='x-large' weight='bold'>…</span> " + ("paused" if paused else "working"))
            self.rows["Time remaining"].set_text("Calculating…")
        self.rows["Name"].set_text(os.path.basename(op.current.rstrip("/")) or "Preparing…")
        self.rows["Speed"].set_text(f"{fmt_size(int(self.rate))}/s" if self.rate > 0 else "—")
        left_items = max(0, op.total_items - op.done_items)
        self.rows["Items remaining"].set_text(f"{left_items:,} ({fmt_size(max(0, op.total - op.done))})" if op.total_items else "—")
        return True


def _fmt_eta(secs: float) -> str:
    secs = int(secs)
    if secs < 5:
        return "A few seconds"
    if secs < 60:
        return f"About {secs} seconds"
    if secs < 3600:
        return f"About {secs // 60} minute{'s' if secs // 60 != 1 else ''}"
    return f"About {secs // 3600} h {(secs % 3600) // 60} min"


# --------------------------------------------------------------------------- Properties
class PropertiesDialog(Gtk.Dialog):
    def __init__(self, parent, e: Entry, cfg: dict, on_rename: Callable[[str, str], bool]):
        super().__init__(title=f"{e.name} Properties", transient_for=parent, modal=True)
        self.e, self.cfg, self.on_rename = e, cfg, on_rename
        self.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Apply", Gtk.ResponseType.APPLY, "_OK", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(480, -1)
        self.cancel = threading.Event()
        nb = Gtk.Notebook()
        self.get_content_area().pack_start(nb, True, True, 0)
        try:
            self.st = os.lstat(e.path)
        except OSError:
            self.st = None
        nb.append_page(self._general(), Gtk.Label(label="General"))
        nb.append_page(self._security(), Gtk.Label(label="Security"))
        nb.append_page(self._details(), Gtk.Label(label="Details"))
        self.connect("response", self._response)
        self.connect("destroy", lambda *_: self.cancel.set())
        self.show_all()

    def _grid(self):
        g = Gtk.Grid(column_spacing=12, row_spacing=6, border_width=12)
        return g

    def _row(self, g, r, key, widget):
        g.attach(Gtk.Label(label=key, xalign=1), 0, r, 1, 1)
        if isinstance(widget, str):
            widget = Gtk.Label(label=widget, xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        widget.set_hexpand(True)
        g.attach(widget, 1, r, 1, 1)
        return widget

    def _general(self):
        e, st, cfg = self.e, self.st, self.cfg
        g = self._grid()
        hb = Gtk.Box(spacing=8)
        img = Gtk.Image.new_from_gicon(e.icon or Gio.ThemedIcon.new("text-x-generic"), Gtk.IconSize.DIALOG)
        hb.pack_start(img, False, False, 0)
        self.name_entry = Gtk.Entry(text=e.name, hexpand=True)
        hb.pack_start(self.name_entry, True, True, 0)
        g.attach(hb, 0, 0, 2, 1)
        g.attach(Gtk.Separator(), 0, 1, 2, 1)
        r = 2
        self._row(g, r, "Type of file:", e.type); r += 1
        if not e.is_dir:
            app = Gio.AppInfo.get_default_for_type(e.content_type, False) if e.content_type else None
            self._row(g, r, "Opens with:", app.get_display_name() if app else "Unknown application"); r += 1
        self._row(g, r, "Location:", pretty_path(os.path.dirname(e.path))); r += 1
        self.size_lab = self._row(g, r, "Size:", fmt_size(e.size or 0) + f" ({(e.size or 0):,} bytes)"); r += 1
        blocks = (st.st_blocks * 512) if st else 0
        self._row(g, r, "Size on disk:", f"{fmt_size(blocks)} ({blocks:,} bytes)"); r += 1
        if e.is_dir:
            self.contains_lab = self._row(g, r, "Contains:", "calculating…"); r += 1
            threading.Thread(target=self._calc, daemon=True).start()
        g.attach(Gtk.Separator(), 0, r, 2, 1); r += 1
        f = "%A, %B %d, %Y, %H:%M:%S"
        self._row(g, r, "Created:", fmt_date(e.ctime, f) or fmt_date(st.st_ctime if st else 0, f) + "  (inode change)"); r += 1
        self._row(g, r, "Modified:", fmt_date(st.st_mtime if st else e.mtime, f)); r += 1
        self._row(g, r, "Accessed:", fmt_date(st.st_atime if st else e.atime, f)); r += 1
        g.attach(Gtk.Separator(), 0, r, 2, 1); r += 1
        mode = _stat.S_IMODE(st.st_mode) if st else 0
        self.ro = Gtk.CheckButton(label="Read-only", active=not (mode & 0o222))
        self.hidden = Gtk.CheckButton(label="Hidden (dot-file)", active=e.name.startswith("."))
        g.attach(Gtk.Label(label="Attributes:", xalign=1), 0, r, 1, 1)
        g.attach(self.ro, 1, r, 1, 1); r += 1
        g.attach(self.hidden, 1, r, 1, 1); r += 1
        return g

    def _calc(self):
        f, d, b = dir_stats_cached(self.e.path, self.cancel)
        GLib.idle_add(self._calc_done, f, d, b)

    def _calc_done(self, f, d, b):
        if not self.cancel.is_set():
            self.size_lab.set_text(f"{fmt_size(b)} ({b:,} bytes)")
            self.contains_lab.set_text(f"{f:,} Files, {d:,} Folders")

    def _security(self):
        st = self.st
        g = self._grid()
        owner = self.e.owner
        try:
            import grp
            group = grp.getgrgid(st.st_gid).gr_name if st else ""
        except Exception:
            group = str(st.st_gid) if st else ""
        self._row(g, 0, "Owner:", f"{owner} ({st.st_uid})" if st else owner)
        self._row(g, 1, "Group:", f"{group} ({st.st_gid})" if st else group)
        g.attach(Gtk.Separator(), 0, 2, 2, 1)
        pg = Gtk.Grid(column_spacing=18, row_spacing=4)
        for c, lab in enumerate(("", "Read", "Write", "Execute")):
            pg.attach(Gtk.Label(label=lab), c, 0, 1, 1)
        mode = _stat.S_IMODE(st.st_mode) if st else 0
        self.perm_checks = []
        for r, (who, shift) in enumerate((("Owner", 6), ("Group", 3), ("Others", 0)), start=1):
            pg.attach(Gtk.Label(label=who, xalign=0), 0, r, 1, 1)
            row = []
            for c, bit in enumerate((4, 2, 1), start=1):
                cb = Gtk.CheckButton(active=bool(mode & (bit << shift)))
                cb.set_halign(Gtk.Align.CENTER)
                pg.attach(cb, c, r, 1, 1)
                row.append(cb)
            self.perm_checks.append(row)
        g.attach(Gtk.Label(label="Permissions:", xalign=1, valign=Gtk.Align.START), 0, 3, 1, 1)
        g.attach(pg, 1, 3, 1, 1)
        self.mode_lab = self._row(g, 4, "Mode:", f"{_stat.filemode(st.st_mode) if st else ''}  ({oct(mode)})")
        note = Gtk.Label(label="Changing the owner requires root (use chown in a terminal).", xalign=0)
        note.get_style_context().add_class("dim-label")
        g.attach(note, 0, 5, 2, 1)
        return g

    def _details(self):
        e, st = self.e, self.st
        g = self._grid()
        rows = [("Full path:", e.path), ("MIME type:", e.content_type or "unknown"),
                ("Inode:", str(st.st_ino) if st else ""), ("Device:", str(st.st_dev) if st else ""),
                ("Hard links:", str(st.st_nlink) if st else ""),
                ("Symbolic link:", ("yes → " + os.readlink(e.path)) if e.is_link else "no"),
                ("Extension:", e.ext or "(none)")]
        for i, (k, v) in enumerate(rows):
            self._row(g, i, k, v)
        if not e.is_dir:
            lab = self._row(g, len(rows), "SHA-256:", "computing…")
            lab.set_selectable(True)

            def work():
                import hashlib
                h = hashlib.sha256()
                try:
                    with open(e.path, "rb") as f:
                        while not self.cancel.is_set():
                            buf = f.read(1 << 20)
                            if not buf:
                                break
                            h.update(buf)
                    digest = h.hexdigest() if not self.cancel.is_set() else ""
                except OSError:
                    digest = "unavailable"
                GLib.idle_add(lambda: (lab.set_text(digest), False)[1])
            threading.Thread(target=work, daemon=True).start()
        return g

    def _apply(self):
        e, st = self.e, self.st
        if st is None:
            return
        mode = _stat.S_IMODE(st.st_mode)
        new_mode = mode & 0o7000
        for row, shift in zip(self.perm_checks, (6, 3, 0)):
            for cb, bit in zip(row, (4, 2, 1)):
                if cb.get_active():
                    new_mode |= bit << shift
        if self.ro.get_active():
            new_mode &= ~0o222
        elif (new_mode & 0o222) == 0 and (mode & 0o222) == 0:
            new_mode |= 0o200
        if new_mode != mode:
            try:
                os.chmod(e.path, new_mode)
                self.st = os.lstat(e.path)
                self.mode_lab.set_text(f"{_stat.filemode(self.st.st_mode)}  ({oct(new_mode)})")
            except OSError as ex:
                message(self, "Properties", f"Couldn't change permissions: {ex}", Gtk.MessageType.ERROR)
        name = self.name_entry.get_text().strip()
        if self.hidden.get_active() and not name.startswith("."):
            name = "." + name
        elif not self.hidden.get_active() and name.startswith("."):
            name = name[1:]
        if name and name != e.name:
            if self.on_rename(e.path, os.path.join(os.path.dirname(e.path), name)):
                e.path = os.path.join(os.path.dirname(e.path), name)
                e.name = name
                self.name_entry.set_text(name)
                self.set_title(f"{name} Properties")

    def _response(self, d, r):
        if r in (Gtk.ResponseType.APPLY, Gtk.ResponseType.OK):
            self._apply()
        if r != Gtk.ResponseType.APPLY:
            self.cancel.set()
            self.destroy()


class MultiPropertiesDialog(Gtk.Dialog):
    def __init__(self, parent, sel: List[Entry]):
        super().__init__(title=f"{len(sel)} Items Properties", transient_for=parent, modal=True)
        self.add_button("_OK", Gtk.ResponseType.OK)
        self.cancel = threading.Event()
        g = Gtk.Grid(column_spacing=12, row_spacing=6, border_width=12)
        nf = sum(1 for e in sel if not e.is_dir)
        g.attach(Gtk.Label(label="Type:", xalign=1), 0, 0, 1, 1)
        g.attach(Gtk.Label(label=f"Multiple Types ({nf} Files, {len(sel) - nf} Folders)", xalign=0), 1, 0, 1, 1)
        g.attach(Gtk.Label(label="Location:", xalign=1), 0, 1, 1, 1)
        g.attach(Gtk.Label(label=f"All in {pretty_path(os.path.dirname(sel[0].path))}", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE), 1, 1, 1, 1)
        g.attach(Gtk.Label(label="Size:", xalign=1), 0, 2, 1, 1)
        self.size_lab = Gtk.Label(label="calculating…", xalign=0)
        g.attach(self.size_lab, 1, 2, 1, 1)
        g.attach(Gtk.Label(label="Contains:", xalign=1), 0, 3, 1, 1)
        self.cont_lab = Gtk.Label(label="…", xalign=0)
        g.attach(self.cont_lab, 1, 3, 1, 1)
        self.get_content_area().pack_start(g, True, True, 0)
        self.connect("response", lambda *_: (self.cancel.set(), self.destroy()))
        self.show_all()
        threading.Thread(target=self._calc, args=(sel,), daemon=True).start()

    def _calc(self, sel):
        f = d = b = 0
        for e in sel:
            if self.cancel.is_set():
                return
            if e.is_dir:
                ff, dd, bb = dir_stats(e.path, self.cancel)
                f, d, b = f + ff, d + dd + 1, b + bb
            else:
                f, b = f + 1, b + (e.size or 0)
        GLib.idle_add(lambda: (self.size_lab.set_text(f"{fmt_size(b)} ({b:,} bytes)"), self.cont_lab.set_text(f"{f:,} Files, {d:,} Folders")) and False)


# --------------------------------------------------------------------------- Folder Options
class OptionsDialog(Gtk.Dialog):
    def __init__(self, parent, cfg: dict, on_apply: Callable, page: int = 0):
        super().__init__(title="Preferences", transient_for=parent, modal=True)
        self.cfg, self.on_apply = cfg, on_apply
        self.add_buttons("Restore _Defaults", 10, "_Cancel", Gtk.ResponseType.CANCEL, "_OK", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        nb = Gtk.Notebook()
        self.get_content_area().pack_start(nb, True, True, 0)
        self.w: Dict[str, Gtk.Widget] = {}
        nb.append_page(self._general(), Gtk.Label(label="General"))
        nb.append_page(self._view(), Gtk.Label(label="View"))
        nb.append_page(self._search(), Gtk.Label(label="Search"))
        nb.append_page(self._dependencies(), Gtk.Label(label="Optional"))
        self.set_default_size(720, 560)
        self.show_all()
        nb.set_current_page(page)
        self.connect("response", self._response)

    @staticmethod
    def _vbox():
        return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=12)

    @staticmethod
    def _heading(box, text):
        l = Gtk.Label(xalign=0)
        l.set_markup(f"<b>{text}</b>")
        l.set_margin_top(8)
        box.pack_start(l, False, False, 0)

    def _radios(self, box, key, options):
        group = None
        for value, label in options:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = group or rb
            rb.set_active(self.cfg[key] == value)
            rb.value = value
            box.pack_start(rb, False, False, 0)
            self.w.setdefault("radio:" + key, []).append(rb)

    def _check(self, box, key, label):
        cb = Gtk.CheckButton(label=label, active=bool(self.cfg[key]))
        box.pack_start(cb, False, False, 0)
        self.w[key] = cb

    def _general(self):
        b = self._vbox()
        self._heading(b, "Open Kestrel to:")
        self._radios(b, "open_to", [("quick", "Quick access"), ("pc", "This PC"), ("home", "Home folder")])
        self._heading(b, "Browse folders")
        self._radios(b, "browse_in_new_tab", [(False, "Open each folder in the same tab"), (True, "Open each folder in its own tab")])
        self._heading(b, "Click items as follows")
        self._radios(b, "single_click", [(True, "Single-click to open an item (point to select)"), (False, "Double-click to open an item (single-click to select)")])
        self._heading(b, "Archives (zip, 7z, tar…)")
        self._radios(b, "archive_action", [("extract", "Double-click extracts to a folder beside the archive and opens it"),
                                           ("open", "Double-click opens the archive with the default application")])
        self._heading(b, "Performance")
        row = Gtk.Box(spacing=6)
        row.pack_start(Gtk.Label(label="CPU cores reserved for the interface:"), False, False, 0)
        self.cores_spin = Gtk.SpinButton.new_with_range(0, max(0, os.cpu_count() or 0), 1)
        self.cores_spin.set_value(int(self.cfg.get("ui_cores", 2)))
        self.cores_spin.set_tooltip_text("Preview decoding, 3D rendering and thumbnails run on the other cores so the window never stalls.\n0 disables pinning. Takes effect after a restart.")
        row.pack_start(self.cores_spin, False, False, 0)
        b.pack_start(row, False, False, 0)
        self._heading(b, "Privacy")
        self._check(b, "show_recent", "Show recently used files in Quick access")
        self._check(b, "show_frequent", "Show frequently used folders in Quick access")
        btn = Gtk.Button(label="Clear Kestrel history", halign=Gtk.Align.START)
        btn.connect("clicked", lambda *_: (self.cfg["recent"].clear(), self.cfg["frequent"].clear()))
        b.pack_start(btn, False, False, 4)
        return b

    def _view(self):
        b = self._vbox()
        self._heading(b, "Files and Folders")
        for key, lab in (("show_hidden", "Show hidden files, folders, and drives"),
                         ("hide_extensions", "Hide extensions for known file types"),
                         ("full_path_title", "Display the full path in the title bar"),
                         ("folders_first", "Show folders before files"),
                         ("confirm_delete", "Display delete confirmation dialog"),
                         ("checkboxes", "Use check boxes to select items"),
                         ("size_in_bytes", "Display file sizes in bytes")):
            self._check(b, key, lab)
        self._heading(b, "Panes")
        for key, lab in (("nav_pane", "Show navigation pane"), ("preview_pane", "Show preview pane"), ("details_pane", "Show details pane")):
            self._check(b, key, lab)
        self._heading(b, "Date format (strftime)")
        e = Gtk.Entry(text=self.cfg["date_format"])
        b.pack_start(e, False, False, 0)
        self.w["date_format"] = e
        return b

    def _search(self):
        b = self._vbox()
        self._heading(b, "How to search")
        self._check(b, "search_subfolders", "Include subfolders in search results")
        self._check(b, "search_contents", "Always search file names and contents (slower)")
        self._heading(b, "File index (faster navigation and search)")
        self._check(b, "index_enabled", "Keep an encrypted index of my folders")
        info = Gtk.Label(xalign=0, wrap=True)
        info.set_markup("<small>Folders you open paint instantly from the index while the real listing loads, and name searches "
                        "answer from it immediately. The index is AES-256 encrypted and authenticated; its key is kept in your desktop "
                        "keyring (or a private key file), never next to the index. Plaintext exists only in Kestrel's memory.</small>")
        b.pack_start(info, False, False, 0)
        b.pack_start(Gtk.Label(label="Folders to index:", xalign=0), False, False, 0)
        self.roots_store = Gtk.ListStore(str)
        for r in self.cfg.get("index_roots", [HOME]):
            self.roots_store.append([r])
        tv = Gtk.TreeView(model=self.roots_store, headers_visible=False)
        tv.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        sw = Gtk.ScrolledWindow(min_content_height=90)
        sw.add(tv)
        b.pack_start(sw, False, False, 0)
        row = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add folder…")
        rem = Gtk.Button(label="Remove")
        add.connect("clicked", lambda w: self._root_add())
        rem.connect("clicked", lambda w: self._root_remove(tv))
        row.pack_start(add, False, False, 0)
        row.pack_start(rem, False, False, 0)
        b.pack_start(row, False, False, 0)
        self._check(b, "index_network", "Include network shares (slow to crawl; off by default)")
        self._check(b, "index_hidden", "Include hidden files and folders")
        row2 = Gtk.Box(spacing=6)
        row2.pack_start(Gtk.Label(label="Re-check every"), False, False, 0)
        self.index_interval = Gtk.SpinButton.new_with_range(1, 168, 1)
        self.index_interval.set_value(int(self.cfg.get("index_interval_h", 6)))
        row2.pack_start(self.index_interval, False, False, 0)
        row2.pack_start(Gtk.Label(label="hours (only changed folders are re-read)"), False, False, 0)
        b.pack_start(row2, False, False, 0)
        row3 = Gtk.Box(spacing=6)
        upd = Gtk.Button(label="Update now")
        full = Gtk.Button(label="Rebuild from scratch")
        wipe = Gtk.Button(label="Delete index and key")
        wipe.get_style_context().add_class("destructive-action")
        upd.connect("clicked", lambda w: self._index_action("update"))
        full.connect("clicked", lambda w: self._index_action("full"))
        wipe.connect("clicked", lambda w: self._index_action("wipe"))
        for w_ in (upd, full):
            row3.pack_start(w_, False, False, 0)
        row3.pack_end(wipe, False, False, 0)
        b.pack_start(row3, False, False, 0)
        self.index_status = Gtk.Label(xalign=0, wrap=True)
        self.index_status.get_style_context().add_class("dim-label")
        b.pack_start(self.index_status, False, False, 0)
        self._index_status_refresh()
        GLib.timeout_add(1500, self._index_status_tick)
        return b

    def _index_status_tick(self):
        if not self.get_visible():
            return False
        self._index_status_refresh()
        return True

    def _index_status_refresh(self):
        parent = self.get_transient_for()
        try:
            self.index_status.set_text(parent.index_status_line() if parent is not None and hasattr(parent, "index_status_line") else "")
        except Exception:
            pass

    def _root_add(self):
        d = Gtk.FileChooserDialog(title="Folder to index", transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK and d.get_filename():
            p = d.get_filename()
            if p not in [r[0] for r in self.roots_store]:
                self.roots_store.append([p])
        d.destroy()

    def _root_remove(self, tv):
        model, it = tv.get_selection().get_selected()
        if it:
            model.remove(it)

    def _index_action(self, what: str):
        parent = self.get_transient_for()
        self._collect()                                  # use the roots/options as currently edited
        if what == "wipe":
            if ask_yes_no(self, "Delete the file index?", "The encrypted index and its key will be deleted. Navigation and search fall back to reading the disk.", default_no=True):
                INDEX.stop()
                INDEX.wipe()
                self._index_status_refresh()
            return
        if parent is not None and hasattr(parent, "index_now"):
            parent.cfg.update({k: self.cfg[k] for k in ("index_roots", "index_network", "index_hidden")})
            parent.index_now(full=(what == "full"))

    def _rv_detect(self):
        found = find_rv(self.rv_entry.get_text().strip())
        self.rv_status.set_text(f"Detected: {found}" if found else "Not found — install OpenRV (github.com/AcademySoftwareFoundation/OpenRV) or point this at its bin/rv")

    def _pick_rv(self, btn):
        d = Gtk.FileChooserDialog(title="Locate the rv executable", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK:
            self.rv_entry.set_text(d.get_filename() or "")
        d.destroy()

    def _dependencies(self):
        """The 'Optional' page: everything Kestrel can use but doesn't need — hardware acceleration, the
        OpenRV integration, the optional-tool checklist with install/copy actions, and the diagnostics report."""
        outer = self._vbox()
        self._heading(outer, "3D preview")
        self.gl_check = Gtk.CheckButton(label="Hardware-accelerated 3D preview (OpenGL) — models render white; software renderer is blue",
                                        active=bool(self.cfg.get("gl_preview", True)))
        self.gl_check.set_sensitive(HAVE_GL)
        if not HAVE_GL:
            self.gl_check.set_tooltip_text("PyOpenGL is not installed — tick it in the list below")
        outer.pack_start(self.gl_check, False, False, 0)
        self._heading(outer, "OpenRV")
        self._check(outer, "rv_enabled", "Enable OpenRV (right-click → Open in RV, Ctrl+Shift+R)")
        row = Gtk.Box(spacing=6)
        row.pack_start(Gtk.Label(label="RV executable:"), False, False, 0)
        self.rv_entry = Gtk.Entry(text=self.cfg.get("rv_path", ""), hexpand=True, placeholder_text="auto-detect (rv on PATH, $RV_HOME, /opt/rv*, ~/OpenRV*)")
        row.pack_start(self.rv_entry, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._pick_rv)
        row.pack_start(browse, False, False, 0)
        outer.pack_start(row, False, False, 0)
        self.rv_status = Gtk.Label(xalign=0, wrap=True)
        self.rv_status.get_style_context().add_class("dim-label")
        outer.pack_start(self.rv_status, False, False, 0)
        self.rv_entry.connect("changed", lambda e: self._rv_detect())
        self._rv_detect()
        self._heading(outer, "Optional tools")
        self.dep_status = Gtk.Label(label="Checking what's installed…", xalign=0)
        outer.pack_start(self.dep_status, False, False, 0)
        self.dep_grid = Gtk.Grid(column_spacing=12, row_spacing=3)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True, min_content_height=220)
        sw.add(self.dep_grid)
        outer.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        self.dep_install = Gtk.Button(label="Install selected in terminal…", sensitive=False)
        self.dep_install.get_style_context().add_class("suggested-action")
        self.dep_install.connect("clicked", lambda b: self._dep_install())
        self.dep_copy = Gtk.Button(label="Copy commands", sensitive=False)
        self.dep_copy.connect("clicked", lambda b: self._dep_copy())
        refresh = Gtk.Button(label="Re-check")
        refresh.connect("clicked", lambda b: self._dep_refresh())
        row.pack_start(self.dep_install, False, False, 0)
        row.pack_start(self.dep_copy, False, False, 0)
        row.pack_end(refresh, False, False, 0)
        outer.pack_start(row, False, False, 0)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("<small>Commands run in a terminal window so you can see them and enter your sudo password; "
                        "EPEL is enabled automatically when needed. Restart Kestrel afterwards.</small>")
        outer.pack_start(note, False, False, 0)
        exp = Gtk.Expander(label="Diagnostics (renderer, importers, converters, interpreters)")
        self.diag_view = Gtk.TextView(editable=False, monospace=True)
        dsw = Gtk.ScrolledWindow(min_content_height=200)
        dsw.add(self.diag_view)
        exp.add(dsw)
        exp.connect("notify::expanded", lambda e, p: e.get_expanded() and self._diag_load())
        outer.pack_start(exp, False, False, 0)
        self.dep_checks: List[Tuple[Gtk.CheckButton, dict]] = []
        self._dep_refresh()
        return outer

    def _dep_refresh(self):
        for c in self.dep_grid.get_children():
            self.dep_grid.remove(c)
        self.dep_checks = []
        self.dep_status.set_text("Checking what's installed…")
        self.dep_install.set_sensitive(False)
        self.dep_copy.set_sensitive(False)

        def fill(deps):
            self.dep_status.set_text(f"{sum(1 for x in deps if x['found'])} of {len(deps)} optional tools found.")
            for i, dep in enumerate(deps):
                cb = Gtk.CheckButton(active=dep["found"] is None, sensitive=dep["found"] is None)
                self.dep_grid.attach(cb, 0, i, 1, 1)
                name = Gtk.Label(xalign=0)
                name.set_markup(f"<b>{GLib.markup_escape_text(dep['name'])}</b>")
                name.set_tooltip_text(dep["cmd"])
                self.dep_grid.attach(name, 1, i, 1, 1)
                st = Gtk.Label(xalign=0)
                st.set_markup("<span foreground='#2e8b57'>installed</span>" if dep["found"] else "<span foreground='#c0392b'>missing</span>")
                self.dep_grid.attach(st, 2, i, 1, 1)
                g = Gtk.Label(label=dep["gives"], xalign=0, wrap=True, hexpand=True)
                g.get_style_context().add_class("dim-label")
                self.dep_grid.attach(g, 3, i, 1, 1)
                self.dep_checks.append((cb, dep))
            self.dep_grid.show_all()
            self.dep_install.set_sensitive(True)
            self.dep_copy.set_sensitive(True)
            return False
        threading.Thread(target=lambda: GLib.idle_add(fill, optional_deps()), daemon=True).start()

    def _dep_chosen(self):
        return [dep for cb, dep in self.dep_checks if cb.get_active() and dep["found"] is None]

    def _dep_copy(self):
        set_clipboard_text("\n".join(dep["cmd"] for dep in self._dep_chosen()) or "# nothing selected")
        self.dep_status.set_text("Commands copied")

    def _dep_install(self):
        chosen = self._dep_chosen()
        if not chosen:
            self.dep_status.set_text("Nothing selected")
            return
        script = os.path.join(GLib.get_user_cache_dir(), "kestrel", "install-deps.sh")
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with open(script, "w") as f:
            f.write(install_script(chosen))
        os.chmod(script, 0o755)
        if spawn_terminal(HOME, ["bash", script]):
            self.dep_status.set_text("Installing in a terminal window")
        else:
            self.dep_status.set_text(f"No terminal found. Script: {script}")

    def _diag_load(self):
        self.diag_view.get_buffer().set_text("Checking…")
        threading.Thread(target=lambda: GLib.idle_add(lambda: self.diag_view.get_buffer().set_text(preview_tool_report()) or False), daemon=True).start()

    def _collect(self):
        cfg = self.cfg
        if hasattr(self, "gl_check"):
            cfg["gl_preview"] = self.gl_check.get_active()
        if hasattr(self, "rv_entry"):
            cfg["rv_path"] = self.rv_entry.get_text().strip()
        if hasattr(self, "cores_spin"):
            cfg["ui_cores"] = int(self.cores_spin.get_value())
        if hasattr(self, "roots_store"):
            cfg["index_roots"] = [r[0] for r in self.roots_store] or [HOME]
            cfg["index_interval_h"] = int(self.index_interval.get_value())
        for key, w in self.w.items():
            if key.startswith("radio:"):
                for rb in w:
                    if rb.get_active():
                        cfg[key[6:]] = rb.value
            elif isinstance(w, Gtk.CheckButton):
                cfg[key] = w.get_active()
            elif isinstance(w, Gtk.Entry):
                try:
                    _dt.datetime.now().strftime(w.get_text())
                    cfg[key] = w.get_text() or DEFAULT_CONFIG[key]
                except Exception:
                    pass

    def _response(self, d, r):
        if r == 10:
            for k, v in DEFAULT_CONFIG.items():
                if k not in ("pinned", "recent", "frequent", "window", "nav_width", "preview_width", "side_width", "side_split", "rv_path", "index_roots"):
                    self.cfg[k] = json.loads(json.dumps(v))
            self.on_apply()
            self.destroy()
            return
        if r == Gtk.ResponseType.OK:
            self._collect()
            self.on_apply()
        self.destroy()


SHORTCUTS = [
    ("Enter", "Open"), ("Backspace / Alt+Left / Mouse Back", "Back"), ("Alt+Right / Mouse Forward", "Forward"),
    ("Alt+Up", "Up one level"), ("F2", "Rename"), ("Delete", "Delete (Recycle Bin)"), ("Shift+Delete", "Delete permanently"),
    ("Ctrl+Shift+R", "Open in OpenRV"), ("Ctrl+C / X / V", "Copy / Cut / Paste"), ("Ctrl+Shift+C", "Copy as path"), ("Ctrl+A", "Select all"),
    ("Ctrl+click / Shift+click", "Multi-select"), ("Ctrl+Z", "Undo"), ("Ctrl+N", "New window"),
    ("Ctrl+F2 (or F2 with several selected)", "Batch rename"), ("Ctrl+Shift+N", "New folder"), ("Ctrl+T / Ctrl+W", "New tab / Close tab"), ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"),
    ("Ctrl+1..9", "Switch to tab"), ("Ctrl+L / F4 / Alt+D", "Address bar"), ("Ctrl+E / Ctrl+F / F3", "Search"),
    ("F5", "Refresh"), ("F10 / Alt+F", "Menu bar"), ("Shift+F10 / Menu / Right-click", "Context menu"),
    ("Alt+Enter", "Properties"), ("Ctrl+H", "Toggle hidden items"), ("Ctrl+1..", ""), ("Alt+P", "Preview pane"),
    ("Alt+Shift+P", "Details pane"), ("Ctrl+Shift+S", "Split view (two folders side by side)"), ("Drag a tab out / right-click tab", "Move tab to its own window"), ("F11", "Full screen"), ("Ctrl+Q", "Quit"), ("Middle-click folder", "Open in new tab"),
    ("Type letters", "Jump to item"),
]


def show_shortcuts(parent):
    d = Gtk.Dialog(title="Keyboard shortcuts", transient_for=parent, modal=True)
    d.add_button("_Close", Gtk.ResponseType.CLOSE)
    d.set_default_size(520, 520)
    store = Gtk.ListStore(str, str)
    for k, v in SHORTCUTS:
        if v:
            store.append([k, v])
    tv = Gtk.TreeView(model=store, headers_visible=True)
    for i, t in enumerate(("Key", "Action")):
        tv.append_column(Gtk.TreeViewColumn(t, Gtk.CellRendererText(), text=i))
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    sw.add(tv)
    d.get_content_area().pack_start(sw, True, True, 0)
    d.show_all()
    d.run()
    d.destroy()


def show_about(parent):
    d = Gtk.AboutDialog(transient_for=parent, modal=True, program_name=APP_FULL_NAME, version=VERSION,
                        comments="A fast, Windows-Explorer-style file manager for Linux.\nGTK 3 · PyGObject · GIO",
                        logo_icon_name="system-file-manager", website="https://github.com/KCArrow/kestrel",
                        copyright="© 2026 Kalib Arrowsmith", license_type=Gtk.License.CUSTOM,
                        license="PolyForm Noncommercial License 1.0.0\n\nFree for individuals and noncommercial organizations. "
                                "Use by companies and studios requires a commercial licence — see COMMERCIAL-LICENSE.md "
                                "at https://github.com/KCArrow/kestrel", wrap_license=True)
    d.run()
    d.destroy()
