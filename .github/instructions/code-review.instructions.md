---
applyTo: "**"
---

# Code Review Instructions for ptop3

## Project Overview

`ptop3` is a Linux TUI process monitor (htop-like) that groups processes by application.
It uses `curses` for the UI, `psutil` for process data, and ships two privileged subscripts
(`ptop3-drop-caches`, `ptop3-swap-clean`) that run via passwordless sudo.

---

## General Standards

- Python 3.10+ required — use modern syntax (`X | None`, `list[str]`, `match`, etc.)
- All code must pass `ruff check` with the project config (line-length 100, rules E/F/I/UP)
- No `typing.Dict`, `typing.List`, `typing.Optional` — use built-in generics
- No bare `except:` — always catch specific exceptions
- No `print()` in library code (`monitor.py`, `sudo_config.py`) — use `sys.stderr` or curses status
- Subprocess calls must use explicit argument lists — never `shell=True`
- File paths must never be constructed by string concatenation — use `pathlib.Path`

---

## Security

- Any code writing to `/proc/sys/vm/drop_caches` or calling `swapoff`/`swapon` must:
  - Check `os.geteuid() == 0` before proceeding
  - Exit with a clear error message if not root
- Never pass unsanitized user input to `subprocess.run()`
- Sudoers file writes must always validate with `visudo -c -f` before moving into place
- GitHub Actions workflows must have explicit `permissions:` blocks (least privilege)
- No secrets or tokens hardcoded anywhere

---

## Testing

- Every new function in `scripts/` or `sudo_config.py` must have a corresponding test
- Tests must not require root — use `patch("os.geteuid", return_value=0)` for root paths
- `/proc/meminfo` and `/proc/swaps` must be mocked via `tmp_path` fixtures — never read real ones
- `curses` functions must be mocked — do not test TUI rendering directly
- All tests must pass on Python 3.10, 3.11, 3.12, and 3.13

---

## CHANGELOG

- **Every PR that changes user-facing behavior must update `CHANGELOG.md`**
- Add an entry under the appropriate unreleased section:
  - `### Added` — new features or CLI flags
  - `### Changed` — behavior changes to existing features
  - `### Fixed` — bug fixes
  - `### Security` — security-related changes
  - `### Deprecated` / `### Removed` — as appropriate
- Format: `## [Unreleased]` at the top, moved to a versioned section on release
- Do **not** bump `version =` in `pyproject.toml` or `__version__` in `__init__.py` — version bumps
  are handled exclusively by `bump-my-version` in the release workflow

---

## Commits & PRs

- Use conventional commit format: `feat:`, `fix:`, `docs:`, `ci:`, `chore:`, `refactor:`, `test:`
- PR title must follow the same convention
- Each PR should do one thing — avoid mixing features, fixes, and refactors
- CI must be green (all 4 Python versions) before merging

---

## Module-Specific Rules

### `ptop3/monitor.py`
- Data functions (`get_proc_rows`, `aggregate`, `build_process_tree`, `normalize_app_name`)
  must remain importable at the top level via `ptop3.__init__`
- TUI class (`TUI`) must not call `sys.exit()` — raise exceptions or return
- Script paths must be resolved via `shutil.which()` first, falling back to `Path(__file__).parent`

### `ptop3/scripts/drop_caches.py` and `swap_clean.py`
- All logic must be testable without root via dry-run and mocked paths
- `main()` must be the only entry point — no module-level side effects
- Return codes must be documented in the function docstring

### `ptop3/sudo_config.py`
- `init_subscripts()` must always call `visudo -c -f` before writing the sudoers file
- Must print clear instructions when not running as root — never silently fail

### GitHub Actions workflows
- New jobs must include explicit `permissions:` (minimum `contents: read`)
- Publish jobs must use `skip-existing: true` for TestPyPI
- Pre-release tags (`v*.*.*-*`) must not publish to PyPI
