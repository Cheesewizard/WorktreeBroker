"""Look and small widgets for the broker dashboard: one palette, ttk styles on the 'clam' theme, cards, pills, meters.

No third-party theme: everything here is Tk 8.6 so the dashboard runs on any Python with tkinter.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# Palette: neutral greys, one indigo accent, and one colour per state (text colour, soft background).
BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e2e5ea"
TEXT = "#1f2328"
MUTED = "#667085"
FAINT = "#98a2b3"
ACCENT = "#4f46e5"
ACCENT_DARK = "#4338ca"
NAV = "#1c2130"
NAV_TEXT = "#aab2c5"
NAV_ACTIVE = "#2c3448"
SELECT = "#e0e7ff"

STATES = {
    "needs": ("#b45309", "#fef3c7"),
    "working": ("#15803d", "#dcfce7"),
    "quiet": ("#a16207", "#fef9c3"),
    "turn": ("#1d4ed8", "#dbeafe"),
    "connected": ("#0f766e", "#ccfbf1"),
    "stopped": ("#475467", "#eef0f3"),
    "idle": ("#667085", "#f2f4f7"),
    "error": ("#b42318", "#fee4e2"),
    "ok": ("#15803d", "#dcfce7"),
    "warn": ("#b45309", "#fef3c7"),
}

GOOD, WARN, BAD = "#16a34a", "#d97706", "#dc2626"

# Pixel sizes are written for 96 dpi and scaled by apply(): the dashboard is DPI aware (crisp text), so Tk draws
# fonts at the display's scale while plain pixel numbers would stay small.
SCALE = 1.0


def px(value):
    return int(round(value * SCALE))


def state_key(state):
    """Map a chat state ("Needs you", "Working (3)", "Working? (quiet)") to a palette key."""
    word = (state or "").split(" (")[0]
    return {"Needs you": "needs", "Working": "working", "Working?": "quiet", "Your turn": "turn",
            "Connected": "connected", "Stopped": "stopped"}.get(word, "idle")


def fonts(root):
    families = set(tkfont.families(root))
    # Tk 8.6 can't pick weights from Windows 11's variable fonts, so titles use the static Semibold cut.
    ui = "Segoe UI"
    display = "Segoe UI Semibold" if "Segoe UI Semibold" in families else "Segoe UI"
    icons = next((name for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if name in families), None)
    mono = next((name for name in ("Cascadia Mono", "Consolas") if name in families), "Courier New")
    return {"ui": ui, "display": display, "icons": icons, "mono": mono}


def apply(root):
    """Set the window's colours and every ttk style the dashboard uses. Returns the font names."""
    global SCALE
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96)
    names = fonts(root)
    ui = names["ui"]
    root.configure(background=BG)
    # No "*Font" option: it would also override every ttk style's font (ttk widgets read it as their -font).
    root.option_add("*TCombobox*Listbox.font", (ui, 10))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=(ui, 10), bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, focuscolor=SELECT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(ui, 9))
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(ui, 9))
    style.configure("Caption.TLabel", background=CARD, foreground=MUTED, font=(ui, 9))
    style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=(names["display"], 12))
    style.configure("PageTitle.TLabel", background=BG, foreground=TEXT, font=(names["display"], 17))
    style.configure("Value.TLabel", background=CARD, foreground=TEXT, font=(ui, 10))
    style.configure("Big.TLabel", background=CARD, foreground=TEXT, font=(names["display"], 13))
    style.configure("Error.TLabel", background=CARD, foreground=STATES["error"][0], font=(ui, 9))
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)])

    style.configure("TButton", background=CARD, foreground=TEXT, padding=(px(14), px(5)), relief="solid",
                    borderwidth=1, bordercolor="#cfd4dc", lightcolor=CARD, darkcolor=CARD)
    style.map("TButton", background=[("disabled", BG), ("pressed", "#e8eaee"), ("active", "#f5f6f8")],
              foreground=[("disabled", FAINT)], bordercolor=[("focus", ACCENT)])
    style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=(ui, 10, "bold"))
    style.map("Primary.TButton", background=[("disabled", "#c7c9f5"), ("pressed", ACCENT_DARK), ("active", ACCENT_DARK)],
              foreground=[("disabled", "#ffffff")], bordercolor=[("disabled", "#c7c9f5")])
    style.configure("Danger.TButton", background=CARD, foreground=STATES["error"][0], bordercolor="#f0a8a0")
    style.map("Danger.TButton", background=[("disabled", BG), ("active", "#fff4f2")], foreground=[("disabled", FAINT)])

    style.configure("TEntry", fieldbackground=CARD, padding=(px(8), px(5)), bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, padding=(px(8), px(4)), arrowsize=14,
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)], bordercolor=[("focus", ACCENT)])

    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=px(30),
                    borderwidth=0, relief="flat", font=(ui, 10))
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=CARD, foreground=MUTED, relief="flat", padding=(px(8), px(6)),
                    font=(ui, 9, "bold"), bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
    style.map("Treeview.Heading", background=[("active", "#f5f6f8")])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Vertical.TScrollbar", background=BG, troughcolor=CARD, bordercolor=CARD, arrowsize=12,
                    lightcolor=BG, darkcolor=BG, gripcount=0)
    return names


class Card(ttk.Frame):
    """A white panel with a thin border, an optional title row and a body frame."""

    def __init__(self, parent, title=None, padding=px(16), **kwargs):
        outer = tk.Frame(parent, background=BORDER)
        super().__init__(outer, style="Card.TFrame", padding=padding, **kwargs)
        super().pack(fill="both", expand=True, padx=px(1), pady=px(1))
        self.outer = outer
        self.header = None
        if title:
            self.header = ttk.Frame(self, style="Card.TFrame")
            self.header.pack(fill="x", pady=(0, px(10)))
            self.title = ttk.Label(self.header, text=title, style="CardTitle.TLabel")
            self.title.pack(side="left")

    # The card's geometry is managed through its border frame.
    def grid(self, **kwargs):
        self.outer.grid(**kwargs)

    def pack(self, **kwargs):
        self.outer.pack(**kwargs)


class Pill(tk.Label):
    """A small rounded-looking state badge: coloured text on a soft background."""

    def __init__(self, parent, text="", kind="idle", font=None, **kwargs):
        super().__init__(parent, text=text, padx=px(9), pady=px(2), borderwidth=0,
                         font=font or ("Segoe UI", 9, "bold"), **kwargs)
        self.set(text, kind)

    def set(self, text, kind="idle"):
        fg, bg = STATES.get(kind, STATES["idle"])
        self.configure(text=text, foreground=fg, background=bg)


class Meter(tk.Canvas):
    """A thin horizontal bar. Its colour follows the fraction: green, amber from `warn`, red from `bad`."""

    def __init__(self, parent, height=8, warn=0.75, bad=0.9, colour=None, background=CARD):
        super().__init__(parent, height=px(height), highlightthickness=0, borderwidth=0, background=background)
        self.fraction, self.warn, self.bad, self.colour = 0.0, warn, bad, colour
        self.bind("<Configure>", lambda _: self.draw())

    def set(self, fraction, colour=None):
        self.fraction = max(0.0, min(1.0, fraction or 0.0))
        self.colour = colour
        self.draw()

    def draw(self):
        self.delete("all")
        width, height = self.winfo_width(), int(self["height"])
        if width <= 2:
            return
        self.create_rectangle(0, 0, width, height, fill="#eceef2", width=0)
        colour = self.colour or (BAD if self.fraction >= self.bad else WARN if self.fraction >= self.warn else GOOD)
        filled = int(width * self.fraction)
        if filled > 0:
            self.create_rectangle(0, 0, filled, height, fill=colour, width=0)


class NavRail(tk.Frame):
    """The docked page list on the left: icon + label per page, the active one highlighted."""

    def __init__(self, parent, icon_font, on_select, title):
        super().__init__(parent, background=NAV, width=px(200))
        self.pack_propagate(False)
        self.icon_font, self.on_select = icon_font, on_select
        self.items, self.active, self.badges = {}, None, {}
        brand = tk.Frame(self, background=NAV)
        brand.pack(fill="x", padx=px(18), pady=(px(20), px(22)))
        tk.Label(brand, text="Worktree Broker", background=NAV, foreground="#ffffff",
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(brand, text=title, background=NAV, foreground=NAV_TEXT,
                 font=("Segoe UI", 9)).pack(anchor="w")

    def add(self, key, label, glyph, fallback):
        row = tk.Frame(self, background=NAV, cursor="hand2")
        row.pack(fill="x", padx=px(10), pady=px(1))
        bar = tk.Frame(row, background=NAV, width=px(3))
        bar.pack(side="left", fill="y")
        icon = tk.Label(row, text=glyph if self.icon_font else fallback, background=NAV, foreground=NAV_TEXT,
                        font=(self.icon_font, 12) if self.icon_font else ("Segoe UI Symbol", 11), width=2)
        icon.pack(side="left", padx=(px(10), px(6)), pady=px(9))
        text = tk.Label(row, text=label, background=NAV, foreground=NAV_TEXT, font=("Segoe UI", 10), anchor="w")
        text.pack(side="left", fill="x", expand=True)
        badge = tk.Label(row, text="", background=NAV, foreground="#ffffff", font=("Segoe UI", 8, "bold"), padx=px(6))
        badge.pack(side="right", padx=(0, px(10)))
        self.items[key] = (row, bar, icon, text, badge)
        for widget in (row, icon, text, badge):
            widget.bind("<Button-1>", lambda _, key=key: self.on_select(key))
            widget.bind("<Enter>", lambda _, key=key: self.hover(key, True))
            widget.bind("<Leave>", lambda _, key=key: self.hover(key, False))

    def footer(self, variable):
        tk.Label(self, textvariable=variable, background=NAV, foreground="#7d869c", font=("Segoe UI", 8),
                 justify="left", wraplength=px(170), anchor="w").pack(side="bottom", fill="x", padx=px(18), pady=px(14))

    def hover(self, key, inside):
        if key == self.active:
            return
        colour = "#252b3b" if inside else NAV
        self.paint(key, colour, NAV_TEXT, NAV)

    def paint(self, key, background, foreground, bar):
        row, bar_widget, icon, text, badge = self.items[key]
        for widget in (row, icon, text):
            widget.configure(background=background)
        icon.configure(foreground=foreground)
        text.configure(foreground=foreground)
        bar_widget.configure(background=bar)
        if not badge.cget("text"):
            badge.configure(background=background)

    def select(self, key):
        if self.active:
            self.paint(self.active, NAV, NAV_TEXT, NAV)
        self.active = key
        self.paint(key, NAV_ACTIVE, "#ffffff", ACCENT)

    def badge(self, key, text, kind=None):
        badge = self.items[key][4]
        if text:
            badge.configure(text=text, background=STATES.get(kind, STATES["idle"])[0])
        else:
            row_bg = NAV_ACTIVE if key == self.active else NAV
            badge.configure(text="", background=row_bg)


def scrolled_tree(parent, columns, height=None, show="tree headings", stretch=None):
    """A Treeview with a vertical scrollbar in a white frame. columns: [(key, title, width, anchor)].
    stretch: the one column that takes spare width (default: all of them)."""
    frame = ttk.Frame(parent, style="Card.TFrame")
    keys = [key for key, *_ in columns if key != "#0"]
    tree = ttk.Treeview(frame, columns=keys, show=show, selectmode="browse", **({"height": height} if height else {}))
    for key, title, width, anchor in columns:
        tree.heading(key, text=title, anchor="w")
        tree.column(key, width=px(width), minwidth=px(50), anchor=anchor,
                    stretch=stretch is None or key == stretch)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    for kind, (fg, _) in STATES.items():
        tree.tag_configure(kind, foreground=fg)
    tree.tag_configure("needs", foreground=STATES["needs"][0], font=("Segoe UI", 10, "bold"))
    tree.tag_configure("muted", foreground=FAINT)
    tree.tag_configure("odd", background="#fafbfc")
    return frame, tree
