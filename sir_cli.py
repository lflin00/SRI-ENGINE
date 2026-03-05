#!/usr/bin/env python3
"""
sir_cli.py — SIR Engine Command Line Interface

A unified CLI for scanning codebases, detecting duplicates, and running
AI-powered cross-language analysis directly from the terminal.

USAGE
-----
# Scan a folder for duplicate functions
sir scan ./my_project

# Scan with minimum cluster size
sir scan ./my_project --min 3

# Save results to JSON
sir scan ./my_project --output report.json

# Scan and fail if duplicates found (for CI/CD pipelines)
sir scan ./my_project --strict

# AI scan for non-Python/JS files
sir ai-scan ./my_project --backend ollama --model codellama:7b

# Show health score only
sir health ./my_project

# Pack a codebase
sir pack ./my_project --output bundle.json

# Diff two folders
sir diff ./version1 ./version2

INSTALL
-------
Add to PATH or run directly:
    python3 sir_cli.py scan ./my_project

Or make executable:
    chmod +x sir_cli.py
    ./sir_cli.py scan ./my_project
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
#  Colour output helpers
# ─────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
DIM    = "\033[2m"

def _c(text: str, colour: str) -> str:
    """Wrap text in colour if stdout is a terminal."""
    if sys.stdout.isatty():
        return f"{colour}{text}{RESET}"
    return text

def ok(msg: str)   -> None: print(_c(f"  ✓  {msg}", GREEN))
def err(msg: str)  -> None: print(_c(f"  ✗  {msg}", RED))
def warn(msg: str) -> None: print(_c(f"  ⚠  {msg}", YELLOW))
def info(msg: str) -> None: print(_c(f"  →  {msg}", CYAN))
def header(msg: str) -> None:
    print()
    print(_c(f"{'─' * 60}", DIM))
    print(_c(f"  {msg}", BOLD))
    print(_c(f"{'─' * 60}", DIM))


# ─────────────────────────────────────────────
#  Core hashing (mirrors sir/core.py)
# ─────────────────────────────────────────────

def _try_import_sir():
    """Try to import sir/core.py from standard locations."""
    # Try relative to this file first
    here = Path(__file__).parent
    candidates = [here, here / "SIR_MAIN", here.parent / "SIR_MAIN"]
    for c in candidates:
        if (c / "sir" / "core.py").exists():
            sys.path.insert(0, str(c))
            try:
                from sir.core import hash_source, encode
                return hash_source, encode
            except ImportError:
                pass
    return None, None


def _hash_python(source: str) -> Optional[str]:
    hash_source, _ = _try_import_sir()
    if hash_source:
        try:
            return hash_source(source, mode="semantic")
        except Exception:
            pass
    # Fallback: simple AST-based hash
    try:
        tree = ast.parse(source)
        return hashlib.sha256(ast.dump(tree).encode()).hexdigest()
    except Exception:
        return None


# ─────────────────────────────────────────────
#  File discovery
# ─────────────────────────────────────────────

PY_EXTS  = {".py"}
JS_EXTS  = {".js", ".ts", ".jsx", ".tsx"}
AI_EXTS  = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".java", ".rs", ".go", ".rb", ".php", ".swift",
    ".kt", ".scala", ".cs", ".lua", ".dart",
    ".hs", ".ex", ".exs", ".ml", ".fs", ".fsx",
    ".jl", ".nim", ".zig", ".r", ".pl",
}
ALL_EXTS = PY_EXTS | JS_EXTS | AI_EXTS

SIR_IGNORE = ".sir_ignore"

def _load_ignore_patterns(root: Path) -> List[str]:
    ignore_file = root / SIR_IGNORE
    if ignore_file.exists():
        lines = ignore_file.read_text().splitlines()
        return [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    return []

def _is_ignored(path: Path, patterns: List[str], root: Path) -> bool:
    rel = str(path.relative_to(root))
    for p in patterns:
        if p in rel or rel.endswith(p):
            return True
    return False

def discover_files(root: Path, exts: set, recursive: bool = True) -> List[Path]:
    patterns = _load_ignore_patterns(root)
    results = []
    if recursive:
        for ext in exts:
            for f in root.rglob(f"*{ext}"):
                if not _is_ignored(f, patterns, root):
                    results.append(f)
    else:
        for ext in exts:
            for f in root.glob(f"*{ext}"):
                if not _is_ignored(f, patterns, root):
                    results.append(f)
    return sorted(results)


# ─────────────────────────────────────────────
#  Python function extraction
# ─────────────────────────────────────────────

def extract_python_functions(source: str) -> List[Tuple[str, int, str]]:
    """Extract (name, lineno, source) for each top-level function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno - 1
            end = node.end_lineno
            src = "\n".join(lines[start:end])
            results.append((node.name, node.lineno, src))
    return results


# ─────────────────────────────────────────────
#  JS/TS function extraction
# ─────────────────────────────────────────────

def _try_import_sir_js():
    """Try to import sir_js.py from standard locations."""
    here = Path(__file__).parent
    candidates = [here, here / "SIR_MAIN", here.parent / "SIR_MAIN"]
    for c in candidates:
        if (c / "sir_js.py").exists():
            sys.path.insert(0, str(c))
            try:
                from sir_js import hash_js_source
                return hash_js_source
            except ImportError:
                pass
    return None


def extract_js_hashes(source: str, filename: str):
    hash_js_source = _try_import_sir_js()
    if not hash_js_source:
        return []
    try:
        return hash_js_source(source, filename)
    except Exception:
        return []


def extract_functions_universal(f, root):
    """Returns (name, lineno, hash, lang) for any supported file."""
    ext = f.suffix.lower()
    source = f.read_text(encoding="utf-8", errors="ignore")
    rel = str(f.relative_to(root) if root.is_dir() else f.name)
    results = []
    if ext == ".py":
        for name, lineno, src in extract_python_functions(source):
            h = _hash_python(src)
            if h:
                results.append((name, lineno, h, "Python"))
    elif ext in {".js", ".jsx"}:
        for name, lineno, h in extract_js_hashes(source, rel):
            results.append((name, lineno, h, "JavaScript"))
    elif ext in {".ts", ".tsx"}:
        for name, lineno, h in extract_js_hashes(source, rel):
            results.append((name, lineno, h, "TypeScript"))
    return results


# ─────────────────────────────────────────────
#  Health score
# ─────────────────────────────────────────────

def compute_health(total_functions: int, duplicate_functions: int) -> int:
    if total_functions == 0:
        return 100
    ratio = duplicate_functions / total_functions
    return max(0, round((1 - ratio) * 100))


# ─────────────────────────────────────────────
#  scan command
# ─────────────────────────────────────────────

def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        err(f"Path not found: {root}")
        return 1

    header(f"SIR Engine — Scanning {root.name}/")

    # Discover files
    if root.is_file():
        files = [root]
    else:
        files = discover_files(root, PY_EXTS | JS_EXTS, recursive=not args.no_recurse)

    if not files:
        warn("No Python files found.")
        return 0

    info(f"Found {len(files)} Python file(s)")

    # Extract and hash functions
    hash_groups: Dict[str, List[dict]] = defaultdict(list)
    total_functions = 0
    errors = 0

    for f in files:
        try:
            for name, lineno, h, lang in extract_functions_universal(f, root):
                total_functions += 1
                rel = str(f.relative_to(root) if root.is_dir() else f.name)
                hash_groups[h].append({
                    "file": rel,
                    "name": name,
                    "lineno": lineno,
                    "lang": lang,
                })
        except Exception as e:
            errors += 1

    # Find duplicates
    duplicates = {h: v for h, v in hash_groups.items() if len(v) >= args.min}
    duplicate_functions = sum(len(v) for v in duplicates.values())
    health = compute_health(total_functions, duplicate_functions)

    # Print summary
    print()
    cols = [
        ("Files",      str(len(files))),
        ("Functions",  str(total_functions)),
        ("Duplicates", str(len(duplicates))),
        ("Health",     f"{health}/100"),
    ]
    for label, value in cols:
        colour = GREEN if label == "Health" and health >= 80 else \
                 YELLOW if label == "Health" and health >= 60 else \
                 RED if label == "Health" else BOLD
        print(f"  {_c(label, DIM):<20} {_c(value, colour)}")

    # Print duplicate clusters
    if duplicates:
        print()
        print(_c(f"  Duplicate clusters ({len(duplicates)} found):", BOLD))
        for h, occurrences in sorted(duplicates.items(), key=lambda x: -len(x[1])):
            print()
            print(f"  {_c('●', RED)}  {len(occurrences)} copies  {_c(h[:16] + '...', DIM)}")
            for o in occurrences:
                lineno_str = f"line {o['lineno']}"
                lang_badge = _c(f"[{o.get('lang', '')}]", DIM) if o.get('lang') else ""
                print(f"     {_c(o['name'], CYAN)}  {lang_badge}  {_c(o['file'], BOLD)}  {_c(lineno_str, DIM)}")
    else:
        print()
        ok("No duplicate functions found.")

    # Save report
    if args.output:
        report = {
            "scanned_path": str(root),
            "files": len(files),
            "total_functions": total_functions,
            "duplicate_clusters": len(duplicates),
            "health_score": health,
            "duplicates": [
                {"hash": h[:16], "occurrences": v}
                for h, v in duplicates.items()
            ]
        }
        Path(args.output).write_text(json.dumps(report, indent=2))
        ok(f"Report saved to {args.output}")

    # Strict mode — exit 1 if duplicates found (for CI/CD)
    if args.strict and duplicates:
        print()
        err(f"Strict mode: {len(duplicates)} duplicate cluster(s) found. Resolve before merging.")
        return 1

    return 0


# ─────────────────────────────────────────────
#  health command
# ─────────────────────────────────────────────

def cmd_health(args: argparse.Namespace) -> int:
    """Quick health score — just the number, nothing else."""
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        err(f"Path not found: {root}")
        return 1

    files = [root] if root.is_file() else discover_files(root, PY_EXTS | JS_EXTS)
    hash_groups: Dict[str, List] = defaultdict(list)
    total = 0

    for f in files:
        try:
            source = f.read_text(encoding="utf-8", errors="ignore")
            for name, lineno, src in extract_python_functions(source):
                total += 1
                h = _hash_python(src)
                if h:
                    hash_groups[h].append(name)
        except Exception:
            pass

    dupes = sum(len(v) for v in hash_groups.values() if len(v) >= 2)
    health = compute_health(total, dupes)

    colour = GREEN if health >= 80 else YELLOW if health >= 60 else RED
    print(_c(f"  Health: {health}/100", colour))
    print(_c(f"  {total} functions, {dupes} duplicates", DIM))
    return 0


# ─────────────────────────────────────────────
#  ai-scan command
# ─────────────────────────────────────────────

def cmd_ai_scan(args: argparse.Namespace) -> int:
    """AI-powered scan for non-Python/JS languages."""
    try:
        from sir_ai_translate import (
            translate_to_python, detect_language,
            is_ai_language, extract_raw_functions,
            CONFIDENCE_ICON
        )
    except ImportError:
        err("sir_ai_translate.py not found. Make sure it's in the same directory.")
        return 1

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        err(f"Path not found: {root}")
        return 1

    header(f"SIR Engine — AI Scan {root.name}/")

    files = [root] if root.is_file() else discover_files(root, AI_EXTS)
    if not files:
        warn("No AI-supported language files found.")
        return 0

    info(f"Found {len(files)} file(s) — translating via {args.backend}")

    hash_groups: Dict[str, List[dict]] = defaultdict(list)
    total = 0
    failed = 0
    low_conf = 0

    for f in files:
        lang = detect_language(str(f))
        if not lang:
            continue
        try:
            source = f.read_text(encoding="utf-8", errors="ignore")
            funcs = extract_raw_functions(source, lang)
            for name, lineno, raw_src in funcs:
                total += 1
                result = translate_to_python(
                    raw_src, lang,
                    backend=args.backend,
                    api_key=args.api_key or "",
                    ollama_model=args.model,
                    ollama_host=args.host,
                )
                conf = result.get("confidence", "FAILED")
                py_src = result.get("python_src", "")

                if conf == "FAILED" or not py_src:
                    failed += 1
                    continue
                if conf == "LOW":
                    low_conf += 1

                h = _hash_python(py_src)
                if h:
                    icon = CONFIDENCE_ICON.get(conf, "🟡")
                    hash_groups[h].append({
                        "file": str(f.relative_to(root) if root.is_dir() else f.name),
                        "name": name,
                        "lineno": lineno,
                        "lang": lang,
                        "confidence": conf,
                        "icon": icon,
                        "cache_hit": result.get("cache_hit", False),
                    })
                    print(f"  {icon} {_c(name, CYAN)} ({lang}) {_c('cached' if result.get('cache_hit') else '', DIM)}")
        except Exception as e:
            failed += 1

    # Find duplicates
    duplicates = {h: v for h, v in hash_groups.items() if len(v) >= args.min}
    duplicate_functions = sum(len(v) for v in duplicates.values())
    health = compute_health(total, duplicate_functions)

    print()
    cols = [
        ("Files",       str(len(files))),
        ("Functions",   str(total)),
        ("Translated",  str(total - failed)),
        ("Failed",      str(failed)),
        ("Low conf",    str(low_conf)),
        ("Duplicates",  str(len(duplicates))),
        ("Health",      f"{health}/100"),
    ]
    for label, value in cols:
        colour = RED if label == "Failed" and failed > 0 else \
                 YELLOW if label == "Low conf" and low_conf > 0 else \
                 GREEN if label == "Health" and health >= 80 else BOLD
        print(f"  {_c(label, DIM):<20} {_c(value, colour)}")

    if duplicates:
        print()
        print(_c(f"  Duplicate clusters ({len(duplicates)} found):", BOLD))
        for h, occurrences in sorted(duplicates.items(), key=lambda x: -len(x[1])):
            print()
            print(f"  {_c('●', RED)}  {len(occurrences)} copies  {_c(h[:16] + '...', DIM)}")
            for o in occurrences:
                lineno_str = f"line {o['lineno']}"
                cache = _c(" (cached)", DIM) if o.get("cache_hit") else ""
                print(f"     {icon} {_c(o['name'], CYAN)}  [{o['lang']}]  {_c(o['file'], BOLD)}  {_c(lineno_str, DIM)}{cache}")
    else:
        print()
        ok("No duplicate functions found across AI-translated files.")

    if args.output:
        report = {
            "scanned_path": str(root),
            "backend": args.backend,
            "files": len(files),
            "total_functions": total,
            "failed_translations": failed,
            "duplicate_clusters": len(duplicates),
            "health_score": health,
            "duplicates": [
                {"hash": h[:16], "occurrences": v}
                for h, v in duplicates.items()
            ]
        }
        Path(args.output).write_text(json.dumps(report, indent=2))
        ok(f"Report saved to {args.output}")

    if args.strict and duplicates:
        err(f"Strict mode: {len(duplicates)} duplicate cluster(s) found.")
        return 1

    return 0


# ─────────────────────────────────────────────
#  diff command
# ─────────────────────────────────────────────

def cmd_diff(args: argparse.Namespace) -> int:
    """Compare two folders semantically."""
    root_a = Path(args.path_a).expanduser().resolve()
    root_b = Path(args.path_b).expanduser().resolve()

    if not root_a.exists():
        err(f"Path A not found: {root_a}")
        return 1
    if not root_b.exists():
        err(f"Path B not found: {root_b}")
        return 1

    header(f"SIR Engine — Diff  {root_a.name}/  vs  {root_b.name}/")

    def get_hashes(root: Path) -> Dict[str, str]:
        """Returns {hash: function_name} for all functions in root."""
        result = {}
        files = [root] if root.is_file() else discover_files(root, PY_EXTS | JS_EXTS)
        for f in files:
            try:
                source = f.read_text(encoding="utf-8", errors="ignore")
                for name, lineno, src in extract_python_functions(source):
                    h = _hash_python(src)
                    if h:
                        rel = str(f.relative_to(root) if root.is_dir() else f.name)
                        result[h] = f"{rel}::{name}::L{lineno}"
            except Exception:
                pass
        return result

    hashes_a = get_hashes(root_a)
    hashes_b = get_hashes(root_b)

    set_a = set(hashes_a.keys())
    set_b = set(hashes_b.keys())

    identical = set_a & set_b
    only_in_a = set_a - set_b
    only_in_b = set_b - set_a

    print()
    print(f"  {_c('Identical functions', GREEN):<35} {len(identical)}")
    print(f"  {_c(f'Only in {root_a.name}/', YELLOW):<35} {len(only_in_a)}")
    print(f"  {_c(f'Only in {root_b.name}/', BLUE):<35} {len(only_in_b)}")

    if identical:
        print()
        print(_c("  Shared logic:", BOLD))
        for h in sorted(identical):
            print(f"    {_c('=', GREEN)}  {hashes_a[h]}  ↔  {hashes_b[h]}")

    if only_in_a:
        print()
        print(_c(f"  Only in {root_a.name}/:", BOLD))
        for h in sorted(only_in_a):
            print(f"    {_c('+', YELLOW)}  {hashes_a[h]}")

    if only_in_b:
        print()
        print(_c(f"  Only in {root_b.name}/:", BOLD))
        for h in sorted(only_in_b):
            print(f"    {_c('-', BLUE)}  {hashes_b[h]}")

    return 0


# ─────────────────────────────────────────────
#  Argument parser
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sir",
        description="SIR Engine — Semantic duplicate detection for any programming language.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sir scan ./my_project
  sir scan ./my_project --min 3 --output report.json
  sir scan ./my_project --strict
  sir ai-scan ./my_project --backend ollama --model codellama:7b
  sir health ./my_project
  sir diff ./v1 ./v2
        """
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── scan ──────────────────────────────────
    p_scan = sub.add_parser("scan", help="Scan Python/JS/TS files for duplicate functions")
    p_scan.add_argument("path", help="File or directory to scan")
    p_scan.add_argument("--min", type=int, default=2, metavar="N",
                        help="Minimum cluster size to report (default: 2)")
    p_scan.add_argument("--output", "-o", metavar="FILE",
                        help="Save JSON report to FILE")
    p_scan.add_argument("--strict", action="store_true",
                        help="Exit with code 1 if duplicates found (for CI/CD)")
    p_scan.add_argument("--no-recurse", action="store_true",
                        help="Do not recurse into subdirectories")
    p_scan.set_defaults(func=cmd_scan)

    # ── ai-scan ────────────────────────────────
    p_ai = sub.add_parser("ai-scan", help="AI-powered scan for C++, Java, Rust, Go, and 20+ other languages")
    p_ai.add_argument("path", help="File or directory to scan")
    p_ai.add_argument("--backend", choices=["ollama", "anthropic"], default="ollama",
                      help="AI backend to use (default: ollama)")
    p_ai.add_argument("--model", default="codellama:7b", metavar="MODEL",
                      help="Ollama model name (default: codellama:7b)")
    p_ai.add_argument("--host", default="http://localhost:11434", metavar="URL",
                      help="Ollama host URL (default: http://localhost:11434)")
    p_ai.add_argument("--api-key", metavar="KEY",
                      help="Anthropic API key (required if --backend anthropic)")
    p_ai.add_argument("--min", type=int, default=2, metavar="N",
                      help="Minimum cluster size to report (default: 2)")
    p_ai.add_argument("--output", "-o", metavar="FILE",
                      help="Save JSON report to FILE")
    p_ai.add_argument("--strict", action="store_true",
                      help="Exit with code 1 if duplicates found (for CI/CD)")
    p_ai.set_defaults(func=cmd_ai_scan)

    # ── health ─────────────────────────────────
    p_health = sub.add_parser("health", help="Show health score for a codebase")
    p_health.add_argument("path", help="File or directory to score")
    p_health.set_defaults(func=cmd_health)

    # ── diff ───────────────────────────────────
    p_diff = sub.add_parser("diff", help="Compare two codebases semantically")
    p_diff.add_argument("path_a", help="First directory")
    p_diff.add_argument("path_b", help="Second directory")
    p_diff.set_defaults(func=cmd_diff)

    return parser


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
