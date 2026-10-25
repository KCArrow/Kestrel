

# --------------------------------------------------------------------------- menu construction helpers
def _menu_item(label: str, cb: Optional[Callable], accel: str = "", check: Optional[bool] = None, radio_group=None,
               sensitive: bool = True, submenu: Optional[Gtk.Menu] = None):
    if label == "-":
        return Gtk.SeparatorMenuItem()
    if radio_group is not None:
        item = Gtk.RadioMenuItem.new_with_mnemonic_from_widget(radio_group[0] if radio_group else None, label)
        radio_group.append(item)
        item.set_active(bool(check))
    elif check is not None:
        item = Gtk.CheckMenuItem.new_with_mnemonic(label)
        item.set_active(check)
    else:
        item = Gtk.MenuItem.new_with_mnemonic(label)
    if accel:
        child = item.get_child()
        if isinstance(child, Gtk.AccelLabel):
            key, mods = Gtk.accelerator_parse(accel)
            child.set_accel(key, mods)
    if submenu is not None:
        item.set_submenu(submenu)
    if cb is not None:
        item.connect("activate", lambda it: cb() if not isinstance(it, (Gtk.CheckMenuItem,)) or it.get_active() or not isinstance(it, Gtk.RadioMenuItem) else None)
    item.set_sensitive(sensitive)
    return item


def build_menu(spec) -> Gtk.Menu:
    """spec: list of dicts/tuples -> Gtk.Menu."""
    menu = Gtk.Menu()
    for s in spec:
        if s == "-":
            menu.append(Gtk.SeparatorMenuItem())
            continue
        if isinstance(s, tuple):
            s = {"label": s[0], "cb": s[1] if len(s) > 1 else None, "accel": s[2] if len(s) > 2 else ""}
        sub = build_menu(s["sub"]) if "sub" in s else None
        menu.append(_menu_item(s["label"], s.get("cb"), s.get("accel", ""), s.get("check"), s.get("radio"),
                               s.get("sensitive", True), sub))
    menu.show_all()
    return menu


def _short_drive_label(label: str) -> str:
    """'KIOXIA_2TB (/mnt/KIOXIA_2TB)' -> 'KIOXIA_2TB'; the full path is shown as a tooltip instead."""
    if label.endswith(")") and " (" in label:
        return label[:label.rindex(" (")]
    return label


# --------------------------------------------------------------------------- main window
class MainWindow(Gtk.ApplicationWindow):
    MENU_KEYS = {"file": "File", "home": "Home", "share": "Share", "view": "View", "tools": "Tools", "help": "Help"}

    def __init__(self, app: "ExplorerApp", path: Optional[str]):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.cfg = app.cfg
        self.clipboard: Dict[str, object] = {"mode": "", "paths": []}
        self.undo_stack: List[Tuple[str, object]] = []
        self.editing = False
        self.status_timer = 0
        self.set_default_size(*self.cfg["window"])
        self.set_icon_name("system-file-manager")
        self.accel = Gtk.AccelGroup()
        self.add_accel_group(self.accel)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)
        self.menubar = Gtk.MenuBar()
        vbox.pack_start(self.menubar, False, False, 0)
        self._build_menubar()
        vbox.pack_start(self._build_toolbar(), False, False, 0)
        vbox.pack_start(self._build_commandbar(), False, False, 0)
        self.notebook = Gtk.Notebook(scrollable=True)
        self.notebook.set_show_border(False)
        self.notebook.set_group_name("kestrel-tabs")
        self.notebook.connect("switch-page", self._on_switch_page)
        self.notebook.connect("page-removed", self._on_page_removed)
        self.notebook.connect("page-added", self._on_page_added)
        self.notebook.connect("create-window", self._on_create_window)
        newtab = Gtk.Button.new_from_icon_name("tab-new-symbolic", Gtk.IconSize.MENU)
        newtab.set_relief(Gtk.ReliefStyle.NONE)
        newtab.set_tooltip_text("New tab (Ctrl+T)")
        newtab.connect("clicked", lambda b: self.new_tab(self.tab.path if self.tab.path not in VIRTUAL else HOME))
        newtab.show()
        self.notebook.set_action_widget(newtab, Gtk.PackType.END)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(self.paned, True, True, 0)
        self.nav_box = self._build_nav()
        self.paned.pack1(self.nav_box, False, False)
        self.right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.right.pack1(self.notebook, True, False)
        self.side = self._build_side()
        self.right.pack2(self.side, False, False)
        self.paned.pack2(self.right, True, False)
        self.paned.set_position(self.cfg["nav_width"])
        self._watch_volumes()
        # The side pane (preview + quick view) is freely resizable, like the nav pane; both splitters are
        # remembered: side_width is the pane's width, side_split the preview/quick-view divider.
        self._side_restored = False
        self.right.connect("size-allocate", self._restore_side_width)
        self.right.connect("notify::position", lambda p, _: self._remember_side_width())
        self.side_paned.connect("notify::position", lambda p, _: self._remember_side_split())

        self.status = Gtk.Label(xalign=0, margin_start=8, margin_end=8, margin_top=3, margin_bottom=3, ellipsize=Pango.EllipsizeMode.END)
        sb = Gtk.Box()
        sb.get_style_context().add_class("statusbar")
        # bottom-left activity wheel: spins while a folder is being read (network shares, big folders) or
        # a search is running; hidden otherwise so the bar stays quiet
        self.spinner = Gtk.Spinner(margin_start=8, no_show_all=True)
        self.spinner.set_size_request(14, 14)
        sb.pack_start(self.spinner, False, False, 0)
        sb.pack_start(self.status, True, True, 0)
        for lay, icon, tip in (("details", "view-list-symbolic", "Details"), ("large", "view-grid-symbolic", "Large icons")):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda b, l=lay: self.set_layout(l))
            sb.pack_end(b, False, False, 0)
        vbox.pack_start(sb, False, False, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("button-press-event", self._on_button)
        self.connect("delete-event", self._on_close)

        self.show_all()
        bench("window")
        self._bench_listed = False
        if path != "":   # "" = empty window that will receive a dragged tab
            start = path or {"quick": QUICK, "pc": THISPC, "home": HOME}.get(self.cfg["open_to"], HOME)
            self.new_tab(start)
        self._apply_panes()

    # ------------------------------------------------------------------ toolbar
    def _build_toolbar(self):
        tb = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=4, margin_bottom=2)
        tb.get_style_context().add_class("toolbar-row")
        self.btn_back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.btn_fwd = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        self.btn_up = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        for b, cb, tip in ((self.btn_back, self.go_back, "Back (Alt+Left)"), (self.btn_fwd, self.go_forward, "Forward (Alt+Right)"),
                           (self.btn_up, self.go_up, "Up (Alt+Up)")):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda w, c=cb: c())
            tb.pack_start(b, False, False, 0)
        self.btn_back.connect("button-press-event", self._history_popup)
        # path bar: breadcrumbs or entry
        self.path_stack = Gtk.Stack()
        self.crumbs = Gtk.Box()
        self.crumbs.get_style_context().add_class("linked")
        crumb_frame = Gtk.EventBox()
        crumb_frame.add(self.crumbs)
        crumb_frame.connect("button-press-event", lambda w, e: self.edit_address() if e.button == 1 else None)
        self.path_stack.add_named(crumb_frame, "crumbs")
        self.addr = Gtk.Entry()
        self.addr.connect("activate", self._on_address)
        self.addr.connect("focus-out-event", lambda w, e: self.path_stack.set_visible_child_name("crumbs"))
        self.addr.connect("key-press-event", self._on_address_key)
        self.path_stack.add_named(self.addr, "entry")
        frame = Gtk.Frame()
        frame.get_style_context().add_class("pathbar")
        frame.add(self.path_stack)
        tb.pack_start(frame, True, True, 0)
        self.searchbox = Gtk.SearchEntry(width_chars=22)
        self.searchbox.set_placeholder_text("Search")
        self.searchbox.connect("activate", lambda e: self.start_search(e.get_text()))
        self.searchbox.connect("stop-search", lambda e: self.clear_search())
        tb.pack_start(self.searchbox, False, False, 0)
        return tb

    def _history_popup(self, btn, event):
        if event.button == 3:
            t = self.tab
            if not t.back:
                return True
            m = Gtk.Menu()
            for i, p in enumerate(reversed(t.back[-15:])):
                it = Gtk.MenuItem(label=self.display_path(p))
                it.connect("activate", lambda it, n=i + 1: self.go_back(n))
                m.append(it)
            m.attach_to_widget(btn, None)
            m.show_all()
            m.popup_at_pointer(event)
            return True
        return False

    def _refresh_crumbs(self):
        t = self.tab
        if t is None:
            return
        for c in self.crumbs.get_children():
            self.crumbs.remove(c)
        for label, p in self._crumb_list(t.path):
            b = Gtk.Button(label=label)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.connect("clicked", lambda w, pp=p: self.navigate(pp))
            b.connect("button-press-event", lambda w, e, pp=p: (self.new_tab(pp) or True) if e.button == 2 else False)
            self.crumbs.pack_start(b, False, False, 0)
            arrow = Gtk.Label(label="›")
            arrow.get_style_context().add_class("dim-label")
            self.crumbs.pack_start(arrow, False, False, 2)
        self.crumbs.show_all()
        self.searchbox.set_placeholder_text(f"Search {self.display_path(t.path)}")
        if not t.search:
            self.searchbox.set_text("")

    def _crumb_list(self, path: str) -> List[Tuple[str, str]]:
        if path in VIRTUAL:
            return [(self.display_path(path), path)]
        out = [("This PC", THISPC)]
        if path == HOME or path.startswith(HOME + "/"):
            out.append((os.path.basename(HOME), HOME))
            rest, acc = path[len(HOME):].strip("/"), HOME
        else:
            out.append(("/", "/"))
            rest, acc = path.strip("/"), "/"
        if rest:
            for part in rest.split("/"):
                acc = os.path.join(acc, part)
                out.append((part, acc))
        return out

    def edit_address(self):
        t = self.tab
        self.addr.set_text("" if t.path in VIRTUAL else t.path)
        self.path_stack.set_visible_child_name("entry")
        self.addr.grab_focus()
        self.addr.select_region(0, -1)

    def _on_address_key(self, entry, event):
        if event.keyval == Gdk.KEY_Escape:
            self.path_stack.set_visible_child_name("crumbs")
            self.tab.view.grab_focus()
            return True
        if event.keyval == Gdk.KEY_Tab:
            v = os.path.expanduser(entry.get_text())
            d, b = os.path.split(v)
            try:
                cands = sorted(n for n in os.listdir(d or "/") if n.startswith(b) and os.path.isdir(os.path.join(d or "/", n)))
            except OSError:
                cands = []
            if cands:
                pre = os.path.commonprefix(cands)
                entry.set_text(os.path.join(d, pre) + ("/" if len(cands) == 1 else ""))
                entry.set_position(-1)
            return True
        return False

    def _on_address(self, entry):
        v = entry.get_text().strip()
        self.path_stack.set_visible_child_name("crumbs")
        if not v:
            return
        low = v.lower()
        if low in ("this pc", "pc", "computer"):
            self.navigate(THISPC)
        elif low in ("recycle bin", "trash", "trash:///", "trash://"):
            self.navigate(TRASH)
        elif low in ("quick access", "quick"):
            self.navigate(QUICK)
        elif low in ("network", "net://", "network:///"):
            self.navigate(NETWORK)
        elif "://" in v and not v.startswith("file://"):
            self.flash(f"Connecting to {v}…")
            mount_uri(v, self, lambda p, err: message(self, "Connect", err, Gtk.MessageType.ERROR) if err else self.navigate(p if p and os.path.isdir(p) else NETWORK))
        else:
            p = os.path.abspath(os.path.expanduser(os.path.expandvars(v)))
            if os.path.isdir(p):
                self.navigate(p)
            elif os.path.isfile(p):
                self.navigate(os.path.dirname(p))
                GLib.timeout_add(300, lambda: self.tab.select_names([os.path.basename(p)]) or False)
            else:
                message(self, APP_NAME, f"Can't find '{v}'. Check the spelling and try again.", Gtk.MessageType.ERROR)
        self.tab.view.grab_focus()

    # ------------------------------------------------------------------ command bar (Windows 11 style)
    def _cmd_button(self, icon: str, label: str, cb: Callable, tip: str = "", menu_cb: Optional[Callable] = None):
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.get_style_context().add_class("cmd")
        box = Gtk.Box(spacing=6)
        box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        if label:
            box.pack_start(Gtk.Label(label=label), False, False, 0)
        if menu_cb:
            box.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.MENU), False, False, 0)
        b.add(box)
        b.set_tooltip_text(tip or label)
        b.set_focus_on_click(False)
        if menu_cb:
            b.connect("clicked", lambda w: self._popup_under(w, build_menu(menu_cb())))
        else:
            b.connect("clicked", lambda w: cb())
        return b

    def _popup_under(self, widget, menu: Gtk.Menu):
        menu.attach_to_widget(widget, None)
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _build_commandbar(self):
        bar = Gtk.Box(spacing=2)
        bar.get_style_context().add_class("commandbar")
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        bar.set_margin_bottom(4)
        sep = lambda: Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
        bar.pack_start(self._cmd_button("list-add-symbolic", "New", None, "New folder or item", lambda: self._new_item_spec_with_folder()), False, False, 0)
        bar.pack_start(sep(), False, False, 0)
        for icon, lab, cb, tip in (("edit-cut-symbolic", "", lambda: self.copy_sel("cut"), "Cut (Ctrl+X)"),
                                   ("edit-copy-symbolic", "", lambda: self.copy_sel("copy"), "Copy (Ctrl+C)"),
                                   ("edit-paste-symbolic", "", lambda: self.paste(), "Paste (Ctrl+V)"),
                                   ("document-edit-symbolic", "", lambda: self.tab.start_rename(), "Rename (F2)"),
                                   ("user-trash-symbolic", "", lambda: self.delete(), "Delete (Del)")):
            bar.pack_start(self._cmd_button(icon, lab, cb, tip), False, False, 0)
        bar.pack_start(self._cmd_button("emblem-shared-symbolic", "", None, "Share", lambda: self.share_spec()), False, False, 0)
        bar.pack_start(sep(), False, False, 0)
        bar.pack_start(self._cmd_button("view-sort-descending-symbolic", "Sort", None, "Sort by", lambda: self.menu_spec("view")[6]["sub"]), False, False, 0)
        bar.pack_start(self._cmd_button("view-grid-symbolic", "View", None, "Layout", lambda: self.menu_spec("view")[5]["sub"] + ["-"] + self.menu_spec("view")[11:14]), False, False, 0)
        bar.pack_start(self._cmd_button("view-dual-symbolic", "Split", self.toggle_split, "Show two folders side by side (Ctrl+Shift+S)"), False, False, 0)
        bar.pack_start(self._cmd_button("view-more-symbolic", "", None, "More", lambda: self.menu_spec("home")), False, False, 0)
        bar.pack_end(self._cmd_button("sidebar-show-right-symbolic", "", lambda: self.toggle_cfg("details_pane"), "Details pane (Alt+Shift+P)"), False, False, 0)
        bar.pack_end(self._cmd_button("image-x-generic-symbolic", "", lambda: self.toggle_cfg("preview_pane"), "Preview pane (Alt+P)"), False, False, 0)
        return bar

    def _new_item_spec_with_folder(self):
        return [("_Folder", self.new_folder, "<Control><Shift>n"), "-"] + self._new_item_spec()[1:]

    # ------------------------------------------------------------------ navigation pane
    def _build_nav(self):
        self.nav_store = Gtk.TreeStore(GObject.TYPE_PYOBJECT)
        tv = Gtk.TreeView(model=self.nav_store, headers_visible=False, activate_on_single_click=True)
        tv.set_enable_tree_lines(False)
        tv.set_level_indentation(6)
        self.nav_tree = tv
        col = Gtk.TreeViewColumn("")
        tv.set_show_expanders(False)          # we draw our own twirly so rows without one can sit flush left
        tv.set_level_indentation(16)
        arrow = Gtk.CellRendererPixbuf()
        arrow.set_fixed_size(16, -1)
        pix = Gtk.CellRendererPixbuf()
        txt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col.pack_start(arrow, False)
        col.pack_start(pix, False)
        col.pack_start(txt, True)
        col.set_cell_data_func(arrow, self._nav_arrow, None)
        col.set_cell_data_func(pix, self._nav_icon, None)
        col.set_cell_data_func(txt, self._nav_text, None)
        enable_row_hover(tv)
        enable_soft_grid(tv, columns=False)
        tv.append_column(col)
        tv.set_tooltip_column(-1)
        tv.set_has_tooltip(True)
        tv.connect("query-tooltip", self._nav_tooltip)
        tv.get_selection().set_select_function(lambda s, m, p, cur, d: bool(m[p][0]) and m[p][0].kind not in ("spacer", "info"), None)
        tv.connect("test-expand-row", self._nav_expand)
        tv.connect("row-expanded", lambda t, it, p: t.queue_draw())
        tv.connect("row-collapsed", lambda t, it, p: t.queue_draw())
        tv.connect("row-activated", self._nav_activate)
        tv.connect("button-press-event", self._nav_button)
        tv.connect("key-press-event", self._nav_key)
        tv.drag_dest_set(Gtk.DestDefaults.ALL, URI_TARGETS, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tv.connect("drag-data-received", self._nav_drop)
        self.rebuild_nav()
        sw = Gtk.ScrolledWindow(hexpand=False, vexpand=True)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        sw.get_style_context().add_class("navpane")
        return sw

    def rebuild_nav(self):
        self.nav_store.clear()
        PATHS.on_change = lambda: (self._fill_recent(self.nav_recent), self._fill_quick(self.nav_quick), self.tab is not None and self.tab.path in (QUICK, RECENT) and self.tab.load())
        self.nav_recent = self.nav_store.append(None, [NavNode("Recent", RECENT, Gio.ThemedIcon.new("document-open-recent"), True, "root")])
        self._fill_recent(self.nav_recent)
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        self.nav_quick = self.nav_store.append(None, [NavNode("Quick access", QUICK, Gio.ThemedIcon.new("starred"), True, "root")])
        self._fill_quick(self.nav_quick)
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        pc = self.nav_store.append(None, [NavNode("This PC", THISPC, Gio.ThemedIcon.new("computer"), True, "root")])
        for label, p, icon in xdg_user_dirs():
            it = self.nav_store.append(pc, [NavNode(label, p, Gio.ThemedIcon.new(icon))])
            self.nav_store.append(it, [None])
        for label, p, icon, k in drives():
            if k == "network":
                continue
            it = self.nav_store.append(pc, [NavNode(_short_drive_label(label), p, icon, True, "removable" if k == "removable" else "drive")])
            self.nav_store.append(it, [None])
        for label, vol, vicon in volumes_unmounted():
            n = NavNode(f"{label}  (not mounted)", None, vicon, False, "volume")
            n.volume = vol
            self.nav_store.append(pc, [n])
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        net = self.nav_store.append(None, [NavNode("Network", NETWORK, Gio.ThemedIcon.new("network-workgroup"), True, "root")])
        nets = drives("network")
        for label, p, icon, k in nets:
            it = self.nav_store.append(net, [NavNode(_short_drive_label(label), p, icon, True, "network")])
            self.nav_store.append(it, [None])
        if not nets:
            self.nav_store.append(net, [NavNode("No network drives", None, Gio.ThemedIcon.new("network-offline"), False, "info")])
        self.nav_store.append(None, [NavNode("", None, None, False, "spacer")])
        self.nav_store.append(None, [NavNode("Recycle Bin", TRASH, Gio.ThemedIcon.new("user-trash"), False, "root")])
        self.nav_tree.expand_row(self.nav_store.get_path(net), False)
        self.nav_tree.expand_row(self.nav_store.get_path(pc), False)
        self.nav_tree.expand_row(self.nav_store.get_path(self.nav_quick), False)

    def _fill_recent(self, parent_iter):
        child = self.nav_store.iter_children(parent_iter)
        while child:
            nxt = self.nav_store.iter_next(child)
            self.nav_store.remove(child)
            child = nxt
        shown = 0
        for p in self.cfg["recent"]:
            if PATHS.exists(p):
                self.nav_store.append(parent_iter, [NavNode(os.path.basename(p), p, Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), False, "file")])
                shown += 1
                if shown >= 12:
                    break
        if not shown:
            self.nav_store.append(parent_iter, [NavNode("No recent files", None, Gio.ThemedIcon.new("document-open-recent-symbolic"), False, "info")])

    def _fill_quick(self, parent_iter):
        child = self.nav_store.iter_children(parent_iter)
        while child:
            nxt = self.nav_store.iter_next(child)
            self.nav_store.remove(child)
            child = nxt
        for p in self.cfg["pinned"]:
            if PATHS.isdir(p):
                it = self.nav_store.append(parent_iter, [NavNode(self.display_path(p), p, Gio.ThemedIcon.new("folder"), True, "pinned")])
                self.nav_store.append(it, [None])
        if self.cfg["show_frequent"]:
            for p in self.frequent_folders()[:6]:
                if p not in self.cfg["pinned"]:
                    it = self.nav_store.append(parent_iter, [NavNode(self.display_path(p), p, Gio.ThemedIcon.new("folder"), True, "frequent")])
                    self.nav_store.append(it, [None])

    def _nav_tooltip(self, tv, x, y, keyboard, tip):
        ok, mx, my, model, path, it = tv.get_tooltip_context(x, y, keyboard)
        if not ok:
            return False
        n = model[it][0]
        if not n or not n.path or n.path in VIRTUAL:
            return False
        text = n.path
        if n.kind in ("drive", "removable"):
            hit = disk_for_mount(n.path)
            if hit:
                d, part = hit
                text += f"\n{part['name']}{(' · ' + part['fstype']) if part['fstype'] else ''} on {d['model']} ({d['tran'] or 'internal'}, {fmt_size(d['size'])})"
        tip.set_text(text)
        tv.set_tooltip_row(tip, path)
        return True

    def _nav_arrow(self, col, cell, model, it, data=None):
        n = model[it][0]
        hover_bg(self.nav_tree, cell, model, it)
        if n is None or n.kind == "spacer" or not n.expandable:
            cell.set_property("visible", False)
            return
        cell.set_property("visible", True)
        cell.set_property("icon-name", "pan-down-symbolic" if self.nav_tree.row_expanded(model.get_path(it)) else "pan-end-symbolic")

    def _nav_icon(self, col, cell, model, it, data=None):
        n = model[it][0]
        if n is not None and n.kind == "spacer":
            cell.set_property("pixbuf", None)
            cell.set_property("cell-background-set", False)
            cell.set_property("height", 8)
            return
        cell.set_property("height", -1)
        hover_bg(self.nav_tree, cell, model, it)
        cell.set_property("xpad", 4 if (n is not None and n.kind == "root" and not n.expandable) else 2)
        cell.set_property("pixbuf", ICONS.pixbuf(n.icon, 16) if n and n.icon else None)

    def _nav_text(self, col, cell, model, it, data=None):
        hover_bg(self.nav_tree, cell, model, it)
        n = model[it][0]
        if n is None or n.kind == "spacer":
            cell.set_property("markup", "")
            cell.set_property("height", 8 if n is not None else -1)
            cell.set_property("cell-background-set", False)
            return
        cell.set_property("height", -1)
        if n.kind == "root":
            cell.set_property("markup", f"<b>{GLib.markup_escape_text(n.label)}</b>")
        elif n.kind == "info":
            cell.set_property("markup", f"<span foreground='#888'>{GLib.markup_escape_text(n.label)}</span>")
        else:
            cell.set_property("markup", GLib.markup_escape_text(n.label))

    def _nav_expand(self, tv, it, path):
        node = self.nav_store[it][0]
        if node is None or node.loaded or not node.path or node.path in VIRTUAL:
            return False
        node.loaded = True
        # Add the "Loading…" row BEFORE removing the placeholder: if the node is ever childless while GTK is
        # deciding whether it can expand, the expansion is dropped and the twirly needs a second click.
        self.nav_store.append(it, [NavNode("Loading…", None, Gio.ThemedIcon.new("content-loading-symbolic"), False, "info")])
        child = self.nav_store.iter_children(it)
        while child:
            nxt = self.nav_store.iter_next(child)
            if self.nav_store[child][0] is None:
                self.nav_store.remove(child)
            child = nxt
        row = Gtk.TreeRowReference.new(self.nav_store, self.nav_store.get_path(it))
        show_hidden, p = self.cfg["show_hidden"], node.path

        def work():
            names = []
            try:
                with os.scandir(p) as sc:
                    names = [(d.name, d.path) for d in sc if d.is_dir() and (show_hidden or not d.name.startswith("."))]
                names.sort(key=lambda t: t[0].lower())
            except OSError:
                pass
            GLib.idle_add(done, names[:1000])

        def done(names):
            if not row.valid():
                return False
            parent = self.nav_store.get_iter(row.get_path())
            for name, path_ in names:
                c = self.nav_store.append(parent, [NavNode(name, path_, Gio.ThemedIcon.new("folder"))])
                self.nav_store.append(c, [None])
            # now drop the "Loading…" row (last, so the branch never becomes empty)
            child = self.nav_store.iter_children(parent)
            while child:
                nxt = self.nav_store.iter_next(child)
                n = self.nav_store[child][0]
                if n is not None and n.kind == "info":
                    if names:
                        self.nav_store.remove(child)
                    else:
                        n.label = "No subfolders"
                        self.nav_store.row_changed(self.nav_store.get_path(child), child)
                child = nxt
            return False
        threading.Thread(target=work, daemon=True).start()
        return False

    def _nav_node_at(self, event):
        hit = self.nav_tree.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return None, None
        return self.nav_store[hit[0]][0], hit[0]

    def _nav_activate(self, tv, path, col):
        node = self.nav_store[path][0]
        if node and node.kind == "volume":
            self.mount_volume(node.volume)
            return
        if node and node.path:
            if node.kind == "file":
                return                     # Recent files: single click previews, double click opens (see _nav_button)
            self.navigate(node.path)       # expanding is only done by the twirly, like Explorer

    def _nav_button(self, tv, event):
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            node, _ = self._nav_node_at(event)
            if node and node.kind == "file" and node.path:
                self.open_file(entry_for_path(node.path))
                return True
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            hit = tv.get_path_at_pos(int(event.x), int(event.y))
            if hit:
                path, col, cx, cy = hit
                node = self.nav_store[path][0]
                if node and node.kind == "file" and node.path:
                    # single click on a Recent file: select it in the nav pane and show it in Preview / Quick view
                    tv.get_selection().select_path(path)
                    if self.tab is not None:
                        self.tab.select_none()
                    self._nav_preview = entry_for_path(node.path)
                    self._update_side()
                    return True
                if node and node.expandable:
                    # click on our twirly (the first 16 px past this row's indentation) toggles expansion
                    indent = (path.get_depth() - 1) * 16
                    if cx < indent + 18:
                        if tv.row_expanded(path):
                            tv.collapse_row(path)
                        else:
                            tv.expand_row(path, False)
                        return True
        if event.button == 3:
            node, path = self._nav_node_at(event)
            if node and (node.path or node.kind == "volume"):
                tv.get_selection().select_path(path)
                self.nav_context_menu(node, path, event)
            return True
        if event.button == 2:
            node, _ = self._nav_node_at(event)
            if node and node.path:
                self.new_tab(node.path)
            return True
        if event.button in (8, 9):
            (self.go_back if event.button == 8 else self.go_forward)()
            return True
        return False

    def _nav_key(self, tv, event):
        if event.keyval == Gdk.KEY_Delete:
            model, it = tv.get_selection().get_selected()
            node = model[it][0] if it else None
            if node and node.kind == "pinned":
                self.unpin(node.path)
            return True
        return False

    def _nav_drop(self, tv, ctx, x, y, sel, info, t):
        uris = sel.get_uris()
        hit = tv.get_dest_row_at_pos(x, y)
        node = self.nav_store[hit[0]][0] if hit else None
        if not uris or not node or not node.path or node.path in VIRTUAL:
            if node and node.path == TRASH and uris:
                paths = [Gio.File.new_for_uri(u).get_path() for u in uris]
                self.run_op("trash", [p for p in paths if p], None)
                Gtk.drag_finish(ctx, True, False, t)
                return
            Gtk.drag_finish(ctx, False, False, t)
            return
        paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        kind = decide_drop(paths, node.path, tv)
        ok = bool(kind) and self.transfer(paths, node.path, kind)
        Gtk.drag_finish(ctx, ok, False, t)

    def nav_context_menu(self, node: NavNode, path, event):
        p = node.path
        if node.kind == "volume":                      # a removable volume that isn't mounted yet
            spec = [("_Mount", lambda: self.mount_volume(node.volume))]
            menu = build_menu(spec)
            menu.attach_to_widget(self.nav_tree, None)
            menu.popup_at_pointer(event)
            return
        pinned = p in self.cfg["pinned"]
        spec = [("_Open", lambda: self.navigate(p)), ("Open in new _tab", lambda: self.new_tab(p)),
                ("Open in new _window", lambda: self.app.new_window(p))]
        if node.kind == "removable":
            spec += ["-", ("_Eject", lambda: self.eject(p)), ("_Format…", lambda: self.format_drive(p, node.label))]
        if p not in VIRTUAL:
            spec += [("Open in _terminal", lambda: self.open_terminal(p)), "-",
                     ("_Unpin from Quick access" if pinned else "_Pin to Quick access", (lambda: self.unpin(p)) if pinned else (lambda: self.pin(p))),
                     "-", ("P_roperties", lambda: self.show_properties([entry_for_path(p)]))]
        elif p == TRASH:
            spec += ["-", ("_Empty Recycle Bin", self.empty_trash)]
        elif p == NETWORK:
            spec += ["-", ("_Connect to server…", self.connect_to_server), ("_Refresh", lambda: (self.rebuild_nav(), self.reload_all()))]
        if node.kind == "network":
            spec += [("_Disconnect (unmount)", lambda: self.unmount(p))]
        menu = build_menu(spec)
        menu.attach_to_widget(self.nav_tree, None)
        menu.popup_at_pointer(event)

    # ------------------------------------------------------------------ network
    def net_entries(self) -> List[Entry]:
        out = []
        drive_entries = []
        for label, p, icon, k in drives("network"):
            e = entry_for_path(p, label)
            e.icon = icon
            e.section = "Network drives"
            e.location = _mount_source(p) or p
            e.type = "…"
            out.append(e)
            drive_entries.append(e)
        t = self.tab
        # free space in a thread (a dead share would hang the window); browse the LAN in a thread too
        def work():
            for e in drive_entries:
                try:
                    u = shutil.disk_usage(e.path)
                    e.usage = (u.total - u.free, u.total)
                    e.type = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                except OSError:
                    e.type = "unavailable"
            found = network_browse()
            GLib.idle_add(lambda: self._net_browse_done(t, found) or False)
        threading.Thread(target=work, daemon=True).start()
        return out

    def _net_browse_done(self, t: FolderView, found: List[Entry]):
        if t.path != NETWORK or t.search:
            return
        existing = {e.uri for e in t.entries}
        new = [e for e in found if e.uri not in existing]
        if new:
            t._fill(t.gen, list(t.entries) + new, "", [e.name for e in t.selected_entries()])
        else:
            touch_all(t.store)
            self.on_tab_loaded(t)

    def connect_to_server(self):
        uri = ask_text(self, "Connect to Server", "Server address (e.g. smb://server/share, sftp://user@host/path, ftp://host, dav://host/dav, nfs://server/export):", "smb://")
        if not uri or uri.strip() in ("smb://", ""):
            return
        uri = uri.strip()
        self.flash(f"Connecting to {uri}…")

        def done(path, err):
            if err:
                message(self, "Connect to Server", err, Gtk.MessageType.ERROR)
                return
            self.rebuild_nav()
            if path and os.path.isdir(path):
                self.navigate(path)
            else:
                self.flash(f"Connected: {uri}")
                self.navigate(NETWORK)
        mount_uri(uri, self, done)

    # ------------------------------------------------------------------ removable drives
    def _watch_volumes(self):
        """Live drive detection: udisks/GVfs tells us when something is plugged in, mounted, ejected or pulled.
        Refresh the nav pane and any This PC view — no manual reload needed."""
        vm = Gio.VolumeMonitor.get()
        self._vm = vm                                   # keep a reference: signals die with the object

        def changed(*_):
            invalidate_mounts()
            if getattr(self, "_vm_timer", None):
                GLib.source_remove(self._vm_timer)
            self._vm_timer = GLib.timeout_add(400, self._drives_changed)    # coalesce the burst of signals
        for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added", "volume-removed", "volume-changed", "drive-connected", "drive-disconnected"):
            vm.connect(sig, changed)

    def _drives_changed(self):
        self._vm_timer = None
        self.rebuild_nav()
        for fv in self._tabs():
            if fv.path in (THISPC, NETWORK):
                fv.load()
            elif fv.path not in VIRTUAL and not os.path.isdir(fv.path):
                self.flash(f"{self.display_path(fv.path)} is no longer available")
                self.navigate(THISPC, tab=fv)
        return False

    def eject(self, path: str):
        label = self.display_path(path)
        self.flash(f"Ejecting {label}…")

        def done(err):
            if err:
                message(self, "Eject", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"{label} can now be removed safely")
                for fv in self._tabs():
                    if fv.path == path or fv.path.startswith(path.rstrip("/") + "/"):
                        self.navigate(THISPC, tab=fv)
        eject_or_unmount(path, self, done)

    def mount_volume(self, vol):
        self.flash(f"Mounting {vol.get_name()}…")

        def done(path, err):
            if err:
                message(self, "Mount", err, Gtk.MessageType.ERROR)
            elif path:
                self.navigate(path)
        mount_volume(vol, self, done)

    def format_drive(self, path: str, label: str):
        """Format a removable drive through UDisks2 (polkit prompt, no sudo). Confirms twice: the dialog itself and
        an explicit 'type the label' check for anything bigger than 64 GB, since a wrong target here is fatal."""
        dev = block_device_for(path)
        if not dev:
            message(self, "Format", "Couldn't find the block device for this drive.", Gtk.MessageType.ERROR)
            return
        try:
            total = shutil.disk_usage(path).total
        except OSError:
            total = 0
        d = Gtk.Dialog(title=f"Format {label}", transient_for=self, modal=True)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Format", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        box = d.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)
        warn = Gtk.Label(xalign=0, wrap=True)
        warn.set_markup(f"<b>Everything on {GLib.markup_escape_text(label)} ({dev}, {fmt_size(total)}) will be erased.</b>\nThis cannot be undone.")
        box.pack_start(warn, False, False, 0)
        box.pack_start(Gtk.Label(label="File system:", xalign=0), False, False, 0)
        combo = Gtk.ComboBoxText()
        avail = [(k, desc) for k, desc, _pkg in FORMAT_CHOICES if mkfs_available(k)]
        for k, desc in avail:
            combo.append(k, desc)
        if avail:
            combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        missing = [f"{desc.split(' — ')[0]} (dnf install {pkg})" for k, desc, pkg in FORMAT_CHOICES if not mkfs_available(k)]
        if missing:
            ml = Gtk.Label(xalign=0, wrap=True)
            ml.set_markup("<small>Not available until installed: " + GLib.markup_escape_text(", ".join(missing)) + "</small>")
            box.pack_start(ml, False, False, 0)
        box.pack_start(Gtk.Label(label="Volume label:", xalign=0), False, False, 0)
        name = Gtk.Entry(text=re.sub(r"[^A-Za-z0-9_ -]", "", label)[:32] or "USB", activates_default=True)
        box.pack_start(name, False, False, 0)
        quick = Gtk.CheckButton(label="Quick format (don't overwrite existing data)", active=True)
        box.pack_start(quick, False, False, 0)
        if total > 64 << 30:
            box.pack_start(Gtk.Label(label=f"This drive is large. Type its current name ({label}) to confirm:", xalign=0, wrap=True), False, False, 0)
            confirm = Gtk.Entry()
            box.pack_start(confirm, False, False, 0)
        else:
            confirm = None
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        while True:
            r = d.run()
            if r != Gtk.ResponseType.OK:
                d.destroy()
                return
            if not avail:
                message(self, "Format", "No mkfs tool is installed for any supported file system.", Gtk.MessageType.ERROR)
                continue
            if confirm is not None and confirm.get_text().strip() != label.strip():
                message(self, "Format", "The name doesn't match — nothing was done.", Gtk.MessageType.WARNING)
                continue
            break
        fstype, vol_label, erase = combo.get_active_id(), name.get_text().strip(), not quick.get_active()
        d.destroy()
        prog = Gtk.Dialog(title="Formatting", transient_for=self, modal=True)
        pb = Gtk.ProgressBar(text=f"Formatting {dev} as {fstype}…", show_text=True, margin=16)
        prog.get_content_area().pack_start(pb, False, False, 0)
        prog.set_default_size(380, -1)
        prog.show_all()
        pulse = GLib.timeout_add(100, lambda: (pb.pulse(), True)[1])

        def done(err):
            GLib.source_remove(pulse)
            prog.destroy()
            invalidate_mounts()
            if err:
                message(self, "Format failed", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"{dev} formatted as {fstype}" + (f" ({vol_label})" if vol_label else ""))
            self._drives_changed()
        # UDisks needs the filesystem unmounted first; it does that itself when asked via Format on a mounted
        # block, but doing it explicitly gives a clearer error when a file is still open on the stick
        def after_unmount(uerr):
            if uerr and "not mounted" not in uerr.lower():
                GLib.source_remove(pulse)
                prog.destroy()
                message(self, "Format", f"Couldn't unmount first: {uerr}", Gtk.MessageType.ERROR)
                return
            udisks_format(dev, fstype, vol_label, erase, done)
        unmount_path(path, self, after_unmount)

    def unmount(self, path: str):
        def done(err):
            if err:
                message(self, "Disconnect", err, Gtk.MessageType.ERROR)
            else:
                self.flash(f"Disconnected {self.display_path(path)}")
                for fv in self._tabs():
                    if fv.path == path or fv.path.startswith(path.rstrip("/") + "/"):
                        self.navigate(NETWORK, tab=fv)
                self.rebuild_nav()
                self.reload_all()
        unmount_path(path, self, done)

    def open_network_entry(self, e: Entry) -> bool:
        """Open a server/share seen through GVfs: mount it first if needed, then browse its FUSE path."""
        if not e.uri or e.uri.startswith("file://"):
            return False
        g = Gio.File.new_for_uri(e.uri)
        p = g.get_path()
        if p and os.path.isdir(p):
            self.navigate(p)
            return True
        self.flash(f"Connecting to {e.name}…")

        def done(path, err):
            if err:
                message(self, "Network", err, Gtk.MessageType.ERROR)
            elif path and os.path.isdir(path):
                self.navigate(path)
            else:
                # not browsable as a folder (e.g. a server list) — enumerate it in place
                self.flash(f"{e.name}: not browsable as a folder from here")
        mount_uri(e.uri, self, done)
        return True

    # ------------------------------------------------------------------ side pane (preview / details)
    def _build_side(self):
        """Right-hand side: Preview on top, a Quick-view properties card below, in a resizable vertical paned."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("sidepane")
        self.side_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.side_paned.set_wide_handle(False)
        outer.pack_start(self.side_paned, True, True, 0)

        # ---- preview
        pv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        self.preview_box = pv
        self.preview_caption = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.preview_caption.get_style_context().add_class("dim-label")
        pv.pack_start(self.preview_caption, False, False, 0)
        self.preview_stack = Gtk.Stack()
        self.preview_stack.set_homogeneous(False)           # minimum = the visible child's, not the biggest child's
        self.preview_stack.set_hhomogeneous(False)
        self.preview_stack.set_vhomogeneous(False)
        # Transitions are done by snapshot: when a new preview lands, a picture of what the pane showed a
        # moment ago is laid over it and faded out (see _show_preview). That gives every file type — images,
        # text, folders, archives, GL/software 3D — the same old→new crossfade with no grey in between,
        # which neither GtkStack's crossfade (child → child only) nor opacity fades (bleed the background
        # through) can do for a widget whose content is swapped in place.
        self.preview_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.preview_overlay = Gtk.Overlay()
        self._snap_surface = None
        self._snap_alpha = 0.0
        # always shown (and therefore realised and allocated from the start): a widget first shown at the
        # moment of a fade is not allocated until the next layout pass and misses the first frames
        self.snap_area = Gtk.DrawingArea()
        self.snap_area.set_has_window(False)        # no GdkWindow of its own → it can never intercept input
        self.snap_area.set_can_focus(False)
        self.snap_area.set_app_paintable(True)
        self.snap_area.connect("draw", self._draw_snapshot)
        self.preview_overlay.add_overlay(self.snap_area)
        self.preview_overlay.set_overlay_pass_through(self.snap_area, True)
        self.preview_image = FitImage()
        if GtkSource is not None:
            self.preview_text = GtkSource.View.new_with_buffer(GtkSource.Buffer())
            self.preview_text.set_show_line_numbers(True)
            self.preview_text.set_highlight_current_line(True)
            self.preview_text.set_tab_width(4)
            self.preview_text.set_insert_spaces_instead_of_tabs(False)
            self.preview_text.set_auto_indent(True)
            self.preview_text.set_monospace(True)
            self.preview_text.set_right_margin_position(100)
            self._lang_mgr = GtkSource.LanguageManager.get_default()
            self._style_mgr = GtkSource.StyleSchemeManager.get_default()
        else:
            self.preview_text = Gtk.TextView(monospace=True)
        self.preview_text.set_editable(False)
        self.preview_text.set_cursor_visible(False)
        self.preview_text.set_wrap_mode(Gtk.WrapMode.NONE)
        self.preview_text.set_left_margin(6)
        self.preview_text.connect("key-press-event", self._edit_key)
        tsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        tsw.add(self.preview_text)
        # quick-edit toolbar: Edit toggle · find · prev/next · Save · Revert
        tb = Gtk.Box(spacing=4)
        tb.get_style_context().add_class("edit-toolbar")
        self.edit_toggle = Gtk.ToggleButton(label="Edit")
        self.edit_toggle.set_tooltip_text("Edit this text file in place (Ctrl+S saves)")
        self.edit_toggle.connect("toggled", self._edit_toggled)
        tb.pack_start(self.edit_toggle, False, False, 0)
        self.edit_badge = Gtk.Label(label="READ-ONLY")
        self.edit_badge.get_style_context().add_class("edit-badge")
        tb.pack_start(self.edit_badge, False, False, 2)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Find in file", width_chars=14, hexpand=True)
        self.find_entry.connect("search-changed", lambda e: self._find(1, from_start=True))
        self.find_entry.connect("activate", lambda e: self._find(1))
        self.find_entry.connect("key-press-event", lambda w, ev: (self._find(-1) or True) if ev.keyval == Gdk.KEY_Return and ev.state & Gdk.ModifierType.SHIFT_MASK else False)
        tb.pack_start(self.find_entry, True, True, 0)
        for icon, d, tip in (("go-up-symbolic", -1, "Previous match (Shift+Enter)"), ("go-down-symbolic", 1, "Next match (Enter)")):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda w, dd=d: self._find(dd))
            tb.pack_start(b, False, False, 0)
        self.find_count = Gtk.Label()
        self.find_count.get_style_context().add_class("dim-label")
        tb.pack_start(self.find_count, False, False, 4)
        self.btn_save = Gtk.Button(label="Save", sensitive=False)
        self.btn_save.set_tooltip_text("Save (Ctrl+S)")
        self.btn_save.connect("clicked", lambda b: self.save_edit())
        self.btn_revert = Gtk.Button(label="Revert", sensitive=False)
        self.btn_revert.connect("clicked", lambda b: self.revert_edit())
        tb.pack_end(self.btn_save, False, False, 0)
        tb.pack_end(self.btn_revert, False, False, 0)
        psw = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        psw.pack_start(clip_width(tb), False, False, 0)
        psw.pack_start(tsw, True, True, 0)
        self.edit_path: Optional[str] = None
        self.edit_entry: Optional[Entry] = None
        self.edit_dirty = False
        self.edit_full = False
        buf = self.preview_text.get_buffer()
        self.find_tag = buf.create_tag("find", background="#f5c542", foreground="#000000")
        self.find_cur_tag = buf.create_tag("findcur", background="#ff8c00", foreground="#000000")
        buf.connect("changed", self._edit_changed)
        self.preview_info = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.preview_info.get_style_context().add_class("dim-label")
        self.preview_big_icon = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        self.preview_icon_img = FitImage(128)
        self.preview_icon_img.set_size_request(-1, 48)
        self.preview_icon_lab = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self.preview_icon_lab.get_style_context().add_class("dim-label")
        self.preview_big_icon.pack_start(self.preview_icon_img, True, True, 0)
        self.preview_big_icon.pack_start(self.preview_icon_lab, False, False, 0)
        self.preview_folder = Gtk.ListStore(GObject.TYPE_PYOBJECT, str)     # entry, size text
        ftv = Gtk.TreeView(model=self.preview_folder, headers_visible=False)
        ftv.set_activate_on_single_click(False)
        fcol = Gtk.TreeViewColumn("")
        fpix, ftxt = Gtk.CellRendererPixbuf(), Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        # Icon themes hand back pixbufs of slightly different heights for different MIME types (and some
        # 16 px requests come back 20–24 px), which made rows uneven. Fix the renderer's box so every row is
        # the same height regardless of the icon it holds.
        fpix.set_fixed_size(22, 22)
        ftxt.set_fixed_size(-1, 24)
        fsize_fixed = 24
        fcol.pack_start(fpix, False)
        fcol.pack_start(ftxt, True)
        fcol.set_expand(True)
        fcol.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        fcol.set_cell_data_func(fpix, lambda c, cell, m, it, d=None: cell.set_property("pixbuf", ICONS.pixbuf(m[it][0].icon, 16)), None)
        fcol.set_cell_data_func(ftxt, lambda c, cell, m, it, d=None: cell.set_property("text", m[it][0].name), None)
        ftv.append_column(fcol)
        fsize = Gtk.CellRendererText(xalign=1.0)
        fsize.set_property("foreground", "#888888")
        fsize.set_fixed_size(-1, fsize_fixed)
        scol = Gtk.TreeViewColumn("", fsize, text=1)
        scol.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        scol.set_fixed_width(90)
        ftv.append_column(scol)
        ftv.set_fixed_height_mode(True)
        ftv.connect("row-activated", lambda t, p, c: self.activate_entry(self.preview_folder[p][0]))
        fsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        fsw.add(ftv)
        self.model_view = None            # created on the first model preview (GL context setup is ~300 ms)
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.model_box = mbox
        mbar = Gtk.Box(spacing=6)
        self.model_count = Gtk.Label(xalign=0, hexpand=True)
        self.model_count.get_style_context().add_class("dim-label")
        mbar.pack_start(self.model_count, True, True, 4)
        self.model_full_toggle = Gtk.ToggleButton(label="All triangles")
        self.model_full_toggle.set_tooltip_text("Override the 500,000-face preview cap and load every triangle.\n"
                                                "Heavy meshes will orbit slowly — this is a software renderer.")
        self.model_full_toggle.get_style_context().add_class("flat")
        self.model_full_toggle.connect("toggled", self._toggle_model_full)
        mbar.pack_end(self.model_full_toggle, False, False, 0)
        mbox.pack_start(clip_width(mbar), False, False, 0)
        self.model_slot = Gtk.Box()
        mbox.pack_start(self.model_slot, True, True, 0)
        mhint = Gtk.Label(label="LMB orbit · MMB pan · Alt+RMB or wheel zoom · double-click reset · W wireframe · S flat/smooth", ellipsize=Pango.EllipsizeMode.END)
        mhint.get_style_context().add_class("dim-label")
        mbox.pack_start(mhint, False, False, 0)
        self.preview_stack.add_named(mbox, "model")
        self.preview_stack.add_named(self.preview_image, "image")
        self.video = VideoPreview() if gst_sink_available() else None
        if self.video is not None:
            self.video.sink_widget.set_size_request(-1, 0)      # no minimum: the pane decides
        if self.video is not None:
            self.preview_stack.add_named(self.video, "video")
        self.preview_stack.add_named(psw, "text")
        self.preview_stack.add_named(self.preview_info, "info")
        self.preview_stack.add_named(self.preview_big_icon, "icon")
        self.preview_stack.add_named(fsw, "folder")
        self.preview_overlay.add(self.preview_stack)
        pv.pack_start(self.preview_overlay, True, True, 0)
        # shrink=True on both halves: the divider can be dragged until either the preview or the quick view
        # is almost closed, regardless of what the current content would like as its minimum size
        self.side_paned.pack1(pv, True, False)

        # ---- quick view (properties card)
        qv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.details_box = qv
        head = Gtk.Box(spacing=8)
        self.qv_icon = Gtk.Image()
        head.pack_start(self.qv_icon, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.details_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, selectable=True)
        self.qv_subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.qv_subtitle.get_style_context().add_class("dim-label")
        tb.pack_start(self.details_title, False, False, 0)
        tb.pack_start(self.qv_subtitle, False, False, 0)
        head.pack_start(tb, True, True, 0)
        qv.pack_start(head, False, False, 0)
        self.details_grid = Gtk.Grid(column_spacing=10, row_spacing=3)
        qsw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        qsw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        qsw.add(self.details_grid)
        qv.pack_start(qsw, True, True, 0)
        act = Gtk.Box(spacing=4)
        act.get_style_context().add_class("linked")
        for lab, cb in (("Open", lambda: self.activate_entry(self.tab.current_entry() if self.tab else None)),
                        ("Open with…", self.open_with), ("Properties…", self.properties)):
            b = Gtk.Button(label=lab)
            b.connect("clicked", lambda w, c=cb: c())
            act.pack_start(b, True, True, 0)
        self.qv_actions = act
        qv.pack_end(act, False, False, 0)
        self.side_paned.pack2(qv, False, False)
        outer.set_size_request(180, -1)                       # floor only; the splitter sets the real width
        return outer

    def _restore_side_width(self, paned, alloc):
        if self._side_restored or alloc.width < 300 or not self.side.get_visible():
            return
        self._side_restored = True
        width = int(self.cfg.get("side_width") or self.cfg.get("preview_width", 300))
        width = max(180, min(width, alloc.width - 300))
        paned.set_position(alloc.width - width)

    def _remember_side_width(self):
        if not self._side_restored or not self.side.get_visible():
            return
        w = self.right.get_allocated_width() - self.right.get_position()
        if w >= 180:
            self.cfg["side_width"] = w

    def _remember_side_split(self):
        if getattr(self, "_split_restored", False) and self.cfg["preview_pane"] and self.cfg["details_pane"]:
            pos = self.side_paned.get_position()
            if pos > 40:
                self.cfg["side_split"] = pos

    def _apply_panes(self):
        self.nav_box.set_visible(self.cfg["nav_pane"])
        show_side = self.cfg["preview_pane"] or self.cfg["details_pane"]
        self.side.set_visible(show_side)
        self.preview_box.set_visible(self.cfg["preview_pane"])
        self.details_box.set_visible(self.cfg["details_pane"])
        if self.cfg["preview_pane"] and self.cfg["details_pane"]:
            def place():
                h = self.side_paned.get_allocated_height()
                saved = int(self.cfg.get("side_split") or 0)
                self.side_paned.set_position(saved if 40 < saved < h - 40 else int(h * 0.55))
                self._split_restored = True
                return False
            GLib.idle_add(place)
        self._side_restored = False          # re-apply the remembered width when the pane (re)appears
        self._update_side()

    def _update_side(self):
        if not (self.cfg["preview_pane"] or self.cfg["details_pane"]):
            return
        t = self.tab
        if t is None:
            return
        sel = t.selected_entries()
        if not sel and getattr(self, "_nav_preview", None) is not None:
            sel = [self._nav_preview]          # a Recent file picked in the nav pane with a single click
        e = sel[0] if sel else None
        self._side_gen = getattr(self, "_side_gen", 0) + 1
        self._take_snapshot()
        # Whatever the pane was computing for the previous item is now moot: stop the folder-size walks at
        # once. Left running they compete with the next folder listing for the disk / network share, which is
        # what made navigating away from a selected folder feel sticky.
        for attr in ("_pv_cancel", "_qv_cancel"):
            ev = getattr(self, attr, None)
            if ev is not None:
                ev.set()
                setattr(self, attr, None)
        if self.cfg["preview_pane"]:
            self._update_preview(e)
        if self.cfg["details_pane"]:
            if self.editing_pinned and self.edit_entry is not None and (e is None or e.path != self.edit_path):
                sel = [self.edit_entry]        # keep the edited file's details on screen while editing
            self._update_details(sel)

    # ---- preview
    def _update_preview(self, e: Optional[Entry]):
        gen = self._side_gen
        # unsaved quick-edit: selecting something else (or nothing) asks first; "Keep editing" stays put
        if self.editing_pinned and (e is None or e.path != self.edit_path):
            if self.edit_dirty:
                # Never open a modal dialog from inside a selection/button handler: the view would miss the
                # button release and think a drag is in progress. Defer it to the next main-loop turn.
                if not getattr(self, "_prompt_pending", False):
                    self._prompt_pending = True
                    GLib.timeout_add(60, self._deferred_prompt)
            elif e is not None:
                self.edit_toggle.set_active(False)      # clean editor, new file chosen: just leave edit mode
                self._update_preview(e)
                return
            self._show_edit_caption()
            self._show_preview("text")
            return
        if e is not None and e.path == self.edit_path and self.editing_pinned:
            self._show_edit_caption()
            self._show_preview("text")      # same file re-selected: keep the edits
            return
        # Re-selecting the item already on screen (same path, size and mtime) keeps the finished preview:
        # re-decoding a model or a big image on every click made double-clicks feel laggy.
        key = (e.path, e.size, e.mtime) if e is not None else None
        shown = self.preview_stack.get_visible_child_name()
        if key is not None and key == getattr(self, "_preview_key", None) and shown not in ("info", None):
            self._drop_snapshot()               # nothing changes: let the held snapshot go at once
            return
        self._preview_key = key
        if self.video is not None and shown == "video":
            self.video.stop()                   # leaving the player: release the decoder, stop the sound
        self._set_caption("")
        if e is None:
            # The selection is empty for an instant between clicks and during navigation; showing the empty
            # state right away flashes grey between two previews. Only show it if nothing follows within 200 ms.
            self._placeholder("Select a file to preview", gen, 200)
            return
        if e.is_dir:
            if e.path:
                # listing happens in a thread: a slow network folder must not freeze the window; the pane keeps
                # the previous item until the listing lands (placeholder only if that takes > 250 ms)
                self._set_caption("Listing " + e.name + "…")
                # the listing itself only starts after the 250 ms double-click debounce below, so the
                # placeholder must come well after that — otherwise every folder shows grey for one frame and
                # the list pops in on top of it instead of crossfading from the previous item
                self._placeholder("Listing…", gen, 900)
                fpath, show_hidden = e.path, self.cfg["show_hidden"]
                # debounce: the first click of a double-click selects the folder — only start reading it if
                # the selection is still current 250 ms later, so navigating never has to compete with a
                # preview scan of the very folder it is opening

                def flist():
                    try:
                        allkids = list_folder(fpath, show_hidden)
                    except Exception:
                        allkids = None
                    GLib.idle_add(fdone, allkids)

                def fdone(allkids):
                    if gen != self._side_gen:
                        return False
                    if allkids is None:
                        self.preview_folder.clear()
                        self._set_caption(e.name)
                        self._preview_as_icon(e, "")
                        return False
                    kids = sort_entries(allkids, "name", False, True)[:300]
                    self.preview_folder.clear()
                    nd = sum(1 for k in allkids if k.is_dir)
                    files_bytes = sum(k.size or 0 for k in allkids if not k.is_dir)
                    self._set_caption(f"{len(allkids) - nd} files, {nd} folders  ·  {fmt_size(files_bytes)} in files" + ("  ·  showing first 300" if len(allkids) > 300 else ""))
                    for k in kids:
                        self.preview_folder.append([k, "" if k.is_dir else fmt_size(k.size, self.cfg["size_in_bytes"])])
                    self._show_preview("folder")             # content arrived: fade it in
                    self._preview_dir_sizes(gen, [k for k in kids if k.is_dir], fpath)
                    return False
                GLib.timeout_add(250, lambda: (gen == self._side_gen and threading.Thread(target=flist, daemon=True).start(), False)[1])
                return
            self.preview_folder.clear()
            self._set_caption(e.name)
            self._preview_as_icon(e, "")
            return
        self._set_caption(f"{e.name}  ·  {e.type}  ·  {fmt_size(e.size, self.cfg['size_in_bytes'])}")
        ct = e.content_type or ""
        if self.video is not None and e.path and (ct.startswith(("video/", "audio/")) or e.ext in (".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".m4v", ".ogv", ".flac", ".ogg", ".mp3", ".wav")):
            self.video.load(e.path)
            self.video.on_ready = lambda: gen == self._side_gen and self._show_preview("video")
            self._placeholder("Loading video…", gen)
            path = e.path

            def minfo():
                rows = describe_media(path)
                GLib.idle_add(lambda: (gen == self._side_gen and [self._qv_add(k, v) for k, v in rows], False)[1])
            threading.Thread(target=minfo, daemon=True).start()
            return
        w = max(120, self.side.get_allocated_width() - 24)
        h = max(120, self.preview_box.get_allocated_height() - 24)
        if ct.startswith("image/") and e.path:
            self._placeholder("Loading preview…", gen)
            path = e.path

            def work():
                try:
                    full = GdkPixbuf.Pixbuf.get_file_info(path)
                    dims = (full[1], full[2]) if full and full[0] else None
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, True)
                    pb = pb.apply_embedded_orientation() or pb
                except GLib.Error:
                    pb, dims = None, None
                if pb is None:
                    # GdkPixbuf has no loader for this one (EXR, IFF, HDR, DDS, PSD…): built-in decoders / tools
                    res = generate_preview(path, ct, w, h)
                    GLib.idle_add(self._extended_done, gen, e, res)
                    return
                GLib.idle_add(done, pb, dims)

            def done(pb, dims):
                if gen != self._side_gen:
                    return False
                self.preview_image.set_from_pixbuf(pb)
                self._show_preview("image")
                if dims:
                    e.extra_dims = dims
                    self._qv_set("Dimensions", f"{dims[0]} × {dims[1]} px")
                return False
            threading.Thread(target=work, daemon=True).start()
            return
        model_exts = (".obj", ".stl", ".ply", ".fbx", ".usd", ".usda", ".usdc", ".usdz", ".abc", ".gltf", ".glb", ".dae", ".blend", ".3ds", ".x3d", ".lwo", ".off")
        # text formats are parsed whole, so cap them; Alembic/USD/.blend readers only touch the first sample
        model_cap = (16 << 30) if e.ext in (".abc", ".usd", ".usdc", ".usdz", ".blend") else (600 << 20)
        if e.path and e.ext in model_exts and (e.size or 0) >= model_cap:
            self._preview_as_icon(e, f"Too large to preview ({fmt_size(e.size)})")
            return
        if e.path and e.ext in model_exts:
            self._load_model_preview(e, gen)
            return
        if e.path and (e.ext in PREVIEW_DECODERS or e.ext in SEVENZIP_EXTS or e.ext in (".obj", ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".whl",
                                                              ".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".dds", ".psd", ".dpx", ".cin", ".tx", ".tex", ".ptex")
                       or ct.startswith(("video/", "audio/", "application/pdf"))):
            self._placeholder("Loading preview…", gen)
            path = e.path
            threading.Thread(target=lambda: GLib.idle_add(self._extended_done, gen, e, generate_preview(path, ct, w, h)), daemon=True).start()
            return
        if (ct.startswith("text/") or ct in ("application/json", "application/xml", "application/x-shellscript", "application/javascript", "application/x-yaml", "application/x-desktop")) and e.path:
            try:
                with open(e.path, "rb") as f:
                    data = f.read(64 << 10)
                if b"\0" not in data:
                    self._load_text(e.path, data, (e.size or 0) <= 64 << 10)
                    self.edit_entry = e
                    self._show_preview("text")
                    return
            except OSError:
                pass
        self._preview_as_icon(e, "No preview available")

    def _preview_dir_sizes(self, gen, subdirs: List[Entry], parent: str):
        """Fill in subfolder sizes in the folder preview, in the background, with a sanity cap."""
        if not subdirs:
            return
        cancel = threading.Event()
        prev = getattr(self, "_pv_cancel", None)
        if prev:
            prev.set()
        self._pv_cancel = cancel
        paths = [d.path for d in subdirs]

        def work():
            results = {}
            for p in paths:
                if cancel.is_set() or gen != self._side_gen:
                    return
                f, d, b = dir_stats_cached(p, cancel)
                results[p] = b
                if len(results) % 5 == 0 or p == paths[-1]:
                    GLib.idle_add(apply, dict(results))

        def apply(res):
            if gen != self._side_gen:
                return False
            for row in self.preview_folder:
                k = row[0]
                if k.is_dir and k.path in res:
                    row[1] = fmt_size(res[k.path], self.cfg["size_in_bytes"])
                    total = sum(res.values())
            # caption: add folder total once everything is in
            if len(res) == len(paths):
                txt = self.preview_caption.get_text()
                if "  ·  total " not in txt:
                    try:
                        files_part = [x for x in txt.split("  ·  ") if x.endswith("in files")][0]
                        fb = sum(k.size or 0 for k in (r[0] for r in self.preview_folder) if not k.is_dir)
                    except IndexError:
                        fb = 0
                    self.preview_caption.set_text(txt + f"  ·  total {fmt_size(fb + sum(res.values()))}")
            return False
        threading.Thread(target=work, daemon=True).start()

    # ---- quick text edit
    def _load_text(self, path: str, data: bytes, complete: bool):
        """Put file text into the preview buffer (first 64 KB for viewing; the whole file once editing starts)."""
        if self.edit_dirty and self.edit_path and self.edit_path != path:
            if not self.prompt_save():
                return
        self.edit_path, self.edit_full, self.edit_dirty = path, complete, False
        self.edit_toggle.set_sensitive(True)
        self.edit_toggle.set_active(False)
        buf = self.preview_text.get_buffer()
        buf.handler_block_by_func(self._edit_changed)
        buf.set_text(data.decode("utf-8", "replace"))
        buf.handler_unblock_by_func(self._edit_changed)
        self._apply_syntax(path)
        self._update_edit_buttons()
        if self.find_entry.get_text():
            self._find(1, from_start=True)

    def _apply_syntax(self, path: str):
        """Pick a GtkSourceView language from the file name/content type and a colour scheme matching the theme."""
        if GtkSource is None:
            return
        buf = self.preview_text.get_buffer()
        ct, _ = Gio.content_type_guess(path, None)
        lang = self._lang_mgr.guess_language(path, ct)
        if lang is None and os.path.basename(path).lower() in ("makefile", "dockerfile", "cmakelists.txt"):
            lang = self._lang_mgr.get_language({"makefile": "makefile", "dockerfile": "dockerfile", "cmakelists.txt": "cmake"}[os.path.basename(path).lower()])
        buf.set_language(lang)
        buf.set_highlight_syntax(lang is not None)
        settings = Gtk.Settings.get_default()
        dark = bool(settings and settings.get_property("gtk-application-prefer-dark-theme"))
        if not dark and settings:
            dark = "dark" in (settings.get_property("gtk-theme-name") or "").lower()
        for name in (("Adwaita-dark", "oblivion", "cobalt", "classic") if dark else ("Adwaita", "classic", "tango")):
            scheme = self._style_mgr.get_scheme(name)
            if scheme is not None:
                buf.set_style_scheme(scheme)
                break

    def prompt_save(self) -> bool:
        """Ask Save / Discard / Keep editing for unsaved quick-edit changes. Returns True if it's OK to move on."""
        if not (self.edit_dirty and self.edit_path):
            return True
        if getattr(self, "_prompting", False):
            return False
        self._prompting = True
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              text=f"Save changes to {os.path.basename(self.edit_path)}?")
        d.format_secondary_text("Your edits in the preview pane haven't been saved.")
        d.add_buttons("_Discard", 2, "_Keep editing", Gtk.ResponseType.CANCEL, "_Save", 1)
        d.set_default_response(1)
        r = d.run()
        d.destroy()
        self._prompting = False
        if r == 1:
            self.save_edit()
            return not self.edit_dirty
        if r == 2:
            self.revert_edit()
            return True
        return False

    def _show_edit_caption(self):
        ee = self.edit_entry
        if ee is not None:
            self.preview_caption.set_text(f"{ee.name}  ·  {ee.type}  ·  {fmt_size(ee.size, self.cfg['size_in_bytes'])}" + ("  ·  (unsaved)" if self.edit_dirty else "  ·  editing"))

    @property
    def editing_pinned(self) -> bool:
        """True while the preview should stay on the file being edited (edit mode on, or unsaved changes)."""
        return bool(self.edit_path) and (self.edit_dirty or self.edit_toggle.get_active())

    def _deferred_prompt(self):
        self._prompt_pending = False
        if not self.edit_dirty:
            return False
        if self.prompt_save():
            self._update_side()                 # proceed with whatever is selected now
        else:
            # Keep editing: reselect the edited file if it's in this folder, keep the editor focused
            t = self.tab
            name = os.path.basename(self.edit_path or "")
            self._show_preview("text")
            if t is not None and os.path.dirname(self.edit_path) == t.path and any(x.name == name for x in t.entries):
                t.select_names([name], scroll=False)
            self.preview_text.grab_focus()
        return False

    def _edit_toggled(self, btn):
        on = btn.get_active()
        if not on and self.edit_dirty and not self.prompt_save():
            btn.set_active(True)
            return
        if on and self.edit_path and not self.edit_full:
            try:
                if os.path.getsize(self.edit_path) > 8 << 20:
                    message(self, "Edit", "Files over 8 MB are too big for the quick editor — use Open with…", Gtk.MessageType.INFO)
                    btn.set_active(False)
                    return
                with open(self.edit_path, "rb") as f:
                    data = f.read()
                buf = self.preview_text.get_buffer()
                buf.handler_block_by_func(self._edit_changed)
                buf.set_text(data.decode("utf-8", "replace"))
                buf.handler_unblock_by_func(self._edit_changed)
                self.edit_full = True
            except OSError as ex:
                message(self, "Edit", str(ex), Gtk.MessageType.ERROR)
                btn.set_active(False)
                return
        self.preview_text.set_editable(on)
        self.preview_text.set_cursor_visible(on)
        if on:
            self.preview_text.grab_focus()
        self._update_edit_buttons()
        self._update_edit_look()

    def _update_edit_look(self):
        """Make the editing state unmistakable: badge text, button label, coloured frame around the editor."""
        on = self.edit_toggle.get_active()
        ctx = self.preview_text.get_style_context()
        tctx = self.edit_toggle.get_style_context()
        bctx = self.edit_badge.get_style_context()
        for c in ("editing", "readonly"):
            ctx.remove_class(c)
        for c in ("suggested-action", "destructive-action"):
            tctx.remove_class(c)
        for c in ("badge-editing", "badge-dirty"):
            bctx.remove_class(c)
        if on:
            ctx.add_class("editing")
            tctx.add_class("suggested-action")
            self.edit_toggle.set_label("Done")
            if self.edit_dirty:
                self.edit_badge.set_text("EDITING · UNSAVED")
                bctx.add_class("badge-dirty")
            else:
                self.edit_badge.set_text("EDITING")
                bctx.add_class("badge-editing")
        else:
            ctx.add_class("readonly")
            self.edit_toggle.set_label("Edit")
            self.edit_badge.set_text("READ-ONLY")

    def _edit_changed(self, buf):
        if self.edit_toggle.get_active():
            self.edit_dirty = True
            self._update_edit_buttons()

    def _update_edit_buttons(self):
        self.btn_save.set_sensitive(self.edit_dirty)
        self.btn_revert.set_sensitive(self.edit_dirty)
        if hasattr(self, "edit_badge"):
            self._update_edit_look()
        if self.edit_path and self.edit_entry is not None and (self.edit_toggle.get_active() or self.edit_dirty):
            self._show_edit_caption()

    def save_edit(self):
        if not (self.edit_path and self.edit_dirty):
            return
        buf = self.preview_text.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        tmp = self.edit_path + ".kestrel-tmp"
        try:
            st = os.stat(self.edit_path)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, _stat.S_IMODE(st.st_mode))
            os.replace(tmp, self.edit_path)
            self.edit_dirty = False
            self._update_edit_buttons()
            self.flash(f"Saved {os.path.basename(self.edit_path)}")
        except OSError as ex:
            message(self, "Save", str(ex), Gtk.MessageType.ERROR)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def revert_edit(self):
        if not self.edit_path:
            return
        try:
            with open(self.edit_path, "rb") as f:
                data = f.read()
        except OSError as ex:
            message(self, "Revert", str(ex), Gtk.MessageType.ERROR)
            return
        buf = self.preview_text.get_buffer()
        buf.handler_block_by_func(self._edit_changed)
        buf.set_text(data.decode("utf-8", "replace"))
        buf.handler_unblock_by_func(self._edit_changed)
        self.edit_dirty = False
        self.edit_full = True
        self._update_edit_buttons()

    def _edit_key(self, tv, event):
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if mods & Gdk.ModifierType.CONTROL_MASK:
            if event.keyval in (Gdk.KEY_s, Gdk.KEY_S):
                self.save_edit()
                return True
            if event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.find_entry.grab_focus()
                return True
        if event.keyval == Gdk.KEY_Escape:
            self.tab.view.grab_focus()
            return True
        return False

    def _find(self, direction: int, from_start: bool = False):
        """Highlight all matches of the find box in the preview text and move to the next/previous one."""
        buf = self.preview_text.get_buffer()
        needle = self.find_entry.get_text()
        s, e = buf.get_start_iter(), buf.get_end_iter()
        buf.remove_tag(self.find_tag, s, e)
        buf.remove_tag(self.find_cur_tag, s, e)
        if not needle:
            self.find_count.set_text("")
            return
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE | Gtk.TextSearchFlags.TEXT_ONLY
        matches = []
        it = buf.get_start_iter()
        while True:
            hit = it.forward_search(needle, flags, None)
            if not hit:
                break
            matches.append(hit)
            buf.apply_tag(self.find_tag, hit[0], hit[1])
            it = hit[1]
        if not matches:
            self.find_count.set_text("0")
            return
        cur = buf.get_iter_at_mark(buf.get_insert())
        if from_start:
            idx = next((i for i, (a, b) in enumerate(matches) if a.compare(cur) >= 0), 0)
        elif direction > 0:
            idx = next((i for i, (a, b) in enumerate(matches) if a.compare(cur) > 0), 0)
        else:
            idx = next((i for i in range(len(matches) - 1, -1, -1) if matches[i][0].compare(cur) < 0), len(matches) - 1)
        a, b = matches[idx]
        buf.apply_tag(self.find_cur_tag, a, b)
        buf.place_cursor(a)
        self.preview_text.scroll_to_iter(a, 0.1, True, 0.0, 0.5)
        self.find_count.set_text(f"{idx + 1}/{len(matches)}")

    def _extended_done(self, gen, e: Entry, res: dict):
        """Main-thread half of generate_preview(): show the image / text and add info rows to the quick view."""
        if gen != self._side_gen:
            if res.get("png"):
                try:
                    os.remove(res["png"])
                except OSError:
                    pass
            return False
        for k, v in res.get("info", []):
            self._qv_add(k, v)
        if "raw" in res:
            img = res["raw"]
            pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(img.rgb), GdkPixbuf.Colorspace.RGB, False, 8, img.w, img.h, img.w * 3)
            self.preview_image.set_from_pixbuf(pb)
            self._show_preview("image")
        elif "png" in res:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(res["png"])
                self.preview_image.set_from_pixbuf(pb)
                self._show_preview("image")
            except GLib.Error:
                self._preview_as_icon(e, "No preview available")
            finally:
                try:
                    os.remove(res["png"])
                except OSError:
                    pass
        elif "text" in res:
            self.edit_toggle.set_sensitive(False)
            buf = self.preview_text.get_buffer()
            buf.handler_block_by_func(self._edit_changed)
            buf.set_text(res["text"])
            buf.handler_unblock_by_func(self._edit_changed)
            if GtkSource is not None:
                buf.set_language(None)
            self.edit_path = None
            self._show_preview("text")
        else:
            self._preview_as_icon(e, "No preview available")
        return False

    def _qv_add(self, key: str, value: str):
        """Append (or update) a row in the quick-view card."""
        labs = getattr(self, "_qv_labels", {})
        if key in labs:
            labs[key].set_text(value)
            return
        g = self.details_grid
        i = len(labs)
        kl = Gtk.Label(label=key, xalign=1, yalign=0)
        kl.get_style_context().add_class("dim-label")
        vl = Gtk.Label(label=value or "—", xalign=0, wrap=True, selectable=True, hexpand=True)
        vl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        vl.set_max_width_chars(34)
        g.attach(kl, 0, i, 1, 1)
        g.attach(vl, 1, i, 1, 1)
        labs[key] = vl
        self._qv_labels = labs
        g.show_all()

    def _make_model_view(self):
        """GLModelView when hardware acceleration is on and PyOpenGL imports, otherwise the software viewer."""
        if self.cfg.get("gl_preview", True) and HAVE_GL:
            v = GLModelView()
            v.on_error = self._gl_failed
            return v
        return ModelView()

    def _gl_failed(self, msg: str):
        """OpenGL viewer reported a failure: show it where the user is looking, drop to the software renderer."""
        self.flash("OpenGL preview failed; using the software renderer")
        if self.cfg.get("gl_preview", True):
            self.cfg["gl_preview"] = True
            self.toggle_gl_preview()          # flips to False and swaps in ModelView
        self.model_count.set_markup(f"<span foreground='#c0392b'>OpenGL failed: {GLib.markup_escape_text(msg[:160])}</span>")
        self.model_count.set_tooltip_text(msg)

    def _ensure_model_view(self):
        if self.model_view is None:
            self.model_view = self._make_model_view()
            self.model_slot.pack_start(self.model_view, True, True, 0)
            self.model_view.show()
        return self.model_view

    def toggle_gl_preview(self):
        """Preferences → Optional. Swaps the viewer widget in place and re-shows the model."""
        self.cfg["gl_preview"] = not self.cfg.get("gl_preview", True)
        save_config(self.cfg)
        old = self.model_view
        m = old.model if old is not None else None
        if old is not None:
            self.model_slot.remove(old)
        new = self._make_model_view()
        self.model_slot.pack_start(new, True, True, 0)
        new.show()
        self.model_view = new
        if m is not None:
            new.set_model(m)
        if self.cfg["gl_preview"] and not HAVE_GL:
            self.flash("PyOpenGL is not installed; using the software renderer")
        else:
            self.flash("3D preview: " + ("hardware accelerated (OpenGL, white)" if isinstance(new, GLModelView) else "software renderer (blue)"))

    def _load_model_preview(self, e: Entry, gen: int):
        """Decode a 3D file in the worker process and hand it to the ModelView. Honors the 'All triangles'
        override (no face cap, no drag thinning); otherwise meshes are decimated to 120k faces for speed."""
        full = self.model_full_toggle.get_active()
        self._placeholder("Loading model…" + (" (all triangles)" if full else ""), gen)
        path = e.path
        self._model_entry = e

        def mwork():
            try:
                m = run_heavy(load_model, path, sys.maxsize if full else 500000)
            except Exception as ex:
                m = {"error": str(ex)}
            GLib.idle_add(mdone, m)

        def mdone(m):
            if gen != self._side_gen:
                return False
            if not m or "error" in (m or {}):
                if e.ext == ".abc":
                    txt = describe_alembic(e.path)
                    if txt:
                        self._extended_done(gen, e, {"text": txt, "info": [("Alembic", "no PyAlembic found — showing hierarchy")]})
                        return False
                self._preview_as_icon(e, "Couldn't read this model" + (f"\n{m['error']}" if m else ""))
                return False
            for k, v in m["info"]:
                self._qv_add(k, v)
            mv = self._ensure_model_view()
            shown, total = len(m["faces"]), m.get("nfaces_total", len(m["faces"]))
            self.model_count.set_text(f"{shown:,} of {total:,} faces" if shown < total else f"{total:,} faces")
            if isinstance(mv, ModelView) and np is not None:
                # software renderer: fade in when the first frame arrives, not before (the widget would be blank)
                mv.on_first_frame = lambda: gen == self._side_gen and self._show_preview("model")
                mv.set_model(m)
                st = self.preview_stack.get_allocation()
                mv.prime(max(mv.get_allocated_width(), st.width), max(mv.get_allocated_height(), st.height - 60))
            else:
                mv.set_model(m)
                self._show_preview("model")
            return False
        threading.Thread(target=mwork, daemon=True).start()

    def _toggle_model_full(self, btn):
        e = getattr(self, "_model_entry", None)
        if e is None or self.preview_stack.get_visible_child_name() not in ("model", "info"):
            return
        self._side_gen += 1
        self._preview_key = None            # force a reload of the same file at the new detail level
        self._load_model_preview(e, self._side_gen)

    FADE_MS = 125

    def _set_caption(self, text: str):
        """Caption for the content that is about to be shown; applied when that content (or its placeholder)
        actually appears, so the caption never changes ahead of the picture under it."""
        self._pending_caption = text

    def _apply_caption(self):
        pc = getattr(self, "_pending_caption", None)
        if pc is not None:
            self.preview_caption.set_text(pc)
            self._pending_caption = None

    def _placeholder(self, text: str, gen: int, delay_ms: int = 250):
        """Show an 'info' placeholder only if the real content hasn't arrived after delay_ms. Until then the
        previous preview stays on screen, so quick loads go old → new in one fade, with no grey in between."""
        def due():
            if gen != self._side_gen or getattr(self, "_content_gen", -1) == gen:
                return False                      # superseded, or the real content already landed
            self.preview_info.set_text(text)
            self._apply_caption()
            self._show_preview("info")
            self._take_snapshot(hold=False)  # so the content that follows crossfades from the placeholder too
                                             # (no hold: the fade INTO the placeholder is still running)
            return False
        GLib.timeout_add(delay_ms, due)

    def _show_preview(self, name: str):
        """Switch the preview pane to child `name` with a ~⅛ s fade. The stack crossfades on its own when the
        child changes; when the same child is reused for a new file (image → image) we fade that child's
        opacity back in so every file switch reads as a soft cut rather than a pop."""
        stack = self.preview_stack
        child = stack.get_child_by_name(name)
        if child is None:
            return
        if name != "info":
            self._content_gen = self._side_gen          # real content for the current selection is on screen
            self._apply_caption()
        stack.set_visible_child_name(name)
        self._crossfade_from_snapshot()

    def _take_snapshot(self, hold: bool = True):
        """Render the preview pane as it is right now into an image (kept until the next content lands).
        With `hold` the image is also shown at once at full opacity, covering whatever the pane does while
        the next item loads."""
        if not self.cfg.get("preview_pane") or not self.preview_stack.get_realized():
            return
        a = self.preview_stack.get_allocation()
        if a.width < 2 or a.height < 2:
            return
        try:
            import cairo
            scale = self.get_scale_factor()
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, a.width * scale, a.height * scale)
            surf.set_device_scale(scale, scale)
            cr = cairo.Context(surf)
            # The snapshot must be OPAQUE or the new content shows through it from frame one (only the old
            # text/image would appear to fade). Most of our containers have no background of their own —
            # the colour comes from the toplevel — so paint the theme's window colour first, then any
            # container backgrounds, then the content.
            ok, bg = self.get_style_context().lookup_color("theme_bg_color")
            if ok:
                cr.set_source_rgb(bg.red, bg.green, bg.blue)
            else:
                cr.set_source_rgb(0.13, 0.13, 0.13)
            cr.paint()
            Gtk.render_background(self.get_style_context(), cr, 0, 0, a.width, a.height)
            for wdg in (self.side, self.preview_box, self.preview_stack):
                Gtk.render_background(wdg.get_style_context(), cr, 0, 0, a.width, a.height)
            self.preview_stack.draw(cr)
            self._snap_surface = surf
            if not hold:
                return
            # HOLD the snapshot on screen at full opacity right away. Whatever the pane does underneath while
            # the next item loads (blanking the video surface, clearing a list, a placeholder) stays hidden;
            # the fade-out starts only when the new content is in place — so every switch is old → new.
            self._fade_token = getattr(self, "_fade_token", 0) + 1
            self._snap_draw = surf
            self._snap_alpha = 1.0
            self.snap_area.queue_draw()
        except Exception as ex:
            bench(f"snapshot failed: {ex}")
            self._snap_surface = None

    def _drop_snapshot(self):
        """Discard a held snapshot without a fade (the content under it is unchanged)."""
        self._snap_surface = None
        self._fade_token = getattr(self, "_fade_token", 0) + 1
        if getattr(self, "_snap_draw", None) is not None:
            self._snap_draw = None
            self.snap_area.queue_draw()

    def _crossfade_from_snapshot(self):
        surf = self._snap_surface
        self._snap_surface = None
        if surf is None:
            return
        token = getattr(self, "_fade_token", 0) + 1
        self._fade_token = token
        self._snap_draw = surf
        self._snap_alpha = 1.0
        self.snap_area.queue_draw()
        t0 = time.monotonic()

        def tick():
            if self._fade_token != token:
                return False
            a = min(1.0, (time.monotonic() - t0) * 1000.0 / self.FADE_MS)
            self._snap_alpha = 1.0 - a * a * (3 - 2 * a)      # smoothstep out
            self.snap_area.queue_draw()
            if a >= 1.0:
                self._snap_draw = None
                self.snap_area.queue_draw()
                return False
            return True
        GLib.timeout_add(16, tick)

    def _draw_snapshot(self, w, cr):
        surf = getattr(self, "_snap_draw", None)
        if surf is None or self._snap_alpha <= 0:
            return False
        cr.set_source_surface(surf, 0, 0)
        cr.paint_with_alpha(self._snap_alpha)
        return True

    def _preview_as_icon(self, e: Entry, note: str):
        pb = ICONS.pixbuf(e.icon, 128)
        self.preview_icon_img.set_from_pixbuf(pb)
        self.preview_icon_lab.set_text(note)
        self._show_preview("icon")

    # ---- quick view
    def _qv_set(self, key: str, value: str):
        lab = getattr(self, "_qv_labels", {}).get(key)
        if lab is not None:
            lab.set_text(value)

    def _update_details(self, sel: List[Entry]):
        g = self.details_grid
        for c in g.get_children():
            g.remove(c)
        self._qv_labels = {}
        t = self.tab
        gen = self._side_gen
        self.qv_actions.set_sensitive(bool(sel))
        f_long = "%A, %B %d, %Y  %H:%M"
        if not sel:
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(Gio.ThemedIcon.new("folder"), 32))
            self.details_title.set_markup(f"<b>{GLib.markup_escape_text(self.display_path(t.path))}</b>")
            nd = sum(1 for e in t.entries if e.is_dir)
            self.qv_subtitle.set_text(f"{len(t.entries) - nd} files, {nd} folders")
            rows = [("Location", t.path if t.path not in VIRTUAL else self.display_path(t.path))]
            if t.path not in VIRTUAL:
                rows.append(("Disk", "…"))
                tp = t.path

                def dwork():
                    try:
                        u = shutil.disk_usage(tp)
                        txt = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                    except OSError:
                        txt = "unavailable"
                    GLib.idle_add(lambda: (gen == self._side_gen and self._qv_set("Disk", txt), False)[1])
                threading.Thread(target=dwork, daemon=True).start()
        elif len(sel) > 1:
            nd = sum(1 for e in sel if e.is_dir)
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(Gio.ThemedIcon.new("edit-select-all"), 32))
            self.details_title.set_markup(f"<b>{len(sel)} items selected</b>")
            self.qv_subtitle.set_text(f"{len(sel) - nd} files, {nd} folders")
            total = sum(e.size or 0 for e in sel if not e.is_dir)
            rows = [("Size", fmt_size(total) + ("  (files only)" if nd else "")),
                    ("Location", os.path.dirname(sel[0].path))]
            exts = sorted({e.ext for e in sel if e.ext})
            if exts:
                rows.append(("Types", ", ".join(exts[:8]) + ("…" if len(exts) > 8 else "")))
        else:
            e = sel[0]
            self.qv_icon.set_from_pixbuf(ICONS.pixbuf(e.icon, 32))
            self.details_title.set_markup(f"<b>{GLib.markup_escape_text(e.name)}</b>")
            self.qv_subtitle.set_text(e.type)
            rows = []
            if e.is_dir:
                rows.append(("Contains", "calculating…"))
                rows.append(("Size", "calculating…"))
            else:
                rows.append(("Size", f"{fmt_size(e.size)}  ({(e.size or 0):,} bytes)"))
            rows.append(("Location", e.location or os.path.dirname(e.path)))
            rows.append(("Modified", fmt_date(e.mtime, f_long)))
            if e.ctime:
                rows.append(("Created", fmt_date(e.ctime, f_long)))
            rows.append(("Accessed", fmt_date(e.atime, f_long)))
            rows.append(("Owner", e.owner))
            rows.append(("Permissions", (_stat.filemode(e.mode) + f"  ({oct(_stat.S_IMODE(e.mode))[2:]})") if e.mode else ""))
            if e.content_type:
                rows.append(("MIME type", e.content_type))
            if not e.is_dir:
                app = Gio.AppInfo.get_default_for_type(e.content_type, False) if e.content_type else None
                rows.append(("Opens with", app.get_display_name() if app else "—"))
            if (e.content_type or "").startswith("image/"):
                rows.append(("Dimensions", getattr(e, "extra_dims", None) and f"{e.extra_dims[0]} × {e.extra_dims[1]} px" or "…"))
            if (e.content_type or "").startswith("text/") and e.path and (e.size or 0) < 8 << 20:
                rows.append(("Lines", "…"))
            if e.is_link and e.path:
                try:
                    rows.append(("Link target", os.readlink(e.path)))
                except OSError:
                    pass
            if e.deleted:
                rows.append(("Deleted", fmt_date(e.deleted, f_long)))
            if script_runner(e.path or ""):
                rows.append(("Runnable", "yes"))
            # background facts: folder size / contents, text line count
            path = e.path
            if e.is_dir and path:
                cancel = threading.Event()
                self._qv_cancel = getattr(self, "_qv_cancel", None)
                if self._qv_cancel:
                    self._qv_cancel.set()
                self._qv_cancel = cancel

                def work():
                    f, d, b = dir_stats_cached(path, cancel)
                    if not cancel.is_set():
                        GLib.idle_add(lambda: (gen == self._side_gen and (self._qv_set("Contains", f"{f:,} files, {d:,} folders"), self._qv_set("Size", fmt_size(b) + f"  ({b:,} bytes)"))) and False)
                # same 250 ms debounce as the folder preview: a double-click must not start a size walk
                GLib.timeout_add(250, lambda: (gen == self._side_gen and not cancel.is_set() and threading.Thread(target=work, daemon=True).start(), False)[1])
            elif (e.content_type or "").startswith("text/") and path and (e.size or 0) < 8 << 20:
                def work_lines():
                    try:
                        with open(path, "rb") as fh:
                            n = sum(buf.count(b"\n") for buf in iter(lambda: fh.read(1 << 20), b""))
                    except OSError:
                        n = 0
                    GLib.idle_add(lambda: (gen == self._side_gen and self._qv_set("Lines", f"{n:,}")) and False)
                threading.Thread(target=work_lines, daemon=True).start()
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=1, yalign=0)
            kl.get_style_context().add_class("dim-label")
            vl = Gtk.Label(label=v or "—", xalign=0, wrap=True, selectable=True, hexpand=True)
            vl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            vl.set_max_width_chars(34)
            g.attach(kl, 0, i, 1, 1)
            g.attach(vl, 1, i, 1, 1)
            self._qv_labels[k] = vl
        g.show_all()

    # ------------------------------------------------------------------ tabs & panes
    @property
    def page(self) -> Optional["Page"]:
        n = self.notebook.get_current_page()
        return self.notebook.get_nth_page(n) if n >= 0 else None

    @property
    def tab(self) -> Optional[FolderView]:
        pg = self.page
        return (pg.active or pg.panes[0]) if pg else None

    def new_tab(self, path: str, switch: bool = True) -> FolderView:
        pg = Page(self, path)
        lab = Gtk.Box(spacing=2)
        lab.pack_start(pg.label, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.get_style_context().add_class("tab-close")
        close.connect("clicked", lambda b: pg.win.close_tab(pg))
        lab.pack_start(close, False, False, 0)
        lab.show_all()
        ev = Gtk.EventBox()
        ev.add(lab)
        ev.connect("button-press-event", lambda w, e: pg.win._on_tab_label_button(w, e, pg))
        ev.show_all()
        n = self.notebook.append_page(pg, ev)
        self.notebook.set_tab_reorderable(pg, True)
        self.notebook.set_tab_detachable(pg, True)
        pg.show_all()
        pg._update_headers()
        if switch:
            self.notebook.set_current_page(n)
        self._update_tabs_visible()
        return pg.panes[0]

    def _on_tab_label_button(self, w, event, pg):
        if event.button == 2:
            self.close_tab(pg)
            return True
        if event.button == 3:
            self.notebook.set_current_page(self.notebook.page_num(pg))
            spec = [("New _tab", lambda: self.new_tab(pg.active.path if pg.active.path not in VIRTUAL else HOME)),
                    ("_Duplicate tab", lambda: self.new_tab(pg.active.path)), "-",
                    ("Move tab to new _window", lambda: self.detach_tab(pg)),
                    ("Close split" if pg.split else "_Split view (two folders side by side)", lambda: pg.unsplit() if pg.split else pg.do_split()), "-",
                    ("Close other tabs", lambda: [self.close_tab(p) for p in self._pages() if p is not pg]),
                    ("_Close tab", lambda: self.close_tab(pg))]
            m = build_menu(spec)
            m.attach_to_widget(w, None)
            m.popup_at_pointer(event)
            return True
        return False

    def detach_tab(self, pg: "Page"):
        """Move a tab into a new window (same as dragging it out)."""
        if self.notebook.get_n_pages() == 1:
            return
        win = self.app.new_window("")
        tab_label = self.notebook.get_tab_label(pg)
        self.notebook.detach_tab(pg)
        n = win.notebook.append_page(pg, tab_label)
        win.notebook.set_tab_reorderable(pg, True)
        win.notebook.set_tab_detachable(pg, True)
        pg.win = win
        for fv in pg.panes:
            fv.win = win
        win.notebook.set_current_page(n)
        win.present()

    def _on_create_window(self, nb, page, x, y):
        win = self.app.new_window("")
        win.move(max(0, int(x) - 100), max(0, int(y) - 40))
        return win.notebook

    def _on_page_added(self, nb, child, n):
        if isinstance(child, Page):
            child.win = self
            for fv in child.panes:
                fv.win = self
        self._update_tabs_visible()

    def _on_page_removed(self, nb, child, n):
        self._update_tabs_visible()
        if nb.get_n_pages() == 0:
            GLib.idle_add(lambda: self.close() or False)

    def toggle_split(self):
        pg = self.page
        if pg:
            pg.unsplit() if pg.split else pg.do_split()

    def set_active_pane(self, fv: FolderView):
        pg = fv.page
        if pg is None or fv.win is not self:
            return
        if pg.active is not fv:
            pg.set_active(fv)
        self._refresh_crumbs()
        self._update_history_buttons()
        self._update_title()
        self._update_status()
        self._update_side()

    def close_tab(self, pg: Optional["Page"] = None):
        pg = pg or self.page
        if pg is None:
            return
        if self.notebook.get_n_pages() == 1:
            self.close()
            return
        for fv in pg.panes:
            fv.stop_search()
            if fv.monitor:
                fv.monitor.cancel()
        self.notebook.remove_page(self.notebook.page_num(pg))
        self._update_tabs_visible()

    def _pages(self) -> List["Page"]:
        return [self.notebook.get_nth_page(i) for i in range(self.notebook.get_n_pages())]

    def _update_tabs_visible(self):
        self.notebook.set_show_tabs(self.notebook.get_n_pages() > 1)

    def _on_switch_page(self, nb, page, n):
        GLib.idle_add(self._after_switch)

    def _after_switch(self):
        if self.tab is None:
            return False
        self._refresh_crumbs()
        self._update_history_buttons()
        self._update_title()
        self._update_status()
        self._update_side()
        self.tab.view.grab_focus()
        return False

    def on_tab_loaded(self, t: FolderView):
        if BENCH and not getattr(self, "_bench_listed", True) and not t.loading and not t.partial and t.entries:
            self._bench_listed = True
            bench(f"listed {len(t.entries)} items")
        if t.page:
            t.page.update_label()
        if t is self.tab:
            self._update_status()
            self._update_side()
            self._update_title()
            self._refresh_crumbs()

    def on_selection_changed(self, t: FolderView):
        self._nav_preview = None               # the folder view owns the side pane again
        if t is self.tab:
            self._update_status()
            self._update_side()

    def _update_title(self):
        t = self.tab
        if t is None:
            return
        self.set_title((t.path if (self.cfg["full_path_title"] and t.path not in VIRTUAL) else t.title()) + " — " + APP_NAME)

    def _update_history_buttons(self):
        t = self.tab
        if t is None:
            return
        self.btn_back.set_sensitive(bool(t.back))
        self.btn_fwd.set_sensitive(bool(t.fwd))
        self.btn_up.set_sensitive(t.path != THISPC)

    def on_tab_busy(self, t: FolderView):
        """A listing just started in t: show the wheel right away (the status text itself is only
        refreshed when results arrive)."""
        if t is self.tab:
            self._set_spinner(True)

    SPINNER_DELAY_MS = 250        # only waits longer than this earn a wheel; quick folders never flash it

    def _set_spinner(self, on: bool):
        pending = getattr(self, "_spin_timer", None)
        if on:
            if self.spinner.get_visible() or pending:
                return
            self._spin_timer = GLib.timeout_add(self.SPINNER_DELAY_MS, self._spinner_due)
        else:
            if pending:
                GLib.source_remove(pending)
                self._spin_timer = None
            if self.spinner.get_visible():
                self.spinner.stop()
                self.spinner.hide()

    def _spinner_due(self):
        self._spin_timer = None
        t = self.tab
        if t is not None and (t.busy or t.loading or getattr(t, "partial", False) or (t.search and t.search.is_alive())):
            self.spinner.show()
            self.spinner.start()
        return False

    def _update_status(self):
        t = self.tab
        if t is None:
            return
        self._set_spinner(bool(t.busy or t.loading or getattr(t, "partial", False) or (t.search and t.search.is_alive())))
        n = len(t.entries)
        sel = t.selected_entries()
        s = f"{n} item{'s' if n != 1 else ''}"
        if sel:
            total = sum(e.size or 0 for e in sel if not e.is_dir)
            s += f"    {len(sel)} item{'s' if len(sel) != 1 else ''} selected" + (f"  {fmt_size(total)}" if total else "")
        if t.search and t.search.is_alive():
            s += "    Searching…"
        elif getattr(t, "partial", False) and getattr(t, "indexed_paint", False):
            s = f"{n} item{'s' if n != 1 else ''}    Verifying against the disk…"
        elif getattr(t, "partial", False):
            s = f"Reading folder…  {getattr(t, 'partial_count', 0):,} items so far"
        elif t.busy:
            s = f"Reading {self.display_path(t.path)}…"
        elif t.loading:
            s += "    Loading…"
        if self.clipboard["mode"]:
            s += f"    [{len(self.clipboard['paths'])} {self.clipboard['mode']}]"
        self.status.set_text(s)

    def flash(self, msg: str):
        self.status.set_text(msg)
        if self.status_timer:
            GLib.source_remove(self.status_timer)
        self.status_timer = GLib.timeout_add(3500, self._flash_end)

    def _flash_end(self):
        self.status_timer = 0
        self._update_status()
        return False

    # ------------------------------------------------------------------ navigation
    def navigate(self, path: str, record: bool = True, tab: Optional[FolderView] = None):
        self._nav_preview = None
        t = tab or self.tab
        if t.search:
            t.stop_search()
            self.searchbox.set_text("")
        if path not in VIRTUAL:
            path = os.path.abspath(path)
            # local paths are checked here (cheap); a network path is not stat'ed on the GTK thread — the
            # listing thread reports a missing folder in the view instead
            if not is_network_path(path) and not os.path.isdir(path):
                self.flash(f"Cannot open: {path}")
                return
        if t is None:
            return
        NAV_LOG.append({"path": path, "t0": time.perf_counter(), "listed": None, "shown": None, "n": 0,
                        "threads": threading.active_count(), "stalls_before": len(WATCHDOG.records)})
        del NAV_LOG[:-400]
        if record and t.path != path:
            t.back.append(t.path)
            t.fwd.clear()
        cur = t.selected_entries()
        if cur:
            t.remember[t.path] = cur[0].name
        old_virtual = t.path in VIRTUAL
        t.path = path
        if path not in VIRTUAL:
            self._bump_frequent(path)
        t.apply_layout()
        t.load(keep_selection=False)
        name = t.remember.get(path)
        if name:
            GLib.timeout_add(250, lambda: (t.select_names([name]) if t.path == path else None) or False)
        if t is self.tab:
            self._refresh_crumbs()
            self._update_history_buttons()
            self._update_title()
        if t.page:
            t.page.update_label()

    def _bump_frequent(self, path: str):
        fq = self.cfg["frequent"]
        fq[path] = fq.get(path, 0) + 1
        if len(fq) > 300:
            for p, _ in sorted(fq.items(), key=lambda kv: kv[1])[:100]:
                del fq[p]

    def frequent_folders(self) -> List[str]:
        return [p for p, _ in sorted(self.cfg["frequent"].items(), key=lambda kv: -kv[1]) if PATHS.isdir(p) and p != HOME]

    def go_back(self, n: int = 1):
        t = self.tab
        for _ in range(n):
            if not t.back:
                return
            t.fwd.append(t.path)
            target = t.back.pop()
            if _ == n - 1:
                self.navigate(target, record=False)
            else:
                t.path = target

    def go_forward(self):
        t = self.tab
        if t.fwd:
            t.back.append(t.path)
            self.navigate(t.fwd.pop(), record=False)

    def go_up(self):
        t = self.tab
        if t.path in VIRTUAL:
            if t.path != THISPC:
                self.navigate(THISPC)
            return
        parent = os.path.dirname(t.path.rstrip("/")) or "/"
        child = t.path
        if parent == t.path:
            self.navigate(THISPC)
        else:
            t.remember[parent] = os.path.basename(child)
            self.navigate(parent)

    def activate_entry(self, e: Optional[Entry]):
        if e is None:
            return
        t = self.tab
        if t.path == NETWORK and not e.path and e.uri:
            self.open_network_entry(e)
            return
        if getattr(e, "volume", None) is not None:
            self.mount_volume(e.volume)
            return
        if e.path and not e.uri and t.path not in VIRTUAL and not os.path.lexists(e.path):
            self.flash(f"{e.name} is no longer there")       # e.g. opened from an index paint before the disk answered
            t.load()
            return
        if e.is_dir:
            if self.cfg["browse_in_new_tab"] and t.path not in VIRTUAL:
                self.new_tab(e.path)
            else:
                self.navigate(e.path)
            return
        if t.path == TRASH:
            self.flash("Items in the Recycle Bin can't be opened. Restore it first.")
            return
        if self.cfg.get("archive_action", "extract") == "extract" and self.is_archive(e) and e.path and os.path.isdir(os.path.dirname(e.path)):
            self.extract_here(e)          # Explorer-style: an archive opens as a folder — here, by extracting it beside itself
            return
        self.open_file(e)

    def open_file(self, e: Entry):
        rec = self.cfg["recent"]
        if e.path in rec:
            rec.remove(e.path)
        rec.insert(0, e.path)
        del rec[30:]
        if getattr(self, "nav_recent", None) is not None:
            self._fill_recent(self.nav_recent)
        if not launch_default(e.uri or e.path, self):
            self.flash("No application is registered to open this file. Try Open with…")

    def open_with(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        d = Gtk.AppChooserDialog.new(self, Gtk.DialogFlags.MODAL, e.gfile)
        d.set_heading(f"How do you want to open {e.name}?")
        w = d.get_widget()
        w.set_show_other(True)
        w.set_show_default(True)
        if d.run() == Gtk.ResponseType.OK:
            app = d.get_app_info()
            if app:
                try:
                    app.launch([e.gfile], self.get_display().get_app_launch_context())
                except GLib.Error as ex:
                    message(self, "Open with", ex.message, Gtk.MessageType.ERROR)
        d.destroy()

    def open_terminal(self, cwd: Optional[str] = None):
        cwd = cwd or (self.tab.path if self.tab.path not in VIRTUAL else HOME)
        if not spawn_terminal(cwd):
            message(self, "Open terminal", "No terminal emulator was found. Set $TERMINAL or install one.", Gtk.MessageType.WARNING)

    def open_shell_here(self):
        """'Open command prompt here' — same as a terminal in this folder."""
        self.open_terminal()

    # ------------------------------------------------------------------ virtual folders
    def display_path(self, path: str) -> str:
        return {QUICK: "Quick access", THISPC: "This PC", TRASH: "Recycle Bin", NETWORK: "Network", RECENT: "Recent"}.get(path) or \
            ("Local Disk (/)" if path == "/" else (os.path.basename(HOME) if path == HOME else (os.path.basename(path.rstrip("/")) or path)))

    def display_name(self, e: Entry) -> str:
        n = e.name
        if self.cfg["hide_extensions"] and not e.is_dir and e.ext and os.path.splitext(n)[0]:
            n = os.path.splitext(n)[0]
        return n

    def cell_text(self, e: Entry, key: str) -> str:
        f = self.cfg["date_format"]
        if key == "modified":
            return fmt_date(e.mtime, f)
        if key == "created":
            return fmt_date(e.ctime, f)
        if key == "accessed":
            return fmt_date(e.atime, f)
        if key == "type":
            return e.type
        if key == "size":
            return fmt_size(e.size, self.cfg["size_in_bytes"])
        if key == "owner":
            return e.owner
        if key == "perms":
            return _stat.filemode(e.mode)[1:] if e.mode else ""
        if key == "location":
            return e.location
        if key == "deleted":
            return fmt_date(e.deleted, f)
        return ""

    def quick_entries(self) -> List[Entry]:
        out = []
        for p in self.cfg["pinned"]:
            if PATHS.isdir(p):
                e = entry_for_path(p)
                e.section = "Pinned folders"
                out.append(e)
        if self.cfg["show_frequent"]:
            for p in self.frequent_folders()[:8]:
                if p not in self.cfg["pinned"]:
                    e = entry_for_path(p)
                    e.section = "Frequent folders"
                    e.location = os.path.dirname(p)
                    out.append(e)
        if self.cfg["show_recent"]:
            for p in self.cfg["recent"][:20]:
                if PATHS.exists(p):
                    e = entry_for_path(p)
                    e.location = os.path.dirname(p)
                    e.section = "Recent files"
                    out.append(e)
        return out

    def recent_entries(self) -> List[Entry]:
        out = []
        for p in self.cfg["recent"]:
            if PATHS.exists(p):
                e = entry_for_path(p)
                e.location = os.path.dirname(p)
                e.section = "Recent files"
                out.append(e)
        return out

    def pc_entries(self) -> List[Entry]:
        out = []
        for label, p, icon in xdg_user_dirs():
            e = entry_for_path(p, label)
            e.icon = Gio.ThemedIcon.new(icon)
            e.section = "Folders"
            out.append(e)
        # Devices and drives, grouped by the PHYSICAL disk each mount lives on — Linux shows mounts, not disks,
        # so a section per disk ("Samsung SSD 980 PRO 2TB · NVME · 2.0 TB") makes it visible which paths share
        # hardware. Mounts lsblk can't place (network, FUSE, virtual) fall into their own sections.
        drive_entries = []
        by_disk: Dict[str, List[Entry]] = {}
        loose: List[Entry] = []
        nets: List[Entry] = []
        disks = physical_disks()
        for label, p, icon, k in drives():
            e = entry_for_path(p, label)
            e.icon = icon
            e.drive_kind = k
            if k == "network":
                e.section = "Network locations"
                e.type = f"Network drive  ·  {_mount_source(p) or p}"
                nets.append(e)
                continue
            e.type = "…"
            hit = disk_for_mount(p)
            if hit:
                d, part = hit
                e.section = f"{d['model']}  ·  {d['tran'] or 'internal'}  ·  {fmt_size(d['size'])}" + ("  ·  removable" if d["removable"] else "")
                e.location = f"{part['name']}" + (f"  {part['fstype']}" if part["fstype"] else "")
                by_disk.setdefault(d["name"], []).append(e)
            else:
                e.section = "Other mounts"
                loose.append(e)
            drive_entries.append(e)
        # unmounted partitions: shown greyed under their disk as information, not as drives (dual-boot rule);
        # removable ones are offered to mount
        mountable = {vol.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE): (label, vol, vicon) for label, vol, vicon in volumes_unmounted()}
        for d in disks:
            sec_entries = by_disk.get(d["name"])
            extra = []
            for part in d["parts"]:
                if part["mountpoints"]:
                    continue
                dev = "/dev/" + part["name"]
                if dev in mountable:
                    label, vol, vicon = mountable[dev]
                    e = Entry()
                    e.name, e.is_dir, e.icon = label, True, vicon
                    e.type = "Not mounted"
                    e.volume, e.drive_kind = vol, "volume"
                    e.location = part["name"]
                    extra.append(e)
                elif part["fstype"] and part["fstype"] not in ("swap", "LVM2_member", "crypto_LUKS", "linux_raid_member"):
                    e = Entry()
                    e.name = f"{part['label'] or part['name']}  (not mounted)"
                    e.is_dir = False
                    e.icon = Gio.ThemedIcon.new("drive-harddisk-symbolic")
                    e.type = f"{part['fstype']} · {fmt_size(part['size'])}"
                    e.drive_kind = "unmounted"
                    e.location = part["name"]
                    extra.append(e)
            if sec_entries or extra:
                sec = f"{d['model']}  ·  {d['tran'] or 'internal'}  ·  {fmt_size(d['size'])}" + ("  ·  removable" if d["removable"] else "")
                for e in extra:
                    e.section = sec
                out.extend((sec_entries or []) + extra)
        out.extend(loose)
        out.extend(nets)
        # free-space queries can hang on a dead network mount: never do them on the main thread
        t = self.tab

        def work():
            for e in drive_entries:
                try:
                    u = shutil.disk_usage(e.path)
                    e.usage = (u.total - u.free, u.total)
                    e.type = f"{fmt_size(u.free)} free of {fmt_size(u.total)}"
                except OSError:
                    e.type = "unavailable"
            GLib.idle_add(lambda: (t and t.path == THISPC and (touch_all(t.store), t.pcview.refresh())) or False)
        threading.Thread(target=work, daemon=True).start()
        return out

    # ------------------------------------------------------------------ view settings
    def sort_by(self, key: str):
        if self.cfg["sort_key"] == key:
            self.cfg["sort_desc"] = not self.cfg["sort_desc"]
        else:
            self.cfg["sort_key"], self.cfg["sort_desc"] = key, False
        for t in self._tabs():
            t.rebuild_columns()
            t.resort()

    def set_sort_desc(self, desc: bool):
        self.cfg["sort_desc"] = desc
        for t in self._tabs():
            t.resort()

    def set_group(self, g: str):
        self.cfg["group_by"] = g
        for t in self._tabs():
            t.resort()

    def set_layout(self, lay: str):
        """Changes the active pane only (so split panes can differ); also becomes the default for new panes."""
        self.cfg["layout"] = lay
        t = self.tab
        if t is not None:
            t.layout = lay
            t.apply_layout()
            t.resort()

    def toggle_column(self, key: str):
        cols = self.cfg["columns"]
        if key in cols:
            cols.remove(key)
        else:
            cols.append(key)
        for t in self._tabs():
            t.rebuild_columns()

    def toggle_cfg(self, key: str):
        self.cfg[key] = not self.cfg[key]
        self.apply_cfg()

    def apply_cfg(self):
        want_gl = bool(self.cfg.get("gl_preview", True) and HAVE_GL)
        have_gl = isinstance(self.model_view, GLModelView) if self.model_view is not None else want_gl
        if want_gl != have_gl:
            self.cfg["gl_preview"] = not self.cfg["gl_preview"]
            self.toggle_gl_preview()         # flips it back and swaps the viewer in place
        self._apply_panes()
        for t in self._tabs():
            t.apply_layout()
        self.rebuild_nav()
        self.reload_all()
        self._update_title()
        save_config(self.cfg)

    def _tabs(self) -> List[FolderView]:
        return [fv for pg in self._pages() for fv in pg.panes]

    def reload_all(self):
        for t in self._tabs():
            if not t.search:
                t.load()

    # ------------------------------------------------------------------ search
    def start_search(self, text: str):
        text = text.strip()
        if not text:
            self.clear_search()
            return
        t = self.tab
        t.start_search(text)
        t.apply_layout()
        self._update_title()
        self.on_tab_loaded(t)

    def clear_search(self):
        t = self.tab
        if t.search:
            t.stop_search()
            t.load()
        self.searchbox.set_text("")
        self._update_title()
        self.on_tab_loaded(t)

    # ------------------------------------------------------------------ keys & mouse
    def _on_key(self, win, event):
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.TextView)) or self.editing:
            if self.propagate_key_event(event):
                return True
            return self.activate_key(event)
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if key == Gdk.KEY_F10 and mods == 0:
            self.menubar.select_first(True)
            return True
        return False

    def on_view_key(self, view, event, t: FolderView):
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift, alt = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK), bool(mods & Gdk.ModifierType.MOD1_MASK)
        if key == Gdk.KEY_BackSpace and not mods:
            self.go_back()
            return True
        if key == Gdk.KEY_Delete:
            self.delete(permanent=shift)
            return True
        if key == Gdk.KEY_F2 and not mods:
            t.start_rename()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and alt:
            self.properties()
            return True
        if key == Gdk.KEY_Menu or (key == Gdk.KEY_F10 and shift):
            self.context_menu(t, None)
            return True
        if key == Gdk.KEY_Escape:
            if t.search:
                self.clear_search()
            else:
                t.select_none()
            return True
        if key == Gdk.KEY_space and ctrl:
            p = cursor_path(view)
            if p is not None:
                if view is t.tree:
                    s = view.get_selection()
                    (s.unselect_path if s.path_is_selected(p) else s.select_path)(p)
                else:
                    (view.unselect_path if view.path_is_selected(p) else view.select_path)(p)
            return True
        if view is t.icons and not mods and event.string and event.string.isprintable():
            self._icon_typeahead(t, event.string)
            return True
        return False

    def _icon_typeahead(self, t: FolderView, ch: str):
        now = time.time()
        if now - getattr(self, "_ta_time", 0) > 1.0:
            self._ta = ""
        self._ta = getattr(self, "_ta", "") + ch
        self._ta_time = now
        q = self._ta.lower()
        cur = cursor_path(t.icons)
        start = (cur.get_indices()[0] + (0 if len(self._ta) > 1 else 1)) if cur else 0
        n = len(t.store)
        for off in range(n):
            i = (start + off) % n
            e = t.entry_at(Gtk.TreePath.new_from_indices([i]))
            if e and e.name.lower().startswith(q):
                p = Gtk.TreePath.new_from_indices([i])
                t.icons.unselect_all()
                t.icons.select_path(p)
                t.icons.set_cursor(p, None, False)
                t.icons.scroll_to_path(p, False, 0, 0)
                return

    def _on_button(self, win, event):
        if event.button == 8:
            self.go_back()
            return True
        if event.button == 9:
            self.go_forward()
            return True
        return False

    def _on_close(self, *a):
        if not self.prompt_save():
            return True        # keep the window open
        if getattr(self, "video", None) is not None:
            self.video.stop()
        self.cfg["window"] = list(self.get_size())
        self.cfg["nav_width"] = self.paned.get_position()
        self._remember_side_width()
        self._remember_side_split()
        save_config(self.cfg)
        return False
