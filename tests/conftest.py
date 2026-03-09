"""Shared fixtures for ptop3 tests."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for module_name in list(sys.modules):
    if module_name == "ptop3" or module_name.startswith("ptop3."):
        sys.modules.pop(module_name, None)

MEMINFO_TEMPLATE = """\
MemTotal:       32768000 kB
MemFree:         4096000 kB
MemAvailable:    8192000 kB
Buffers:          512000 kB
Cached:          6144000 kB
SwapCached:            0 kB
SwapTotal:       8388608 kB
SwapFree:        6291456 kB
"""

SWAPS_TEMPLATE = """\
Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority
/dev/sda2                               partition\t8388608\t\t2097152\t\t-2
"""


@pytest.fixture()
def meminfo_content():
    return MEMINFO_TEMPLATE


@pytest.fixture()
def swaps_content():
    return SWAPS_TEMPLATE


@pytest.fixture()
def tmp_meminfo(tmp_path, meminfo_content):
    p = tmp_path / "meminfo"
    p.write_text(meminfo_content)
    return str(p)


@pytest.fixture()
def tmp_swaps(tmp_path, swaps_content):
    p = tmp_path / "swaps"
    p.write_text(swaps_content)
    return str(p)
