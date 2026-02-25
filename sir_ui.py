#!/usr/bin/env python3
"""
sir_ui.py — SIR Engine Web App (upload-based, Streamlit Cloud compatible)

Visitors upload .py files directly in the browser — no folder paths needed.
"""

from __future__ import annotations

import ast
import json
import tempfile
import os
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


def safe_slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in ("_", "-", ".")) else "_" for c in s)


# ─────────────────────────────────────────────
#  Page config
# ─────────────────────────────────────────────

st.set_page_config(page_title="SIR Engine", layout="wide", page_icon="🔍")

st.title("🔍 SIR Engine")
st.caption("Structural semantic duplicate detection for Python code — upload your files and find logic clones instantly.")

tab_scan, tab_diff, tab_about = st.tabs(["Scan", "Diff", "About"])


# ─────────────────────────────────────────────
#  SCAN TAB
# ─────────────────────────────────────────────

with tab_scan:
    st.subheader("Scan: find structurally duplicate functions")
    st.write("Upload one or more `.py` files. SIR will find functions that are logically identical — even if they have different names or variable names.")

    uploaded = st.file_uploader(
        "Upload Python files",
        type=["py"],
        accept_multiple_files=True,
        key="scan_upload",
    )

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
            errors = 0

            with st.spinner(f"Scanning {len(uploaded)} file(s)..."):
                for f in uploaded:
                    src = f.read().decode("utf-8", errors="replace")
                    funcs = extract_functions(src, f.name, include_methods)
                    total_funcs += len(funcs)
                    for qualname, lineno, code in funcs:
                        try:
                            h = hash_source(code, mode="semantic")
                            groups[h].append(Occur(
                                file=f.name,
                                qualname=qualname,
                                lineno=lineno,
                                semantic_hash=h,
                            ))
                        except Exception as e:
                            errors += 1

            dupes = {h: occ for h, occ in groups.items() if len(occ) >= int(min_cluster)}

            # Summary metrics
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files scanned", len(uploaded))
            c2.metric("Functions found", total_funcs)
            c3.metric("Unique structures", len(groups))
            c4.metric(f"Duplicate clusters (≥{min_cluster})", len(dupes))

            if errors:
                st.warning(f"{errors} function(s) failed to hash and were skipped.")

            st.divider()

            if not dupes:
                st.success("✅ No duplicate function structures found!")
                st.info("Every function in your uploaded files has a unique logical structure.")
            else:
                st.error(f"⚠️ Found {len(dupes)} duplicate cluster(s)")
                st.write("Functions in the same cluster are **logically identical** even if they look different:")

                for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0])):
                    with st.expander(f"🔴 {len(occs)} duplicates — structural hash: `{h[:16]}...`", expanded=True):
                        for o in occs:
                            st.markdown(f"- **`{o.qualname}`** in `{o.file}` (line {o.lineno})")

                # Download report
                report = {
                    "files_scanned": len(uploaded),
                    "total_functions": total_funcs,
                    "unique_structures": len(groups),
                    "duplicate_clusters": [
                        {
                            "semantic_hash": h,
                            "count": len(occs),
                            "occurrences": [
                                {"file": o.file, "qualname": o.qualname, "lineno": o.lineno}
                                for o in occs
                            ],
                        }
                        for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0]))
                    ],
                }
                st.download_button(
                    "📥 Download report (JSON)",
                    data=json.dumps(report, indent=2),
                    file_name="sir_report.json",
                    mime="application/json",
                )


# ─────────────────────────────────────────────
#  DIFF TAB
# ─────────────────────────────────────────────

with tab_diff:
    st.subheader("Diff: compare two sets of Python files structurally")
    st.write("Upload files for **Set A** and **Set B**. SIR will tell you which logical structures are shared, unique to A, or unique to B.")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Set A**")
        files_a = st.file_uploader("Upload Set A files", type=["py"], accept_multiple_files=True, key="diff_a")
    with col_b:
        st.markdown("**Set B**")
        files_b = st.file_uploader("Upload Set B files", type=["py"], accept_multiple_files=True, key="diff_b")

    if st.button("Run diff", type="primary"):
        if not files_a or not files_b:
            st.warning("Please upload files for both Set A and Set B.")
        else:
            def hash_uploaded(files) -> Dict[str, List[str]]:
                hashes: Dict[str, List[str]] = defaultdict(list)
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

            set_a = set(ha.keys())
            set_b = set(hb.keys())
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
#  ABOUT TAB
# ─────────────────────────────────────────────

with tab_about:
    st.subheader("What is SIR Engine?")
    st.markdown("""
**SIR (Structured Intermediate Representation)** is a semantic code analysis engine for Python.

Instead of comparing code as text, SIR parses Python into an abstract syntax tree, strips away 
all cosmetic differences (variable names, formatting, comments), and reduces it to its pure logical structure.
Two functions that do the same thing will always produce the **same structural hash** — even if they look completely different on the surface.

---

### Why does this matter?

In large codebases, the same logic gets written over and over by different developers who didn't know it already existed. 
Normal duplicate detectors miss these because they compare text, not meaning.

**SIR catches them all.** `calculate_total(price, tax_rate)` and `calc_total(x, y)` are the same function. SIR proves it.

---

### How it works

1. Your Python file is parsed into an AST (Abstract Syntax Tree)
2. All identifiers are alpha-renamed to canonical placeholders (`v0`, `v1`, `f0`...)
3. The resulting structure is hashed with SHA-256
4. Identical hashes = identical logic

---

### Built by
Lucas Flinders — [GitHub](https://github.com/lflin00/SRI-ENGINE)
    """)
