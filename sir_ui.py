#!/usr/bin/env python3
"""
sir_ui.py — SIR Engine Web App (fully browser-based, Streamlit Cloud compatible)
"""

from __future__ import annotations

import ast
import io
import json
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import streamlit as st
from sir.core import encode, hash_source, decode_sir


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Occur:
    file: str
    qualname: str
    lineno: int
    semantic_hash: str


def extract_functions(src: str, filename: str, include_methods: bool) -> List[Tuple[str, int, str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        st.warning(f"Skipping {filename}: SyntaxError — {e}")
        return []
    out = []

    def add_func(name, node):
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


def safe_slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in ("_", "-", ".")) else "_" for c in s)


def occurrence_filename(r: Dict[str, Any], idx: int) -> str:
    file_part = safe_slug(Path(r["file"]).name)
    qual = safe_slug(r["qualname"])
    line = int(r.get("lineno", 1))
    root8 = r["root"][:8]
    return f"{idx:04d}_{file_part}_L{line}_{qual}_{root8}.py"


# ─────────────────────────────────────────────
#  Page config
# ─────────────────────────────────────────────

st.set_page_config(page_title="SIR Engine", layout="wide", page_icon="🔍")
st.title("🔍 SIR Engine")
st.caption("Semantic duplicate detection, structural compression, and code diffing for Python.")

tab_scan, tab_pack, tab_unpack, tab_verify, tab_diff, tab_about = st.tabs([
    "Scan", "Pack", "Unpack", "Verify", "Diff", "About"
])


# ─────────────────────────────────────────────
#  SCAN
# ─────────────────────────────────────────────

with tab_scan:
    st.subheader("Scan: find structurally duplicate functions")
    st.write("Upload `.py` files — SIR finds functions that are logically identical even if they have different names or variable names.")

    uploaded = st.file_uploader("Upload Python files", type=["py"], accept_multiple_files=True, key="scan_upload")
    col1, col2 = st.columns(2)
    with col1:
        include_methods = st.checkbox("Include class methods", value=False)
    with col2:
        min_cluster = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1)

    if st.button("Run scan", type="primary"):
        if not uploaded:
            st.warning("Please upload at least one .py file.")
        else:
            groups: Dict[str, List[Occur]] = defaultdict(list)
            total_funcs = 0
            with st.spinner(f"Scanning {len(uploaded)} file(s)..."):
                for f in uploaded:
                    src = f.read().decode("utf-8", errors="replace")
                    for qualname, lineno, code in extract_functions(src, f.name, include_methods):
                        total_funcs += 1
                        try:
                            h = hash_source(code, mode="semantic")
                            groups[h].append(Occur(file=f.name, qualname=qualname, lineno=lineno, semantic_hash=h))
                        except Exception:
                            pass

            dupes = {h: occ for h, occ in groups.items() if len(occ) >= int(min_cluster)}

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files", len(uploaded))
            c2.metric("Functions", total_funcs)
            c3.metric("Unique structures", len(groups))
            c4.metric(f"Duplicate clusters (≥{min_cluster})", len(dupes))
            st.divider()

            if not dupes:
                st.success("✅ No duplicate function structures found!")
            else:
                st.error(f"⚠️ Found {len(dupes)} duplicate cluster(s)")
                for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0])):
                    with st.expander(f"🔴 {len(occs)} duplicates — hash: `{h[:16]}...`", expanded=True):
                        for o in occs:
                            st.markdown(f"- **`{o.qualname}`** in `{o.file}` (line {o.lineno})")

                report = {
                    "files_scanned": len(uploaded),
                    "total_functions": total_funcs,
                    "duplicate_clusters": [
                        {"semantic_hash": h, "count": len(occs),
                         "occurrences": [{"file": o.file, "qualname": o.qualname, "lineno": o.lineno} for o in occs]}
                        for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0]))
                    ],
                }
                st.download_button("📥 Download report (JSON)", data=json.dumps(report, indent=2),
                                   file_name="sir_report.json", mime="application/json")


# ─────────────────────────────────────────────
#  PACK
# ─────────────────────────────────────────────

with tab_pack:
    st.subheader("Pack: compress Python files into a SIR bundle")
    st.write("Upload `.py` files — SIR encodes each function into a structural node graph, deduplicates shared logic, and bundles everything into a single downloadable `bundle.json`.")

    pack_uploaded = st.file_uploader("Upload Python files to pack", type=["py"], accept_multiple_files=True, key="pack_upload")
    pack_methods = st.checkbox("Include class methods", value=False, key="pack_methods")

    if st.button("Build pack", type="primary"):
        if not pack_uploaded:
            st.warning("Please upload at least one .py file.")
        else:
            global_nodes: Dict[str, Any] = {}
            roots: List[Dict[str, Any]] = []
            namemaps: Dict[str, Any] = {}
            total_funcs = 0
            errors = 0

            with st.spinner(f"Packing {len(pack_uploaded)} file(s)..."):
                for f in pack_uploaded:
                    src = f.read().decode("utf-8", errors="replace")
                    funcs = extract_functions(src, f.name, pack_methods)
                    total_funcs += len(funcs)
                    for qualname, lineno, code in funcs:
                        try:
                            sir = encode(code, mode="semantic")
                            root_id = sir["root"]
                            for nid, nd in sir["nodes"].items():
                                global_nodes[nid] = nd
                            roots.append({
                                "root": root_id,
                                "file": f.name,
                                "qualname": qualname,
                                "lineno": lineno,
                                "sir_sha256": sir.get("sir_sha256"),
                            })
                            namemaps[root_id] = sir.get("name_map", {})
                        except Exception:
                            errors += 1

            meta = {
                "format": "SIR-PACK",
                "version": "0.3-web",
                "files_scanned": len(pack_uploaded),
                "total_functions": total_funcs,
                "unique_roots": len({r["root"] for r in roots}),
                "unique_nodes": len(global_nodes),
            }
            bundle = {"meta": meta, "nodes": global_nodes, "roots": roots, "namemaps": namemaps}
            bundle_json = json.dumps(bundle, separators=(",", ":"), sort_keys=True)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files packed", meta["files_scanned"])
            c2.metric("Functions", meta["total_functions"])
            c3.metric("Unique nodes", meta["unique_nodes"])
            c4.metric("Bundle size", f"{len(bundle_json):,} bytes")

            if errors:
                st.warning(f"{errors} function(s) failed to encode and were skipped.")

            st.success("✅ Pack built successfully!")
            st.download_button(
                "📥 Download bundle.json",
                data=bundle_json,
                file_name="bundle.json",
                mime="application/json",
            )


# ─────────────────────────────────────────────
#  UNPACK
# ─────────────────────────────────────────────

with tab_unpack:
    st.subheader("Unpack: restore Python functions from a SIR bundle")
    st.write("Upload a `bundle.json` produced by Pack. SIR will reconstruct all the original Python functions and let you download them as a `.zip`.")

    bundle_file = st.file_uploader("Upload bundle.json", type=["json"], key="unpack_upload")
    rehydrate = st.checkbox("Restore original variable names (rehydrate)", value=True)

    if st.button("Unpack", type="primary"):
        if not bundle_file:
            st.warning("Please upload a bundle.json file.")
        else:
            with st.spinner("Unpacking..."):
                try:
                    bundle = json.loads(bundle_file.read().decode("utf-8"))
                    nodes = bundle["nodes"]
                    roots = bundle["roots"]
                    namemaps = bundle.get("namemaps", {})
                    meta = bundle.get("meta", {})

                    st.write("**Pack info:**", meta)

                    # Build zip in memory
                    zip_buffer = io.BytesIO()
                    restored = []
                    errors = 0

                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        for i, r in enumerate(roots):
                            root_id = r["root"]
                            nm = namemaps.get(root_id, {}) if rehydrate else {}
                            try:
                                code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=rehydrate)
                                fname = occurrence_filename(r, i)
                                zf.writestr(fname, code + "\n")
                                restored.append({"file": fname, "qualname": r["qualname"], "original_file": r["file"]})
                            except Exception as e:
                                errors += 1

                    zip_buffer.seek(0)

                    c1, c2 = st.columns(2)
                    c1.metric("Functions restored", len(restored))
                    c2.metric("Errors", errors)

                    st.success(f"✅ Restored {len(restored)} function(s)!")

                    # Show preview
                    with st.expander("Preview restored functions", expanded=False):
                        st.dataframe(restored, use_container_width=True)

                    st.download_button(
                        "📥 Download restored functions (.zip)",
                        data=zip_buffer,
                        file_name="restored_functions.zip",
                        mime="application/zip",
                    )

                except Exception as e:
                    st.error(f"Failed to unpack: {e}")


# ─────────────────────────────────────────────
#  VERIFY
# ─────────────────────────────────────────────

with tab_verify:
    st.subheader("Verify: confirm restored functions match pack hashes")
    st.write("Upload the original `bundle.json` and the restored `.py` files. SIR will verify that every function's structural hash matches.")

    verify_bundle = st.file_uploader("Upload bundle.json", type=["json"], key="verify_bundle")
    verify_files = st.file_uploader("Upload restored .py files", type=["py"], accept_multiple_files=True, key="verify_files")

    if st.button("Run verify", type="primary"):
        if not verify_bundle or not verify_files:
            st.warning("Please upload both the bundle.json and the restored .py files.")
        else:
            with st.spinner("Verifying..."):
                bundle = json.loads(verify_bundle.read().decode("utf-8"))
                roots = bundle["roots"]

                # Expected hashes from pack
                expected = {r["sir_sha256"] for r in roots if r.get("sir_sha256")}

                # Actual hashes from uploaded restored files
                actual = set()
                for f in verify_files:
                    src = f.read().decode("utf-8", errors="replace")
                    try:
                        h = hash_source(src, mode="semantic")
                        actual.add(h)
                    except Exception:
                        pass

            missing = expected - actual
            extra = actual - expected

            c1, c2, c3 = st.columns(3)
            c1.metric("Expected hashes", len(expected))
            c2.metric("Actual hashes", len(actual))
            c3.metric("Mismatches", len(missing) + len(extra))
            st.divider()

            if not missing and not extra:
                st.success("✅ VERIFY PASSED — all restored functions match the pack hashes perfectly.")
            else:
                st.error("❌ VERIFY FAILED — mismatch detected")
                if missing:
                    st.write("**Missing from restored files:**")
                    for h in missing:
                        st.code(h)
                if extra:
                    st.write("**Extra hashes not in pack:**")
                    for h in extra:
                        st.code(h)


# ─────────────────────────────────────────────
#  DIFF
# ─────────────────────────────────────────────

with tab_diff:
    st.subheader("Diff: compare two sets of Python files structurally")
    st.write("Upload files for **Set A** and **Set B**. SIR shows which logical structures are shared, unique to A, or unique to B.")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Set A**")
        files_a = st.file_uploader("Upload Set A", type=["py"], accept_multiple_files=True, key="diff_a")
    with col_b:
        st.markdown("**Set B**")
        files_b = st.file_uploader("Upload Set B", type=["py"], accept_multiple_files=True, key="diff_b")

    if st.button("Run diff", type="primary"):
        if not files_a or not files_b:
            st.warning("Please upload files for both sets.")
        else:
            def hash_uploaded(files):
                hashes = defaultdict(list)
                for f in files:
                    src = f.read().decode("utf-8", errors="replace")
                    try:
                        h = hash_source(src, mode="semantic")
                        hashes[h].append(f.name)
                    except Exception:
                        pass
                return hashes

            with st.spinner("Diffing..."):
                ha = hash_uploaded(files_a)
                hb = hash_uploaded(files_b)

            set_a, set_b = set(ha.keys()), set(hb.keys())
            common = sorted(set_a & set_b)
            only_a = sorted(set_a - set_b)
            only_b = sorted(set_b - set_a)

            c1, c2, c3 = st.columns(3)
            c1.metric("Common structures", len(common))
            c2.metric("Only in A", len(only_a))
            c3.metric("Only in B", len(only_b))
            st.divider()

            if common:
                with st.expander(f"✅ {len(common)} shared structure(s)", expanded=True):
                    for h in common:
                        st.markdown(f"- `{h[:16]}...` → A: `{'`, `'.join(ha[h])}` | B: `{'`, `'.join(hb[h])}`")
            if only_a:
                with st.expander(f"🔵 {len(only_a)} only in Set A", expanded=True):
                    for h in only_a:
                        st.markdown(f"- `{h[:16]}...` → `{'`, `'.join(ha[h])}`")
            if only_b:
                with st.expander(f"🟠 {len(only_b)} only in Set B", expanded=True):
                    for h in only_b:
                        st.markdown(f"- `{h[:16]}...` → `{'`, `'.join(hb[h])}`")


# ─────────────────────────────────────────────
#  ABOUT
# ─────────────────────────────────────────────

with tab_about:
    st.subheader("What is SIR Engine?")
    st.markdown("""
**SIR (Structured Intermediate Representation)** is a semantic code analysis engine for Python.

Instead of comparing code as text, SIR parses Python into an abstract syntax tree, strips away 
all cosmetic differences (variable names, formatting, comments), and reduces it to its pure logical structure.
Two functions that do the same thing will always produce the **same structural hash** — even if they look completely different on the surface.

---

### The full pipeline

| Tab | What it does |
|-----|-------------|
| **Scan** | Upload `.py` files → find structurally duplicate functions |
| **Pack** | Upload `.py` files → compress into a `bundle.json` |
| **Unpack** | Upload `bundle.json` → restore all functions as `.py` files |
| **Verify** | Upload `bundle.json` + restored files → confirm hashes match |
| **Diff** | Upload two sets of files → compare their logical structures |

---

### How it works

1. Your Python file is parsed into an AST (Abstract Syntax Tree)
2. All identifiers are alpha-renamed to canonical placeholders (`v0`, `v1`, `f0`...)
3. The resulting structure is hashed with SHA-256
4. Identical hashes = identical logic, regardless of naming

---

### Built by
Lucas Flinders — [GitHub](https://github.com/lflin00/SRI-ENGINE)
    """)
