#!/usr/bin/env python3
"""
sir_js_pipeline.py — Pack, Unpack, Verify, Diff, and Merge for JavaScript and TypeScript.

Works the same way as the Python SIR pipeline but for .js, .jsx, .ts, .tsx files.
Uses sir_js.py for tokenisation, canonicalisation, and hashing.

Commands:
  pack    <folder>          Scan folder, build bundle.json
  unpack  <bundle.json>     Restore deduplicated JS/TS files
  verify  <bundle.json>     Confirm all hashes still match
  diff    <folder_a> <folder_b>   Structural diff between two JS/TS folders
  merge   <folder>          Find duplicates and consolidate into utils.js

Usage:
  python3 sir_js_pipeline.py pack ./my_project -o ./bundle
  python3 sir_js_pipeline.py unpack ./bundle/bundle.json -o ./restored
  python3 sir_js_pipeline.py verify ./bundle/bundle.json ./restored
  python3 sir_js_pipeline.py diff ./project_v1 ./project_v2
  python3 sir_js_pipeline.py merge ./my_project -o ./merged
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import the JS parser
sys.path.insert(0, str(Path(__file__).parent))
from sir_js import (
    extract_js_functions,
    tokenize,
    canonicalize_js,
    strip_typescript,
)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

JS_EXTENSIONS = {'.js', '.jsx', '.ts', '.tsx'}


def sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def find_js_files(folder: str) -> List[Path]:
    results = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')
                   and d not in ('node_modules', '__pycache__', 'dist', 'build', '.git')]
        for f in files:
            if Path(f).suffix in JS_EXTENSIONS:
                results.append(Path(root) / f)
    return sorted(results)


def get_lang(path: Path) -> str:
    return 'typescript' if path.suffix in ('.ts', '.tsx') else 'javascript'


def hash_function(params: List[str], body_src: str) -> Tuple[str, Dict]:
    """Returns (sir_sha256, sir_dict)."""
    body_tokens = tokenize(body_src)
    sir = canonicalize_js(params, body_tokens)
    return sir['sir_sha256'], sir


def extract_all_functions(folder: str) -> List[Dict]:
    """
    Walk folder and extract all JS/TS functions with metadata.
    Returns list of dicts with keys:
      file, rel_file, name, lineno, params, body_src, sir_sha256,
      name_map, occurrence_key, lang
    """
    results = []
    base = Path(folder)
    for fpath in find_js_files(folder):
        rel = str(fpath.relative_to(base))
        src = fpath.read_text(encoding='utf-8', errors='replace')
        try:
            funcs = extract_js_functions(src, fpath.name)
        except Exception:
            continue
        for name, lineno, params, body_src in funcs:
            try:
                h, sir = hash_function(params, body_src)
            except Exception:
                continue
            occ_key = f"{rel}::{name}::{lineno}"
            results.append({
                'file': str(fpath),
                'rel_file': rel,
                'name': name,
                'lineno': lineno,
                'params': params,
                'body_src': body_src,
                'sir_sha256': h,
                'name_map': sir.get('name_map', {}),
                'occurrence_key': occ_key,
                'lang': get_lang(fpath),
            })
    return results


# ─────────────────────────────────────────────
#  PACK
# ─────────────────────────────────────────────

def cmd_pack(args: argparse.Namespace) -> int:
    folder = Path(args.folder).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve() if args.output else folder / '.sir_js_pack'
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Scanning: {folder}")
    funcs = extract_all_functions(str(folder))

    if not funcs:
        print("No JS/TS functions found.")
        return 1

    # Build deduplicated node store
    # Keyed by sir_sha256 — store canonical form once
    canonical_store: Dict[str, Dict] = {}
    roots = []
    namemaps = {}

    for f in funcs:
        h = f['sir_sha256']
        if h not in canonical_store:
            canonical_store[h] = {
                'sir_sha256': h,
                'params_count': len(f['params']),
                'body_src': f['body_src'],  # canonical (already stripped)
                'lang': f['lang'],
            }
        # Store name map keyed by occurrence (not hash) — fixes the overwrite bug
        namemaps[f['occurrence_key']] = {
            'sir_sha256': h,
            'name_map': f['name_map'],
            'original_params': f['params'],
        }
        roots.append({
            'sir_sha256': h,
            'file': f['rel_file'],
            'name': f['name'],
            'lineno': f['lineno'],
            'occurrence_key': f['occurrence_key'],
            'lang': f['lang'],
            'source_sha256': sha256_str(f['body_src']),
        })

    # Compute stats
    total = len(funcs)
    unique = len(canonical_store)
    dupes = total - unique

    bundle = {
        'format': 'SIR-JS-PACK',
        'version': '0.1',
        'source_path': str(folder),
        'total_functions': total,
        'unique_structures': unique,
        'duplicate_functions': dupes,
        'canonical_store': canonical_store,
        'roots': roots,
        'namemaps': namemaps,
    }

    bundle_path = out_dir / 'bundle.json'
    bundle_path.write_text(json.dumps(bundle, indent=2), encoding='utf-8')

    print(f"\nPack complete:")
    print(f"  Total functions:    {total}")
    print(f"  Unique structures:  {unique}")
    print(f"  Duplicates found:   {dupes}")
    print(f"  Bundle written to:  {bundle_path}")
    return 0


# ─────────────────────────────────────────────
#  UNPACK
# ─────────────────────────────────────────────

def cmd_unpack(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve() if args.output else bundle_path.parent / 'restored'
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
    roots = bundle['roots']
    namemaps = bundle['namemaps']
    canonical_store = bundle['canonical_store']

    # Group by file
    by_file: Dict[str, List[Dict]] = defaultdict(list)
    for r in roots:
        by_file[r['file']].append(r)

    restored_count = 0
    for rel_file, occurrences in by_file.items():
        out_path = out_dir / rel_file
        out_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [f'// Restored by SIR Engine from {rel_file}\n\n']
        seen_hashes = set()

        for occ in sorted(occurrences, key=lambda x: x['lineno']):
            h = occ['sir_sha256']
            nm_entry = namemaps.get(occ['occurrence_key'], {})
            name_map = nm_entry.get('name_map', {})
            orig_params = nm_entry.get('original_params', [])
            canonical = canonical_store.get(h, {})

            # Rehydrate original names
            body = canonical.get('body_src', '')
            for canon_name, orig_name in name_map.items():
                body = re.sub(rf'\b{re.escape(canon_name)}\b', orig_name, body)

            # Rebuild function signature
            params_str = ', '.join(orig_params) if orig_params else ''
            func_src = f'function {occ["name"]}({params_str}) {body}\n\n'

            # Only write unique structures once (deduplicated)
            if args.deduplicate and h in seen_hashes:
                lines.append(f'// DUPLICATE of {occ["name"]} — skipped (same structure)\n\n')
            else:
                lines.append(func_src)
                seen_hashes.add(h)

        out_path.write_text(''.join(lines), encoding='utf-8')
        restored_count += 1

    print(f"Restored {restored_count} file(s) to {out_dir}")
    return 0


# ─────────────────────────────────────────────
#  VERIFY
# ─────────────────────────────────────────────

def cmd_verify(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).expanduser().resolve()
    restored_dir = Path(args.restored_dir).expanduser().resolve()

    bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
    expected_hashes = {r['sir_sha256'] for r in bundle['roots']}

    # Re-hash all functions in restored dir
    actual_hashes = set()
    for fpath in find_js_files(str(restored_dir)):
        src = fpath.read_text(encoding='utf-8', errors='replace')
        try:
            funcs = extract_js_functions(src, fpath.name)
            for name, lineno, params, body_src in funcs:
                h, _ = hash_function(params, body_src)
                actual_hashes.add(h)
        except Exception:
            pass

    missing = expected_hashes - actual_hashes
    extra = actual_hashes - expected_hashes

    print(f"Expected hashes: {len(expected_hashes)}")
    print(f"Actual hashes:   {len(actual_hashes)}")

    if not missing and not extra:
        print("\nVERIFY: SUCCESS — all structural hashes match.")
        return 0

    print("\nVERIFY: MISMATCH")
    if missing:
        print(f"  Missing: {len(missing)} hash(es)")
    if extra:
        print(f"  Extra:   {len(extra)} hash(es)")
    return 1


# ─────────────────────────────────────────────
#  DIFF
# ─────────────────────────────────────────────

def cmd_diff(args: argparse.Namespace) -> int:
    folder_a = Path(args.folder_a).expanduser().resolve()
    folder_b = Path(args.folder_b).expanduser().resolve()

    print(f"Diffing:\n  A: {folder_a}\n  B: {folder_b}\n")

    funcs_a = extract_all_functions(str(folder_a))
    funcs_b = extract_all_functions(str(folder_b))

    hashes_a: Dict[str, List[str]] = defaultdict(list)
    hashes_b: Dict[str, List[str]] = defaultdict(list)

    for f in funcs_a:
        hashes_a[f['sir_sha256']].append(f"{f['rel_file']}::{f['name']}")
    for f in funcs_b:
        hashes_b[f['sir_sha256']].append(f"{f['rel_file']}::{f['name']}")

    set_a = set(hashes_a.keys())
    set_b = set(hashes_b.keys())
    common = set_a & set_b
    only_a = set_a - set_b
    only_b = set_b - set_a

    health_a = int(100 * len(set_a) / max(len(funcs_a), 1))
    health_b = int(100 * len(set_b) / max(len(funcs_b), 1))

    print(f"Folder A: {len(funcs_a)} functions, {len(set_a)} unique, health {health_a}/100")
    print(f"Folder B: {len(funcs_b)} functions, {len(set_b)} unique, health {health_b}/100")
    print(f"\nShared structures:  {len(common)}")
    print(f"Only in A:          {len(only_a)}")
    print(f"Only in B:          {len(only_b)}")

    if common:
        print("\n--- Shared (identical logic in both) ---")
        for h in sorted(common):
            a_names = ', '.join(hashes_a[h])
            b_names = ', '.join(hashes_b[h])
            print(f"  A: {a_names}  ↔  B: {b_names}")

    if only_a:
        print("\n--- Only in A (removed or not yet ported) ---")
        for h in sorted(only_a):
            print(f"  {', '.join(hashes_a[h])}")

    if only_b:
        print("\n--- Only in B (added or new) ---")
        for h in sorted(only_b):
            print(f"  {', '.join(hashes_b[h])}")

    return 0


# ─────────────────────────────────────────────
#  MERGE
# ─────────────────────────────────────────────

def cmd_merge(args: argparse.Namespace) -> int:
    folder = Path(args.folder).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve() if args.output else folder.parent / (folder.name + '_merged')
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Scanning for duplicates in: {folder}")
    funcs = extract_all_functions(str(folder))

    # Group by hash
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for f in funcs:
        groups[f['sir_sha256']].append(f)

    dupes = {h: v for h, v in groups.items() if len(v) > 1}

    if not dupes:
        print("No duplicate JS/TS functions found.")
        # Copy files as-is
        shutil.copytree(str(folder), str(out_dir), dirs_exist_ok=True)
        return 0

    print(f"Found {len(dupes)} duplicate cluster(s)\n")

    # Read all source files
    file_sources: Dict[str, str] = {}
    for fpath in find_js_files(str(folder)):
        rel = str(fpath.relative_to(folder))
        file_sources[rel] = fpath.read_text(encoding='utf-8', errors='replace')

    modified = dict(file_sources)
    utils_functions: Dict[str, str] = {}
    changes = []

    for h, occs in dupes.items():
        # First occurrence is canonical
        canonical = occs[0]
        canon_name = canonical['name']
        canon_params = canonical['params']
        canon_body = canonical['body_src']

        # Rebuild canonical function
        params_str = ', '.join(canon_params)
        canon_func_src = f'function {canon_name}({params_str}) {canon_body}'
        utils_functions[canon_name] = canon_func_src

        print(f"  Canonical: {canon_name} ({canonical['rel_file']} line {canonical['lineno']})")

        for occ in occs:
            rel = occ['rel_file']
            dup_name = occ['name']
            src = modified.get(rel, '')

            # Remove duplicate function from file
            src = remove_js_function(src, dup_name)

            # Rename all calls to canonical name
            if dup_name != canon_name:
                src = rename_js_calls(src, dup_name, canon_name)
                print(f"    Removed: {dup_name} → renamed calls to {canon_name} in {rel}")
            else:
                print(f"    Removed duplicate: {dup_name} in {rel}")

            # Add import at top
            src = add_js_import(src, canon_name)
            modified[rel] = src
            changes.append({
                'file': rel,
                'removed': dup_name,
                'canonical': canon_name,
                'canonical_file': canonical['rel_file'],
            })

    # Build utils.js
    lang_ext = '.js'  # default to JS for utils
    utils_lines = ['// utils.js — canonical functions by SIR Engine\n\n']
    for name, src in utils_functions.items():
        utils_lines.append(src + '\n\n')
    utils_src = ''.join(utils_lines)

    # Write all modified files
    for rel, src in modified.items():
        out_path = out_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(src, encoding='utf-8')

    # Write utils.js
    (out_dir / 'utils.js').write_text(utils_src, encoding='utf-8')

    # Write report
    report = {
        'total_duplicates_removed': len(changes),
        'canonical_functions_in_utils': list(utils_functions.keys()),
        'changes': changes,
    }
    (out_dir / 'sir_js_merge_report.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )

    print(f"\nMerge complete:")
    print(f"  {len(changes)} duplicate(s) removed")
    print(f"  {len(utils_functions)} canonical function(s) written to utils.js")
    print(f"  Output: {out_dir}")
    return 0


# ─────────────────────────────────────────────
#  JS function manipulation helpers
# ─────────────────────────────────────────────

def remove_js_function(src: str, func_name: str) -> str:
    """Remove a named function declaration from JS source."""
    # Match: function name(...) { ... } with brace counting
    pattern = rf'\bfunction\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{'
    match = re.search(pattern, src)
    if not match:
        # Try arrow: const name = (...) => { ... }
        pattern = rf'\b(?:const|let|var)\s+{re.escape(func_name)}\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{{'
        match = re.search(pattern, src)

    if not match:
        return src

    start = match.start()
    brace_start = src.index('{', match.start())
    depth = 0
    end = brace_start
    for i in range(brace_start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    # Remove the function plus surrounding whitespace
    removed = src[:start].rstrip() + '\n\n' + src[end:].lstrip()
    return removed


def rename_js_calls(src: str, old_name: str, new_name: str) -> str:
    """Rename all call sites of old_name to new_name."""
    return re.sub(
        rf'\b{re.escape(old_name)}\s*\(',
        f'{new_name}(',
        src
    )


def add_js_import(src: str, func_name: str) -> str:
    """Add an import statement for func_name from utils.js if not already present."""
    import_line = f"import {{ {func_name} }} from './utils.js';"
    if func_name in src and 'utils.js' in src:
        return src
    # Insert after existing imports or at top
    lines = src.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip().startswith(('import ', 'const ', '//')):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    return '\n'.join(lines)


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='sir_js_pipeline.py',
        description='SIR Engine — JS/TS pack, unpack, verify, diff, merge'
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('pack', help='Pack a JS/TS folder into a bundle')
    p.add_argument('folder')
    p.add_argument('-o', '--output', default=None)

    u = sub.add_parser('unpack', help='Restore files from a bundle')
    u.add_argument('bundle')
    u.add_argument('-o', '--output', default=None)
    u.add_argument('--deduplicate', action='store_true', help='Skip duplicate structures on restore')

    v = sub.add_parser('verify', help='Verify restored files match bundle hashes')
    v.add_argument('bundle')
    v.add_argument('restored_dir')

    d = sub.add_parser('diff', help='Structural diff between two JS/TS folders')
    d.add_argument('folder_a')
    d.add_argument('folder_b')

    m = sub.add_parser('merge', help='Find duplicates and consolidate into utils.js')
    m.add_argument('folder')
    m.add_argument('-o', '--output', default=None)

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    if args.cmd == 'pack':    return cmd_pack(args)
    if args.cmd == 'unpack':  return cmd_unpack(args)
    if args.cmd == 'verify':  return cmd_verify(args)
    if args.cmd == 'diff':    return cmd_diff(args)
    if args.cmd == 'merge':   return cmd_merge(args)
    ap.error('Unknown command')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
