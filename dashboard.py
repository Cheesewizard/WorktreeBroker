"""The broker dashboard: the shared Unity editor, the AI chats and the machine at a glance. Run with pythonw.exe.

Layout: a docked page rail on the left (Overview, Worktrees, Activity, Machine, Usage), a header
with status chips, and a status bar. Overview answers "is the editor free, who is using it, does main compile,
which chats need me, is there memory to spare" on one screen. Activity is the broker's call log and the editor
guard's actions, newest first, with slow, failed and cut-off calls marked (editor_holder.py's limits).

Data comes from the same places as before: broker_state.snapshot (worktrees, chats, windows; every 2 s),
editor_state.read (the editor, lease, holder, compile; every 5 s on its own thread, since an unfocused editor
answers slowly), activity.jsonl / editor-guard.jsonl (Activity; every 3 s) and performance.sample (CPU and memory; every 5 s while
Overview or Machine is showing). Nothing here starts a console program, so nothing flashes (README, Troubleshooting).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import time

import tkinter as tk
from tkinter import ttk

import dashboard_ui as ui
import editor_state
from broker_state import EDITOR_FILES, HERE, PROJECT_NAME, routes as configured_routes, REPOSITORY, STATE_PRIORITY, ai_status, read_json, snapshot, worktrees
from session_control import inspect, pause, recover_next, resume, session_host
from windows import confirm_focus, jump_to_window

GIB = 1024 ** 3
ACTIVE_STATES = {"Needs you", "Working", "Working? (quiet)", "Your turn", "Connected"}
RECENT_SECONDS = 6 * 3600  # Overview also lists chats that stopped within this window
IDLE_WORKTREE_SECONDS = 24 * 3600
SLOW_CALL_SECONDS = 60


def ago(timestamp, now=None):
    if not timestamp:
        return "—"
    seconds = max(0, int((now or time.time()) - timestamp))
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return datetime.fromtimestamp(timestamp).strftime("%d %b %H:%M")


def span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def tail_jsonl(path, max_bytes=262144):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if size > max_bytes:
        lines = lines[1:]  # the first line is probably cut
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.fonts = ui.apply(root)
        self.rows = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.controls = ThreadPoolExecutor(max_workers=1)
        self.performance_executor = ThreadPoolExecutor(max_workers=1)
        self.editor_executor = ThreadPoolExecutor(max_workers=1)
        self.editor_future = self.future = self.performance_future = self.control_task = None
        self.recovery_queue, self.recovering = [], None
        self.sort_column, self.sort_reverse = "ai", False
        self.performance_sort, self.performance_reverse = "ram", True
        self.last_sample = None
        self.connected = bool(os.environ.get("BROKER_CODEX_URL"))
        self.player_routes = {
            str(Path(route["worktree"]).resolve()): route
            for route in configured_routes() if route["kind"] == "player"
        }
        self.worktree_cache, self.cache_until = [], 0
        self.editor, self.editor_until, self.play_since = None, 0, None
        self.updated_at, self.read_seconds = None, 0
        self.message = tk.StringVar(value=f"Discovering {PROJECT_NAME} worktrees…")
        self.freshness = tk.StringVar(value="Waiting for the first refresh…")
        self.show_idle = tk.BooleanVar(value=False)
        self.search = tk.StringVar()
        self.activity_filter = tk.StringVar(value="All calls")
        self.usage_period = tk.StringVar(value="Today")
        self.usage_executor = ThreadPoolExecutor(max_workers=1)
        self.usage_future, self.usage_data = None, None

        root.title(f"Worktree Broker for Unity  ·  {PROJECT_NAME}")
        root.geometry(f"{ui.px(1240)}x{ui.px(780)}")
        root.minsize(ui.px(980), ui.px(600))
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.rail = ui.NavRail(root, self.fonts["icons"], self.show_page, PROJECT_NAME)
        self.rail.pack(side="left", fill="y")
        main = ttk.Frame(root)
        main.pack(side="left", fill="both", expand=True)
        self.build_header(main)
        self.build_status_bar(main)
        self.stack = ttk.Frame(main, padding=(ui.px(24), ui.px(4), ui.px(24), ui.px(12)))
        self.stack.pack(fill="both", expand=True)
        self.pages = {}
        for key, label, glyph, fallback, builder in (
            ("overview", "Overview", "", "⌂", self.build_overview),
            ("worktrees", "Worktrees", "", "▤", self.build_worktrees),
            ("activity", "Activity", "", "↻", self.build_activity),
            ("machine", "Machine", "", "▦", self.build_machine),
            ("usage", "Usage", "", "Σ", self.build_usage),
        ):
            page = ttk.Frame(self.stack)
            self.pages[key] = page
            self.rail.add(key, label, glyph, fallback)
            builder(page)
        self.rail.footer(self.freshness)
        self.page = None
        self.show_page("overview")

        self.update_buttons()
        self.poll()
        self.poll_editor()
        self.poll_activity()
        self.poll_performance()
        self.poll_usage()
        self.tick()

    # ---------------------------------------------------------------- frame

    def build_header(self, parent):
        header = ttk.Frame(parent, padding=(ui.px(24), ui.px(18), ui.px(24), ui.px(10)))
        header.pack(fill="x")
        self.page_title = ttk.Label(header, text="", style="PageTitle.TLabel")
        self.page_title.pack(side="left")
        chips = ttk.Frame(header)
        chips.pack(side="right")
        self.chip_memory = ui.Pill(chips, "RAM …")
        self.chip_memory.pack(side="right", padx=(ui.px(6), 0))
        self.chip_cpu = ui.Pill(chips, "CPU …")
        self.chip_cpu.pack(side="right", padx=(ui.px(6), 0))
        self.chip_chats = ui.Pill(chips, "Chats …")
        self.chip_chats.pack(side="right", padx=(ui.px(6), 0))
        self.chip_editor = ui.Pill(chips, "Editor …")
        self.chip_editor.pack(side="right", padx=(ui.px(6), 0))
        for chip, page in ((self.chip_editor, "overview"), (self.chip_chats, "worktrees"), (self.chip_memory, "machine"),
                           (self.chip_cpu, "machine")):
            chip.configure(cursor="hand2")
            chip.bind("<Button-1>", lambda _, page=page: self.show_page(page))

    def build_status_bar(self, parent):
        bar = tk.Frame(parent, background=ui.CARD, highlightthickness=1, highlightbackground=ui.BORDER)
        bar.pack(side="bottom", fill="x")
        tk.Label(bar, textvariable=self.message, background=ui.CARD, foreground=ui.MUTED, anchor="w",
                 font=(self.fonts["ui"], 9), padx=ui.px(24), pady=ui.px(6)).pack(side="left", fill="x", expand=True)

    def show_page(self, key):
        if self.page == key:
            return
        if self.page:
            self.pages[self.page].pack_forget()
        self.page = key
        self.pages[key].pack(fill="both", expand=True)
        self.rail.select(key)
        self.page_title.configure(text={"overview": "Overview", "worktrees": "Worktrees", "activity": "Activity",
                                        "machine": "Machine", "usage": "Token usage"}[key])
        if key in {"overview", "machine"}:
            self.poll_performance()
        if key == "usage":
            self.poll_usage(reschedule=False)

    # ---------------------------------------------------------------- overview

    def build_overview(self, page):
        page.columnconfigure(0, weight=3, uniform="overview")
        page.columnconfigure(1, weight=2, uniform="overview")
        page.rowconfigure(1, weight=1)

        editor = ui.Card(page, "Shared editor")
        editor.grid(row=0, column=0, sticky="nsew", padx=(0, ui.px(12)), pady=(0, ui.px(12)))
        self.editor_pill = ui.Pill(editor.header, "Checking…")
        self.editor_pill.pack(side="left", padx=(ui.px(10), 0))
        self.free_button = ttk.Button(editor.header, text="Free the editor", style="Danger.TButton",
                                      command=self.free_editor, state="disabled")
        self.free_button.pack(side="right")
        self.editor_headline = ttk.Label(editor, text="", style="Big.TLabel")
        self.editor_headline.pack(anchor="w")
        grid = ttk.Frame(editor, style="Card.TFrame")
        grid.pack(fill="x", pady=(ui.px(12), 0))
        grid.columnconfigure(1, weight=1)
        self.editor_rows = {}
        for index, (key, caption) in enumerate((("running", "Running now"), ("lease", "Lease"),
                                                 ("used", "Last broker call"), ("guard", "Editor guard"))):
            ttk.Label(grid, text=caption, style="Caption.TLabel").grid(row=index * 2, column=0, sticky="nw",
                                                                        padx=(0, ui.px(16)), pady=(ui.px(6), 0))
            value = ttk.Label(grid, text="—", style="Value.TLabel", wraplength=ui.px(520), justify="left")
            value.grid(row=index * 2, column=1, sticky="w", pady=(ui.px(6), 0))
            meter = ui.Meter(grid, height=5, warn=0.6, bad=0.85)
            meter.grid(row=index * 2 + 1, column=1, sticky="ew", pady=(ui.px(4), ui.px(2)))
            meter.grid_remove()
            self.editor_rows[key] = (value, meter)
        grid.bind("<Configure>", lambda event: [value.configure(wraplength=max(ui.px(240), event.width - ui.px(150)))
                                                for value, _ in self.editor_rows.values()])

        compile_card = ui.Card(page, "Main checkout compile")
        compile_card.grid(row=0, column=1, sticky="nsew", pady=(0, ui.px(12)))
        self.compile_pill = ui.Pill(compile_card.header, "…")
        self.compile_pill.pack(side="left", padx=(ui.px(10), 0))
        self.compile_headline = ttk.Label(compile_card, text="", style="Value.TLabel", wraplength=ui.px(360), justify="left")
        self.compile_headline.pack(anchor="w")
        self.compile_files = ttk.Label(compile_card, text="", style="Error.TLabel", wraplength=ui.px(360), justify="left")
        self.compile_files.pack(anchor="w", pady=(ui.px(8), 0))
        compile_card.bind("<Configure>", lambda event: [label.configure(wraplength=max(ui.px(220), event.width - ui.px(8)))
                                                        for label in (self.compile_headline, self.compile_files)])

        chats = ui.Card(page, "AI chats")
        chats.grid(row=1, column=0, sticky="nsew", padx=(0, ui.px(12)))
        self.chats_count = ttk.Label(chats.header, text="", style="CardMuted.TLabel")
        self.chats_count.pack(side="right")
        frame, self.chat_tree = ui.scrolled_tree(chats, (
            ("state", "State", 120, "w"), ("chat", "Chat", 200, "w"), ("worktree", "Worktree", 130, "w"),
            ("doing", "Doing", 260, "w"), ("when", "Last activity", 110, "w")), show="headings", stretch="doing")
        frame.pack(fill="both", expand=True)
        self.chat_empty = ttk.Label(chats, text="No AI chat is active. Chats appear here as soon as they connect "
                                    "to the broker or a hook reports.", style="CardMuted.TLabel")
        self.chat_tree.bind("<Double-1>", self.open_chat_worktree)

        machine = ui.Card(page, "Machine headroom")
        machine.grid(row=1, column=1, sticky="nsew")
        self.machine_warning = ttk.Label(machine, text="", style="Error.TLabel", wraplength=ui.px(360), justify="left")
        self.overview_meters = {}
        for key in ("cpu", "ram", "commit", "gpu0", "gpu1"):
            row = ttk.Frame(machine, style="Card.TFrame")
            label = ttk.Label(row, text="", style="CardMuted.TLabel")
            label.pack(anchor="w")
            meter = ui.Meter(row, height=8)
            meter.pack(fill="x", pady=(ui.px(3), ui.px(10)))
            self.overview_meters[key] = (row, label, meter)
        self.room_line = ttk.Label(machine, text="Sampling…", style="Value.TLabel", wraplength=ui.px(360), justify="left")
        self.room_line.pack(side="bottom", anchor="w")
        # The Unity editors and players using that memory (the Machine page has the full table).
        self.unity_list = ttk.Label(machine, text="", style="CardMuted.TLabel", justify="left")
        self.unity_list.pack(side="bottom", anchor="w", fill="x", pady=(0, ui.px(12)))

    def show_editor(self):
        state = self.editor
        if state is None:
            return
        now = time.time()
        playing = state["reachable"] and state["play_mode"] in {"playing", "paused"}
        if playing and self.play_since is None:
            self.play_since = now
        elif not playing and state["reachable"]:
            self.play_since = None
        holder, held = state.get("holder"), state.get("lease")
        # A running broker call wins: the editor's main thread is busy with it, so it can't answer the status ping.
        if holder:
            word, kind, headline = "In use", "working", f"{holder['client']} is running {holder['tool']}."
        elif not state["reachable"]:
            word, kind, headline = "Not answering", "stopped", "The editor isn't answering: closed, busy or reloading."
        elif state["compiling"]:
            word, kind, headline = "Compiling", "warn", "Compiling scripts or reloading the domain."
        elif playing:
            minutes = int((now - self.play_since) // 60) if self.play_since else 0
            paused = " (paused)" if state["play_mode"] == "paused" else ""
            idle = now - max(state["last_use"] or 0, self.play_since or 0) > 600
            word, kind = ("In Play mode", "warn" if idle else "turn")
            headline = f"In Play mode{paused} for {minutes} min" + (", unused for 10+ min." if idle else ".")
        else:
            word, kind, headline = "Free", "ok", "Free: not in Play mode, nothing running."
        if held and kind == "ok":
            word, kind = "Leased", "turn"
        self.editor_pill.set(word, kind)
        self.chip_editor.set(f"Editor · {word}", kind)
        self.editor_headline.configure(text=headline)

        def row(key, text, fraction=None, colour=None):
            value, meter = self.editor_rows[key]
            value.configure(text=text)
            if fraction is None:
                meter.grid_remove()
            else:
                meter.grid()
                meter.set(fraction, colour)

        if holder:
            elapsed, limit = now - holder["since"], holder["limit"]
            worktree = Path(holder["worktree"]).name
            row("running", f"{holder['client']} in {worktree} · {holder['tool']} · {span(elapsed)} of its "
                           f"{span(limit)} limit (the broker stops it then)", elapsed / max(1, limit))
        else:
            row("running", "Nothing")
        if held:
            left = max(0, held["expires"] - now)
            total = max(1, held["expires"] - held.get("granted", held["expires"] - 600))
            row("lease", f"{held['holder']} · “{held['reason']}” · {span(left)} left", left / total, ui.ACCENT)
        else:
            row("lease", "Not leased")
        last_tool = (state["last_use_by"] or "").split()[-1:]  # "main read_console" -> read_console
        row("used", f"{ago(state['last_use'], now)} · {last_tool[0]}" if state["last_use"] and last_tool
            else ago(state["last_use"], now) if state["last_use"] else "No calls yet")
        guard = state.get("guard")
        if guard and now - guard.get("time", 0) < RECENT_SECONDS:
            what = {"stop": "Stopped Play mode", "warn": "Warned it will stop Play mode"}.get(
                guard.get("action"), "Turned run-in-background on")
            reason = guard.get("reason", "").split(":")[0]
            row("guard", f"{what} at {time.strftime('%H:%M', time.localtime(guard['time']))} ({reason})")
        else:
            row("guard", "Nothing in the last 6 hours")

        broken = state.get("compile")
        if broken:
            self.compile_pill.set("Broken", "error")
            self.compile_headline.configure(text="The editor runs the last good build. Another chat's uncommitted "
                                                 "file is the usual cause: leave it and tell whoever is editing it.")
            files = state.get("compile_files") or []
            self.compile_files.configure(text="\n".join(f"• {line}" for line in files[:6]) or broken)
        elif state.get("compiled_at"):
            self.compile_pill.set("Compiles", "ok")
            self.compile_headline.configure(text=f"Last compile {ago(state['compiled_at'], now)} had no errors.")
            self.compile_files.configure(text="")
        else:
            self.compile_pill.set("Unknown", "idle")
            self.compile_headline.configure(text="No compile recorded yet.")
            self.compile_files.configure(text="")
        self.free_button.configure(state="normal" if (playing or held) and self.control_task is None else "disabled")

    def all_chats(self):
        chats = []
        for row in self.rows.values():
            for chat in row["chats"]:
                chats.append(dict(chat, worktree=row["name"], worktree_path=row["path"]))
        return chats

    def fill_overview_chats(self):
        now = time.time()
        chats = [chat for chat in self.all_chats() if chat["state"] != "Closed" and (
            chat["id"].startswith("pid:") or now - chat["updated_at"] < RECENT_SECONDS)]
        chats.sort(key=lambda chat: (STATE_PRIORITY.index(chat["state"]) if chat["state"] in STATE_PRIORITY else 99,
                                     -chat["updated_at"]))
        self.chat_tree.delete(*self.chat_tree.get_children())
        for chat in chats:
            detail = chat["detail"] + (" · holds the editor lease" if chat["holds_lease"] else "")
            self.chat_tree.insert("", "end", iid="ov:" + chat["id"], values=(
                f"●  {chat['state']}", self.chat_name(chat), chat["worktree"], detail, ago(chat["updated_at"], now)),
                tags=(ui.state_key(chat["state"]),))
        if chats:
            self.chat_empty.pack_forget()
        else:
            self.chat_empty.pack(anchor="w", pady=(ui.px(10), 0))
        active = [chat for chat in chats if chat["state"] in ACTIVE_STATES]
        self.chats_count.configure(text=f"{len(active)} active · {len(chats) - len(active)} stopped recently")
        needs = sum(chat["state"] == "Needs you" for chat in chats)
        working = sum(chat["state"].startswith("Working") for chat in chats)
        if needs:
            self.chip_chats.set(f"{needs} need{'s' if needs == 1 else ''} you", "needs")
            self.rail.badge("worktrees", str(needs), "needs")
        elif working:
            self.chip_chats.set(f"{working} working", "working")
            self.rail.badge("worktrees", str(working), "working")
        else:
            self.chip_chats.set(f"{len(active)} chats" if active else "No active chats", "idle")
            self.rail.badge("worktrees", "")

    @staticmethod
    def chat_name(chat):
        """'Claude Code · 3f2a' or 'Codex · pid 4148': many chats share a client, so add a short id."""
        if chat["id"].startswith("pid:"):
            return f"{chat['label']} · pid {chat['id'][4:]}"
        return f"{chat['label']} · {chat['id'].split(':', 1)[-1][-4:]}"

    def open_chat_worktree(self, event):
        item = self.chat_tree.identify_row(event.y)
        chat = next((c for c in self.all_chats() if "ov:" + c["id"] == item), None)
        if chat:
            self.show_idle.set(True)
            self.show_page("worktrees")
            self.update_rows(list(self.rows.values()))
            target = "chat:" + chat["id"]
            if self.tree.exists(target):
                self.tree.see(target)
                self.tree.selection_set(target)

    # ---------------------------------------------------------------- worktrees

    def build_worktrees(self, page):
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, ui.px(10)))
        entry = ttk.Entry(toolbar, textvariable=self.search, width=34)
        entry.pack(side="left")
        entry.insert(0, "")
        self.search.trace_add("write", lambda *_: self.update_rows(list(self.rows.values())))
        ttk.Label(toolbar, text="  Filter by name or branch", style="Muted.TLabel").pack(side="left")
        ttk.Checkbutton(toolbar, text="Show idle worktrees", variable=self.show_idle,
                        command=lambda: self.update_rows(list(self.rows.values()))).pack(side="left", padx=(ui.px(18), 0))
        self.refresh_button = ttk.Button(toolbar, text="Refresh", command=self.refresh_now)
        self.refresh_button.pack(side="right")
        self.worktree_count = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.worktree_count.pack(side="right", padx=(0, ui.px(12)))

        card = ui.Card(page, padding=ui.px(1))
        card.pack(fill="both", expand=True)
        columns = (("#0", "Worktree / chat", 270, "w"), ("branch", "Branch, or what the chat is doing", 300, "w"),
                   ("ai", "State", 150, "w"), ("last_seen", "Last activity", 120, "w"),
                   ("window", "Unity window", 110, "w"))
        frame, self.tree = ui.scrolled_tree(card, columns, stretch="branch")
        frame.pack(fill="both", expand=True)
        self.column_titles = {key: title for key, title, *_ in columns}
        for key in self.column_titles:
            self.tree.heading(key, command=lambda column=key: self.sort_worktrees(column))
        self.show_sort_arrows()
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.selection_changed())
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<Return>", lambda _: self.jump())

        details = ui.Card(page, padding=ui.px(14))
        details.pack(fill="x", pady=(ui.px(12), 0))
        top = ttk.Frame(details, style="Card.TFrame")
        top.pack(fill="x")
        self.detail_title = ttk.Label(top, text="Select a worktree or chat", style="CardTitle.TLabel")
        self.detail_title.pack(side="left")
        self.detail_pill = ui.Pill(top, "")
        self.detail_pill.pack(side="left", padx=(ui.px(10), 0))
        buttons = ttk.Frame(top, style="Card.TFrame")
        buttons.pack(side="right")
        self.jump_button = ttk.Button(buttons, text="Jump to window", style="Primary.TButton", command=self.jump)
        self.jump_button.pack(side="right")
        self.open_button = ttk.Button(buttons, text="Open folder", command=self.open_folder)
        self.open_button.pack(side="right", padx=(0, ui.px(6)))
        self.start_player_button = ttk.Button(buttons, text="Start player", command=self.start_player)
        self.start_player_button.pack(side="right", padx=(0, ui.px(6)))
        self.pause_button = ttk.Button(buttons, text="Pause AI", command=lambda: self.choose_session("pause"))
        self.resume_button = ttk.Button(buttons, text="Resume AI", command=lambda: self.choose_session("resume"))
        self.recover_button = ttk.Button(buttons, text="Recover all", command=self.recover_all)
        if self.connected:  # these need a Codex control host; hidden otherwise
            for button in (self.recover_button, self.resume_button, self.pause_button):
                button.pack(side="right", padx=(0, ui.px(6)))
        self.detail_text = ttk.Label(details, text="Double-click a row with a Unity window to bring it forward.",
                                     style="CardMuted.TLabel", justify="left")
        self.detail_text.pack(anchor="w", pady=(ui.px(8), 0))

    def is_idle(self, row, now):
        recent = any(chat["state"] in ACTIVE_STATES or now - chat["updated_at"] < IDLE_WORKTREE_SECONDS
                     for chat in row["chats"])
        return not (row.get("is_main") or row["windows"] or recent)

    def priority(self, row):
        state = ai_status(row["chats"]).split(" (")[0]
        rank = STATE_PRIORITY.index(state) if state in STATE_PRIORITY else len(STATE_PRIORITY)
        return (rank, -self.last_seen(row))

    def last_seen(self, row):
        return max((chat["updated_at"] for chat in row["chats"]), default=0)

    def worktree_sort_key(self, row):
        column = self.sort_column
        if column == "#0":
            return (not row.get("is_main"), row["name"].casefold())
        if column == "branch":
            return row["branch"].casefold()
        if column == "last_seen":
            return -self.last_seen(row)
        if column == "window":
            return (not row["windows"], ", ".join(item["kind"] for item in row["windows"]))
        return (not row.get("is_main"), *self.priority(row))

    def update_rows(self, rows):
        first = not self.rows
        self.rows = {row["path"]: row for row in rows}
        now = time.time()
        query = self.search.get().strip().casefold()
        shown = [row for row in rows if (self.show_idle.get() or not self.is_idle(row, now))
                 and (not query or query in row["name"].casefold() or query in row["branch"].casefold())]
        keep = {row["path"] for row in shown}
        for key in self.tree.get_children():
            if key not in keep:
                self.tree.delete(key)
        for index, row in enumerate(sorted(shown, key=self.worktree_sort_key, reverse=self.sort_reverse)):
            key = row["path"]
            status = ai_status(row["chats"])
            title = f"{row['name']}   ·   Main Unity" if row.get("is_main") else row["name"]
            values = (row["branch"], f"●  {status}" if row["chats"] else status, ago(self.last_seen(row), now),
                      ", ".join(item["kind"] for item in row["windows"]) or "—")
            tags = (ui.state_key(status) if row["chats"] else "muted",)
            if self.tree.exists(key):
                self.tree.item(key, text=title, values=values, tags=tags)
                if self.tree.index(key) != index:
                    self.tree.move(key, "", index)
            else:
                self.tree.insert("", index, iid=key, text=title, values=values, tags=tags)
            children = set(self.tree.get_children(key))
            for chat in sorted(row["chats"], key=lambda item: item["updated_at"], reverse=True):
                if chat["state"] == "Closed" and not self.show_idle.get():
                    continue
                child = "chat:" + chat["id"]
                children.discard(child)
                detail = chat["detail"] + (" · holds the editor lease" if chat["holds_lease"] else "")
                details = (detail, chat["state"], ago(chat["updated_at"], now), "")
                tag = (ui.state_key(chat["state"]),)
                if self.tree.exists(child):
                    self.tree.item(child, text=self.chat_name(chat), values=details, tags=tag)
                else:
                    self.tree.insert(key, "end", iid=child, text=self.chat_name(chat), values=details, tags=tag)
                    self.tree.item(key, open=chat["state"] in {"Needs you", "Working"} or row.get("is_main"))
            for child in children:
                self.tree.delete(child)
        hidden = len(rows) - len(shown)
        self.worktree_count.configure(text=f"{len(shown)} shown" + (f" · {hidden} idle hidden" if hidden else ""))
        if first:
            self.message.set(f"{len(rows)} worktrees. Needs you and Working sort to the top.")
        self.fill_overview_chats()
        self.selection_changed(quiet=True)

    def sort_worktrees(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, False
        self.show_sort_arrows()
        self.update_rows(list(self.rows.values()))

    def show_sort_arrows(self):
        for key, title in self.column_titles.items():
            arrow = ("  ▲" if self.sort_reverse else "  ▼") if key == self.sort_column else ""
            self.tree.heading(key, text=title + arrow)

    def selected_row(self):
        selected = self.tree.selection()
        if not selected or not self.tree.exists(selected[0]):
            return None
        return self.rows.get(self.tree.parent(selected[0]) or selected[0])

    def selected_chat(self):
        selected = self.tree.selection()
        row = self.selected_row()
        if not row or not selected[0].startswith("chat:"):
            return None
        return next((chat for chat in row["chats"] if chat["id"] == selected[0][5:]), None)

    def selected_windows(self):
        row = self.selected_row()
        return row["windows"] if row else []

    def selected_sessions(self):
        row = self.selected_row()
        if not row:
            return []
        chat = self.selected_chat()
        if chat is not None:
            return [chat["session"]] if chat.get("session") else []
        return row["sessions"]

    def selection_changed(self, quiet=False):
        row, chat = self.selected_row(), self.selected_chat()
        if row is None:
            self.detail_title.configure(text="Select a worktree or chat")
            self.detail_pill.configure(text="", background=ui.CARD)
            self.detail_text.configure(text="Double-click a row with a Unity window to bring it forward.")
        elif chat is not None:
            self.detail_title.configure(text=f"{self.chat_name(chat)} in {row['name']}")
            self.detail_pill.set(chat["state"], ui.state_key(chat["state"]))
            lines = [chat["detail"], f"Last activity {ago(chat['updated_at'])}"]
            if chat["holds_lease"]:
                lines.append("Holds the editor lease")
            self.detail_text.configure(text="   ·   ".join(lines))
        else:
            status = ai_status(row["chats"])
            self.detail_title.configure(text=row["name"] + ("  (Main Unity)" if row.get("is_main") else ""))
            self.detail_pill.set(status, ui.state_key(status) if row["chats"] else "idle")
            windows = ", ".join(f"{item['kind']} (PID {item['pid']})" for item in row["windows"]) or "no Unity window"
            self.detail_text.configure(text=f"{row['path']}   ·   {row['branch']}   ·   {windows}")
        if not quiet and row is not None:
            self.message.set(row["path"])
        self.update_buttons()

    def update_buttons(self):
        row = self.selected_row()
        idle = self.control_task is None
        self.jump_button.configure(state="normal" if self.selected_windows() else "disabled")
        self.open_button.configure(state="normal" if row else "disabled")
        can_start = idle and row and row["path"] in self.player_routes and not any(
            item["kind"] == "Player" for item in row["windows"])
        self.start_player_button.configure(state="normal" if can_start else "disabled")
        self.refresh_button.configure(state="normal" if idle else "disabled")
        ready = self.connected and idle
        has_sessions = bool(self.selected_sessions())
        self.pause_button.configure(state="normal" if ready and has_sessions else "disabled")
        self.resume_button.configure(state="normal" if ready and has_sessions else "disabled")
        self.recover_button.configure(state="normal" if ready and not self.recovery_queue and not self.recovering
                                      and any(r["sessions"] for r in self.rows.values()) else "disabled")
        if self.editor is not None:
            self.show_editor()

    def open_folder(self):
        row = self.selected_row()
        if row:
            os.startfile(row["path"])  # Explorer, not a console program

    # ---------------------------------------------------------------- activity

    def build_activity(self, page):
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, ui.px(10)))
        box = ttk.Combobox(toolbar, textvariable=self.activity_filter, state="readonly", width=24,
                           values=("All calls", "Editor calls", "Slow, failed or cut off", "Editor guard"))
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _: self.poll_activity(reschedule=False))
        self.activity_summary = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.activity_summary.pack(side="left", padx=(ui.px(16), 0))
        card = ui.Card(page, padding=ui.px(1))
        card.pack(fill="both", expand=True)
        frame, self.activity_tree = ui.scrolled_tree(card, (
            ("time", "Time", 90, "w"), ("who", "Chat", 130, "w"), ("worktree", "Worktree", 170, "w"),
            ("tool", "Tool or event", 200, "w"), ("took", "Took", 80, "e"), ("result", "Result", 300, "w")),
            show="headings", stretch="result")
        frame.pack(fill="both", expand=True)
        ttk.Label(page, text="Every broker call is logged (no arguments or results). Calls are cut off at their "
                  "limit: 4 min by default, longer for tests, builds and refreshes.", style="Muted.TLabel").pack(
            anchor="w", pady=(ui.px(8), 0))

    def poll_activity(self, reschedule=True):
        try:
            self.fill_activity()
        except Exception as error:  # noqa: BLE001 - shown, never fatal
            self.activity_summary.configure(text=f"Couldn't read the log: {error}")
        if reschedule:
            self.root.after(3000, self.poll_activity)

    def fill_activity(self):
        import activity
        import editor_holder
        from presence import client_label

        calls = tail_jsonl(activity.LOG)
        guard = tail_jsonl(EDITOR_FILES / "editor-guard.jsonl", 65536)
        now = time.time()
        events = []
        for call in calls:
            limit = editor_holder.limit_for(call.get("tool", ""))
            seconds = call.get("seconds", 0)
            if call.get("ok"):
                result, kind = ("Slow" if seconds >= SLOW_CALL_SECONDS else "OK"), (
                    "warn" if seconds >= SLOW_CALL_SECONDS else "ok")
            elif seconds >= limit - 1:
                result, kind = "Cut off at its limit: the editor didn't answer", "error"
            else:
                reason = (call.get("error") or "").split("\n")[0]
                result, kind = (f"Failed: {reason}" if reason else "Failed"), "error"
            editor_call = call.get("tool") not in activity.NON_EDITOR
            events.append({"time": call.get("time", 0), "who": client_label(call.get("client")) if call.get("client")
                           else call.get("route", "—"), "worktree": Path(call.get("worktree") or "").name or "—",
                           "tool": call.get("tool", "?"), "took": span(seconds) if seconds >= 1 else f"{seconds:.2f} s",
                           "result": result, "kind": kind, "editor": editor_call, "guard": False})
        for item in guard:
            what = {"stop": "Stopped Play mode", "warn": "Warned it will stop Play mode"}.get(
                item.get("action"), "Turned run-in-background on")
            events.append({"time": item.get("time", 0), "who": "Editor guard", "worktree": "Main Unity",
                           "tool": what, "took": "", "result": item.get("reason", ""), "kind": "warn",
                           "editor": True, "guard": True})
        events.sort(key=lambda event: event["time"], reverse=True)
        mode = self.activity_filter.get()
        shown = [event for event in events if mode == "All calls"
                 or mode == "Editor calls" and event["editor"] and not event["guard"]
                 or mode == "Slow, failed or cut off" and event["kind"] in {"warn", "error"} and not event["guard"]
                 or mode == "Editor guard" and event["guard"]][:500]
        tree = self.activity_tree
        tree.delete(*tree.get_children())
        for index, event in enumerate(shown):
            stamp = datetime.fromtimestamp(event["time"])
            when = stamp.strftime("%H:%M:%S") if now - event["time"] < 86400 else stamp.strftime("%d %b %H:%M")
            tags = [event["kind"] if event["kind"] != "ok" else "muted" if not event["editor"] else ""]
            tree.insert("", "end", values=(when, event["who"], event["worktree"], event["tool"], event["took"],
                                           event["result"]), tags=[tag for tag in tags if tag])
        hour = [event for event in events if now - event["time"] < 3600 and not event["guard"]]
        problems = [event for event in hour if event["kind"] == "error"]
        slowest = max((call for call in calls if now - call.get("time", 0) < 3600), key=lambda call: call.get("seconds", 0),
                      default=None)
        parts = [f"Last hour: {len(hour)} calls", f"{len(problems)} failed or cut off"]
        if slowest and slowest.get("seconds", 0) >= 1:
            parts.append(f"longest {span(slowest['seconds'])} ({slowest.get('tool')})")
        self.activity_summary.configure(text="  ·  ".join(parts))
        self.rail.badge("activity", str(len(problems)) if problems else "", "error")

    # ---------------------------------------------------------------- usage

    USAGE_PERIODS = {"Today": 1, "Last 2 days": 2, "Last 7 days": 7}

    def build_usage(self, page):
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, ui.px(10)))
        box = ttk.Combobox(toolbar, textvariable=self.usage_period, state="readonly", width=14,
                           values=tuple(self.USAGE_PERIODS))
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _: self.render_usage())
        self.usage_summary = ttk.Label(toolbar, text="Reading transcripts (the first read of a week takes a minute or two)",
                                       style="Muted.TLabel")
        self.usage_summary.pack(side="left", padx=(ui.px(16), 0))
        top = ttk.Frame(page)
        top.pack(fill="x", pady=(0, ui.px(12)))
        top.columnconfigure((0, 1, 2), weight=1, uniform="usage")
        self.usage_cards = {}
        for column, (key, title) in enumerate((("total", "Tokens in this period"), ("claude", "Claude plan limit"),
                                               ("codex", "Codex plan limit"))):
            card = ui.Card(top, title)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 12, 0))
            big = ttk.Label(card, text="—", style="CardTitle.TLabel")
            big.pack(anchor="w")
            text = ttk.Label(card, text="", style="CardMuted.TLabel", wraplength=ui.px(250), justify="left")
            text.pack(anchor="w", pady=(ui.px(4), 0))
            meter = ui.Meter(card, height=10)
            if key != "total":
                meter.pack(fill="x", pady=(ui.px(8), 0))
            self.usage_cards[key] = (big, text, meter)
        card = ui.Card(page, "By worktree", padding=ui.px(1))
        card.pack(fill="x", pady=(0, ui.px(12)))
        frame, self.usage_worktrees = ui.scrolled_tree(card, (
            ("worktree", "Worktree", 260, "w"), ("total", "Total", 90, "e"), ("input", "New input", 90, "e"),
            ("write", "Cache writes", 100, "e"), ("read", "Cache reads", 100, "e"), ("output", "Output", 80, "e"),
            ("chats", "Chats", 60, "e")), height=3, show="headings", stretch="worktree")
        frame.pack(fill="both", expand=True)
        card = ui.Card(page, "By chat", padding=ui.px(1))
        card.pack(fill="both", expand=True)
        frame, self.usage_chats = ui.scrolled_tree(card, (
            ("chat", "Chat", 210, "w"), ("worktree", "Worktree", 110, "w"),
            ("total", "Total", 70, "e"), ("write", "Cache writes", 90, "e"), ("output", "Output", 65, "e"),
            ("images", "Images", 60, "e"), ("calls", "Calls", 50, "e"), ("last", "Last call", 105, "w")),
            show="headings", stretch="chat")
        frame.pack(fill="both", expand=True)
        ttk.Label(page, text=f"Exact counts from Claude Code's and Codex's own transcripts ({PROJECT_NAME} folders only; "
                  "subagents count toward their chat). Cache reads are cheap; new input, cache writes and output are "
                  "what use a plan up. Claude logs only the moment a limit is hit, not how much of it is used.",
                  style="Muted.TLabel", wraplength=ui.px(900), justify="left").pack(anchor="w", pady=(ui.px(8), 0))

    def usage_paths(self):
        return [row["path"] for row in self.worktree_cache] or [str(REPOSITORY)]

    def poll_usage(self, reschedule=True):
        if self.usage_future is None:
            import usage
            self.usage_future = self.usage_executor.submit(usage.refresh, self.usage_paths())
            self.root.after(300, self.finish_usage)
        if reschedule:
            self.root.after(60000, self.poll_usage)

    def finish_usage(self):
        if not self.usage_future.done():
            self.root.after(300, self.finish_usage)
            return
        try:
            self.usage_data = self.usage_future.result()
            self.render_usage()
        except Exception as error:  # noqa: BLE001 - shown, never fatal
            self.usage_summary.configure(text=f"Couldn't read the transcripts: {error}")
        self.usage_future = None

    @staticmethod
    def iso_time(stamp):
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            return None

    def render_usage(self):
        import usage
        if self.usage_data is None:
            return
        paths = self.usage_paths()
        report = usage.summarize(self.usage_data, paths, self.USAGE_PERIODS.get(self.usage_period.get(), 1))
        short = usage.short
        main = str(REPOSITORY).lower()

        def name(path):
            # Codex worktrees all end in ...\<id>\<project>, so name them by their folder: "codex 857f".
            path = Path(str(path))
            if str(path).lower() == main:
                return f"{PROJECT_NAME} (main)"
            if path.name.lower() == REPOSITORY.name.lower() and path.parent.name:
                return ("codex " if ".codex" in str(path).lower() else "") + path.parent.name
            return path.name or str(path)

        now = time.time()
        totals = [sum(sums[index] for sums in report["worktrees"].values()) for index in range(4)]
        big, text, meter = self.usage_cards["total"]
        big.configure(text=short(sum(totals)))
        text.configure(text=f"{short(totals[0] + totals[1] + totals[3])} without cache reads\n"
                            f"input {short(totals[0])} · cache writes {short(totals[1])}\noutput {short(totals[3])}")

        big, text, meter = self.usage_cards["claude"]
        claude = report["limits"].get("claude")
        if claude:
            resets = claude.get("resetsAt") or 0
            kind = (claude.get("rateLimitType") or "plan").replace("_", "-")
            if resets > now:
                big.configure(text=f"{kind} limit reached")
                text.configure(text=f"resets {datetime.fromtimestamp(resets).strftime('%H:%M')} (in {span(resets - now)})")
                meter.set(1.0)
            else:
                hit = self.iso_time(claude.get("at"))
                big.configure(text="Not at a limit now")
                text.configure(text=f"last hit the {kind} limit {ago(hit, now) if hit else ''}".strip())
                meter.set(0.0)
        else:
            big.configure(text="No limit hit")
            text.configure(text="No limit hit in this week's transcripts.")
            meter.set(0.0)

        big, text, meter = self.usage_cards["codex"]
        codex = report["limits"].get("codex") or {}
        primary = codex.get("primary") or {}
        if primary:
            used = float(primary.get("used_percent") or 0)
            window = int(primary.get("window_minutes") or 0)
            label = "weekly" if window >= 7 * 1440 else f"{window // 60}-hour" if window else "plan"
            big.configure(text=f"{used:.0f}% of the {label} limit")
            resets = primary.get("resets_at") or 0
            seen = self.iso_time(codex.get("at"))
            text.configure(text=f"resets {datetime.fromtimestamp(resets).strftime('%a %H:%M') if resets else '?'}"
                                f"  ·  read {ago(seen, now) if seen else '?'}")
            meter.set(used / 100)
        else:
            big.configure(text="—")
            text.configure(text="No Codex limit reading this week.")
            meter.set(0.0)

        counts = {}
        for row in report["chats"]:
            counts[row["worktree"]] = counts.get(row["worktree"], 0) + 1
        tree = self.usage_worktrees
        tree.delete(*tree.get_children())
        for path, sums in sorted(report["worktrees"].items(), key=lambda item: -sum(item[1])):
            tree.insert("", "end", values=(name(path), short(sum(sums)), short(sums[0]), short(sums[1]),
                                           short(sums[2]), short(sums[3]), counts.get(path, 0)))
        tree = self.usage_chats
        tree.delete(*tree.get_children())
        for row in report["chats"][:300]:
            title = row["title"] or row["session"][:8]
            if row["tool"] == "codex":
                title = ("codex exec " if row["title"] == "codex_exec" else "Codex ") + row["session"][-6:]
            if row["subagent_tokens"]:
                title += f"  (subagents {short(row['subagent_tokens'])})"
            last = self.iso_time(row["last"])
            tree.insert("", "end", values=(title, name(row["worktree"]), short(row["total"]),
                                           short(row["sums"][1]), short(row["sums"][3]), row["images"] or "",
                                           row["calls"], ago(last, now) if last else ""))
        scanned = report.get("scanned_at")
        self.usage_summary.configure(text=f"{len(report['chats'])} chats since {report['since']}  ·  read "
                                          f"{ago(scanned, now) if scanned else '—'}  ·  refreshes every minute")

    # ---------------------------------------------------------------- machine

    def build_machine(self, page):
        self.machine_banner = tk.Label(page, text="", anchor="w", justify="left", padx=ui.px(14), pady=ui.px(10),
                                       font=(self.fonts["ui"], 10, "bold"))
        self.machine_banner.bind("<Configure>", lambda event: self.machine_banner.configure(
            wraplength=max(ui.px(300), event.width - ui.px(30))))
        top = self.machine_top = ttk.Frame(page)
        top.pack(fill="x")
        top.columnconfigure((0, 1, 2, 3), weight=1, uniform="machine")
        self.machine_cards = {}
        for column, (key, title) in enumerate((("cpu", "CPU"), ("memory", "Memory"), ("gpu0", "GPU"), ("gpu1", "GPU"))):
            card = ui.Card(top, title)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 12, 0))
            fields = []
            for _ in range(2):
                label = ttk.Label(card, text="—", style="CardMuted.TLabel")
                label.pack(anchor="w")
                meter = ui.Meter(card, height=10)
                meter.pack(fill="x", pady=(ui.px(3), ui.px(12)))
                fields.append((label, meter))
            self.machine_cards[key] = (card, fields)
        self.capacity = ttk.Label(page, text="", style="TLabel", font=(self.fonts["ui"], 11, "bold"),
                                  wraplength=ui.px(900), justify="left")
        self.capacity.pack(anchor="w", fill="x", pady=(ui.px(16), ui.px(8)))
        self.capacity.bind("<Configure>", lambda event: self.capacity.configure(wraplength=event.width))
        card = ui.Card(page, padding=ui.px(1))
        card.pack(fill="both", expand=True)
        columns = (("#0", "Worktree / project", 250, "w"), ("kind", "Process", 110, "w"), ("pid", "PID", 80, "w"),
                   ("cpu", "CPU with helpers", 120, "e"), ("ram", "RAM with helpers", 140, "e"), ("helpers", "Helpers", 130, "e"),
                   ("gpu", "GPU memory", 110, "e"), ("note", "Note", 150, "w"))
        frame, self.performance_table = ui.scrolled_tree(card, columns, stretch="#0")
        frame.pack(fill="both", expand=True)
        self.performance_titles = {key: title for key, title, *_ in columns}
        for key in self.performance_titles:
            self.performance_table.heading(key, command=lambda column=key: self.sort_performance(column))
        self.show_performance_arrows()
        self.consumers = ttk.Label(page, text="", style="Muted.TLabel", wraplength=ui.px(900), justify="left")
        self.consumers.pack(anchor="w", pady=(ui.px(8), 0))

    def poll_performance(self):
        if self.performance_future is not None or self.page not in {"overview", "machine"}:
            return
        from performance import sample

        self.performance_future = self.performance_executor.submit(sample, list(self.rows.values()))
        self.root.after(150, self.finish_performance)

    def finish_performance(self):
        if not self.performance_future.done():
            self.root.after(150, self.finish_performance)
            return
        try:
            self.render_performance(self.performance_future.result())
        except Exception as error:  # noqa: BLE001
            self.room_line.configure(text=f"Memory sample failed: {error}")
        self.performance_future = None
        self.root.after(5000, self.poll_performance)

    def render_performance(self, sample):
        total, free = sample["total_ram"] / GIB, sample["available_ram"] / GIB
        used = total - free
        low = free < 2.0
        ram_colour = ui.BAD if low else ui.WARN if free < 4.0 else ui.GOOD
        ram_text = f"RAM  {used:.1f} of {total:.1f} GiB  ·  {free:.2f} GiB free"
        system = sample["system"]
        commit_text, commit_fraction = "Windows commit unavailable", 0
        if system:
            commit_fraction = system["committed"] / max(1, system["commit_limit"])
            commit_text = f"Commit  {system['committed'] / GIB:.0f} of {system['commit_limit'] / GIB:.0f} GiB"
        cpu = sample.get("cpu")
        cpu_text = (f"CPU  {cpu:.0f}% of {sample['logical_cpus']} logical processors" if cpu is not None
                    else "CPU  measuring…")
        meters = [("cpu", cpu_text, (cpu or 0) / 100, None), ("ram", ram_text, used / max(1, total), ram_colour),
                  ("commit", commit_text, commit_fraction, None)]
        for index, adapter in enumerate(sample["gpus"][:2]):
            usage = adapter["usage"] or {}
            # An integrated GPU has a token dedicated carve-out and really uses shared memory: show that instead.
            integrated = adapter["dedicated_max"] < 2 * GIB
            field, capacity_bytes = (("Shared Usage", adapter["shared_max"]) if integrated
                                     else ("Dedicated Usage", adapter["dedicated_max"]))
            value = usage.get(field) or 0
            name = adapter["name"].replace("AMD Radeon(TM) ", "Radeon ")
            meters.append((f"gpu{index}", f"{name}  ·  {value / GIB:.1f} of {capacity_bytes / GIB:.1f} GiB "
                           f"{'shared' if integrated else 'dedicated'}", value / max(1, capacity_bytes), None))
        shown = set()
        for key, text, fraction, colour in meters:
            row, label, meter = self.overview_meters[key]
            label.configure(text=text)
            meter.set(fraction, colour)
            if not row.winfo_ismapped():
                row.pack(fill="x", before=self.room_line)
            shown.add(key)
        for key, (row, _, _) in self.overview_meters.items():
            if key not in shown:
                row.pack_forget()
        room = sample.get("room") or {}
        players, editor = room.get("players", 0), room.get("editor", False)
        capacity = (f"{room.get('editors_running', 0)} Unity editor{'s' if room.get('editors_running') != 1 else ''} "
                    f"running  ·  room for {players} more player{'s' if players != 1 else ''}  ·  "
                    f"{'room' if editor else 'no room'} for another editor  (keeping 2 GiB free)")
        self.room_line.configure(text=("No room for another editor" if not editor else "Room for another editor")
                                 + f" · {players} more player{'s' if players != 1 else ''}")
        if low:
            self.machine_warning.configure(text=f"Memory is low: {free:.1f} GiB free. Close an editor or a player "
                                                "before starting another.")
            if not self.machine_warning.winfo_ismapped():
                self.machine_warning.pack(anchor="w", fill="x", pady=(0, ui.px(10)),
                                          before=self.overview_meters["ram"][0])
        else:
            self.machine_warning.pack_forget()
        self.chip_memory.set(f"RAM {used / total:.0%}", "error" if low else "warn" if free < 4.0 else "ok")
        self.chip_cpu.set("CPU …" if cpu is None else f"CPU {cpu:.0f}%",
                          "ok" if cpu is None or cpu < 75 else "warn" if cpu < 90 else "error")
        self.rail.badge("machine", "!" if low else "", "error")

        # Machine page
        _, cpu_fields = self.machine_cards["cpu"]
        unity_cpu = [item["cpu"] for item in sample["targets"] if item.get("cpu") is not None]
        cpu_fields[0][0].configure(text=cpu_text.replace("CPU  ", "All processes  ", 1))
        cpu_fields[0][1].set((cpu or 0) / 100)
        cpu_fields[1][0].configure(text=f"Unity editors and players  {sum(unity_cpu):.0f}%" if unity_cpu
                                   else "Unity editors and players  —")
        cpu_fields[1][1].set(sum(unity_cpu) / 100 if unity_cpu else 0)
        _, memory_fields = self.machine_cards["memory"]
        memory_fields[0][0].configure(text=ram_text)
        memory_fields[0][1].set(used / max(1, total), ram_colour)
        memory_fields[1][0].configure(text=commit_text)
        memory_fields[1][1].set(commit_fraction)
        for index in range(2):
            card, fields = self.machine_cards[f"gpu{index}"]
            if index >= len(sample["gpus"]):
                card.outer.grid_remove()
                continue
            adapter = sample["gpus"][index]
            card.title.configure(text=adapter["name"].replace("AMD Radeon(TM) ", "Radeon "))
            usage = adapter["usage"] or {}
            for (label, meter), field, title, capacity_bytes in zip(
                    fields, ("Dedicated Usage", "Shared Usage"), ("Dedicated", "Shared"),
                    (adapter["dedicated_max"], adapter["shared_max"])):
                value = usage.get(field)
                label.configure(text=f"{title}  {value / GIB:.2f} of {capacity_bytes / GIB:.2f} GiB" if value is not None
                                else f"{title}  —")
                meter.set((value or 0) / max(1, capacity_bytes))
        if low:
            fg, bg = ui.STATES["error"]
            self.machine_banner.configure(text=f"Memory is low: {free:.2f} GiB free. Unity editors and players need "
                                               "about 5 GiB and 1 GiB. Close something before starting more.",
                                          foreground=fg, background=bg)
            self.machine_banner.pack(fill="x", pady=(0, ui.px(12)), before=self.machine_top)
        else:
            self.machine_banner.pack_forget()
        self.capacity.configure(text=capacity)
        top = "  ·  ".join(f"{name} {size / GIB:.1f} GiB" for name, size in sample["top"])
        kernel = (f"\nKernel pools  ·  {system['nonpaged'] / GIB:.2f} GiB nonpaged  ·  "
                  f"{system['paged_resident'] / GIB:.2f} GiB paged resident") if system else ""
        busiest = "  ·  ".join(f"{name} {share:.0f}%" for name, share in sample.get("top_cpu", []))
        busiest = f"\nBusiest process groups by CPU  ·  {busiest}" if busiest else ""
        self.consumers.configure(text=f"Largest process groups by working set  ·  {top}{busiest}{kernel}")
        lines = [f"{item['kind']}  ·  {Path(item['name']).name or item['name']}  ·  "
                 f"{item['ram'] / GIB:.1f} GiB" + ("  · not a worktree" if item.get("note") == "not a worktree"
                                                   else f"  ({item['note']})" if item.get("note") else "")
                 for item in sorted(sample["targets"], key=lambda item: -item["ram"])[:5]]
        self.unity_list.configure(text=chr(10).join(["Unity processes", *lines]) if lines
                                  else "No Unity editor or player is running.")
        self.last_sample = sample
        self.fill_performance_table()

    def fill_performance_table(self):
        if not self.last_sample:
            return
        keys = {"#0": lambda item: item["name"].casefold(), "kind": lambda item: item["kind"].casefold(),
                "pid": lambda item: item["pid"], "cpu": lambda item: item.get("cpu") or 0, "ram": lambda item: item["ram"],
                "helpers": lambda item: item.get("helper_ram", 0), "gpu": lambda item: item["gpu"] or 0,
                "note": lambda item: item.get("note", "")}
        ordered = sorted(self.last_sample["targets"], key=keys[self.performance_sort], reverse=self.performance_reverse)
        table = self.performance_table
        table.delete(*table.get_children())
        for item in ordered:
            gpu = f"{item['gpu'] / GIB:.2f} GiB" if item["gpu"] is not None else "—"
            helpers = f"{item['helpers']}  ·  {item['helper_ram'] / GIB:.2f} GiB" if item.get("helpers") else "—"
            cpu = f"{item['cpu']:.1f}%" if item.get("cpu") is not None else "—"
            table.insert("", "end", text=item["name"], values=(
                item["kind"], item["pid"], cpu, f"{item['ram'] / GIB:.2f} GiB", helpers, gpu, item.get("note", "")),
                tags=("warn",) if item.get("note") else ())

    def sort_performance(self, column):
        if self.performance_sort == column:
            self.performance_reverse = not self.performance_reverse
        else:
            self.performance_sort, self.performance_reverse = column, column in {"cpu", "ram", "helpers", "gpu", "pid"}
        self.show_performance_arrows()
        self.fill_performance_table()

    def show_performance_arrows(self):
        for key, title in self.performance_titles.items():
            arrow = ("  ▼" if self.performance_reverse else "  ▲") if key == self.performance_sort else ""
            self.performance_table.heading(key, text=title + arrow)

    # ---------------------------------------------------------------- polling

    def read(self):
        started = time.monotonic()
        if started >= self.cache_until:
            self.worktree_cache = worktrees()
            self.cache_until = started + 10
        rows = snapshot(self.worktree_cache)
        self.read_seconds = time.monotonic() - started
        return rows

    def poll(self):
        if self.future is None:
            self.future = self.executor.submit(self.read)
        if not self.future.done():
            self.root.after(100, self.poll)
            return
        try:
            self.update_rows(self.future.result())
            self.updated_at = time.time()
        except Exception as error:  # noqa: BLE001
            self.message.set(f"Refresh failed: {error}")
        self.future = None
        self.root.after(2000, self.poll)

    def poll_editor(self):
        if self.editor_future is None and time.monotonic() >= self.editor_until:
            self.editor_future = self.editor_executor.submit(editor_state.read)
        if self.editor_future is not None and self.editor_future.done():
            try:
                self.editor = self.editor_future.result()
                self.show_editor()
            except Exception as error:  # noqa: BLE001
                self.editor_headline.configure(text=f"Couldn't read the editor: {error}")
            self.editor_future = None
            self.editor_until = time.monotonic() + 5
        self.root.after(250, self.poll_editor)

    def tick(self):
        """Once a second: the freshness line and the running call's clock."""
        if self.updated_at:
            age = max(0, int(time.time() - self.updated_at))
            slow = "  ·  slow refresh" if self.read_seconds > 3 else ""
            self.freshness.set(f"Updated {age} s ago\nRefresh took {self.read_seconds:.1f} s{slow}")
        if self.editor and self.editor.get("holder"):
            self.show_editor()
        self.root.after(1000, self.tick)

    # ---------------------------------------------------------------- actions

    def free_editor(self):
        def done(result):
            self.message.set(result)
            self.editor_until = 0

        self.submit(editor_state.stop_play_mode, done, local=True)

    def start_player(self):
        row = self.selected_row()
        if row is None or row["path"] not in self.player_routes:
            return
        root = Path(row["path"])
        route = self.player_routes[row["path"]]

        def launch():
            from broker import start_match
            from broker_state import RUNS

            run = start_match(root, route, RUNS / route["name"])
            label = f"Started player for {row['name']} · PID {run['pid']}"
            if "startup_patch_error" in run:
                return f"{label}. Startup patch failed: {run['startup_patch_error']}"
            if "startup_patch" in run:
                return f"{label}. Applied the worktree's configured C# and PNG patch."
            return label

        self.message.set(f"Starting player for {row['name']}…")
        self.submit(launch, local=True)

    def refresh_now(self):
        from session_refresh import refresh_sessions

        def refresh():
            try:
                return refresh_sessions()
            except RuntimeError as error:
                if "Codex CLI was not found" in str(error):
                    return "Refreshed. (Codex isn't installed here, so Codex chats keep their last hook state.)"
                raise

        self.cache_until = self.editor_until = 0
        self.message.set("Refreshing worktrees and Codex activity…")
        self.submit(refresh, local=True)

    def choose_session(self, action):
        sessions = self.selected_sessions()
        if len(sessions) == 1:
            self.submit(lambda client: self.act(client, sessions[0], action))
        elif sessions:
            menu = tk.Menu(self.root, tearoff=False)
            for session in sorted(sessions, key=lambda item: item["updated_at"], reverse=True):
                menu.add_command(label=f"{session['state']} · {session['id']}",
                                 command=lambda session=session: self.submit(
                                     lambda client: self.act(client, session, action)))
            menu.post(self.root.winfo_pointerx(), self.root.winfo_pointery())

    @staticmethod
    def act(client, session, action):
        if action == "pause":
            return pause(client, session)
        return "Resume sent to the original chat." if resume(client, session) else "No interrupted turn to resume."

    def submit(self, operation, on_result=None, *, local=False):
        def work():
            if local:
                return operation()
            with session_host() as client:
                return operation(client)

        if self.control_task is not None:
            return
        self.control_task = self.controls.submit(work)
        self.update_buttons()
        self.root.after(100, lambda: self.finish_control(on_result))

    def finish_control(self, on_result):
        if not self.control_task.done():
            self.root.after(100, lambda: self.finish_control(on_result))
            return
        task, self.control_task = self.control_task, None
        try:
            result = task.result()
            if on_result is not None:
                on_result(result)
            elif result:
                self.message.set(result)
        except Exception as error:  # noqa: BLE001
            self.recovery_queue, self.recovering = [], None
            self.message.set(f"Action failed: {error}")
        self.update_buttons()

    def recover_all(self):
        self.recovery_queue = [session for row in self.rows.values() for session in row["sessions"]]
        self.recover_step()

    def recover_step(self):
        if self.control_task is not None:
            self.root.after(2000, self.recover_step)
        elif self.recovering:
            session = self.recovering
            self.submit(lambda client: inspect(client, session), self.recovery_progress)
        elif self.recovery_queue:
            self.submit(lambda client: recover_next(client, self.recovery_queue), self.recovery_started)
        else:
            self.message.set("Recovery queue complete.")
            self.update_buttons()

    def recovery_started(self, result):
        session, errors = result
        if session is None:
            self.recovery_queue = []
            self.message.set("; ".join(errors) if errors else "No more interrupted chats to recover.")
            return
        self.recovering = session
        index = self.recovery_queue.index(session)
        del self.recovery_queue[:index + 1]
        self.message.set(f"Recovering {Path(session['worktree']).name} · {len(self.recovery_queue)} remaining to check")
        self.root.after(3000, self.recover_step)

    def recovery_progress(self, state):
        if state not in {"Working", "Pausing"}:
            self.recovering = None
        self.root.after(3000, self.recover_step)

    def double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.jump()

    def jump(self):
        windows = self.selected_windows()
        if len(windows) == 1:
            self.activate(windows[0])
        elif windows:
            menu = tk.Menu(self.root, tearoff=False)
            for window in windows:
                menu.add_command(label=f"{window['kind']} · {window['title']} · PID {window['pid']}",
                                 command=lambda window=window: self.activate(window))
            menu.post(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def activate(self, window):
        try:
            jump_to_window(window)
            self.root.after(200, lambda: self.show_jump_result(window))
        except Exception as error:  # noqa: BLE001
            self.message.set(str(error))

    def show_jump_result(self, window):
        if confirm_focus(window):
            self.message.set(f"Opened {window['kind']} · PID {window['pid']}")
        else:
            self.message.set("Windows kept another window focused. Check the target on the taskbar.")

    def close(self):
        for pool in (self.executor, self.controls, self.performance_executor, self.editor_executor):
            pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


if __name__ == "__main__":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except Exception:  # noqa: BLE001 - not Windows, or already set
        pass
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()
