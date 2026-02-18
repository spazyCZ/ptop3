# ptop3

[![PyPI version](https://img.shields.io/pypi/v/ptop3.svg)](https://pypi.org/project/ptop3/)
[![Python versions](https://img.shields.io/pypi/pyversions/ptop3.svg)](https://pypi.org/project/ptop3/)
[![CI](https://github.com/spazyCZ/ptop3/actions/workflows/ci.yml/badge.svg)](https://github.com/spazyCZ/ptop3/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An htop-like TUI process monitor that groups processes by application.

## Features

- Groups processes by application name with smart alias resolution
- Colored header with memory, swap, load-average badges
- Sort by memory, CPU, RSS, swap, I/O, network, or count
- Regex filter across app name, process name, and cmdline
- Process tree view within a selected application group
- Kill signals (SIGTERM / SIGKILL) for individual processes or entire groups
- `w` key: clean swap by cycling swapoff/swapon (passwordless sudo)
- `d` key: drop kernel caches (passwordless sudo)
- Alerts for high CPU, memory, swap, disk usage, and zombie processes
- Lite mode (`--lite`) for lower overhead on busy systems

## Installation

```bash
pip install ptop3
```

## Quick Start

```bash
ptop3                 # interactive TUI
python -m ptop3       # same via module
ptop3 --once          # print one-shot table and exit
ptop3 --filter python # filter to python processes
```

## Sudo Setup

The `w` (swap-clean) and `d` (drop-caches) keys require root. Configure passwordless sudo once:

```bash
sudo ptop3 --init-subscripts
```

Or manually:

```bash
sudo visudo -f /etc/sudoers.d/ptop3
# Add:
# YOUR_USER ALL=(root) NOPASSWD: /path/to/ptop3-drop-caches
# YOUR_USER ALL=(root) NOPASSWD: /path/to/ptop3-swap-clean
```

Check sudo status:

```bash
ptop3 --check-sudo
```

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--once` | off | Print one-shot table and exit |
| `-f/--filter REGEX` | — | Filter by app/name/cmdline |
| `-s/--sort KEY` | `mem` | Sort key: mem, cpu, rss, swap, io, net, count |
| `-n/--top N` | 15 | Rows to show in `--once` mode |
| `--refresh SECS` | 2.0 | Refresh interval |
| `--lite` | off | Lite mode: skip cmdline/IO for tiny procs |
| `--check-sudo` | — | Check sudo config for subscripts |
| `--init-subscripts` | — | Write /etc/sudoers.d/ptop3 |

## Key Bindings

| Key | Action |
|-----|--------|
| `↑/↓` or `j/k` | Move selection |
| `PgUp/PgDn` | Page up/down |
| `Home/End` | Jump to first/last |
| `Enter` or `l` | Expand group to detail view |
| `h` | Back to group view |
| `t` | Toggle process tree (detail view) |
| `s` | Cycle sort key |
| `f` | Enter filter regex |
| `r` | Reset filter |
| `+/-` | Increase/decrease refresh interval |
| `k/K` | Send SIGTERM/SIGKILL to selected |
| `g` | Kill whole group (SIGTERM) |
| `w` | Run swap-clean |
| `d` | Drop caches |
| `q` or `Ctrl-C` | Quit |

## Subscripts

The privileged subscripts can also be run directly:

```bash
ptop3-drop-caches --help
ptop3-drop-caches --level 1 --dry-run

ptop3-swap-clean --help
ptop3-swap-clean --safety-mb 256 --dry-run
```

## Development

```bash
git clone https://github.com/yourusername/ptop3
cd ptop3
pip install -e ".[dev]"
pytest
ruff check ptop3/
```
