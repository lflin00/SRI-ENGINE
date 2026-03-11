# SIR Engine — Project Guide for Claude

## What is SIR Engine?

SIR Engine detects when two functions implement identical logic — across any programming language. It works by canonicalizing code (stripping names, normalizing structure) and hashing the result with SHA-256. Matching hashes = same logic, regardless of language, variable names, or formatting.

The approach is based on **alpha equivalence** from formal logic: two programs are equivalent if one can be made identical to the other by a consistent renaming of variables.

**Supported natively:** Python, JavaScript, TypeScript
**Via AI translation:** 25+ languages (Java, Go, Rust, C, C#, Swift, Kotlin, etc.)

---

## V1 vs V2

### V1 — Function-level detection (the current main system)

Everything in the root directory that doesn't say "v2" is V1. V1 works at the **function level**:
1. Extract all functions from `.py`, `.js`, `.ts` files
2. Canonicalize: strip names, normalize structure
3. Hash: SHA-256 of canonical form
4. Group matching hashes → duplicate clusters

V1 is production-ready. It powers the web app, CLI, and VS Code extension.

### V2 — Class-level detection (new, additive)

`sir2_core.py` is V2. It extends the same concept to **classes**:
- Uses a Merkle tree: hash each method independently, then combine into a class hash
- Detects exact class duplicates (same hash) and partial duplicates (% of matching method hashes)
- Normalizes `self.attr` references across classes
- **Does not modify any V1 files.** Imports only stdlib + `AlphaRenamer` from `sir1.py`.

V2 is implemented and tested but not yet wired into the CLI or UI.

---

## File Map

### Core Engine

| File | What it does |
|------|-------------|
| `sir1.py` | **V1 core engine** (standalone script). Encodes Python → SIR JSON node graph, hashes it, and decodes back to Python. Two modes: `exact` (preserves names) and `semantic` (alpha-renames for canonical equality). The original prototype — still used by `sir_scan.py`, `sir_pack.py`, `sir_tools.py`. |
| `sir2_core.py` | **V2 class-level engine** (new). Adds `ClassAlphaRenamer`, Merkle class hashing, class similarity scoring, and `scan_for_class_dupes`. Standalone, does not break V1. |
| `sir/` | **Importable Python package** version of V1. Refactored from `sir1.py` into clean modules so other tools can `from sir.core import encode, hash_source, decode_sir`. Used by `sir_ui.py`. |
| `sir/core.py` | Core encode/hash/decode logic extracted from `sir1.py`. Public API. |
| `sir/canonicalize.py` | (stub/placeholder) Canonicalization logic. |
| `sir/graph.py` | (stub/placeholder) Node graph data structures. |
| `sir/index.py` | (stub/placeholder) Hash index/lookup. |
| `sir/storage.py` | (stub/placeholder) Pack storage layer. |
| `sir/__init__.py` | Exports `encode`, `hash_source`, `decode_sir`, `sir_hash`, etc. |

### Language Support

| File | What it does |
|------|-------------|
| `sir_js.py` | JS/TS parser. Tokenizes JS/TS functions into the same canonical node format as `sir1.py`. Supports function declarations, arrow functions, method definitions, async variants. Strips TypeScript type annotations before parsing. |
| `sir_js_check.py` | Earlier/simpler version of the JS parser (same docstring as `sir_js.py` — appears to be a snapshot or duplicate). |
| `sir_universal.py` | Cross-language canonical hasher. Produces matching hashes for equivalent Python, JS, and TS functions by compiling all three to a common token sequence (identifiers → `v0, v1...`, strings → `STR`, numbers → `NUM`). |
| `sir_ai_translate.py` | AI backend for non-Python/JS languages. Translates any language to Python using Ollama (local, free) or Anthropic API. Includes: validation (rejects invalid Python), caching (saves by source hash to `.sir_cache/`), confidence scoring (translates twice; matching hashes = HIGH confidence). |

### CLI & Scanning

| File | What it does |
|------|-------------|
| `sir_cli.py` | **Unified CLI entrypoint.** Commands: `scan`, `ai-scan`, `health`, `pack`, `diff`. Colored terminal output, `--strict` flag for CI/CD (exit 1 if duplicates found). Intended to be aliased as `sir` in the shell. |
| `sir_scan.py` | **Original scan script.** Recursively scans `.py` files, extracts top-level functions (optionally methods), computes semantic hash for each, groups duplicate clusters. Writes JSON reports. Used internally by `sir_cli.py`. |

### Pack / Unpack / Diff

| File | What it does |
|------|-------------|
| `sir_pack.py` | Builds a SIR pack from a Python codebase: encodes every function in semantic mode, deduplicates nodes globally (content-addressed), writes `nodes.json`, `roots.json`, `namemaps.json`, `meta.json`, and a combined `bundle.json`. Optionally compresses with zstd. |
| `sir_pack1.py` | Appears to be an older/duplicate version of `sir_pack.py` with identical docstring. |
| `sir_unpack.py` | Restores functions from a SIR pack back into `.py` files. Supports `list`, `restore-root`, `restore-occurrence`, `restore-all` commands. Handles multiple occurrences sharing the same root (true duplicates) with unique filenames. |
| `sir_tools.py` | `verify` (re-hash restored files, confirm match against pack's `sir_sha256`) and `diff` (structural diff between two folders using semantic hashes: identical / added / removed). |
| `sir_js_pipeline.py` | Same pack/unpack/verify/diff/merge pipeline but for JS/TS files. Commands: `pack`, `unpack`, `verify`, `diff`, `merge` (consolidates duplicates into `utils.js`). |

### Web App

| File | What it does |
|------|-------------|
| `sir_ui.py` | **Streamlit web app.** Browser-based UI with tabs for: Scan (upload Python/JS/TS files, detect duplicates), Pack (export semantic fingerprints as `.sir.json`), Diff (compare two packs), Merge (auto-merge or manual-merge duplicate clusters, download cleaned files + HTML report). Detects local vs Streamlit Cloud, switches AI backend accordingly (Ollama locally, Anthropic in cloud). |

### Utility / Experimental

| File | What it does |
|------|-------------|
| `zenith_wrap.py` | Encode/compress/decode tool for Jarvis/Zenith. Three modes: SIR mode for `.py` files (lossless, via `sir1.py`), zstd mode for other text (lossless), LLM+zstd mode for maximum compression (best-effort fidelity). All produce `.zwrap` files. |
| `out.py` | Output/scratch file (likely a decoded/restored function used for testing). |
| `test_restored.py` | Minimal test file containing a single function (`calculate_total`) — used to verify round-trip encode/decode. |
| `test_sir2.py` | **Test suite for V2.** Tests: exact class duplicates, partial duplicates, unique classes, inheritance Merkle hashing, method order independence. |

### Benchmark Data

| Path | What it contains |
|------|-----------------|
| `bench/raw/` | 100+ raw Python files (`f0000.py` – `f0NNN.py`) used as a benchmark corpus. |
| `bench/sir/` | Pre-computed `.sir.json` files for each raw benchmark file. |
| `bench/sir_all.json` | Combined SIR for all benchmark files. |
| `bench/global_bundle.json` | Global deduplicated pack for the benchmark corpus. |

### Demo

| Path | What it contains |
|------|-----------------|
| `demo_scan/a.py`, `demo_scan/b.py` | Small sample files with duplicate functions for testing the scanner. |

---

## Architecture Summary

```
Source code (any language)
        │
        ├─ Python/JS/TS ──→ sir1.py / sir_js.py   (native parsers)
        │
        └─ Other languages ──→ sir_ai_translate.py  (LLM → Python)
                                    │
                                    ▼
                          AlphaRenamer / ClassAlphaRenamer
                          (strip all names → canonical form)
                                    │
                                    ▼
                          SHA-256(ast.dump() / token sequence)
                                    │
                                    ▼
                          Hash comparison → duplicate clusters
```

---

## What's Built

- [x] V1 function-level duplicate detection (Python, JS, TS)
- [x] AI-powered cross-language detection (Ollama + Claude API)
- [x] CLI (`sir scan`, `sir diff`, `sir health`, `sir pack`, `sir ai-scan`)
- [x] Streamlit web app with scan, pack, diff, merge tabs
- [x] Auto-merge and manual-merge with downloadable output + HTML report
- [x] VS Code extension (`.vsix`, v0.0.2)
- [x] SIR pack format (portable semantic fingerprints)
- [x] JS/TS pipeline (pack, unpack, verify, diff, merge)
- [x] Universal cross-language hasher (`sir_universal.py`)
- [x] V2 class-level detection engine (`sir2_core.py`)
- [x] V2 test suite (`test_sir2.py`)

## What's Pending / TODO

- [ ] Wire V2 class detection into the CLI (`sir scan` should also report class duplicates)
- [ ] Wire V2 class detection into the Streamlit UI
- [ ] `sir/canonicalize.py`, `sir/graph.py`, `sir/index.py`, `sir/storage.py` are stubs — need implementation or deletion if unused
- [ ] `sir_pack1.py` appears to be a duplicate of `sir_pack.py` — should be removed or reconciled
- [ ] `sir_js_check.py` appears to be a duplicate of `sir_js.py` — should be removed or reconciled
- [ ] VS Code extension doesn't use the `sir/` package yet — still calls `sir_cli.py` as subprocess
- [ ] Confidence scoring from `sir_ai_translate.py` not yet surfaced in the UI
- [ ] `bench/` benchmark data has no runner script — no automated benchmarking workflow

---

## Key Concepts

**Alpha renaming:** Replace all variable/function names with positional placeholders (`v0`, `v1`, ...). Two functions that differ only in names hash identically.

**Semantic hash:** SHA-256 of `ast.dump()` of the alpha-renamed AST. Deterministic and language-agnostic (once translated to Python).

**SIR JSON:** The canonical node graph representation. Contains `nodes` (content-addressed AST nodes), `root` (entry-point node id), and optionally `name_map` (original → canonical name mapping for rehydration).

**Pack:** A folder containing `bundle.json` — a globally deduplicated index of all functions in a codebase. Portable: can compare two codebases without sharing source.

**Merkle class hash (V2):** Hash each method individually, sort the method hashes, then hash the sorted list. Produces a class fingerprint that is independent of method order.
