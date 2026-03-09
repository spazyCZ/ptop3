"""Tests for ptop3.monitor."""
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ptop3.monitor as monitor
from ptop3.monitor import SORT_KEYS, ProcRow, aggregate, build_process_tree, normalize_app_name

# ---------------------------------------------------------------------------
# normalize_app_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, cmd, expected", [
    ("code", "", "vscode"),
    ("code-insiders", "", "vscode-insiders"),
    ("chromium", "", "chrome"),
    ("chromium-browse", "", "chrome"),
    ("chrome", "", "chrome"),
    ("firefox", "", "firefox"),
    ("python3", "", "python"),
    ("python", "", "python"),
    ("cursor", "", "cursor"),
    ("gnome-shell", "", "gnome-shell"),
    # cmdline-specific aliases
    ("node", "/home/user/.cursor/extensions/foo", "cursor"),
    ("node", "/opt/cursor/cursor --type=renderer", "cursor"),
    ("sh", "cloud-code --some-arg", "cursor"),
    # fallback: unknown process
    ("myapp", "", "myapp"),
    ("", "", "unknown"),
])
def test_normalize_app_name(name, cmd, expected):
    assert normalize_app_name(name, cmd) == expected


def test_normalize_app_name_python3_via_name():
    assert normalize_app_name("python3", "") == "python"


def test_normalize_app_name_code_workspace_matches_vscode():
    # "code_workspace" starts with "code" — name prefix matching maps it to vscode
    result = normalize_app_name("code_workspace", "code_workspace --some-arg")
    assert result == "vscode"


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def _make_row(**kwargs) -> ProcRow:
    defaults = dict(
        pid=1, ppid=0, name="app", rss_mb=100.0, cpu=10.0,
        mem_pct=5.0, swap_mb=0.0, cmdline="", app="myapp",
        io_read_mb=0.0, io_write_mb=0.0,
        status="running",
    )
    defaults.update(kwargs)
    return ProcRow(**defaults)


def test_aggregate_single_proc():
    row = _make_row(app="foo", rss_mb=200.0, cpu=50.0, mem_pct=10.0)
    groups = aggregate([row])
    assert len(groups) == 1
    g = groups[0]
    assert g.app == "foo"
    assert g.procs == 1
    assert g.rss_mb == pytest.approx(200.0)
    assert g.cpu == pytest.approx(50.0)
    assert g.mem_pct == pytest.approx(10.0)


def test_aggregate_multiple_procs_same_app():
    rows = [
        _make_row(pid=1, app="foo", rss_mb=100.0, cpu=10.0, mem_pct=5.0, swap_mb=10.0),
        _make_row(pid=2, app="foo", rss_mb=200.0, cpu=20.0, mem_pct=8.0, swap_mb=5.0),
        _make_row(pid=3, app="foo", rss_mb=50.0,  cpu=5.0,  mem_pct=2.0, swap_mb=0.0),
    ]
    groups = aggregate(rows)
    assert len(groups) == 1
    g = groups[0]
    assert g.procs == 3
    assert g.rss_mb == pytest.approx(350.0)
    assert g.cpu == pytest.approx(35.0)
    assert g.mem_pct == pytest.approx(15.0)
    assert g.swap_mb == pytest.approx(15.0)


def test_aggregate_multiple_apps():
    rows = [
        _make_row(pid=1, app="foo", rss_mb=100.0),
        _make_row(pid=2, app="bar", rss_mb=200.0),
        _make_row(pid=3, app="foo", rss_mb=50.0),
    ]
    groups = aggregate(rows)
    assert len(groups) == 2
    by_app = {g.app: g for g in groups}
    assert by_app["foo"].procs == 2
    assert by_app["foo"].rss_mb == pytest.approx(150.0)
    assert by_app["bar"].procs == 1


def test_aggregate_io_fields():
    rows = [
        _make_row(pid=1, app="foo", io_read_mb=10.0, io_write_mb=5.0),
        _make_row(pid=2, app="foo", io_read_mb=20.0, io_write_mb=3.0),
    ]
    groups = aggregate(rows)
    g = groups[0]
    assert g.io_read_mb == pytest.approx(30.0)
    assert g.io_write_mb == pytest.approx(8.0)


def test_aggregate_empty():
    assert aggregate([]) == []


# ---------------------------------------------------------------------------
# build_process_tree
# ---------------------------------------------------------------------------

def test_build_process_tree_flat():
    """Processes without parent relationships form flat roots."""
    rows = [
        _make_row(pid=1, ppid=0, app="foo", rss_mb=100.0),
        _make_row(pid=2, ppid=0, app="foo", rss_mb=200.0),
    ]
    tree = build_process_tree(rows, sort_key="rss")
    assert len(tree) == 2
    # Roots sorted by rss descending: pid 2 first
    procs = [item[0] for item in tree]
    assert procs[0].pid == 2
    assert procs[1].pid == 1
    # All at depth 0
    assert all(item[1] == 0 for item in tree)


def test_build_process_tree_parent_child():
    """Child process appears under parent with correct prefix."""
    rows = [
        _make_row(pid=1, ppid=0, app="foo", rss_mb=200.0),
        _make_row(pid=2, ppid=1, app="foo", rss_mb=100.0),
    ]
    tree = build_process_tree(rows, sort_key="rss")
    assert len(tree) == 2
    root_item = tree[0]
    child_item = tree[1]
    assert root_item[0].pid == 1
    assert root_item[1] == 0  # depth
    assert child_item[0].pid == 2
    assert child_item[1] == 1  # depth
    assert "└──" in child_item[2] or "├──" in child_item[2]


def test_build_process_tree_prefix_connectors():
    """Multiple children get correct ├──/└── connectors."""
    rows = [
        _make_row(pid=10, ppid=0,  app="x", rss_mb=500.0),
        _make_row(pid=11, ppid=10, app="x", rss_mb=300.0),
        _make_row(pid=12, ppid=10, app="x", rss_mb=100.0),
    ]
    tree = build_process_tree(rows, sort_key="rss")
    assert len(tree) == 3
    prefixes = [item[2] for item in tree]
    # Second item (first child) should have ├── because there is another child
    assert "├──" in prefixes[1]
    # Third item (last child) should have └──
    assert "└──" in prefixes[2]


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptop3"])
    from ptop3.monitor import parse_args
    args, fil = parse_args()
    assert args.once is False
    assert args.sort == "mem"
    assert args.refresh == 2.0
    assert args.lite is False
    assert fil is None


def test_parse_args_once(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptop3", "--once"])
    from ptop3.monitor import parse_args
    args, _ = parse_args()
    assert args.once is True


def test_parse_args_filter(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptop3", "--filter", "python"])
    from ptop3.monitor import parse_args
    args, fil = parse_args()
    assert fil is not None
    assert fil.search("python")


def test_parse_args_sort_choices(monkeypatch):
    for key in SORT_KEYS:
        monkeypatch.setattr("sys.argv", ["ptop3", "--sort", key])
        from ptop3.monitor import parse_args
        args, _ = parse_args()
        assert args.sort == key


def test_parse_args_check_sudo(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptop3", "--check-sudo"])
    from ptop3.monitor import parse_args
    args, _ = parse_args()
    assert args.check_sudo is True


def test_parse_args_init_subscripts(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ptop3", "--init-subscripts"])
    from ptop3.monitor import parse_args
    args, _ = parse_args()
    assert args.init_subscripts is True


def test_parse_args_invalid_filter_prints_warning(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ptop3", "--filter", "["])
    args, fil = monitor.parse_args()
    assert args.filter == "["
    assert fil is None
    assert "Invalid regex for --filter; ignoring." in capsys.readouterr().err


def test_find_subscript_uses_path_lookup(monkeypatch):
    monkeypatch.setattr(monitor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert monitor._find_subscript("ptop3-drop-caches") == "/usr/bin/ptop3-drop-caches"


def test_find_subscript_uses_dev_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor.shutil, "which", lambda name: None)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    fake_file = scripts_dir / "drop_caches.py"
    fake_file.write_text("print('ok')\n")

    monkeypatch.setattr(monitor, "__file__", str(tmp_path / "monitor.py"))

    assert monitor._find_subscript("ptop3-drop-caches") == str(fake_file)


def test_subscript_cmd_uses_python_for_dev_script(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor.shutil, "which", lambda name: None)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    fake_file = scripts_dir / "swap_clean.py"
    fake_file.write_text("print('ok')\n")
    monkeypatch.setattr(monitor, "__file__", str(tmp_path / "monitor.py"))

    assert monitor._subscript_cmd("ptop3-swap-clean") == [monitor.sys.executable, str(fake_file)]


def test_read_vmswap_mb_reads_and_caches(monkeypatch):
    monitor.SWAP_CACHE.clear()
    now = {"value": 100.0}

    def fake_time():
        return now["value"]

    file_obj = MagicMock()
    file_obj.__enter__.return_value = ["Name:\tproc\n", "VmSwap:\t2048 kB\n"]
    file_obj.__exit__.return_value = False
    opener = MagicMock(return_value=file_obj)

    monkeypatch.setattr(monitor.time, "time", fake_time)
    monkeypatch.setattr("builtins.open", opener)

    assert monitor.read_vmswap_mb(123) == pytest.approx(2.0)
    assert monitor.read_vmswap_mb(123) == pytest.approx(2.0)
    assert opener.call_count == 1


def test_read_vmswap_mb_missing_file_returns_zero(monkeypatch):
    monitor.SWAP_CACHE.clear()
    monkeypatch.setattr(monitor.time, "time", lambda: 50.0)

    def raising_open(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr("builtins.open", raising_open)
    assert monitor.read_vmswap_mb(999) == 0.0


class _FakeProc:
    def __init__(
        self,
        pid,
        ppid,
        name,
        cmdline,
        rss_mb,
        cpu,
        status="running",
        io_read_mb=0.0,
        io_write_mb=0.0,
        memory_denied=False,
    ):
        self.info = {"pid": pid, "ppid": ppid, "name": name}
        self.pid = pid
        self._cmdline = cmdline
        self._rss_bytes = int(rss_mb * 1024 * 1024)
        self._cpu = cpu
        self._status = status
        self._io_read_bytes = int(io_read_mb * 1024 * 1024)
        self._io_write_bytes = int(io_write_mb * 1024 * 1024)
        self._memory_denied = memory_denied

    def oneshot(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cmdline(self):
        return self._cmdline

    def memory_info(self):
        if self._memory_denied:
            raise monitor.psutil.AccessDenied(pid=self.info["pid"])
        return SimpleNamespace(rss=self._rss_bytes)

    def cpu_percent(self):
        return self._cpu

    def io_counters(self):
        return SimpleNamespace(read_bytes=self._io_read_bytes, write_bytes=self._io_write_bytes)

    def status(self):
        return self._status


def test_get_proc_rows_filters_and_collects_metrics(monkeypatch):
    monitor.PID_CACHE.clear()
    monitor.SWAP_CACHE.clear()
    monitor.LITE_MODE = False

    procs = [
        _FakeProc(10, 1, "python3", ["python3", "server.py"], rss_mb=64, cpu=12.5, status="sleeping", io_read_mb=5.0, io_write_mb=3.0),
        _FakeProc(11, 1, "bash", ["bash"], rss_mb=1, cpu=1.0, memory_denied=True),
    ]

    monkeypatch.setattr(monitor.psutil, "process_iter", lambda attrs=None: procs)
    monkeypatch.setattr(monitor.psutil, "virtual_memory", lambda: SimpleNamespace(total=1024 * 1024 * 1024))
    monkeypatch.setattr(monitor.ProcessSampler, "read_vmswap_mb", lambda self, pid: 4.0 if pid == 10 else 0.0)

    rows = monitor.get_proc_rows(filter_re=monitor.re.compile("python"))

    assert len(rows) == 1
    row = rows[0]
    assert row.pid == 10
    assert row.app == "python"
    assert row.cpu == pytest.approx(12.5)
    assert row.swap_mb == pytest.approx(4.0)
    assert row.io_read_mb == pytest.approx(5.0)
    assert row.io_write_mb == pytest.approx(3.0)
    assert row.status == "sleeping"


def test_print_once_sorts_by_io(monkeypatch, capsys):
    rows = [
        _make_row(pid=1, app="foo", io_read_mb=1.0, io_write_mb=2.0),
        _make_row(pid=2, app="bar", io_read_mb=10.0, io_write_mb=0.0),
    ]
    monkeypatch.setattr(monitor, "sample_processes", lambda filter_re: monitor.SampleResult(rows))

    monitor.print_once(None, "io", 2)

    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("APP")
    assert out[1].startswith("bar")
    assert out[2].startswith("foo")


@pytest.mark.parametrize(
    ("value", "lo", "hi", "expected"),
    [
        (-1, 0, 5, 0),
        (10, 0, 5, 5),
        (3, 0, 5, 3),
    ],
)
def test_clamp(value, lo, hi, expected):
    assert monitor.clamp(value, lo, hi) == expected


@pytest.mark.parametrize(
    ("sort_key", "attr"),
    [
        ("mem", "mem_pct"),
        ("cpu", "cpu"),
        ("rss", "rss_mb"),
        ("swap", "swap_mb"),
    ],
)
def test_tree_sort_key(sort_key, attr):
    row = _make_row(**{attr: 9.0})
    assert monitor._tree_sort_key(sort_key)(row) == 9.0


def test_tree_sort_key_io_and_default():
    row = _make_row(io_read_mb=2.0, io_write_mb=3.0, rss_mb=7.0)
    assert monitor._tree_sort_key("io")(row) == 5.0
    assert monitor._tree_sort_key("count")(row) == 7.0


class _FakeScreen:
    def __init__(self, keys=None, text=b""):
        self.calls = []
        self.keys = list(keys or [])
        self.text = text
        self.timeout_value = None
        self.keypad_value = None
        self.nodelay_calls = []
        self.erased = 0
        self.refreshed = 0

    def getmaxyx(self):
        return (24, 120)

    def addnstr(self, y, x, text, width, attrs=0):
        self.calls.append((y, x, text, width, attrs))

    def getyx(self):
        return (2, 3)

    def keypad(self, value):
        self.keypad_value = value

    def timeout(self, value):
        self.timeout_value = value

    def getch(self):
        if self.keys:
            return self.keys.pop(0)
        return -1

    def nodelay(self, value):
        self.nodelay_calls.append(value)

    def erase(self):
        self.erased += 1

    def refresh(self):
        self.refreshed += 1

    def getstr(self, y, x):
        return self.text


def _patch_fake_curses(monkeypatch, has_colors=True, curs_set_error=False, pair_error=False):
    monkeypatch.setattr(monitor.curses, "noecho", lambda: None)
    monkeypatch.setattr(monitor.curses, "cbreak", lambda: None)
    monkeypatch.setattr(monitor.curses, "start_color", lambda: None)
    monkeypatch.setattr(monitor.curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(monitor.curses, "has_colors", lambda: has_colors)

    pair_failed = {"value": False}

    def fake_curs_set(value):
        if curs_set_error:
            raise monitor.curses.error("no cursor")

    def fake_init_pair(pair, fg, bg):
        if pair_error and pair in (20, 21) and not pair_failed["value"]:
            pair_failed["value"] = True
            raise ValueError("fallback")

    monkeypatch.setattr(monitor.curses, "curs_set", fake_curs_set)
    monkeypatch.setattr(monitor.curses, "init_pair", fake_init_pair)
    monkeypatch.setattr(monitor.curses, "color_pair", lambda value: value * 100)


def _make_initialized_tui(monkeypatch, keys=None, text=b"", has_colors=True, curs_set_error=False, pair_error=False):
    _patch_fake_curses(monkeypatch, has_colors=has_colors, curs_set_error=curs_set_error, pair_error=pair_error)
    screen = _FakeScreen(keys=keys, text=text)
    tui = monitor.TUI(screen, None, "mem", 2.0)
    return tui, screen


def test_tui_length_and_toggle_view():
    tui = object.__new__(monitor.TUI)
    tui.view = "groups"
    tui.groups = [SimpleNamespace(app="python")]
    tui.detail_tree = [("tree",)]
    tui.detail_list = [_make_row()]
    tui.tree_mode = False
    tui.sel = 0

    assert tui.length() == 1
    tui.toggle_view()
    assert tui.view == "detail"
    assert tui.detail_app == "python"
    tui.tree_mode = True
    assert tui.length() == 1
    tui.toggle_view()
    assert tui.view == "groups"
    assert tui.detail_app is None
    assert tui.tree_mode is False


def test_tui_init_sets_up_screen(monkeypatch):
    tui, screen = _make_initialized_tui(monkeypatch, has_colors=True, curs_set_error=True, pair_error=True)

    assert screen.keypad_value is True
    assert screen.timeout_value == 2000
    assert tui.sel_attr == monitor.curses.color_pair(7) | monitor.curses.A_BOLD


def test_tui_init_without_colors_uses_reverse(monkeypatch):
    tui, _screen = _make_initialized_tui(monkeypatch, has_colors=False)

    assert tui.sel_attr == monitor.curses.A_REVERSE


def test_tui_addstr_and_clrtoeol():
    tui = object.__new__(monitor.TUI)
    tui.stdscr = _FakeScreen()

    tui.addstr(1, 1, "hello", 7)
    tui.addstr(99, 1, "skip", 0)
    tui.clrtoeol()

    assert tui.stdscr.calls[0][:3] == (1, 1, "hello")
    assert tui.stdscr.calls[1][0:2] == (2, 3)


def test_tui_status_sets_timestamp(monkeypatch):
    tui = object.__new__(monitor.TUI)
    monkeypatch.setattr(monitor.time, "time", lambda: 123.0)
    tui.status("ready")
    assert tui.status_msg == "ready"
    assert tui.status_time == 123.0


def test_tui_content_and_selected_helpers():
    tui = object.__new__(monitor.TUI)
    tui.scroll = 3
    tui.sel = 0
    tui.view = "groups"
    tui.groups = [_make_row(app="one"), _make_row(app="two")]
    tui.tree_mode = False
    tui.detail_list = []
    tui.detail_tree = []

    assert tui._content_rows(20) == 5
    tui._reset_scroll()
    assert tui.scroll == 0
    assert tui._selected_group().app == "one"
    assert tui._selected_proc() is None


def test_tui_sync_scroll(monkeypatch):
    tui = object.__new__(monitor.TUI)
    tui.sel = 6
    tui.scroll = 0
    tui.view = "groups"
    tui.groups = [_make_row(app=str(i)) for i in range(10)]
    tui.tree_mode = False
    tui.detail_list = []
    tui.detail_tree = []

    tui._sync_scroll(20)

    assert tui.scroll == 2


def test_tui_kill_selected(monkeypatch):
    signals = []
    proc = SimpleNamespace(send_signal=lambda sig: signals.append(sig))
    monkeypatch.setattr(monitor.psutil, "Process", lambda pid: proc)

    tui = object.__new__(monitor.TUI)
    tui.view = "detail"
    tui.tree_mode = False
    tui.detail_list = [_make_row(pid=42, name="worker")]
    tui.sel = 0
    tui.status = lambda msg: setattr(tui, "last_status", msg)

    tui.kill_selected(monitor.signal.SIGTERM)

    assert signals == [monitor.signal.SIGTERM]
    assert "42" in tui.last_status


def test_tui_kill_selected_uses_tree_selection(monkeypatch):
    signals = []
    proc = SimpleNamespace(send_signal=lambda sig: signals.append(sig))
    monkeypatch.setattr(monitor.psutil, "Process", lambda pid: proc)

    tui = object.__new__(monitor.TUI)
    tui.view = "detail"
    tui.tree_mode = True
    tui.detail_list = [_make_row(pid=1, name="wrong")]
    tui.detail_tree = [(_make_row(pid=99, name="right"), 0, "")]
    tui.sel = 0
    tui.status = lambda msg: setattr(tui, "last_status", msg)

    tui.kill_selected(monitor.signal.SIGTERM)

    assert signals == [monitor.signal.SIGTERM]
    assert "99" in tui.last_status


def test_tui_kill_group(monkeypatch):
    killed = []

    class FakeProcess:
        def __init__(self, name, cmd):
            self._name = name
            self._cmd = cmd

        def name(self):
            return self._name

        def cmdline(self):
            return self._cmd

        def send_signal(self, sig):
            killed.append(sig)

    monkeypatch.setattr(
        monitor.psutil,
        "process_iter",
        lambda: [FakeProcess("python3", ["python3"]), FakeProcess("bash", ["bash"])],
    )

    tui = object.__new__(monitor.TUI)
    tui.groups = [SimpleNamespace(app="python")]
    tui.sel = 0
    tui.status = lambda msg: setattr(tui, "last_status", msg)

    tui.kill_group()

    assert killed == [monitor.signal.SIGTERM]
    assert "python" in tui.last_status


def test_tui_kill_group_denied(monkeypatch):
    class FakeProcess:
        def name(self):
            raise monitor.psutil.AccessDenied(pid=1)

    monkeypatch.setattr(monitor.psutil, "process_iter", lambda: [FakeProcess()])

    tui = object.__new__(monitor.TUI)
    tui.groups = [SimpleNamespace(app="python")]
    tui.sel = 0
    tui.status = lambda msg: setattr(tui, "last_status", msg)

    tui.kill_group()

    assert "denied" in tui.last_status


def test_tui_run_swap_clean_handles_password_error(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: ["ptop3-swap-clean"])
    monkeypatch.setattr(monitor.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="sudo: a password is required", stdout=""),
    )

    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)
    tui.draw = lambda: None

    tui.run_swap_clean()

    assert msgs[-1].startswith("swap-clean needs NOPASSWD sudo")


def test_tui_run_swap_clean_not_found(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: None)
    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)

    tui.run_swap_clean()

    assert msgs[-1].startswith("ptop3-swap-clean not found")


def test_tui_run_swap_clean_exception(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: ["ptop3-swap-clean"])
    monkeypatch.setattr(monitor.os, "geteuid", lambda: 0)

    def raise_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(monitor.subprocess, "run", raise_run)
    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)
    tui.draw = lambda: None

    tui.run_swap_clean()

    assert msgs[-1].startswith("swap-clean error:")


def test_tui_run_drop_caches_success(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: ["ptop3-drop-caches"])
    monkeypatch.setattr(monitor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout="Dropped caches (level 3), freed 0 MB.\n"),
    )

    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)
    tui.draw = lambda: None

    tui.run_drop_caches()

    assert msgs[-1] == "drop-caches: Dropped caches (level 3), freed 0 MB."


def test_tui_run_drop_caches_not_found(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: None)
    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)

    tui.run_drop_caches()

    assert msgs[-1].startswith("ptop3-drop-caches not found")


def test_tui_run_drop_caches_generic_failure(monkeypatch):
    monkeypatch.setattr(monitor, "_subscript_cmd", lambda name: ["ptop3-drop-caches"])
    monkeypatch.setattr(monitor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stderr="failed badly", stdout=""),
    )

    tui = object.__new__(monitor.TUI)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)
    tui.draw = lambda: None

    tui.run_drop_caches()

    assert msgs[-1].startswith("drop-caches failed (2)")


def test_visible_window_start():
    assert monitor.visible_window_start(selected=0, current_start=0, window_size=5, total=10) == 0
    assert monitor.visible_window_start(selected=6, current_start=0, window_size=5, total=10) == 2
    assert monitor.visible_window_start(selected=9, current_start=2, window_size=5, total=10) == 5


def test_visible_window_start_small_window():
    assert monitor.visible_window_start(selected=3, current_start=2, window_size=0, total=10) == 0
    assert monitor.visible_window_start(selected=2, current_start=2, window_size=5, total=3) == 0


def test_sort_helpers():
    groups = monitor.sort_groups([
        monitor.GroupRow(app="a", procs=1, rss_mb=1, mem_pct=2, cpu=3, swap_mb=4, io_read_mb=0, io_write_mb=1),
        monitor.GroupRow(app="b", procs=5, rss_mb=10, mem_pct=1, cpu=1, swap_mb=0, io_read_mb=0, io_write_mb=0),
    ], "count")
    procs = monitor.sort_processes([
        _make_row(pid=1, rss_mb=1.0),
        _make_row(pid=2, rss_mb=5.0),
    ], "rss")

    assert groups[0].app == "b"
    assert procs[0].pid == 2


def test_helper_functions(monkeypatch):
    proc = _FakeProc(1, 0, "python3", ["python3"], rss_mb=64, cpu=1.0, io_read_mb=2.0, io_write_mb=3.0)

    assert monitor._matches_filter(monitor.re.compile("py").search, "python", "python3", "python3") is True
    assert monitor._safe_cmdline(proc) == "python3"
    assert monitor._io_values(proc, 64, False) == (2.0, 3.0)
    assert monitor._io_values(proc, 1, False) == (0.0, 0.0)
    assert monitor._swap_value(1, 1, False) == 0.0

    monkeypatch.setattr(monitor, "read_vmswap_mb", lambda pid: 9.0)
    assert monitor._swap_value(1, 64, False) == 9.0


def test_safe_cmdline_handles_access_denied():
    class DeniedProc:
        def cmdline(self):
            raise monitor.psutil.AccessDenied(pid=1)

    assert monitor._safe_cmdline(DeniedProc()) == ""


def test_color_helpers(monkeypatch):
    _patch_fake_curses(monkeypatch)
    tui = object.__new__(monitor.TUI)

    assert tui.color_group(monitor.GroupRow(app="x", procs=1, rss_mb=2000, mem_pct=1, cpu=1, swap_mb=1)) == monitor.curses.color_pair(4) | monitor.curses.A_BOLD
    assert tui.color_group(monitor.GroupRow(app="x", procs=1, rss_mb=900, mem_pct=1, cpu=1, swap_mb=1)) == monitor.curses.color_pair(3) | monitor.curses.A_BOLD
    assert tui.color_proc(_make_row(cpu=250)) == monitor.curses.color_pair(4) | monitor.curses.A_BOLD
    assert tui.color_proc(_make_row(cpu=120)) == monitor.curses.color_pair(3) | monitor.curses.A_BOLD


def test_sampler_cleanup_and_failure_paths(monkeypatch):
    sampler = monitor.ProcessSampler(pid_cache_ttl=1.0, swap_cache_ttl=1.0)
    sampler.pid_cache = {1: (0.0, "a", "", "a")}
    sampler.swap_cache = {1: (0.0, 1.0)}
    sampler._cleanup_cache(20.0)

    assert sampler.pid_cache == {}
    assert sampler.swap_cache == {}

    monkeypatch.setattr(monitor.psutil, "virtual_memory", lambda: SimpleNamespace(total=1024))
    monkeypatch.setattr(monitor.psutil, "process_iter", lambda attrs=None: (_ for _ in ()).throw(RuntimeError("no iter")))

    sample = sampler.sample(None)

    assert sample.rows == []
    assert sample.error.startswith("sampling failed:")


def test_sampler_sample_process_cached_and_status_unknown(monkeypatch):
    sampler = monitor.ProcessSampler()
    proc = _FakeProc(1, 0, "python3", ["python3"], rss_mb=4, cpu=3.0)
    sampler.pid_cache[1] = (10.0, "python3", "", "python")

    def raising_status():
        raise monitor.psutil.AccessDenied(pid=1)

    proc.status = raising_status
    row = sampler._sample_process(proc, 10.1, 1.0, False, monitor.re.compile("python").search)

    assert row is not None
    assert row.status == "unknown"


def test_sampler_sample_process_uses_sampler_swap_reader(monkeypatch):
    sampler = monitor.ProcessSampler()
    proc = _FakeProc(1, 0, "python3", ["python3"], rss_mb=64, cpu=3.0)

    monkeypatch.setattr(monitor, "read_vmswap_mb", lambda pid: 0.0)
    monkeypatch.setattr(monitor.ProcessSampler, "read_vmswap_mb", lambda self, pid: 7.0)

    row = sampler._sample_process(proc, 1.0, 1.0, False, None)

    assert row is not None
    assert row.swap_mb == 7.0


def test_sampler_sample_process_filtered_out_and_memory_denied(monkeypatch):
    sampler = monitor.ProcessSampler()
    proc = _FakeProc(1, 0, "bash", ["bash"], rss_mb=4, cpu=1.0)
    assert sampler._sample_process(proc, 1.0, 1.0, False, monitor.re.compile("python").search) is None

    denied = _FakeProc(2, 0, "python3", ["python3"], rss_mb=4, cpu=1.0, memory_denied=True)
    assert sampler._sample_process(denied, 1.0, 1.0, False, None) is None


def test_sampler_read_vmswap_without_vmswap_line(monkeypatch):
    sampler = monitor.ProcessSampler()
    monkeypatch.setattr(monitor.time, "time", lambda: 1.0)
    file_obj = MagicMock()
    file_obj.__enter__.return_value = ["Name:\tproc\n"]
    file_obj.__exit__.return_value = False
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: file_obj)

    assert sampler.read_vmswap_mb(1) == 0.0


def test_tui_sample_updates_detail_views(monkeypatch):
    rows = [
        _make_row(pid=1, app="python", cpu=10.0, rss_mb=10.0),
        _make_row(pid=2, app="python", cpu=5.0, rss_mb=20.0, ppid=1),
        _make_row(pid=3, app="bash", cpu=1.0, rss_mb=1.0),
    ]
    monkeypatch.setattr(monitor, "sample_processes", lambda filter_re: monitor.SampleResult(rows, "warn"))

    tui = object.__new__(monitor.TUI)
    tui.filter_re = None
    tui.sort_key = "cpu"
    tui.view = "detail"
    tui.detail_app = "python"
    tui.tree_mode = True
    tui.sel = 10

    tui.sample()

    assert tui.sample_error == "warn"
    assert [group.app for group in tui.groups] == ["python", "bash"]
    assert [row.pid for row in tui.detail_list] == [1, 2]
    assert tui.detail_tree[0][0].pid == 1
    assert tui.sel == 1


def test_tui_sample_clears_detail_when_in_group_view(monkeypatch):
    monkeypatch.setattr(monitor, "sample_processes", lambda filter_re: monitor.SampleResult([]))
    tui = object.__new__(monitor.TUI)
    tui.filter_re = None
    tui.sort_key = "mem"
    tui.view = "groups"
    tui.detail_app = None
    tui.tree_mode = False
    tui.sel = 0

    tui.sample()

    assert tui.detail_list == []
    assert tui.detail_tree == []


def test_tui_status_line_prefers_recent_status(monkeypatch):
    tui = object.__new__(monitor.TUI)
    tui.status_msg = "ready"
    tui.status_time = 10.0
    tui.sample_error = "warn"
    monkeypatch.setattr(monitor.time, "time", lambda: 12.0)
    assert tui._status_line() == "ready"
    monkeypatch.setattr(monitor.time, "time", lambda: 30.0)
    assert tui._status_line() == "warn"


def test_tui_draw_group_and_detail_views(monkeypatch):
    tui, screen = _make_initialized_tui(monkeypatch)
    tui.groups = [monitor.GroupRow(app="python", procs=2, rss_mb=10.0, mem_pct=5.0, cpu=8.0, swap_mb=1.0, io_read_mb=2.0, io_write_mb=1.0)]
    tui.sel = 0
    tui.scroll = 0
    tui.status_msg = "hello"
    tui.status_time = 0.0
    tui.sample_error = ""
    tui.alerts_cache = []
    tui.alerts_cache_time = 0.0
    tui.last_proc_rows = []
    monkeypatch.setattr(monitor.time, "time", lambda: 1.0)
    monkeypatch.setattr(monitor.os, "getloadavg", lambda: (1.0, 0.5, 0.2))
    monkeypatch.setattr(monitor.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(monitor.psutil, "virtual_memory", lambda: SimpleNamespace(used=2 * 1024**3, total=8 * 1024**3, available=4 * 1024**3, free=3 * 1024**3, buffers=1 * 1024**3, cached=1 * 1024**3, percent=30.0))
    monkeypatch.setattr(monitor.psutil, "swap_memory", lambda: SimpleNamespace(used=1 * 1024**3, total=2 * 1024**3, percent=25.0))
    monkeypatch.setattr(tui, "collect_alerts", lambda: ["critical alert"])

    tui.draw()

    rendered = "\n".join(call[2] for call in screen.calls)
    assert "python" in rendered
    assert "critical alert" in rendered

    screen.calls.clear()
    tui.view = "detail"
    tui.detail_app = "python"
    tui.tree_mode = False
    tui.detail_list = [_make_row(pid=1, app="python", cmdline="python app.py")]
    tui.draw()
    rendered = "\n".join(call[2] for call in screen.calls)
    assert "App: python" in rendered
    assert "python app.py" in rendered

    screen.calls.clear()
    tui.tree_mode = True
    tui.detail_tree = [(_make_row(pid=1, app="python", cmdline="python app.py"), 1, "└── ")]
    tui.draw()
    rendered = "\n".join(call[2] for call in screen.calls)
    assert "TREE" in rendered


def test_collect_alerts_paths(monkeypatch):
    tui = object.__new__(monitor.TUI)
    tui.alerts_cache = []
    tui.alerts_cache_time = 0.0
    tui.last_proc_rows = [
        _make_row(pid=1, app="python", cpu=250.0, mem_pct=20.0, swap_mb=2000.0, io_read_mb=200.0, io_write_mb=0.0, status="zombie", cmdline="python worker"),
    ]
    tui.groups = [monitor.GroupRow(app="python", procs=2, rss_mb=10.0, mem_pct=5.0, cpu=250.0, swap_mb=2500.0, io_read_mb=0.0, io_write_mb=0.0)]
    now = {"value": 100.0}
    monkeypatch.setattr(monitor.time, "time", lambda: now["value"])
    monkeypatch.setattr(monitor.time, "strftime", lambda fmt: "12:00:00")
    monkeypatch.setattr(monitor.psutil, "virtual_memory", lambda: SimpleNamespace(percent=96.0))
    monkeypatch.setattr(monitor.psutil, "swap_memory", lambda: SimpleNamespace(percent=85.0))
    monkeypatch.setattr(monitor.psutil, "disk_usage", lambda path: SimpleNamespace(percent=96.0))

    alerts = tui.collect_alerts()
    cached = tui.collect_alerts()

    assert alerts == cached
    assert any("MEMORY CRITICAL" in alert for alert in alerts)
    assert any("Group CPU critical" in alert or "CPU critical" in alert for alert in alerts)
    assert any("ZOMBIE PROCESS" in alert for alert in alerts)


def test_collect_alerts_error_path(monkeypatch):
    tui = object.__new__(monitor.TUI)
    tui.alerts_cache = []
    tui.alerts_cache_time = 0.0
    tui.last_proc_rows = []
    tui.groups = []
    monkeypatch.setattr(monitor.time, "time", lambda: 1.0)
    monkeypatch.setattr(monitor.time, "strftime", lambda fmt: "12:00:00")

    def raise_vm():
        raise RuntimeError("boom")

    monkeypatch.setattr(monitor.psutil, "virtual_memory", raise_vm)
    alerts = tui.collect_alerts()
    assert "Alert collection failed" in alerts[0]


def test_prompt_filter_paths(monkeypatch):
    tui, _screen = _make_initialized_tui(monkeypatch, text=b"python")
    monkeypatch.setattr(monitor.curses, "echo", lambda: None)
    monkeypatch.setattr(monitor.curses, "noecho", lambda: None)
    msgs = []
    tui.status = lambda msg: msgs.append(msg)
    tui.prompt_filter()
    assert tui.filter_text == "python"
    assert msgs[-1] == "Filter: python"

    tui.stdscr.text = b"["
    tui.prompt_filter()
    assert tui.filter_text == "invalid"
    assert msgs[-1] == "Filter invalid"

    tui.stdscr.text = b""
    tui.prompt_filter()
    assert tui.filter_text == ""
    assert msgs[-1] == "Filter cleared"


def test_tui_run_loop_branches(monkeypatch):
    keys = [
        monitor.curses.KEY_DOWN,
        monitor.curses.KEY_UP,
        monitor.curses.KEY_NPAGE,
        monitor.curses.KEY_PPAGE,
        monitor.curses.KEY_HOME,
        monitor.curses.KEY_END,
        10,
        ord("k"),
        ord("K"),
        ord("t"),
        ord("h"),
        ord("g"),
        ord("s"),
        ord("+"),
        ord("-"),
        ord("r"),
        ord("f"),
        ord("w"),
        ord("d"),
        ord("q"),
    ]
    tui, _screen = _make_initialized_tui(monkeypatch, keys=keys)
    calls = []
    tui.sample = lambda: None
    tui.draw = lambda: None
    tui.length = lambda: 12 if tui.view == "groups" else 4

    def toggle():
        tui.view = "detail" if tui.view == "groups" else "groups"
        if tui.view == "groups":
            tui.tree_mode = False

    tui.toggle_view = toggle
    tui.prompt_filter = lambda: calls.append("filter")
    tui.kill_selected = lambda sig: calls.append(("kill_selected", sig))
    tui.kill_group = lambda sig=monitor.signal.SIGTERM: calls.append(("kill_group", sig))
    tui.run_swap_clean = lambda: calls.append("swap")
    tui.run_drop_caches = lambda: calls.append("drop")

    tui.run()

    assert ("kill_selected", monitor.signal.SIGTERM) in calls
    assert ("kill_selected", monitor.signal.SIGKILL) in calls
    assert ("kill_group", monitor.signal.SIGTERM) in calls
    assert "filter" in calls
    assert "swap" in calls
    assert "drop" in calls


def test_tui_run_esc_arrow_translation(monkeypatch):
    keys = [27, 91, 65, ord("q")]
    tui, _screen = _make_initialized_tui(monkeypatch, keys=keys)
    tui.sample = lambda: None
    tui.draw = lambda: None
    tui.length = lambda: 3
    tui.sel = 1

    tui.run()

    assert tui.sel == 0


def test_main_check_sudo_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "parse_args", lambda: (SimpleNamespace(
        once=False,
        lite=False,
        check_sudo=True,
        init_subscripts=False,
        sort="mem",
        top=15,
        refresh=2.0,
    ), None))
    fake_module = SimpleNamespace(check_sudo=lambda: {"a": "ok"})
    monkeypatch.setitem(monitor.sys.modules, "ptop3.sudo_config", fake_module)

    with pytest.raises(SystemExit) as exc_info:
        monitor.main()

    assert exc_info.value.code == 0
    assert "a: ok" in capsys.readouterr().out


def test_main_init_subscripts(monkeypatch):
    called = []
    monkeypatch.setattr(monitor, "parse_args", lambda: (SimpleNamespace(
        once=False,
        lite=False,
        check_sudo=False,
        init_subscripts=True,
        sort="mem",
        top=15,
        refresh=2.0,
    ), None))
    fake_module = SimpleNamespace(init_subscripts=lambda: called.append(True))
    monkeypatch.setitem(monitor.sys.modules, "ptop3.sudo_config", fake_module)

    monitor.main()

    assert called == [True]


def test_main_once_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "parse_args", lambda: (SimpleNamespace(
        once=True,
        lite=True,
        check_sudo=False,
        init_subscripts=False,
        sort="cpu",
        top=5,
        refresh=2.0,
    ), "FILTER"))
    monkeypatch.setattr(monitor, "print_once", lambda fil, sort, top: calls.append((fil, sort, top)))

    monitor.main()

    assert monitor.LITE_MODE is True
    assert calls == [("FILTER", "cpu", 5)]


def test_main_tui_mode_uses_curses_wrapper(monkeypatch):
    wrapped = []
    monkeypatch.setattr(monitor, "parse_args", lambda: (SimpleNamespace(
        once=False,
        lite=False,
        check_sudo=False,
        init_subscripts=False,
        sort="rss",
        top=15,
        refresh=3.0,
    ), "FILTER"))
    monkeypatch.setattr(monitor, "TUI", lambda stdscr, fil, sort, refresh: SimpleNamespace(run=lambda: wrapped.append((stdscr, fil, sort, refresh))))
    monkeypatch.setattr(monitor.curses, "wrapper", lambda fn: fn("STDOUT"))

    monitor.main()

    assert wrapped == [("STDOUT", "FILTER", "rss", 3.0)]


def test_main_module_invokes_monitor_main(monkeypatch):
    called = []
    monkeypatch.setattr(monitor, "main", lambda: called.append(True))
    monkeypatch.delitem(monitor.sys.modules, "ptop3.__main__", raising=False)

    importlib.import_module("ptop3.__main__")

    assert called == [True]


def test_sample_processes_surfaces_unexpected_error(monkeypatch):
    class BrokenProc:
        info = {"pid": 1, "ppid": 0, "name": "broken"}

        def oneshot(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cmdline(self):
            return ["broken"]

        def memory_info(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(monitor.psutil, "virtual_memory", lambda: SimpleNamespace(total=1024 * 1024 * 1024))
    monkeypatch.setattr(monitor.psutil, "process_iter", lambda attrs=None: [BrokenProc()])

    sample = monitor.sample_processes(None)

    assert sample.rows == []
    assert sample.error.startswith("sampling warning:")
