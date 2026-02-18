#!/usr/bin/env python3
"""Clean swap by cycling swapoff/swapon with RAM safety checks."""
import argparse
import os
import subprocess
import sys


def read_meminfo(meminfo_path: str = "/proc/meminfo") -> dict[str, int]:
    """Parse /proc/meminfo and return a dict of field -> kB values."""
    result: dict[str, int] = {}
    with open(meminfo_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                try:
                    result[key] = int(parts[1])
                except ValueError:
                    pass
    return result


def read_swaps(swaps_path: str = "/proc/swaps") -> list[dict[str, object]]:
    """Parse /proc/swaps and return list of swap entry dicts.

    Each dict has keys: filename (str), type (str), size_kb (int),
    used_kb (int), priority (int).
    """
    entries = []
    with open(swaps_path) as f:
        lines = f.readlines()
    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 5:
            continue
        entries.append(
            {
                "filename": parts[0],
                "type": parts[1],
                "size_kb": int(parts[2]),
                "used_kb": int(parts[3]),
                "priority": int(parts[4]),
            }
        )
    return entries


def _run(cmd: list[str], dry_run: bool, verbose: bool) -> bool:
    """Run cmd unless dry_run. Return True on success."""
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return True
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"error running {cmd[0]}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def swap_clean(
    safety_mb: int = 512,
    target: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    meminfo_path: str = "/proc/meminfo",
    swaps_path: str = "/proc/swaps",
) -> int:
    """Clean swap. Returns 0 on success, non-zero on failure/skip.

    Return codes:
      0  clean completed
      1  general error
      2  not enough RAM / nothing cleaned
      3  not enough RAM for sole swap entry
    """

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    meminfo = read_meminfo(meminfo_path)
    mem_available_kb = meminfo.get("MemAvailable")
    if mem_available_kb is None:
        print("error: failed to read MemAvailable from meminfo", file=sys.stderr)
        return 1

    safety_kb = safety_mb * 1024

    if target:
        # Single target mode
        entries = read_swaps(swaps_path)
        target_resolved = os.path.realpath(target)
        entry = next(
            (e for e in entries if e["filename"] in (target, target_resolved)),
            None,
        )
        if entry is None:
            print(f"error: target swap not active: {target}", file=sys.stderr)
            return 1

        swap_used_kb = entry["used_kb"]
        swap_total_kb = entry["size_kb"]
        swap_free_kb = swap_total_kb - swap_used_kb

        log(f"MemAvailable: {mem_available_kb} kB")
        log(f"SwapTotal:    {swap_total_kb} kB")
        log(f"SwapFree:     {swap_free_kb} kB")
        log(f"SwapUsed:     {swap_used_kb} kB")
        log(f"Safety:       {safety_kb} kB")

        if swap_total_kb == 0:
            print("No swap configured. Nothing to do.")
            return 0
        if swap_used_kb == 0:
            print("Swap is not in use. Nothing to do.")
            return 0

        required_kb = swap_used_kb + safety_kb
        if mem_available_kb < required_kb:
            print("Not enough RAM to clean swap safely.", file=sys.stderr)
            print(f"MemAvailable: {mem_available_kb} kB", file=sys.stderr)
            print(f"SwapUsed:     {swap_used_kb} kB", file=sys.stderr)
            print(f"Safety:       {safety_kb} kB", file=sys.stderr)
            return 2

        log(f"Disabling swap: {target}")
        if not _run(["swapoff", target], dry_run, verbose):
            return 1
        log(f"Re-enabling swap: {target}")
        if not _run(["swapon", target], dry_run, verbose):
            return 1

        if verbose:
            _run(["swapon", "--show"], dry_run, verbose)
        print("Swap clean completed.")
        return 0

    # All-swap mode
    swap_total_kb = meminfo.get("SwapTotal", 0)
    swap_free_kb = meminfo.get("SwapFree", 0)
    if swap_total_kb == 0:
        print("No swap configured. Nothing to do.")
        return 0

    swap_used_kb = swap_total_kb - swap_free_kb
    if swap_used_kb == 0:
        print("Swap is not in use. Nothing to do.")
        return 0

    log(f"MemAvailable: {mem_available_kb} kB")
    log(f"SwapTotal:    {swap_total_kb} kB")
    log(f"SwapFree:     {swap_free_kb} kB")
    log(f"SwapUsed:     {swap_used_kb} kB")
    log(f"Safety:       {safety_kb} kB")

    required_kb = swap_used_kb + safety_kb
    if mem_available_kb >= required_kb:
        # Enough RAM for all swap at once
        log("Disabling swap...")
        if not _run(["swapoff", "-a"], dry_run, verbose):
            return 1
        log("Re-enabling swap...")
        if not _run(["swapon", "-a"], dry_run, verbose):
            return 1
        if verbose:
            _run(["swapon", "--show"], dry_run, verbose)
        print("Swap clean completed.")
        return 0

    # Not enough RAM — try file-by-file
    print("Not enough RAM to clean all swap at once. Trying file-by-file...", file=sys.stderr)
    entries = read_swaps(swaps_path)
    if not entries:
        print("No swap entries found in /proc/swaps.", file=sys.stderr)
        return 2

    # Sort by used_kb ascending so smallest first
    entries_sorted = sorted(entries, key=lambda e: e["used_kb"])
    total_entries = len(entries_sorted)
    did_any = False

    for entry in entries_sorted:
        swap_file = entry["filename"]
        # Re-read current state
        current_entries = read_swaps(swaps_path)
        current_entry = next((e for e in current_entries if e["filename"] == swap_file), None)
        if current_entry is None:
            log(f"Skipping {swap_file} (not active)")
            continue

        current_used_kb = current_entry["used_kb"]
        current_meminfo = read_meminfo(meminfo_path)
        current_mem_available_kb = current_meminfo.get("MemAvailable", 0)
        current_required_kb = current_used_kb + safety_kb

        log(f"MemAvailable: {current_mem_available_kb} kB")
        log(f"Target:       {swap_file}")
        log(f"SwapUsed:     {current_used_kb} kB")
        log(f"Safety:       {safety_kb} kB")
        log(f"Required:     {current_required_kb} kB")

        if current_mem_available_kb < current_required_kb:
            if current_mem_available_kb >= current_used_kb:
                print(f"Warning: safety buffer not met for {swap_file}; proceeding without safety.", file=sys.stderr)
            else:
                if total_entries == 1:
                    print(f"Not enough RAM to clean the only swap entry ({swap_file}).", file=sys.stderr)
                    print(f"Need {current_used_kb} kB, have {current_mem_available_kb} kB.", file=sys.stderr)
                    return 3
                print(
                    f"Skipping {swap_file}: not enough RAM"
                    f" (need {current_used_kb} kB, have {current_mem_available_kb} kB)",
                    file=sys.stderr,
                )
                continue

        log(f"Disabling swap: {swap_file}")
        if not _run(["swapoff", swap_file], dry_run, verbose):
            print(f"Failed to swapoff {swap_file}", file=sys.stderr)
            continue
        log(f"Re-enabling swap: {swap_file}")
        if not _run(["swapon", swap_file], dry_run, verbose):
            print(f"Failed to swapon {swap_file}", file=sys.stderr)
            continue
        did_any = True

    if did_any:
        if verbose:
            _run(["swapon", "--show"], dry_run, verbose)
        print("Swap clean completed (file-by-file).")
        return 0

    print("No swap entries could be cleaned safely.", file=sys.stderr)
    return 2


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Clean swap by cycling swapoff/swapon. Only proceeds if there is"
            " enough RAM to hold the currently used swap plus a safety buffer."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Passwordless sudo setup:
  sudo ptop3 --init-subscripts
""",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be done without making changes")
    ap.add_argument("--safety-mb", type=int, default=512, metavar="N", help="safety buffer in MB (default: 512)")
    ap.add_argument("--target", metavar="PATH", help="swap file/device to cycle (default: all)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("error: must run as root to use swapoff/swapon", file=sys.stderr)
        sys.exit(1)

    rc = swap_clean(
        safety_mb=args.safety_mb,
        target=args.target,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
