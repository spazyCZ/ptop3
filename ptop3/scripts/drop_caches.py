#!/usr/bin/env python3
"""Drop kernel filesystem caches by writing to /proc/sys/vm/drop_caches."""
import argparse
import os
import subprocess
import sys


def read_mem_available(meminfo_path: str = "/proc/meminfo") -> int:
    """Return MemAvailable in kB from /proc/meminfo."""
    with open(meminfo_path) as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    raise ValueError("MemAvailable not found in meminfo")


def drop_caches(
    level: int = 3,
    verbose: bool = False,
    dry_run: bool = False,
    meminfo_path: str = "/proc/meminfo",
    drop_caches_path: str = "/proc/sys/vm/drop_caches",
) -> int:
    """Drop caches at the given level. Returns freed MB."""

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    mem_before = read_mem_available(meminfo_path)
    log(f"MemAvailable before: {mem_before} kB")

    log("Syncing filesystems...")
    if dry_run:
        print("[dry-run] sync")
    else:
        subprocess.run(["sync"], check=True)

    log(f"Dropping caches (level {level})...")
    if dry_run:
        print(f"[dry-run] echo {level} > {drop_caches_path}")
    else:
        with open(drop_caches_path, "w") as f:
            f.write(str(level))

    mem_after = read_mem_available(meminfo_path)
    freed_kb = mem_after - mem_before
    freed_mb = freed_kb // 1024

    log(f"MemAvailable after:  {mem_after} kB")
    log(f"Freed: {freed_kb} kB ({freed_mb} MB)")

    return freed_mb


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Drop kernel filesystem caches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Levels:
  1  Free page cache only
  2  Free dentries and inodes
  3  Free page cache, dentries, and inodes (default)

Passwordless sudo setup:
  sudo ptop3 --init-subscripts
""",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be done without making changes")
    ap.add_argument("--level", type=int, default=3, choices=[1, 2, 3], help="cache drop level (default: 3)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("error: must run as root", file=sys.stderr)
        sys.exit(1)

    try:
        freed_mb = drop_caches(
            level=args.level,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )
        print(f"Dropped caches (level {args.level}), freed {freed_mb} MB.")
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
