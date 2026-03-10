# Changelog

## [Unreleased]

### Changed
- Branching strategy: `feature → test → main` (previously direct to `main`)
- TestPyPI publish now triggers on merge to `test` branch (was `main`)
- PyPI production publish is now manual (`workflow_dispatch`) instead of automatic on tag
- GitHub Release creation on tag push remains automatic
- `ptop3` no longer advertises or supports the non-functional `net` sort mode in the TUI/CLI

### Added
- `CLAUDE.md` with project context for Claude Code agents
- `codecov.yml` to suppress spurious uploader warnings
- Claude Quality Gate workflow: three parallel agents for test coverage, changelog, and code review
- Branch protection on `test` (CI required) and `main` (CI + PR + linear history required)
- `publish-pypi.yml` workflow for manual production releases

## [0.1.3] - 2026-02-18

### Fixed
- Python versions badge in README now uses static badge (was showing "not found" on PyPI)

## [0.1.2] - 2026-02-18

### Added
- Screenshots in README (group view, detail view, tree view)
- Python 3.13 to CI test matrix

### Fixed
- CI badge URL corrected to spazyCZ/ptop3
- Image URLs in README changed to absolute raw.githubusercontent.com paths for PyPI rendering
- TestPyPI publish now skips existing versions to avoid failures on non-version commits

### Security
- Added explicit least-privilege `permissions` blocks to all workflows (fixes 5 CodeQL alerts)

## [0.1.1] - 2026-02-18

### Added
- API token authentication for TestPyPI and PyPI publish workflows
- `test` branch added to CI triggers
- Codecov test results upload in CI

## [0.1.0] - 2026-02-18

### Added
- Initial release as pip-installable package
- `ptop3` TUI entry point (grouped process monitor)
- `ptop3-drop-caches` entry point (Python rewrite of drop-caches.sh)
- `ptop3-swap-clean` entry point (Python rewrite of swap-clean.sh)
- `ptop3 --check-sudo` to verify NOPASSWD sudo for subscripts
- `ptop3 --init-subscripts` to write /etc/sudoers.d/ptop3
- Test suite with pytest covering monitor, drop_caches, swap_clean, sudo_config
- CI workflow for Python 3.10, 3.11, 3.12 on ubuntu-latest
