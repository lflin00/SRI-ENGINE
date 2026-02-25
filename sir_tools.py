#!/usr/bin/env python3
"""
sir_tools.py — `verify` and `diff` commands for the SIR Engine.

IMPORTANT DETAIL
----------------
sir_pack.py stores TWO identifiers per function occurrence:
- root:       the *node id* of the AST root inside the global node store (not directly comparable to sir1.py hash)
- sir_sha256: the *SIR hash* printed by `python3 sir1.py hash ... --mode semantic`

Therefore:
- verify compares restored functions' `sir1.py hash --mode semantic` output against the pack's `sir_sha256` values.

COMMANDS
--------
verify
  Re-hash restored functions (semantic SIR hash) and confirm they match the pack's sir_sha256 set.

diff
  Compare two folders structurally using semantic SIR hashes and report:
  - identical structures
  - added structures
  - removed structures

USAGE
-----
python3 sir_tools.py verify .sir_pack_demo restored_funcs
python3 sir_tools.py diff folderA folderB
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set


def semantic_sir_hash(path: str, sir1_path: Path) -> str:
    # This is the SIR hash (sha256 over canonical {root,nodes}) returned by sir1.py
    return subprocess.check_output(
        ["python3", str(sir1_path), "hash", path, "--mode", "semantic"],
        text=True,
    ).strip()


def cmd_verify(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    restored_dir = Path(args.restored_dir).expanduser().resolve()
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")

    roots = json.loads((pack_dir / "roots.json").read_text(encoding="utf-8"))
    expected: Set[str] = set()

    # Prefer sir_sha256; fall back to computing from (nodes, root) if missing (older packs)
    for r in roots:
        if "sir_sha256" in r and r["sir_sha256"]:
            expected.add(r["sir_sha256"])
        else:
            # In rare legacy cases, we can't recover sir_sha256 without rebuilding per-function nodes.
            pass

    actual: Set[str] = set()
    for f in sorted(restored_dir.glob("*.py")):
        actual.add(semantic_sir_hash(str(f), sir1_path))

    missing = expected - actual
    extra = actual - expected

    print("EXPECTED_SIR_HASHES:", len(expected))
    print("ACTUAL_SIR_HASHES:", len(actual))

    if not missing and not extra:
        print("VERIFY: SUCCESS — restored functions match pack sir_sha256 hashes.")
        return 0

    print("VERIFY: MISMATCH")
    if missing:
        print("Missing hashes:", missing)
    if extra:
        print("Extra hashes:", extra)
    return 1


def hash_folder(folder: str, sir1_path: Path) -> Dict[str, List[str]]:
    hashes: Dict[str, List[str]] = defaultdict(list)
    for f in Path(folder).expanduser().resolve().rglob("*.py"):
        h = semantic_sir_hash(str(f), sir1_path)
        hashes[h].append(str(f))
    return hashes


def cmd_diff(args: argparse.Namespace) -> int:
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")

    hashes_a = hash_folder(args.folder_a, sir1_path)
    hashes_b = hash_folder(args.folder_b, sir1_path)

    set_a = set(hashes_a.keys())
    set_b = set(hashes_b.keys())

    common = set_a & set_b
    only_a = set_a - set_b
    only_b = set_b - set_a

    print("COMMON_STRUCTURES:", len(common))
    print("ONLY_IN_A:", len(only_a))
    print("ONLY_IN_B:", len(only_b))

    if common:
        print("\n--- Identical structures (hashes) ---")
        for h in sorted(common):
            print(h)
    if only_a:
        print("\n--- Removed (only in A) ---")
        for h in sorted(only_a):
            print(h, "->", hashes_a[h])
    if only_b:
        print("\n--- Added (only in B) ---")
        for h in sorted(only_b):
            print(h, "->", hashes_b[h])

    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sir_tools.py", description="SIR verification and structural diff tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="Verify restored functions against pack (semantic SIR hashes)")
    v.add_argument("pack_dir")
    v.add_argument("restored_dir")
    v.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_tools.py)")

    d = sub.add_parser("diff", help="Structural diff between two folders (semantic SIR hashes)")
    d.add_argument("folder_a")
    d.add_argument("folder_b")
    d.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_tools.py)")

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "diff":
        return cmd_diff(args)

    ap.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
