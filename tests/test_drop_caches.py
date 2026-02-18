"""Tests for ptop3.scripts.drop_caches."""
import pytest
from unittest.mock import patch, call, mock_open, MagicMock
from ptop3.scripts.drop_caches import read_mem_available, drop_caches


# ---------------------------------------------------------------------------
# read_mem_available
# ---------------------------------------------------------------------------

def test_read_mem_available(tmp_meminfo):
    result = read_mem_available(tmp_meminfo)
    assert result == 8192000


def test_read_mem_available_missing_field(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1000 kB\n")
    with pytest.raises(ValueError, match="MemAvailable not found"):
        read_mem_available(str(p))


# ---------------------------------------------------------------------------
# drop_caches
# ---------------------------------------------------------------------------

def test_drop_caches_dry_run(tmp_meminfo, capsys):
    freed = drop_caches(level=3, dry_run=True, meminfo_path=tmp_meminfo)
    out = capsys.readouterr().out
    assert "[dry-run] sync" in out
    assert "[dry-run] echo 3" in out
    # freed_mb = (mem_after - mem_before) / 1024 — both reads same file so 0
    assert freed == 0


def test_drop_caches_dry_run_level_1(tmp_meminfo, capsys):
    drop_caches(level=1, dry_run=True, meminfo_path=tmp_meminfo)
    out = capsys.readouterr().out
    assert "[dry-run] echo 1" in out


def test_drop_caches_writes_correct_level(tmp_path, tmp_meminfo):
    drop_caches_file = tmp_path / "drop_caches"
    drop_caches_file.write_text("")

    with patch("ptop3.scripts.drop_caches.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        freed = drop_caches(
            level=2,
            dry_run=False,
            meminfo_path=tmp_meminfo,
            drop_caches_path=str(drop_caches_file),
        )

    assert drop_caches_file.read_text() == "2"
    mock_run.assert_called_once_with(["sync"], check=True)


def test_drop_caches_verbose(tmp_meminfo, capsys):
    with patch("ptop3.scripts.drop_caches.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        # Need a real file for the write; use dry_run to skip actual write
        drop_caches(level=3, verbose=True, dry_run=True, meminfo_path=tmp_meminfo)
    out = capsys.readouterr().out
    assert "MemAvailable before" in out
    assert "Syncing" in out
    assert "Dropping caches" in out


# ---------------------------------------------------------------------------
# root check in main()
# ---------------------------------------------------------------------------

def test_main_root_check_exits_when_not_root():
    """main() should sys.exit(1) when not root."""
    from ptop3.scripts.drop_caches import main
    with patch("os.geteuid", return_value=1000):
        with patch("sys.argv", ["ptop3-drop-caches"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1


def test_main_passes_when_root(tmp_path, tmp_meminfo):
    from ptop3.scripts.drop_caches import main
    drop_caches_file = tmp_path / "drop_caches"
    drop_caches_file.write_text("")

    with patch("os.geteuid", return_value=0):
        with patch("sys.argv", ["ptop3-drop-caches", "--dry-run"]):
            with patch("ptop3.scripts.drop_caches.subprocess.run"):
                # Patch read_mem_available to use our tmp file
                with patch(
                    "ptop3.scripts.drop_caches.drop_caches",
                    return_value=0,
                ) as mock_dc:
                    main()
                    mock_dc.assert_called_once()
