"""Tests for ptop3.sudo_config."""
import os
import pytest
from unittest.mock import patch, MagicMock
from ptop3.sudo_config import check_sudo, init_subscripts, _build_sudoers_content


# ---------------------------------------------------------------------------
# _build_sudoers_content
# ---------------------------------------------------------------------------

def test_build_sudoers_content_contains_username():
    content = _build_sudoers_content("alice", {"ptop3-drop-caches": "/usr/bin/ptop3-drop-caches"})
    assert "alice" in content
    assert "/usr/bin/ptop3-drop-caches" in content
    assert "NOPASSWD" in content


def test_build_sudoers_content_all_scripts():
    paths = {
        "ptop3-drop-caches": "/usr/bin/ptop3-drop-caches",
        "ptop3-swap-clean": "/usr/bin/ptop3-swap-clean",
    }
    content = _build_sudoers_content("bob", paths)
    assert "ptop3-drop-caches" in content
    assert "ptop3-swap-clean" in content


# ---------------------------------------------------------------------------
# check_sudo — scripts not found
# ---------------------------------------------------------------------------

def test_check_sudo_scripts_missing():
    with patch("shutil.which", return_value=None):
        result = check_sudo()
    assert result["ptop3-drop-caches"] == "missing"
    assert result["ptop3-swap-clean"] == "missing"


# ---------------------------------------------------------------------------
# check_sudo — sudo -n succeeds
# ---------------------------------------------------------------------------

def test_check_sudo_ok():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stderr = ""

    with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
        with patch("subprocess.run", return_value=mock_proc):
            result = check_sudo()

    assert result["ptop3-drop-caches"] == "ok"
    assert result["ptop3-swap-clean"] == "ok"


# ---------------------------------------------------------------------------
# check_sudo — sudo -n needs password
# ---------------------------------------------------------------------------

def test_check_sudo_needs_password():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = "sudo: a password is required"

    with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
        with patch("subprocess.run", return_value=mock_proc):
            result = check_sudo()

    assert result["ptop3-drop-caches"] == "needs_password"


# ---------------------------------------------------------------------------
# init_subscripts — not root: prints howto
# ---------------------------------------------------------------------------

def test_init_subscripts_not_root_prints_howto(capsys):
    with patch("os.geteuid", return_value=1000):
        with patch("os.getuid", return_value=1000):
            with patch("pwd.getpwuid") as mock_pw:
                mock_pw.return_value.pw_name = "alice"
                with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
                    init_subscripts()

    out = capsys.readouterr().out
    assert "Not running as root" in out
    assert "sudo ptop3 --init-subscripts" in out
    assert "NOPASSWD" in out


def test_init_subscripts_not_root_shows_file_content(capsys):
    with patch("os.geteuid", return_value=1000):
        with patch("os.getuid", return_value=1000):
            with patch("pwd.getpwuid") as mock_pw:
                mock_pw.return_value.pw_name = "testuser"
                with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
                    init_subscripts()

    out = capsys.readouterr().out
    assert "testuser" in out


# ---------------------------------------------------------------------------
# init_subscripts — as root: writes file
# ---------------------------------------------------------------------------

def test_init_subscripts_as_root_writes_file(tmp_path):
    sudoers_file = tmp_path / "ptop3"

    mock_visudo = MagicMock()
    mock_visudo.returncode = 0

    with patch("os.geteuid", return_value=0):
        with patch("os.getuid", return_value=0):
            with patch("pwd.getpwuid") as mock_pw:
                mock_pw.return_value.pw_name = "root"
                with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
                    with patch("subprocess.run", return_value=mock_visudo):
                        with patch("ptop3.sudo_config.SUDOERS_FILE", str(sudoers_file)):
                            with patch("shutil.move") as mock_move:
                                with patch("os.chmod"):
                                    init_subscripts()
                                    # move was called (tmp -> sudoers)
                                    assert mock_move.called


def test_init_subscripts_as_root_validates_with_visudo(tmp_path):
    """visudo -c -f must be called when running as root."""
    mock_visudo = MagicMock()
    mock_visudo.returncode = 0

    with patch("os.geteuid", return_value=0):
        with patch("os.getuid", return_value=0):
            with patch("pwd.getpwuid") as mock_pw:
                mock_pw.return_value.pw_name = "root"
                with patch("shutil.which", return_value="/usr/bin/ptop3-drop-caches"):
                    with patch("subprocess.run", return_value=mock_visudo) as mock_run:
                        with patch("ptop3.sudo_config.SUDOERS_FILE", str(tmp_path / "ptop3")):
                            with patch("shutil.move"):
                                with patch("os.chmod"):
                                    init_subscripts()

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("visudo" in cmd for cmd in calls)
