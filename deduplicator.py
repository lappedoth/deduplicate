
#!/usr/bin/env python3
"""deduplicator.py

A small utility to find duplicate files within a directory tree and replace
them with either hard links or symbolic links to a single canonical copy.
It can also undo the process ("reduplicate"), restoring real file copies
in place of links.

Usage
-----

# Preview duplicates, then deduplicate (default = hard links)
$ deduplicator.py deduplicate /path/to/data

# Explicitly choose link type
$ deduplicator.py deduplicate -l /path/to/data   # hard‑links
$ deduplicator.py deduplicate -s /path/to/data   # symlinks

# Restore files (remove links, re‑copy real data)
$ deduplicator.py reduplicate /path/to/data
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

################################################################################
# Helpers
################################################################################


def file_hash(path: Path, chunk_size: int = 8192) -> str:
    """Return SHA‑256 hex digest of *path*."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


################################################################################
# Deduplication
################################################################################


def deduplicate_preview(directory: Path) -> Tuple[int, List[Tuple[Path, Path]]]:
    """Traverse *directory* and collect duplicates.

    Returns
    -------
    total_saving : int
        Total number of bytes that *could* be saved.
    duplicates : list[tuple[Path, Path]]
        A list of (duplicate, original) pairs.
    """
    hash_to_path: Dict[str, Path] = {}
    total_saving = 0
    duplicates: List[Tuple[Path, Path]] = []

    for root, _, files in os.walk(directory):
        for name in files:
            full_path = Path(root) / name

            # Ignore symlinks
            if full_path.is_symlink():
                continue

            try:
                digest = file_hash(full_path)
                size = full_path.stat().st_size
            except (OSError, PermissionError) as exc:
                print(f"[skip] {full_path}: {exc}")
                continue

            if digest in hash_to_path:
                total_saving += size
                duplicates.append((full_path, hash_to_path[digest]))
            else:
                hash_to_path[digest] = full_path

    return total_saving, duplicates


def _link_or_symlink(duplicate: Path, original: Path, mode: str) -> None:
    """Replace *duplicate* with hard‑link or symlink to *original*."""
    # Remove the duplicate file first
    duplicate.unlink()

    if mode == "hard":
        os.link(original, duplicate)
    elif mode == "symlink":
        rel_target = os.path.relpath(original, start=duplicate.parent)
        duplicate.symlink_to(rel_target)
    else:  # pragma: no cover
        raise ValueError(f"Unknown mode {mode!r}")


def deduplicate_file(pair: Tuple[Path, Path], mode: str) -> str | None:
    """Worker helper for ThreadPoolExecutor."""
    duplicate, original = pair
    try:
        _link_or_symlink(duplicate, original, mode)
        link_desc = "hard‑linked" if mode == "hard" else "symlinked"
        return f"{link_desc} {duplicate} → {original}"
    except Exception as exc:
        return f"[error] {duplicate}: {exc}"


def apply_deduplication(duplicates: List[Tuple[Path, Path]], mode: str) -> None:
    """Parallel replacement of duplicates with links."""
    workers = max(1, (os.cpu_count() or 4) - 1)
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = [exe.submit(deduplicate_file, pair, mode) for pair in duplicates]
        for fut in as_completed(futures):
            msg = fut.result()
            if msg:
                print(msg)


################################################################################
# Reduplication (undo)
################################################################################


def reduplicate_file(path: Path) -> str | None:
    """Restore a real file where *path* is a symlink or inode‑shared file."""
    try:
        if path.is_symlink():
            target = path.resolve()
            # Replace symlink with a real copy
            path.unlink()
            shutil.copy2(target, path)
            return f"restored copy from symlink: {path}"

        # Hard‑link detection: link count > 1
        if path.stat().st_nlink > 1:
            tmp = path.with_suffix(path.suffix + ".redup_tmp")
            shutil.copy2(path, tmp)  # copy contents (creates new inode)
            tmp.replace(path)        # atomically replace
            return f"unlinked hard‑link: {path}"

    except Exception as exc:
        return f"[error] {path}: {exc}"

    return None


def reduplicate(directory: Path) -> None:
    workers = max(1, (os.cpu_count() or 4) - 1)
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = []
        for root, _, files in os.walk(directory):
            for name in files:
                futures.append(exe.submit(reduplicate_file, Path(root) / name))
        for fut in as_completed(futures):
            msg = fut.result()
            if msg:
                print(msg)


################################################################################
# CLI
################################################################################


def _print_usage() -> None:
    print(
        """Usage:
  deduplicator.py deduplicate [-l|-s] /path
      -l   create hard links (default)
      -s   create symbolic links

  deduplicator.py reduplicate /path
        """
    )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] not in {"deduplicate", "reduplicate"}:
        _print_usage()
        sys.exit(1)

    action = argv[0]

    if action == "deduplicate":
        # Defaults
        mode = "hard"   # hard‑link
        directory_arg_index = 1

        if len(argv) >= 2 and argv[1] in {"-l", "-s"}:
            flag = argv[1]
            mode = "hard" if flag == "-l" else "symlink"
            directory_arg_index = 2

        if len(argv) <= directory_arg_index:
            _print_usage()
            sys.exit(1)

        directory = Path(argv[directory_arg_index]).expanduser().resolve()

        if not directory.is_dir():
            print(f"[error] {directory} is not a directory")
            sys.exit(1)

        print(f"Scanning {directory} …\n")
        saving, dups = deduplicate_preview(directory)
        print(f"Found {len(dups)} duplicate files.")
        print(f"Potential space saving: {saving / (1024 ** 2):.2f} MB\n")

        # Show a few examples
        if dups:
            ans = input("Show a few duplicates? [y/N]: ").strip().lower()
            if ans == "y":
                n = min(5, len(dups))
                print("\nExamples:\n")
                for dup, orig in dups[:n]:
                    print(f"   duplicate: {dup}\n   original : {orig}\n")

        confirm = input(f"Proceed with deduplication using {mode} links? [y/N]: ").strip().lower()
        if confirm == "y":
            apply_deduplication(dups, mode)
            print("\nDeduplication complete.")
        else:
            print("Aborted.")

    elif action == "reduplicate":
        if len(argv) != 2:
            _print_usage()
            sys.exit(1)

        directory = Path(argv[1]).expanduser().resolve()
        if not directory.is_dir():
            print(f"[error] {directory} is not a directory")
            sys.exit(1)

        reduplicate(directory)
        print("\nReduplication complete.")


if __name__ == "__main__":
    main()
