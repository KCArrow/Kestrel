

# --------------------------------------------------------------------------- row hover for tree views
def clip_width(widget: Gtk.Widget) -> Gtk.Widget:
    """Wrap a horizontal toolbar so it never dictates the pane's minimum width: a ScrolledWindow with an
    external horizontal policy reports a tiny minimum and simply clips what doesn't fit. Without this the
    preview pane grew whenever a wide toolbar (text editor, model bar, video controls) became visible."""
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
    sw.set_propagate_natural_height(True)
    sw.add(widget)
    return sw


class FitImage(Gtk.DrawingArea):
    """A picture that scales DOWN to whatever space it is given (never up), centred, with a minimum size of
    zero — unlike Gtk.Image, whose minimum is the pixbuf's full size and therefore stops a pane from being
    made smaller than the picture. Used for the preview image and the big fallback icon."""

    def __init__(self, max_size: int = 0):
        super().__init__()
        self.pixbuf = None
        self.max_size = max_size              # 0 = natural size cap is the pixbuf itself
        self.set_size_request(1, 1)
        self.connect("draw", self._draw)

    def set_from_pixbuf(self, pb):
        self.pixbuf = pb
        self.queue_draw()

    def _draw(self, w, cr):
        pb = self.pixbuf
        if pb is None:
            return False
        a = self.get_allocation()
        pw, ph = pb.get_width(), pb.get_height()
        cap = self.max_size or max(pw, ph)
        s = min(1.0, a.width / pw, a.height / ph, cap / max(pw, ph))
        dw, dh = pw * s, ph * s
        x, y = (a.width - dw) / 2, (a.height - dh) / 2
        cr.save()
        cr.translate(x, y)
        cr.scale(s, s)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.get_source().set_filter(cairo_filter_good())
        cr.paint()
        cr.restore()
        return False


def cairo_filter_good():
    try:
        import cairo
        return cairo.FILTER_GOOD
    except Exception:
        return 0


def enable_row_hover(tv: Gtk.TreeView):
    """Track the row under the pointer in tv.hover_path and repaint; cell data funcs read it via hover_bg()."""
    tv.hover_path = None
    tv.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

    def motion(w, event):
        hit = w.get_path_at_pos(int(event.x), int(event.y))
        p = hit[0] if hit else None
        old = w.hover_path
        if (p is None) != (old is None) or (p is not None and old is not None and p.compare(old) != 0):
            w.hover_path = p
            w.queue_draw()
        return False

    def leave(w, event):
        if w.hover_path is not None:
            w.hover_path = None
            w.queue_draw()
        return False
    tv.connect("motion-notify-event", motion)
    tv.connect("leave-notify-event", leave)


def hover_bg(tv: Gtk.TreeView, cell, path_or_iter_model, it):
    """Apply/clear the subtle hover background on a renderer for the row being drawn."""
    hp = getattr(tv, "hover_path", None)
    on = hp is not None and path_or_iter_model.get_path(it).compare(hp) == 0 and not tv.get_selection().iter_is_selected(it)
    if on:
        fg = tv.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        cell.set_property("cell-background-rgba", Gdk.RGBA(fg.red, fg.green, fg.blue, 0.07))
    cell.set_property("cell-background-set", on)


# --------------------------------------------------------------------------- soft grid lines for tree views
def enable_soft_grid(tv: Gtk.TreeView, columns: bool = True):
    """Draw our own separators after the rows: faint horizontal rules, and (optionally) vertical rules that
    fade out as they approach each horizontal rule so the crossings never form a visible '+'."""
    tv.set_grid_lines(Gtk.TreeViewGridLines.NONE)

    def draw(w, cr):
        model = w.get_model()
        if model is None:
            return False
        rng = w.get_visible_range()
        if not rng or rng[0] is None:
            return False
        start, end = rng
        cols = [c for c in w.get_columns() if c.get_visible()]
        if not cols:
            return False
        fg = w.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        alloc = w.get_allocation()
        rows = []
        it = model.get_iter(start)
        while it is not None:
            p = model.get_path(it)
            r = w.get_background_area(p, cols[0])
            _, y = w.convert_bin_window_to_widget_coords(0, r.y)
            v = model[it][0]
            if not (getattr(v, "kind", "") == "spacer"):
                rows.append((y, r.height))
            if p.compare(end) == 0:
                break
            it = model.iter_next(it)
            if it is None:
                break
        cr.save()
        cr.set_line_width(1)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.055)
        for y, h in rows:
            cr.move_to(0, y + h - 0.5)
            cr.line_to(alloc.width, y + h - 0.5)
        cr.stroke()
        if columns and rows:
            xs = []
            for c in cols[:-1]:
                r = w.get_background_area(start, c)
                x, _ = w.convert_bin_window_to_widget_coords(r.x + r.width, 0)
                xs.append(x - 0.5)
            fade = 7
            for x in xs:
                for y, h in rows:
                    y0, y1 = y, y + h - 1
                    if h <= 2 * fade + 2:
                        continue
                    pat = cairo_linear(x, y0, x, y1, fg, fade / h)
                    cr.set_source(pat)
                    cr.move_to(x, y0)
                    cr.line_to(x, y1)
                    cr.stroke()
        cr.restore()
        return False
    tv.connect_after("draw", draw)


def cairo_linear(x0, y0, x1, y1, fg, frac):
    import cairo
    pat = cairo.LinearGradient(x0, y0, x1, y1)
    pat.add_color_stop_rgba(0.0, fg.red, fg.green, fg.blue, 0.0)
    pat.add_color_stop_rgba(frac, fg.red, fg.green, fg.blue, 0.055)
    pat.add_color_stop_rgba(1.0 - frac, fg.red, fg.green, fg.blue, 0.055)
    pat.add_color_stop_rgba(1.0, fg.red, fg.green, fg.blue, 0.0)
    return pat


# --------------------------------------------------------------------------- virtualized icon grid
class IconGrid(Gtk.Layout):
    """A Nautilus-style icon view: only the tiles inside the viewport are laid out and painted, so a
    folder with 100k entries costs the same per frame as one with 100. Reads rows straight from the
    FolderView ListStore (col 0 Entry, col 2 pixbuf, col 3 markup). API mirrors the bits of
    Gtk.IconView that FolderView used: paths are Gtk.TreePath, signals item-activated / selection-changed."""

    __gsignals__ = {
        "item-activated": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, model: Gtk.ListStore):
        super().__init__()
        self.model: Optional[Gtk.ListStore] = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK |
                        Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.get_style_context().add_class("icongrid")
        # geometry
        self.icon_size = 48
        self.horizontal = False
        self.tile_w, self.tile_h = 96, 90
        self.margin = 8
        self.cols = 1
        self.text_lines = 2
        # state
        self.selected: set = set()
        self.cursor = -1
        self.anchor = -1
        self.prelight = -1
        self.single_click = False
        self.press_index = -1
        self.press_pos = None
        self.press_was_selected = False
        self.rubber: Optional[Tuple[float, float, float, float]] = None
        self.rubber_base: set = set()
        self.drag_targets = None
        self.drag_actions = 0
        self.provider: Optional[Callable] = None     # entry -> (pixbuf, markup); used when model cols 2/3 are None
        self.muted = False                           # inactive split pane: grey icons, dimmer text
        self._cache: Dict[int, Tuple[object, str]] = {}
        self._layout_cache: Dict[int, Pango.Layout] = {}
        self.connect("draw", self._draw)
        self.connect("size-allocate", lambda w, a: self._relayout())
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", lambda w, e: self._set_prelight(-1))
        self.connect("key-press-event", self._key)
        self.connect("focus-in-event", lambda w, e: self.queue_draw() or False)
        self.connect("focus-out-event", lambda w, e: self.queue_draw() or False)
        self.connect("drag-begin", self._drag_begin)
        self.connect("grab-broken-event", lambda w, e: self._reset_press())
        self.connect("grab-notify", lambda w, was_grabbed: None if was_grabbed else self._reset_press())
        self.set_model(model)

    def _reset_press(self):
        """A dialog or drag grab interrupted a click: forget the pending press/rubber band."""
        self.press_index, self.press_pos = -1, None
        if self.rubber is not None:
            self.rubber = None
            self.queue_draw()
        return False

    def _drag_begin(self, w, ctx):
        if self.cursor >= 0 and self.model is not None:
            try:
                pb = self._tile_data(self.cursor, self.model[self.cursor])[0]
                if pb is not None:
                    Gtk.drag_set_icon_pixbuf(ctx, pb, pb.get_width() // 2, pb.get_height() // 2)
            except (IndexError, ValueError):
                pass

    # ---- model
    def set_model(self, model):
        if self.model is not None:
            for h in getattr(self, "_mh", []):
                self.model.disconnect(h)
        self.model = model
        self._mh = []
        if model is not None:
            self._mh = [model.connect("row-changed", self._row_changed),
                        model.connect("row-inserted", lambda m, p, it: self._model_resized()),
                        model.connect("row-deleted", lambda m, p: self._model_resized())]
        self.selected.clear()
        self.cursor = self.anchor = -1
        self._model_resized()

    def get_model(self):
        return self.model

    def _row_changed(self, m, p, it):
        self._cache.pop(p.get_indices()[0], None)
        self.queue_draw()

    def invalidate(self):
        self._cache.clear()
        self.queue_draw()

    def _tile_data(self, i, row):
        """(pixbuf, markup) for a row: model columns if filled, else computed once via provider and cached."""
        pb, mk = row[2], row[3]
        if pb is not None and mk is not None:
            return pb, mk
        hit = self._cache.get(i)
        if hit is None:
            hit = self.provider(row[0]) if self.provider else (None, "")
            self._cache[i] = hit
        return hit

    def _n(self) -> int:
        return len(self.model) if self.model is not None else 0

    def _model_resized(self):
        n = self._n()
        self.selected = {i for i in self.selected if i < n}
        if self.cursor >= n:
            self.cursor = n - 1
        self._cache.clear()
        self._relayout()

    # ---- layout
    def configure(self, icon_size: int, horizontal: bool, item_width: int, single_click: bool):
        self.icon_size, self.horizontal, self.single_click = icon_size, horizontal, single_click
        probe = self.create_pango_layout("Xg")
        line = probe.get_pixel_size()[1] + 1
        if horizontal:
            self.text_lines = 3 if icon_size >= 48 else 1
            self.tile_w = item_width
            self.tile_h = max(icon_size, self.text_lines * line) + 12
        else:
            self.text_lines = 2
            self.tile_w = max(90, item_width)
            self.tile_h = icon_size + 10 + self.text_lines * line + 10
        self._layout_cache.clear()
        self._cache.clear()
        self._relayout()

    def _relayout(self):
        w = max(1, self.get_allocated_width() - 2 * self.margin)
        self.cols = max(1, w // self.tile_w)
        rows = (self._n() + self.cols - 1) // self.cols
        self.set_size(self.get_allocated_width(), rows * self.tile_h + 2 * self.margin)
        self.queue_draw()

    def _tile_rect(self, i: int) -> Tuple[int, int, int, int]:
        r, c = divmod(i, self.cols)
        return self.margin + c * self.tile_w, self.margin + r * self.tile_h, self.tile_w, self.tile_h

    def _index_at(self, x: float, y: float) -> int:
        """Index of the tile under canvas coords, or -1 (empty space counts as -1 only outside the tile box)."""
        c = int((x - self.margin) // self.tile_w)
        r = int((y - self.margin) // self.tile_h)
        if x < self.margin or y < self.margin or c < 0 or c >= self.cols or r < 0:
            return -1
        i = r * self.cols + c
        return i if i < self._n() else -1

    def _visible_range(self) -> Tuple[int, int]:
        v = Gtk.Scrollable.get_vadjustment(self)
        top = int(v.get_value()) if v else 0
        h = self.get_allocated_height()
        r0 = max(0, (top - self.margin) // self.tile_h)
        r1 = (top + h - self.margin) // self.tile_h + 1
        return int(r0 * self.cols), int(min(self._n(), (r1 + 1) * self.cols))

    # ---- drawing
    def _draw(self, w, cr):
        n = self._n()
        if n == 0:
            return False
        # GTK 3.24 hands us a widget-relative cairo context; the scrolled canvas lives in the bin window,
        # which sits at (-hadj, -vadj). Translate so we can draw in canvas coordinates.
        cr.save()
        cr.translate(-self._hoff(), -self._voff())
        try:
            self._draw_tiles(cr)
        finally:
            cr.restore()
        return False

    def _draw_tiles(self, cr):
        n = self._n()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        ok, sel = sc.lookup_color("theme_selected_bg_color")
        sel = sel if ok else Gdk.RGBA(0.2, 0.5, 0.9, 1)
        ok, selfg = sc.lookup_color("theme_selected_fg_color")
        selfg = selfg if ok else Gdk.RGBA(1, 1, 1, 1)
        focused = self.has_focus()
        a, b = self._visible_range()
        pad = 6
        if self.horizontal:
            cr.save()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.055)
            cr.set_line_width(1)
            for i in range(a, b, self.cols):
                x, y, tw, th = self._tile_rect(i)
                cr.move_to(self.margin, y + th - 0.5)
                cr.line_to(self.margin + self.cols * self.tile_w, y + th - 0.5)
            cr.stroke()
            cr.restore()
        for i in range(a, b):
            try:
                row = self.model[i]
            except (IndexError, ValueError):
                break
            x, y, tw, th = self._tile_rect(i)
            is_sel = i in self.selected
            if is_sel or i == self.prelight:
                cr.save()
                _rounded(cr, x + 2, y + 2, tw - 4, th - 4, 6)
                if is_sel:
                    g_ = 0.2126 * sel.red + 0.7152 * sel.green + 0.0722 * sel.blue
                    if self.muted:
                        cr.set_source_rgba(g_, g_, g_, 0.35)
                    else:
                        cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.85 if focused else 0.5)
                else:
                    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
                cr.fill()
                cr.restore()
            pb, markup = self._tile_data(i, row)
            size = self.icon_size
            if self.horizontal:
                ix, iy = x + pad, y + (th - size) // 2
                tx, ty, tw_text = x + pad + size + 8, y + 6, tw - size - 3 * pad - 8
            else:
                ix, iy = x + (tw - size) // 2, y + pad
                tx, ty, tw_text = x + 4, y + pad + size + 6, tw - 8
            if pb is not None:
                if self.muted:
                    pb = ICONS.desaturated(pb)
                ox, oy = (size - pb.get_width()) // 2, (size - pb.get_height()) // 2
                Gdk.cairo_set_source_pixbuf(cr, pb, ix + ox, iy + oy)
                cr.paint()
            lay = self._text_layout(cr, i, markup, tw_text)
            if is_sel:
                cr.set_source_rgba(selfg.red, selfg.green, selfg.blue, 1)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6 if self.muted else 1)
            cr.move_to(tx, ty if self.horizontal else ty)
            PangoCairo.show_layout(cr, lay)
            if i == self.cursor and focused:
                cr.save()
                _rounded(cr, x + 2, y + 2, tw - 4, th - 4, 6)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
                cr.set_line_width(1)
                cr.set_dash([2, 2])
                cr.stroke()
                cr.restore()
        if self.rubber:
            x0, y0, x1, y1 = self.rubber
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.2)
            cr.fill_preserve()
            cr.set_source_rgba(sel.red, sel.green, sel.blue, 0.8)
            cr.set_line_width(1)
            cr.stroke()
        return False

    def _text_layout(self, cr, i, markup, width):
        lay = self.create_pango_layout("")        # widget's own Pango context: same font as the rest of the UI
        lay.set_markup(markup or "", -1)
        lay.set_width(max(10, int(width * Pango.SCALE)))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_height(-self.text_lines)
        lay.set_alignment(Pango.Alignment.LEFT if self.horizontal else Pango.Alignment.CENTER)
        return lay

    # ---- selection API (Gtk.IconView-compatible subset)
    def _emit_sel(self):
        self.emit("selection-changed")
        self.queue_draw()

    def get_selected_items(self):
        return [Gtk.TreePath.new_from_indices([i]) for i in sorted(self.selected)]

    def path_is_selected(self, path) -> bool:
        return path.get_indices()[0] in self.selected

    def select_path(self, path):
        self.selected.add(path.get_indices()[0])
        self._emit_sel()

    def unselect_path(self, path):
        self.selected.discard(path.get_indices()[0])
        self._emit_sel()

    def select_all(self):
        self.selected = set(range(self._n()))
        self._emit_sel()

    def unselect_all(self):
        if self.selected:
            self.selected.clear()
            self._emit_sel()

    def set_cursor(self, path, cell=None, start_editing=False):
        self.cursor = path.get_indices()[0]
        self.anchor = self.cursor
        self.queue_draw()

    def get_cursor(self):
        return (Gtk.TreePath.new_from_indices([self.cursor]) if self.cursor >= 0 else None, None)

    def scroll_to_path(self, path, use_align=False, row_align=0.0, col_align=0.0):
        i = path.get_indices()[0]
        x, y, tw, th = self._tile_rect(i)
        v = Gtk.Scrollable.get_vadjustment(self)
        if v is None:
            return
        top, page = v.get_value(), v.get_page_size()
        if y < top:
            v.set_value(y - self.margin)
        elif y + th > top + page:
            v.set_value(y + th - page + self.margin)

    def get_path_at_pos(self, x, y):
        """x, y in bin-window (canvas) coordinates, which is what button events on a Gtk.Layout carry."""
        i = self._index_at(x, y)
        return Gtk.TreePath.new_from_indices([i]) if i >= 0 else None

    def get_dest_item_at_pos(self, x, y):
        """x, y in widget coordinates (what drag-data-received gives)."""
        p = self.get_path_at_pos(x + self._hoff(), y + self._voff())
        return (p, Gtk.IconViewDropPosition.DROP_INTO) if p is not None else None

    def set_activate_on_single_click(self, v):
        self.single_click = v

    def enable_model_drag_source(self, start_button_mask, targets, actions):
        self.drag_targets = Gtk.TargetList.new(targets)
        self.drag_actions = actions

    def _voff(self):
        v = Gtk.Scrollable.get_vadjustment(self)          # (Gtk.Layout's own getter is deprecated)
        return v.get_value() if v else 0

    def _hoff(self):
        h = Gtk.Scrollable.get_hadjustment(self)
        return h.get_value() if h else 0

    # ---- mouse
    def _set_prelight(self, i):
        if i != self.prelight:
            self.prelight = i
            self.queue_draw()

    def _press(self, w, event):
        self.grab_focus()
        x, y = event.x, event.y          # bin-window coordinates
        i = self._index_at(x, y)
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            if i >= 0 and not self.single_click:
                self.emit("item-activated", Gtk.TreePath.new_from_indices([i]))
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if event.button == 1:
            self.press_index, self.press_pos = i, (event.x, event.y)
            self.press_was_selected = i in self.selected
            if i < 0:
                if not (ctrl or shift):
                    self.unselect_all()
                self.rubber = (x, y, x, y)
                self.rubber_base = set(self.selected) if ctrl else set()
                return True
            if ctrl:
                (self.selected.discard if i in self.selected else self.selected.add)(i)
                self.cursor = self.anchor = i
                self._emit_sel()
            elif shift and self.anchor >= 0:
                lo, hi = min(self.anchor, i), max(self.anchor, i)
                self.selected = set(range(lo, hi + 1))
                self.cursor = i
                self._emit_sel()
            elif i not in self.selected:
                self.selected = {i}
                self.cursor = self.anchor = i
                self._emit_sel()
            else:
                self.cursor = i
                self.queue_draw()
            return True
        if event.button == 3:
            return False     # FolderView handles the context menu (it reads get_path_at_pos)
        return False

    def _motion(self, w, event):
        x, y = event.x, event.y
        if self.rubber is not None:
            self.rubber = (self.rubber[0], self.rubber[1], x, y)
            x0, y0, x1, y1 = self.rubber
            rx0, ry0, rx1, ry1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
            hit = set()
            r0, r1 = max(0, int((ry0 - self.margin) // self.tile_h)), int((ry1 - self.margin) // self.tile_h)
            for r in range(r0, r1 + 1):
                for c in range(self.cols):
                    i = r * self.cols + c
                    if i >= self._n():
                        break
                    tx, ty, tw, th = self._tile_rect(i)
                    if tx < rx1 and tx + tw > rx0 and ty < ry1 and ty + th > ry0:
                        hit.add(i)
            new = self.rubber_base ^ hit if self.rubber_base else hit
            if new != self.selected:
                self.selected = new
                self.emit("selection-changed")
            self.queue_draw()
            # autoscroll near edges
            v = Gtk.Scrollable.get_vadjustment(self)
            if v:
                vy = event.y - v.get_value()
                if vy < 12:
                    v.set_value(max(v.get_lower(), v.get_value() - 10))
                elif vy > self.get_allocated_height() - 12:
                    v.set_value(min(v.get_upper() - v.get_page_size(), v.get_value() + 10))
            return True
        if self.press_index >= 0 and self.press_pos is not None and self.drag_targets is not None and (event.state & Gdk.ModifierType.BUTTON1_MASK):
            if self.drag_check_threshold(int(self.press_pos[0]), int(self.press_pos[1]), int(event.x), int(event.y)):
                if self.press_index not in self.selected:
                    self.selected = {self.press_index}
                    self._emit_sel()
                self.drag_begin_with_coordinates(self.drag_targets, self.drag_actions, 1, event, int(event.x), int(event.y))
                self.press_index, self.press_pos = -1, None
                return True
        self._set_prelight(self._index_at(x, y))
        return False

    def _release(self, w, event):
        if event.button != 1:
            return False
        if self.rubber is not None:
            self.rubber = None
            self.queue_draw()
            return True
        i = self.press_index
        if i >= 0 and self.press_pos is not None:
            mods = event.state & Gtk.accelerator_get_default_mod_mask()
            moved = self.drag_check_threshold(int(self.press_pos[0]), int(self.press_pos[1]), int(event.x), int(event.y))
            if not moved and not mods:
                if self.single_click:
                    self.emit("item-activated", Gtk.TreePath.new_from_indices([i]))
                elif self.press_was_selected and self.selected != {i}:
                    self.selected = {i}     # click on one of several selected items selects just it
                    self._emit_sel()
        self.press_index, self.press_pos = -1, None
        return True

    # ---- keyboard
    def _key(self, w, event):
        n = self._n()
        if n == 0:
            return False
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        cur = self.cursor if self.cursor >= 0 else 0
        page_rows = max(1, int(self.get_allocated_height() // self.tile_h))
        moves = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -self.cols, Gdk.KEY_Down: self.cols,
                 Gdk.KEY_Page_Up: -self.cols * page_rows, Gdk.KEY_Page_Down: self.cols * page_rows,
                 Gdk.KEY_KP_Left: -1, Gdk.KEY_KP_Right: 1, Gdk.KEY_KP_Up: -self.cols, Gdk.KEY_KP_Down: self.cols}
        if key in moves or key in (Gdk.KEY_Home, Gdk.KEY_End):
            new = 0 if key == Gdk.KEY_Home else (n - 1 if key == Gdk.KEY_End else cur + moves[key])
            new = max(0, min(n - 1, new))
            self.cursor = new
            if shift:
                a = self.anchor if self.anchor >= 0 else cur
                self.selected = set(range(min(a, new), max(a, new) + 1))
            elif not ctrl:
                self.selected = {new}
                self.anchor = new
            self.scroll_to_path(Gtk.TreePath.new_from_indices([new]))
            self._emit_sel()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not mods and self.cursor >= 0:
            self.emit("item-activated", Gtk.TreePath.new_from_indices([self.cursor]))
            return True
        if key == Gdk.KEY_space and not mods and self.cursor >= 0:
            self.selected = {self.cursor}
            self.anchor = self.cursor
            self._emit_sel()
            return True
        if key == Gdk.KEY_a and ctrl:
            self.select_all()
            return True
        return False


def _rounded(cr, x, y, w, h, r):
    import math
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# --------------------------------------------------------------------------- This PC: Windows-style tile view
class DriveBar(Gtk.DrawingArea):
    """The thin capacity bar under a drive's name: used/total, red past 90 % like Explorer."""

    def __init__(self):
        super().__init__()
        self.frac = None
        self.set_size_request(120, 10)
        self.connect("draw", self._draw)

    def set_fraction(self, frac):
        self.frac = frac
        self.queue_draw()

    def _draw(self, w, cr):
        a = self.get_allocation()
        sc = self.get_style_context()
        fg = sc.get_color(Gtk.StateFlags.NORMAL)
        _rounded(cr, 0, 2, a.width, a.height - 4, 2)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.fill()
        if self.frac is not None:
            f = max(0.0, min(1.0, self.frac))
            if f > 0:
                _rounded(cr, 0, 2, max(4, a.width * f), a.height - 4, 2)
                if f > 0.9:
                    cr.set_source_rgb(0.85, 0.22, 0.2)
                else:
                    ok, sel = sc.lookup_color("theme_selected_bg_color")
                    cr.set_source_rgb(sel.red, sel.green, sel.blue) if ok else cr.set_source_rgb(0.2, 0.5, 0.9)
                cr.fill()
        return False


class PCView(Gtk.ScrolledWindow):
    """Explorer-style 'This PC': collapsible sections (Folders, one per physical disk, Network locations) of
    tiles. Folder tiles are icon + name; drive tiles are icon, name, capacity bar and 'x free of y'.
    Built from ordinary GTK widgets (FlowBox per section) so selection, keyboard navigation, hover and
    activation behave like the rest of the desktop. Emits 'selection-changed', 'item-activated' (Entry) and
    'context-menu' (Entry or None, event)."""

    __gsignals__ = {"selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
                    "item-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
                    "context-menu": (GObject.SignalFlags.RUN_FIRST, None, (object, object))}

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=12)
        self.add(self.box)
        self.flows: List[Gtk.FlowBox] = []
        self.tiles: Dict[int, Tuple[Gtk.FlowBoxChild, Entry, Optional[DriveBar], Optional[Gtk.Label]]] = {}
        self.single_click = False
        self._suppress = False
        self.collapsed: set = set()

    # ---- build
    def set_entries(self, ents: List[Entry], single_click: bool):
        self.single_click = single_click
        for c in self.box.get_children():
            self.box.remove(c)
        self.flows, self.tiles = [], {}
        sections: List[Tuple[str, List[Entry]]] = []
        for e in ents:
            if sections and sections[-1][0] == e.section:
                sections[-1][1].append(e)
            else:
                sections.append((e.section, [e]))
        for title, items in sections:
            exp = Gtk.Expander(expanded=title not in self.collapsed)
            lab = Gtk.Label(xalign=0)
            lab.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>  <span alpha='55%'>({len(items)})</span>")
            exp.set_label_widget(lab)
            exp.connect("notify::expanded", lambda ex, p, t=title: (self.collapsed.discard(t) if ex.get_expanded() else self.collapsed.add(t)))
            fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.MULTIPLE, homogeneous=True, max_children_per_line=12, min_children_per_line=1,
                             row_spacing=4, column_spacing=4, margin_top=4, margin_bottom=8, margin_start=16, valign=Gtk.Align.START)
            fb.set_activate_on_single_click(single_click)
            fb.connect("selected-children-changed", self._sel_changed)
            fb.connect("child-activated", lambda f, ch: self.emit("item-activated", self.tiles[id(ch)][1]))
            fb.connect("button-press-event", self._press)
            for e in items:
                ch = Gtk.FlowBoxChild()
                tile, bar, info = self._make_tile(e)
                ch.add(tile)
                fb.add(ch)
                self.tiles[id(ch)] = (ch, e, bar, info)
            exp.add(fb)
            self.flows.append(fb)
            self.box.pack_start(exp, False, False, 0)
            sep = Gtk.Separator()
            sep.set_opacity(0.35)
            self.box.pack_start(sep, False, False, 2)
        self.box.show_all()
        self.refresh()

    def _make_tile(self, e: Entry):
        is_drive = e.section not in ("Folders",) and (getattr(e, "drive_kind", "") or e.usage is not None)
        h = Gtk.Box(spacing=10, margin=6)
        img = Gtk.Image.new_from_pixbuf(ICONS.pixbuf(e.icon, 48)) if e.icon else Gtk.Image.new_from_icon_name("folder", Gtk.IconSize.DIALOG)
        img.set_valign(Gtk.Align.START)
        h.pack_start(img, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=e.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26)
        v.pack_start(name, False, False, 0)
        bar = info = None
        if is_drive:
            bar = DriveBar()
            v.pack_start(bar, False, False, 2)
            info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
            info.get_style_context().add_class("dim-label")
            v.pack_start(info, False, False, 0)
            h.set_size_request(250, -1)
        else:
            sub = Gtk.Label(label=e.location or "", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=26)
            sub.get_style_context().add_class("dim-label")
            v.pack_start(sub, False, False, 0)
            h.set_size_request(200, -1)
        h.pack_start(v, True, True, 0)
        if getattr(e, "drive_kind", "") == "unmounted":
            h.set_opacity(0.55)
        return h, bar, info

    def refresh(self):
        """Re-read usage/type on the entries (the free-space thread fills them in after the first paint)."""
        for ch, e, bar, info in self.tiles.values():
            if bar is not None:
                used, total = (e.usage or (0, 0))
                bar.set_fraction((used / total) if total else None)
            if info is not None:
                info.set_text(e.type or "")

    # ---- selection
    def _sel_changed(self, fb):
        if self._suppress:
            return
        self._suppress = True
        for other in self.flows:            # one selection across all sections (plain click only)
            if other is not fb and not (Gtk.get_current_event_state()[1] & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)):
                other.unselect_all()
        self._suppress = False
        self.emit("selection-changed")

    def selected_entries(self) -> List[Entry]:
        out = []
        for fb in self.flows:
            for ch in fb.get_selected_children():
                out.append(self.tiles[id(ch)][1])
        return out

    def select_none(self):
        self._suppress = True
        for fb in self.flows:
            fb.unselect_all()
        self._suppress = False
        self.emit("selection-changed")

    def select_all(self):
        self._suppress = True
        for fb in self.flows:
            fb.select_all()
        self._suppress = False
        self.emit("selection-changed")

    def select_names(self, names: List[str]):
        self._suppress = True
        for fb in self.flows:
            fb.unselect_all()
            for ch in fb.get_children():
                if self.tiles[id(ch)][1].name in names:
                    fb.select_child(ch)
        self._suppress = False
        self.emit("selection-changed")

    def _press(self, fb, event):
        if event.button == 3:
            ch = fb.get_child_at_pos(int(event.x), int(event.y))
            if ch is not None:
                if not ch.is_selected():
                    self.select_none()
                    fb.select_child(ch)
                self.emit("context-menu", self.tiles[id(ch)][1], event)
            else:
                self.select_none()
                self.emit("context-menu", None, event)
            return True
        if event.button == 1 and fb.get_child_at_pos(int(event.x), int(event.y)) is None:
            self.select_none()
        return False


# --------------------------------------------------------------------------- system clipboard: files
class FileClipboard:
    """Puts copied/cut files on the system clipboard in every form other applications look for.

    Gtk.Clipboard.set_with_data() - the usual way to offer custom formats - is not callable from PyGObject
    (it is marked non-introspectable), so Kestrel owns the CLIPBOARD selection through a hidden widget and
    answers format requests itself. Offered, in order of preference:
      image/png (+ the file's own image type)  a single picture: pastes as an image in any chat / web app
      x-special/gnome-copied-files             Nautilus and other GTK file managers (copy vs cut)
      text/uri-list                            Chromium, Firefox, Electron apps (Teams, Discord, Slack), KDE:
                                               web pages receive these as real files on paste
      UTF8_STRING / text/plain                 the paths, for terminals and text fields
    The files stay on the clipboard while Kestrel runs (as with every X11/Wayland app)."""
    IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                   ".webp": "image/webp", ".bmp": "image/bmp"}
    MAX_IMAGE = 40 << 20             # bigger pictures are offered as files only

    def __init__(self):
        self.w = Gtk.Invisible()
        self.w.connect("selection-get", self._get)
        self.w.connect("selection-clear-event", self._lost)
        self.sel = Gdk.SELECTION_CLIPBOARD
        self.mode, self.paths, self.uris = "copy", [], []
        self.image_path: Optional[str] = None
        self._png: Optional[bytes] = None

    def targets_for(self, paths: List[str]) -> List[str]:
        targets = []
        self.image_path = None
        if len(paths) == 1:
            ext = os.path.splitext(paths[0])[1].lower()
            try:
                small = os.path.getsize(paths[0]) <= self.MAX_IMAGE
            except OSError:
                small = False
            if ext in self.IMAGE_TYPES and small:
                self.image_path = paths[0]
                targets.append("image/png")
                if self.IMAGE_TYPES[ext] != "image/png":
                    targets.append(self.IMAGE_TYPES[ext])
        return targets + ["x-special/gnome-copied-files", "text/uri-list", "UTF8_STRING", "text/plain;charset=utf-8",
                          "text/plain", "STRING", "TEXT"]

    def set_files(self, mode: str, paths: List[str]) -> bool:
        self.mode, self.paths = mode, list(paths)
        self.uris = [Gio.File.new_for_path(p).get_uri() for p in paths]
        self._png = None
        targets = self.targets_for(paths)
        try:
            Gtk.selection_clear_targets(self.w, self.sel)
            for i, t in enumerate(targets):
                Gtk.selection_add_target(self.w, self.sel, Gdk.Atom.intern(t, False), i)
            t0 = Gtk.get_current_event_time() or Gdk.CURRENT_TIME
            return bool(Gtk.selection_owner_set(self.w, self.sel, t0))
        except Exception as ex:
            bench(f"clipboard: owning the selection failed: {ex}")
            return False

    def _lost(self, *a):
        self.paths, self.uris, self.image_path, self._png = [], [], None, None
        return False

    def payload(self, name: str) -> Optional[bytes]:
        """The bytes for one clipboard format (None: nothing to offer)."""
        if name.startswith("image/"):
            p = self.image_path
            if not p:
                return None
            ext = os.path.splitext(p)[1].lower()
            try:
                if name == self.IMAGE_TYPES.get(ext):
                    with open(p, "rb") as f:
                        return f.read()
                if name == "image/png":
                    if self._png is None:
                        pb = GdkPixbuf.Pixbuf.new_from_file(p)
                        ok, buf = pb.save_to_bufferv("png", [], [])
                        self._png = bytes(buf) if ok else b""
                    return self._png or None
            except Exception as ex:
                bench(f"clipboard: image conversion failed: {ex}")
            return None
        if name == "x-special/gnome-copied-files":
            return (self.mode + "\n" + "\n".join(self.uris)).encode()
        if name == "text/uri-list":
            return ("\r\n".join(self.uris) + "\r\n").encode()          # RFC 2483: CRLF-separated
        return "\n".join(self.paths).encode()

    def _get(self, widget, sd, info, time_):
        name = sd.get_target().name()
        if name in ("UTF8_STRING", "STRING", "TEXT", "text/plain", "text/plain;charset=utf-8"):
            sd.set_text("\n".join(self.paths), -1)
            return
        data = self.payload(name)
        if data:
            sd.set(sd.get_target(), 8, data)


_FILE_CLIPBOARD: Optional[FileClipboard] = None


def file_clipboard() -> FileClipboard:
    global _FILE_CLIPBOARD
    if _FILE_CLIPBOARD is None:
        _FILE_CLIPBOARD = FileClipboard()
    return _FILE_CLIPBOARD
