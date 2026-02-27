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
#  Environment detection
# ─────────────────────────────────────────────

def is_running_locally() -> bool:
    """Detect if running on local machine vs Streamlit Cloud."""
    import os
    # Streamlit Cloud sets IS_CLOUD or STREAMLIT_SHARING_MODE
    if os.environ.get("STREAMLIT_SHARING_MODE") or os.environ.get("IS_CLOUD"):
        return False
    # Check if localhost
    try:
        import socket
        hostname = socket.gethostname()
        if "streamlit" in hostname.lower() or "cloud" in hostname.lower():
            return False
    except Exception:
        pass
    return True

IS_LOCAL = is_running_locally()


def get_ai_config():
    """Get AI backend config from session state."""
    return {
        "backend": st.session_state.get("ai_backend", "ollama" if IS_LOCAL else "anthropic"),
        "api_key": st.session_state.get("ai_api_key", ""),
        "ollama_model": st.session_state.get("ollama_model", "codellama:7b"),
        "ollama_host": st.session_state.get("ollama_host", "http://localhost:11434"),
    }


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

with st.sidebar:
    st.markdown("### AI Translation Engine")
    if IS_LOCAL:
        st.success("Running locally")
        try:
            from sir_ai_translate import check_ollama, get_ollama_models
            if check_ollama():
                models = get_ollama_models()
                st.success("Ollama detected")
                if models:
                    st.selectbox("Model", models, key="ollama_model")
                else:
                    st.text_input("Model", value="codellama:7b", key="ollama_model")
                    st.caption("Run: ollama pull codellama:7b")
                st.text_input("Host", value="http://localhost:11434", key="ollama_host")
                st.session_state["ai_backend"] = "ollama"
                st.caption("AI translation is free via Ollama.")
            else:
                st.warning("Ollama not running")
                st.caption("Install: ollama.ai then: ollama pull codellama:7b")
                key = st.text_input("Anthropic API key", type="password", key="ai_api_key")
                st.session_state["ai_backend"] = "anthropic" if key else "none"
        except ImportError:
            st.warning("sir_ai_translate.py not found")
    else:
        st.info("Running on Streamlit Cloud")
        key = st.text_input("Anthropic API key", type="password", key="ai_api_key")
        if key:
            st.session_state["ai_backend"] = "anthropic"
            st.success("API key set")
        else:
            st.session_state["ai_backend"] = "none"
            st.caption("Native Python/JS/TS works without a key.")
    st.divider()
    st.markdown("**Run locally for free AI:**")
    st.code("git clone https://github.com/lflin00/SRI-ENGINE\ncd SRI-ENGINE/SIR_MAIN\npip install streamlit\nollama pull codellama:7b\nstreamlit run sir_ui.py", language="bash")
    st.markdown("[GitHub](https://github.com/lflin00/SRI-ENGINE)")

st.title("SIR Engine")
st.caption("Semantic duplicate detection for Python, JavaScript, TypeScript, and 25+ languages via AI translation.")

tab_scan, tab_github, tab_pack, tab_unpack, tab_verify, tab_diff, tab_merge, tab_about = st.tabs([
    "Scan", "GitHub Scanner", "Pack", "Unpack", "Verify", "Diff", "Merge", "About"
])


# ─────────────────────────────────────────────
#  SCAN
# ─────────────────────────────────────────────

with tab_scan:
    st.subheader("Scan: find structurally duplicate functions")

    scan_lang = st.selectbox(
        "Language",
        ["Python (.py)", "JavaScript (.js / .jsx)", "TypeScript (.ts / .tsx)", "Cross-Language (all)", "Any Language (AI-powered 🤖)"],
        key="scan_lang"
    )

    # ── PYTHON SCAN ──────────────────────────────
    if scan_lang == "Python (.py)":
        st.write("Upload `.py` files — SIR finds functions that are logically identical even if they have different names or variable names.")
        uploaded = st.file_uploader("Upload Python files", type=["py"], accept_multiple_files=True, key="scan_upload")
        col1, col2 = st.columns(2)
        with col1:
            include_methods = st.checkbox("Include class methods", value=False)
        with col2:
            min_cluster = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1)
        sir_ignore_input = st.text_area(
            ".sir_ignore — function names to skip (one per line)",
            placeholder="calculate_total\nadd_values\n# Use # for comments",
            key="sir_ignore_py", height=80
        )
        ignored_funcs = set()
        if sir_ignore_input.strip():
            ignored_funcs = {l.strip() for l in sir_ignore_input.splitlines()
                             if l.strip() and not l.strip().startswith("#")}
        if ignored_funcs:
            st.caption(f"Ignoring {len(ignored_funcs)} function(s): {', '.join(sorted(ignored_funcs))}")

        if st.button("Run scan", type="primary"):
            if not uploaded:
                st.warning("Please upload at least one .py file.")
            else:
                scan_sources: Dict[str, str] = {}
                groups: Dict[str, List[Occur]] = defaultdict(list)
                func_code_map: Dict[str, str] = {}
                total_funcs = 0
                progress = st.progress(0, text="Starting scan...")
                status = st.empty()
                for i, f in enumerate(uploaded):
                    status.text(f"Scanning {f.name}...")
                    src = f.read().decode("utf-8", errors="replace")
                    scan_sources[f.name] = src
                    for qualname, lineno, code in extract_functions(src, f.name, include_methods):
                        # Check sir_ignore
                        short_name = qualname.split(".")[-1]
                        if short_name in ignored_funcs or qualname in ignored_funcs:
                            continue
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
                health = int(100 * len(groups) / max(total_funcs, 1))

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Files", len(uploaded))
                c2.metric("Functions", total_funcs)
                c3.metric("Unique structures", len(groups))
                c4.metric(f"Duplicate clusters (≥{min_cluster})", len(dupes))
                c5.metric("Health score", f"{health}/100", help="100 = no duplicates. Lower = more redundant logic.")
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
                    "health_score": health,
                    "duplicate_clusters": [
                        {"semantic_hash": h, "count": len(occs),
                         "occurrences": [{"file": o.file, "qualname": o.qualname, "lineno": o.lineno} for o in occs]}
                        for h, occs in sorted(dupes.items(), key=lambda x: (-len(x[1]), x[0]))
                    ],
                }
                st.download_button("📥 Download report (JSON)", data=json.dumps(report, indent=2),
                                   file_name="sir_report.json", mime="application/json")

    # ── JAVASCRIPT SCAN ───────────────────────────
    elif scan_lang == "JavaScript (.js / .jsx)":
        st.write("Upload `.js` or `.jsx` files — SIR finds structurally duplicate functions.")
        try:
            from sir_js import hash_js_source, extract_js_functions, tokenize as js_tokenize, canonicalize_js
            js_available = True
        except ImportError:
            js_available = False

        if not js_available:
            st.error("sir_js.py not found.")
        else:
            js_uploaded = st.file_uploader("Upload JavaScript files", type=["js", "jsx"], accept_multiple_files=True, key="scan_js_upload")
            js_min = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="scan_js_min")

            if st.button("Run JS scan", type="primary"):
                if not js_uploaded:
                    st.warning("Please upload at least one .js file.")
                else:
                    js_groups = defaultdict(list)
                    total_js = 0
                    progress = st.progress(0, text="Scanning...")
                    status = st.empty()
                    for i, f in enumerate(js_uploaded):
                        status.text(f"Scanning {f.name}...")
                        src = f.read().decode("utf-8", errors="replace")
                        funcs = extract_js_functions(src, f.name)
                        for name, lineno, params, body_src in funcs:
                            total_js += 1
                            body_tokens = js_tokenize(body_src)
                            sir = canonicalize_js(params, body_tokens)
                            js_groups[sir["sir_sha256"]].append({"file": f.name, "name": name, "lineno": lineno, "body": body_src})
                        progress.progress((i + 1) / len(js_uploaded), text=f"Scanned {i+1}/{len(js_uploaded)} files")
                    progress.progress(1.0, text="Done!")
                    status.empty()

                    js_dupes = {h: v for h, v in js_groups.items() if len(v) >= int(js_min)}
                    health = int(100 * len(js_groups) / max(total_js, 1))

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Files", len(js_uploaded))
                    c2.metric("Functions", total_js)
                    c3.metric("Unique structures", len(js_groups))
                    c4.metric("Duplicate clusters", len(js_dupes))
                    c5.metric("Health score", f"{health}/100")
                    st.divider()

                    if not js_dupes:
                        st.success("No duplicate JavaScript functions found!")
                    else:
                        st.error(f"Found {len(js_dupes)} duplicate cluster(s)")
                        for h, occs in sorted(js_dupes.items(), key=lambda x: -len(x[1])):
                            with st.expander(f"🔴 {len(occs)} duplicates — hash: {h[:16]}...", expanded=True):
                                for o in occs:
                                    st.markdown(f"**`{o['name']}`** in `{o['file']}` (line {o['lineno']})")
                                    st.code(o["body"], language="javascript")

    # ── TYPESCRIPT SCAN ───────────────────────────
    elif scan_lang == "TypeScript (.ts / .tsx)":
        st.write("Upload `.ts` or `.tsx` files — SIR strips type annotations and finds structurally duplicate functions.")
        try:
            from sir_js import extract_js_functions, tokenize as js_tokenize, canonicalize_js
            ts_available = True
        except ImportError:
            ts_available = False

        if not ts_available:
            st.error("sir_js.py not found.")
        else:
            ts_uploaded = st.file_uploader("Upload TypeScript files", type=["ts", "tsx"], accept_multiple_files=True, key="scan_ts_upload")
            ts_min = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="scan_ts_min")

            if st.button("Run TS scan", type="primary"):
                if not ts_uploaded:
                    st.warning("Please upload at least one .ts file.")
                else:
                    ts_groups = defaultdict(list)
                    total_ts = 0
                    progress = st.progress(0, text="Scanning...")
                    status = st.empty()
                    for i, f in enumerate(ts_uploaded):
                        status.text(f"Scanning {f.name}...")
                        src = f.read().decode("utf-8", errors="replace")
                        funcs = extract_js_functions(src, f.name)
                        for name, lineno, params, body_src in funcs:
                            total_ts += 1
                            body_tokens = js_tokenize(body_src)
                            sir = canonicalize_js(params, body_tokens)
                            ts_groups[sir["sir_sha256"]].append({"file": f.name, "name": name, "lineno": lineno, "body": body_src})
                        progress.progress((i + 1) / len(ts_uploaded), text=f"Scanned {i+1}/{len(ts_uploaded)} files")
                    progress.progress(1.0, text="Done!")
                    status.empty()

                    ts_dupes = {h: v for h, v in ts_groups.items() if len(v) >= int(ts_min)}
                    health = int(100 * len(ts_groups) / max(total_ts, 1))

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Files", len(ts_uploaded))
                    c2.metric("Functions", total_ts)
                    c3.metric("Unique structures", len(ts_groups))
                    c4.metric("Duplicate clusters", len(ts_dupes))
                    c5.metric("Health score", f"{health}/100")
                    st.divider()

                    if not ts_dupes:
                        st.success("No duplicate TypeScript functions found!")
                    else:
                        st.error(f"Found {len(ts_dupes)} duplicate cluster(s)")
                        for h, occs in sorted(ts_dupes.items(), key=lambda x: -len(x[1])):
                            with st.expander(f"🔴 {len(occs)} duplicates — hash: {h[:16]}...", expanded=True):
                                for o in occs:
                                    st.markdown(f"**`{o['name']}`** in `{o['file']}` (line {o['lineno']})")
                                    st.code(o["body"], language="typescript")

    # ── AI UNIVERSAL SCAN ────────────────────────
    elif scan_lang == "Any Language (AI-powered 🤖)":
        st.write("Upload code files in **any language** — C, C++, Java, Rust, Go, Ruby, Swift, Kotlin, and more. SIR uses AI to translate each function to Python, then runs the full structural analysis.")

        st.info("**How it works:** Claude translates each function to equivalent Python, preserving logical structure. The result is hashed through the same SIR pipeline as native Python. This means you can compare a Java function against a Python function against a C++ function.")

        st.warning("**Limitations (AI translation layer):** Results are highly reliable but not mathematically guaranteed like the native Python/JS pipelines. Very complex language-specific features may be simplified. Best for pure logic functions.")

        ai_uploaded = st.file_uploader(
            "Upload code files (any language)",
            type=["c", "cpp", "cc", "cxx", "h", "hpp", "java", "rs", "go", "rb",
                  "php", "swift", "kt", "scala", "cs", "lua", "dart", "hs", "ex",
                  "ml", "fs", "jl", "nim", "zig", "r", "pl", "py", "js", "ts"],
            accept_multiple_files=True,
            key="ai_scan_upload"
        )
        ai_min = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="ai_min")

        if st.button("Run AI scan", type="primary"):
            if not ai_uploaded:
                st.warning("Please upload at least one file.")
            else:
                try:
                    from sir_ai_translate import detect_language, is_ai_language, translate_to_python, extract_raw_functions
                    from sir.core import hash_source as _hash_source
                    _ai_cfg = get_ai_config()
                    if _ai_cfg["backend"] == "none":
                        st.error("No AI backend configured. Set up Ollama or add an Anthropic API key in the sidebar.")
                        st.stop()

                    ai_groups = defaultdict(list)
                    total_ai = 0
                    errors_ai = 0
                    translations = []
                    progress = st.progress(0, text="Starting AI translation scan...")
                    status = st.empty()

                    for i, f in enumerate(ai_uploaded):
                        status.text(f"Translating {f.name}...")
                        src = f.read().decode("utf-8", errors="replace")
                        ext = f.name.rsplit(".", 1)[-1].lower()

                        # Route to native pipelines for Python/JS/TS
                        if ext == "py":
                            for qualname, lineno, code in extract_functions(src, f.name, False):
                                total_ai += 1
                                try:
                                    h = hash_source(code, mode="semantic")
                                    ai_groups[h].append({"file": f.name, "name": qualname,
                                                         "lineno": lineno, "lang": "Python",
                                                         "code": code, "translated": False})
                                except Exception:
                                    errors_ai += 1
                        elif ext in ("js", "jsx", "ts", "tsx"):
                            from sir_js import extract_js_functions, tokenize as _jt, canonicalize_js as _cj
                            for name, lineno, params, body_src in extract_js_functions(src, f.name):
                                total_ai += 1
                                try:
                                    sir = _cj(params, _jt(body_src))
                                    ai_groups[sir["sir_sha256"]].append({"file": f.name, "name": name,
                                                                         "lineno": lineno, "lang": "JS/TS",
                                                                         "code": body_src, "translated": False})
                                except Exception:
                                    errors_ai += 1
                        else:
                            # AI translation path
                            lang = detect_language(f.name) or ext.upper()
                            try:
                                from sir_ai_translate import extract_raw_functions
                                raw_funcs = extract_raw_functions(src, lang)
                                for name, lineno, raw_src in raw_funcs:
                                    total_ai += 1
                                    try:
                                        py_src = translate_to_python(
                                            raw_src, lang,
                                            backend=_ai_cfg["backend"],
                                            api_key=_ai_cfg["api_key"],
                                            ollama_model=_ai_cfg["ollama_model"],
                                            ollama_host=_ai_cfg["ollama_host"]
                                        )
                                        if py_src.strip():
                                            h = _hash_source(py_src, mode="semantic")
                                            ai_groups[h].append({"file": f.name, "name": name,
                                                                  "lineno": lineno, "lang": lang,
                                                                  "code": raw_src, "translated": True,
                                                                  "python_src": py_src})
                                    except Exception:
                                        errors_ai += 1
                            except Exception:
                                errors_ai += 1

                        progress.progress((i+1)/len(ai_uploaded), text=f"Processed {i+1}/{len(ai_uploaded)} files")

                    progress.progress(1.0, text="Done!")
                    status.empty()

                    ai_dupes = {h: v for h, v in ai_groups.items() if len(v) >= int(ai_min)}
                    cross_lang = {h: v for h, v in ai_dupes.items() if len(set(o["lang"] for o in v)) > 1}
                    health = int(100 * len(ai_groups) / max(total_ai, 1))

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Files", len(ai_uploaded))
                    c2.metric("Functions", total_ai)
                    c3.metric("Duplicate clusters", len(ai_dupes))
                    c4.metric("Cross-language", len(cross_lang))
                    c5.metric("Health score", f"{health}/100")
                    if errors_ai:
                        st.caption(f"{errors_ai} function(s) could not be translated or hashed.")
                    st.divider()

                    if not ai_dupes:
                        st.success("No structural duplicates found across any language!")
                    else:
                        if cross_lang:
                            st.error(f"Found {len(cross_lang)} cross-language duplicate(s)!")
                        for h, occs in sorted(ai_dupes.items(), key=lambda x: -len(x[1])):
                            langs = set(o["lang"] for o in occs)
                            is_cross = len(langs) > 1
                            icon = "🌐" if is_cross else "🔴"
                            with st.expander(f"{icon} {len(occs)} duplicates across {', '.join(sorted(langs))} — hash: {h[:16]}...", expanded=is_cross):
                                if is_cross:
                                    st.info("Same logic detected across multiple languages via AI translation.")
                                for o in occs:
                                    translated_note = " *(AI translated)*" if o.get("translated") else ""
                                    st.markdown(f"**`{o['name']}`** — `{o['file']}` (line {o['lineno']}) [{o['lang']}]{translated_note}")
                                    lang_key = {"Python": "python", "JS/TS": "javascript"}.get(o["lang"], "c")
                                    st.code(o["code"][:600], language=lang_key)
                                    if o.get("translated") and o.get("python_src"):
                                        with st.expander("Show translated Python"):
                                            st.code(o["python_src"], language="python")

                except Exception as e:
                    st.error(f"AI scan failed: {e}")
                    st.caption("Make sure sir_ai_translate.py is in your SIR_MAIN folder.")

    # ── CROSS-LANGUAGE SCAN ───────────────────────
    elif scan_lang == "Cross-Language (all)":
        st.write("Upload any mix of `.py`, `.js`, `.ts`, `.jsx`, or `.tsx` files. SIR finds functions that are structurally identical across languages.")
        try:
            from sir_universal import hash_file_universal
            universal_available = True
        except ImportError:
            universal_available = False

        if not universal_available:
            st.error("sir_universal.py not found.")
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
                    total_cross = 0
                    progress = st.progress(0, text="Scanning...")
                    status = st.empty()
                    for i, f in enumerate(cross_uploaded):
                        status.text(f"Scanning {f.name}...")
                        src = f.read().decode("utf-8", errors="replace")
                        try:
                            results = hash_file_universal(src, f.name)
                            for name, lineno, h in results:
                                total_cross += 1
                                lang = "Python" if f.name.endswith(".py") else "TypeScript" if f.name.endswith((".ts",".tsx")) else "JavaScript"
                                cross_groups[h].append({"file": f.name, "name": name, "lineno": lineno, "lang": lang})
                        except Exception as e:
                            st.warning(f"Could not scan {f.name}: {e}")
                        progress.progress((i + 1) / len(cross_uploaded), text=f"Scanned {i+1}/{len(cross_uploaded)} files")
                    progress.progress(1.0, text="Done!")
                    status.empty()

                    cross_dupes = {h: v for h, v in cross_groups.items() if len(v) >= int(cross_min)}
                    cross_lang_dupes = {h: v for h, v in cross_dupes.items() if len(set(o["lang"] for o in v)) > 1}
                    health = int(100 * len(cross_groups) / max(total_cross, 1))

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Files", len(cross_uploaded))
                    c2.metric("Functions", total_cross)
                    c3.metric("Duplicate clusters", len(cross_dupes))
                    c4.metric("Cross-language matches", len(cross_lang_dupes))
                    c5.metric("Health score", f"{health}/100")
                    st.divider()

                    if not cross_dupes:
                        st.success("No duplicate function structures found!")
                    else:
                        if cross_lang_dupes:
                            st.error(f"Found {len(cross_lang_dupes)} cross-language duplicate(s)!")
                        for h, occs in sorted(cross_dupes.items(), key=lambda x: -len(x[1])):
                            langs = set(o["lang"] for o in occs)
                            is_cross = len(langs) > 1
                            icon = "🌐" if is_cross else "🔴"
                            with st.expander(f"{icon} {len(occs)} duplicates across {', '.join(sorted(langs))} — hash: {h[:16]}...", expanded=is_cross):
                                if is_cross:
                                    st.info("Same logic exists in multiple languages.")
                                for o in occs:
                                    badge = {"Python": "🐍", "JavaScript": "🟨", "TypeScript": "🔷"}.get(o["lang"], "📄")
                                    st.markdown(f"{badge} **{o['lang']}** — `{o['name']}` in `{o['file']}` (line {o['lineno']})")




# ─────────────────────────────────────────────
#  GITHUB SCANNER
# ─────────────────────────────────────────────

with tab_github:
    st.subheader("GitHub Scanner: scan any public repository for duplicate functions")
    st.write("Paste a public GitHub repo URL — SIR fetches the code and runs a full structural duplicate scan. No download needed.")

    gh_url = st.text_input(
        "GitHub repository URL",
        placeholder="https://github.com/username/repository",
        key="gh_url"
    )
    gh_lang = st.selectbox(
        "Language to scan",
        ["Python (.py)", "JavaScript (.js / .jsx)", "TypeScript (.ts / .tsx)", "All languages", "Any Language (AI-powered 🤖)"],
        key="gh_lang"
    )
    gh_min = st.number_input("Min duplicates to show", min_value=2, max_value=50, value=2, step=1, key="gh_min")
    gh_ignore = st.text_area(
        "sir_ignore — paste function names to ignore (one per line)",
        placeholder="calculate_total\nadd_values\n...",
        key="gh_ignore",
        height=80
    )

    if st.button("Scan GitHub repo", type="primary"):
        if not gh_url.strip():
            st.warning("Please enter a GitHub repository URL.")
        else:
            try:
                import urllib.request as _urllib
                import base64 as _base64

                # Parse URL → owner/repo/branch
                gh_clean = gh_url.strip().rstrip("/")
                gh_clean = gh_clean.replace("https://github.com/", "").replace("http://github.com/", "")
                parts = gh_clean.split("/")
                if len(parts) < 2:
                    st.error("Invalid GitHub URL. Format: https://github.com/username/repository")
                    st.stop()
                owner, repo = parts[0], parts[1]
                branch = parts[3] if len(parts) > 3 and parts[2] == "tree" else "main"

                # Parse ignore list
                ignored_names = set()
                if gh_ignore.strip():
                    ignored_names = {line.strip() for line in gh_ignore.strip().splitlines() if line.strip()}

                # Determine file extensions
                if "Python" in gh_lang:
                    target_exts = (".py",)
                elif "JavaScript" in gh_lang:
                    target_exts = (".js", ".jsx")
                elif "TypeScript" in gh_lang:
                    target_exts = (".ts", ".tsx")
                elif "AI-powered" in gh_lang:
                    target_exts = (".py", ".js", ".jsx", ".ts", ".tsx",
                                   ".c", ".cpp", ".cc", ".java", ".rs", ".go",
                                   ".rb", ".php", ".swift", ".kt", ".cs", ".lua", ".dart")
                else:
                    target_exts = (".py", ".js", ".jsx", ".ts", ".tsx")

                status = st.empty()
                progress = st.progress(0, text="Fetching file tree from GitHub...")

                # Fetch file tree
                def gh_get(url):
                    req = _urllib.Request(url, headers={"User-Agent": "SIR-Engine/1.0", "Accept": "application/vnd.github.v3+json"})
                    resp = _urllib.urlopen(req, timeout=15)
                    return json.loads(resp.read().decode("utf-8"))

                # Try main then master
                tree_data = None
                for try_branch in [branch, "main", "master"]:
                    try:
                        tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{try_branch}?recursive=1"
                        tree_data = gh_get(tree_url)
                        branch = try_branch
                        break
                    except Exception:
                        continue

                if not tree_data:
                    st.error(f"Could not fetch repo tree. Check the URL and make sure the repo is public.")
                    st.stop()

                # Filter to target files, skip node_modules etc
                skip_dirs = {"node_modules", "__pycache__", ".git", "dist", "build", "vendor", ".venv", "venv"}
                all_files = [
                    f for f in tree_data.get("tree", [])
                    if f["type"] == "blob"
                    and any(f["path"].endswith(ext) for ext in target_exts)
                    and not any(part in skip_dirs for part in f["path"].split("/"))
                    and f.get("size", 0) < 500000  # skip files > 500KB
                ]

                if not all_files:
                    st.warning(f"No {gh_lang} files found in this repository.")
                    st.stop()

                st.info(f"Found {len(all_files)} file(s) in `{owner}/{repo}` — scanning...")

                # Fetch and scan each file
                from collections import defaultdict as _dd
                gh_groups = _dd(list)
                total_gh_funcs = 0
                errors_gh = 0
                scanned = 0

                for i, file_info in enumerate(all_files):
                    fpath = file_info["path"]
                    status.text(f"Scanning {fpath}...")
                    progress.progress((i + 1) / len(all_files), text=f"Scanned {i+1}/{len(all_files)} files")

                    try:
                        # Fetch file content via raw GitHub
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fpath}"
                        req = _urllib.Request(raw_url, headers={"User-Agent": "SIR-Engine/1.0"})
                        resp = _urllib.urlopen(req, timeout=10)
                        src = resp.read().decode("utf-8", errors="replace")
                        scanned += 1
                    except Exception:
                        errors_gh += 1
                        continue

                    try:
                        if fpath.endswith(".py"):
                            for qualname, lineno, code in extract_functions(src, fpath, False):
                                if qualname.split(".")[-1] in ignored_names:
                                    continue
                                total_gh_funcs += 1
                                try:
                                    h = hash_source(code, mode="semantic")
                                    gh_groups[h].append({
                                        "file": fpath, "name": qualname,
                                        "lineno": lineno, "lang": "Python", "code": code
                                    })
                                except Exception:
                                    pass
                        elif any(fpath.endswith(e) for e in (".js", ".jsx", ".ts", ".tsx")):
                            from sir_js import extract_js_functions, tokenize as _jt, canonicalize_js as _cj
                            funcs = extract_js_functions(src, fpath)
                            for name, lineno, params, body_src in funcs:
                                if name in ignored_names:
                                    continue
                                total_gh_funcs += 1
                                try:
                                    body_tokens = _jt(body_src)
                                    sir = _cj(params, body_tokens)
                                    gh_groups[sir["sir_sha256"]].append({
                                        "file": fpath, "name": name,
                                        "lineno": lineno, "lang": "JS/TS", "code": body_src
                                    })
                                except Exception:
                                    pass
                        elif "AI-powered" in gh_lang:
                            from sir_ai_translate import detect_language, extract_raw_functions, translate_to_python
                            lang = detect_language(fpath) or fpath.rsplit(".", 1)[-1].upper()
                            raw_funcs = extract_raw_functions(src, lang)
                            for name, lineno, raw_src in raw_funcs:
                                if name in ignored_names:
                                    continue
                                total_gh_funcs += 1
                                try:
                                    _cfg = get_ai_config()
                                    if _cfg["backend"] == "none":
                                        continue
                                    py_src = translate_to_python(
                                        raw_src, lang,
                                        backend=_cfg["backend"],
                                        api_key=_cfg["api_key"],
                                        ollama_model=_cfg["ollama_model"],
                                        ollama_host=_cfg["ollama_host"]
                                    )
                                    if py_src.strip():
                                        h = hash_source(py_src, mode="semantic")
                                        gh_groups[h].append({
                                            "file": fpath, "name": name,
                                            "lineno": lineno, "lang": lang,
                                            "code": raw_src, "translated": True
                                        })
                                except Exception:
                                    pass
                    except Exception:
                        errors_gh += 1

                progress.progress(1.0, text="Scan complete!")
                status.empty()

                gh_dupes = {h: v for h, v in gh_groups.items() if len(v) >= int(gh_min)}
                health = int(100 * len(gh_groups) / max(total_gh_funcs, 1))

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Files scanned", scanned)
                c2.metric("Functions", total_gh_funcs)
                c3.metric("Unique structures", len(gh_groups))
                c4.metric(f"Duplicate clusters", len(gh_dupes))
                c5.metric("Health score", f"{health}/100")

                if ignored_names:
                    st.caption(f"Ignored {len(ignored_names)} function name(s): {', '.join(sorted(ignored_names))}")
                if errors_gh:
                    st.caption(f"{errors_gh} file(s) could not be fetched or parsed.")

                st.divider()

                if not gh_dupes:
                    st.success(f"✅ `{owner}/{repo}` has no structural duplicate functions!")
                else:
                    st.error(f"Found {len(gh_dupes)} duplicate cluster(s) in `{owner}/{repo}`")

                    # Build report
                    report_data = {
                        "repo": f"{owner}/{repo}",
                        "branch": branch,
                        "health_score": health,
                        "total_functions": total_gh_funcs,
                        "duplicate_clusters": len(gh_dupes),
                        "clusters": []
                    }

                    for h, occs in sorted(gh_dupes.items(), key=lambda x: -len(x[1])):
                        langs = set(o["lang"] for o in occs)
                        is_cross = len(langs) > 1
                        icon = "🌐" if is_cross else "🔴"
                        label = f"{icon} {len(occs)} duplicates — hash: `{h[:16]}...`"

                        with st.expander(label, expanded=len(occs) >= 3):
                            for o in occs:
                                gh_file_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{o['file']}#L{o['lineno']}"
                                st.markdown(f"**`{o['name']}`** in [`{o['file']}`]({gh_file_url}) (line {o['lineno']})")
                                lang_key = "python" if o["lang"] == "Python" else "javascript"
                                st.code(o["code"][:800] + ("..." if len(o["code"]) > 800 else ""), language=lang_key)

                        report_data["clusters"].append({
                            "hash": h,
                            "count": len(occs),
                            "occurrences": [{"file": o["file"], "name": o["name"], "lineno": o["lineno"]} for o in occs]
                        })

                    st.download_button(
                        "📥 Download scan report (JSON)",
                        data=json.dumps(report_data, indent=2),
                        file_name=f"sir_scan_{repo}.json",
                        mime="application/json"
                    )

            except Exception as e:
                st.error(f"GitHub scan failed: {e}")
                st.caption("Make sure the repository is public and the URL is correct.")

# ─────────────────────────────────────────────
#  PACK
# ─────────────────────────────────────────────

with tab_pack:
    st.subheader("Pack: compress files into a SIR bundle")

    pack_lang = st.selectbox("Language", ["Python (.py)", "JavaScript (.js / .jsx)", "TypeScript (.ts / .tsx)"], key="pack_lang")

    if pack_lang == "Python (.py)":
        st.write("Upload `.py` files — SIR encodes each function into a structural node graph, deduplicates shared logic, and bundles everything into a single downloadable `bundle.json`.")
        pack_uploaded = st.file_uploader("Upload Python files to pack", type=["py"], accept_multiple_files=True, key="pack_upload")
        pack_methods = st.checkbox("Include class methods", value=False, key="pack_methods")
    else:
        _exts = ["js", "jsx"] if "JavaScript" in pack_lang else ["ts", "tsx"]
        st.write(f"Upload `{'`, `'.join('.' + e for e in _exts)}` files — SIR tokenises each function, strips type annotations, deduplicates by structural hash, and bundles into `bundle_js.json`.")
        pack_uploaded_js = st.file_uploader(f"Upload {pack_lang} files", type=_exts, accept_multiple_files=True, key="pack_js_upload")

        if st.button("Build JS/TS pack", type="primary"):
            if not pack_uploaded_js:
                st.warning("Please upload at least one file.")
            else:
                try:
                    from sir_js import extract_js_functions, tokenize as js_tokenize, canonicalize_js
                    import hashlib as _hashlib
                    js_canonical_store = {}
                    js_roots = []
                    js_namemaps = {}
                    total_js = 0
                    progress = st.progress(0, text="Packing...")
                    status = st.empty()
                    for i, f in enumerate(pack_uploaded_js):
                        status.text(f"Encoding {f.name}...")
                        src = f.read().decode("utf-8", errors="replace")
                        funcs = extract_js_functions(src, f.name)
                        for name, lineno, params, body_src in funcs:
                            total_js += 1
                            body_tokens = js_tokenize(body_src)
                            sir = canonicalize_js(params, body_tokens)
                            h = sir["sir_sha256"]
                            occ_key = f"{f.name}::{name}::{lineno}"
                            if h not in js_canonical_store:
                                js_canonical_store[h] = {"sir_sha256": h, "params_count": len(params), "body_src": body_src}
                            js_namemaps[occ_key] = {"sir_sha256": h, "name_map": sir.get("name_map", {}), "original_params": params}
                            js_roots.append({"sir_sha256": h, "file": f.name, "name": name, "lineno": lineno, "occurrence_key": occ_key})
                        progress.progress((i+1)/len(pack_uploaded_js), text=f"Packed {i+1}/{len(pack_uploaded_js)} files")
                    progress.progress(1.0, text="Done!")
                    status.empty()

                    unique = len(js_canonical_store)
                    deduped = total_js - unique
                    health = int(100 * unique / max(total_js, 1))
                    bundle = {"format": "SIR-JS-PACK", "version": "0.1", "total_functions": total_js,
                              "unique_structures": unique, "canonical_store": js_canonical_store,
                              "roots": js_roots, "namemaps": js_namemaps}
                    bundle_json = json.dumps(bundle, indent=2)

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Files", len(pack_uploaded_js))
                    c2.metric("Functions", total_js)
                    c3.metric("Unique structures", unique)
                    c4.metric("Duplicates removed", deduped)
                    c5.metric("Health score", f"{health}/100")
                    st.success("✅ JS/TS pack built!")
                    st.download_button("📥 Download bundle_js.json", data=bundle_json, file_name="bundle_js.json", mime="application/json")
                except Exception as e:
                    st.error(f"Pack failed: {e}")

    if pack_lang == "Python (.py)":
        pack_methods = locals().get("pack_methods", False)
        pack_uploaded = locals().get("pack_uploaded", None)

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

    unpack_lang = st.selectbox("Language", ["Python (.py)", "JavaScript / TypeScript"], key="unpack_lang")

    if unpack_lang == "JavaScript / TypeScript":
        st.write("Upload a `bundle_js.json` produced by the JS/TS Pack tab. SIR restores all files with original names rehydrated.")
        unpack_js_bundle = st.file_uploader("Upload bundle_js.json", type=["json"], key="unpack_js_bundle")
        unpack_js_dedup = st.checkbox("Skip duplicate structures (write only unique functions)", value=True, key="unpack_js_dedup")

        if st.button("Unpack JS/TS", type="primary"):
            if not unpack_js_bundle:
                st.warning("Please upload a bundle_js.json.")
            else:
                try:
                    import re as _re
                    bundle = json.loads(unpack_js_bundle.read().decode("utf-8"))
                    roots = bundle.get("roots", [])
                    namemaps = bundle.get("namemaps", {})
                    canonical_store = bundle.get("canonical_store", {})

                    by_file = defaultdict(list)
                    for r in roots:
                        by_file[r["file"]].append(r)

                    zip_buffer = io.BytesIO()
                    restored_files = 0
                    seen_hashes = set()

                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        for rel_file, occs in by_file.items():
                            lines = [f"// Restored by SIR Engine from {rel_file}\n\n"]
                            for occ in sorted(occs, key=lambda x: x["lineno"]):
                                h = occ["sir_sha256"]
                                nm_entry = namemaps.get(occ.get("occurrence_key", ""), {})
                                name_map = nm_entry.get("name_map", {})
                                orig_params = nm_entry.get("original_params", [])
                                canonical = canonical_store.get(h, {})
                                body = canonical.get("body_src", "")
                                for canon_name, orig_name in name_map.items():
                                    body = _re.sub(rf'\b{_re.escape(canon_name)}\b', orig_name, body)
                                params_str = ", ".join(orig_params)
                                func_src = f"function {occ['name']}({params_str}) {body}\n\n"
                                if unpack_js_dedup and h in seen_hashes:
                                    lines.append(f"// DUPLICATE skipped: {occ['name']}\n\n")
                                else:
                                    lines.append(func_src)
                                    seen_hashes.add(h)
                            zf.writestr(rel_file, "".join(lines))
                            restored_files += 1

                    zip_buffer.seek(0)
                    c1, c2 = st.columns(2)
                    c1.metric("Files restored", restored_files)
                    c2.metric("Unique structures written", len(seen_hashes))
                    st.success("JS/TS unpack complete!")
                    st.download_button("📥 Download restored JS/TS files (.zip)", data=zip_buffer,
                                      file_name="restored_js.zip", mime="application/zip")
                except Exception as e:
                    st.error(f"Unpack failed: {e}")
    else:
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

    verify_lang = st.selectbox("Language", ["Python (.py)", "JavaScript / TypeScript"], key="verify_lang")

    if verify_lang == "JavaScript / TypeScript":
        st.write("Upload the JS/TS `bundle_js.json` and the restored JS files. SIR will re-hash every function and confirm they match.")
        verify_bundle_js = st.file_uploader("Upload bundle_js.json", type=["json"], key="verify_bundle_js")
        verify_restored_js = st.file_uploader("Upload restored JS/TS files", type=["js","jsx","ts","tsx"], accept_multiple_files=True, key="verify_restored_js")

        if st.button("Verify JS/TS", type="primary"):
            if not verify_bundle_js or not verify_restored_js:
                st.warning("Please upload both the bundle and restored files.")
            else:
                try:
                    from sir_js import extract_js_functions, tokenize as js_tokenize, canonicalize_js
                    bundle = json.loads(verify_bundle_js.read().decode("utf-8"))
                    expected = {r["sir_sha256"] for r in bundle.get("roots", [])}
                    actual = set()
                    for f in verify_restored_js:
                        src = f.read().decode("utf-8", errors="replace")
                        funcs = extract_js_functions(src, f.name)
                        for name, lineno, params, body_src in funcs:
                            body_tokens = js_tokenize(body_src)
                            sir = canonicalize_js(params, body_tokens)
                            actual.add(sir["sir_sha256"])
                    missing = expected - actual
                    extra = actual - expected
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Expected hashes", len(expected))
                    c2.metric("Found hashes", len(actual))
                    c3.metric("Missing", len(missing))
                    if not missing and not extra:
                        st.success("✅ VERIFY SUCCESS — all JS/TS structural hashes match!")
                    else:
                        st.error(f"❌ MISMATCH — {len(missing)} missing, {len(extra)} extra")
                except Exception as e:
                    st.error(f"Verify failed: {e}")
    else:
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
    diff_lang = st.selectbox("Language", ["Python (.py)", "JavaScript / TypeScript"], key="diff_lang")

    if diff_lang == "JavaScript / TypeScript":
        st.subheader("Diff: structural diff between two JS/TS file sets")
        st.write("Upload two sets of JS/TS files. SIR finds shared logic, additions, and removals by structural hash.")
        diff_js_a = st.file_uploader("Upload Set A (JS/TS)", type=["js","jsx","ts","tsx"], accept_multiple_files=True, key="diff_js_a")
        diff_js_b = st.file_uploader("Upload Set B (JS/TS)", type=["js","jsx","ts","tsx"], accept_multiple_files=True, key="diff_js_b")

        if st.button("Run JS/TS diff", type="primary"):
            if not diff_js_a or not diff_js_b:
                st.warning("Please upload files for both sets.")
            else:
                try:
                    from sir_js import extract_js_functions, tokenize as js_tokenize, canonicalize_js
                    def _hash_js_files(files):
                        groups = defaultdict(list)
                        for f in files:
                            src = f.read().decode("utf-8", errors="replace")
                            funcs = extract_js_functions(src, f.name)
                            for name, lineno, params, body_src in funcs:
                                body_tokens = js_tokenize(body_src)
                                sir = canonicalize_js(params, body_tokens)
                                groups[sir["sir_sha256"]].append(f"{f.name}::{name}")
                        return groups

                    hashes_a = _hash_js_files(diff_js_a)
                    diff_js_b_reset = [f for f in diff_js_b]
                    hashes_b = _hash_js_files(diff_js_b)

                    set_a, set_b = set(hashes_a), set(hashes_b)
                    common = set_a & set_b
                    only_a = set_a - set_b
                    only_b = set_b - set_a
                    total_a = sum(len(v) for v in hashes_a.values())
                    total_b = sum(len(v) for v in hashes_b.values())
                    health_a = int(100 * len(set_a) / max(total_a, 1))
                    health_b = int(100 * len(set_b) / max(total_b, 1))

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Shared structures", len(common))
                    c2.metric("Only in A", len(only_a))
                    c3.metric("Only in B", len(only_b))
                    c4.metric("Health A", f"{health_a}/100")
                    c5.metric("Health B", f"{health_b}/100")
                    st.divider()

                    if common:
                        with st.expander(f"✅ {len(common)} shared structures", expanded=False):
                            for h in sorted(common):
                                st.write(f"A: {', '.join(hashes_a[h])}  ↔  B: {', '.join(hashes_b[h])}")
                    if only_a:
                        with st.expander(f"🔴 {len(only_a)} only in A (removed or not ported)", expanded=True):
                            for h in sorted(only_a):
                                st.write(f"• {', '.join(hashes_a[h])}")
                    if only_b:
                        with st.expander(f"🟢 {len(only_b)} only in B (added or new)", expanded=True):
                            for h in sorted(only_b):
                                st.write(f"• {', '.join(hashes_b[h])}")
                except Exception as e:
                    st.error(f"Diff failed: {e}")
    else:
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
    merge_lang = st.selectbox("Language", ["Python (.py)", "JavaScript / TypeScript"], key="merge_lang")

    if merge_lang == "JavaScript / TypeScript":
        st.subheader("Merge: remove duplicate JS/TS functions and consolidate into utils.js")
        st.write("Upload JS/TS files — SIR finds all structural duplicates, keeps one canonical version, removes the rest, and rewrites call sites.")
        st.warning("Always back up your code before merging. Review the output before using in production.")
        merge_js_uploaded = st.file_uploader("Upload JS/TS files", type=["js","jsx","ts","tsx"], accept_multiple_files=True, key="merge_js_upload")
        merge_js_min = st.number_input("Min duplicates to merge", min_value=2, max_value=50, value=2, step=1, key="merge_js_min")

        if st.button("Run JS/TS merge", type="primary"):
            if not merge_js_uploaded:
                st.warning("Please upload at least one file.")
            else:
                try:
                    import re as _re
                    from sir_js import extract_js_functions, tokenize as js_tokenize, canonicalize_js

                    file_sources = {}
                    all_funcs = []
                    for f in merge_js_uploaded:
                        src = f.read().decode("utf-8", errors="replace")
                        file_sources[f.name] = src
                        funcs = extract_js_functions(src, f.name)
                        for name, lineno, params, body_src in funcs:
                            body_tokens = js_tokenize(body_src)
                            sir = canonicalize_js(params, body_tokens)
                            all_funcs.append({"file": f.name, "name": name, "lineno": lineno,
                                              "params": params, "body_src": body_src,
                                              "sir_sha256": sir["sir_sha256"]})

                    groups = defaultdict(list)
                    for fn in all_funcs:
                        groups[fn["sir_sha256"]].append(fn)
                    dupes = {h: v for h, v in groups.items() if len(v) >= int(merge_js_min)}

                    if not dupes:
                        st.success("No duplicate JS/TS functions found!")
                    else:
                        modified = dict(file_sources)
                        utils_functions = {}
                        changes = []

                        for h, occs in dupes.items():
                            canonical = occs[0]
                            canon_name = canonical["name"]
                            params_str = ", ".join(canonical["params"])
                            utils_functions[canon_name] = f"function {canon_name}({params_str}) {canonical['body_src']}"

                            for occ in occs:
                                src = modified.get(occ["file"], "")
                                pat = r"function\s+" + _re.escape(occ["name"]) + r"\s*\([^)]*\)\s*\{"
                                m = _re.search(pat, src)
                                if m:
                                    brace_start = src.index("{", m.start())
                                    depth = 0
                                    end = brace_start
                                    for idx in range(brace_start, len(src)):
                                        if src[idx] == "{": depth += 1
                                        elif src[idx] == "}":
                                            depth -= 1
                                            if depth == 0: end = idx + 1; break
                                    src = src[:m.start()].rstrip() + "\n\n" + src[end:].lstrip()
                                if occ["name"] != canon_name:
                                    src = _re.sub(r"" + _re.escape(occ["name"]) + r"\s*\(", canon_name + "(", src)
                                import_line = f"import {{ {canon_name} }} from './utils.js';"
                                if import_line not in src:
                                    src = import_line + "\n" + src
                                modified[occ["file"]] = src
                                changes.append({"file": occ["file"], "removed": occ["name"], "canonical": canon_name})

                        utils_src = "// utils.js - canonical functions by SIR Engine\n\n"
                        for name, src in utils_functions.items():
                            utils_src += src + "\n\n"

                        health_before = int(100 * len(groups) / max(len(all_funcs), 1))
                        remaining = len(all_funcs) - sum(len(v)-1 for v in dupes.values())
                        health_after = min(int(100 * len(groups) / max(remaining, 1)), 100)

                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Duplicates removed", len(changes))
                        c2.metric("Canonical functions", len(utils_functions))
                        c3.metric("Health before", f"{health_before}/100")
                        c4.metric("Health after", f"{health_after}/100")
                        st.divider()

                        for ch in changes:
                            st.markdown(f"Removed **`{ch['removed']}`** from `{ch['file']}` kept as **`{ch['canonical']}`** in `utils.js`")

                        zip_buffer = io.BytesIO()
                        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                            for fname, src in modified.items():
                                zf.writestr(fname, src)
                            zf.writestr("utils.js", utils_src)
                            report = {"changes": changes, "canonical_functions": list(utils_functions.keys())}
                            zf.writestr("sir_js_merge_report.json", json.dumps(report, indent=2))
                        zip_buffer.seek(0)

                        st.success(f"Merged {len(changes)} duplicate(s) into utils.js!")
                        st.download_button("Download merged codebase (.zip)", data=zip_buffer,
                                          file_name="merged_js.zip", mime="application/zip")
                except Exception as e:
                    st.error(f"Merge failed: {e}")

    else:
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
    st.subheader("SIR Engine — Semantic Code Intelligence")
    st.markdown("""
**SIR** stands for **Semantic Intermediate Representation**.

Instead of comparing code as text, SIR strips away every cosmetic detail — variable names, formatting,
comments, type annotations — and reduces every function to its pure logical structure. Two functions
that do the same thing will always produce the **same structural hash**, regardless of what they are
named, how they are formatted, or which language they are written in.

This is based on **alpha equivalence** — a concept from lambda calculus (1936) that proves two
expressions are logically identical if you can rename their variables and produce the same result.

---
""")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Get started in 30 seconds")
        st.markdown("""
**Option 1 — Use the web app (no install)**

1. Pick a tab above
2. Upload your code files
3. Hit the scan/run button
4. That's it

Native Python, JavaScript, and TypeScript scanning is completely free with no account needed.

---

**Option 2 — Run locally (free AI translation)**

Run SIR Engine on your own machine to get free AI-powered scanning for C++, Java, Rust, Go, and 25+ other languages via Ollama:

```bash
git clone https://github.com/lflin00/SRI-ENGINE
cd SRI-ENGINE/SIR_MAIN
pip install streamlit
ollama pull codellama:7b
streamlit run sir_ui.py
```

Then open http://localhost:8501 in your browser. Ollama runs the AI translation locally — your code never leaves your machine.

---

**Option 3 — AI scanning on the web app**

For C++, Java, Rust, and other languages on the live web app:
1. Get a free API key at [console.anthropic.com](https://console.anthropic.com)
2. Paste it in the sidebar on the left
3. Select **Any Language (AI-powered)** in the Scan tab
""")

    with col2:
        st.markdown("### What each tab does")
        st.markdown("""
| Tab | What it does |
|-----|-------------|
| **Scan** | Upload code files → find structurally duplicate functions. Supports Python, JS, TS, and 25+ languages via AI. |
| **GitHub Scanner** | Paste any public GitHub repo URL → scan without downloading anything. |
| **Pack** | Compress Python or JS/TS files into a deduplicated `bundle.json`. |
| **Unpack** | Restore files from a bundle with original names rehydrated. |
| **Verify** | Confirm restored files match the original structural hashes. |
| **Diff** | Compare two codebases structurally — find shared logic, additions, removals. |
| **Merge** | Find all duplicates, keep one canonical version, remove the rest, rename all call sites. |

---

### How the health score works

Every scan shows a **Health Score from 0 to 100**.

```
Health = (unique structures / total functions) × 100
```

100 means no duplicate logic anywhere.
70 means 30% of your functions are structural duplicates.
Lower scores mean more redundant code to clean up.

---

### The .sir_ignore feature

In any scan you can paste function names into the **.sir_ignore** box to skip them.
Use this for intentional duplicates — functions you know are the same but want to keep separate.
One name per line. Lines starting with # are treated as comments.

---

### VS Code Extension

Scan for duplicates directly in your editor. Auto-scans every time you save a file.
Shows health score in the status bar. Auto-merge with before/after diff preview — fully reversible with Cmd+Z.
""")
        st.link_button(
            "📥 Download VS Code Extension (.vsix)",
            "https://github.com/lflin00/SRI-ENGINE/raw/main/sir-engine-0.0.2.vsix"
        )
        st.caption("Install: open VS Code → Cmd+Shift+P → Install from VSIX → select the file.")

    st.divider()
    st.markdown("### Language support")

    lang_data = {
        "Language": ["Python", "JavaScript", "TypeScript", "C", "C++", "Java", "Rust", "Go",
                     "Ruby", "Swift", "Kotlin", "C#", "PHP", "Dart", "Lua", "Scala",
                     "Haskell", "R", "Elixir", "OCaml", "F#", "Julia", "Nim", "Zig"],
        "Scan": ["✓ Native"]*3 + ["✓ AI"]*21,
        "Cross-language": ["✓"]*3 + ["✓ via AI"]*21,
        "Pack/Unpack": ["✓"]*2 + ["✓"]*1 + ["Planned"]*21,
    }
    import pandas as pd
    try:
        st.dataframe(pd.DataFrame(lang_data), use_container_width=True, hide_index=True)
    except Exception:
        st.markdown("Python, JavaScript, TypeScript — native. C, C++, Java, Rust, Go, Ruby, Swift, Kotlin, C#, PHP, Dart, Lua, Scala, Haskell, R, and more — via AI translation.")

    st.divider()
    st.markdown("""
### Limitations (AI translation layer)
The AI translation layer for non-Python/JS/TS languages is powered by a language model.
Results are highly reliable but not mathematically guaranteed like the native pipelines.
Very complex language-specific features (C++ templates, Rust lifetimes, Java generics) may be simplified.
Best for pure logic functions. Marked as *(AI translated)* in results so you always know which path was used.

---
**Built by Lucas Flinders** · [GitHub](https://github.com/lflin00/SRI-ENGINE) · [Live App](https://sri-engine-7amwtce7a23k7q34cpnxem.streamlit.app)
""")
