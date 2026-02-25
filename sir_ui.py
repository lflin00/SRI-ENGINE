#!/usr/bin/env python3
"""
sir_ui.py — Minimal local UI for the SIR Engine (Streamlit).

What you can do in the UI:
- Scan: find semantic-duplicate functions in a folder (function-level)
- Pack: build a global SIR pack + optional zstd, and view pack stats
- Unpack: list occurrences, restore occurrences to files
- Verify: check restored functions match pack sir_sha256 hashes
- Diff: structural diff between two folders

Run:
  cd ~/Downloads/Jarvis/SIR/SIR_MAIN
  python3 -m pip install --upgrade streamlit
  streamlit run sir_ui.py
"""

from __future__ import annotations

import ast
import glob
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from sir.core import encode, hash_source, hash_file, decode_sir


# ---------------- helpers ----------------

@dataclass(frozen=True)
class Occur:
    file: str
    qualname: str
    lineno: int
    semantic_hash: str


def iter_py_files(root: Path) -> List[Path]:
    if root.is_file() and root.suffix == ".py":
        return [root]
    return sorted([Path(p) for p in glob.glob(str(root / "**" / "*.py"), recursive=True)])


def extract_functions(py_path: Path, include_methods: bool) -> List[Tuple[str, int, str]]:
    src = py_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    out: List[Tuple[str, int, str]] = []

    def add_func(name: str, node: ast.AST):
        seg = ast.get_source_segment(src, node)
        if seg:
            out.append((name, getattr(node, "lineno", 1), seg))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_func(node.name, node)
        elif include_methods and isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_func(f"{node.name}.{sub.name}", sub)

    return out


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def try_zstd(in_path: Path, out_path: Path, level: int = 19) -> Tuple[bool, str]:
    try:
        p = subprocess.run(["zstd", f"-{level}", str(in_path), "-o", str(out_path)], capture_output=True, text=True)
        if p.returncode == 0:
            return True, p.stdout.strip() or "ok"
        return False, (p.stderr.strip() or p.stdout.strip() or "zstd failed")
    except FileNotFoundError:
        return False, "zstd not installed"


def load_pack(pack_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    nodes = json.loads((pack_dir / "nodes.json").read_text(encoding="utf-8"))
    roots = json.loads((pack_dir / "roots.json").read_text(encoding="utf-8"))
    namemaps = json.loads((pack_dir / "namemaps.json").read_text(encoding="utf-8"))
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    return nodes, roots, namemaps, meta


def safe_slug(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def occurrence_filename(r: Dict[str, Any], idx: int) -> str:
    file_part = safe_slug(Path(r["file"]).name)
    qual = safe_slug(r["qualname"])
    line = int(r.get("lineno", 1))
    root8 = r["root"][:8]
    return f"{idx:04d}_{file_part}_L{line}_{qual}_{root8}.py"


# ---------------- UI ----------------

st.set_page_config(page_title="SIR Engine UI", layout="wide")
st.title("SIR Engine UI")
st.caption("Semantic structure hashing, duplicate detection, packing, unpacking, verification, and diffing — locally.")

tab_scan, tab_pack, tab_unpack, tab_verify, tab_diff = st.tabs(["Scan", "Pack", "Unpack", "Verify", "Diff"])


with tab_scan:
    st.subheader("Scan: semantic duplicate functions")
    path = st.text_input("Folder or .py file to scan", value="demo_scan")
    include_methods = st.checkbox("Include class methods", value=False)
    min_cluster = st.number_input("Min cluster size", min_value=2, max_value=50, value=2, step=1)

    if st.button("Run scan"):
        root = Path(path).expanduser().resolve()
        files = iter_py_files(root)
        if not files:
            st.warning("No .py files found.")
        else:
            groups: Dict[str, List[Occur]] = defaultdict(list)
            total_funcs = 0
            for f in files:
                for qualname, lineno, code in extract_functions(f, include_methods):
                    h = hash_source(code, mode="semantic")
                    total_funcs += 1
                    groups[h].append(Occur(file=str(f), qualname=qualname, lineno=lineno, semantic_hash=h))

            dupes = {h: occ for h, occ in groups.items() if len(occ) >= int(min_cluster)}

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files", len(files))
            c2.metric("Functions", total_funcs)
            c3.metric("Unique structures", len(groups))
            c4.metric(f"Duplicate clusters (≥{min_cluster})", len(dupes))

            if not dupes:
                st.info("No duplicate clusters found.")
            else:
                for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0])):
                    with st.expander(f"{h}  —  count={len(occs)}", expanded=False):
                        st.table([{"file": o.file, "line": o.lineno, "qualname": o.qualname} for o in occs])


with tab_pack:
    st.subheader("Pack: build a global node store + roots + name maps")
    path = st.text_input("Folder or .py file to pack", value="demo_scan", key="pack_path")
    out_dir = st.text_input("Output pack directory", value=".sir_pack_ui")
    include_methods = st.checkbox("Include class methods", value=False, key="pack_methods")
    do_zstd = st.checkbox("Also create bundle.json.zst (requires zstd installed)", value=True)
    zstd_level = st.slider("zstd level", min_value=1, max_value=22, value=19)

    if st.button("Build pack"):
        root = Path(path).expanduser().resolve()
        outp = Path(out_dir).expanduser().resolve()
        files = iter_py_files(root)
        if not files:
            st.warning("No .py files found.")
        else:
            global_nodes: Dict[str, Any] = {}
            roots: List[Dict[str, Any]] = []
            namemaps: Dict[str, Any] = {}
            total_funcs = 0

            for f in files:
                funcs = extract_functions(f, include_methods)
                total_funcs += len(funcs)
                for qualname, lineno, code in funcs:
                    sir = encode(code, mode="semantic")
                    root_id = sir["root"]
                    for nid, nd in sir["nodes"].items():
                        global_nodes[nid] = nd
                    roots.append({
                        "root": root_id,
                        "file": str(f),
                        "qualname": qualname,
                        "lineno": lineno,
                        "sir_sha256": sir.get("sir_sha256"),
                    })
                    namemaps[root_id] = sir.get("name_map", {})

            meta = {
                "format": "SIR-PACK",
                "version": "0.2-ui",
                "source_path": str(root),
                "include_methods": bool(include_methods),
                "files_scanned": len(files),
                "total_functions": total_funcs,
                "unique_roots": len({r["root"] for r in roots}),
                "unique_nodes": len(global_nodes),
            }

            ensure_dir(outp)
            (outp / "nodes.json").write_text(json.dumps(global_nodes, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
            (outp / "roots.json").write_text(json.dumps(roots, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
            (outp / "namemaps.json").write_text(json.dumps(namemaps, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
            (outp / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            bundle = {"meta": meta, "nodes": global_nodes, "roots": roots, "namemaps": namemaps}
            bundle_path = outp / "bundle.json"
            bundle_path.write_text(json.dumps(bundle, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files", meta["files_scanned"])
            c2.metric("Functions", meta["total_functions"])
            c3.metric("Unique nodes", meta["unique_nodes"])
            c4.metric("bundle.json bytes", bundle_path.stat().st_size)

            st.success(f"Packed to {outp}")

            if do_zstd:
                zst_path = outp / "bundle.json.zst"
                ok, msg = try_zstd(bundle_path, zst_path, level=int(zstd_level))
                if ok:
                    st.success(f"Zstd wrote {zst_path} ({zst_path.stat().st_size} bytes)")
                else:
                    st.warning(f"Zstd not created: {msg}")


with tab_unpack:
    st.subheader("Unpack: list + restore occurrences to files")
    pack_dir = st.text_input("Pack directory", value=".sir_pack_ui")
    out_dir = st.text_input("Restore output folder", value="restored_ui")
    which = st.selectbox("Restore mode", ["List only", "Restore one occurrence", "Restore all occurrences"])
    idx = st.number_input("Occurrence index (for restore one)", min_value=0, value=0, step=1)

    if st.button("Run unpack"):
        pdir = Path(pack_dir).expanduser().resolve()
        if not (pdir / "nodes.json").exists():
            st.error("Pack dir doesn't look valid (missing nodes.json).")
        else:
            nodes, roots, namemaps, meta = load_pack(pdir)
            st.write("Pack meta:", meta)

            st.dataframe([{
                "idx": i,
                "root": r["root"],
                "file": r["file"],
                "qualname": r["qualname"],
                "lineno": r["lineno"],
                "sir_sha256": r.get("sir_sha256")
            } for i, r in enumerate(roots)], use_container_width=True, height=300)

            if which == "List only":
                st.info("Listed occurrences above.")
            else:
                outp = Path(out_dir).expanduser().resolve()
                ensure_dir(outp)

                def write_occ(i: int):
                    r = roots[i]
                    root_id = r["root"]
                    nm = namemaps.get(root_id, {})
                    code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=True)
                    out_path = outp / occurrence_filename(r, i)
                    out_path.write_text(code + "\n", encoding="utf-8")
                    return out_path

                if which == "Restore one occurrence":
                    if idx >= len(roots):
                        st.error(f"Index out of range (0..{len(roots)-1})")
                    else:
                        out_path = write_occ(int(idx))
                        st.success(f"Wrote {out_path}")
                        st.code(out_path.read_text(encoding="utf-8"), language="python")

                if which == "Restore all occurrences":
                    written = []
                    for i in range(len(roots)):
                        written.append(str(write_occ(i)))
                    st.success(f"Wrote {len(written)} files into {outp}")


with tab_verify:
    st.subheader("Verify: restored folder matches pack hashes")
    pack_dir = st.text_input("Pack directory", value=".sir_pack_ui", key="verify_pack")
    restored_dir = st.text_input("Restored folder (one function per file)", value="restored_ui")

    if st.button("Run verify"):
        pdir = Path(pack_dir).expanduser().resolve()
        rdir = Path(restored_dir).expanduser().resolve()
        if not (pdir / "roots.json").exists():
            st.error("Pack dir missing roots.json")
        elif not rdir.exists():
            st.error("Restored folder does not exist")
        else:
            roots = json.loads((pdir / "roots.json").read_text(encoding="utf-8"))
            expected = {r["sir_sha256"] for r in roots if r.get("sir_sha256")}
            actual = set()
            for f in sorted(rdir.glob("*.py")):
                actual.add(hash_file(str(f), mode="semantic"))

            missing = expected - actual
            extra = actual - expected

            c1, c2, c3 = st.columns(3)
            c1.metric("Expected hashes", len(expected))
            c2.metric("Actual hashes", len(actual))
            c3.metric("Mismatch count", len(missing) + len(extra))

            if not missing and not extra:
                st.success("VERIFY: SUCCESS — restored functions match pack sir_sha256 hashes.")
            else:
                st.error("VERIFY: MISMATCH")
                if missing:
                    st.write("Missing hashes:", list(missing))
                if extra:
                    st.write("Extra hashes:", list(extra))


with tab_diff:
    st.subheader("Diff: structural diff between two folders")
    folder_a = st.text_input("Folder A", value="demo_scan")
    folder_b = st.text_input("Folder B", value="restored_ui")

    if st.button("Run diff"):
        def hash_folder(folder: str) -> Dict[str, List[str]]:
            hashes: Dict[str, List[str]] = defaultdict(list)
            for f in Path(folder).expanduser().resolve().rglob("*.py"):
                h = hash_file(str(f), mode="semantic")
                hashes[h].append(str(f))
            return hashes

        ha = hash_folder(folder_a)
        hb = hash_folder(folder_b)
        set_a = set(ha.keys())
        set_b = set(hb.keys())

        common = sorted(set_a & set_b)
        only_a = sorted(set_a - set_b)
        only_b = sorted(set_b - set_a)

        c1, c2, c3 = st.columns(3)
        c1.metric("Common structures", len(common))
        c2.metric("Only in A", len(only_a))
        c3.metric("Only in B", len(only_b))

        if common:
            st.write("### Common (hashes)")
            st.code("\n".join(common))

        if only_a:
            st.write("### Only in A")
            st.json({h: ha[h] for h in only_a})

        if only_b:
            st.write("### Only in B")
            st.json({h: hb[h] for h in only_b})
