#!/usr/bin/env python3
"""
ptop3 — htop-like TUI grouped by application.
Keys:
  ↑/↓/PgUp/PgDn/Home/End  Move selection
  Enter or l              Expand group / back with h
  t                       Toggle process tree view   [detail view]
  s                       Cycle sort: mem -> cpu -> rss -> swap -> io -> count
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
from dataclasses import dataclass, field
from pathlib import Path

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

DISK_USAGE_CRITICAL = 95.0
DISK_USAGE_HIGH = 85.0

SORT_KEYS = ("mem", "cpu", "rss", "swap", "io", "count")
MAX_APP_NAME = 24

ALIASES = {
    "code-insiders": "vscode-insiders",
    "code": "vscode",
    "chromium": "chrome",
    "chromium-browse": "chrome",
    "chrome": "chrome",
    "firefox": "firefox",
    "web content": "firefox",
    "python3": "python",
    "python": "python",
    "java": "java",
    "cursor": "cursor",
    "gnome-shell": "gnome-shell",
    "xwayland": "Xwayland",
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

LITE_MODE = False


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


@dataclass(slots=True)
class SampleResult:
    rows: list[ProcRow]
    error: str = ""


@dataclass(slots=True)
class ProcessSampler:
    pid_cache_ttl: float = 30.0
    swap_cache_ttl: float = 2.0
    pid_cache: dict[int, tuple[float, str, str, str]] = field(default_factory=dict)
    swap_cache: dict[int, tuple[float, float]] = field(default_factory=dict)
    last_cache_clean: float = 0.0

    def read_vmswap_mb(self, pid: int) -> float:
        now = time.time()
        cached = self.swap_cache.get(pid)
        if cached and (now - cached[0] < self.swap_cache_ttl):
            return cached[1]
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if not line.startswith("VmSwap:"):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        swap_mb = float(parts[1]) / 1024.0
                        self.swap_cache[pid] = (now, swap_mb)
                        return swap_mb
        except OSError:
            pass
        self.swap_cache[pid] = (now, 0.0)
        return 0.0

    def _cleanup_cache(self, now: float) -> None:
        if now - self.last_cache_clean <= 10.0:
            return
        stale_pids = [pid for pid, (ts, *_rest) in self.pid_cache.items() if now - ts > self.pid_cache_ttl]
        for pid in stale_pids:
            self.pid_cache.pop(pid, None)
        stale_swaps = [
            pid for pid, (ts, _swap) in self.swap_cache.items()
            if now - ts > self.swap_cache_ttl * 5
        ]
        for pid in stale_swaps:
            self.swap_cache.pop(pid, None)
        self.last_cache_clean = now

    def sample(self, filter_re: re.Pattern[str] | None) -> SampleResult:
        rows: list[ProcRow] = []
        errors: list[str] = []
        now = time.time()
        self._cleanup_cache(now)

        try:
            mem = psutil.virtual_memory()
            inv_mem_total = 100.0 / mem.total
        except Exception as exc:  # pragma: no cover - defensive fallback
            return SampleResult([], f"sampling failed: {exc}")

        lite = LITE_MODE
        filter_search = filter_re.search if filter_re else None
        attrs = ["pid", "ppid", "name"]

        try:
            proc_iter = psutil.process_iter(attrs=attrs)
        except Exception as exc:
            return SampleResult([], f"sampling failed: {exc}")

        for proc in proc_iter:
            try:
                row = self._sample_process(proc, now, inv_mem_total, lite, filter_search)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as exc:
                if not errors:
                    errors.append(f"sampling warning: {exc}")
                continue
            if row is not None:
                rows.append(row)

        return SampleResult(rows, errors[0] if errors else "")

    def _sample_process(
        self,
        proc: psutil.Process,
        now: float,
        inv_mem_total: float,
        lite: bool,
        filter_search,
    ) -> ProcRow | None:
        with proc.oneshot():
            pid = proc.info["pid"]
            ppid = proc.info.get("ppid") or 0

            cache_entry = self.pid_cache.get(pid)
            if cache_entry and (now - cache_entry[0] < self.pid_cache_ttl):
                _, name, cmdline, app = cache_entry
                if filter_search and not cmdline:
                    cmdline = _safe_cmdline(proc)
            else:
                name = proc.info.get("name") or "?"
                cmdline = _safe_cmdline(proc) if (not lite) or filter_search else ""
                app = normalize_app_name(name, cmdline)
                self.pid_cache[pid] = (now, name, cmdline, app)

            if filter_search and not _matches_filter(filter_search, app, name, cmdline):
                return None

            try:
                rss_bytes = proc.memory_info().rss
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                return None

            rss_mb = rss_bytes / (1024 * 1024)
            cpu = 0.0 if lite and rss_mb < 2.0 else proc.cpu_percent()
            swap_mb = _swap_value(proc.pid, rss_mb, lite, read_swap=self.read_vmswap_mb)
            io_read_mb, io_write_mb = _io_values(proc, rss_mb, lite)

            try:
                status = proc.status() if not lite else "running"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                status = "unknown"

            return ProcRow(
                pid=pid,
                ppid=ppid,
                name=name,
                rss_mb=rss_mb,
                cpu=cpu,
                mem_pct=rss_bytes * inv_mem_total,
                swap_mb=swap_mb,
                cmdline=cmdline,
                app=app,
                io_read_mb=io_read_mb,
                io_write_mb=io_write_mb,
                status=status,
            )


DEFAULT_SAMPLER = ProcessSampler()
PID_CACHE = DEFAULT_SAMPLER.pid_cache
SWAP_CACHE = DEFAULT_SAMPLER.swap_cache


def _find_subscript(name: str) -> str | None:
    """Find an installed subscript by console_script name, fall back to dev path."""
    path = shutil.which(name)
    if path:
        return path
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


def _safe_cmdline(proc: psutil.Process) -> str:
    try:
        cmd_list = proc.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return ""
    return " ".join(cmd_list[:10]) if cmd_list else ""


def _matches_filter(filter_search, app: str, name: str, cmdline: str) -> bool:
    return bool(filter_search(app) or filter_search(name) or (cmdline and filter_search(cmdline)))


def _swap_value(pid: int, rss_mb: float, lite: bool, read_swap=None) -> float:
    if read_swap is None:
        read_swap = read_vmswap_mb
    if not lite and rss_mb > 50:
        return read_swap(pid)
    if lite and rss_mb > 200:
        return read_swap(pid)
    return 0.0


def _io_values(proc: psutil.Process, rss_mb: float, lite: bool) -> tuple[float, float]:
    if lite or rss_mb <= 25:
        return 0.0, 0.0
    try:
        io_counters = proc.io_counters()
    except (psutil.AccessDenied, AttributeError, OSError):
        return 0.0, 0.0
    if not io_counters:
        return 0.0, 0.0
    return (
        io_counters.read_bytes / (1024 * 1024),
        io_counters.write_bytes / (1024 * 1024),
    )


def normalize_app_name(name_hint: str, cmd_hint: str) -> str:
    app = (name_hint or "").split("/")[-1].split(".")[0].lower()
    cmd_low = (cmd_hint or "").lower()
    if app == "python3":
        app = "python"
    for key, value in ALIASES.items():
        if app.startswith(key):
            return value
    for pattern, value in _CMD_ALIASES:
        if pattern in cmd_low:
            return value
    for pattern, value in _ALIASES_CMD_REGEX:
        if pattern.search(cmd_low):
            return value
    return app or "unknown"


def read_vmswap_mb(pid: int) -> float:
    return DEFAULT_SAMPLER.read_vmswap_mb(pid)


def sample_processes(filter_re: re.Pattern[str] | None) -> SampleResult:
    return DEFAULT_SAMPLER.sample(filter_re)


def get_proc_rows(filter_re: re.Pattern[str] | None) -> list[ProcRow]:
    return sample_processes(filter_re).rows


def aggregate(rows: list[ProcRow]) -> list[GroupRow]:
    groups: dict[str, GroupRow] = {}
    for row in rows:
        group = groups.get(row.app)
        if group is None:
            groups[row.app] = GroupRow(
                app=row.app,
                procs=1,
                rss_mb=row.rss_mb,
                mem_pct=row.mem_pct,
                cpu=row.cpu,
                swap_mb=row.swap_mb,
                io_read_mb=row.io_read_mb,
                io_write_mb=row.io_write_mb,
            )
            continue
        group.procs += 1
        group.rss_mb += row.rss_mb
        group.mem_pct += row.mem_pct
        group.cpu += row.cpu
        group.swap_mb += row.swap_mb
        group.io_read_mb += row.io_read_mb
        group.io_write_mb += row.io_write_mb
    return list(groups.values())


def _group_sort_value(group: GroupRow, sort_key: str) -> float:
    if sort_key == "mem":
        return group.mem_pct
    if sort_key == "cpu":
        return group.cpu
    if sort_key == "rss":
        return group.rss_mb
    if sort_key == "swap":
        return group.swap_mb
    if sort_key == "io":
        return group.io_read_mb + group.io_write_mb
    return float(group.procs)


def sort_groups(groups: list[GroupRow], sort_key: str) -> list[GroupRow]:
    groups.sort(key=lambda group: _group_sort_value(group, sort_key), reverse=True)
    return groups


def _proc_sort_value(row: ProcRow, sort_key: str) -> float:
    if sort_key == "mem":
        return row.mem_pct
    if sort_key == "cpu":
        return row.cpu
    if sort_key == "rss":
        return row.rss_mb
    if sort_key == "swap":
        return row.swap_mb
    if sort_key == "io":
        return row.io_read_mb + row.io_write_mb
    return float(row.pid)


def sort_processes(rows: list[ProcRow], sort_key: str) -> list[ProcRow]:
    rows.sort(key=lambda row: _proc_sort_value(row, sort_key), reverse=True)
    return rows


def print_once(filter_re: re.Pattern[str] | None, sort_key: str, top: int) -> None:
    sample = sample_processes(filter_re)
    groups = sort_groups(aggregate(sample.rows), sort_key)
    if sample.error:
        print(sample.error, file=sys.stderr)
    print(
        f"{'APP':{MAX_APP_NAME}} {'PROCS':>5} {'RSS(MiB)':>10} {'SWAP(MiB)':>10}"
        f" {'%MEM':>7} {'%CPU':>7} {'IO_R(MB)':>10} {'IO_W(MB)':>10}"
    )
    for group in groups[:top]:
        print(
            f"{group.app[:MAX_APP_NAME]:{MAX_APP_NAME}} {group.procs:>5} {group.rss_mb:>10.1f}"
            f" {group.swap_mb:>10.1f} {group.mem_pct:>7.1f} {group.cpu:>7.1f}"
            f" {group.io_read_mb:>10.1f} {group.io_write_mb:>10.1f}"
        )


def clamp(value, low, high):
    return max(low, min(high, value))


def visible_window_start(selected: int, current_start: int, window_size: int, total: int) -> int:
    if window_size <= 0 or total <= window_size:
        return 0
    if selected < current_start:
        current_start = selected
    elif selected >= current_start + window_size:
        current_start = selected - window_size + 1
    return clamp(current_start, 0, max(0, total - window_size))


def _tree_sort_key(sort_key: str):
    if sort_key == "mem":
        return lambda row: row.mem_pct
    if sort_key == "cpu":
        return lambda row: row.cpu
    if sort_key == "rss":
        return lambda row: row.rss_mb
    if sort_key == "swap":
        return lambda row: row.swap_mb
    if sort_key == "io":
        return lambda row: row.io_read_mb + row.io_write_mb
    return lambda row: row.rss_mb


def build_process_tree(procs: list[ProcRow], sort_key: str = "rss") -> list[tuple[ProcRow, int, str]]:
    """Build a flattened tree list from processes using ppid."""
    by_pid = {row.pid: row for row in procs}
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    pids_in_group = {row.pid for row in procs}
    keyfn = _tree_sort_key(sort_key)

    for row in procs:
        if row.ppid in pids_in_group and row.ppid != row.pid:
            children.setdefault(row.ppid, []).append(row.pid)
        else:
            roots.append(row.pid)

    roots.sort(key=lambda pid: keyfn(by_pid[pid]), reverse=True)
    result: list[tuple[ProcRow, int, str]] = []

    def walk(pid: int, depth: int, continuation: list[bool]) -> None:
        proc = by_pid.get(pid)
        if proc is None:
            return
        prefix = ""
        if depth > 0:
            for has_more in continuation[:-1]:
                prefix += "│   " if has_more else "    "
            prefix += "├── " if continuation[-1] else "└── "
        result.append((proc, depth, prefix))
        kids = children.get(pid, [])
        kids.sort(key=lambda child_pid: keyfn(by_pid[child_pid]), reverse=True)
        for idx, child_pid in enumerate(kids):
            walk(child_pid, depth + 1, continuation + [idx < len(kids) - 1])

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
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(10, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_BLUE)
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
        self.scroll = 0
        self.groups: list[GroupRow] = []
        self.detail_list: list[ProcRow] = []
        self.detail_tree: list[tuple[ProcRow, int, str]] = []
        self.tree_mode = False
        self.detail_app: str | None = None
        self.last = 0.0
        self.last_proc_rows: list[ProcRow] = []
        self.alerts_cache: list[str] = []
        self.alerts_cache_time = 0.0
        self.status_msg = ""
        self.status_time = 0.0
        self.sample_error = ""
        self.sel_attr = curses.A_REVERSE
        if curses.has_colors():
            self.sel_attr = curses.color_pair(7) | curses.A_BOLD

    def _set_timeout(self, refresh):
        self.stdscr.timeout(int(refresh * 1000))

    def _content_rows(self, maxy: int) -> int:
        return max(1, maxy - 15)

    def _reset_scroll(self) -> None:
        self.scroll = 0

    def _sync_scroll(self, maxy: int) -> None:
        self.scroll = visible_window_start(self.sel, self.scroll, self._content_rows(maxy), self.length())

    def _selected_group(self) -> GroupRow | None:
        if not self.groups:
            return None
        return self.groups[self.sel]

    def _selected_proc(self) -> ProcRow | None:
        if self.view != "detail":
            return None
        if self.tree_mode:
            if not self.detail_tree:
                return None
            return self.detail_tree[self.sel][0]
        if not self.detail_list:
            return None
        return self.detail_list[self.sel]

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
            if ch in (curses.KEY_DOWN, ord("j")):
                self.sel = clamp(self.sel + 1, 0, max(0, self.length() - 1))
            elif ch == curses.KEY_UP:
                self.sel = clamp(self.sel - 1, 0, max(0, self.length() - 1))
            elif ch == curses.KEY_NPAGE:
                self.sel = clamp(self.sel + 10, 0, max(0, self.length() - 1))
            elif ch == curses.KEY_PPAGE:
                self.sel = clamp(self.sel - 10, 0, max(0, self.length() - 1))
            elif ch == curses.KEY_HOME:
                self.sel = 0
            elif ch == curses.KEY_END:
                self.sel = max(0, self.length() - 1)
            elif ch in (10, 13, curses.KEY_ENTER, ord("l")):
                self.toggle_view()
            elif ch == ord("h"):
                if self.view == "detail":
                    self.toggle_view()
            elif ch == ord("s"):
                idx = (SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)
                self.sort_key = SORT_KEYS[idx]
                self.sel = 0
                self._reset_scroll()
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
                self._reset_scroll()
                self.status("Filter cleared")
            elif ch == ord("f"):
                self.prompt_filter()
                self.sel = 0
                self._reset_scroll()
            elif ch == ord("k"):
                if self.view == "detail":
                    self.kill_selected(signal.SIGTERM)
                elif self.view == "groups":
                    self.kill_group(signal.SIGTERM)
            elif ch == ord("K"):
                if self.view == "detail":
                    self.kill_selected(signal.SIGKILL)
                elif self.view == "groups":
                    self.kill_group(signal.SIGKILL)
            elif ch == ord("t") and self.view == "detail":
                self.tree_mode = not self.tree_mode
                self.sel = 0
                self._reset_scroll()
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
        sample = sample_processes(self.filter_re)
        groups = sort_groups(aggregate(sample.rows), self.sort_key)
        self.groups = groups
        self.last_proc_rows = sample.rows
        self.sample_error = sample.error

        if self.view == "detail" and self.detail_app:
            detail_list = [row for row in sample.rows if row.app == self.detail_app]
            self.detail_list = sort_processes(detail_list, self.sort_key)
            self.detail_tree = build_process_tree(detail_list, self.sort_key)
        else:
            self.detail_list = []
            self.detail_tree = []

        self.sel = clamp(self.sel, 0, max(0, self.length() - 1))

    def toggle_view(self):
        if self.view == "groups":
            group = self._selected_group()
            if group is None:
                return
            self.detail_app = group.app
            self.view = "detail"
        else:
            self.view = "groups"
            self.detail_app = None
            self.tree_mode = False
            self.detail_list = []
            self.detail_tree = []
        self.sel = 0
        self._reset_scroll()

    def draw(self):
        self.stdscr.erase()
        maxy, maxx = self.stdscr.getmaxyx()
        self._sync_scroll(maxy)

        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        gib = 1024 ** 3
        try:
            l1, l5, l15 = os.getloadavg()
        except OSError:
            l1 = l5 = l15 = 0.0
        cpu_count = os.cpu_count() or 1

        bg_title = curses.color_pair(10) | curses.A_BOLD
        bg_label = curses.color_pair(11) | curses.A_BOLD
        bg_ok = curses.color_pair(17) | curses.A_BOLD
        bg_warn = curses.color_pair(18) | curses.A_BOLD
        bg_crit = curses.color_pair(19) | curses.A_BOLD
        bg_neut = curses.color_pair(16)
        bg_buf = curses.color_pair(15) | curses.A_BOLD
        bg_swap_l = curses.color_pair(20) | curses.A_BOLD
        bg_swap = curses.color_pair(21) | curses.A_BOLD
        bg_gap = curses.color_pair(1)

        def mem_bg(percent):
            if percent > 90:
                return bg_crit
            if percent > 70:
                return bg_warn
            return bg_ok

        def swap_bg(percent):
            if percent > 80:
                return bg_crit
            if percent > 50:
                return bg_warn
            return bg_swap

        def load_bg(load_val):
            per_core = load_val / cpu_count
            if per_core > 2.0:
                return bg_crit
            if per_core > 1.0:
                return bg_warn
            return bg_ok

        def avail_bg(gib_val, threshold):
            return bg_ok if gib_val > threshold else bg_warn

        mem_used = vm.used / gib
        mem_total = vm.total / gib
        mem_avail = getattr(vm, "available", 0) / gib
        mem_free = getattr(vm, "free", 0) / gib
        mem_buf = getattr(vm, "buffers", 0) / gib
        mem_cache = getattr(vm, "cached", 0) / gib
        badges = [
            ("", "ptop3", bg_title, bg_title),
            ("mem", f"{mem_used:.1f}/{mem_total:.1f}G", bg_label, mem_bg(vm.percent)),
            ("avl", f"{mem_avail:.1f}G", bg_label, avail_bg(mem_avail, 2.0)),
            ("free", f"{mem_free:.1f}G", bg_label, avail_bg(mem_free, 1.0)),
            ("buf", f"{mem_buf:.1f}G", bg_buf, bg_neut),
            ("cache", f"{mem_cache:.1f}G", bg_buf, bg_neut),
            ("swap", f"{sm.used/gib:.1f}/{sm.total/gib:.1f}G", bg_swap_l, swap_bg(sm.percent)),
            ("load", f"{l1:.2f} {l5:.2f} {l15:.2f}", bg_label, load_bg(l1)),
            ("sort", self.sort_key, bg_label, bg_neut),
            ("ref", f"{self.refresh:.1f}s", bg_label, bg_neut),
            ("filt", self.filter_text or "-", bg_label, bg_neut),
        ]

        col = 0
        for label, value, label_attr, value_attr in badges:
            if col >= maxx - 1:
                break
            if label:
                label_text = f" {label} "
                self.addstr(0, col, label_text[: maxx - col - 1], label_attr)
                col += len(label_text)
            value_text = f" {value} "
            if col < maxx - 1:
                self.addstr(0, col, value_text[: maxx - col - 1], value_attr)
                col += len(value_text)
            if col < maxx - 1:
                self.addstr(0, col, " ", bg_gap)
                col += 1

        if self.view == "groups":
            self.draw_groups(maxx, maxy)
        else:
            self.draw_detail(maxx, maxy)

        alerts = self.collect_alerts()
        if alerts:
            alert_start_y = maxy - len(alerts) - 2
            for idx, alert in enumerate(alerts):
                y = alert_start_y + idx
                if y < 2:
                    continue
                color = curses.color_pair(3) if "critical" in alert.lower() else curses.color_pair(1)
                if "critical" in alert.lower():
                    color |= curses.A_BOLD
                self.addstr(y, 0, alert[: maxx - 1], color)

        status_line = self._status_line()
        if status_line:
            self.addstr(maxy - 2, 0, status_line[: maxx - 1], curses.color_pair(2))

        help_line = (
            "↑/↓ PgUp/PgDn Home/End  Enter/l expand  h back  s sort  f filter"
            "  r reset  +/- refresh  t tree  k/K kill  g kill-group  w swap-clean  d drop-caches  q quit"
        )
        self.addstr(maxy - 1, 0, help_line[: maxx - 1], curses.color_pair(5))
        self.stdscr.refresh()

    def _status_line(self) -> str:
        if self.status_msg and (time.time() - self.status_time) < 8.0:
            return self.status_msg
        return self.sample_error

    def _make_bar(self, pct, width=10):
        pct = max(0.0, min(100.0, pct))
        filled = int(round(pct / 100.0 * width))
        return "█" * filled + "░" * (width - filled)

    def draw_groups(self, maxx, maxy):
        bar_w = 10
        header = (
            f"{'APP':{MAX_APP_NAME}} {'PROCS':>5} {'RSS(MiB)':>10} {'SWAP(MiB)':>10}"
            f" {'%MEM':>7} {'MEM':>{bar_w}} {'%CPU':>7} {'CPU':>{bar_w}}"
            f" {'IO_R':>7} {'IO_W':>7}"
        )
        self.addstr(2, 0, header[: maxx - 1], curses.A_BOLD)

        available_rows = self._content_rows(maxy)
        visible_groups = self.groups[self.scroll:self.scroll + available_rows]
        for offset, group in enumerate(visible_groups):
            idx = self.scroll + offset
            y = 3 + offset
            mem_bar = self._make_bar(group.mem_pct, bar_w)
            cpu_bar = self._make_bar(min(group.cpu, 100.0), bar_w)
            line = (
                f"{group.app[:MAX_APP_NAME]:{MAX_APP_NAME}} {group.procs:>5} {group.rss_mb:>10.1f} {group.swap_mb:>10.1f}"
                f" {group.mem_pct:>7.1f} {mem_bar} {group.cpu:>7.1f} {cpu_bar}"
                f" {group.io_read_mb:>7.1f} {group.io_write_mb:>7.1f}"
            )
            color = self.sel_attr if idx == self.sel else self.color_group(group)
            self.addstr(y, 0, line[: maxx - 1], color)

    def draw_detail(self, maxx, maxy):
        mode_label = " [TREE]" if self.tree_mode else ""
        self.addstr(2, 0, f"App: {self.detail_app}{mode_label}", curses.A_BOLD)
        available_rows = self._content_rows(maxy)

        if self.tree_mode:
            self.addstr(
                3,
                0,
                f"{'PID':>7} {'RSS(MiB)':>9} {'%CPU':>6} {'%MEM':>6} {'SWAP':>6}  TREE",
                curses.A_BOLD,
            )
            visible_rows = self.detail_tree[self.scroll:self.scroll + available_rows]
            for offset, (row, depth, prefix) in enumerate(visible_rows):
                idx = self.scroll + offset
                y = 4 + offset
                stats_w = 40
                remaining = maxx - stats_w - 1
                tree_str = row.cmdline or row.name
                if depth > 0:
                    tree_str = prefix + tree_str
                tree_str = tree_str[: max(remaining, 10)]
                line = (
                    f"{row.pid:>7} {row.rss_mb:>9.1f} {row.cpu:>6.1f} "
                    f"{row.mem_pct:>6.1f} {row.swap_mb:>6.1f}  {tree_str}"
                )
                color = self.sel_attr if idx == self.sel else self.color_proc(row)
                self.addstr(y, 0, line[: maxx - 1], color)
            return

        self.addstr(
            3,
            0,
            f"{'PID':>7} {'PPID':>7} {'RSS(MiB)':>9} {'%CPU':>6} {'%MEM':>6}"
            f" {'SWAP(MiB)':>9} {'IO_R':>7} {'IO_W':>7}  CMD",
            curses.A_BOLD,
        )
        visible_rows = self.detail_list[self.scroll:self.scroll + available_rows]
        for offset, row in enumerate(visible_rows):
            idx = self.scroll + offset
            y = 4 + offset
            stats_w = 66
            remaining = maxx - stats_w - 1
            cmd = (row.cmdline or row.name)[: max(remaining, 10)]
            line = (
                f"{row.pid:>7} {row.ppid:>7} {row.rss_mb:>9.1f} {row.cpu:>6.1f} {row.mem_pct:>6.1f}"
                f" {row.swap_mb:>9.1f} {row.io_read_mb:>7.1f} {row.io_write_mb:>7.1f}  {cmd}"
            )
            color = self.sel_attr if idx == self.sel else self.color_proc(row)
            self.addstr(y, 0, line[: maxx - 1], color)

    def color_group(self, group: GroupRow):
        if group.cpu >= CPU_HOT * 2 or group.rss_mb >= RSS_HOT_MB * 2 or group.swap_mb >= SWAP_HOT_MB * 2:
            return curses.color_pair(4) | curses.A_BOLD
        if group.cpu >= CPU_HOT or group.rss_mb >= RSS_HOT_MB or group.swap_mb >= SWAP_HOT_MB:
            return curses.color_pair(3) | curses.A_BOLD
        return curses.color_pair(1)

    def color_proc(self, row: ProcRow):
        if row.cpu >= CPU_HOT * 2 or row.rss_mb >= RSS_HOT_MB * 2 or row.swap_mb >= SWAP_HOT_MB * 2:
            return curses.color_pair(4) | curses.A_BOLD
        if row.cpu >= CPU_HOT or row.rss_mb >= RSS_HOT_MB or row.swap_mb >= SWAP_HOT_MB:
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
            except OSError:
                pass

            zombie_count = sum(1 for row in self.last_proc_rows if row.status == "zombie")
            if zombie_count > 0:
                alerts.append(f"{now} SYSTEM  {'':<10} {'':<30}: {zombie_count} zombie processes detected")

            hot_rows = [row for row in self.last_proc_rows if row.cpu >= 5.0 or row.mem_pct >= 5.0]
            hot_rows = sorted(hot_rows, key=lambda row: row.cpu + row.mem_pct, reverse=True)[:20]
            for row in hot_rows:
                cmd_short = (row.cmdline or row.name)[:30]
                if row.cpu >= CPU_HOT * 2:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: CPU critical ({row.cpu:.1f}%)")
                elif row.cpu >= CPU_HOT:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: High CPU ({row.cpu:.1f}%)")
                if row.mem_pct >= 15.0:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: MEMORY CRITICAL ({row.mem_pct:.1f}%)")
                elif row.mem_pct >= 10.0:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: High memory ({row.mem_pct:.1f}%)")
                if row.swap_mb >= SWAP_HOT_MB * 3:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: SWAP CRITICAL ({row.swap_mb:.1f}MB)")
                elif row.swap_mb >= SWAP_HOT_MB:
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: High swap ({row.swap_mb:.1f}MB)")
                if row.mem_pct >= 5.0 and row.swap_mb >= SWAP_HOT_MB:
                    alerts.append(
                        f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: Memory pressure"
                        f" ({row.mem_pct:.1f}% + {row.swap_mb:.1f}MB)"
                    )
                if row.io_read_mb > 100.0 or row.io_write_mb > 100.0:
                    alerts.append(
                        f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: High I/O"
                        f" ({row.io_read_mb:.1f}MB read, {row.io_write_mb:.1f}MB write)"
                    )
                if row.status == "zombie":
                    alerts.append(f"{now} {row.pid:>6} {row.app[:10]:<10} {cmd_short:<30}: ZOMBIE PROCESS")

            for group in sorted(self.groups, key=lambda row: row.cpu, reverse=True)[:10]:
                if group.cpu >= CPU_HOT * 2:
                    alerts.append(
                        f"{now} group   {group.app[:10]:<10} {group.procs:>3}procs{'':<26}: Group CPU critical ({group.cpu:.1f}%)"
                    )
                elif group.cpu >= CPU_HOT:
                    alerts.append(
                        f"{now} group   {group.app[:10]:<10} {group.procs:>3}procs{'':<26}: Group high CPU ({group.cpu:.1f}%)"
                    )
                if group.swap_mb >= SWAP_HOT_MB * 4:
                    alerts.append(
                        f"{now} group   {group.app[:10]:<10} {group.procs:>3}procs{'':<26}: Group swap critical ({group.swap_mb:.1f}MB)"
                    )

            self.alerts_cache = alerts[-10:]
            self.alerts_cache_time = current_time
            return self.alerts_cache
        except Exception as exc:
            now = time.strftime("%H:%M:%S")
            error_alert = [f"{now} ERROR: Alert collection failed: {str(exc)[:40]}"]
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
        row = self._selected_proc()
        if row is None:
            return
        try:
            psutil.Process(row.pid).send_signal(sig)
            self.status(f"Sent {signal.Signals(sig).name} to {row.pid} ({row.name})")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            self.status(str(exc))

    def kill_group(self, sig=signal.SIGTERM):
        group = self._selected_group()
        if group is None:
            return
        sig_name = signal.Signals(sig).name
        count = 0
        denied = 0
        for proc in psutil.process_iter():
            try:
                name = proc.name() or ""
                try:
                    cmd = " ".join(proc.cmdline()[:4])
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    cmd = ""
                if normalize_app_name(name, cmd) == group.app:
                    proc.send_signal(sig)
                    count += 1
            except psutil.AccessDenied:
                denied += 1
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        msg = f"Sent {sig_name} to '{group.app}' ({count} procs)"
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
        except Exception as exc:
            self.status(f"swap-clean error: {str(exc)[:60]}")
            return
        if proc.returncode == 0:
            self.status("swap-clean finished")
            return
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        tail = f": {msg[0]}" if msg else ""
        if use_sudo and "password" in (proc.stderr or "").lower():
            self.status("swap-clean needs NOPASSWD sudo — run: sudo ptop3 --init-subscripts")
        else:
            self.status(f"swap-clean failed ({proc.returncode}){tail}")

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
        except Exception as exc:
            self.status(f"drop-caches error: {str(exc)[:60]}")
            return
        if proc.returncode == 0:
            msg = (proc.stdout or "").strip().splitlines()[-1:]
            tail = msg[0] if msg else "done"
            self.status(f"drop-caches: {tail}")
            return
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        tail = f": {msg[0]}" if msg else ""
        if use_sudo and "password" in (proc.stderr or "").lower():
            self.status("drop-caches needs NOPASSWD sudo — run: sudo ptop3 --init-subscripts")
        else:
            self.status(f"drop-caches failed ({proc.returncode}){tail}")

    def addstr(self, y, x, text, attrs=0):
        maxy, maxx = self.stdscr.getmaxyx()
        if 0 <= y < maxy and 0 <= x < maxx:
            self.stdscr.addnstr(y, x, text, maxx - x - 1, attrs)

    def clrtoeol(self):
        y, x = self.stdscr.getyx()
        _maxy, maxx = self.stdscr.getmaxyx()
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
    filter_re = None
    if args.filter:
        try:
            filter_re = re.compile(args.filter, re.IGNORECASE)
        except re.error:
            print("Invalid regex for --filter; ignoring.", file=sys.stderr)
    return args, filter_re


def main():
    args, filter_re = parse_args()
    global LITE_MODE
    LITE_MODE = bool(args.lite)

    if args.check_sudo:
        from ptop3.sudo_config import check_sudo

        result = check_sudo()
        for script, status in result.items():
            print(f"  {script}: {status}")
        sys.exit(0 if all(value == "ok" for value in result.values()) else 1)

    if args.init_subscripts:
        from ptop3.sudo_config import init_subscripts

        init_subscripts()
        return

    if args.once:
        print_once(filter_re, args.sort, args.top)
        return

    curses.wrapper(lambda stdscr: TUI(stdscr, filter_re, args.sort, args.refresh).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
