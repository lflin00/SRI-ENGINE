# SIR Testing — Skill Reference

## Test Files

| File | What It Tests |
|------|--------------|
| `sir_benchmark.py` | V1 function-level detection accuracy (24 cases, known duplicates + non-duplicates) |
| `test_sir2.py` | V2 class-level detection (5 tests: exact, partial, unique, method order, inheritance) |

---

## Running Tests

```bash
# V1 benchmark — summary
python3 sir_benchmark.py

# V1 benchmark — per-test breakdown
python3 sir_benchmark.py --verbose

# V1 benchmark — machine-readable JSON
python3 sir_benchmark.py --json
python3 sir_benchmark.py --json -o results.json

# V2 test suite
python3 test_sir2.py
```

Both scripts exit with code 1 if any test fails, suitable for CI.

---

## V1 Benchmark (`sir_benchmark.py`)

### Structure
Each test case is a `TestCase(label, description, func_a, func_b, expected_duplicate)`.

Two pools:
- `DUPLICATE_PAIRS` — 12 pairs that should hash identically (same logic, different names)
- `NON_DUPLICATE_PAIRS` — 12 pairs that should hash differently (genuinely different logic)

### Metrics Reported
- **TP** — duplicate correctly detected
- **TN** — non-duplicate correctly rejected
- **FP** — non-duplicate wrongly flagged (false alarm)
- **FN** — duplicate missed
- Precision, Recall, F1, Accuracy, wall time, avg time per pair

### Baseline Results (after fixes)
```
Accuracy:   100%  (24/24)
Precision:  100%
Recall:     100%
F1:         1.000
Avg time:   ~0.7 ms per pair
```

### Adding a Test Case

```python
# In sir_benchmark.py, add to DUPLICATE_PAIRS or NON_DUPLICATE_PAIRS:
TestCase(
    label="my_new_case",
    description="What this tests",
    func_a="""
        def foo(x, y):
            return x + y
    """,
    func_b="""
        def bar(a, b):
            return a + b
    """,
    expected_duplicate=True,   # or False
),
```

Functions are passed as strings and can be multi-line with leading indentation — `textwrap.dedent` is applied automatically.

---

## V2 Test Suite (`test_sir2.py`)

### Tests

| # | Name | What it checks |
|---|------|---------------|
| 1 | `test_exact_duplicate` | `ShoppingCart` == `OrderBasket` (same logic, different names) |
| 2 | `test_partial_duplicate` | `ShoppingCart` vs `Inventory` → 75% similar |
| 3 | `test_unique_classes_dont_match` | `ShoppingCart` vs `FileParser` → no match |
| 4 | `test_method_order_independence` | `Calculator` == `MathHelper` (methods in different order) |
| 5 | `test_inheritance_hashing` | `Dog(Animal)` ≠ `AndroidDog(Robot)` despite identical `bark()` |

### Adding a V2 Test

```python
SRC_MY_CLASS = '''
class MyClass:
    def method(self, x):
        return x * 2
'''

def test_my_case():
    exact, similar, total = scan_files_for_classes(
        {"a.py": SRC_A, "b.py": SRC_B},
        min_similarity=1.0,
    )
    result = len(exact) == 1
    print(f"  My case: {'✓ PASS' if result else '✗ FAIL'}")
    return result
```

Then add `test_my_case()` to the `results` list at the bottom.

---

## How `sir_benchmark.py` Loads `sir1`

It imports `sir1` dynamically via `importlib` and registers it in `sys.modules` before `exec_module` — required for Python 3.14's dataclass decorator to resolve `__module__`:

```python
spec = importlib.util.spec_from_file_location("sir1", sir1_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["sir1"] = mod   # must come before exec_module
spec.loader.exec_module(mod)
```

---

## Known Engine Limitations (Tracked by Benchmark)

These were bugs that the benchmark caught and that are now fixed in `sir1.py`:

1. **Recursive self-calls** — recursive function calls were not getting alpha-renamed because the function name wasn't in the local scope. Fixed: function name is registered in its own local scope after args are allocated.

2. **Comprehension `elt` visited before `generators`** — `generic_visit` visits `ListComp.elt` before `ListComp.generators`, so iteration variables weren't in scope when the output expression was canonicalised. Fixed: explicit `visit_ListComp`, `visit_SetComp`, `visit_GeneratorExp`, `visit_DictComp` methods that visit generators first.

`sir1_backup.py` contains the original pre-fix version.
