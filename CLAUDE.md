# ptop3 — Claude Code Context

`ptop3` is a Linux TUI process monitor (htop-like) that groups processes by application.
It uses `curses` for the UI, `psutil` for process data, and ships two privileged scripts
(`ptop3-drop-caches`, `ptop3-swap-clean`) that run via passwordless sudo.

## Key Files

| File | Purpose |
|------|---------|
| `ptop3/monitor.py` | Core TUI + data aggregation |
| `ptop3/sudo_config.py` | Passwordless sudo setup |
| `ptop3/scripts/drop_caches.py` | Kernel cache clearing |
| `ptop3/scripts/swap_clean.py` | Swap cleanup |
| `tests/` | Pytest suite — all 4 Python versions |
| `CHANGELOG.md` | User-facing change log |
| `.github/instructions/code-review.instructions.md` | Full style & quality guide |

## Code Standards

- Python 3.10+: use `X | None`, `list[str]`, `match`, builtin generics — no `typing.Dict/List/Optional`
- Ruff: line-length 100, rules E/F/I/UP — run `ruff check ptop3/` before committing
- No bare `except:`, no `print()` in library code, no `shell=True` in subprocess calls
- File paths via `pathlib.Path` — never string concatenation

## Testing

- Run: `pytest --cov=ptop3 --cov-report=term-missing`
- Every new function must have a test; mock `/proc/*` files with `tmp_path`; mock `curses`
- Tests must not require root — patch `os.geteuid` for privileged paths

## CHANGELOG

Every PR with user-facing changes must add an entry under `## [Unreleased]`.
Sections: `Added`, `Changed`, `Fixed`, `Security`, `Deprecated`, `Removed`.
Do **not** bump versions manually — that's handled by `bump-my-version` in the release workflow.

## Commits

Conventional format: `feat:`, `fix:`, `docs:`, `ci:`, `chore:`, `refactor:`, `test:`
CI must be green on all 4 Python versions (3.10–3.13) before merging.
