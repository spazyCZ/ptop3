# Changelog

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
