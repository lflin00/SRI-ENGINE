#!/usr/bin/env python3
"""
sir_pack.py — Build a global SIR pack (nodes + roots + name_maps) for a folder of Python code.

This is the next logical "product" step after sir_scan:
- Uses sir1.py to encode each function in --mode semantic (includes name_map).
- Deduplicates all nodes globally (content-addressed node ids).
- Writes a pack directory with:
    nodes.json        (global node store)
    roots.json        (list of function roots + file/line/qualname + hashes)
    namemaps.json     (name_map per function root)
    meta.json         (pack metadata)
    bundle.json       (meta + nodes + roots + namemaps)
  Optionally compresses bundle.json using zstd (if installed).

Requirements:
- Python 3.9+
- sir1.py (v0.2+) available (same folder by default, or via --sir1)
- Optional: zstd CLI for compression (--zstd)

USAGE
-----
python3 sir_pack.py pack ./demo_scan
python3 sir_pack.py pack ./demo_scan -o ./my_pack
python3 sir_pack.py pack ./demo_scan --include-methods
python3 sir_pack.py pack ./demo_scan --zstd
python3 sir_pack.py stats ./my_pack
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FuncMeta:
    file: str
    qualname: str
    lineno: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_py_files(root: Path) -> List[Path]:
    if root.is_file() and root.suffix == ".py":
        return [root]
    return sorted([Path(p) for p in glob.glob(str(root / "**" / "*.py"), recursive=True)])


def extract_functions(py_path: Path, include_methods: bool) -> List[Tuple[FuncMeta, str]]:
    """Returns list of (FuncMeta, source_segment)."""
    src = py_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    out: List[Tuple[FuncMeta, str]] = []

    def add_func(qualname: str, node: ast.AST):
        seg = ast.get_source_segment(src, node)
        if seg:
            lineno = getattr(node, "lineno", 1)
            out.append((FuncMeta(file=str(py_path), qualname=qualname, lineno=lineno), seg))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_func(node.name, node)
        elif include_methods and isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_func(f"{node.name}.{sub.name}", sub)

    return out


def run_sir1_encode_semantic(src: str, sir1_path: Path) -> Dict[str, Any]:
    p = subprocess.run(
        ["python3", str(sir1_path), "encode", "-", "--mode", "semantic"],
        input=src,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "sir1.py encode failed")
    obj = json.loads(p.stdout)
    if not isinstance(obj, dict) or "nodes" not in obj or "root" not in obj:
        raise RuntimeError("sir1.py output did not look like SIR JSON")
    if "name_map" not in obj:
        raise RuntimeError("sir1.py semantic encode did not include name_map (need v0.2+)")
    return obj


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj: Any, pretty: bool = False) -> None:
    if pretty:
        text = json.dumps(obj, indent=2, sort_keys=True)
    else:
        text = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def try_zstd(in_path: Path, out_path: Path, level: int = 19) -> bool:
    try:
        p = subprocess.run(
            ["zstd", f"-{level}", str(in_path), "-o", str(out_path)],
            capture_output=True,
            text=True,
        )
        return p.returncode == 0
    except FileNotFoundError:
        return False


def cmd_pack(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve() if args.out else (Path.cwd() / ".sir_pack")
    sir1_path = Path(args.sir1).expanduser().resolve() if args.sir1 else (Path(__file__).parent / "sir1.py")

    if not sir1_path.exists():
        print(f"Error: sir1.py not found at: {sir1_path}", file=sys.stderr)
        print("Tip: pass --sir1 /path/to/sir1.py", file=sys.stderr)
        return 2

    files = iter_py_files(root)
    if not files:
        print("No .py files found.")
        return 0

    global_nodes: Dict[str, Any] = {}
    roots: List[Dict[str, Any]] = []
    namemaps: Dict[str, Any] = {}

    total_funcs = 0
    for f in files:
        funcs = extract_functions(f, include_methods=bool(args.include_methods))
        total_funcs += len(funcs)
        for meta, code in funcs:
            sir = run_sir1_encode_semantic(code, sir1_path)
            root_id = sir["root"]

            for nid, nd in sir["nodes"].items():
                global_nodes[nid] = nd

            roots.append({
                "root": root_id,
                "file": meta.file,
                "qualname": meta.qualname,
                "lineno": meta.lineno,
                "sir_sha256": sir.get("sir_sha256"),
                "source_sha256": sha256_bytes(code.encode("utf-8")),
            })

            namemaps[root_id] = sir["name_map"]

    meta = {
        "format": "SIR-PACK",
        "version": "0.1",
        "source_path": str(root),
        "sir1_path": str(sir1_path),
        "include_methods": bool(args.include_methods),
        "files_scanned": len(files),
        "total_functions": total_funcs,
        "unique_roots": len({r["root"] for r in roots}),
        "unique_nodes": len(global_nodes),
    }

    ensure_dir(out_dir)
    write_json(out_dir / "nodes.json", global_nodes, pretty=False)
    write_json(out_dir / "roots.json", roots, pretty=False)
    write_json(out_dir / "namemaps.json", namemaps, pretty=False)
    write_json(out_dir / "meta.json", meta, pretty=True)

    bundle_path = out_dir / "bundle.json"
    write_json(bundle_path, {"meta": meta, "nodes": global_nodes, "roots": roots, "namemaps": namemaps}, pretty=False)

    print(f"Packed to: {out_dir}")
    print(f"FILES_SCANNED: {meta['files_scanned']}")
    print(f"TOTAL_FUNCTIONS: {meta['total_functions']}")
    print(f"UNIQUE_ROOTS: {meta['unique_roots']}")
    print(f"UNIQUE_NODES: {meta['unique_nodes']}")
    print(f"BUNDLE_BYTES: {bundle_path.stat().st_size}")

    if args.zstd:
        zst_path = out_dir / "bundle.json.zst"
        ok = try_zstd(bundle_path, zst_path, level=int(args.zstd_level))
        if ok:
            print(f"ZSTD: wrote {zst_path} ({zst_path.stat().st_size} bytes)")
        else:
            print("ZSTD: failed or zstd not installed. Install zstd or omit --zstd.", file=sys.stderr)

    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir).expanduser().resolve()
    meta_path = pack_dir / "meta.json"
    if not meta_path.exists():
        print(f"Error: meta.json not found in {pack_dir}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    print(json.dumps(meta, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sir_pack.py", description="Build a global SIR pack for Python code.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pack", help="Create a SIR pack from a folder or file")
    p.add_argument("path", help="Folder or .py file to pack")
    p.add_argument("-o", "--out", help="Output directory (default: ./.sir_pack)")
    p.add_argument("--sir1", help="Path to sir1.py (default: alongside sir_pack.py)")
    p.add_argument("--include-methods", action="store_true", help="Also pack methods inside classes")
    p.add_argument("--zstd", action="store_true", help="Also write bundle.json.zst (requires zstd CLI)")
    p.add_argument("--zstd-level", type=int, default=19, help="zstd compression level (default: 19)")

    s = sub.add_parser("stats", help="Print pack meta.json")
    s.add_argument("pack_dir", help="Pack directory (contains meta.json)")

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.cmd == "pack":
        return cmd_pack(args)
    if args.cmd == "stats":
        return cmd_stats(args)

    ap.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
