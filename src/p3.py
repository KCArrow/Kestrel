

# --------------------------------------------------------------------------- icon / thumbnail cache
class IconCache:
    def __init__(self):
        self.cache: Dict[Tuple[str, int], GdkPixbuf.Pixbuf] = {}
        self.thumbs: Dict[Tuple[str, int], Optional[GdkPixbuf.Pixbuf]] = {}
        self.pending: set = set()
        self.theme = Gtk.IconTheme.get_default()
        self.lock = threading.Lock()
        self.queue: List[Tuple[str, int, Callable]] = []
        self.worker: Optional[threading.Thread] = None

    def pixbuf(self, icon: Optional[Gio.Icon], size: int) -> Optional[GdkPixbuf.Pixbuf]:
        key = (icon.to_string() if icon else "text-x-generic", size)
        pb = self.cache.get(key)
        if pb is not None:
            return pb
        try:
            info = self.theme.lookup_by_gicon(icon, size, Gtk.IconLookupFlags.FORCE_SIZE) if icon else None
            if info is None:
                info = self.theme.lookup_icon("text-x-generic", size, Gtk.IconLookupFlags.FORCE_SIZE)
            pb = info.load_icon() if info else None
        except GLib.Error:
            pb = None
        if pb is None:
            try:
                pb = self.theme.load_icon("text-x-generic", size, Gtk.IconLookupFlags.FORCE_SIZE)
            except GLib.Error:
                pb = None
        self.cache[key] = pb
        return pb

    def desaturated(self, pb: Optional[GdkPixbuf.Pixbuf]) -> Optional[GdkPixbuf.Pixbuf]:
        """Greyed copy of a pixbuf (cached) — used for the inactive split pane."""
        if pb is None:
            return None
        key = ("desat", id(pb))
        hit = self.cache.get(key)
        if hit is None:
            hit = pb.copy()
            pb.saturate_and_pixelate(hit, 0.12, False)
            self.cache[key] = hit
        return hit

    def thumbnail(self, e: Entry, size: int, on_ready: Callable) -> Optional[GdkPixbuf.Pixbuf]:
        """Return a cached thumbnail, or None and schedule generation."""
        if not e.path or not (e.content_type or "").startswith("image/") or (e.size is not None and e.size < 64):
            return None
        key = (e.path, size)
        if key in self.thumbs:
            return self.thumbs[key]
        if key in self.pending:
            return None
        self.pending.add(key)
        with self.lock:
            self.queue.append((e.path, size, on_ready))
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=lambda: (pin_worker(), self._work()), daemon=True)
                self.worker.start()
        return None

    def _work(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                path, size, cb = self.queue.pop()
            pb = None
            try:
                if os.path.getsize(path) < 40 << 20:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                    pb = pb.apply_embedded_orientation() or pb
            except Exception:
                pb = None
            GLib.idle_add(self._done, path, size, pb, cb)

    def _done(self, path, size, pb, cb):
        self.thumbs[(path, size)] = pb
        self.pending.discard((path, size))
        if pb is not None:
            cb(path)
        return False


ICONS = IconCache()


def cursor_path(view):
    """Cursor TreePath of a TreeView or IconView (IconView.get_cursor returns (ok, path, cell))."""
    r = view.get_cursor()
    if isinstance(view, Gtk.IconView):
        return r[1] if r and r[0] else None
    return r[0] if r else None


def touch_all(store):
    """Ask every row to redraw (cell data funcs re-run)."""
    store.foreach(lambda m, p, it, d: (m.row_changed(p, it), False)[1], None)

LAYOUT_ICON_SIZE = {"xlarge": 160, "large": 96, "medium": 48, "small": 24, "list": 16, "tiles": 48, "details": 16, "content": 32}

URI_TARGETS = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]


def decide_drop(paths: List[str], dest: str, widget: Gtk.Widget) -> Optional[str]:
    """Explorer rules: drop into the folder the items already live in = nothing;
    Ctrl = copy, Shift = move; otherwise move on the same disk, copy across disks."""
    paths = [p for p in paths if p]
    if not paths or not dest:
        return None
    if all(os.path.dirname(p.rstrip("/")) == dest for p in paths):
        return None
    mask = 0
    try:
        win = widget.get_window()
        dev = Gdk.Display.get_default().get_default_seat().get_pointer()
        if win and dev:
            mask = win.get_device_position(dev)[3]
    except Exception:
        mask = 0
    if mask & Gdk.ModifierType.CONTROL_MASK:
        return "copy"
    if mask & Gdk.ModifierType.SHIFT_MASK:
        return "move"
    try:
        same_disk = all(os.stat(p).st_dev == os.stat(dest).st_dev for p in paths)
    except OSError:
        same_disk = False
    return "move" if same_disk else "copy"


# --------------------------------------------------------------------------- navigation pane nodes
class NavNode:
    def __init__(self, label: str, path: Optional[str], icon: Gio.Icon, expandable: bool = True, kind: str = "folder"):
        self.label, self.path, self.icon, self.expandable, self.kind = label, path, icon, expandable, kind
        self.loaded = False


# --------------------------------------------------------------------------- a tab (one folder view)
class FolderView(Gtk.Box):
    def __init__(self, win: "MainWindow", path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.cfg = win.cfg
        self.path = path
        self.back: List[str] = []
        self.fwd: List[str] = []
        self.entries: List[Entry] = []
        self.search: Optional[SearchOp] = None
        self.search_text = ""
        self.error = ""
        self.monitor: Optional[Gio.FileMonitor] = None
        self.reload_pending = 0
        self.gen = 0
        self.remember: Dict[str, str] = {}
        self.loading = False          # chunked fill in progress
        self.indexed_paint = False    # the view currently shows the index's copy, not yet confirmed by the disk
        self.busy = False             # a listing is running in the worker thread (drives the status spinner)
        self.partial = False
        self.layout = win.cfg["layout"]      # per-pane; the config value is the default for new panes
        self.muted = False                    # inactive pane of a split: desaturated icons, dim text
        self.page: Optional["Page"] = None
        # slim header shown only in split view
        self.header = Gtk.Box(spacing=4)
        self.header.get_style_context().add_class("pane-header")
        self.header_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, margin_start=8)
        self.header.pack_start(self.header_label, True, True, 0)
        hclose = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        hclose.set_relief(Gtk.ReliefStyle.NONE)
        hclose.set_tooltip_text("Close this pane")
        hclose.connect("clicked", lambda b: self.page.unsplit(self) if self.page else None)
        self.header.pack_end(hclose, False, False, 0)
        hev = Gtk.EventBox()
        hev.add(self.header)
        hev.connect("button-press-event", lambda w, e: self.view.grab_focus() or False)
        self.header_box = hev
        self.pack_start(hev, False, False, 0)
        self.header_box.set_no_show_all(True)

        # Entry-or-label, is_group, icon-view pixbuf, icon-view markup.  Columns 2/3 are bound directly to the
        # icon view's renderers so GTK lays out 50k tiles without calling back into Python per tile.
        self.store = Gtk.ListStore(GObject.TYPE_PYOBJECT, bool, GdkPixbuf.Pixbuf, str)
        self._index: Optional[Dict[str, int]] = None
        self.stack = Gtk.Stack()
        self.pack_start(self.stack, True, True, 0)
        self.err_label = Gtk.Label(xalign=0.5)
        self.err_label.get_style_context().add_class("dim-label")
        self.stack.add_named(self.err_label, "error")
        self._build_tree()
        self._build_iconview()
        self.apply_layout()

    # ---- details view
    def _build_tree(self):
        tv = Gtk.TreeView(model=self.store, enable_search=True, rubber_banding=True, headers_clickable=True)
        tv.set_search_equal_func(self._search_equal, None)
        tv.set_fixed_height_mode(True)      # O(1) row layout: 100k-row folders scroll like 100
        enable_soft_grid(tv, columns=True)              # faint rules, crossings fade out
        enable_row_hover(tv)
        self.tree = tv
        sel = tv.get_selection()
        sel.set_mode(Gtk.SelectionMode.MULTIPLE)
        sel.set_select_function(lambda s, m, p, cur, d: not m[p][1], None)
        sel.connect("changed", lambda s: self.win.on_selection_changed(self))
        tv.connect("row-activated", lambda t, p, c: self.win.activate_entry(self.entry_at(p)))
        tv.connect("button-press-event", self._on_button, "tree")
        tv.connect("key-press-event", self.win.on_view_key, self)
        tv.connect("popup-menu", lambda w: self.win.context_menu(self, None) or True)
        tv.connect("focus-in-event", lambda w, e: self.win.set_active_pane(self) or False)
        tv.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.ASK)
        tv.connect("drag-data-get", self._drag_get)
        tv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tv.connect("drag-data-received", self._drag_received, "tree")
        self.name_renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        self.name_renderer.connect("edited", self._on_edited)
        self.name_renderer.connect("editing-canceled", lambda r: self._end_edit())
        self.name_renderer.connect("editing-started", self._on_editing_started)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        self.stack.add_named(sw, "details")
        self.rebuild_columns()

    def rebuild_columns(self):
        tv = self.tree
        for c in tv.get_columns():
            tv.remove_column(c)
        cols = list(self.cfg["columns"])
        if "name" not in cols:
            cols.insert(0, "name")
        # Size stays small and sits right after Name so it never gets pushed off-screen by wide columns
        if "size" in cols:
            cols.remove("size")
            cols.insert(1, "size")
        if self.search or self.path in (TRASH, QUICK, RECENT):
            if "location" not in cols:
                cols.append("location")
        if self.path == TRASH:
            cols.insert(2, "deleted")
        if self.path == THISPC:
            cols = ["name", "usage", "type"]
        if self.path == NETWORK:
            cols = ["name", "location", "usage", "type"]
        if self.cfg["checkboxes"]:
            tog = Gtk.CellRendererToggle()
            tog.connect("toggled", self._on_toggled)
            c = Gtk.TreeViewColumn("", tog)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_fixed_width(28)
            c.set_cell_data_func(tog, self._chk_data)
            tv.append_column(c)
        for key in cols:
            col = Gtk.TreeViewColumn(COLUMN_TITLES.get(key, "Space used" if key == "usage" else key))
            col.set_resizable(True)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width({"name": 260, "location": 260, "modified": 150, "type": 170, "size": 90, "usage": 170, "deleted": 150}.get(key, 120))
            col.key = key
            if key == "name":
                pix = Gtk.CellRendererPixbuf()
                col.pack_start(pix, False)
                col.set_cell_data_func(pix, self._icon_data)
                col.pack_start(self.name_renderer, True)
                col.set_cell_data_func(self.name_renderer, self._name_data)
                col.set_expand(True)
                col.set_min_width(180)
            elif key == "usage":
                prog = Gtk.CellRendererProgress()
                col.pack_start(prog, True)
                col.set_cell_data_func(prog, self._usage_data)
                col.set_min_width(160)
            else:
                r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
                if key == "size":
                    r.set_property("xalign", 1.0)
                col.pack_start(r, True)
                col.set_cell_data_func(r, self._text_data, key)
                col.set_min_width({"modified": 150, "type": 160, "size": 90}.get(key, 120))
                if key == "location":
                    col.set_fixed_width(260)
            if self.path != THISPC:
                col.set_clickable(True)
                col.connect("clicked", lambda c: self.win.sort_by(c.key))
                if key == self.cfg["sort_key"]:
                    col.set_sort_indicator(True)
                    col.set_sort_order(Gtk.SortType.DESCENDING if self.cfg["sort_desc"] else Gtk.SortType.ASCENDING)
            tv.append_column(col)

    def _search_equal(self, model, column, key, it, data):
        v = model[it][0]
        return not (isinstance(v, Entry) and v.name.lower().startswith(key.lower()))

    # cell data funcs
    def _icon_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("pixbuf", None)
            return
        size = 32 if self.layout == "content" else 16
        pb = ICONS.pixbuf(v.icon, size)
        cell.set_property("pixbuf", ICONS.desaturated(pb) if self.muted else pb)

    def _name_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("markup", f"<span foreground='#1a5fb4'><b>{GLib.markup_escape_text(str(v))}</b></span>")
            cell.set_property("foreground-set", False)
            return
        name = self.win.display_name(v)
        if self.layout == "content":
            sub = v.type + ("" if v.is_dir else f"  ·  {fmt_size(v.size, self.cfg['size_in_bytes'])}") + (f"  ·  {v.location}" if v.location else "")
            cell.set_property("markup", f"{GLib.markup_escape_text(name)}\n<small><span foreground='#777'>{GLib.markup_escape_text(sub)}</span></small>")
        else:
            cell.set_property("markup", GLib.markup_escape_text(name))
        cut = self.win.clipboard["mode"] == "cut" and v.path in self.win.clipboard["paths"]
        dim = cut or v.hidden or self.muted
        cell.set_property("foreground", "#999999" if dim else None)
        cell.set_property("foreground-set", bool(dim))

    def _text_data(self, col, cell, model, it, key):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if not isinstance(v, Entry):
            cell.set_property("text", "")
            return
        cell.set_property("text", self.win.cell_text(v, key))
        cell.set_property("foreground", "#999999" if self.muted else None)
        cell.set_property("foreground-set", self.muted)

    def _usage_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        if isinstance(v, Entry) and v.usage:
            used, total = v.usage
            pct = int(100 * used / total) if total else 0
            cell.set_property("value", pct)
            cell.set_property("text", f"{pct}% used")
            cell.set_property("visible", True)
        else:
            cell.set_property("visible", False)

    def _chk_data(self, col, cell, model, it, data=None):
        hover_bg(self.tree, cell, model, it)
        v = model[it][0]
        cell.set_property("visible", isinstance(v, Entry))
        cell.set_property("active", isinstance(v, Entry) and self.tree.get_selection().iter_is_selected(it))

    def _on_toggled(self, cell, path):
        sel = self.tree.get_selection()
        p = Gtk.TreePath.new_from_string(path)
        if sel.path_is_selected(p):
            sel.unselect_path(p)
        else:
            sel.select_path(p)

    # ---- icon view
    def _build_iconview(self):
        iv = IconGrid(self.store)
        self.icons = iv
        iv.provider = self._iv_tile
        iv.connect("item-activated", lambda v, p: self.win.activate_entry(self.entry_at(p)))
        iv.connect("selection-changed", lambda v: self.win.on_selection_changed(self))
        iv.connect("button-press-event", self._on_button, "icons")
        iv.connect("key-press-event", self.win.on_view_key, self)
        iv.connect("popup-menu", lambda w: self.win.context_menu(self, None) or True)
        iv.connect("focus-in-event", lambda w, e: self.win.set_active_pane(self) or False)
        iv.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.ASK)
        iv.connect("drag-data-get", self._drag_get)
        iv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        iv.connect("drag-data-received", self._drag_received, "icons")
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(iv)
        self.stack.add_named(sw, "icons")
        # This PC tile view (Windows-style sections); used for every layout except Details
        self.pcview = PCView()
        self.pcview.connect("selection-changed", lambda v: self.win.on_selection_changed(self))
        self.pcview.connect("item-activated", lambda v, e: self.win.activate_entry(e))
        self.pcview.connect("context-menu", lambda v, e, ev: self.win.context_menu(self, ev))
        self.stack.add_named(self.pcview, "pc")

    # ---- per-row icon-view data (computed once at fill time, see _rows_for)
    def _iv_tile(self, e: Entry):
        """Called by the grid the first time a tile scrolls into view (and cached there)."""
        size = LAYOUT_ICON_SIZE.get(self.layout, 48)
        pb = None
        if size >= 48 and len(self.entries) <= 2500:
            pb = ICONS.thumbnail(e, size, self._thumb_ready)
        return (pb or ICONS.pixbuf(e.icon, size), self._iv_markup(e))

    def _iv_markup(self, e: Entry) -> str:
        name = GLib.markup_escape_text(self.win.display_name(e))
        cut = self.win.clipboard["mode"] == "cut" and e.path in self.win.clipboard["paths"]
        if cut or e.hidden:
            name = f"<span foreground='#999999'>{name}</span>"
        if self.layout == "tiles":
            sub = e.type + ("" if e.is_dir else f"\n{fmt_size(e.size, self.cfg['size_in_bytes'])}")
            return f"{name}\n<small><span foreground='#777'>{GLib.markup_escape_text(sub)}</span></small>"
        return name

    def _row_index(self) -> Dict[str, int]:
        if self._index is None:
            idx = {}
            for i, row in enumerate(self.store):
                e = row[0]
                if isinstance(e, Entry) and e.path:
                    idx[e.path] = i
            self._index = idx
        return self._index

    def _thumb_ready(self, path):
        i = self._row_index().get(path)
        if i is None:
            return
        try:
            it = self.store.get_iter(Gtk.TreePath.new_from_indices([i]))
            e = self.store[it][0]
            if isinstance(e, Entry) and e.path == path:
                self.store.row_changed(self.store.get_path(it), it)
        except (ValueError, TypeError):
            pass

    def refresh_rows(self, paths):
        """Recompute the icon-view label for specific rows (cut-state changed)."""
        for p in paths:
            i = self._row_index().get(p)
            if i is None:
                continue
            try:
                it = self.store.get_iter(Gtk.TreePath.new_from_indices([i]))
            except ValueError:
                continue
            e = self.store[it][0]
            if isinstance(e, Entry):
                self.store.row_changed(self.store.get_path(it), it)

    @property
    def pc_mode(self) -> bool:
        return self.path == THISPC and self.layout != "details"

    def apply_layout(self):
        lay = self.layout
        if self.pc_mode:
            self.stack.set_visible_child_name("pc")
            if self.entries:
                self.pcview.set_entries(self.entries, self.cfg["single_click"])
            return
        if self.path == THISPC or lay in ("details", "content"):
            self.stack.set_visible_child_name("details")
            self.tree.set_activate_on_single_click(self.cfg["single_click"])
            self.rebuild_columns()
        else:
            self.stack.set_visible_child_name("icons")
            size = LAYOUT_ICON_SIZE.get(lay, 48)
            if lay in ("list", "small"):
                self.icons.configure(size, True, 220, self.cfg["single_click"])
            elif lay == "tiles":
                self.icons.configure(size, True, 260, self.cfg["single_click"])
            else:
                self.icons.configure(size, False, size + 44, self.cfg["single_click"])
            self.icons.invalidate()
        self.tree.queue_draw()      # visible rows re-run their data funcs; no need to signal every row

    @property
    def view(self):
        return self.icons if self.stack.get_visible_child_name() == "icons" else self.tree

    # ---- selection helpers
    def entry_at(self, path) -> Optional[Entry]:
        try:
            v = self.store[path][0]
            return v if isinstance(v, Entry) else None
        except Exception:
            return None

    def selected_paths(self) -> List[Gtk.TreePath]:
        if self.pc_mode:
            return []
        if self.view is self.tree:
            return self.tree.get_selection().get_selected_rows()[1]
        return self.icons.get_selected_items()

    def selected_entries(self) -> List[Entry]:
        if self.pc_mode:
            return self.pcview.selected_entries()
        out = []
        for p in self.selected_paths():
            e = self.entry_at(p)
            if e:
                out.append(e)
        out.sort(key=lambda e: e.name.lower())
        return out

    def current_entry(self) -> Optional[Entry]:
        sel = self.selected_entries()
        if sel:
            return sel[0]
        p = cursor_path(self.view)
        return self.entry_at(p) if p is not None else None

    def select_all(self):
        if self.pc_mode:
            self.pcview.select_all()
            return
        if self.view is self.tree:
            self.tree.get_selection().select_all()
        else:
            self.icons.select_all()

    def select_none(self):
        if self.pc_mode:
            self.pcview.select_none()
            return
        if self.view is self.tree:
            self.tree.get_selection().unselect_all()
        else:
            self.icons.unselect_all()

    def invert_selection(self):
        if self.view is self.tree:
            sel = self.tree.get_selection()
            for row in self.store:
                if isinstance(row[0], Entry):
                    (sel.unselect_path if sel.path_is_selected(row.path) else sel.select_path)(row.path)
        else:
            cur = set(str(p) for p in self.icons.get_selected_items())
            for row in self.store:
                if isinstance(row[0], Entry):
                    (self.icons.unselect_path if str(row.path) in cur else self.icons.select_path)(row.path)

    def select_names(self, names: List[str], scroll: bool = True):
        if self.pc_mode:
            self.pcview.select_names(names)
            return
        self.select_none()
        first = None
        for row in self.store:
            v = row[0]
            if isinstance(v, Entry) and v.name in names:
                if self.view is self.tree:
                    self.tree.get_selection().select_path(row.path)
                else:
                    self.icons.select_path(row.path)
                first = first or row.path
        if first is not None and scroll:
            if self.view is self.tree:
                self.tree.set_cursor(first, None, False)
                self.tree.scroll_to_cell(first, None, False, 0, 0)
            else:
                self.icons.set_cursor(first, None, False)
                self.icons.scroll_to_path(first, False, 0, 0)

    # ---- mouse
    def _on_button(self, view, event, kind):
        if event.button == 3:
            if kind == "tree":
                hit = view.get_path_at_pos(int(event.x), int(event.y))
                path = hit[0] if hit else None
                sel = view.get_selection()
                if path is not None and not sel.path_is_selected(path):
                    sel.unselect_all()
                    sel.select_path(path)
                    view.set_cursor(path, None, False)
                elif path is None:
                    sel.unselect_all()
            else:
                path = view.get_path_at_pos(int(event.x), int(event.y))
                if path is not None and not view.path_is_selected(path):
                    view.unselect_all()
                    view.select_path(path)
                elif path is None:
                    view.unselect_all()
            self.win.context_menu(self, event)
            return True
        if event.button == 2:
            path = (view.get_path_at_pos(int(event.x), int(event.y)) or [None])[0] if kind == "tree" else view.get_path_at_pos(int(event.x), int(event.y))
            e = self.entry_at(path) if path is not None else None
            if e and e.is_dir:
                self.win.new_tab(e.path)
                return True
        if event.button in (8, 9):
            (self.win.go_back if event.button == 8 else self.win.go_forward)()
            return True
        return False

    # ---- drag & drop
    def _drag_get(self, widget, ctx, sel, info, t):
        uris = [e.gfile.get_uri() for e in self.selected_entries()]
        sel.set_uris(uris)

    def _drag_received(self, widget, ctx, x, y, sel, info, t, kind):
        uris = sel.get_uris()
        if not uris:
            Gtk.drag_finish(ctx, False, False, t)
            return
        dest = self.path if self.path not in VIRTUAL else None
        if kind == "tree":
            hit = widget.get_dest_row_at_pos(x, y)
            if hit:
                e = self.entry_at(hit[0])
                if e and e.is_dir:
                    dest = e.path
        else:
            hit = widget.get_dest_item_at_pos(x, y)
            if hit:
                e = self.entry_at(hit[0])
                if e and e.is_dir:
                    dest = e.path
        paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        kind = decide_drop(paths, dest, widget)
        ok = bool(kind) and self.win.transfer(paths, dest, kind)
        Gtk.drag_finish(ctx, ok, False, t)

    # ---- rename
    def start_rename(self):
        if len(self.selected_entries()) > 1:
            self.win.batch_rename()
            return
        if self.view is not self.tree:
            e = self.current_entry()
            if e:
                new = ask_text(self.win, "Rename", "New name:", e.name, select_stem=not e.is_dir)
                if new and new != e.name:
                    self.win.do_rename(e.path, os.path.join(os.path.dirname(e.path), new))
            return
        p = cursor_path(self.tree)
        if p is None:
            return
        col = next((c for c in self.tree.get_columns() if getattr(c, "key", "") == "name"), None)
        if col is None:
            return
        self.name_renderer.set_property("editable", True)
        self.tree.set_cursor(p, col, True)

    def _on_editing_started(self, renderer, editable, path):
        self.win.editing = True
        e = self.entry_at(Gtk.TreePath.new_from_string(path))
        if isinstance(editable, Gtk.Entry) and e and not e.is_dir:
            stem = os.path.splitext(e.name)[0]
            GLib.idle_add(lambda: editable.select_region(0, len(stem)) or False)

    def _end_edit(self):
        self.name_renderer.set_property("editable", False)
        self.win.editing = False

    def _on_edited(self, renderer, path, new_text):
        self._end_edit()
        e = self.entry_at(Gtk.TreePath.new_from_string(path))
        if e and new_text and new_text != e.name:
            self.win.do_rename(e.path, os.path.join(os.path.dirname(e.path), new_text))

    # ---- loading
    def title(self) -> str:
        if self.search:
            return f"Search Results in {self.win.display_path(self.path)}"
        return self.win.display_path(self.path)

    def short_title(self) -> str:
        """Tab label: parent + own folder, e.g. ~/Documents/Autodesk/ or …/parent/name/"""
        p = self.path
        if p in VIRTUAL:
            return self.win.display_path(p)
        if self.search:
            return "Search: " + self.search_text
        if p == HOME:
            return "~/"
        if p.startswith(HOME + "/"):
            rel = "~/" + p[len(HOME) + 1:]
        else:
            rel = p
        parts = [x for x in rel.split("/") if x]
        if rel.startswith("/"):
            parts.insert(0, "")
        if len(parts) > 3:
            return "…/" + "/".join(parts[-2:]) + "/"
        return "/".join(parts) + "/" if parts != [""] else "/"

    def load(self, keep_selection: bool = True):
        self.gen += 1
        gen = self.gen
        old = [e.name for e in self.selected_entries()] if keep_selection else []
        cfg = self.cfg
        path = self.path
        if self.search:
            return
        self.err_label.set_text("")
        self.busy = True
        self.win.on_tab_busy(self)
        if path in (THISPC, NETWORK):
            try:
                ents = {THISPC: self.win.pc_entries, NETWORK: self.win.net_entries}[path]()
                self._fill(gen, ents, "", old)
            except Exception as ex:
                self._fill(gen, [], str(ex), old)
            self._watch()
            return
        if path in (QUICK, RECENT):
            fn = self.win.quick_entries if path == QUICK else self.win.recent_entries

            def vwork():
                try:
                    ents, err = fn(), ""
                except Exception as ex:
                    ents, err = [], str(ex)
                GLib.idle_add(self._fill, gen, ents, err, old)
            threading.Thread(target=vwork, daemon=True).start()
            self._watch()
            return

        if cfg.get("index_enabled") and INDEX.loaded:
            rows = INDEX.lookup(path)
            if rows:
                # instant first paint from the index (sorted like the real listing); the real one replaces it
                ents0 = sort_entries(index_entries_for(path, rows), cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
                if not cfg["show_hidden"]:
                    ents0 = [e for e in ents0 if not e.hidden]
                self._fill_partial(gen, ents0[:3000], len(ents0))
                self.indexed_paint = True

        def partial(snapshot):
            """Early/intermediate view of a big folder while the rest is still being read."""
            if gen != self.gen:
                return
            part = sort_entries(snapshot, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])[:3000]
            GLib.idle_add(self._fill_partial, gen, part, len(snapshot))

        def work():
            try:
                ents = list_folder(path, cfg["show_hidden"], partial)
                err = ""
            except GLib.Error as ex:
                ents, err = [], ("You don't have permission to access this folder." if ex.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED) else ex.message)
            except Exception as ex:
                ents, err = [], str(ex)
            if path not in (THISPC, QUICK):
                ents = sort_entries(ents, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
            if cfg.get("index_enabled") and INDEX.loaded and not err and (cfg["show_hidden"] or cfg.get("index_hidden")) is not None:
                try:
                    if any(path == r or path.startswith(r.rstrip("/") + "/") for r in cfg.get("index_roots", [])) and (cfg.get("index_network") or not is_network_path(path)):
                        INDEX.update_dir(path, [e for e in ents if cfg.get("index_hidden") or not e.hidden])
                except Exception:
                    pass
            GLib.idle_add(self._fill, gen, ents, err, old)
        threading.Thread(target=work, daemon=True).start()
        self._watch()

    CHUNK = 4000          # rows appended per main-loop iteration (~15 ms)

    def _fill_partial(self, gen, ents, read_so_far=0):
        """Show the first few thousand entries read so far (cheap); the final _fill replaces it."""
        if gen != self.gen or self.search:
            return False
        self.partial = True
        self.partial_count = read_so_far
        self.entries = ents
        rows = self._rows_for(ents)
        view = self.view
        view.set_model(None)
        self.store.clear()
        app = self.store.append
        for r in rows:
            app(r)
        view.set_model(self.store)
        self.win.on_tab_loaded(self)
        return False

    def _rows_for(self, ents: List[Entry]) -> List[list]:
        group = self.cfg["group_by"]
        details = self.stack.get_visible_child_name() == "details" or self.path == THISPC
        rows = []
        self._index = None
        if details:
            # details/content views use cell-data funcs (only visible rows are rendered); columns 2/3 unused
            if self.path in (THISPC, QUICK, NETWORK, RECENT):
                last = None
                for e in ents:
                    if e.section != last:
                        rows.append([e.section, True, None, ""])
                        last = e.section
                    rows.append([e, False, None, ""])
            elif group != "none":
                last = None
                for e in ents:
                    g = group_label(e, group)
                    if g != last:
                        rows.append([g, True, None, ""])
                        last = g
                    rows.append([e, False, None, ""])
            else:
                rows = [[e, False, None, ""] for e in ents]
        else:
            # icon/tile/list: tiles are rendered lazily by IconGrid, so rows carry no pixbuf/markup up front
            rows = [[e, False, None, None] for e in ents]
        self._index = None      # built lazily by _row_index() the first time a thumbnail/refresh needs it
        return rows

    def _fill(self, gen, ents, err, old_names):
        """Runs on the main loop. Small folders are filled at once; big ones in chunks so the UI never stalls."""
        if gen != self.gen:
            return False
        self.busy = False
        self.indexed_paint = False
        for rec in reversed(NAV_LOG):
            if rec["path"] == self.path and rec["listed"] is None:
                rec["listed"] = time.perf_counter()
                rec["n"] = len(ents)
                rec["err"] = err
                break
        self.partial = False
        self.entries = ents
        self.error = err
        rows = self._rows_for(ents)
        self.store.clear()
        if self.view.get_model() is None:
            self.view.set_model(self.store)
        if err:
            self.err_label.set_text(err)
            self.stack.set_visible_child_name("error")
        elif self.stack.get_visible_child_name() == "error":
            self.apply_layout()
        view = self.view
        if len(rows) <= self.CHUNK:
            view.set_model(None)
            app = self.store.append
            for r in rows:
                app(r)
            view.set_model(self.store)
            self._fill_done(old_names)
        else:
            # TreeView (fixed-height) copes with live inserts; IconView re-lays-out every tile per insert,
            # so for it we fill detached and attach once at the end. Either way the main loop stays free.
            self.loading = True
            detached = view is self.icons
            if detached:
                view.set_model(None)
            self._fill_chunk(gen, rows, 0, old_names, detached)
        return False

    def _fill_chunk(self, gen, rows, i, old_names, detached):
        if gen != self.gen:
            if detached:
                self.view.set_model(self.store)
            return False
        app = self.store.append
        for r in rows[i:i + self.CHUNK]:
            app(r)
        i += self.CHUNK
        if i < len(rows):
            self.win.on_tab_loaded(self)     # keep the "N items" count moving
            GLib.idle_add(self._fill_chunk, gen, rows, i, old_names, detached, priority=GLib.PRIORITY_DEFAULT_IDLE)
        else:
            if detached:
                self.view.set_model(self.store)
            self.loading = False
            self._fill_done(old_names)
        return False

    def _fill_done(self, old_names):
        if self.pc_mode:
            self.pcview.set_entries(self.entries, self.cfg["single_click"])
            self.stack.set_visible_child_name("pc")
        for rec in reversed(NAV_LOG):
            if rec["path"] == self.path and rec["shown"] is None:
                rec["shown"] = time.perf_counter()
                rec["stalls_during"] = len(WATCHDOG.records) - rec["stalls_before"]
                break
        if old_names:
            self.select_names(old_names, scroll=False)
        self.win.on_tab_loaded(self)

    def resort(self):
        """Re-sort what is already loaded (no disk access); sorting happens off the main thread."""
        if self.search or self.path in (THISPC, QUICK) or not self.entries:
            self.load()
            return
        self.gen += 1
        gen = self.gen
        ents, cfg = list(self.entries), self.cfg
        old = [e.name for e in self.selected_entries()]

        def work():
            res = sort_entries(ents, cfg["sort_key"], cfg["sort_desc"], cfg["folders_first"])
            GLib.idle_add(self._fill, gen, res, "", old)
        threading.Thread(target=work, daemon=True).start()

    def _watch(self):
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
        if self.path in VIRTUAL and self.path != TRASH:
            return
        try:
            f = Gio.File.new_for_uri(self.path) if "://" in self.path else Gio.File.new_for_path(self.path)
            self.monitor = f.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect("changed", self._on_changed)
        except GLib.Error:
            self.monitor = None

    def _on_changed(self, mon, f, other, ev):
        if self.reload_pending:
            return
        self.reload_pending = GLib.timeout_add(400, self._reload_now)

    def _reload_now(self):
        self.reload_pending = 0
        if not self.search:
            self.load()
        return False

    # ---- search
    def start_search(self, text: str):
        self.stop_search()
        self.search_text = text
        root = self.path if self.path not in VIRTUAL else HOME
        self.entries = []
        self.store.clear()
        self._index = None
        self.search = SearchOp(root, text, self.cfg["search_subfolders"], self.cfg["search_contents"], self.cfg["show_hidden"], self._search_batch)
        self.rebuild_columns()
        self._search_seen = set()
        if self.cfg.get("index_enabled") and INDEX.loaded and self.cfg["search_subfolders"]:
            hits = INDEX.search(text, root)
            if hits:
                batch = []
                for p, is_dir, size, mtime in hits:
                    if not self.cfg["show_hidden"] and os.path.basename(p).startswith("."):
                        continue
                    e = Entry()
                    e.name, e.path, e.is_dir = os.path.basename(p), p, bool(is_dir)
                    e.size = None if is_dir else int(size)
                    e.mtime = float(mtime)
                    e.location = os.path.dirname(p)
                    if is_dir:
                        e.content_type, e.type, e.icon = "inode/directory", "File folder", _FOLDER_ICON
                    else:
                        e.content_type, e.type, e.icon = _ct_info(e.name)
                    batch.append(e)
                self._search_batch(batch, False)             # instant results from the index
        self.search.start()
        self.win.on_tab_loaded(self)

    def _search_batch(self, batch: List[Entry], done: bool):
        if not self.search:
            return False
        seen = getattr(self, "_search_seen", set())
        for e in batch:
            if e.path in seen:
                continue                      # already shown from the index
            seen.add(e.path)
            self.entries.append(e)
            self.store.append([e, False, None, None])
        self._index = None
        self.win.on_tab_loaded(self)
        if done:
            GLib.timeout_add(250, lambda: (self.win._update_status(), False)[1])     # thread has exited by then → wheel stops
        return False

    def stop_search(self):
        if self.search:
            self.search.cancel.set()
            self.search = None
            self.search_text = ""
            self.rebuild_columns()


# --------------------------------------------------------------------------- a notebook page: one or two panes
class Page(Gtk.Box):
    def __init__(self, win: "MainWindow", path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(False)
        self.pack_start(self.paned, True, True, 0)
        self.panes: List[FolderView] = []
        self.active: Optional[FolderView] = None
        self.label = Gtk.Label(label="", width_chars=14, max_width_chars=30, ellipsize=Pango.EllipsizeMode.MIDDLE, xalign=0)
        self.add_pane(path)
        self.active = self.panes[0]

    def add_pane(self, path: str) -> FolderView:
        fv = FolderView(self.win, path)
        fv.page = self
        self.panes.append(fv)
        if len(self.panes) == 1:
            self.paned.pack1(fv, True, False)
        else:
            self.paned.pack2(fv, True, False)
        fv.show_all()
        self._update_headers()
        if path not in VIRTUAL:
            self.win._bump_frequent(path)
        fv.load()
        return fv

    @property
    def split(self) -> bool:
        return len(self.panes) > 1

    def do_split(self, path: Optional[str] = None):
        if self.split:
            return self.panes[1]
        base = self.active or self.panes[0]
        p = path or (base.path if base.path not in VIRTUAL else HOME)
        fv = self.add_pane(p)
        GLib.idle_add(lambda: (self.paned.set_position(self.paned.get_allocated_width() // 2), fv.view.grab_focus()) and False)
        return fv

    def unsplit(self, pane: Optional[FolderView] = None):
        if not self.split:
            return
        pane = pane or self.panes[1]
        pane.stop_search()
        if pane.monitor:
            pane.monitor.cancel()
        self.panes.remove(pane)
        self.paned.remove(pane)
        keep = self.panes[0]
        self.paned.remove(keep)
        self.paned.pack1(keep, True, False)
        self.active = keep
        self._update_headers()
        keep.view.grab_focus()
        self.win.set_active_pane(keep)

    def set_active(self, fv: FolderView):
        if fv in self.panes and fv is not self.active:
            self.active = fv
            self._update_headers()

    def _update_headers(self):
        for fv in self.panes:
            fv.header_box.set_visible(self.split)
            fv.header.set_visible(self.split)
            fv.header_label.set_visible(self.split)
            ctx = fv.header.get_style_context()
            sctx = fv.stack.get_style_context()
            muted = self.split and fv is not self.active
            if not muted:
                ctx.add_class("active")
                sctx.remove_class("inactive-pane")
            else:
                ctx.remove_class("active")
                sctx.add_class("inactive-pane")
            if fv.muted != muted:
                fv.muted = muted
                fv.icons.muted = muted
                fv.icons.queue_draw()
                fv.tree.queue_draw()
            fv.header_label.set_text(fv.path if fv.path not in VIRTUAL else self.win.display_path(fv.path))
        self.update_label()

    def update_label(self):
        fv = self.active or self.panes[0]
        self.label.set_text(fv.short_title() if not self.split else " | ".join(p.short_title() for p in self.panes))
        self.label.set_tooltip_text(" | ".join(p.path for p in self.panes))
        for p in self.panes:
            p.header_label.set_text(p.path if p.path not in VIRTUAL else self.win.display_path(p.path))
