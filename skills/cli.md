# SIR CLI — Skill Reference

## Overview

`sir_cli.py` is the unified CLI entrypoint. Alias it as `sir` for convenience:

```bash
alias sir="python3 /path/to/sir_cli.py"
```

---

## Commands

### `sir scan <path>`
Scan a folder or file for semantically duplicate functions (Python, JS, TS).

```bash
# Basic scan
sir scan ./src

# Include methods inside classes
sir scan ./src --include-methods

# Strict mode — exits with code 1 if duplicates found (CI/CD)
sir scan ./src --strict

# Write JSON report
sir scan ./src -o report.json

# Set minimum cluster size
sir scan ./src --min-cluster-size 3
```

Internally delegates to `sir_scan.py` for Python and `sir_js.py` for JS/TS.

---

### `sir ai-scan <path>`
Same as `scan` but also handles non-Python/JS languages via AI translation.

```bash
sir ai-scan ./src                        # auto-detect backend
sir ai-scan ./src --backend ollama       # local (free)
sir ai-scan ./src --backend anthropic    # cloud (requires ANTHROPIC_API_KEY)
```

Translates unsupported languages (Java, Go, Rust, C, C#, Swift, Kotlin, etc.) to Python using `sir_ai_translate.py`, then applies the same semantic hashing. Results are cached in `.sir_cache/` by source hash.

---

### `sir health`
Check that all dependencies and backends are available.

```bash
sir health
```

Reports: Python version, sir1.py found, Ollama status, Anthropic API key presence.

---

### `sir pack <path>`
Build a SIR pack from a Python codebase.

```bash
sir pack ./src -o my_pack/
sir pack ./src -o my_pack/ --compress    # zstd compression
```

Output folder contains:
- `nodes.json` — content-addressed AST nodes
- `roots.json` — entry points per function
- `namemaps.json` — canonical ↔ original name mappings
- `meta.json` — metadata (file list, timestamps)
- `bundle.json` — all of the above combined

Internally uses `sir_pack.py`.

---

### `sir diff <pack_a> <pack_b>`
Structural diff between two SIR packs.

```bash
sir diff old_pack/ new_pack/
```

Reports functions that are:
- **identical** — same semantic hash in both
- **added** — in new but not old
- **removed** — in old but not new

Internally uses `sir_tools.py`.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success / no duplicates found |
| 1 | Duplicates found (when `--strict` is set) |
| 2 | Bad arguments or missing dependency |

---

## JS/TS Pipeline (`sir_js_pipeline.py`)

Separate script for JS/TS pack/unpack/diff/merge (not yet wired into `sir_cli.py`):

```bash
python3 sir_js_pipeline.py pack ./src -o js_pack/
python3 sir_js_pipeline.py diff old/ new/
python3 sir_js_pipeline.py merge ./src    # consolidates dupes into utils.js
```

---

## AI Translation Details (`sir_ai_translate.py`)

- **Caching**: translations are cached in `.sir_cache/` keyed by SHA-256 of the source. Re-running on unchanged files is instant.
- **Confidence scoring**: each function is translated twice. If both translations produce the same hash → `HIGH` confidence. Mismatch → `LOW`. Currently not surfaced in CLI output.
- **Supported backends**: `ollama` (local, any model), `anthropic` (Claude API)

---

## Scan Output Format

```
FILES_SCANNED: 12
TOTAL_FUNCTIONS: 84
UNIQUE_FUNCTIONS: 71
DUPLICATE_CLUSTERS (>= 2): 3

--- Duplicate function clusters ---

a3f9... (count=3)
  - src/cart.py:14 :: calculate_total
  - src/order.py:32 :: compute_total
  - src/invoice.py:8 :: get_total
```

With `--strict`, exits code 1 if any clusters are found — useful in pre-commit hooks or CI pipelines.

---

## Related Files

| File | Role |
|------|------|
| `sir_cli.py` | Unified entrypoint |
| `sir_scan.py` | Python scanner (used by `scan`) |
| `sir_js_pipeline.py` | JS/TS pack/diff/merge pipeline |
| `sir_pack.py` | Pack builder |
| `sir_unpack.py` | Pack restorer |
| `sir_tools.py` | Verify and diff utilities |
| `sir_ai_translate.py` | AI translation backend |
