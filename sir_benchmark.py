#!/usr/bin/env python3
"""
sir_benchmark.py — Accuracy benchmark for SIR Engine function-level duplicate detection.

Tests the semantic hash engine against known duplicate pairs (should match) and
known non-duplicate pairs (should not match), then reports:
  - True positives  (duplicate correctly detected)
  - False negatives (duplicate missed)
  - True negatives  (non-duplicate correctly rejected)
  - False positives (non-duplicate wrongly flagged as duplicate)
  - Precision, Recall, F1
  - Wall-clock time

USAGE
-----
  python3 sir_benchmark.py
  python3 sir_benchmark.py --verbose      # show per-test results
  python3 sir_benchmark.py --json         # machine-readable output
  python3 sir_benchmark.py --json -o results.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Bootstrap: import sir1 from the same directory without installing it
# ---------------------------------------------------------------------------

def _load_sir1():
    sir1_path = Path(__file__).parent / "sir1.py"
    if not sir1_path.exists():
        sys.exit(f"Error: sir1.py not found at {sir1_path}")
    spec = importlib.util.spec_from_file_location("sir1", sir1_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sir1"] = mod  # required so @dataclass can resolve __module__
    spec.loader.exec_module(mod)
    return mod


sir1 = _load_sir1()


def semantic_hash(source: str) -> Optional[str]:
    """Return the semantic SHA-256 hash of a single Python function source string."""
    src = textwrap.dedent(source).strip()
    try:
        sir = sir1.encode_to_sir(src, sir1.CanonConfig(mode="semantic"))
        return sir1.sir_hash(sir)
    except SyntaxError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test case definitions
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    label: str
    description: str
    func_a: str
    func_b: str
    expected_duplicate: bool  # True = should hash the same


# --- Duplicate pairs (expected_duplicate=True) ----------------------------

DUPLICATE_PAIRS: List[TestCase] = [
    TestCase(
        label="simple_rename",
        description="Same addition logic, different parameter names",
        func_a="""
            def add(x, y):
                return x + y
        """,
        func_b="""
            def sum_two(a, b):
                return a + b
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="rename_with_local",
        description="Same logic with renamed local variable",
        func_a="""
            def compute(values):
                total = 0
                for v in values:
                    total += v
                return total
        """,
        func_b="""
            def accumulate(items):
                result = 0
                for item in items:
                    result += item
                return result
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="conditional_rename",
        description="Same conditional logic, different names",
        func_a="""
            def clamp(value, lo, hi):
                if value < lo:
                    return lo
                if value > hi:
                    return hi
                return value
        """,
        func_b="""
            def clip(x, min_val, max_val):
                if x < min_val:
                    return min_val
                if x > max_val:
                    return max_val
                return x
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="multiline_rename",
        description="Multi-step computation with all variables renamed",
        func_a="""
            def discount_price(price, rate):
                discount = price * rate
                final = price - discount
                return final
        """,
        func_b="""
            def apply_discount(cost, pct):
                reduction = cost * pct
                net = cost - reduction
                return net
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="loop_with_index",
        description="Index-based loop with renamed vars",
        func_a="""
            def find_max(nums):
                best = nums[0]
                for i in range(1, len(nums)):
                    if nums[i] > best:
                        best = nums[i]
                return best
        """,
        func_b="""
            def maximum(data):
                top = data[0]
                for k in range(1, len(data)):
                    if data[k] > top:
                        top = data[k]
                return top
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="boolean_flag_rename",
        description="Linear search with boolean flag, all names different",
        func_a="""
            def contains(items, target):
                found = False
                for item in items:
                    if item == target:
                        found = True
                        break
                return found
        """,
        func_b="""
            def has_element(collection, value):
                exists = False
                for elem in collection:
                    if elem == value:
                        exists = True
                        break
                return exists
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="nested_call_rename",
        description="Composed function calls with renamed args",
        func_a="""
            def normalize(text):
                stripped = text.strip()
                lowered = stripped.lower()
                return lowered
        """,
        func_b="""
            def clean(s):
                s2 = s.strip()
                s3 = s2.lower()
                return s3
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="exception_handler_rename",
        description="Try/except block with renamed variables",
        func_a="""
            def safe_divide(numerator, denominator):
                try:
                    result = numerator / denominator
                    return result
                except ZeroDivisionError:
                    return None
        """,
        func_b="""
            def divide(a, b):
                try:
                    q = a / b
                    return q
                except ZeroDivisionError:
                    return None
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="dict_builder_rename",
        description="Dict construction loop with renamed variables",
        func_a="""
            def index_by_key(pairs):
                result = {}
                for k, v in pairs:
                    result[k] = v
                return result
        """,
        func_b="""
            def build_map(entries):
                mapping = {}
                for key, val in entries:
                    mapping[key] = val
                return mapping
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="recursive_sum_rename",
        description="Recursive function with different name and parameter",
        func_a="""
            def sum_list(lst):
                if not lst:
                    return 0
                return lst[0] + sum_list(lst[1:])
        """,
        func_b="""
            def total(arr):
                if not arr:
                    return 0
                return arr[0] + total(arr[1:])
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="list_filter_rename",
        description="List comprehension filter with renamed variables",
        func_a="""
            def keep_positive(numbers):
                return [n for n in numbers if n > 0]
        """,
        func_b="""
            def filter_positive(vals):
                return [x for x in vals if x > 0]
        """,
        expected_duplicate=True,
    ),
    TestCase(
        label="string_join_rename",
        description="Join with separator, different variable names",
        func_a="""
            def join_words(words, sep):
                return sep.join(words)
        """,
        func_b="""
            def concatenate(tokens, delimiter):
                return delimiter.join(tokens)
        """,
        expected_duplicate=True,
    ),
]


# --- Non-duplicate pairs (expected_duplicate=False) -----------------------

NON_DUPLICATE_PAIRS: List[TestCase] = [
    TestCase(
        label="add_vs_subtract",
        description="Addition vs subtraction — different operators",
        func_a="""
            def compute(a, b):
                return a + b
        """,
        func_b="""
            def compute(a, b):
                return a - b
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="different_arity",
        description="Same operation but different number of parameters",
        func_a="""
            def multiply(x, y):
                return x * y
        """,
        func_b="""
            def multiply(x, y, z):
                return x * y * z
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="loop_vs_recursive",
        description="Iterative vs recursive sum — structurally different",
        func_a="""
            def total(lst):
                acc = 0
                for x in lst:
                    acc += x
                return acc
        """,
        func_b="""
            def total(lst):
                if not lst:
                    return 0
                return lst[0] + total(lst[1:])
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="different_return_value",
        description="Same structure but returns different constant on base case",
        func_a="""
            def base(items):
                if not items:
                    return 0
                return items[0]
        """,
        func_b="""
            def base(items):
                if not items:
                    return -1
                return items[0]
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="filter_vs_map",
        description="List comprehension with condition vs without",
        func_a="""
            def process(nums):
                return [n for n in nums if n > 0]
        """,
        func_b="""
            def process(nums):
                return [n * 2 for n in nums]
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="different_string_ops",
        description="strip().lower() vs strip() only",
        func_a="""
            def clean(s):
                return s.strip().lower()
        """,
        func_b="""
            def clean(s):
                return s.strip()
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="if_vs_while",
        description="If branch vs while loop — different control flow",
        func_a="""
            def run(x):
                if x > 0:
                    x -= 1
                return x
        """,
        func_b="""
            def run(x):
                while x > 0:
                    x -= 1
                return x
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="different_exception_type",
        description="Catching ValueError vs ZeroDivisionError",
        func_a="""
            def parse(s):
                try:
                    return int(s)
                except ValueError:
                    return None
        """,
        func_b="""
            def parse(s):
                try:
                    return int(s)
                except TypeError:
                    return None
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="greater_vs_less",
        description="max-finding vs min-finding — inverted comparison",
        func_a="""
            def extreme(nums):
                best = nums[0]
                for n in nums[1:]:
                    if n > best:
                        best = n
                return best
        """,
        func_b="""
            def extreme(nums):
                best = nums[0]
                for n in nums[1:]:
                    if n < best:
                        best = n
                return best
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="extra_branch",
        description="Same logic but one function has an extra guard clause",
        func_a="""
            def safe_head(items):
                return items[0]
        """,
        func_b="""
            def safe_head(items):
                if not items:
                    return None
                return items[0]
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="multiply_vs_exponent",
        description="Multiplication vs power operator",
        func_a="""
            def scale(x, n):
                return x * n
        """,
        func_b="""
            def scale(x, n):
                return x ** n
        """,
        expected_duplicate=False,
    ),
    TestCase(
        label="dict_get_vs_subscript",
        description="dict.get() with default vs direct subscript",
        func_a="""
            def lookup(d, key):
                return d.get(key, None)
        """,
        func_b="""
            def lookup(d, key):
                return d[key]
        """,
        expected_duplicate=False,
    ),
]


ALL_CASES: List[TestCase] = DUPLICATE_PAIRS + NON_DUPLICATE_PAIRS


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    label: str
    description: str
    expected_duplicate: bool
    got_duplicate: bool
    hash_a: Optional[str]
    hash_b: Optional[str]
    elapsed_ms: float

    @property
    def correct(self) -> bool:
        return self.expected_duplicate == self.got_duplicate

    @property
    def verdict(self) -> str:
        if self.expected_duplicate and self.got_duplicate:
            return "TP"
        if not self.expected_duplicate and not self.got_duplicate:
            return "TN"
        if self.expected_duplicate and not self.got_duplicate:
            return "FN"
        return "FP"


def run_benchmark(cases: List[TestCase]) -> Tuple[List[TestResult], float]:
    results: List[TestResult] = []
    wall_start = time.perf_counter()

    for tc in cases:
        t0 = time.perf_counter()
        h_a = semantic_hash(tc.func_a)
        h_b = semantic_hash(tc.func_b)
        elapsed = (time.perf_counter() - t0) * 1000  # ms

        if h_a is None or h_b is None:
            got_dup = False  # hash error = can't confirm duplicate
        else:
            got_dup = h_a == h_b

        results.append(TestResult(
            label=tc.label,
            description=tc.description,
            expected_duplicate=tc.expected_duplicate,
            got_duplicate=got_dup,
            hash_a=h_a,
            hash_b=h_b,
            elapsed_ms=elapsed,
        ))

    total_elapsed = (time.perf_counter() - wall_start) * 1000
    return results, total_elapsed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
RESET  = "\033[0m"

def _c(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{RESET}" if use_color else text


def print_report(results: List[TestResult], total_ms: float, verbose: bool, use_color: bool) -> None:
    tp = sum(1 for r in results if r.verdict == "TP")
    tn = sum(1 for r in results if r.verdict == "TN")
    fp = sum(1 for r in results if r.verdict == "FP")
    fn = sum(1 for r in results if r.verdict == "FN")
    total = len(results)
    correct = tp + tn

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy  = correct / total if total > 0 else 0.0

    if verbose:
        print()
        print(_c("Per-test results", BOLD, use_color))
        print("-" * 72)
        for r in results:
            icon = _c("✓", GREEN, use_color) if r.correct else _c("✗", RED, use_color)
            verdict_color = GREEN if r.correct else RED
            label_str = _c(f"[{r.verdict}]", verdict_color, use_color)
            print(f"  {icon} {label_str} {r.label} ({r.elapsed_ms:.1f} ms)")
            print(f"       {r.description}")
            if not r.correct:
                exp = "duplicate" if r.expected_duplicate else "non-duplicate"
                got = "duplicate" if r.got_duplicate else "non-duplicate"
                print(f"       Expected: {exp} | Got: {got}")
                if r.hash_a and r.hash_b:
                    print(f"       hash_a: {r.hash_a[:16]}...")
                    print(f"       hash_b: {r.hash_b[:16]}...")
        print()

    print(_c("=" * 72, BOLD, use_color))
    print(_c("SIR Engine Benchmark Results", BOLD, use_color))
    print(_c("=" * 72, BOLD, use_color))

    dup_cases    = [r for r in results if r.expected_duplicate]
    nondup_cases = [r for r in results if not r.expected_duplicate]

    print(f"\n  {'Cases run:':<28} {total}")
    print(f"  {'Duplicate pairs:':<28} {len(dup_cases)}")
    print(f"  {'Non-duplicate pairs:':<28} {len(nondup_cases)}")
    print()

    tp_str = _c(str(tp), GREEN, use_color)
    tn_str = _c(str(tn), GREEN, use_color)
    fp_str = _c(str(fp), RED if fp > 0 else RESET, use_color)
    fn_str = _c(str(fn), RED if fn > 0 else RESET, use_color)

    print(f"  {'True Positives  (TP):':<28} {tp_str}  (duplicates correctly found)")
    print(f"  {'True Negatives  (TN):':<28} {tn_str}  (non-duplicates correctly rejected)")
    print(f"  {'False Positives (FP):':<28} {fp_str}  (non-duplicates wrongly flagged)")
    print(f"  {'False Negatives (FN):':<28} {fn_str}  (duplicates missed)")
    print()

    acc_color = GREEN if accuracy >= 0.95 else YELLOW if accuracy >= 0.80 else RED
    print(f"  {'Accuracy:':<28} {_c(f'{accuracy:.1%}', acc_color, use_color)}  ({correct}/{total})")
    print(f"  {'Precision:':<28} {precision:.1%}")
    print(f"  {'Recall:':<28} {recall:.1%}")
    print(f"  {'F1 Score:':<28} {f1:.3f}")
    print()
    print(f"  {'Total wall time:':<28} {total_ms:.1f} ms")
    print(f"  {'Avg time per pair:':<28} {total_ms/total:.1f} ms")
    print()

    if fp == 0 and fn == 0:
        print(_c("  All tests passed.", GREEN + BOLD, use_color))
    else:
        failures = [r for r in results if not r.correct]
        print(_c(f"  {len(failures)} test(s) failed:", RED + BOLD, use_color))
        for r in failures:
            print(f"    - {r.label}  [{r.verdict}]  {r.description}")

    print(_c("=" * 72, BOLD, use_color))


def build_json_output(results: List[TestResult], total_ms: float) -> dict:
    tp = sum(1 for r in results if r.verdict == "TP")
    tn = sum(1 for r in results if r.verdict == "TN")
    fp = sum(1 for r in results if r.verdict == "FP")
    fn = sum(1 for r in results if r.verdict == "FN")
    total = len(results)
    correct = tp + tn

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy  = correct / total if total > 0 else 0.0

    return {
        "summary": {
            "total_cases": total,
            "correct": correct,
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "total_ms": round(total_ms, 2),
            "avg_ms_per_pair": round(total_ms / total, 2) if total else 0,
        },
        "results": [
            {
                "label": r.label,
                "description": r.description,
                "expected_duplicate": r.expected_duplicate,
                "got_duplicate": r.got_duplicate,
                "correct": r.correct,
                "verdict": r.verdict,
                "hash_a": r.hash_a,
                "hash_b": r.hash_b,
                "elapsed_ms": round(r.elapsed_ms, 2),
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="sir_benchmark.py",
        description="Accuracy benchmark for SIR Engine function-level duplicate detection.",
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="Show per-test results")
    ap.add_argument("--json", action="store_true", help="Output results as JSON")
    ap.add_argument("-o", "--out", help="Write JSON results to this file (implies --json)")
    args = ap.parse_args()

    use_json = args.json or bool(args.out)
    use_color = sys.stdout.isatty() and not use_json

    if not use_json:
        print(f"Running {len(ALL_CASES)} test cases "
              f"({len(DUPLICATE_PAIRS)} duplicate pairs, {len(NON_DUPLICATE_PAIRS)} non-duplicate pairs)...")

    results, total_ms = run_benchmark(ALL_CASES)

    if use_json:
        output = build_json_output(results, total_ms)
        text = json.dumps(output, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Wrote results to {args.out}")
        else:
            print(text)
    else:
        print_report(results, total_ms, verbose=args.verbose, use_color=use_color)

    # Exit 1 if any test failed (useful for CI)
    failures = [r for r in results if not r.correct]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
