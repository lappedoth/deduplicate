#!/usr/bin/env python3

import os
import hashlib
import sys
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

def file_hash(path, chunk_size=8192):
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

def deduplicate_preview(directory):
    hash_to_path = {}
    total_saving = 0
    duplicates = []

    for root, _, files in os.walk(directory):
        for name in files:
            full_path = os.path.join(root, name)
            if os.path.islink(full_path):
                continue
            try:
                h = file_hash(full_path)
                size = os.path.getsize(full_path)
            except Exception as e:
                print(f"Skipping {full_path}: {e}")
                continue
            if h in hash_to_path:
                total_saving += size
                duplicates.append((full_path, hash_to_path[h]))
            else:
                hash_to_path[h] = full_path
    return total_saving, duplicates

def create_alias(target, alias_path):
    try:
        subprocess.run([
            "osascript", "-e",
            f'tell application "Finder" to make alias file to (POSIX file "{os.path.abspath(target)}") at (POSIX file "{os.path.dirname(os.path.abspath(alias_path))}")'
        ], check=True)
        os.rename(os.path.join(os.path.dirname(alias_path), os.path.basename(target) + " alias"), alias_path)
    except subprocess.CalledProcessError:
        print(f"Failed to create alias for {target} at {alias_path}")

def deduplicate_file(pair, mode):
    duplicate, original = pair
    try:
        os.remove(duplicate)
        if mode == "hard":
            os.link(original, duplicate)
            return f"Hard-linked {duplicate} → {original}"
        elif mode == "symlink":
            rel = os.path.relpath(original, start=os.path.dirname(duplicate))
            os.symlink(rel, duplicate)
            return f"Symlinked {duplicate} → {rel}"
        elif mode == "alias":
            create_alias(original, duplicate)
            return f"Alias created for {duplicate} → {original}"
    except Exception as e:
        return f"Failed to deduplicate {duplicate}: {e}"

def apply_deduplication(duplicates, mode):
    max_workers = max(1, (os.cpu_count() or 4) - 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(deduplicate_file, pair, mode) for pair in duplicates]
        for future in as_completed(futures):
            result = future.result()
            if result:
                print(result)

def is_mac_alias(path):
    try:
        result = subprocess.run([
            'osascript', '-e',
            f'tell application "Finder" to get original item of alias file (POSIX file "{os.path.abspath(path)}")'
        ], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False

def resolve_mac_alias(path):
    result = subprocess.run([
        'osascript', '-e',
        f'tell application "Finder" to POSIX path of (original item of alias file (POSIX file "{os.path.abspath(path)}"))'
    ], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    else:
        return None

def reduplicate_file(full_path):
    try:
        if os.path.islink(full_path):
            target = os.readlink(full_path)
            abs_target = os.path.abspath(os.path.join(os.path.dirname(full_path), target))
            os.remove(full_path)
            shutil.copy2(abs_target, full_path)
            return f"Restored real file from symlink: {full_path}"
        elif os.stat(full_path).st_nlink > 1:
            tmp_path = full_path + ".redup_tmp"
            shutil.copy2(full_path, tmp_path)
            os.replace(tmp_path, full_path)
            return f"Unlinked hard link: {full_path}"
        elif is_mac_alias(full_path):
            resolved = resolve_mac_alias(full_path)
            if resolved and os.path.exists(resolved):
                os.remove(full_path)
                shutil.copy2(resolved, full_path)
                return f"Restored real file from alias: {full_path}"
    except Exception as e:
        return f"Failed to reduplicate {full_path}: {e}"
    return None

def reduplicate(directory):
    max_workers = max(1, (os.cpu_count() or 4) - 1)
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for root, _, files in os.walk(directory):
            for name in files:
                full_path = os.path.join(root, name)
                tasks.append(executor.submit(reduplicate_file, full_path))
        for future in as_completed(tasks):
            result = future.result()
            if result:
                print(result)

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("deduplicate", "reduplicate"):
        print("Usage:\n  deduplicator.py deduplicate [-l|-s|-a] /path\n  deduplicator.py reduplicate /path")
        sys.exit(1)

    action = sys.argv[1]

    if action == "deduplicate":
        mode = "hard"
        if len(sys.argv) == 4 and sys.argv[2] in ("-l", "-s", "-a"):
            mode_flag = sys.argv[2]
            mode = {"-l": "hard", "-s": "symlink", "-a": "alias"}[mode_flag]
            directory = sys.argv[3]
        elif len(sys.argv) == 3:
            directory = sys.argv[2]
        else:
            print("Usage: deduplicator.py deduplicate [-l|-s|-a] /path")
            sys.exit(1)

        total_saving, duplicates = deduplicate_preview(directory)
        print(f"\nIdentified {len(duplicates)} duplicate files.")
        print(f"Estimated disk space saving: {total_saving / (1024 * 1024):.2f} MB\n")

        show_examples = input("Do you want to see examples of duplicates? [y/N]: ").strip().lower()
        if show_examples == 'y':
            try:
                num = int(input(f"How many examples? (1–{len(duplicates)}): ").strip())
            except ValueError:
                num = 5
            print("\nExample duplicates:\n")
            for dup, orig in duplicates[:max(0, min(num, len(duplicates)))]:
                print(f"Original:  {orig}")
                print(f"Duplicate: {dup}\n")

        confirm = input(f"Proceed with deduplication using {mode} links? [y/N]: ").strip().lower()
        if confirm == 'y':
            apply_deduplication(duplicates, mode)
            print("Deduplication complete.")
        else:
            print("Operation cancelled.")

    elif action == "reduplicate":
        if len(sys.argv) != 3:
            print("Usage: deduplicator.py reduplicate /path")
            sys.exit(1)
        directory = sys.argv[2]
        reduplicate(directory)
        print("Reduplication complete.")
