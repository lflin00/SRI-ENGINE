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
from datetime import datetime
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


def count_lines(src: str) -> int:
    return len([l for l in src.splitlines() if l.strip()])


def build_merge_report(
    file_sources: Dict[str, str],
    modified_sources: Dict[str, str],
    dupes_data: Dict[str, List],
    canonical_names: Dict[str, str],
    utils_functions: Dict[str, str],
) -> str:
    """Build a self-contained HTML summary report for a merge operation."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_removed = sum(len(occs) - 1 for occs in dupes_data.values())
    orig_lines = sum(count_lines(s) for s in file_sources.values())
    new_lines = sum(count_lines(s) for s in modified_sources.values())
    saved = orig_lines - new_lines

    clusters_html = ""
    for h, occs in dupes_data.items():
        canon = canonical_names.get(h, occs[0]["qualname"])
        dupes_list = "".join(
            f'<li><code>{o["qualname"]}</code> in <code>{o["file"]}</code> line {o["lineno"]}'
            f'{"  <span class=\"badge kept\">KEPT</span>" if o["qualname"] == canon else "  <span class=\"badge removed\">REMOVED</span>"}</li>'
            for o in occs
        )
        clusters_html += f"""
        <div class="cluster">
            <div class="cluster-header">
                Hash <code>{h[:24]}...</code> — {len(occs)} duplicates
            </div>
            <div class="cluster-body">
                <p>Canonical name kept: <strong><code>{canon}</code></strong> → moved to <code>utils.py</code></p>
                <ul>{dupes_list}</ul>
            </div>
        </div>"""

    files_html = ""
    for fname in file_sources:
        orig = count_lines(file_sources[fname])
        new = count_lines(modified_sources.get(fname, ""))
        delta = orig - new
        color = "#2ecc71" if delta > 0 else "#aaa"
        files_html += f"""
        <tr>
            <td><code>{fname}</code></td>
            <td>{orig}</td>
            <td>{new}</td>
            <td style="color:{color}">-{delta}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>SIR Engine Merge Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0e1117; color: #e0e0e0; margin: 0; padding: 32px; }}
  h1 {{ color: #fff; border-bottom: 2px solid #333; padding-bottom: 12px; }}
  h2 {{ color: #aaa; font-size: 1rem; text-transform: uppercase; letter-spacing: 1px; margin-top: 32px; }}
  .summary {{ display: flex; gap: 16px; margin: 24px 0; flex-wrap: wrap; }}
  .card {{ background: #1c1f26; border-radius: 8px; padding: 20px 28px; min-width: 140px; }}
  .card .num {{ font-size: 2rem; font-weight: bold; color: #fff; }}
  .card .label {{ color: #888; font-size: 0.85rem; margin-top: 4px; }}
  .cluster {{ background: #1c1f26; border-radius: 8px; margin: 12px 0; overflow: hidden; }}
  .cluster-header {{ background: #2a2d36; padding: 12px 16px; font-size: 0.9rem; }}
  .cluster-body {{ padding: 12px 16px; }}
  code {{ background: #2a2d36; padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }}
  ul {{ margin: 8px 0; padding-left: 20px; }}
  li {{ margin: 4px 0; line-height: 1.6; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.75rem;
            font-weight: bold; margin-left: 6px; }}
  .kept {{ background: #1a4a2e; color: #2ecc71; }}
  .removed {{ background: #4a1a1a; color: #e74c3c; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th {{ text-align: left; color: #888; font-size: 0.8rem; padding: 8px; border-bottom: 1px solid #333; }}
  td {{ padding: 8px; border-bottom: 1px solid #222; font-size: 0.85rem; }}
  .footer {{ margin-top: 40px; color: #555; font-size: 0.8rem; border-top: 1px solid #222; padding-top: 16px; }}
</style>
</head>
<body>
<h1>🔍 SIR Engine — Merge Report</h1>
<p style="color:#888">Generated {now}</p>

<div class="summary">
  <div class="card"><div class="num">{len(file_sources)}</div><div class="label">Files processed</div></div>
  <div class="card"><div class="num">{len(dupes_data)}</div><div class="label">Duplicate clusters</div></div>
  <div class="card"><div class="num">{total_removed}</div><div class="label">Functions removed</div></div>
  <div class="card"><div class="num">{len(utils_functions)}</div><div class="label">Functions in utils.py</div></div>
  <div class="card"><div class="num" style="color:#2ecc71">-{saved}</div><div class="label">Lines saved</div></div>
</div>

<h2>Duplicate Clusters</h2>
{clusters_html}

<h2>File Summary</h2>
<table>
  <tr><th>File</th><th>Original lines</th><th>New lines</th><th>Saved</th></tr>
  {files_html}
</table>

<div class="footer">
  Built with SIR Engine — github.com/lflin00/SRI-ENGINE
</div>
</body>
</html>"""


# ─────────────────────────────────────────────
#  Merge helpers
# ─────────────────────────────────────────────

def remove_function_node(tree: ast.Module, func_name: str) -> ast.Module:
    tree.body = [
        node for node in tree.body
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name)
    ]
    return tree


def rename_calls(source: str, old_name: str, new_name: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    class CallRenamer(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id == old_name:
                node.func.id = new_name
            elif isinstance(node.func, ast.Attribute) and node.func.attr == old_name:
                node.func.attr = new_name
            return node

    new_tree = CallRenamer().visit(tree)
    ast.fix_missing_locations(new_tree)
    try:
        return ast.unparse(new_tree)
    except Exception:
        return source


def add_import(source: str, func_name: str) -> str:
    import_line = f"from utils import {func_name}"
    if import_line in source:
        return source
    lines = source.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    return "\n".join(lines)


def get_function_source(source: str, func_name: str) -> str:
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                seg = ast.get_source_segment(source, node)
                if seg:
                    return seg
    except SyntaxError:
        pass
    return ""


# ─────────────────────────────────────────────
#  Page config
# ─────────────────────────────────────────────

st.set_page_config(page_title="SIR Engine", layout="wide", page_icon="🔍")
st.title("🔍 SIR Engine")
st.caption("Semantic duplicate detection, structural compression, and code diffing for Python.")

tab_scan, tab_js, tab_cross, tab_pack, tab_unpack, tab_verify, tab_diff, tab_merge, tab_about = st.tabs([
    "Scan (Python)", "Scan (JavaScript)", "Scan (Cross-Language)", "Pack", "Unpack", "Verify", "Diff", "Merge", "About"
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
            # Store file sources for syntax highlighting
            scan_sources: Dict[str, str] = {}
            groups: Dict[str, List[Occur]] = defaultdict(list)
            # Also store code per (file, qualname) for syntax highlighting
            func_code_map: Dict[str, str] = {}
            total_funcs = 0
            progress = st.progress(0, text="Starting scan...")
            status = st.empty()
            for i, f in enumerate(uploaded):
                status.text(f"Scanning {f.name}...")
                src = f.read().decode("utf-8", errors="replace")
                scan_sources[f.name] = src
                for qualname, lineno, code in extract_functions(src, f.name, include_methods):
                    total_funcs += 1
                    func_code_map[f"{f.name}::{qualname}"] = code
                    try:
                        h = hash_source(code, mode="semantic")
                        groups[h].append(Occur(file=f.name, qualname=qualname, lineno=lineno, semantic_hash=h))
                    except Exception:
                        pass
                progress.progress((i + 1) / len(uploaded), text=f"Scanned {i+1}/{len(uploaded)} files")
            progress.progress(1.0, text="Scan complete!")
            status.empty()

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
                        # Copy hash button
                        col_h, col_btn = st.columns([5, 1])
                        with col_h:
                            st.caption(f"Full hash: `{h}`")
                        with col_btn:
                            st.button("📋 Copy hash", key=f"copy_{h}",
                                      on_click=lambda hh=h: st.session_state.update({"copied_hash": hh}))

                        for o in occs:
                            st.markdown(f"**`{o.qualname}`** in `{o.file}` (line {o.lineno})")
                            # Syntax highlighted code
                            code_key = f"{o.file}::{o.qualname}"
                            if code_key in func_code_map:
                                st.code(func_code_map[code_key], language="python")

                if "copied_hash" in st.session_state:
                    st.info(f"Hash copied: `{st.session_state['copied_hash']}`")

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
#  SCAN JS
# ─────────────────────────────────────────────

with tab_js:
    st.subheader("Scan: find structurally duplicate JavaScript functions")
    st.write("Upload `.js` or `.ts` files — SIR finds functions that are logically identical even if named differently.")

    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        from sir_js import hash_js_source, extract_js_functions
        js_available = True
    except ImportError:
        js_available = False

    if not js_available:
        st.error("sir_js.py not found. Make sure it is in the same folder as sir_ui.py.")
    else:
        js_uploaded = st.file_uploader("Upload JavaScript/TypeScript files", type=["js", "ts", "jsx", "tsx"], accept_multiple_files=True, key="js_upload")
        js_min_cluster = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="js_min")

        if st.button("Run JS scan", type="primary"):
            if not js_uploaded:
                st.warning("Please upload at least one .js or .ts file.")
            else:
                from collections import defaultdict
                js_groups = defaultdict(list)
                js_func_code = {}
                total_js_funcs = 0
                progress = st.progress(0, text="Starting JS scan...")
                status = st.empty()

                for i, f in enumerate(js_uploaded):
                    status.text(f"Scanning {f.name}...")
                    src = f.read().decode("utf-8", errors="replace")
                    funcs = extract_js_functions(src, f.name)
                    for name, lineno, params, body_src in funcs:
                        total_js_funcs += 1
                        from sir_js import tokenize, canonicalize_js
                        body_tokens = tokenize(body_src)
                        sir = canonicalize_js(params, body_tokens)
                        h = sir["sir_sha256"]
                        js_groups[h].append({"file": f.name, "name": name, "lineno": lineno, "body": body_src})
                        js_func_code[f"{f.name}::{name}"] = body_src
                    progress.progress((i + 1) / len(js_uploaded), text=f"Scanned {i+1}/{len(js_uploaded)} files")

                progress.progress(1.0, text="Scan complete!")
                status.empty()

                js_dupes = {h: v for h, v in js_groups.items() if len(v) >= int(js_min_cluster)}

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Files", len(js_uploaded))
                c2.metric("Functions", total_js_funcs)
                c3.metric("Unique structures", len(js_groups))
                c4.metric(f"Duplicate clusters (≥{js_min_cluster})", len(js_dupes))
                st.divider()

                if not js_dupes:
                    st.success("✅ No duplicate JavaScript function structures found!")
                else:
                    st.error(f"⚠️ Found {len(js_dupes)} duplicate cluster(s)")
                    for h, occs in sorted(js_dupes.items(), key=lambda x: -len(x[1])):
                        with st.expander(f"🔴 {len(occs)} duplicates — hash: `{h[:16]}...`", expanded=True):
                            st.caption(f"Full hash: `{h}`")
                            for o in occs:
                                st.markdown(f"**`{o['name']}`** in `{o['file']}` (line {o['lineno']})")
                                st.code(o["body"], language="javascript")



# ─────────────────────────────────────────────
#  CROSS-LANGUAGE SCAN
# ─────────────────────────────────────────────

with tab_cross:
    st.subheader("Cross-Language Scan: find duplicate logic across Python, JavaScript and TypeScript")
    st.write("Upload any mix of `.py`, `.js`, `.ts`, `.jsx`, or `.tsx` files. SIR finds functions that are structurally identical across languages — a Python function and a JavaScript function that do the same thing will match.")

    try:
        from sir_universal import hash_file_universal
        universal_available = True
    except ImportError:
        universal_available = False

    if not universal_available:
        st.error("sir_universal.py not found. Make sure it is in the same folder as sir_ui.py.")
    else:
        cross_uploaded = st.file_uploader(
            "Upload Python, JavaScript, or TypeScript files",
            type=["py", "js", "ts", "jsx", "tsx"],
            accept_multiple_files=True,
            key="cross_upload"
        )
        cross_min = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="cross_min")

        if st.button("Run cross-language scan", type="primary"):
            if not cross_uploaded:
                st.warning("Please upload at least one file.")
            else:
                cross_groups = defaultdict(list)
                total_cross_funcs = 0
                progress = st.progress(0, text="Starting cross-language scan...")
                status = st.empty()

                for i, f in enumerate(cross_uploaded):
                    status.text(f"Scanning {f.name}...")
                    src = f.read().decode("utf-8", errors="replace")
                    try:
                        results = hash_file_universal(src, f.name)
                        for name, lineno, h in results:
                            total_cross_funcs += 1
                            lang = "Python" if f.name.endswith(".py") else "TypeScript" if f.name.endswith((".ts", ".tsx")) else "JavaScript"
                            cross_groups[h].append({
                                "file": f.name,
                                "name": name,
                                "lineno": lineno,
                                "lang": lang
                            })
                    except Exception as e:
                        st.warning(f"Could not scan {f.name}: {e}")
                    progress.progress((i + 1) / len(cross_uploaded), text=f"Scanned {i+1}/{len(cross_uploaded)} files")

                progress.progress(1.0, text="Scan complete!")
                status.empty()

                cross_dupes = {h: v for h, v in cross_groups.items() if len(v) >= int(cross_min)}
                cross_lang_dupes = {h: v for h, v in cross_dupes.items()
                                    if len(set(o["lang"] for o in v)) > 1}

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Files", len(cross_uploaded))
                c2.metric("Functions", total_cross_funcs)
                c3.metric("Duplicate clusters", len(cross_dupes))
                c4.metric("Cross-language matches", len(cross_lang_dupes))
                st.divider()

                if not cross_dupes:
                    st.success("No duplicate function structures found!")
                else:
                    if cross_lang_dupes:
                        st.error(f"Found {len(cross_lang_dupes)} cross-language duplicate(s) — same logic in multiple languages!")
                    
                    for h, occs in sorted(cross_dupes.items(), key=lambda x: -len(x[1])):
                        langs = set(o["lang"] for o in occs)
                        is_cross = len(langs) > 1
                        icon = "🌐" if is_cross else "🔴"
                        label = f"{icon} {len(occs)} duplicates across {', '.join(sorted(langs))} — hash: {h[:16]}..."
                        with st.expander(label, expanded=is_cross):
                            if is_cross:
                                st.info("This logic exists in multiple languages — potential for code consolidation or API alignment.")
                            for o in occs:
                                lang_badge = {"Python": "🐍", "JavaScript": "🟨", "TypeScript": "🔷"}.get(o["lang"], "📄")
                                st.markdown(f"{lang_badge} **{o['lang']}** — `{o['name']}` in `{o['file']}` (line {o['lineno']})")

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
            total_orig_bytes = 0
            progress = st.progress(0, text="Starting pack...")
            status = st.empty()
            for i, f in enumerate(pack_uploaded):
                status.text(f"Encoding {f.name}...")
                src = f.read().decode("utf-8", errors="replace")
                total_orig_bytes += len(src.encode("utf-8"))
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
                progress.progress((i + 1) / len(pack_uploaded), text=f"Packed {i+1}/{len(pack_uploaded)} files")
            progress.progress(1.0, text="Pack complete!")
            status.empty()

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
            bundle_bytes = len(bundle_json.encode("utf-8"))

            # Dedup savings
            total_roots = len(roots)
            unique_roots = len({r["root"] for r in roots})
            deduped = total_roots - unique_roots
            size_reduction = 100 * (1 - bundle_bytes / max(total_orig_bytes, 1))

            orig_kb = total_orig_bytes / 1024
            bundle_kb = bundle_bytes / 1024
            ratio = bundle_kb / max(orig_kb, 0.001)

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Files packed", meta["files_scanned"])
            c2.metric("Functions", meta["total_functions"])
            c3.metric("Unique nodes", meta["unique_nodes"])
            c4.metric("Duplicate structures removed", deduped)
            c5.metric("Bundle size", f"{bundle_kb:.1f} KB", delta=f"{bundle_kb - orig_kb:.1f} KB vs source")

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
    st.subheader("Unpack: restore files from a SIR bundle")

    unpack_mode = st.radio(
        "Restore mode",
        ["🔧 Full file restore (fix duplicates, get original .py files back)",
         "🔬 Individual functions (one .py per function)"],
        key="unpack_mode"
    )

    bundle_file = st.file_uploader("Upload bundle.json", type=["json"], key="unpack_upload")
    rehydrate = st.checkbox("Restore original variable names (rehydrate)", value=True)

    if st.button("Unpack", type="primary"):
        if not bundle_file:
            st.warning("Please upload a bundle.json file.")
        else:
            try:
                bundle = json.loads(bundle_file.read().decode("utf-8"))
                nodes = bundle["nodes"]
                roots = bundle["roots"]
                namemaps = bundle.get("namemaps", {})
                meta = bundle.get("meta", {})

                st.write("**Pack info:**", meta)

                if "Full file restore" in unpack_mode:
                    st.info("Restoring full files with duplicates removed and calls updated...")

                    by_file: Dict[str, List[Dict]] = defaultdict(list)
                    for r in roots:
                        by_file[r["file"]].append(r)

                    hash_to_roots: Dict[str, List[Dict]] = defaultdict(list)
                    for r in roots:
                        if r.get("sir_sha256"):
                            hash_to_roots[r["sir_sha256"]].append(r)
                    dupes = {h: rs for h, rs in hash_to_roots.items() if len(rs) >= 2}

                    canonical_map: Dict[str, str] = {}
                    canonical_funcs: Dict[str, str] = {}
                    for h, rs in dupes.items():
                        canonical = rs[0]["qualname"]
                        for r in rs:
                            canonical_map[r["qualname"]] = canonical
                        root_id = rs[0]["root"]
                        nm = namemaps.get(root_id, {}) if rehydrate else {}
                        try:
                            code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=rehydrate)
                            canonical_funcs[canonical] = code
                        except Exception:
                            pass

                    progress = st.progress(0, text="Starting restore...")
                    status = st.empty()
                    file_list = list(by_file.items())
                    reconstructed: Dict[str, str] = {}

                    for i, (fname, file_roots) in enumerate(file_list):
                        status.text(f"Restoring {fname}...")
                        lines = []
                        all_canonicals = set()
                        for r in file_roots:
                            q = r["qualname"]
                            if q in canonical_map:
                                all_canonicals.add(canonical_map[q])
                            elif q in canonical_funcs:
                                all_canonicals.add(q)
                        if all_canonicals:
                            lines.append(f"from utils import {', '.join(sorted(all_canonicals))}")
                            lines.append("")

                        for r in sorted(file_roots, key=lambda x: x.get("lineno", 0)):
                            qualname = r["qualname"]
                            if qualname in canonical_map and canonical_map[qualname] != qualname:
                                continue
                            if qualname in canonical_funcs:
                                continue
                            root_id = r["root"]
                            nm = namemaps.get(root_id, {}) if rehydrate else {}
                            try:
                                code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=rehydrate)
                                lines.append(code)
                                lines.append("")
                            except Exception:
                                pass

                        reconstructed[fname] = "\n".join(lines)
                        progress.progress((i + 1) / len(file_list), text=f"Restored {i+1}/{len(file_list)} files")

                    progress.progress(1.0, text="Building utils.py...")
                    utils_lines = ['"""utils.py — Canonical functions deduplicated by SIR Engine."""', ""]
                    for cname, csrc in canonical_funcs.items():
                        utils_lines.append(csrc)
                        utils_lines.append("")
                    reconstructed["utils.py"] = "\n".join(utils_lines)
                    status.empty()

                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fname, src in reconstructed.items():
                            zf.writestr(fname, src)
                    zip_buffer.seek(0)

                    c1, c2, c3 = st.columns(3)
                    c1.metric("Files restored", len(reconstructed) - 1)
                    c2.metric("Duplicate clusters removed", len(dupes))
                    c3.metric("Functions in utils.py", len(canonical_funcs))

                    st.success("✅ Full restore complete! Duplicates removed, utils.py created.")
                    st.download_button(
                        "📥 Download restored codebase (.zip)",
                        data=zip_buffer,
                        file_name="restored_codebase.zip",
                        mime="application/zip",
                    )

                else:
                    progress = st.progress(0, text="Starting unpack...")
                    status = st.empty()
                    zip_buffer = io.BytesIO()
                    restored = []
                    errors = 0

                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        for i, r in enumerate(roots):
                            status.text(f"Restoring function {i+1}/{len(roots)}: {r['qualname']}...")
                            root_id = r["root"]
                            nm = namemaps.get(root_id, {}) if rehydrate else {}
                            try:
                                code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=rehydrate)
                                fname = occurrence_filename(r, i)
                                zf.writestr(fname, code + "\n")
                                restored.append({"file": fname, "qualname": r["qualname"], "original_file": r["file"]})
                            except Exception:
                                errors += 1
                            progress.progress((i + 1) / len(roots), text=f"Restored {i+1}/{len(roots)} functions")

                    progress.progress(1.0, text="Done!")
                    status.empty()
                    zip_buffer.seek(0)

                    c1, c2 = st.columns(2)
                    c1.metric("Functions restored", len(restored))
                    c2.metric("Errors", errors)

                    st.success(f"✅ Restored {len(restored)} function(s)!")
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
            progress = st.progress(0, text="Verifying...")
            bundle = json.loads(verify_bundle.read().decode("utf-8"))
            roots = bundle["roots"]
            expected = {r["sir_sha256"] for r in roots if r.get("sir_sha256")}
            actual = set()
            for i, f in enumerate(verify_files):
                src = f.read().decode("utf-8", errors="replace")
                try:
                    h = hash_source(src, mode="semantic")
                    actual.add(h)
                except Exception:
                    pass
                progress.progress((i + 1) / len(verify_files), text=f"Verified {i+1}/{len(verify_files)} files")
            progress.progress(1.0, text="Done!")

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

            progress = st.progress(0, text="Diffing...")
            ha = hash_uploaded(files_a)
            progress.progress(0.5, text="Hashing Set B...")
            hb = hash_uploaded(files_b)
            progress.progress(1.0, text="Done!")

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
#  MERGE
# ─────────────────────────────────────────────

with tab_merge:
    st.subheader("Merge: eliminate duplicate functions from your codebase")
    st.write(
        "Upload your `.py` files. SIR finds duplicate function clusters, "
        "you pick which name to keep for each cluster, and it produces a cleaned-up "
        "zip with all duplicates removed, calls renamed, and shared functions moved to `utils.py`."
    )

    merge_uploaded = st.file_uploader(
        "Upload Python files", type=["py"], accept_multiple_files=True, key="merge_upload"
    )
    merge_methods = st.checkbox("Include class methods", value=False, key="merge_methods")

    if st.button("Scan for duplicates", type="primary", key="merge_scan"):
        if not merge_uploaded:
            st.warning("Please upload at least one .py file.")
        else:
            file_sources: Dict[str, str] = {}
            for f in merge_uploaded:
                file_sources[f.name] = f.read().decode("utf-8", errors="replace")

            groups: Dict[str, List[Occur]] = defaultdict(list)
            total_funcs = 0
            progress = st.progress(0, text="Scanning for duplicates...")
            status = st.empty()
            file_list = list(file_sources.items())
            for i, (fname, src) in enumerate(file_list):
                status.text(f"Scanning {fname}...")
                for qualname, lineno, code in extract_functions(src, fname, merge_methods):
                    total_funcs += 1
                    try:
                        h = hash_source(code, mode="semantic")
                        groups[h].append(Occur(file=fname, qualname=qualname, lineno=lineno, semantic_hash=h))
                    except Exception:
                        pass
                progress.progress((i + 1) / len(file_list), text=f"Scanned {i+1}/{len(file_list)} files")
            progress.progress(1.0, text="Scan complete!")
            status.empty()

            dupes = {h: occ for h, occ in groups.items() if len(occ) >= 2}

            # Build func code map for syntax highlighting
            merge_func_map = {}
            for fname, src in file_sources.items():
                for qualname, lineno, code in extract_functions(src, fname, merge_methods):
                    merge_func_map[f"{fname}::{qualname}"] = code

            st.session_state["merge_file_sources"] = file_sources
            st.session_state["merge_dupes"] = {h: [vars(o) for o in occ] for h, occ in dupes.items()}
            st.session_state["merge_total"] = total_funcs
            st.session_state["merge_func_map"] = merge_func_map

    if "merge_dupes" in st.session_state and st.session_state["merge_dupes"]:
        dupes_data = st.session_state["merge_dupes"]
        file_sources = st.session_state["merge_file_sources"]

        st.divider()
        st.success(f"Found **{len(dupes_data)}** duplicate cluster(s) across {len(file_sources)} file(s).")
        st.write("For each cluster, pick which function name to keep as the canonical version:")

        for h, occs in dupes_data.items():
            names = [o["qualname"] for o in occs]
            with st.expander(f"Cluster `{h[:16]}...` — {len(occs)} duplicates: {', '.join(f'`{n}`' for n in names)}", expanded=True):
                cols = st.columns([3, 1])
                with cols[0]:
                    st.selectbox("Canonical name (keep this one)", options=names, key=f"canon_{h}")
                with cols[1]:
                    st.markdown("<br>", unsafe_allow_html=True)
                    st.markdown(f"**{len(occs) - 1}** duplicate(s) will be removed")
                # Show syntax highlighted code for each occurrence
                merge_func_map = st.session_state.get("merge_func_map", {})
                for o in occs:
                    src = file_sources.get(o["file"], "")
                    code = get_function_source(src, o["qualname"]) or merge_func_map.get(f"{o['file']}::{o['qualname']}", "")
                    if code:
                        st.markdown(f"**`{o['qualname']}`** in `{o['file']}` (line {o['lineno']})")
                        st.code(code, language="python")

        st.divider()

        if st.button("🔀 Apply merge and download", type="primary"):
            modified_sources: Dict[str, str] = {k: v for k, v in file_sources.items()}
            utils_functions: Dict[str, str] = {}
            canonical_names_used: Dict[str, str] = {}
            progress = st.progress(0, text="Applying merge...")
            status = st.empty()
            dupe_list = list(dupes_data.items())

            for i, (h, occs) in enumerate(dupe_list):
                canonical_name = st.session_state.get(f"canon_{h}", occs[0]["qualname"])
                canonical_names_used[h] = canonical_name
                status.text(f"Merging cluster {i+1}/{len(dupe_list)}: keeping '{canonical_name}'...")

                canonical_occ = next((o for o in occs if o["qualname"] == canonical_name), occs[0])
                canonical_file = canonical_occ["file"]
                canonical_src = get_function_source(modified_sources[canonical_file], canonical_name)

                if canonical_src:
                    utils_functions[canonical_name] = canonical_src

                for occ in occs:
                    fname = occ["file"]
                    func_name = occ["qualname"]
                    src = modified_sources[fname]
                    if func_name != canonical_name:
                        src = rename_calls(src, func_name, canonical_name)
                    try:
                        tree = ast.parse(src)
                        tree = remove_function_node(tree, func_name)
                        ast.fix_missing_locations(tree)
                        src = ast.unparse(tree)
                    except Exception:
                        pass
                    src = add_import(src, canonical_name)
                    modified_sources[fname] = src

                try:
                    src = modified_sources[canonical_file]
                    tree = ast.parse(src)
                    tree = remove_function_node(tree, canonical_name)
                    ast.fix_missing_locations(tree)
                    modified_sources[canonical_file] = ast.unparse(tree)
                except Exception:
                    pass

                progress.progress((i + 1) / len(dupe_list), text=f"Merged {i+1}/{len(dupe_list)} clusters")

            progress.progress(1.0, text="Building zip and report...")
            status.empty()

            utils_src = '"""utils.py — Canonical functions extracted by SIR Engine merge."""\n\n'
            for fname, fsrc in utils_functions.items():
                utils_src += fsrc + "\n\n"

            # Build HTML report
            report_html = build_merge_report(
                file_sources, modified_sources, dupes_data, canonical_names_used, utils_functions
            )

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname, src in modified_sources.items():
                    zf.writestr(fname, src)
                zf.writestr("utils.py", utils_src)
                zf.writestr("sir_merge_report.html", report_html)
            zip_buffer.seek(0)

            total_removed = sum(len(occs) - 1 for occs in dupes_data.values())
            orig_lines = sum(count_lines(s) for s in file_sources.values())
            new_lines = sum(count_lines(s) for s in modified_sources.values())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Files processed", len(file_sources))
            c2.metric("Duplicates removed", total_removed)
            c3.metric("Functions in utils.py", len(utils_functions))
            c4.metric("Lines saved", orig_lines - new_lines)

            st.success(f"✅ Merged! A full HTML report is included in the zip as `sir_merge_report.html`.")
            st.download_button(
                "📥 Download merged codebase (.zip)",
                data=zip_buffer,
                file_name="merged_codebase.zip",
                mime="application/zip",
            )

    elif "merge_dupes" in st.session_state and not st.session_state["merge_dupes"]:
        st.success("✅ No duplicate functions found — your codebase is already clean!")


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
| **Scan** | Upload `.py` files → find structurally duplicate functions with syntax highlighting |
| **Pack** | Upload `.py` files → compress into a `bundle.json` with size stats |
| **Unpack** | Upload `bundle.json` → restore full files or individual functions |
| **Verify** | Upload `bundle.json` + restored files → confirm hashes match |
| **Diff** | Upload two sets of files → compare their logical structures |
| **Merge** | Upload `.py` files → remove duplicates, get HTML report + clean zip |

---

### How it works

1. Your Python file is parsed into an AST (Abstract Syntax Tree)
2. All identifiers are alpha-renamed to canonical placeholders (`v0`, `v1`, `f0`...)
3. The resulting structure is hashed with SHA-256
4. Identical hashes = identical logic, regardless of naming

---

### Built by
Lucas Flinders — [GitHub](https://github.com/lflin00/SRI-ENGINE)

---

### VS Code Extension

Install the SIR Engine extension to scan for duplicates directly in your editor — auto-scans on every save.
    """)

st.link_button(
    "📥 Download VS Code Extension (.vsix)",
    "https://github.com/lflin00/SRI-ENGINE/raw/main/sir-engine-0.0.1.vsix"
)
st.caption("After downloading: open VS Code → Cmd+Shift+P → 'Install from VSIX' → select the file.")
