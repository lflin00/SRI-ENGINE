# SIR Hashing — Skill Reference

## What It Does

SIR hashing detects when two functions implement identical logic by:
1. **Alpha-renaming** — stripping all variable/function names, replacing with positional placeholders (`v0`, `v1`, ..., `f0`, `f1`, ...)
2. **Canonicalising** — building a content-addressed node graph from the renamed AST
3. **Hashing** — SHA-256 of the serialised node graph

Two functions with the same logic but different names produce the same hash. This is alpha equivalence from formal logic.

---

## Core Files

| File | Role |
|------|------|
| `sir1.py` | V1 engine — encode, hash, decode Python functions |
| `sir2_core.py` | V2 engine — class-level Merkle hashing |
| `sir_js.py` | JS/TS tokeniser producing the same canonical format |
| `sir_universal.py` | Cross-language hasher (Python + JS + TS → same hash) |

---

## Key Classes in `sir1.py`

### `AlphaRenamer(ast.NodeTransformer)`
Walks a Python AST and renames all identifiers deterministically:
- Module-level function names → `f0`, `f1`, ...
- Args and locals within each function → `v0`, `v1`, ...
- Recursive self-calls are renamed (the function's own name is added to its local scope after args are processed)
- Comprehension iteration variables are renamed correctly (generators visited before elt)
- Stores a reversible `name_map` for rehydration

### `SIRBuilder`
Builds a content-addressed node graph from a (renamed) AST:
- Each node is identified by `sha256(type + fields)`
- Duplicate subgraphs are stored once (structural sharing)
- Produces `{root, nodes}` — the canonical SIR representation

### `CanonConfig(mode)`
- `mode="exact"` — preserves original identifiers, still normalises formatting
- `mode="semantic"` — alpha-renames everything; use this for duplicate detection

---

## Public API

```python
from sir1 import encode_to_sir, sir_hash, CanonConfig

# Hash a function for duplicate detection
sir = encode_to_sir(source_code, CanonConfig(mode="semantic"))
h = sir_hash(sir)  # hex SHA-256 string

# Two functions with different names but same logic → same hash
assert sir_hash(encode_to_sir("def foo(x): return x+1", cfg)) == \
       sir_hash(encode_to_sir("def bar(y): return y+1", cfg))
```

---

## SIR JSON Format

```json
{
  "format": "SIR-1",
  "version": "0.2",
  "mode": "semantic",
  "root": "<node_id_sha256>",
  "nodes": {
    "<node_id>": {"t": "<ASTNodeType>", "f": { "<field>": "<value_or_ref>" }}
  },
  "source_sha256": "<sha256_of_original_source>",
  "sir_sha256": "<hash_of_root_and_nodes>",
  "name_map": { "functions": [ { "canon_func": "f0", "orig_func": "foo", "canon_to_orig": {} } ] }
}
```

`sir_sha256` is computed from `{root, nodes}` only — it is the canonical fingerprint.

---

## V2: Class-Level Hashing (`sir2_core.py`)

### Merkle Class Hash
1. Hash each method independently using `AlphaRenamer` + `_SelfAttrNormalizer`
2. Sort the method hashes
3. `class_hash = sha256(json(sorted_method_hashes))`

Method order doesn't matter. Two classes with the same methods in any order hash identically.

### `_SelfAttrNormalizer`
Runs before `AlphaRenamer`. Replaces `self.attr` names with `self.a0`, `self.a1`, ... in encounter order. Ensures `self.total` and `self.sum` canonicalise to the same thing.

### Inheritance Hashing
`apply_inheritance_hashes(classes)` folds parent class hashes into child hashes:
```
child_hash = sha256({"methods": sorted_method_hashes, "parents": sorted_parent_hashes})
```
Only works for parents found in the same scan. Single-level only.

### Public API

```python
from sir2_core import extract_classes, scan_for_class_dupes, scan_files_for_classes

# Single file
classes = extract_classes(source_str, "myfile.py")

# Multiple files — main entry point
exact_clusters, similar_pairs, total = scan_files_for_classes(
    {"a.py": src_a, "b.py": src_b},
    min_similarity=0.75,   # 1.0 = exact only, <1.0 = partial matches too
    apply_inheritance=True,
)
```

---

## CLI Hashing

```bash
# Hash a file (semantic mode)
python3 sir1.py hash myfile.py --mode semantic

# Hash from stdin
echo "def foo(x): return x+1" | python3 sir1.py hash - --mode semantic

# Encode to SIR JSON
python3 sir1.py encode myfile.py --mode semantic -o out.sir.json

# Decode SIR JSON back to Python
python3 sir1.py decode out.sir.json -o restored.py --rehydrate
```

---

## Known Limitations

- Alpha-renaming is Python-AST-based; JS/TS use a separate token-level approach (`sir_js.py`, `sir_universal.py`)
- `self.attr` normalisation is per-method (fresh counter per method call) — two methods in the same class share no attr mapping
- Inheritance hashing is one level deep; deeply nested hierarchies only get one parent folded in
- Class extraction uses `ast.walk` so nested class definitions are also extracted as top-level entries
