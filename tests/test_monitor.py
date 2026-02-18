"""Tests for ptop3.monitor data layer functions."""
import re
import pytest
from ptop3.monitor import (
    ProcRow,
    GroupRow,
    normalize_app_name,
    aggregate,
    build_process_tree,
    SORT_KEYS,
)


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
        io_read_mb=0.0, io_write_mb=0.0, net_sent_mb=0.0, net_recv_mb=0.0,
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
