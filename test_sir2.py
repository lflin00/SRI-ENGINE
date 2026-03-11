"""
test_sir2.py — Test suite for SIR Engine v2 class-level duplicate detection.

Tests:
1. Exact duplicate classes (different names, same logic)
2. Partial duplicate classes (similarity score)
3. Unique classes don't match
4. Inheritance Merkle hashing
5. Method order independence
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sir2_core import (
    extract_classes,
    scan_for_class_dupes,
    scan_files_for_classes,
    class_similarity,
    apply_inheritance_hashes,
)

PASS = "✓ PASS"
FAIL = "✗ FAIL"

# ─────────────────────────────────────────────
#  Test sources
# ─────────────────────────────────────────────

# Two classes with identical logic, different names/variable names
SRC_EXACT_A = '''
class ShoppingCart:
    def __init__(self):
        self.items = []
        self.total = 0

    def add_item(self, price, quantity):
        result = price * quantity
        self.total += result
        self.items.append(result)

    def get_total(self):
        return self.total

    def clear(self):
        self.items = []
        self.total = 0
'''

SRC_EXACT_B = '''
class OrderBasket:
    def __init__(self):
        self.products = []
        self.sum = 0

    def add_product(self, cost, amount):
        value = cost * amount
        self.sum += value
        self.products.append(value)

    def get_sum(self):
        return self.sum

    def reset(self):
        self.products = []
        self.sum = 0
'''

# A class that shares 2 of 3 methods with ShoppingCart (75% similar)
SRC_PARTIAL = '''
class Inventory:
    def __init__(self):
        self.items = []
        self.total = 0

    def add_item(self, price, quantity):
        result = price * quantity
        self.total += result
        self.items.append(result)

    def get_total(self):
        return self.total

    def apply_discount(self, rate):
        self.total = self.total * (1 - rate)
'''

# Unique class with completely different logic
SRC_UNIQUE = '''
class FileParser:
    def __init__(self, path):
        self.path = path
        self.lines = []

    def read(self):
        with open(self.path) as f:
            self.lines = f.readlines()

    def count_lines(self):
        return len(self.lines)
'''

# Inheritance test
SRC_PARENT = '''
class Animal:
    def breathe(self):
        return True

    def eat(self, food):
        self.food = food
        return food
'''

SRC_CHILD_A = '''
class Dog(Animal):
    def bark(self):
        sound = "woof"
        return sound
'''

SRC_CHILD_B = '''
class Cat(Animal):
    def meow(self):
        sound = "meow"
        return sound
'''

# Same child logic but different parent — should NOT match after inheritance hashing
SRC_DIFFERENT_PARENT = '''
class Robot:
    def breathe(self):
        return False  # robots don't breathe

    def eat(self, fuel):
        self.fuel = fuel
        return fuel
'''

SRC_CHILD_ROBOT = '''
class AndroidDog(Robot):
    def bark(self):
        sound = "woof"
        return sound
'''

# Method order independence test
SRC_ORDER_A = '''
class Calculator:
    def add(self, a, b):
        return a + b

    def multiply(self, x, y):
        result = x * y
        return result
'''

SRC_ORDER_B = '''
class MathHelper:
    def multiply(self, p, q):
        value = p * q
        return value

    def add(self, m, n):
        return m + n
'''


# ─────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────

def test_exact_duplicate():
    print("\n=== TEST 1: Exact duplicate classes ===")
    classes_a = extract_classes(SRC_EXACT_A, "cart.py")
    classes_b = extract_classes(SRC_EXACT_B, "basket.py")
    all_classes = classes_a + classes_b

    exact, _, total = scan_files_for_classes(
        {"cart.py": SRC_EXACT_A, "basket.py": SRC_EXACT_B},
        min_similarity=1.0
    )

    print(f"  Total classes found: {total}")
    print(f"  Exact duplicate clusters: {len(exact)}")
    if exact:
        for cluster in exact:
            names = [f"{c.name} ({c.file})" for c in cluster.members]
            print(f"  Cluster: {', '.join(names)}")

    result = len(exact) == 1 and len(exact[0].members) == 2
    print(f"  ShoppingCart == OrderBasket: {PASS if result else FAIL}")
    return result


def test_partial_duplicate():
    print("\n=== TEST 2: Partial duplicate classes (similarity) ===")
    exact, similar, total = scan_files_for_classes(
        {"cart.py": SRC_EXACT_A, "inventory.py": SRC_PARTIAL},
        min_similarity=0.5
    )

    print(f"  Total classes found: {total}")
    print(f"  Exact duplicates: {len(exact)}")
    print(f"  Similar pairs (>=50%): {len(similar)}")

    if similar:
        for pair in similar:
            print(f"  {pair.class_a.name} vs {pair.class_b.name}: {pair.similarity:.0%} similar")
            matching = [f"{a.name}=={b.name}" for a, b in pair.matching_methods]
            print(f"  Matching methods: {matching}")
            print(f"  Only in {pair.class_a.name}: {[m.name for m in pair.only_in_a]}")
            print(f"  Only in {pair.class_b.name}: {[m.name for m in pair.only_in_b]}")

    result = len(similar) >= 1 and any(p.similarity >= 0.5 for p in similar)
    print(f"  Partial similarity detected: {PASS if result else FAIL}")
    return result


def test_unique_classes_dont_match():
    print("\n=== TEST 3: Unique classes don't match ===")
    exact, similar, total = scan_files_for_classes(
        {"cart.py": SRC_EXACT_A, "parser.py": SRC_UNIQUE},
        min_similarity=0.3
    )

    print(f"  Total classes found: {total}")
    print(f"  Exact duplicates: {len(exact)}")
    print(f"  Similar pairs (>=30%): {len(similar)}")

    result = len(exact) == 0
    print(f"  ShoppingCart != FileParser: {PASS if result else FAIL}")
    return result


def test_method_order_independence():
    print("\n=== TEST 4: Method order independence ===")
    exact, _, total = scan_files_for_classes(
        {"calc.py": SRC_ORDER_A, "math.py": SRC_ORDER_B},
        min_similarity=1.0
    )

    print(f"  Total classes found: {total}")
    print(f"  Exact duplicates: {len(exact)}")
    if exact:
        for cluster in exact:
            names = [f"{c.name} ({c.file})" for c in cluster.members]
            print(f"  Cluster: {', '.join(names)}")

    result = len(exact) == 1
    print(f"  Calculator == MathHelper (different method order): {PASS if result else FAIL}")
    return result


def test_inheritance_hashing():
    print("\n=== TEST 5: Inheritance Merkle hashing ===")

    # Dog and Cat inherit from Animal and have different methods — should NOT match
    exact_animal, _, total = scan_files_for_classes(
        {
            "animal.py": SRC_PARENT,
            "dog.py": SRC_CHILD_A,
            "cat.py": SRC_CHILD_B,
        },
        min_similarity=1.0,
        apply_inheritance=True,
    )

    print(f"  Total classes found: {total}")
    print(f"  Exact duplicates (Dog vs Cat should NOT match): {len(exact_animal)}")

    # Dog inheriting Animal vs AndroidDog inheriting Robot — different parents
    # even though both have identical bark() methods — should NOT match
    exact_robot, _, _ = scan_files_for_classes(
        {
            "animal.py": SRC_PARENT,
            "dog.py": SRC_CHILD_A,
            "robot.py": SRC_DIFFERENT_PARENT,
            "android.py": SRC_CHILD_ROBOT,
        },
        min_similarity=1.0,
        apply_inheritance=True,
    )

    # Dog and AndroidDog have same bark() but different parents → should NOT match
    dog_android_match = any(
        {c.name for c in cluster.members} == {"Dog", "AndroidDog"}
        for cluster in exact_robot
    )

    print(f"  Dog (Animal parent) vs AndroidDog (Robot parent):")
    print(f"  Different parents → no match: {PASS if not dog_android_match else FAIL}")

    result = not dog_android_match
    return result


def test_hash_table():
    print("\n=== HASH TABLE ===")
    sources = {
        "cart.py": SRC_EXACT_A,
        "basket.py": SRC_EXACT_B,
        "inventory.py": SRC_PARTIAL,
        "parser.py": SRC_UNIQUE,
        "calc.py": SRC_ORDER_A,
        "math.py": SRC_ORDER_B,
    }
    all_classes = []
    for fname, src in sources.items():
        all_classes.extend(extract_classes(src, fname))

    for cls in all_classes:
        methods = ", ".join(m.name for m in cls.methods)
        print(f"  {cls.name:20s} ({cls.file:15s}) hash: {cls.class_hash[:20]}  methods: [{methods}]")


# ─────────────────────────────────────────────
#  Run all tests
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("SIR Engine v2 — Class Duplicate Detection Test Suite")
    print("=" * 55)

    test_hash_table()

    results = [
        test_exact_duplicate(),
        test_partial_duplicate(),
        test_unique_classes_dont_match(),
        test_method_order_independence(),
        test_inheritance_hashing(),
    ]

    passed = sum(results)
    total = len(results)

    print(f"\n{'='*55}")
    print(f"Results: {passed}/{total} tests passed")
    if passed == total:
        print("All tests passed ✓")
    else:
        print("Some tests failed ✗")
