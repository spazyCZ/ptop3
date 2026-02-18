#!/usr/bin/env python3
"""
ptop3 — htop-like TUI grouped by application.
Keys:
  ↑/↓/PgUp/PgDn/Home/End  Move selection
  Enter or l              Expand group / back with h
  t                       Toggle process tree view   [detail view]
  s                       Cycle sort: mem -> cpu -> rss -> swap -> io -> net -> count
  f / r                   Filter regex / reset
  + / -                   Refresh interval up/down
  k / K                   Kill (TERM / KILL) — process in detail, group in group view
  g                       Kill whole group (TERM)      [group view]
  w                       Run swap-clean (if root)
  d                       Run drop-caches (if root)
  q                       Quit

Passwordless sudo for w/d keys:
  sudo ptop3 --init-subscripts
  # or manually: sudo visudo -f /etc/sudoers.d/ptop3
"""
import argparse
import curses
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# --- Performance Caching & Modes ---
PID_CACHE: dict = {}
PID_CACHE_TTL = 30.0
LAST_CACHE_CLEAN = 0.0
SWAP_CACHE: dict = {}
SWAP_CACHE_TTL = 2.0

LITE_MODE = False

try:
    import psutil
except ImportError:
    print("Install dependency first:  pip install psutil", file=sys.stderr)
    sys.exit(1)

DEFAULT_REFRESH = 2.0
MIN_REFRESH, MAX_REFRESH = 1.0, 10.0
CPU_HOT = 100.0
RSS_HOT_MB = 800.0
SWAP_HOT_MB = 500.0
SWAP_CLEAN_SUGGEST_PCT = 80.0
SUDOERS_PATH = "/etc/sudoers.d/ptop3"

DISK_USAGE_CRITICAL = 95.0
DISK_USAGE_HIGH = 85.0
DISK_IO_HIGH_MBPS = 50.0
NETWORK_HIGH_MBPS = 100.0
LOAD_CRITICAL = 8.0
LOAD_HIGH = 4.0
TEMP_CRITICAL = 85.0
TEMP_HIGH = 70.0

SORT_KEYS = ("mem", "cpu", "rss", "swap", "io", "net", "count")
MAX_APP_NAME = 24

ALIASES = {
    "code-insiders": "vscode-insiders", "code": "vscode",
    "chromium": "chrome", "chromium-browse": "chrome", "chrome": "chrome",
    "firefox": "firefox", "web content": "firefox",
    "python3": "python", "python": "python",
    "java": "java", "cursor": "cursor",
    "gnome-shell": "gnome-shell", "xwayland": "Xwayland",
}
_ALIASES_BY_LEN = sorted(ALIASES.items(), key=lambda x: len(x[0]), reverse=True)
_CMD_WORDLIKE = r"[a-z0-9_-]"
_ALIASES_CMD_REGEX = [
    (re.compile(rf"(?<!{_CMD_WORDLIKE}){re.escape(k)}(?!{_CMD_WORDLIKE})"), v)
    for k, v in _ALIASES_BY_LEN
]
_CMD_ALIASES = [
    ("cloud-code", "cursor"),
    ("cloudcode", "cursor"),
    (".cursor/", "cursor"),
    ("/cursor/cursor", "cursor"),
    (".mount_cursor", "cursor"),
]


def _find_subscript(name: str) -> str | None:
    """Find an installed subscript by console_script name, fall back to dev path."""
    path = shutil.which(name)
    if path:
        return path
    # Development fallback: run via sys.executable
    stem = name.replace("ptop3-", "").replace("-", "_")
    dev_path = Path(__file__).parent / "scripts" / f"{stem}.py"
    if dev_path.exists():
        return str(dev_path)
    return None


def _subscript_cmd(name: str) -> list[str] | None:
    """Return the command list to invoke a subscript, or None if not found."""
    path = shutil.which(name)
    if path:
        return [path]
    stem = name.replace("ptop3-", "").replace("-", "_")
    dev_path = Path(__file__).parent / "scripts" / f"{stem}.py"
    if dev_path.exists():
        return [sys.executable, str(dev_path)]
    return None


@dataclass(slots=True)
class ProcRow:
    pid: int
    ppid: int
    name: str
    rss_mb: float
    cpu: float
    mem_pct: float
    swap_mb: float
    cmdline: str
    app: str
    io_read_mb: float = 0.0
    io_write_mb: float = 0.0
    net_sent_mb: float = 0.0
    net_recv_mb: float = 0.0
    status: str = "running"


@dataclass(slots=True)
class GroupRow:
    app: str
    procs: int
    rss_mb: float
    mem_pct: float
    cpu: float
    swap_mb: float
    io_read_mb: float = 0.0
    io_write_mb: float = 0.0
    net_sent_mb: float = 0.0
    net_recv_mb: float = 0.0


def normalize_app_name(name_hint: str, cmd_hint: str) -> str:
    app = (name_hint or "").split("/")[-1].split(".")[0].lower()
    cmd_low = (cmd_hint or "").lower()
    if app == "python3":
        app = "python"
    for k, v in ALIASES.items():
        if app.startswith(k):
            return v
    for pattern, v in _CMD_ALIASES:
        if pattern in cmd_low:
            return v
    for pattern, v in _ALIASES_CMD_REGEX:
        if pattern.search(cmd_low):
            return v
    return app or "unknown"


def read_vmswap_mb(pid: int) -> float:
    now = time.time()
    cached = SWAP_CACHE.get(pid)
    if cached and (now - cached[0] < SWAP_CACHE_TTL):
        return cached[1]
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmSwap:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        swap_mb = float(parts[1]) / 1024.0
                        SWAP_CACHE[pid] = (now, swap_mb)
                        return swap_mb
    except Exception:
        pass
    SWAP_CACHE[pid] = (now, 0.0)
    return 0.0


def get_proc_rows(filter_re: re.Pattern | None) -> list[ProcRow]:
    global LAST_CACHE_CLEAN
    rows: list[ProcRow] = []
    now = time.time()
    mem = psutil.virtual_memory()
    mem_total = mem.total
    inv_mem_total = 100.0 / mem_total

    if now - LAST_CACHE_CLEAN > 10.0:
        stale = [pid for pid, (ts, _, _, _) in PID_CACHE.items() if now - ts > PID_CACHE_TTL]
        for pid in stale:
            PID_CACHE.pop(pid, None)
        swap_stale = [pid for pid, (ts, _) in SWAP_CACHE.items() if now - ts > SWAP_CACHE_TTL * 5]
        for pid in swap_stale:
            SWAP_CACHE.pop(pid, None)
        LAST_CACHE_CLEAN = now

    lite = LITE_MODE
    attrs = ["pid", "ppid", "name"]
    filter_search = filter_re.search if filter_re else None
    read_swap = read_vmswap_mb

    try:
        for p in psutil.process_iter(attrs=attrs):
            try:
                with p.oneshot():
                    pid = p.info["pid"]
                    ppid = p.info.get("ppid") or 0

                    cache_entry = PID_CACHE.get(pid)
                    if cache_entry and (now - cache_entry[0] < PID_CACHE_TTL):
                        _, name, cmdline, app = cache_entry
                        if filter_search and not cmdline:
                            try:
                                cmd_list = p.cmdline()
                                cmdline = " ".join(cmd_list[:10]) if cmd_list else ""
                            except (psutil.AccessDenied, psutil.NoSuchProcess):
                                cmdline = ""
                    else:
                        name = p.info.get("name") or "?"
                        cmdline = ""
                        if (not lite) or filter_search:
                            try:
                                cmd_list = p.cmdline()
                                cmdline = " ".join(cmd_list[:10]) if cmd_list else ""
                            except (psutil.AccessDenied, psutil.NoSuchProcess):
                                cmdline = ""
                        app = normalize_app_name(name, cmdline)
                        PID_CACHE[pid] = (now, name, cmdline, app)

                    if filter_search and not (
                        filter_search(app) or filter_search(name) or (cmdline and filter_search(cmdline))
                    ):
                        continue

                    try:
                        mi = p.memory_info()
                        rss_bytes = mi.rss
                        rss_mb = rss_bytes / (1024 * 1024)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue

                    if lite and rss_mb < 2.0:
                        cpu = 0.0
                    else:
                        cpu = p.cpu_percent()

                    if not lite and rss_mb > 50:
                        swap_mb = read_swap(pid)
                    elif lite and rss_mb > 200:
                        swap_mb = read_swap(pid)
                    else:
                        swap_mb = 0.0

                    io_read_mb = io_write_mb = 0.0
                    if not lite and rss_mb > 25:
                        try:
                            io_counters = p.io_counters()
                            if io_counters:
                                io_read_mb = io_counters.read_bytes / (1024 * 1024)
                                io_write_mb = io_counters.write_bytes / (1024 * 1024)
                        except (psutil.AccessDenied, AttributeError):
                            pass

                    net_sent_mb = net_recv_mb = 0.0

                    try:
                        status = p.status() if not lite else "running"
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        status = "unknown"

                    mem_pct = rss_bytes * inv_mem_total

                    rows.append(
                        ProcRow(
                            pid, ppid, name, rss_mb, cpu, mem_pct, swap_mb, cmdline, app,
                            io_read_mb, io_write_mb, net_sent_mb, net_recv_mb, status,
                        )
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        pass

    return rows


def aggregate(rows: list[ProcRow]) -> list[GroupRow]:
    groups: dict[str, GroupRow] = {}
    for r in rows:
        g = groups.get(r.app)
        if g is None:
            groups[r.app] = GroupRow(
                app=r.app,
                procs=1,
                rss_mb=r.rss_mb,
                mem_pct=r.mem_pct,
                cpu=r.cpu,
                swap_mb=r.swap_mb,
                io_read_mb=r.io_read_mb,
                io_write_mb=r.io_write_mb,
                net_sent_mb=r.net_sent_mb,
                net_recv_mb=r.net_recv_mb,
            )
            continue
        g.procs += 1
        g.rss_mb += r.rss_mb
        g.mem_pct += r.mem_pct
        g.cpu += r.cpu
        g.swap_mb += r.swap_mb
        g.io_read_mb += r.io_read_mb
        g.io_write_mb += r.io_write_mb
        g.net_sent_mb += r.net_sent_mb
        g.net_recv_mb += r.net_recv_mb
    return list(groups.values())


def print_once(filter_re: re.Pattern | None, sort_key: str, top: int):
    rows = get_proc_rows(filter_re)
    groups = aggregate(rows)
    if sort_key == "io":
        groups.sort(key=lambda g: (g.io_read_mb + g.io_write_mb), reverse=True)
    else:
        key = {
            "mem": "mem_pct", "cpu": "cpu", "rss": "rss_mb",
            "swap": "swap_mb", "net": "net_sent_mb", "count": "procs",
        }[sort_key]
        groups.sort(key=lambda g: getattr(g, key), reverse=True)
    print(
        f"{'APP':{MAX_APP_NAME}} {'PROCS':>5} {'RSS(MiB)':>10} {'SWAP(MiB)':>10}"
        f" {'%MEM':>7} {'%CPU':>7} {'IO_R(MB)':>10} {'IO_W(MB)':>10}"
    )
    for g in groups[:top]:
        print(
            f"{g.app[:MAX_APP_NAME]:{MAX_APP_NAME}} {g.procs:>5} {g.rss_mb:>10.1f}"
            f" {g.swap_mb:>10.1f} {g.mem_pct:>7.1f} {g.cpu:>7.1f}"
            f" {g.io_read_mb:>10.1f} {g.io_write_mb:>10.1f}"
        )


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _tree_sort_key(sort_key: str):
    if sort_key == "mem":
        return lambda r: r.mem_pct
    if sort_key == "cpu":
        return lambda r: r.cpu
    if sort_key == "rss":
        return lambda r: r.rss_mb
    if sort_key == "swap":
        return lambda r: r.swap_mb
    if sort_key == "io":
        return lambda r: r.io_read_mb + r.io_write_mb
    if sort_key == "net":
        return lambda r: r.net_sent_mb
    return lambda r: r.rss_mb


def build_process_tree(procs: list[ProcRow], sort_key: str = "rss") -> list[tuple]:
    """Build a flattened tree list from processes using ppid.

    Returns list of (ProcRow, indent_level, tree_prefix_str) tuples.
    Roots and siblings are sorted by sort_key descending.
    """
    by_pid = {r.pid: r for r in procs}
    children: dict[int, list[int]] = {}
    roots = []
    pids_in_group = set(r.pid for r in procs)
    keyfn = _tree_sort_key(sort_key)

    for r in procs:
        if r.ppid in pids_in_group and r.ppid != r.pid:
            children.setdefault(r.ppid, []).append(r.pid)
        else:
            roots.append(r.pid)

    roots.sort(key=lambda pid: keyfn(by_pid[pid]), reverse=True)

    result = []

    def walk(pid, depth, continuation):
        proc = by_pid.get(pid)
        if not proc:
            return
        prefix = ""
        if depth > 0:
            for has_more in continuation[:-1]:
                prefix += "│   " if has_more else "    "
            prefix += "├── " if continuation[-1] else "└── "
        result.append((proc, depth, prefix))
        kids = children.get(pid, [])
        kids.sort(key=lambda p: keyfn(by_pid[p]) if p in by_pid else 0, reverse=True)
        for i, child_pid in enumerate(kids):
            has_more_siblings = i < len(kids) - 1
            walk(child_pid, depth + 1, continuation + [has_more_siblings])

    for root_pid in roots:
        walk(root_pid, 0, [])

    return result


class TUI:
    def __init__(self, stdscr, filter_re, sort_key, refresh):
        self.stdscr = stdscr
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)
        self._set_timeout(refresh)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_GREEN, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(10, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(12, curses.COLOR_WHITE, curses.COLOR_GREEN)
        curses.init_pair(13, curses.COLOR_WHITE, curses.COLOR_YELLOW)
        curses.init_pair(14, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(15, curses.COLOR_WHITE, curses.COLOR_CYAN)
        curses.init_pair(16, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(17, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(18, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(19, curses.COLOR_WHITE, curses.COLOR_RED)
        try:
            curses.init_pair(20, curses.COLOR_BLACK, 208)
            curses.init_pair(21, curses.COLOR_BLACK, 208)
        except (curses.error, ValueError):
            curses.init_pair(20, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(21, curses.COLOR_BLACK, curses.COLOR_YELLOW)

        self.filter_re = filter_re
        self.filter_text = filter_re.pattern if filter_re else ""
        self.sort_key = sort_key
        self.refresh = refresh
        self.view = "groups"
        self.sel = 0
        self.groups: list[GroupRow] = []
        self.detail_list: list[ProcRow] = []
        self.detail_tree: list[tuple] = []
        self.tree_mode = False
        self.detail_app: str | None = None
        self.last = 0.0
        self.last_proc_rows: list[ProcRow] = []
        self.last_sample_time = 0.0
        self.alerts_cache: list[str] = []
        self.alerts_cache_time = 0.0
        self.sample_skip_counter = 0
        self.status_msg = ""
        self.status_time = 0.0
        self.sel_attr = curses.A_REVERSE
        if curses.has_colors():
            self.sel_attr = curses.color_pair(7) | curses.A_BOLD

    def _set_timeout(self, r):
        self.stdscr.timeout(int(r * 1000))

    def run(self):
        while True:
            now = time.time()
            if now - self.last >= self.refresh:
                self.sample()
                self.last = now
            self.draw()
            ch = self.stdscr.getch()
            if ch == -1:
                continue
            if ch == 27:
                self.stdscr.nodelay(True)
                a, b = self.stdscr.getch(), self.stdscr.getch()
                self.stdscr.nodelay(False)
                if a == 91 and b in (65, 66, 67, 68):
                    ch = {65: curses.KEY_UP, 66: curses.KEY_DOWN, 67: ord("l"), 68: ord("h")}.get(b, ch)
            if ch in (ord("q"), 3):
                break
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.sel = clamp(self.sel + 1, 0, max(0, self.length() - 1))
            elif ch == curses.KEY_UP:
                self.sel = clamp(self.sel - 1, 0, max(0, self.length() - 1))
            elif ch in (curses.KEY_NPAGE,):
                self.sel = clamp(self.sel + 10, 0, max(0, self.length() - 1))
            elif ch in (curses.KEY_PPAGE,):
                self.sel = clamp(self.sel - 10, 0, max(0, self.length() - 1))
            elif ch in (curses.KEY_HOME,):
                self.sel = 0
            elif ch in (curses.KEY_END,):
                self.sel = max(0, self.length() - 1)
            elif ch in (10, 13, curses.KEY_ENTER, ord("l")):
                self.toggle_view()
            elif ch == ord("h"):
                if self.view == "detail":
                    self.toggle_view()
            elif ch == ord("s"):
                i = (SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)
                self.sort_key = SORT_KEYS[i]
                self.sel = 0
                self.status(f"Sort: {self.sort_key}")
            elif ch == ord("+"):
                self.refresh = clamp(self.refresh + 0.1, MIN_REFRESH, MAX_REFRESH)
                self._set_timeout(self.refresh)
                self.status(f"Refresh: {self.refresh:.1f}s")
            elif ch == ord("-"):
                self.refresh = clamp(self.refresh - 0.1, MIN_REFRESH, MAX_REFRESH)
                self._set_timeout(self.refresh)
                self.status(f"Refresh: {self.refresh:.1f}s")
            elif ch == ord("r"):
                self.filter_text = ""
                self.filter_re = None
                self.sel = 0
                self.status("Filter cleared")
            elif ch == ord("f"):
                self.prompt_filter()
                self.sel = 0
            elif ch == ord("k"):
                if self.view == "detail":
                    self.kill_selected(signal.SIGTERM)
                elif self.view == "groups":
                    self.kill_group(sig=signal.SIGTERM)
            elif ch == ord("K"):
                if self.view == "detail":
                    self.kill_selected(signal.SIGKILL)
                elif self.view == "groups":
                    self.kill_group(sig=signal.SIGKILL)
            elif ch == ord("t") and self.view == "detail":
                self.tree_mode = not self.tree_mode
                self.status(f"Tree view: {'ON' if self.tree_mode else 'OFF'}")
            elif ch == ord("g") and self.view == "groups":
                self.kill_group()
            elif ch == ord("w"):
                self.run_swap_clean()
            elif ch == ord("d"):
                self.run_drop_caches()

    def length(self):
        if self.view == "groups":
            return len(self.groups)
        if self.tree_mode:
            return len(self.detail_tree)
        return len(self.detail_list)

    def sample(self):
        self.sample_skip_counter += 1
        rows = get_proc_rows(self.filter_re)
        groups = aggregate(rows)
        keymap = {
            "mem": "mem_pct", "cpu": "cpu", "rss": "rss_mb",
            "swap": "swap_mb", "net": "net_sent_mb", "count": "procs",
        }
        if self.sort_key == "io":
            groups.sort(key=lambda g: (g.io_read_mb + g.io_write_mb), reverse=True)
        else:
            groups.sort(key=lambda g: getattr(g, keymap[self.sort_key]), reverse=True)
        self.groups = groups
        self.last_proc_rows = rows

        if self.view == "detail" and self.detail_app:
            lst = [r for r in rows if r.app == self.detail_app]
            if self.tree_mode:
                self.detail_tree = build_process_tree(lst, self.sort_key)
                self.detail_list = lst
            else:
                if self.sort_key == "mem":
                    lst.sort(key=lambda x: x.mem_pct, reverse=True)
                elif self.sort_key == "cpu":
                    lst.sort(key=lambda x: x.cpu, reverse=True)
                elif self.sort_key == "rss":
                    lst.sort(key=lambda x: x.rss_mb, reverse=True)
                elif self.sort_key == "swap":
                    lst.sort(key=lambda x: x.swap_mb, reverse=True)
                elif self.sort_key == "io":
                    lst.sort(key=lambda x: (x.io_read_mb + x.io_write_mb), reverse=True)
                elif self.sort_key == "net":
                    lst.sort(key=lambda x: x.net_sent_mb, reverse=True)
                else:
                    lst.sort(key=lambda x: x.pid, reverse=True)
                self.detail_list = lst
        self.sel = clamp(self.sel, 0, max(0, self.length() - 1))

    def toggle_view(self):
        if self.view == "groups":
            if not self.groups:
                return
            self.detail_app = self.groups[self.sel].app
            self.view = "detail"
            self.sel = 0
        else:
            self.view = "groups"
            self.detail_app = None
            self.sel = 0
            self.tree_mode = False

    def draw(self):
        self.stdscr.erase()
        maxy, maxx = self.stdscr.getmaxyx()
        vm, sm = psutil.virtual_memory(), psutil.swap_memory()
        gib = 1024 ** 3
        try:
            l1, l5, l15 = os.getloadavg()
        except OSError:
            l1 = l5 = l15 = 0.0
        mem_used = vm.used / gib
        mem_total = vm.total / gib
        mem_avail = getattr(vm, "available", 0) / gib
        mem_free = getattr(vm, "free", 0) / gib
        mem_buf = getattr(vm, "buffers", 0) / gib
        mem_cache = getattr(vm, "cached", 0) / gib
        cpu_count = os.cpu_count() or 1
        BG_TITLE = curses.color_pair(10) | curses.A_BOLD
        BG_LABEL = curses.color_pair(11) | curses.A_BOLD
        BG_OK = curses.color_pair(17) | curses.A_BOLD
        BG_WARN = curses.color_pair(18) | curses.A_BOLD
        BG_CRIT = curses.color_pair(19) | curses.A_BOLD
        BG_NEUT = curses.color_pair(16)
        BG_BUF = curses.color_pair(15) | curses.A_BOLD
        BG_SWAP_L = curses.color_pair(20) | curses.A_BOLD
        BG_SWAP = curses.color_pair(21) | curses.A_BOLD
        BG_GAP = curses.color_pair(1)

        def _mem_bg(pct):
            if pct > 90:
                return BG_CRIT
            if pct > 70:
                return BG_WARN
            return BG_OK

        def _swap_bg(pct):
            if pct > 80:
                return BG_CRIT
            if pct > 50:
                return BG_WARN
            return BG_SWAP

        def _load_bg(load_val):
            per_core = load_val / cpu_count
            if per_core > 2.0:
                return BG_CRIT
            if per_core > 1.0:
                return BG_WARN
            return BG_OK

        def _avail_bg(gib_val, thresh):
            return BG_OK if gib_val > thresh else BG_WARN

        badges = [
            ("", "ptop3", BG_TITLE, BG_TITLE),
            ("mem", f"{mem_used:.1f}/{mem_total:.1f}G", BG_LABEL, _mem_bg(vm.percent)),
            ("avl", f"{mem_avail:.1f}G", BG_LABEL, _avail_bg(mem_avail, 2.0)),
            ("free", f"{mem_free:.1f}G", BG_LABEL, _avail_bg(mem_free, 1.0)),
            ("buf", f"{mem_buf:.1f}G", BG_BUF, BG_NEUT),
            ("cache", f"{mem_cache:.1f}G", BG_BUF, BG_NEUT),
            ("swap", f"{sm.used/gib:.1f}/{sm.total/gib:.1f}G", BG_SWAP_L, _swap_bg(sm.percent)),
            ("load", f"{l1:.2f} {l5:.2f} {l15:.2f}", BG_LABEL, _load_bg(l1)),
            ("sort", self.sort_key, BG_LABEL, BG_NEUT),
            ("ref", f"{self.refresh:.1f}s", BG_LABEL, BG_NEUT),
            ("filt", self.filter_text or "-", BG_LABEL, BG_NEUT),
        ]

        col = 0
        for label, value, lattr, vattr in badges:
            if col >= maxx - 1:
                break
            if label:
                ltxt = f" {label} "
                self.addstr(0, col, ltxt[: maxx - col - 1], lattr)
                col += len(ltxt)
            vtxt = f" {value} "
            if col < maxx - 1:
                self.addstr(0, col, vtxt[: maxx - col - 1], vattr)
                col += len(vtxt)
            if col < maxx - 1:
                self.addstr(0, col, " ", BG_GAP)
                col += 1

        if self.view == "groups":
            self.draw_groups(maxx, maxy)
        else:
            self.draw_detail(maxx, maxy)

        alerts = self.collect_alerts()
        if alerts:
            alert_start_y = maxy - len(alerts) - 2
            for i, alert in enumerate(alerts):
                y = alert_start_y + i
                if y >= 2:
                    color = curses.color_pair(3) if "critical" in alert.lower() else curses.color_pair(1)
                    if "critical" in alert.lower():
                        color |= curses.A_BOLD
                    self.addstr(y, 0, alert[: maxx - 1], color)

        if self.status_msg and (time.time() - self.status_time) < 8.0:
            self.addstr(maxy - 2, 0, self.status_msg[: maxx - 1], curses.color_pair(2))

        help_line = (
            "↑/↓ PgUp/PgDn Home/End  Enter/l expand  h back  s sort  f filter"
            "  r reset  +/- refresh  t tree  k/K kill  g kill-group  w swap-clean  d drop-caches  q quit"
        )
        self.addstr(maxy - 1, 0, help_line[: maxx - 1], curses.color_pair(5))
        self.stdscr.refresh()

    def _make_bar(self, pct, width=10):
        pct = max(0.0, min(100.0, pct))
        filled = int(round(pct / 100.0 * width))
        return "█" * filled + "░" * (width - filled)

    def draw_groups(self, maxx, maxy):
        bar_w = 10
        hdr = (
            f"{'APP':{MAX_APP_NAME}} {'PROCS':>5} {'RSS(MiB)':>10} {'SWAP(MiB)':>10}"
            f" {'%MEM':>7} {'MEM':>{bar_w}} {'%CPU':>7} {'CPU':>{bar_w}}"
            f" {'IO_R':>7} {'IO_W':>7}"
        )
        self.addstr(2, 0, hdr[: maxx - 1], curses.A_BOLD)
        available_rows = maxy - 14
        for i, g in enumerate(self.groups[:available_rows]):
            y = 3 + i
            mem_bar = self._make_bar(g.mem_pct, bar_w)
            cpu_bar = self._make_bar(min(g.cpu, 100.0), bar_w)
            line = (
                f"{g.app[:MAX_APP_NAME]:{MAX_APP_NAME}} {g.procs:>5} {g.rss_mb:>10.1f} {g.swap_mb:>10.1f}"
                f" {g.mem_pct:>7.1f} {mem_bar} {g.cpu:>7.1f} {cpu_bar}"
                f" {g.io_read_mb:>7.1f} {g.io_write_mb:>7.1f}"
            )
            color = self.color_group(g)
            if i == self.sel:
                color = self.sel_attr
            self.addstr(y, 0, line[: maxx - 1], color)

    def draw_detail(self, maxx, maxy):
        mode_label = " [TREE]" if self.tree_mode else ""
        self.addstr(2, 0, f"App: {self.detail_app}{mode_label}", curses.A_BOLD)
        if self.tree_mode:
            stats_w = 40
            self.addstr(
                3, 0,
                f"{'PID':>7} {'RSS(MiB)':>9} {'%CPU':>6} {'%MEM':>6} {'SWAP':>6}  TREE",
                curses.A_BOLD,
            )
            available_rows = maxy - 15
            for i, (r, depth, prefix) in enumerate(self.detail_tree[:available_rows]):
                y = 4 + i
                cmd_full = r.cmdline or r.name
                tree_str = cmd_full if depth == 0 else prefix + cmd_full
                remaining = maxx - stats_w - 1
                tree_str = tree_str[: max(remaining, 10)]
                line = f"{r.pid:>7} {r.rss_mb:>9.1f} {r.cpu:>6.1f} {r.mem_pct:>6.1f} {r.swap_mb:>6.1f}  {tree_str}"
                color = self.color_proc(r)
                if i == self.sel:
                    color = self.sel_attr
                self.addstr(y, 0, line[: maxx - 1], color)
        else:
            stats_w = 66
            self.addstr(
                3, 0,
                f"{'PID':>7} {'PPID':>7} {'RSS(MiB)':>9} {'%CPU':>6} {'%MEM':>6}"
                f" {'SWAP(MiB)':>9} {'IO_R':>7} {'IO_W':>7}  CMD",
                curses.A_BOLD,
            )
            available_rows = maxy - 15
            for i, r in enumerate(self.detail_list[:available_rows]):
                y = 4 + i
                remaining = maxx - stats_w - 1
                cmd = (r.cmdline or r.name)[: max(remaining, 10)]
                line = (
                    f"{r.pid:>7} {r.ppid:>7} {r.rss_mb:>9.1f} {r.cpu:>6.1f} {r.mem_pct:>6.1f}"
                    f" {r.swap_mb:>9.1f} {r.io_read_mb:>7.1f} {r.io_write_mb:>7.1f}  {cmd}"
                )
                color = self.color_proc(r)
                if i == self.sel:
                    color = self.sel_attr
                self.addstr(y, 0, line[: maxx - 1], color)

    def color_group(self, g: GroupRow):
        if g.cpu >= CPU_HOT * 2 or g.rss_mb >= RSS_HOT_MB * 2 or g.swap_mb >= SWAP_HOT_MB * 2:
            return curses.color_pair(4) | curses.A_BOLD
        if g.cpu >= CPU_HOT or g.rss_mb >= RSS_HOT_MB or g.swap_mb >= SWAP_HOT_MB:
            return curses.color_pair(3) | curses.A_BOLD
        return curses.color_pair(1)

    def color_proc(self, r: ProcRow):
        if r.cpu >= CPU_HOT * 2 or r.rss_mb >= RSS_HOT_MB * 2 or r.swap_mb >= SWAP_HOT_MB * 2:
            return curses.color_pair(4) | curses.A_BOLD
        if r.cpu >= CPU_HOT or r.rss_mb >= RSS_HOT_MB or r.swap_mb >= SWAP_HOT_MB:
            return curses.color_pair(3) | curses.A_BOLD
        return curses.color_pair(1)

    def collect_alerts(self):
        current_time = time.time()
        if self.alerts_cache and (current_time - self.alerts_cache_time) < 3.0:
            return self.alerts_cache
        try:
            alerts = []
            now = time.strftime("%H:%M:%S")
            vm, sm = psutil.virtual_memory(), psutil.swap_memory()
            if vm.percent > 95:
                alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: MEMORY CRITICAL ({vm.percent:.1f}%)")
            elif vm.percent > 85:
                alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: High memory usage ({vm.percent:.1f}%)")

            if sm.percent >= SWAP_CLEAN_SUGGEST_PCT:
                alerts.append(
                    f"{now} SYSTEM  {'':<10} {'':<30}: Swap {sm.percent:.1f}% — consider running swap-clean"
                )

            try:
                disk_usage = psutil.disk_usage("/")
                if disk_usage.percent > DISK_USAGE_CRITICAL:
                    alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: DISK CRITICAL ({disk_usage.percent:.1f}%)")
                elif disk_usage.percent > DISK_USAGE_HIGH:
                    alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: High disk usage ({disk_usage.percent:.1f}%)")
            except Exception:
                pass

            all_rows = self.last_proc_rows or []
            zombie_count = sum(1 for r in all_rows if r.status == "zombie")
            if zombie_count > 0:
                alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: {zombie_count} zombie processes detected")

            high_usage_rows = [r for r in all_rows if r.cpu >= 5.0 or r.mem_pct >= 5.0]
            high_usage_rows = sorted(high_usage_rows, key=lambda x: x.cpu + x.mem_pct, reverse=True)[:20]

            for r in high_usage_rows:
                cmd_short = (r.cmdline or r.name)[:30]
                if r.cpu >= CPU_HOT * 2:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: CPU critical ({r.cpu:.1f}%)")
                elif r.cpu >= CPU_HOT:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: High CPU ({r.cpu:.1f}%)")
                if r.mem_pct >= 15.0:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: MEMORY CRITICAL ({r.mem_pct:.1f}%)")
                elif r.mem_pct >= 10.0:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: High memory ({r.mem_pct:.1f}%)")
                if r.swap_mb >= SWAP_HOT_MB * 3:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: SWAP CRITICAL ({r.swap_mb:.1f}MB)")
                elif r.swap_mb >= SWAP_HOT_MB:
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: High swap ({r.swap_mb:.1f}MB)")
                if r.mem_pct >= 5.0 and r.swap_mb >= SWAP_HOT_MB:
                    alerts.append(
                        f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: Memory pressure"
                        f" ({r.mem_pct:.1f}% + {r.swap_mb:.1f}MB)"
                    )
                if r.io_read_mb > 100.0 or r.io_write_mb > 100.0:
                    alerts.append(
                        f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: High I/O"
                        f" ({r.io_read_mb:.1f}MB read, {r.io_write_mb:.1f}MB write)"
                    )
                if r.status == "zombie":
                    alerts.append(f"{now} {r.pid:>6} {r.app[:10]:<10} {cmd_short:<30}: ZOMBIE PROCESS")

            top_groups = sorted(self.groups, key=lambda x: x.cpu, reverse=True)[:10]
            for g in top_groups:
                if g.cpu >= CPU_HOT * 2:
                    alerts.append(
                        f"{now} group   {g.app[:10]:<10} {g.procs:>3}procs{'':<26}: Group CPU critical ({g.cpu:.1f}%)"
                    )
                elif g.cpu >= CPU_HOT:
                    alerts.append(
                        f"{now} group   {g.app[:10]:<10} {g.procs:>3}procs{'':<26}: Group high CPU ({g.cpu:.1f}%)"
                    )
                if g.swap_mb >= SWAP_HOT_MB * 4:
                    alerts.append(
                        f"{now} group   {g.app[:10]:<10} {g.procs:>3}procs{'':<26}: Group swap critical ({g.swap_mb:.1f}MB)"
                    )

            result = alerts[-10:]
            self.alerts_cache = result
            self.alerts_cache_time = current_time
            return result
        except Exception as e:
            now = time.strftime("%H:%M:%S")
            error_alert = [f"{now} ERROR: Alert collection failed: {str(e)[:40]}"]
            self.alerts_cache = error_alert
            self.alerts_cache_time = current_time
            return error_alert

    def prompt_filter(self):
        curses.echo()
        self.addstr(1, 0, "Filter (regex): ")
        self.clrtoeol()
        try:
            txt = self.stdscr.getstr(1, 17).decode("utf-8", "ignore")
        except Exception:
            txt = ""
        curses.noecho()
        txt = txt.strip()
        if txt:
            try:
                self.filter_re = re.compile(txt, re.IGNORECASE)
                self.filter_text = txt
                self.status(f"Filter: {txt}")
            except re.error:
                self.filter_re = None
                self.filter_text = "invalid"
                self.status("Filter invalid")
        else:
            self.filter_re = None
            self.filter_text = ""
            self.status("Filter cleared")

    def kill_selected(self, sig):
        if not self.detail_list:
            return
        pid = self.detail_list[self.sel].pid
        try:
            psutil.Process(pid).send_signal(sig)
            name = self.detail_list[self.sel].name
            self.status(f"Sent {signal.Signals(sig).name} to {pid} ({name})")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.status(str(e))

    def kill_group(self, sig=signal.SIGTERM):
        if not self.groups:
            return
        app = self.groups[self.sel].app
        sig_name = signal.Signals(sig).name
        n = 0
        denied = 0
        for p in psutil.process_iter():
            try:
                name = p.name() or ""
                try:
                    cmd = " ".join(p.cmdline()[:4])
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmd = ""
                if normalize_app_name(name, cmd) == app:
                    p.send_signal(sig)
                    n += 1
            except psutil.AccessDenied:
                denied += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        msg = f"Sent {sig_name} to '{app}' ({n} procs)"
        if denied:
            msg += f" [{denied} denied]"
        self.status(msg)

    def run_swap_clean(self):
        cmd = _subscript_cmd("ptop3-swap-clean")
        if cmd is None:
            self.status("ptop3-swap-clean not found (pip install ptop3)")
            return
        use_sudo = os.geteuid() != 0
        if use_sudo:
            cmd = ["sudo", "-n"] + cmd
        self.status("swap-clean started ...")
        self.draw()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                self.status("swap-clean finished")
            else:
                msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
                tail = f": {msg[0]}" if msg else ""
                if use_sudo and "password" in (proc.stderr or "").lower():
                    self.status("swap-clean needs NOPASSWD sudo — run: sudo ptop3 --init-subscripts")
                else:
                    self.status(f"swap-clean failed ({proc.returncode}){tail}")
        except Exception as e:
            self.status(f"swap-clean error: {str(e)[:60]}")

    def run_drop_caches(self):
        cmd = _subscript_cmd("ptop3-drop-caches")
        if cmd is None:
            self.status("ptop3-drop-caches not found (pip install ptop3)")
            return
        use_sudo = os.geteuid() != 0
        if use_sudo:
            cmd = ["sudo", "-n"] + cmd
        self.status("drop-caches started ...")
        self.draw()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                msg = (proc.stdout or "").strip().splitlines()[-1:]
                tail = msg[0] if msg else "done"
                self.status(f"drop-caches: {tail}")
            else:
                msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
                tail = f": {msg[0]}" if msg else ""
                if use_sudo and "password" in (proc.stderr or "").lower():
                    self.status("drop-caches needs NOPASSWD sudo — run: sudo ptop3 --init-subscripts")
                else:
                    self.status(f"drop-caches failed ({proc.returncode}){tail}")
        except Exception as e:
            self.status(f"drop-caches error: {str(e)[:60]}")

    def addstr(self, y, x, text, attrs=0):
        maxy, maxx = self.stdscr.getmaxyx()
        if 0 <= y < maxy and 0 <= x < maxx:
            self.stdscr.addnstr(y, x, text, maxx - x - 1, attrs)

    def clrtoeol(self):
        y, x = self.stdscr.getyx()
        maxy, maxx = self.stdscr.getmaxyx()
        self.stdscr.addnstr(y, x, " " * (maxx - x - 1), maxx - x - 1)

    def status(self, msg):
        self.status_msg = msg
        self.status_time = time.time()


def parse_args():
    ap = argparse.ArgumentParser(description="ptop3 — grouped process TUI")
    ap.add_argument("--once", action="store_true", help="print one-shot table and exit")
    ap.add_argument("-f", "--filter", default="", help="regex to filter by app/name/cmd")
    ap.add_argument("-s", "--sort", default="mem", choices=SORT_KEYS)
    ap.add_argument("-n", "--top", type=int, default=15, help="rows for --once")
    ap.add_argument("--refresh", type=float, default=DEFAULT_REFRESH)
    ap.add_argument("--lite", action="store_true", help="lite mode: skip cmdline/io for tiny procs")
    ap.add_argument("--check-sudo", action="store_true", help="check sudo config for subscripts")
    ap.add_argument("--init-subscripts", action="store_true", help="write /etc/sudoers.d/ptop3 for subscripts")
    args = ap.parse_args()
    fil = None
    if args.filter:
        try:
            fil = re.compile(args.filter, re.IGNORECASE)
        except re.error:
            print("Invalid regex for --filter; ignoring.", file=sys.stderr)
    return args, fil


def main():
    args, fil = parse_args()
    global LITE_MODE
    if args.lite:
        LITE_MODE = True

    if args.check_sudo:
        from ptop3.sudo_config import check_sudo
        result = check_sudo()
        for script, status in result.items():
            print(f"  {script}: {status}")
        all_ok = all(v == "ok" for v in result.values())
        sys.exit(0 if all_ok else 1)

    if args.init_subscripts:
        from ptop3.sudo_config import init_subscripts
        init_subscripts()
        return

    if args.once:
        print_once(fil, args.sort, args.top)
        return

    curses.wrapper(lambda stdscr: TUI(stdscr, fil, args.sort, args.refresh).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
