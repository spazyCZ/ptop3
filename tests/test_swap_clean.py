"""Tests for ptop3.scripts.swap_clean."""
import pytest
from unittest.mock import patch, MagicMock, call
from ptop3.scripts.swap_clean import read_meminfo, read_swaps, swap_clean


# ---------------------------------------------------------------------------
# read_meminfo
# ---------------------------------------------------------------------------

def test_read_meminfo(tmp_meminfo):
    info = read_meminfo(tmp_meminfo)
    assert info["MemAvailable"] == 8192000
    assert info["SwapTotal"] == 8388608
    assert info["SwapFree"] == 6291456
    assert info["MemTotal"] == 32768000


def test_read_meminfo_missing_file():
    with pytest.raises(OSError):
        read_meminfo("/nonexistent/path/meminfo")


# ---------------------------------------------------------------------------
# read_swaps
# ---------------------------------------------------------------------------

def test_read_swaps(tmp_swaps):
    entries = read_swaps(tmp_swaps)
    assert len(entries) == 1
    e = entries[0]
    assert e["filename"] == "/dev/sda2"
    assert e["size_kb"] == 8388608
    assert e["used_kb"] == 2097152
    assert e["priority"] == -2


def test_read_swaps_no_entries(tmp_path):
    p = tmp_path / "swaps"
    p.write_text("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n")
    entries = read_swaps(str(p))
    assert entries == []


def test_read_swaps_multiple_entries(tmp_path):
    content = (
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/dev/sda2\t\t\t\tpartition\t8388608\t\t1048576\t\t-2\n"
        "/swapfile\t\t\t\tfile\t\t4194304\t\t524288\t\t-3\n"
    )
    p = tmp_path / "swaps"
    p.write_text(content)
    entries = read_swaps(str(p))
    assert len(entries) == 2
    assert entries[0]["filename"] == "/dev/sda2"
    assert entries[1]["filename"] == "/swapfile"


# ---------------------------------------------------------------------------
# swap_clean — no swap configured
# ---------------------------------------------------------------------------

def test_swap_clean_no_swap(tmp_path, capsys):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemAvailable: 8192000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    rc = swap_clean(meminfo_path=str(meminfo))
    assert rc == 0
    out = capsys.readouterr().out
    assert "No swap configured" in out


# ---------------------------------------------------------------------------
# swap_clean — swap not in use
# ---------------------------------------------------------------------------

def test_swap_clean_swap_not_in_use(tmp_path, capsys):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemAvailable: 8192000 kB\nSwapTotal: 8388608 kB\nSwapFree: 8388608 kB\n"
    )
    rc = swap_clean(meminfo_path=str(meminfo))
    assert rc == 0
    out = capsys.readouterr().out
    assert "not in use" in out


# ---------------------------------------------------------------------------
# swap_clean — enough RAM, clean all at once
# ---------------------------------------------------------------------------

def test_swap_clean_all_at_once(tmp_meminfo, tmp_swaps, capsys):
    # MemAvailable=8192000 kB, SwapUsed = 8388608-6291456 = 2097152 kB
    # safety_mb=512 -> safety_kb=524288; required=2621440; 8192000 >= 2621440 -> ok
    with patch("ptop3.scripts.swap_clean.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        rc = swap_clean(
            safety_mb=512,
            meminfo_path=tmp_meminfo,
            swaps_path=tmp_swaps,
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Swap clean completed" in out
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("swapoff" in c and "-a" in c for c in calls)
    assert any("swapon" in c and "-a" in c for c in calls)


# ---------------------------------------------------------------------------
# swap_clean — not enough RAM (safety check fails), single target
# ---------------------------------------------------------------------------

def test_swap_clean_insufficient_ram_single_target(tmp_path, tmp_swaps):
    meminfo = tmp_path / "meminfo"
    # Very little available RAM
    meminfo.write_text(
        "MemAvailable: 100 kB\nSwapTotal: 8388608 kB\nSwapFree: 6291456 kB\n"
    )
    rc = swap_clean(
        safety_mb=512,
        target="/dev/sda2",
        meminfo_path=str(meminfo),
        swaps_path=tmp_swaps,
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# swap_clean — target not in swaps
# ---------------------------------------------------------------------------

def test_swap_clean_target_not_active(tmp_meminfo, tmp_swaps):
    rc = swap_clean(
        target="/dev/nonexistent",
        meminfo_path=tmp_meminfo,
        swaps_path=tmp_swaps,
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# swap_clean — dry run
# ---------------------------------------------------------------------------

def test_swap_clean_dry_run(tmp_meminfo, tmp_swaps, capsys):
    rc = swap_clean(
        dry_run=True,
        meminfo_path=tmp_meminfo,
        swaps_path=tmp_swaps,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "swapoff" in out
    assert "swapon" in out


# ---------------------------------------------------------------------------
# swap_clean — swapoff/swapon called with correct args
# ---------------------------------------------------------------------------

def test_swap_clean_calls_swapoff_swapon(tmp_meminfo, tmp_swaps):
    with patch("ptop3.scripts.swap_clean.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        rc = swap_clean(
            safety_mb=512,
            meminfo_path=tmp_meminfo,
            swaps_path=tmp_swaps,
        )
    assert rc == 0
    called_cmds = [c.args[0] for c in mock_run.call_args_list]
    assert ["swapoff", "-a"] in called_cmds
    assert ["swapon", "-a"] in called_cmds


# ---------------------------------------------------------------------------
# root check in main()
# ---------------------------------------------------------------------------

def test_main_root_check_exits_when_not_root():
    from ptop3.scripts.swap_clean import main
    with patch("os.geteuid", return_value=1000):
        with patch("sys.argv", ["ptop3-swap-clean"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1
