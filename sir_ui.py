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

tab_scan, tab_pack, tab_unpack, tab_verify, tab_diff, tab_merge, tab_about = st.tabs([
    "Scan", "Pack", "Unpack", "Verify", "Diff", "Merge", "About"
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
            progress = st.progress(0, text="Starting scan...")
            status = st.empty()
            for i, f in enumerate(uploaded):
                status.text(f"Scanning {f.name}...")
                src = f.read().decode("utf-8", errors="replace")
                for qualname, lineno, code in extract_functions(src, f.name, include_methods):
                    total_funcs += 1
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
            progress = st.progress(0, text="Starting pack...")
            status = st.empty()
            for i, f in enumerate(pack_uploaded):
                status.text(f"Encoding {f.name}...")
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

                # ── MODE 1: Full file restore ──
                if "Full file restore" in unpack_mode:
                    st.info("Restoring full files with duplicates removed and calls updated...")

                    # Group roots by original file
                    by_file: Dict[str, List[Dict]] = defaultdict(list)
                    for r in roots:
                        by_file[r["file"]].append(r)

                    # Find duplicate structures (same sir_sha256, different qualnames)
                    hash_to_roots: Dict[str, List[Dict]] = defaultdict(list)
                    for r in roots:
                        if r.get("sir_sha256"):
                            hash_to_roots[r["sir_sha256"]].append(r)
                    dupes = {h: rs for h, rs in hash_to_roots.items() if len(rs) >= 2}

                    # Pick canonical name for each dupe cluster (first occurrence wins)
                    canonical_map: Dict[str, str] = {}  # qualname -> canonical_qualname
                    canonical_funcs: Dict[str, str] = {}  # canonical_qualname -> reconstructed source
                    for h, rs in dupes.items():
                        canonical = rs[0]["qualname"]
                        for r in rs:
                            canonical_map[r["qualname"]] = canonical
                        # Reconstruct canonical function source
                        root_id = rs[0]["root"]
                        nm = namemaps.get(root_id, {}) if rehydrate else {}
                        try:
                            code = decode_sir({"nodes": nodes, "root": root_id, "name_map": nm}, rehydrate=rehydrate)
                            canonical_funcs[canonical] = code
                        except Exception:
                            pass

                    # Reconstruct each file
                    progress = st.progress(0, text="Starting restore...")
                    status = st.empty()
                    file_list = list(by_file.items())
                    reconstructed: Dict[str, str] = {}

                    for i, (fname, file_roots) in enumerate(file_list):
                        status.text(f"Restoring {fname}...")
                        lines = []

                        # Add utils import if needed
                        needs_import = [r["qualname"] for r in file_roots
                                        if r["qualname"] in canonical_map
                                        and canonical_map[r["qualname"]] != r["qualname"]]
                        if needs_import or any(r["qualname"] in canonical_funcs for r in file_roots):
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

                        # Add non-duplicate functions (reconstruct from nodes)
                        for r in sorted(file_roots, key=lambda x: x.get("lineno", 0)):
                            qualname = r["qualname"]
                            # Skip duplicates — they move to utils.py
                            if qualname in canonical_map and canonical_map[qualname] != qualname:
                                continue
                            # Skip canonicals too — they move to utils.py
                            if qualname in canonical_funcs:
                                continue
                            # Reconstruct this function
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

                    # Build utils.py
                    utils_lines = ['"""utils.py — Canonical functions deduplicated by SIR Engine."""', ""]
                    for cname, csrc in canonical_funcs.items():
                        utils_lines.append(csrc)
                        utils_lines.append("")
                    reconstructed["utils.py"] = "\n".join(utils_lines)

                    progress.progress(1.0, text="Creating zip...")
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

                # ── MODE 2: Individual functions ──
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
#  MERGE HELPERS
# ─────────────────────────────────────────────

def remove_function_node(tree: ast.Module, func_name: str) -> ast.Module:
    """Remove a top-level function definition from an AST."""
    tree.body = [
        node for node in tree.body
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name)
    ]
    return tree


def rename_calls(source: str, old_name: str, new_name: str) -> str:
    """Rename all calls to old_name -> new_name in source using AST rewrite."""
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
    """Add 'from utils import func_name' at the top if not already present."""
    import_line = f"from utils import {func_name}"
    if import_line in source:
        return source
    lines = source.splitlines()
    # Insert after any existing imports
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    return "\n".join(lines)


def get_function_source(source: str, func_name: str) -> str:
    """Extract source of a named top-level function."""
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
#  MERGE TAB
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

            st.session_state["merge_file_sources"] = file_sources
            st.session_state["merge_dupes"] = {h: [vars(o) for o in occ] for h, occ in dupes.items()}
            st.session_state["merge_total"] = total_funcs

    # Show results and canonical picker
    if "merge_dupes" in st.session_state and st.session_state["merge_dupes"]:
        dupes_data = st.session_state["merge_dupes"]
        file_sources = st.session_state["merge_file_sources"]

        st.divider()
        st.success(f"Found **{len(dupes_data)}** duplicate cluster(s) across {len(file_sources)} file(s).")
        st.write("For each cluster, pick which function name to keep as the canonical version:")

        canonical_choices: Dict[str, str] = {}

        for h, occs in dupes_data.items():
            names = [o["qualname"] for o in occs]
            with st.expander(f"Cluster `{h[:16]}...` — {len(occs)} duplicates: {', '.join(f'`{n}`' for n in names)}", expanded=True):
                cols = st.columns([3, 1])
                with cols[0]:
                    choice = st.selectbox(
                        "Canonical name (keep this one)",
                        options=names,
                        key=f"canon_{h}",
                    )
                with cols[1]:
                    st.markdown("<br>", unsafe_allow_html=True)
                    st.markdown(f"**{len(occs) - 1}** duplicate(s) will be removed")
                canonical_choices[h] = choice

        st.divider()

        if st.button("🔀 Apply merge and download", type="primary"):
            modified_sources: Dict[str, str] = {k: v for k, v in file_sources.items()}
            utils_functions: Dict[str, str] = {}
            progress = st.progress(0, text="Applying merge...")
            status = st.empty()
            dupe_list = list(dupes_data.items())

            for i, (h, occs) in enumerate(dupe_list):
                canonical_name = st.session_state.get(f"canon_{h}", occs[0]["qualname"])
                status.text(f"Merging cluster {i+1}/{len(dupe_list)}: keeping '{canonical_name}'...")

                # Find the file containing the canonical function
                canonical_occ = next((o for o in occs if o["qualname"] == canonical_name), occs[0])
                canonical_file = canonical_occ["file"]
                canonical_src = get_function_source(modified_sources[canonical_file], canonical_name)

                if canonical_src:
                    utils_functions[canonical_name] = canonical_src

                # Process each occurrence
                for occ in occs:
                    fname = occ["file"]
                    func_name = occ["qualname"]
                    src = modified_sources[fname]

                    # Rename all calls of this function to canonical name
                    if func_name != canonical_name:
                        src = rename_calls(src, func_name, canonical_name)

                    # Remove the duplicate function definition (keep canonical in utils.py)
                    try:
                        tree = ast.parse(src)
                        tree = remove_function_node(tree, func_name)
                        ast.fix_missing_locations(tree)
                        src = ast.unparse(tree)
                    except Exception:
                        pass

                    # Add import from utils
                    src = add_import(src, canonical_name)
                    modified_sources[fname] = src

                # Also remove canonical from its original file (it moves to utils.py)
                try:
                    src = modified_sources[canonical_file]
                    tree = ast.parse(src)
                    tree = remove_function_node(tree, canonical_name)
                    ast.fix_missing_locations(tree)
                    modified_sources[canonical_file] = ast.unparse(tree)
                except Exception:
                    pass

                progress.progress((i + 1) / len(dupe_list), text=f"Merged {i+1}/{len(dupe_list)} clusters")

            progress.progress(1.0, text="Building zip...")
            status.empty()

            # Build utils.py
            utils_src = '"""utils.py — Canonical functions extracted by SIR Engine merge."""\n\n'
            for fname, fsrc in utils_functions.items():
                utils_src += fsrc + "\n\n"

            # Build zip
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname, src in modified_sources.items():
                    zf.writestr(fname, src)
                zf.writestr("utils.py", utils_src)

            zip_buffer.seek(0)

            total_removed = sum(len(occs) for occs in dupes_data.values())
            st.success(f"✅ Merged! Removed **{total_removed}** duplicate function(s), moved **{len(utils_functions)}** canonical function(s) to `utils.py`.")
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
| **Scan** | Upload `.py` files → find structurally duplicate functions |
| **Pack** | Upload `.py` files → compress into a `bundle.json` |
| **Unpack** | Upload `bundle.json` → restore all functions as `.py` files |
| **Verify** | Upload `bundle.json` + restored files → confirm hashes match |
| **Diff** | Upload two sets of files → compare their logical structures |
| **Merge** | Upload `.py` files → remove duplicates and consolidate into `utils.py` |

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
