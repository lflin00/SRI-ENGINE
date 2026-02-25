#!/usr/bin/env python3
"""
sir_unpack.py — Restore functions from a SIR pack into separate .py files (productized).

This version fixes a key issue you hit:
- Multiple occurrences can share the same root (true duplicates), so restoring "by root"
  alone can overwrite files. We now support restoring per-occurrence and generate
  unique filenames by default.

COMMANDS
--------
list
  List occurrences in the pack (one line per occurrence).

restore-root
  Restore ONE canonical function by root id (writes a single file).

restore-occurrence
  Restore ONE occurrence by its index from `list` (writes one file with unique name).

restore-all
  Restore ALL occurrences into separate files (unique filenames, no overwrites).

USAGE
-----
# List (with indices)
python3 sir_unpack.py list .sir_pack_demo

# Restore a specific occurrence (index shown in list)
python3 sir_unpack.py restore-occurrence .sir_pack_demo 2 -o restored_funcs

# Restore all occurrences (one file per occurrence, unique filenames)
python3 sir_unpack.py restore-all .sir_pack_demo -o restored_funcs

# Restore one canonical function by root id
python3 sir_unpack.py restore-root .sir_pack_demo d54e2e... -o out.py

NOTES
-----
- Uses sir1.py decode under the hood, with --rehydrate using the pack's namemap.
- Output code is formatting-normalized (AST unparse); comments won't be preserved.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_pack(pack_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    nodes = json.loads((pack_dir / "nodes.json").read_text(encoding="utf-8"))
    roots = json.loads((pack_dir / "roots.json").read_text(encoding="utf-8"))
    namemaps = json.loads((pack_dir / "namemaps.json").read_text(encoding="utf-8"))
    return nodes, roots, namemaps


def safe_slug(s: str) -> str:
    # file-safe slug
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_tmp_sir(pack_dir: Path, nodes: Dict[str, Any], root_id: str, name_map: Dict[str, Any]) -> Path:
    sir_obj = {"nodes": nodes, "root": root_id, "name_map": name_map}
    tmp_json = pack_dir / "__tmp_decode.json"
    tmp_json.write_text(json.dumps(sir_obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return tmp_json


def run_decode(tmp_json: Path, out_path: Path, sir1_path: Path) -> None:
    subprocess.run(
        ["python3", str(sir1_path), "decode", str(tmp_json), "-o", str(out_path), "--rehydrate"],
        check=True,
    )


def cmd_list(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    _, roots, _ = load_pack(pack_dir)
    for i, r in enumerate(roots):
        print(f"[{i}] {r['root']} :: {r['file']} :: {r['qualname']} (line {r['lineno']})")
    print(f"\nTotal occurrences: {len(roots)}")
    return 0


def cmd_restore_root(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")
    nodes, _, namemaps = load_pack(pack_dir)

    root_id = args.root
    if root_id not in namemaps:
        print("Warning: no namemap found for this root; proceeding with empty map.", file=sys.stderr)
        nm = {}
    else:
        nm = namemaps[root_id]

    out_path = Path(args.out).expanduser().resolve()
    tmp = write_tmp_sir(pack_dir, nodes, root_id, nm)
    try:
        run_decode(tmp, out_path, sir1_path)
    finally:
        if tmp.exists():
            tmp.unlink()

    print(f"Restored root {root_id} -> {out_path}")
    return 0


def occurrence_filename(r: Dict[str, Any], idx: int) -> str:
    file_part = safe_slug(Path(r["file"]).name)
    qual = safe_slug(r["qualname"])
    line = int(r.get("lineno", 1))
    root8 = r["root"][:8]
    return f"{idx:04d}_{file_part}_L{line}_{qual}_{root8}.py"


def cmd_restore_occurrence(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")
    nodes, roots, namemaps = load_pack(pack_dir)

    idx = int(args.index)
    if idx < 0 or idx >= len(roots):
        print(f"Error: index out of range (0..{len(roots)-1}).", file=sys.stderr)
        return 2

    r = roots[idx]
    root_id = r["root"]
    nm = namemaps.get(root_id, {})

    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_dir(out_dir)
    out_path = out_dir / occurrence_filename(r, idx)

    tmp = write_tmp_sir(pack_dir, nodes, root_id, nm)
    try:
        run_decode(tmp, out_path, sir1_path)
    finally:
        if tmp.exists():
            tmp.unlink()

    print(f"Wrote {out_path}")
    return 0


def cmd_restore_all(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")
    nodes, roots, namemaps = load_pack(pack_dir)

    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_dir(out_dir)

    for i, r in enumerate(roots):
        root_id = r["root"]
        nm = namemaps.get(root_id, {})
        out_path = out_dir / occurrence_filename(r, i)

        tmp = write_tmp_sir(pack_dir, nodes, root_id, nm)
        try:
            run_decode(tmp, out_path, sir1_path)
        finally:
            if tmp.exists():
                tmp.unlink()

        print(f"Wrote {out_path}")

    print(f"\nRestored {len(roots)} occurrences into {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sir_unpack.py", description="Restore functions from SIR pack.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", help="List occurrences in pack (with indices)")
    l.add_argument("pack_dir")

    rr = sub.add_parser("restore-root", help="Restore one canonical function by root id")
    rr.add_argument("pack_dir")
    rr.add_argument("root")
    rr.add_argument("-o", "--out", required=True)
    rr.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_unpack.py)")

    ro = sub.add_parser("restore-occurrence", help="Restore one occurrence by list index")
    ro.add_argument("pack_dir")
    ro.add_argument("index", help="Occurrence index from `list`")
    ro.add_argument("-o", "--out-dir", required=True, help="Output directory")
    ro.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_unpack.py)")

    ra = sub.add_parser("restore-all", help="Restore all occurrences into separate files (no overwrites)")
    ra.add_argument("pack_dir")
    ra.add_argument("-o", "--out-dir", required=True, help="Output directory")
    ra.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_unpack.py)")

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "restore-root":
        return cmd_restore_root(args)
    if args.cmd == "restore-occurrence":
        return cmd_restore_occurrence(args)
    if args.cmd == "restore-all":
        return cmd_restore_all(args)

    ap.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
