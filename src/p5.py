

    # ------------------------------------------------------------------ menus
    def _build_menubar(self):
        for i, (key, title) in enumerate(self.MENU_KEYS.items()):
            item = Gtk.MenuItem.new_with_mnemonic("_" + title)
            menu = Gtk.Menu()
            item.set_submenu(menu)
            menu.connect("show", lambda m, k=key: self._fill_menu(m, k))
            self.menubar.append(item)
        self.menubar.show_all()
        # global accelerators (registered once; menu items are rebuilt dynamically)
        for accel, cb in (("<Control>t", lambda: self.new_tab(self.tab.path if self.tab.path not in VIRTUAL else HOME)),
                          ("<Control>w", lambda: self.close_tab()), ("<Control>n", lambda: self.app.new_window(self.tab.path)),
                          ("<Control>q", self.close), ("<Control>l", self.edit_address), ("F4", self.edit_address),
                          ("<Alt>d", self.edit_address), ("<Control>e", lambda: self.searchbox.grab_focus()),
                          ("<Control>f", lambda: self.searchbox.grab_focus()), ("F3", lambda: self.searchbox.grab_focus()),
                          ("F5", self.reload_all), ("<Control>r", self.reload_all), ("<Control><Shift>r", self.open_in_rv), ("F1", lambda: show_shortcuts(self)),
                          ("<Alt>Left", self.go_back), ("<Alt>Right", self.go_forward), ("<Alt>Up", self.go_up),
                          ("<Control>c", lambda: self.copy_sel("copy")), ("<Control>x", lambda: self.copy_sel("cut")),
                          ("<Control>v", lambda: self.paste()), ("<Control><Shift>c", self.copy_path),
                          ("<Control>a", lambda: self.tab.select_all()), ("<Control>z", self.undo),
                          ("<Control><Shift>n", self.new_folder), ("<Control>h", lambda: self.toggle_cfg("show_hidden")),
                          ("<Alt>p", lambda: self.toggle_cfg("preview_pane")), ("<Alt><Shift>p", lambda: self.toggle_cfg("details_pane")),
                          ("<Control>o", self.open_with), ("<Control>Tab", lambda: self.notebook.next_page()),
                          ("<Control><Shift>Tab", lambda: self.notebook.prev_page()), ("<Control>Page_Down", lambda: self.notebook.next_page()),
                          ("<Control>Page_Up", lambda: self.notebook.prev_page()), ("F11", self._toggle_fullscreen),
                          ("<Alt>Return", self.properties), ("<Control>j", lambda: self.open_terminal()),
                          ("<Control><Shift>s", self.toggle_split), ("<Control>F2", self.batch_rename)):
            key, mods = Gtk.accelerator_parse(accel)
            self.accel.connect(key, mods, Gtk.AccelFlags.VISIBLE, lambda g, o, k, m, c=cb: (c(), True)[1])
        for i in range(1, 10):
            key, mods = Gtk.accelerator_parse(f"<Control>{i}")
            self.accel.connect(key, mods, Gtk.AccelFlags.VISIBLE, lambda g, o, k, m, n=i: (self.notebook.set_current_page(n - 1), True)[1])

    def _toggle_fullscreen(self):
        st = self.get_window().get_state() if self.get_window() else 0
        if st & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fill_menu(self, menu: Gtk.Menu, key: str):
        for c in menu.get_children():
            menu.remove(c)
        for s in self.menu_spec(key):
            if s == "-":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if isinstance(s, tuple):
                s = {"label": s[0], "cb": s[1] if len(s) > 1 else None, "accel": s[2] if len(s) > 2 else ""}
            sub = build_menu(s["sub"]) if "sub" in s else None
            menu.append(_menu_item(s["label"], s.get("cb"), s.get("accel", ""), s.get("check"), s.get("radio"), s.get("sensitive", True), sub))
        menu.show_all()

    def menu_spec(self, key: str):
        cfg = self.cfg
        t = self.tab
        if t is None:
            return []
        sel = t.selected_entries()
        e = sel[0] if sel else None
        real = t.path not in VIRTUAL
        has = bool(sel) and t.path not in (QUICK, THISPC)
        can_paste = real and (bool(self.clipboard["paths"]) or self._external_clipboard_has_files())
        if key == "file":
            return [("New _tab", lambda: self.new_tab(t.path if real else HOME), "<Control>t"),
                    ("New _window", lambda: self.app.new_window(t.path if real else HOME), "<Control>n"), "-",
                    ("Open _terminal here", lambda: self.open_terminal(), "<Control>j"),
                    ("Connect to _server…", self.connect_to_server), "-",
                    ("_Preferences…", lambda: self.folder_options(0)),
                    ("_Help", lambda: show_shortcuts(self), "F1"), "-",
                    ("_Close tab", lambda: self.close_tab(), "<Control>w"), ("E_xit", self.close, "<Control>q")]
        if key == "home":
            target = e.path if (e and e.is_dir) else t.path
            pinned = target in cfg["pinned"]
            return [{"label": "_Unpin from Quick access" if pinned else "_Pin to Quick access",
                     "cb": (lambda: self.unpin(target)) if pinned else (lambda: self.pin(target)), "sensitive": real}, "-",
                    {"label": "_Copy", "cb": lambda: self.copy_sel("copy"), "accel": "<Control>c", "sensitive": has},
                    {"label": "_Paste", "cb": lambda: self.paste(), "accel": "<Control>v", "sensitive": can_paste},
                    {"label": "Cu_t", "cb": lambda: self.copy_sel("cut"), "accel": "<Control>x", "sensitive": has},
                    {"label": "Copy pat_h", "cb": self.copy_path, "accel": "<Control><Shift>c", "sensitive": has},
                    {"label": "Paste _shortcut", "cb": lambda: self.paste(shortcut=True), "sensitive": can_paste}, "-",
                    {"label": "_Move to…", "cb": lambda: self.move_or_copy_to("move"), "sensitive": has and real},
                    {"label": "Cop_y to…", "cb": lambda: self.move_or_copy_to("copy"), "sensitive": has and real},
                    {"label": "_Delete", "cb": lambda: self.delete(), "accel": "Delete", "sensitive": has},
                    {"label": "Delete permanently", "cb": lambda: self.delete(True), "accel": "<Shift>Delete", "sensitive": has},
                    {"label": "_Rename", "cb": lambda: t.start_rename(), "accel": "F2", "sensitive": has and real}, "-",
                    {"label": "New _folder", "cb": self.new_folder, "accel": "<Control><Shift>n", "sensitive": real},
                    {"label": "New _item", "sub": self._new_item_spec(), "sensitive": real}, "-",
                    ("P_roperties", self.properties, "<Alt>Return"),
                    {"label": "_Open", "cb": lambda: self.activate_entry(e), "accel": "Return", "sensitive": e is not None},
                    {"label": "Open _with…", "cb": self.open_with, "accel": "<Control>o", "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Edit", "cb": self.edit_file, "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Run in terminal", "cb": lambda: self.run_script(e, True), "sensitive": bool(e and not e.is_dir and script_runner(e.path))}, "-",
                    ("Select _all", lambda: t.select_all(), "<Control>a"), ("Select _none", lambda: t.select_none()),
                    ("_Invert selection", lambda: t.invert_selection())]
        if key == "share":
            return [{"label": "_Share", "sub": self.share_spec()},
                    {"label": "_Zip (compress)", "cb": self.zip_selection, "sensitive": has and real},
                    {"label": "E_xtract here", "cb": self.extract_here, "sensitive": self.is_archive(e)},
                    {"label": "Extract _to…", "cb": self.extract_all, "sensitive": self.is_archive(e)},
                    {"label": "_Print", "cb": self.print_files, "sensitive": has}]
        if key == "view":
            lay = t.layout
            layouts, sorts, groups, cols, lgroup = [], [], [], [], []
            for name, k in (("E_xtra large icons", "xlarge"), ("_Large icons", "large"), ("_Medium icons", "medium"),
                            ("_Small icons", "small"), ("L_ist", "list"), ("_Details", "details"), ("_Tiles", "tiles"), ("_Content", "content")):
                layouts.append({"label": name, "cb": lambda k=k: self.set_layout(k), "check": lay == k, "radio": lgroup})
            sgroup = []
            for name, k in (("_Name", "name"), ("Date _modified", "modified"), ("_Type", "type"), ("_Size", "size"),
                            ("Date _created", "created"), ("Date _accessed", "accessed"), ("_Owner", "owner")):
                sorts.append({"label": name, "cb": lambda k=k: (self.sort_by(k) if cfg["sort_key"] != k else None), "check": cfg["sort_key"] == k, "radio": sgroup})
            dgroup = []
            sorts += ["-", {"label": "_Ascending", "cb": lambda: self.set_sort_desc(False), "check": not cfg["sort_desc"], "radio": dgroup},
                      {"label": "_Descending", "cb": lambda: self.set_sort_desc(True), "check": cfg["sort_desc"], "radio": dgroup}]
            ggroup = []
            for name, k in (("(_None)", "none"), ("_Name", "name"), ("Date _modified", "modified"), ("_Type", "type"),
                            ("_Size", "size"), ("Date _created", "created"), ("_Owner", "owner")):
                groups.append({"label": name, "cb": lambda k=k: self.set_group(k), "check": cfg["group_by"] == k, "radio": ggroup})
            for name, k in (("Date _modified", "modified"), ("_Type", "type"), ("_Size", "size"), ("Date _created", "created"),
                            ("Date _accessed", "accessed"), ("_Owner", "owner"), ("_Permissions", "perms")):
                cols.append({"label": name, "cb": lambda k=k: self.toggle_column(k), "check": k in cfg["columns"]})
            return [{"label": "_Navigation pane", "cb": lambda: self.toggle_cfg("nav_pane"), "check": cfg["nav_pane"]},
                    {"label": "_Preview pane", "cb": lambda: self.toggle_cfg("preview_pane"), "accel": "<Alt>p", "check": cfg["preview_pane"]},
                    {"label": "_Details pane", "cb": lambda: self.toggle_cfg("details_pane"), "accel": "<Alt><Shift>p", "check": cfg["details_pane"]},
                    {"label": "Split _view (two folders)", "cb": self.toggle_split, "accel": "<Control><Shift>s", "check": bool(self.page and self.page.split)}, "-",
                    {"label": "_Layout", "sub": layouts}, {"label": "_Sort by", "sub": sorts}, {"label": "_Group by", "sub": groups},
                    {"label": "Add _columns", "sub": cols},
                    ("Size all columns to _fit", lambda: [c.queue_resize() for c in t.tree.get_columns()] and None), "-",
                    {"label": "Item check _boxes", "cb": lambda: self.toggle_cfg("checkboxes"), "check": cfg["checkboxes"]},
                    {"label": "File name _extensions", "cb": lambda: self.toggle_cfg("hide_extensions"), "check": not cfg["hide_extensions"]},
                    {"label": "_Hidden items", "cb": lambda: self.toggle_cfg("show_hidden"), "accel": "<Control>h", "check": cfg["show_hidden"]}, "-",
                    {"label": "Hide selected _items", "cb": self.hide_selected, "sensitive": has and real}]
        if key == "tools":
            multi = len(sel) >= 1 and real
            return [{"label": "_Batch rename…", "cb": self.batch_rename, "accel": "<Control>F2", "sensitive": multi},
                    {"label": "Copy SHA-256 c_hecksum", "cb": self.copy_checksum, "sensitive": bool(e and not e.is_dir)},
                    {"label": "_Run in terminal", "cb": lambda: self.run_script(e, True), "sensitive": bool(e and not e.is_dir and script_runner(e.path))}, "-",
                    ("Empty Recycle _Bin", self.empty_trash), "-",
                    ("_Performance report…", self.performance_report),
                    {"label": "_Update file index now", "cb": self.index_now, "sensitive": bool(cfg.get("index_enabled"))},
                    ("_Optional tools and integrations…", lambda: self.folder_options(3)), "-",
                    ("_Preferences…", lambda: self.folder_options(0))]
        return [("_View help (keyboard shortcuts)", lambda: show_shortcuts(self), "F1"), ("_About Kestrel", lambda: show_about(self))]

    def _new_item_spec(self):
        return [("_Folder", lambda: self.new_item("folder")), ("_Shortcut", lambda: self.new_item("shortcut")),
                ("_Text Document", lambda: self.new_item("text")), ("_Markdown Document", lambda: self.new_item("markdown")),
                ("S_hell Script", lambda: self.new_item("script")), ("_Python Script", lambda: self.new_item("python"))]

    def context_menu(self, t: FolderView, event):
        sel = t.selected_entries()
        e = sel[0] if sel else None
        real = t.path not in VIRTUAL
        can_paste = real and (bool(self.clipboard["paths"]) or self._external_clipboard_has_files())
        spec = []
        if t.path == TRASH:
            if e:
                spec += [("_Restore", self.restore), ("_Delete permanently", lambda: self.delete()), "-", ("P_roperties", self.properties), "-"]
            spec += [("_Empty Recycle Bin", self.empty_trash), ("Re_fresh", self.reload_all)]
        elif e:
            pinned = e.path in self.cfg["pinned"]
            item = bool(e.path) and os.path.exists(e.path)   # the item itself is a real file/folder (also true in Quick access)
            spec += [("_Open", lambda: self.activate_entry(e))]
            if t.path == NETWORK and e.path:
                spec += [("_Disconnect (unmount)", lambda: self.unmount(e.path))]
            if t.path == THISPC and getattr(e, "drive_kind", "") == "removable" and e.path:
                spec += [("_Eject", lambda: self.eject(e.path)), ("_Format…", lambda: self.format_drive(e.path, e.name)), "-"]
            if t.path == THISPC and getattr(e, "volume", None) is not None:
                spec = [("_Mount", lambda: self.mount_volume(e.volume))]
            if e.is_dir and e.path:
                spec += [("Open in new _tab", lambda: self.new_tab(e.path)), ("Open in new _window", lambda: self.app.new_window(e.path)),
                         ("Open in t_erminal", lambda: self.open_terminal(e.path)),
                         ("_Unpin from Quick access" if pinned else "_Pin to Quick access", (lambda: self.unpin(e.path)) if pinned else (lambda: self.pin(e.path)))]
            else:
                spec += [("Open _with…", self.open_with), ("_Edit", self.edit_file)]
                if self.cfg.get("rv_enabled") and any((x.ext or "").lower() in RV_IMAGE_EXTS for x in sel if not x.is_dir):
                    spec += [("Open in _RV", self.open_in_rv, "<Control><Shift>r")]
                    if len(sel) == 1 and rv_sequence_for(e.path or ""):
                        spec += [("Open image se_quence in RV", lambda: self.open_in_rv(sequence=True))]
                if script_runner(e.path):
                    spec += ["-", ("_Run in terminal", lambda: self.run_script(e, True)),
                             ("Run (_no window)", lambda: self.run_script(e, False)), "-"]
                if self.is_archive(e):
                    spec += [("E_xtract here", lambda: self.extract_here(e)), ("Extract _to…", self.extract_all),
                             ("Open with _default app", lambda: self.open_file(e))]
            spec += ["-", {"label": "Se_nd to", "sub": [{"label": "_Compressed (zipped) folder", "cb": self.zip_selection, "sensitive": real},
                                                        {"label": "_Desktop (create shortcut)", "cb": lambda: self.send_to_desktop(e), "sensitive": item},
                                                        ("_Mail recipient", self.share_email)]}, "-",
                     {"label": "Cu_t", "cb": lambda: self.copy_sel("cut"), "sensitive": item}, {"label": "_Copy", "cb": lambda: self.copy_sel("copy"), "sensitive": item},
                     {"label": "_Paste", "cb": lambda: self.paste(), "sensitive": can_paste}, ("Copy as _path", self.copy_path), "-",
                     {"label": "Create _shortcut", "cb": lambda: self.shortcut_here(e), "sensitive": real},
                     {"label": "Unpin from Quick access" if t.path == QUICK and e.is_dir else "_Delete", "cb": lambda: self.delete(), "sensitive": item and t.path != THISPC},
                     {"label": "_Rename" if len(sel) == 1 else "_Batch rename…", "cb": lambda: t.start_rename(), "sensitive": real}, "-", ("P_roperties", self.properties)]
        elif t.path == NETWORK:
            spec += [("_Connect to server…", self.connect_to_server), ("Re_fresh", lambda: (self.rebuild_nav(), self.reload_all())), "-",
                     {"label": "_View", "sub": self.menu_spec("view")[5]["sub"]}]
        else:
            view_spec = self.menu_spec("view")
            spec += [{"label": "_View", "sub": view_spec[5]["sub"]}, {"label": "_Sort by", "sub": view_spec[6]["sub"]},
                     {"label": "_Group by", "sub": view_spec[7]["sub"]}, ("Re_fresh", self.reload_all), "-",
                     {"label": "_Paste", "cb": lambda: self.paste(), "sensitive": can_paste},
                     {"label": "Paste _shortcut", "cb": lambda: self.paste(shortcut=True), "sensitive": can_paste},
                     {"label": "_Undo", "cb": self.undo, "sensitive": bool(self.undo_stack)}, "-",
                     ("Open in _terminal", lambda: self.open_terminal()), {"label": "Ne_w", "sub": self._new_item_spec(), "sensitive": real}, "-",
                     ("P_roperties", lambda: self.show_properties([entry_for_path(t.path)]) if real else self.properties())]
        menu = build_menu(spec)
        menu.attach_to_widget(t.view, None)
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(t.view, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH_WEST, None)

    # ------------------------------------------------------------------ clipboard
    def copy_sel(self, mode: str):
        t = self.tab
        sel = t.selected_entries()
        if not sel or t.path == THISPC:
            return
        paths = [e.path for e in sel if e.path and os.path.exists(e.path)]
        if not paths:
            return
        old = list(self.clipboard["paths"])
        self.clipboard = {"mode": mode, "paths": paths}
        self._export_clipboard(mode, paths)
        for fv in self._tabs():
            fv.refresh_rows(set(old) | set(paths))
        self.flash(f"{len(paths)} item{'s' if len(paths) != 1 else ''} {'copied' if mode == 'copy' else 'cut'}")
        self._update_status()

    def _export_clipboard(self, mode: str, paths: List[str]):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
        targets = [Gtk.TargetEntry.new("x-special/gnome-copied-files", 0, 0), Gtk.TargetEntry.new("text/uri-list", 0, 1),
                   Gtk.TargetEntry.new("UTF8_STRING", 0, 2), Gtk.TargetEntry.new("text/plain", 0, 3)]

        def get(cb_, sd, info, data):
            if info == 0:
                sd.set(Gdk.Atom.intern("x-special/gnome-copied-files", False), 8, (mode + "\n" + "\n".join(uris)).encode())
            elif info == 1:
                sd.set_uris(uris)
            else:
                sd.set_text("\n".join(paths), -1)
        try:
            cb.set_with_data(targets, get, lambda c, d: None, None)
        except Exception:
            cb.set_text("\n".join(paths), -1)

    def _external_clipboard_has_files(self) -> bool:
        try:
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            return cb.wait_is_target_available(Gdk.Atom.intern("x-special/gnome-copied-files", False)) or \
                cb.wait_is_target_available(Gdk.Atom.intern("text/uri-list", False))
        except Exception:
            return False

    def _read_external_clipboard(self) -> Tuple[str, List[str]]:
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        try:
            sd = cb.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
            if sd:
                lines = bytes(sd.get_data()).decode("utf-8", "replace").split("\n")
                mode = "cut" if lines and lines[0].strip() == "cut" else "copy"
                paths = [Gio.File.new_for_uri(u).get_path() for u in lines[1:] if u.strip()]
                return mode, [p for p in paths if p]
            uris = cb.wait_for_uris()
            if uris:
                return "copy", [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
        except Exception:
            pass
        return "", []

    def copy_path(self):
        sel = self.tab.selected_entries()
        if sel:
            set_clipboard_text("\n".join(f'"{e.path}"' if " " in e.path else e.path for e in sel))
            self.flash("Path copied: " + (sel[0].path if len(sel) == 1 else f"{len(sel)} paths"))

    def paste(self, dest: Optional[str] = None, shortcut: bool = False):
        t = self.tab
        dest = dest or t.path
        if dest in VIRTUAL:
            self.flash("Can't paste here.")
            return
        mode, paths = self.clipboard["mode"], list(self.clipboard["paths"])
        if not paths:
            mode, paths = self._read_external_clipboard()
        paths = [p for p in paths if os.path.lexists(p)]
        if not paths:
            self.flash("Nothing to paste.")
            return
        if shortcut:
            for p in paths:
                link = unique_name(os.path.join(dest, os.path.basename(p) + " - Shortcut"))
                try:
                    os.symlink(p, link)
                except OSError as ex:
                    message(self, "Create shortcut", str(ex), Gtk.MessageType.ERROR)
            return
        if self.transfer(paths, dest, mode or "copy") and mode == "cut":
            self.clipboard = {"mode": "", "paths": []}
            self._update_status()

    def ask_drop_action(self) -> Optional[bool]:
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION, text="Drop files")
        d.add_buttons("_Move here", 1, "_Copy here", 2, "_Cancel", Gtk.ResponseType.CANCEL)
        r = d.run()
        d.destroy()
        return {1: True, 2: False}.get(r)

    # ------------------------------------------------------------------ file operations
    def transfer(self, paths: List[str], dest: str, kind: str) -> bool:
        paths = [os.path.abspath(p) for p in paths if p]
        if kind == "move" and all(os.path.dirname(p) == dest for p in paths):
            self.flash("Source and destination are the same.")
            return False
        for p in paths:
            if os.path.isdir(p) and (dest == p or dest.startswith(p.rstrip("/") + "/")):
                message(self, "Interrupted Action", "The destination folder is a subfolder of the source folder.", Gtk.MessageType.ERROR)
                return False
        plan = self._resolve_conflicts(paths, dest, kind)
        if plan is None:
            return False
        self.run_op(kind, paths, dest, plan)
        return True

    def _resolve_conflicts(self, paths: List[str], dest: str, mode: str) -> Optional[Dict[str, str]]:
        plan: Dict[str, str] = {}
        apply_all: Optional[str] = None
        for p in paths:
            d = os.path.join(dest, os.path.basename(p.rstrip("/")))
            if not os.path.lexists(d):
                continue
            if mode == "copy" and os.path.abspath(p) == os.path.abspath(d):
                plan[p] = "keep"
                continue
            if apply_all:
                plan[p] = apply_all
                continue
            r, all_ = conflict_dialog(self, p, d)
            if r is None:
                return None
            plan[p] = r
            if all_:
                apply_all = r
        return plan

    def run_op(self, kind: str, paths: List[str], dest: Optional[str], plan: Optional[Dict[str, str]] = None):
        verb = {"copy": "Copying", "move": "Moving", "delete": "Deleting", "trash": "Recycling"}.get(kind, "Working on")
        title = f"{verb} {len(paths)} item{'s' if len(paths) != 1 else ''}" + (f" to {self.display_path(dest)}" if dest else "")
        op = FileOp(kind, paths, dest, plan or {}, self._op_done)
        self._start_op(op, title)

    def _start_op(self, op, title: str):
        op.title = title
        op.dialog = None
        op.start()
        GLib.timeout_add(120, self._maybe_show_progress, op)

    def _maybe_show_progress(self, op):
        if not op.finished and op.dialog is None:
            op.dialog = ProgressDialog(self, op, op.title)
        return False

    def _op_done(self, op: FileOp):
        if op.dialog is not None:
            op.dialog.destroy()
            op.dialog = None
        if op.errors:
            message(self, APP_NAME, "Some items could not be processed:\n" + "\n".join(op.errors[:10]), Gtk.MessageType.WARNING)
        if op.results and op.kind in ("move", "trash", "copy"):
            self.undo_stack.append((op.kind, op.results))
        if op.kind == "extract" and op.results and not op.cancel.is_cancelled():
            self.navigate(op.results[0][1])
        self.reload_all()
        self.flash(f"{op.title} — {'cancelled' if op.cancel.is_cancelled() else 'done'}")
        return False

    def delete(self, permanent: bool = False):
        t = self.tab
        sel = t.selected_entries()
        if not sel:
            return
        if t.path == QUICK:
            for e in sel:
                self.unpin(e.path)
            return
        if t.path == THISPC:
            return
        desc = f"'{sel[0].name}'" if len(sel) == 1 else f"these {len(sel)} items"
        if t.path == TRASH:
            if ask_yes_no(self, "Delete File", f"Are you sure you want to permanently delete {desc}?", True):
                for e in sel:
                    try:
                        self._delete_trash_item(e)
                    except GLib.Error as ex:
                        message(self, "Delete", ex.message, Gtk.MessageType.ERROR)
                self.reload_all()
            return
        paths = [e.path for e in sel if e.path]
        if permanent:
            if ask_yes_no(self, "Delete File", f"Are you sure you want to permanently delete {desc}?", True):
                self.run_op("delete", paths, None)
            return
        if self.cfg["confirm_delete"] and not ask_yes_no(self, "Delete File", f"Are you sure you want to move {desc} to the Recycle Bin?"):
            return
        self.run_op("trash", paths, None)

    @staticmethod
    def _delete_trash_item(e: Entry):
        f = e.gfile
        if e.is_dir:
            # GIO trash backend deletes whole directories only via the real files dir
            real = e.path
            if real and os.path.isdir(real):
                shutil.rmtree(real)
                info_file = os.path.join(os.path.dirname(os.path.dirname(real)), "info", e.name + ".trashinfo")
                try:
                    os.remove(info_file)
                except OSError:
                    pass
                return
        f.delete(None)

    def restore(self):
        t = self.tab
        if t.path != TRASH:
            return
        for e in t.selected_entries():
            if not e.orig:
                message(self, "Restore", f"No original location recorded for {e.name}.", Gtk.MessageType.WARNING)
                continue
            try:
                os.makedirs(os.path.dirname(e.orig), exist_ok=True)
                dest = unique_name(e.orig)
                e.gfile.move(Gio.File.new_for_path(dest), Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
                self.flash(f"Restored to {dest}")
            except GLib.Error as ex:
                message(self, "Restore", ex.message, Gtk.MessageType.ERROR)
        self.reload_all()

    def empty_trash(self):
        try:
            items = list_folder(TRASH, True)
        except GLib.Error:
            items = []
        if not items:
            self.flash("The Recycle Bin is empty.")
            return
        if ask_yes_no(self, "Delete Multiple Items", f"Are you sure you want to permanently delete these {len(items)} items?", True):
            for e in items:
                try:
                    self._delete_trash_item(e)
                except GLib.Error:
                    pass
            self.reload_all()

    def do_rename(self, old: str, new: str) -> bool:
        base = os.path.basename(new)
        if not base or "/" in base:
            message(self, "Rename", "A file name can't contain '/' or be empty.", Gtk.MessageType.ERROR)
            return False
        if os.path.lexists(new):
            message(self, "Rename File", "There is already a file with the same name in this location.", Gtk.MessageType.ERROR)
            return False
        try:
            os.rename(old, new)
        except OSError as ex:
            message(self, "Rename", str(ex), Gtk.MessageType.ERROR)
            return False
        self.undo_stack.append(("rename", (old, new)))
        t = self.tab
        t.load()
        GLib.timeout_add(250, lambda: t.select_names([base]) or False)
        return True

    def batch_rename(self):
        t = self.tab
        sel = [e for e in t.selected_entries() if e.path]
        if not sel or t.path in VIRTUAL:
            self.flash("Select the files to rename first.")
            return

        def done(plan):
            renamed, errors = apply_renames(plan)
            if renamed:
                self.undo_stack.append(("move", renamed))      # undo = move each back
                self.flash(f"Renamed {len(renamed)} item{'s' if len(renamed) != 1 else ''}")
            if errors:
                message(self, "Batch Rename", "Some items could not be renamed:\n" + "\n".join(errors[:10]), Gtk.MessageType.WARNING)
            t.load()
            GLib.timeout_add(300, lambda: t.select_names([os.path.basename(n) for _, n in renamed]) or False)
        BatchRenameDialog(self, sel, done)

    def preview_diagnostics(self):
        """Which converters / VFX Python modules Kestrel can find for previews."""
        d = Gtk.Dialog(title="Dependencies — status and diagnostics", transient_for=self, modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(720, 420)
        tv = Gtk.TextView(editable=False, monospace=True)
        tv.get_buffer().set_text("Checking… (mayapy can take a few seconds to start)")
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        d.get_content_area().pack_start(sw, True, True, 0)
        d.show_all()
        threading.Thread(target=lambda: GLib.idle_add(lambda: tv.get_buffer().set_text(preview_tool_report()) or False), daemon=True).start()
        d.run()
        d.destroy()

    def open_in_rv(self, sequence: bool = False):
        """Open the selected images / movies in OpenRV (Ctrl+Shift+R). With sequence=True, or when the single
        selected file is one frame of a numbered sequence and the others are not selected, RV gets the
        name.#.ext spec so it loads the whole sequence."""
        if not self.cfg.get("rv_enabled"):
            self.flash("OpenRV integration is off (Preferences → Optional)")
            return
        rv = find_rv(self.cfg.get("rv_path", ""))
        if not rv:
            self.flash("OpenRV not found (Preferences → Optional)")
            return
        sel = [x for x in self.tab.selected_entries() if not x.is_dir and x.path and (x.ext or "").lower() in RV_IMAGE_EXTS]
        if not sel:
            self.flash("Select images or movies to open in RV")
            return
        args = [x.path for x in sel]
        if sequence and len(sel) == 1:
            seq = rv_sequence_for(sel[0].path)
            if seq:
                args = [seq]
        cwd = os.path.dirname(args[0]) if os.path.isdir(os.path.dirname(args[0])) else HOME
        if run_detached([rv] + args, cwd):
            self.flash(f"Opening {len(args)} item{'s' if len(args) != 1 else ''} in RV")
        else:
            self.flash("Could not start RV")

    def index_now(self, full: bool = False):
        if not INDEX.loaded:
            threading.Thread(target=lambda: (INDEX.load(), GLib.idle_add(lambda: (self.index_now(full), False)[1])), daemon=True).start()
            return
        INDEX.on_status = lambda: self.flash(self.index_status_line())
        INDEX.crawl(self.cfg.get("index_roots", [HOME]), self.cfg.get("index_network", False), self.cfg.get("index_hidden", False), full)
        self.flash("Updating the file index in the background…")

    def index_status_line(self) -> str:
        st = INDEX.stats()
        if st["crawling"]:
            return f"Indexing… {INDEX.progress['dirs']:,} folders, {INDEX.progress['files']:,} items — {INDEX.progress['current']}"
        when = _dt.datetime.fromtimestamp(st["finished"]).strftime("%b %d %H:%M") if st["finished"] else "never"
        return f"Index: {st['files']:,} items in {st['dirs']:,} folders · {fmt_size(st['size'])} encrypted on disk · updated {when} · key in {st['key']}"

    def performance_report(self):
        """Tools → Performance report…: the stall watchdog's findings (always on), plus the cProfile summary
        when started with --profile. Refreshes while open."""
        d = Gtk.Dialog(title="Performance report", transient_for=self, modal=False)
        d.add_button("_Save to file…", 1)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(900, 600)
        tv = Gtk.TextView(editable=False, monospace=True)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        d.get_content_area().pack_start(sw, True, True, 0)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup("<small>Stalls are moments the interface thread was blocked for more than 120 ms; the stack shows what it was doing. "
                        "Run <tt>python3 kestrel.py --profile</tt> for a full cProfile of everything on the interface thread, "
                        "and <tt>python3 kestrel.py --selftest</tt> for timed unit/benchmark tests.</small>")
        d.get_content_area().pack_start(hint, False, False, 6)

        def refresh():
            if not d.get_visible():
                return False
            text = "==== recent navigations ====\n" + nav_report() + "\n\n==== interface stalls ====\n" + WATCHDOG.report()
            if PROFILE is not None:
                text += "\n\n==== cProfile (--profile) ====\n" + profiling_report(25)
            tv.get_buffer().set_text(text)
            return True
        refresh()
        GLib.timeout_add(2000, refresh)
        d.show_all()
        while True:
            r = d.run()
            if r == 1:
                out = os.path.join(GLib.get_user_cache_dir(), "kestrel", f"performance-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w") as f:
                    f.write(WATCHDOG.report() + ("\n\n" + profiling_report(60) if PROFILE is not None else ""))
                self.flash(f"Saved {out}")
                continue
            break
        d.destroy()

    def install_deps(self):
        """Preferences → Optional: tick the missing optional tools and run their install
        commands in a terminal (sudo prompts there, nothing runs silently)."""
        d = Gtk.Dialog(title="Install missing dependencies", transient_for=self, modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        copy_btn = d.add_button("_Copy commands", 1)
        run_btn = d.add_button("_Install selected in terminal", Gtk.ResponseType.OK)
        run_btn.get_style_context().add_class("suggested-action")
        d.set_default_size(680, 460)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Checking what's installed…", xalign=0), False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(grid)
        box.pack_start(sw, True, True, 0)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("<small>Commands run in a terminal window so you can see them and enter your sudo password. "
                        "EPEL is enabled automatically when needed. Restart Kestrel afterwards.</small>")
        box.pack_start(note, False, False, 0)
        checks: List[Tuple[Gtk.CheckButton, dict]] = []
        d.show_all()
        run_btn.set_sensitive(False)
        copy_btn.set_sensitive(False)

        def fill(deps):
            box.get_children()[0].set_text(f"{sum(1 for x in deps if x['found'])} of {len(deps)} optional tools found.")
            for i, dep in enumerate(deps):
                cb = Gtk.CheckButton()
                cb.set_active(dep["found"] is None)
                cb.set_sensitive(dep["found"] is None)
                grid.attach(cb, 0, i, 1, 1)
                name = Gtk.Label(xalign=0)
                name.set_markup(f"<b>{GLib.markup_escape_text(dep['name'])}</b>")
                grid.attach(name, 1, i, 1, 1)
                st = Gtk.Label(xalign=0)
                st.set_markup(f"<span foreground='#2e8b57'>installed</span>" if dep["found"] else "<span foreground='#c0392b'>missing</span>")
                grid.attach(st, 2, i, 1, 1)
                g = Gtk.Label(label=dep["gives"], xalign=0, wrap=True, hexpand=True)
                g.get_style_context().add_class("dim-label")
                grid.attach(g, 3, i, 1, 1)
                name.set_tooltip_text(dep["cmd"])
                checks.append((cb, dep))
            grid.show_all()
            run_btn.set_sensitive(True)
            copy_btn.set_sensitive(True)
            return False
        threading.Thread(target=lambda: GLib.idle_add(fill, optional_deps()), daemon=True).start()
        while True:
            resp = d.run()
            chosen = [dep for cb, dep in checks if cb.get_active() and dep["found"] is None]
            if resp == 1:
                set_clipboard_text("\n".join(dep["cmd"] for dep in chosen) or "# nothing selected")
                self.flash("Install commands copied")
                continue
            if resp == Gtk.ResponseType.OK and chosen:
                script = os.path.join(GLib.get_user_cache_dir(), "kestrel", "install-deps.sh")
                os.makedirs(os.path.dirname(script), exist_ok=True)
                with open(script, "w") as f:
                    f.write(install_script(chosen))
                os.chmod(script, 0o755)
                if not spawn_terminal(HOME, ["bash", script]):
                    message(self, "No terminal found", f"Run this script yourself:\n{script}", Gtk.MessageType.WARNING)
            break
        d.destroy()

    def copy_checksum(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        self.flash(f"Computing SHA-256 of {e.name}…")

        def work():
            import hashlib
            h = hashlib.sha256()
            try:
                with open(e.path, "rb") as f:
                    for buf in iter(lambda: f.read(1 << 20), b""):
                        h.update(buf)
                digest = h.hexdigest()
            except OSError as ex:
                GLib.idle_add(lambda: message(self, "Checksum", str(ex), Gtk.MessageType.ERROR) and False)
                return
            GLib.idle_add(lambda: (set_clipboard_text(digest), self.flash(f"SHA-256 copied: {digest[:16]}…")) and False)
        threading.Thread(target=work, daemon=True).start()

    def new_folder(self):
        self.new_item("folder")

    def new_item(self, kind: str):
        t = self.tab
        if t.path in VIRTUAL:
            self.flash("Can't create items here.")
            return
        if kind == "shortcut":
            target = ask_text(self, "Create Shortcut", "Type the location of the item:")
            if not target:
                return
            target = os.path.expanduser(target)
            name = ask_text(self, "Create Shortcut", "Type a name for this shortcut:", os.path.basename(target.rstrip("/")) + " - Shortcut")
            if name:
                try:
                    os.symlink(target, os.path.join(t.path, name))
                except OSError as ex:
                    message(self, "Create Shortcut", str(ex), Gtk.MessageType.ERROR)
            return
        default = {"folder": "New folder", "text": "New Text Document.txt", "markdown": "New Document.md",
                   "script": "New Script.sh", "python": "New Script.py"}.get(kind, "New file")
        path = os.path.join(t.path, default)
        n = 2
        while os.path.lexists(path):
            stem, ext = (default, "") if kind == "folder" else os.path.splitext(default)
            path = os.path.join(t.path, f"{stem} ({n}){ext}")
            n += 1
        try:
            if kind == "folder":
                os.mkdir(path)
            else:
                with open(path, "w") as f:
                    f.write({"script": "#!/bin/bash\n", "python": "#!/usr/bin/env python3\n"}.get(kind, ""))
                if kind in ("script", "python"):
                    os.chmod(path, 0o755)
        except OSError as ex:
            message(self, "New item", str(ex), Gtk.MessageType.ERROR)
            return
        self.undo_stack.append(("new", path))
        t.load()

        def then():
            t.select_names([os.path.basename(path)])
            t.start_rename()
            return False
        GLib.timeout_add(300, then)

    def undo(self):
        if not self.undo_stack:
            self.flash("Nothing to undo.")
            return
        kind, data = self.undo_stack.pop()
        try:
            if kind == "rename":
                old, new = data
                if os.path.lexists(new) and not os.path.lexists(old):
                    os.rename(new, old)
                    self.flash(f"Undo rename: {os.path.basename(old)}")
            elif kind == "move":
                n = 0
                for src, dst in data:
                    if os.path.lexists(dst) and not os.path.lexists(src):
                        os.makedirs(os.path.dirname(src), exist_ok=True)
                        shutil.move(dst, src)
                        n += 1
                self.flash(f"Undo move: {n} item(s) moved back")
            elif kind == "copy":
                if ask_yes_no(self, "Undo Copy", f"Delete the {len(data)} copied item(s)?", True):
                    for _, dst in data:
                        if os.path.lexists(dst):
                            shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
                    self.flash("Undo copy")
            elif kind == "trash":
                n = 0
                trashed = list_folder(TRASH, True)
                for src, name in data:
                    for e in trashed:
                        if e.orig == src and not os.path.lexists(src):
                            e.gfile.move(Gio.File.new_for_path(src), Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
                            n += 1
                            break
                self.flash(f"Undo delete: {n} item(s) restored")
            elif kind == "new":
                if os.path.lexists(data):
                    shutil.rmtree(data) if os.path.isdir(data) and not os.path.islink(data) else os.remove(data)
                    self.flash("Undo new item")
        except (OSError, GLib.Error) as ex:
            message(self, "Undo", str(ex), Gtk.MessageType.ERROR)
        self.reload_all()

    def move_or_copy_to(self, kind: str):
        sel = self.tab.selected_entries()
        if not sel or self.tab.path in VIRTUAL:
            return
        dest = pick_folder(self, f"{'Move' if kind == 'move' else 'Copy'} Items", self.tab.path)
        if dest:
            self.transfer([e.path for e in sel], dest, kind)

    def is_archive(self, e: Optional[Entry]) -> bool:
        return bool(e) and not e.is_dir and e.name.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz") + SEVENZIP_EXTS)

    def zip_selection(self):
        t = self.tab
        sel = t.selected_entries()
        if not sel or t.path in VIRTUAL:
            return
        d = Gtk.Dialog(title="Compress", transient_for=self, modal=True)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Create", Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_border_width(12)
        box.set_spacing(6)
        base = sel[0].name if len(sel) == 1 else self.display_path(t.path)
        stem = base if sel[0].is_dir or len(sel) > 1 else os.path.splitext(base)[0]
        box.pack_start(Gtk.Label(label="Archive name:", xalign=0), False, False, 0)
        name = Gtk.Entry(text=stem, activates_default=True)
        box.pack_start(name, False, False, 0)
        combo = Gtk.ComboBoxText()
        fmts = [("zip (Compressed folder)", "zip"), ("tar.gz", "gztar"), ("tar.xz", "xztar"), ("tar", "tar")]
        if sevenzip():
            fmts.insert(1, ("7z (7-Zip, best compression)", "7z"))
        for f, _ in fmts:
            combo.append_text(f)
        combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        r = d.run()
        fmt = fmts[combo.get_active()][1]
        stem = name.get_text().strip() or stem
        d.destroy()
        if r != Gtk.ResponseType.OK:
            return
        ext = {"zip": ".zip", "7z": ".7z", "gztar": ".tar.gz", "xztar": ".tar.xz", "tar": ".tar"}[fmt]
        out = unique_name(os.path.join(t.path, stem + ext))
        op = ArchiveOp("zip", [e.path for e in sel], out, fmt, self._op_done)
        self._start_op(op, f"Compressing {len(sel)} item{'s' if len(sel) != 1 else ''} to {os.path.basename(out)}")

    @staticmethod
    def archive_stem(name: str) -> str:
        low = name.lower()
        for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar"):
            if low.endswith(suf):
                return name[:-len(suf)]
        return os.path.splitext(name)[0]

    def extract_here(self, e: Optional[Entry] = None):
        """Extract into a new folder named after the archive, next to it, with the progress window, then open
        that folder. No questions asked (unique name on collision) — this is what double-click does."""
        e = e or self.tab.current_entry()
        if not self.is_archive(e) or not e.path:
            return
        dest = unique_name(os.path.join(os.path.dirname(e.path), self.archive_stem(e.name)))
        op = ArchiveOp("extract", [e.path], dest, "", self._op_done)
        self._start_op(op, f"Extracting {e.name}")

    def extract_all(self):
        t = self.tab
        e = t.current_entry()
        if not self.is_archive(e):
            return
        default = os.path.join(t.path, self.archive_stem(e.name))
        dest = ask_text(self, "Extract Compressed Folder", "Files will be extracted to this folder:", default)
        if not dest:
            return
        op = ArchiveOp("extract", [e.path], os.path.expanduser(dest), "", self._op_done)
        self._start_op(op, f"Extracting {e.name} to {self.display_path(dest)}")

    def pin(self, path: str):
        if path in VIRTUAL or not os.path.isdir(path):
            return
        if path not in self.cfg["pinned"]:
            self.cfg["pinned"].append(path)
        self._fill_quick(self.nav_quick)
        self.nav_tree.expand_row(self.nav_store.get_path(self.nav_quick), False)
        self.flash(f"Pinned {self.display_path(path)} to Quick access")
        if self.tab.path == QUICK:
            self.tab.load()

    def unpin(self, path: str):
        if path in self.cfg["pinned"]:
            self.cfg["pinned"].remove(path)
            self._fill_quick(self.nav_quick)
            self.flash("Unpinned from Quick access")
            if self.tab.path == QUICK:
                self.tab.load()

    def hide_selected(self):
        t = self.tab
        if t.path in VIRTUAL:
            return
        for e in t.selected_entries():
            new = os.path.join(os.path.dirname(e.path), e.name[1:] if e.name.startswith(".") else "." + e.name)
            if not os.path.lexists(new):
                try:
                    os.rename(e.path, new)
                except OSError as ex:
                    message(self, "Hide", str(ex), Gtk.MessageType.ERROR)
        t.load()

    def shortcut_here(self, e: Entry):
        link = unique_name(os.path.join(os.path.dirname(e.path), shortcut_name(e)))
        try:
            os.symlink(e.path, link)
        except OSError as ex:
            message(self, "Create shortcut", str(ex), Gtk.MessageType.ERROR)

    def send_to_desktop(self, e: Entry):
        desk = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP) or os.path.join(HOME, "Desktop")
        os.makedirs(desk, exist_ok=True)
        try:
            os.symlink(e.path, unique_name(os.path.join(desk, shortcut_name(e))))
            self.flash("Shortcut created on Desktop")
        except OSError as ex:
            message(self, "Send to Desktop", str(ex), Gtk.MessageType.ERROR)

    def share_email(self):
        sel = [e for e in self.tab.selected_entries() if not e.is_dir]
        if not sel:
            self.flash("Select one or more files to share.")
            return
        if shutil.which("xdg-email"):
            argv = ["xdg-email"]
            for e in sel:
                argv += ["--attach", e.path]
            subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            message(self, "Share", "No email client integration found (xdg-email).", Gtk.MessageType.WARNING)

    def share_spec(self):
        t = self.tab
        sel = t.selected_entries() if t else []
        has = bool(sel) and t.path not in VIRTUAL
        files_only = bool(sel) and all(not e.is_dir for e in sel)
        return [{"label": "Copy _path", "cb": self.copy_path, "sensitive": bool(sel)},
                {"label": "Copy _link (file:// URI)", "cb": lambda: set_clipboard_text("\n".join(e.gfile.get_uri() for e in sel)), "sensitive": bool(sel)},
                {"label": "Copy _to…", "cb": lambda: self.move_or_copy_to("copy"), "sensitive": has},
                {"label": "Compress to _zip…", "cb": self.zip_selection, "sensitive": has}, "-",
                {"label": "_Email as attachment…", "cb": self.share_email, "sensitive": files_only and bool(shutil.which("xdg-email"))},
                {"label": "_Print…", "cb": self.print_files, "sensitive": files_only},
                {"label": "Send to _Desktop (shortcut)", "cb": lambda: self.send_to_desktop(sel[0]), "sensitive": len(sel) == 1 and bool(sel[0].path) and os.path.exists(sel[0].path)}]

    def print_files(self):
        """Explorer-style Print: text and images go through the GTK print dialog; anything else is
        opened in its default application (which has its own Print)."""
        sel = [e for e in self.tab.selected_entries() if not e.is_dir]
        if not sel:
            self.flash("Select a file to print.")
            return
        for e in sel:
            ct = e.content_type or ""
            if ct.startswith("image/"):
                self._print_image(e)
            elif ct.startswith("text/") or ct in ("application/json", "application/xml", "application/x-shellscript", "application/javascript", "application/x-yaml"):
                self._print_text(e)
            elif ct == "application/pdf" and shutil.which("lp"):
                subprocess.Popen(["lp", e.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.flash(f"Sent {e.name} to the default printer")
            else:
                launch_default(e.path, self)
                self.flash(f"Opened {e.name} in its application")

    def _run_print(self, name: str, begin, draw):
        op = Gtk.PrintOperation()
        op.set_job_name(name)
        op.set_embed_page_setup(True)
        op.connect("begin-print", begin)
        op.connect("draw-page", draw)
        try:
            res = op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self)
        except GLib.Error as ex:
            message(self, "Print", ex.message, Gtk.MessageType.ERROR)
            return
        if res == Gtk.PrintOperationResult.ERROR:
            message(self, "Print", "Printing failed.", Gtk.MessageType.ERROR)

    def _print_text(self, e: Entry):
        try:
            with open(e.path, "rb") as f:
                text = f.read(4 << 20).decode("utf-8", "replace").expandtabs(4)
        except OSError as ex:
            message(self, "Print", str(ex), Gtk.MessageType.ERROR)
            return
        state = {"pages": [], "lines": text.splitlines() or [""], "lph": 60}

        def begin(op, ctx):
            layout = ctx.create_pango_layout()
            layout.set_font_description(Pango.FontDescription("Monospace 9"))
            layout.set_text("X", -1)
            _, lh = layout.get_pixel_size()
            lph = max(10, int((ctx.get_height() - 40) // (lh * 1.15)))
            state["lph"] = lph
            n = max(1, (len(state["lines"]) + lph - 1) // lph)
            op.set_n_pages(n)

        def draw(op, ctx, page):
            cr = ctx.get_cairo_context()
            layout = ctx.create_pango_layout()
            layout.set_font_description(Pango.FontDescription("Monospace 9"))
            layout.set_width(int(ctx.get_width() * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            lph = state["lph"]
            header = ctx.create_pango_layout()
            header.set_font_description(Pango.FontDescription("Sans 8"))
            header.set_text(f"{e.name}   —   page {page + 1}", -1)
            cr.move_to(0, 0)
            PangoCairo.show_layout(cr, header)
            layout.set_text("\n".join(state["lines"][page * lph:(page + 1) * lph]), -1)
            cr.move_to(0, 24)
            PangoCairo.show_layout(cr, layout)
        self._run_print(e.name, begin, draw)

    def _print_image(self, e: Entry):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(e.path)
            pb = pb.apply_embedded_orientation() or pb
        except GLib.Error as ex:
            message(self, "Print", ex.message, Gtk.MessageType.ERROR)
            return

        def begin(op, ctx):
            op.set_n_pages(1)

        def draw(op, ctx, page):
            cr = ctx.get_cairo_context()
            w, h = ctx.get_width(), ctx.get_height()
            scale = min(w / pb.get_width(), h / pb.get_height(), 1e9)
            cr.translate((w - pb.get_width() * scale) / 2, (h - pb.get_height() * scale) / 2)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
        self._run_print(e.name, begin, draw)

    def run_script(self, e: Entry, in_terminal: bool):
        cmd = script_runner(e.path)
        if not cmd:
            return
        cwd = os.path.dirname(e.path)
        if in_terminal:
            if not spawn_terminal(cwd, cmd):
                message(self, "Run", "No terminal emulator was found. Set $TERMINAL or install one.", Gtk.MessageType.WARNING)
        elif run_detached(cmd, cwd):
            self.flash(f"Started {e.name}")
        else:
            message(self, "Run", f"Couldn't start {e.name}.", Gtk.MessageType.ERROR)

    def edit_file(self):
        e = self.tab.current_entry()
        if not e or e.is_dir:
            return
        app = Gio.AppInfo.get_default_for_type("text/plain", False)
        try:
            if app:
                app.launch([e.gfile], self.get_display().get_app_launch_context())
            else:
                launch_default(e.path, self)
        except GLib.Error as ex:
            message(self, "Edit", ex.message, Gtk.MessageType.ERROR)

    # ------------------------------------------------------------------ dialogs
    def properties(self):
        t = self.tab
        sel = t.selected_entries()
        if not sel:
            if t.path in VIRTUAL:
                if t.path == TRASH:
                    try:
                        n = len(list_folder(TRASH, True))
                    except GLib.Error:
                        n = 0
                    message(self, "Recycle Bin Properties", f"Items: {n}")
                return
            sel = [entry_for_path(t.path)]
        self.show_properties(sel)

    def show_properties(self, sel: List[Entry]):
        sel = [e for e in sel if e.path]
        if not sel:
            return
        if len(sel) > 1:
            MultiPropertiesDialog(self, sel)
        else:
            PropertiesDialog(self, sel[0], self.cfg, self.do_rename)

    def folder_options(self, page: int = 0):
        OptionsDialog(self, self.cfg, self.apply_cfg, page)


# --------------------------------------------------------------------------- application
CSS = b"""
/* ---- Windows 11 "Fluent" flavour on top of the current GTK theme ---- */
menubar { padding: 0 4px; }
menubar > menuitem { padding: 4px 8px; border-radius: 4px; }
menubar > menuitem:hover { background: alpha(@theme_fg_color, 0.08); color: @theme_fg_color; box-shadow: none; }
menu menuitem:hover { background: alpha(@theme_selected_bg_color, 0.85); border-radius: 4px; }
.toolbar-row button:hover, .statusbar button:hover, notebook > header button:hover { background: alpha(@theme_fg_color, 0.08); }
.sidepane button:hover { background: alpha(@theme_fg_color, 0.08); }
.toolbar-row button { min-width: 28px; min-height: 28px; padding: 2px; border-radius: 6px; }
.pathbar { background: alpha(@theme_fg_color, 0.05); border: 1px solid alpha(@theme_fg_color, 0.14);
           border-radius: 6px; padding: 1px 4px; min-height: 30px; }
.pathbar > border { border-style: none; }        /* GtkFrame draws its own frame - keep only ours */
.pathbar entry { border: none; background: transparent; box-shadow: none; min-height: 26px; }
.toolbar-row entry.search, .toolbar-row searchentry { border: 1px solid alpha(@theme_fg_color, 0.14); border-radius: 6px;
           background: alpha(@theme_fg_color, 0.05); box-shadow: none; }
.toolbar-row entry.search:focus, .toolbar-row searchentry:focus { border-color: @theme_selected_bg_color; }
.pathbar:hover { background: alpha(@theme_fg_color, 0.08); }
.pathbar button { padding: 1px 8px; min-height: 24px; border-radius: 4px; border: none; background: none; box-shadow: none; }
.pathbar button:hover { background: alpha(@theme_fg_color, 0.1); }
.pathbar label.dim-label { padding: 0 1px; }
entry.search { border-radius: 6px; min-height: 30px; }
.commandbar { padding: 2px 0; }
.commandbar button.cmd { padding: 4px 10px; min-height: 30px; border-radius: 6px; }
.commandbar button.cmd:hover { background: alpha(@theme_fg_color, 0.08); }
.commandbar button.cmd label { font-size: 0.95em; }
.navpane treeview { padding: 2px 0; }
.navpane treeview:selected { border-radius: 4px; }
.navpane { background: alpha(@theme_fg_color, 0.025); }
paned > separator { min-width: 1px; min-height: 1px; background: alpha(@theme_fg_color, 0.12); background-clip: content-box;
                    margin: 0; border: none; box-shadow: none; }
treeview.view { padding: 1px 0; }
treeview.view header button { padding: 4px 8px; background: none; border-style: solid; border-color: alpha(@theme_fg_color, 0.12);
                               border-width: 0 1px 1px 0; border-radius: 0; }
treeview.view header button:last-child { border-right-width: 0; }
treeview.view header button:hover { background: alpha(@theme_fg_color, 0.06); }
treeview.view header button label { font-weight: 600; color: alpha(@theme_fg_color, 0.75); }
treeview.view:selected { background: alpha(@theme_selected_bg_color, 0.85); border-radius: 3px; }
iconview:selected { background: alpha(@theme_selected_bg_color, 0.8); border-radius: 6px; }
notebook > header { background: alpha(@theme_fg_color, 0.04); border: none; padding: 4px 4px 0 4px; }
notebook > header tab { padding: 4px 10px; margin: 0 2px; border: none; border-radius: 6px 6px 0 0; min-width: 120px; }
notebook > header tab:checked { background: @theme_base_color; }
notebook > header tab:hover:not(:checked) { background: alpha(@theme_fg_color, 0.06); }
notebook > header tab button.tab-close { min-width: 18px; min-height: 18px; padding: 0; border-radius: 4px; }
.pane-header { padding: 3px 4px; background: alpha(@theme_fg_color, 0.04); border-bottom: 2px solid alpha(@theme_fg_color, 0.1); }
.pane-header.active { background: alpha(@theme_selected_bg_color, 0.22); border-bottom: 2px solid @theme_selected_bg_color; }
.pane-header.active label { font-weight: bold; }
/* the inactive side is dimmed (darker in dark themes, greyer in light ones); the active side keeps the normal base */
.inactive-pane, .inactive-pane treeview.view, .inactive-pane .icongrid { background: shade(@theme_base_color, 0.92); }
.inactive-pane .pane-header label { color: alpha(@theme_fg_color, 0.6); }
.pane-header label { font-size: 0.9em; }
.sidepane { background: alpha(@theme_fg_color, 0.025); }
.sidepane textview, .sidepane textview text { background: alpha(@theme_fg_color, 0.03); }
.edit-toolbar button { padding: 2px 8px; min-height: 24px; }
.edit-badge { font-size: 0.75em; font-weight: bold; letter-spacing: 1px; padding: 1px 6px; border-radius: 3px;
              color: alpha(@theme_fg_color, 0.55); background: alpha(@theme_fg_color, 0.07); }
.edit-badge.badge-editing { color: #ffffff; background: #2b8a3e; }
.edit-badge.badge-dirty { color: #ffffff; background: #d9480f; }
textview.editing, textview.editing text { background: alpha(#2b8a3e, 0.06); }
.sidepane textview.editing { border: 2px solid #2b8a3e; border-radius: 4px; }
.sidepane textview.readonly { border: 2px solid transparent; }
.edit-toolbar entry { min-height: 24px; }
.statusbar { background: alpha(@theme_fg_color, 0.04); border-top: 1px solid alpha(@theme_fg_color, 0.08); }
.statusbar label { font-size: 0.92em; }
.statusbar button { padding: 0 4px; min-height: 22px; min-width: 22px; }
"""


class ExplorerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)
        self.cfg = load_config()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        WATCHDOG.start(os.path.join(GLib.get_user_cache_dir(), "kestrel", "stalls.log") if PROFILE is not None else None)
        GLib.timeout_add(400, lambda: (threading.Thread(target=warm_workers, daemon=True).start(), False)[1])
        GLib.timeout_add(1500, self._index_startup)

    def _index_startup(self):
        """Load the encrypted index off the main thread; then crawl (incrementally) if it is stale."""
        cfg = load_config()
        if not cfg.get("index_enabled"):
            return False

        def work():
            pin_worker()
            INDEX.load()
            age_h = (time.time() - INDEX.progress.get("finished", 0)) / 3600.0
            if age_h > float(cfg.get("index_interval_h", 6)):
                INDEX.crawl(cfg.get("index_roots", [HOME]), cfg.get("index_network", False), cfg.get("index_hidden", False))
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add_seconds(1800, self._index_tick)
        return False

    def _index_tick(self):
        cfg = load_config()
        if cfg.get("index_enabled") and INDEX.loaded and not INDEX.crawling:
            if (time.time() - INDEX.progress.get("finished", 0)) / 3600.0 > float(cfg.get("index_interval_h", 6)):
                INDEX.crawl(cfg.get("index_roots", [HOME]), cfg.get("index_network", False), cfg.get("index_hidden", False))
        return True
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        w = self.new_window(None)
        if STRESS is not None:
            drv = StressDriver(w, STRESS[0], STRESS[1], STRESS[2])
            GLib.timeout_add(1500, lambda: (drv.start(), False)[1])

    def do_open(self, files, n, hint):
        for f in files:
            p = f.get_path()
            self.new_window(p if p and os.path.isdir(p) else (os.path.dirname(p) if p else None))

    def new_window(self, path: Optional[str]):
        if path in VIRTUAL:
            path = HOME
        w = MainWindow(self, path)
        w.present()
        return w


def _probe_gl_child() -> int:
    """Runs in a subprocess with a given GDK_BACKEND: realise a GLArea in a window and report as JSON."""
    import json
    out = {"backend": os.environ.get("GDK_BACKEND", "(default)"), "ok": False, "attempts": [], "renderer": "", "error": ""}
    try:
        win = Gtk.Window(title="kestrel gl probe", default_width=64, default_height=64)
        area = Gtk.GLArea()
        area.set_has_depth_buffer(True)
        result = {}

        def create(a):
            ctx, label = gl_try_contexts(a.get_window(), out["attempts"])
            result["label"] = label
            return ctx
        area.connect("create-context", create)

        def render(a, ctx):
            try:
                out["renderer"] = (_GL.glGetString(_GL.GL_RENDERER) or b"").decode(errors="replace") + " / " + \
                                  (_GL.glGetString(_GL.GL_VERSION) or b"").decode(errors="replace")
                hdr = _gl_header(ctx)
                _GLshaders.compileProgram(_GLshaders.compileShader(hdr + _GL_VERT, _GL.GL_VERTEX_SHADER),
                                          _GLshaders.compileShader(hdr + _GL_FRAG, _GL.GL_FRAGMENT_SHADER))
                out["ok"] = True
                out["flavour"] = result.get("label")
            except Exception as ex:
                out["error"] = f"shader/render: {ex}"[:400]
            return True
        area.connect("render", render)
        win.add(area)
        win.show_all()
        t0 = time.time()
        while time.time() - t0 < 3 and not out["ok"] and not out["error"]:
            while Gtk.events_pending():
                Gtk.main_iteration()
            if area.get_error() is not None and not out["error"]:
                out["error"] = f"context: {area.get_error().message}"
            time.sleep(0.02)
        win.destroy()
    except Exception as ex:
        out["error"] = f"{ex}"[:400]
    print("KESTREL-GL " + json.dumps(out))
    return 0


def probe_gl(args: List[str]) -> int:
    """kestrel.py --probe-gl: system GL facts, then the GLArea test under each GDK backend. If the default
    backend fails but another works, that backend is saved to the config (--no-save to only report)."""
    import json
    if args and args[0] == "--child":
        return _probe_gl_child()
    print("Kestrel OpenGL probe")
    print("=" * 60)
    env = os.environ
    print(f"session:      XDG_SESSION_TYPE={env.get('XDG_SESSION_TYPE', '?')}  WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '-')}  DISPLAY={env.get('DISPLAY', '-')}")
    print(f"GDK env:      GDK_BACKEND={env.get('GDK_BACKEND', '-')}  GDK_GL={env.get('GDK_GL', '-')}  GDK_DEBUG={env.get('GDK_DEBUG', '-')}  LIBGL_ALWAYS_SOFTWARE={env.get('LIBGL_ALWAYS_SOFTWARE', '-')}")
    print(f"python:       {sys.executable}  PyOpenGL: {'yes' if HAVE_GL else 'NO (python3 -m pip install --user PyOpenGL)'}")
    print(f"GTK:          {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    try:
        gpu = subprocess.run(["sh", "-c", "lspci 2>/dev/null | grep -iE 'vga|3d|display'"], capture_output=True, text=True, timeout=10).stdout.strip()
        print("GPU:          " + (gpu.replace("\n", "\n              ") or "(lspci unavailable)"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for tool, argv in (("glxinfo", ["glxinfo", "-B"]), ("eglinfo", ["eglinfo", "-B"])):
        if shutil.which(tool):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                keep = [l.strip() for l in r.stdout.splitlines() if any(k in l for k in ("OpenGL renderer", "OpenGL version", "OpenGL core profile version", "Vendor", "EGL version", "Device:", "Accelerated"))]
                print(f"{tool + ':':<14}" + ("\n              ".join(keep[:6]) or (r.stderr.strip().splitlines() or ["no output"])[-1]))
            except (OSError, subprocess.TimeoutExpired) as ex:
                print(f"{tool + ':':<14}{ex}")
        else:
            print(f"{tool + ':':<14}not installed (sudo dnf install {'glx-utils' if tool == 'glxinfo' else 'egl-utils'})")
    verdicts = []
    try:
        r = subprocess.run(["sh", "-c", "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1"], capture_output=True, text=True, timeout=10)
        smi = r.stdout.strip()
        print("nvidia:       " + (smi.replace("\n", "\n              ") or "(no nvidia-smi)"))
        if "version mismatch" in smi.lower():
            verdicts.append("The NVIDIA kernel module and the user-space driver libraries are different versions (a driver\n"
                            "  update was installed but the old module is still loaded). Nothing can create a GL context until\n"
                            "  they match: REBOOT, then run this probe again. (Check with: cat /proc/driver/nvidia/version)")
        try:
            kmod = open("/proc/driver/nvidia/version").read().strip().splitlines()[0]
            print("nvidia kmod:  " + kmod)
        except OSError:
            pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    if shutil.which("glxinfo"):
        r = subprocess.run(["glxinfo", "-B"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or "OpenGL renderer" not in r.stdout:
            verdicts.append("glxinfo itself fails on this X display, so GLX is broken system-wide, not just in Kestrel:\n  " +
                            " | ".join((r.stderr.strip() + "\n" + r.stdout.strip()).strip().splitlines()[-3:]))
    if not HAVE_GL:
        print("\nPyOpenGL is missing, so the viewer cannot use the GPU at all. Install it and run the probe again.")
        return 1
    print("\nGLArea context test (what the viewer does), per GDK backend:")
    results = {}
    for backend in (None, "x11", "wayland"):
        if backend == "wayland" and not env.get("WAYLAND_DISPLAY"):
            continue
        if backend == "x11" and not env.get("DISPLAY"):
            continue
        cenv = dict(env)
        if backend:
            cenv["GDK_BACKEND"] = backend
        else:
            cenv.pop("GDK_BACKEND", None)
        try:
            r = subprocess.run([sys.executable, os.path.abspath(sys.argv[0]), "--probe-gl", "--child"], env=cenv, capture_output=True, text=True, timeout=30)
            line = [l for l in r.stdout.splitlines() if l.startswith("KESTREL-GL ")]
            res = json.loads(line[-1][11:]) if line else {"ok": False, "error": (r.stderr.strip().splitlines() or ["no output"])[-1][:300], "attempts": []}
        except (OSError, subprocess.TimeoutExpired, ValueError) as ex:
            res = {"ok": False, "error": str(ex), "attempts": []}
        results[backend] = res
        name = backend or "default"
        print(f"  {name:<8} {'OK  ' if res['ok'] else 'FAIL'}  {res.get('renderer') or res.get('error', '')}" + (f"  [{res.get('flavour')}]" if res.get("ok") else ""))
        for a in res.get("attempts", []):
            print(f"           - {a}")
    print()
    if results.get(None, {}).get("ok"):
        print("Hardware acceleration works with the default backend. Turn on Tools → Hardware-accelerated 3D preview.")
        return 0
    working = [b for b, r in results.items() if b and r.get("ok")]
    if working:
        b = working[0]
        print(f"The default backend fails but GDK_BACKEND={b} works.")
        if "--no-save" not in args:
            cfg = load_config()
            cfg["gdk_backend"] = b
            save_config(cfg)
            print(f"Saved gdk_backend={b} to the Kestrel config: the app will start with that backend from now on.")
        else:
            print(f"Run Kestrel as:  GDK_BACKEND={b} python3 kestrel.py   (or drop --no-save to make it permanent)")
        return 0
    if verdicts:
        print("Diagnosis:")
        for v in verdicts:
            print("* " + v)
        print()
    print("No backend could create a GL context. Common causes:")
    print("  - NVIDIA driver without GBM/EGL Wayland support: try the x11 result above, or update the driver")
    print("  - remote session / VNC / software-only GL: set LIBGL_ALWAYS_SOFTWARE=1 to use llvmpipe (slow but works)")
    print("  - Flatpak/snap sandboxes or missing mesa-libGL / mesa-libEGL packages: sudo dnf install mesa-libGL mesa-libEGL mesa-dri-drivers")
    return 1


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    if "--stress" in sys.argv[1:] or "--nav-bench" in sys.argv[1:]:
        args = sys.argv[1:]
        flag = "--stress" if "--stress" in args else "--nav-bench"
        i = args.index(flag)
        rest = []
        for a in args[i + 1:]:
            if a.startswith("--"):
                break
            rest.append(a)
        folder = " ".join(rest) if rest and os.path.isdir(" ".join(rest)) else None       # unquoted paths with spaces
        if rest and folder is None:
            print(f"stress: folder not found: {' '.join(rest)!r} — using a generated one", file=sys.stderr)
        cycles = int(args[args.index("--cycles") + 1]) if "--cycles" in args and args.index("--cycles") + 1 < len(args) else 1
        globals()["STRESS"] = (folder, cycles, "nav" if flag == "--nav-bench" else "stress")
        sys.argv = [sys.argv[0]]
    if "--preview-bench" in sys.argv[1:]:
        return run_preview_bench(big="--quick" not in sys.argv[1:])
    if "--selftest" in sys.argv[1:]:
        return run_selftest("--bench" in sys.argv[1:])
    if "--profile" in sys.argv[1:]:
        import cProfile
        sys.argv.remove("--profile")
        globals()["PROFILE"] = cProfile.Profile()
        PROFILE.enable()
        log = os.path.join(GLib.get_user_cache_dir(), "kestrel", "stalls.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        open(log, "w").close()
        print(f"kestrel: profiling — stalls are logged live to {log}; a summary prints at exit (Tools → Performance report… while running)", file=sys.stderr)
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-gl":
        return probe_gl(sys.argv[2:])
    if len(sys.argv) > 2 and sys.argv[1] == "--probe-model":
        # terminal self-test for the 3D preview chain: kestrel.py --probe-model file.abc
        print(preview_tool_report())
        print("-" * 60)
        globals()["BLENDER_VERBOSE"] = True
        t0 = time.time()
        m = load_model(" ".join(sys.argv[2:]))     # tolerate an unquoted path with spaces
        if m and "error" not in m:
            print(f"OK in {time.time() - t0:.1f}s: {len(m['verts'])} verts, {len(m['faces'])} faces, info={m['info']}")
            return 0
        print("FAILED:", (m or {}).get("error", "no result"))
        return 1
    if "--demo" in sys.argv[1:]:
        # kestrel --demo /path/to/KestrelDemo : a fictional machine for screenshots (see docs/DEMO.md)
        i = sys.argv.index("--demo")
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            root = sys.argv[i + 1]
            del sys.argv[i:i + 2]
        else:
            root = os.path.join(HOME, "KestrelDemo")
            del sys.argv[i]
        if not os.path.isdir(root):
            print(f"kestrel: demo folder not found: {root}", file=sys.stderr)
            return 2
        globals()["DEMO"] = demo_setup(root)
        globals()["HOME"] = DEMO["home"]
        if len(sys.argv) == 1:
            sys.argv.append(os.path.join(DEMO["root"], "Projects", "Hollowmere"))
    cfg0 = load_config()
    globals()["UI_CORES"] = int(cfg0.get("ui_cores", 2))
    pin_ui_thread()
    install_thread_pinning()
    sys.setswitchinterval(0.002)      # let the GTK thread grab the GIL back from Python workers within ~2 ms
    if cfg0.get("gdk_backend") and not os.environ.get("GDK_BACKEND"):
        os.environ["GDK_BACKEND"] = cfg0["gdk_backend"]       # chosen by --probe-gl; must be set before the display opens
    app = ExplorerApp()
    try:
        return app.run(sys.argv)
    finally:
        save_config(app.cfg)
        if PROFILE is not None:
            out = os.path.join(GLib.get_user_cache_dir(), "kestrel", "profile.prof")
            PROFILE.disable()
            PROFILE.dump_stats(out)
            print("\n" + WATCHDOG.report() + "\n\n" + profiling_report(40), file=sys.stderr)
            print(f"\nkestrel: full profile saved to {out}  (python3 -m pstats {out})", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
