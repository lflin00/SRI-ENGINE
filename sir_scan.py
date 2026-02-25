#!/usr/bin/env python3
"""
sir_scan.py — Function-level semantic duplicate scanner (SIR Engine companion)

This tool turns your proof-of-concept into a product-feeling CLI command:
- Recursively scans a folder for .py files
- Extracts top-level functions (and optionally methods inside classes)
- Computes a semantic structural hash for each function using sir1.py
- Groups duplicates and prints clusters + summary stats
- Optionally writes a JSON report for later use (VS Code extension, UI, etc.)

REQUIREMENTS
------------
- Python 3.9+
- sir1.py in the same folder OR provide --sir1 path/to/sir1.py

USAGE
-----
# Scan a folder (top-level functions only)
python3 sir_scan.py scan ./demo_scan

# Include methods inside classes too
python3 sir_scan.py scan ./demo_scan --include-methods

# Write a JSON report
python3 sir_scan.py scan ./demo_scan -o report.json

# Only show clusters of size >= 3
python3 sir_scan.py scan ./demo_scan --min-cluster-size 3

# Save hashes cache (faster re-runs if files unchanged)
python3 sir_scan.py scan ./demo_scan --cache .sir_cache.json

NOTES
-----
- Hashing uses: python3 sir1.py hash - --mode semantic  (stdin)
- This is deterministic structural equality under your current canonicalization rules.
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FuncOccur:
    file: str
    qualname: str
    lineno: int


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_py_files(root: Path) -> List[Path]:
    if root.is_file() and root.suffix == ".py":
        return [root]
    return sorted([Path(p) for p in glob.glob(str(root / "**" / "*.py"), recursive=True)])


def extract_functions(py_path: Path, include_methods: bool) -> List[Tuple[str, int, str]]:
    """
    Returns list of (qualname, lineno, source_segment).
    - qualname for top-level: func
    - qualname for methods: ClassName.method
    """
    src = py_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)

    out: List[Tuple[str, int, str]] = []

    def add_func(name: str, node: ast.AST):
        seg = ast.get_source_segment(src, node)
        if seg:
            lineno = getattr(node, "lineno", 1)
            out.append((name, lineno, seg))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_func(node.name, node)
        elif include_methods and isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_func(f"{node.name}.{sub.name}", sub)

    return out


def semantic_hash_from_source(src: str, sir1_path: Path) -> str:
    p = subprocess.run(
        ["python3", str(sir1_path), "hash", "-", "--mode", "semantic"],
        input=src,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "sir1.py failed")
    return p.stdout.strip()


def load_cache(cache_path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    """
    Cache format:
    {
      "<file_path>": {
         "file_sha256": "<...>",
         "<qualname>:<lineno>": "<semantic_hash>",
         ...
      },
      ...
    }
    """
    if not cache_path:
        return {}
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache_path: Optional[Path], cache: Dict[str, Dict[str, str]]) -> None:
    if not cache_path:
        return
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")

    if not sir1_path.exists():
        print(f"Error: sir1.py not found at: {sir1_path}", file=sys.stderr)
        print("Tip: pass --sir1 /path/to/sir1.py", file=sys.stderr)
        return 2

    files = iter_py_files(root)
    if not files:
        print("No .py files found.")
        return 0

    cache_path = Path(args.cache).expanduser().resolve() if args.cache else None
    cache = load_cache(cache_path)

    groups: Dict[str, List[FuncOccur]] = defaultdict(list)

    total_funcs = 0
    hashed_funcs = 0
    cache_hits = 0

    for f in files:
        rel = str(f)
        fhash = file_sha256(f)

        entry = cache.get(rel, {})
        if entry.get("file_sha256") != fhash:
            entry = {"file_sha256": fhash}

        funcs = extract_functions(f, include_methods=bool(args.include_methods))
        total_funcs += len(funcs)

        for qualname, lineno, code in funcs:
            key = f"{qualname}:{lineno}"
            if key in entry:
                h = entry[key]
                cache_hits += 1
            else:
                h = semantic_hash_from_source(code, sir1_path)
                entry[key] = h
                hashed_funcs += 1

            groups[h].append(FuncOccur(file=rel, qualname=qualname, lineno=lineno))

        cache[rel] = entry

    # Build duplicate clusters
    dupes = {h: occ for h, occ in groups.items() if len(occ) >= int(args.min_cluster_size)}
    unique_funcs = len(groups)

    # Summary
    print(f"FILES_SCANNED: {len(files)}")
    print(f"TOTAL_FUNCTIONS: {total_funcs}")
    print(f"UNIQUE_FUNCTIONS: {unique_funcs}")
    print(f"DUPLICATE_CLUSTERS (>= {args.min_cluster_size}): {len(dupes)}")
    if cache_path:
        print(f"CACHE_HITS: {cache_hits}")
        print(f"HASHED_NOW: {hashed_funcs}")

    # Print clusters
    if dupes:
        print("\n--- Duplicate function clusters ---")
        for h, occ in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0])):
            print(f"\n{h}  (count={len(occ)})")
            for o in sorted(occ, key=lambda x: (x.file, x.lineno, x.qualname)):
                print(f"  - {o.file}:{o.lineno} :: {o.qualname}")
    else:
        print("\nNo duplicate clusters found with the current settings.")

    # Optional JSON report
    if args.out:
        report = {
            "path": str(root),
            "sir1_path": str(sir1_path),
            "include_methods": bool(args.include_methods),
            "min_cluster_size": int(args.min_cluster_size),
            "files_scanned": len(files),
            "total_functions": total_funcs,
            "unique_functions": unique_funcs,
            "duplicate_clusters": [
                {
                    "semantic_hash": h,
                    "count": len(occ),
                    "occurrences": [
                        {"file": o.file, "qualname": o.qualname, "lineno": o.lineno}
                        for o in sorted(occ, key=lambda x: (x.file, x.lineno, x.qualname))
                    ],
                }
                for h, occ in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0]))
            ],
        }
        out_path = Path(args.out).expanduser().resolve()
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nWrote report: {out_path}")

    save_cache(cache_path, cache)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sir_scan.py", description="Function-level semantic duplicate scanner (Python).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Scan a folder or file for semantic duplicate functions")
    scan.add_argument("path", help="Folder or .py file to scan")
    scan.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_scan.py)")
    scan.add_argument("--include-methods", action="store_true", help="Also hash methods inside classes")
    scan.add_argument("--min-cluster-size", type=int, default=2, help="Only show clusters with at least this many occurrences")
    scan.add_argument("-o", "--out", help="Write JSON report to this path")
    scan.add_argument("--cache", help="Path to cache file (JSON) to speed up re-scans")

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.cmd == "scan":
        return cmd_scan(args)

    ap.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
