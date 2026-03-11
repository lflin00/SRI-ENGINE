#!/usr/bin/env python3
"""
sir2_core.py — SIR Engine v2: Class-level semantic duplicate detection.

New in v2 (does NOT modify any v1 files):
- ClassAlphaRenamer   : strips class names, method names, variable names
- hash_class_source   : Merkle hash for a class (method hashes → class hash)
- extract_classes     : extract classes + their methods from Python source
- class_similarity    : percentage-based similarity between two classes
- scan_for_class_dupes: find exact (binary) and partial (%) duplicate classes

Architecture:
    Python class source
            │
            ▼
    ClassAlphaRenamer       ← strips class name, method names, self.vars
            │
            ▼
    Per-method hash         ← SHA-256(ast.dump(canonical_method))
            │
            ▼
    Merkle class hash       ← SHA-256(sorted method hashes)  [Merkle tree]
            │
            ▼
    Hash comparison         ← exact match = duplicate class
    Similarity score        ← % of matching method hashes = partial duplicate

v2 is completely standalone. It imports only from Python stdlib and sir1.py
(for AlphaRenamer). It does not touch sir_ui.py, sir_cli.py, or any v1 file.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────
#  Reuse AlphaRenamer from sir1.py for method-level hashing
# ─────────────────────────────────────────────

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from sir1 import AlphaRenamer


class _SelfAttrNormalizer(ast.NodeTransformer):
    """
    Normalize self.attr_name references so that two classes with different
    instance variable names (self.total vs self.sum) hash identically.
    Replaces self.X with self.a0, self.a1, ... in encounter order.
    """
    def __init__(self) -> None:
        super().__init__()
        self.attr_map: Dict[str, str] = {}
        self.counter = 0

    def _canon_attr(self, name: str) -> str:
        if name not in self.attr_map:
            self.attr_map[name] = f"a{self.counter}"
            self.counter += 1
        return self.attr_map[name]

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            node.attr = self._canon_attr(node.attr)
        self.generic_visit(node)
        return node


def _hash_method_src(method_src: str) -> str:
    """
    Hash a single method using alpha-equivalence + self-attribute normalization.
    Strips argument names, local variable names, and self.attr names.
    Returns a hex SHA-256 string.
    """
    tree = ast.parse(method_src.strip())

    # Step 1: normalize self.attr names
    normalizer = _SelfAttrNormalizer()
    tree = normalizer.visit(tree)
    ast.fix_missing_locations(tree)

    # Step 2: alpha-rename args and locals
    renamer = AlphaRenamer()
    renamed = renamer.visit(tree)
    ast.fix_missing_locations(renamed)

    dumped = ast.dump(renamed)
    return hashlib.sha256(dumped.encode()).hexdigest()


# ─────────────────────────────────────────────
#  Data structures
# ─────────────────────────────────────────────

@dataclass
class MethodInfo:
    name: str           # original method name
    lineno: int
    source: str         # raw source of the method
    hash: str           # alpha-equivalent hash of this method


@dataclass
class ClassInfo:
    name: str           # original class name
    file: str           # source file
    lineno: int
    bases: List[str]    # parent class names (for display only)
    methods: List[MethodInfo]
    class_hash: str     # Merkle hash over sorted method hashes
    parent_hash: Optional[str] = None       # hash of parent class (if found in same scan)
    ai_translated: bool = False             # True if extracted via AI translation
    ai_confidence: Optional[str] = None    # HIGH / MEDIUM / LOW / FAILED
    original_language: Optional[str] = None  # source language before translation


@dataclass
class ClassDuplicateCluster:
    """A group of classes that are exact structural duplicates."""
    class_hash: str
    members: List[ClassInfo]


@dataclass
class ClassSimilarityPair:
    """Two classes that share some but not all method hashes."""
    class_a: ClassInfo
    class_b: ClassInfo
    similarity: float           # 0.0 – 1.0
    matching_methods: List[Tuple[MethodInfo, MethodInfo]]  # (a_method, b_method)
    only_in_a: List[MethodInfo]
    only_in_b: List[MethodInfo]


# ─────────────────────────────────────────────
#  Extraction
# ─────────────────────────────────────────────

def extract_classes(src: str, filename: str) -> List[ClassInfo]:
    """
    Parse Python source and return ClassInfo for every class definition.
    Each class includes its methods with per-method alpha-equivalent hashes
    and a Merkle class hash.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    classes: List[ClassInfo] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        bases = []
        for b in node.bases:
            if isinstance(b, ast.Name):
                bases.append(b.id)
            elif isinstance(b, ast.Attribute):
                bases.append(ast.unparse(b))

        methods: List[MethodInfo] = []
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_src = ast.get_source_segment(src, item)
            if not method_src:
                continue
            try:
                method_hash = _hash_method_src(method_src)
            except Exception:
                method_hash = hashlib.sha256(method_src.encode()).hexdigest()
            methods.append(MethodInfo(
                name=item.name,
                lineno=item.lineno,
                source=method_src,
                hash=method_hash,
            ))

        if not methods:
            # Skip empty classes / pure data classes with no methods
            continue

        class_hash = _merkle_class_hash(methods)

        classes.append(ClassInfo(
            name=node.name,
            file=filename,
            lineno=node.lineno,
            bases=bases,
            methods=methods,
            class_hash=class_hash,
        ))

    return classes


def _merkle_class_hash(methods: List[MethodInfo]) -> str:
    """
    Merkle hash for a class:
    SHA-256( sorted( method_hashes ) )

    Sorting ensures that two classes with methods in different orders
    still produce the same hash if their method logic is identical.
    """
    sorted_hashes = sorted(m.hash for m in methods)
    combined = json.dumps(sorted_hashes, separators=(",", ":")).encode()
    return hashlib.sha256(combined).hexdigest()


# ─────────────────────────────────────────────
#  Merkle inheritance hash
# ─────────────────────────────────────────────

def apply_inheritance_hashes(classes: List[ClassInfo]) -> None:
    """
    For each class that has a parent in the same scan,
    fold the parent's class_hash into the child's hash (Merkle style).

    This means two classes that inherit from logically identical parents
    and have identical methods will produce the same final hash.

    Mutates ClassInfo.class_hash in place for classes with known parents.
    Stores the parent hash in ClassInfo.parent_hash for display.
    """
    hash_by_name: Dict[str, str] = {c.name: c.class_hash for c in classes}

    for cls in classes:
        if not cls.bases:
            continue
        parent_hashes = []
        for base in cls.bases:
            if base in hash_by_name:
                parent_hashes.append(hash_by_name[base])

        if not parent_hashes:
            continue

        # Fold parent hash(es) into child hash — Merkle style
        sorted_method_hashes = sorted(m.hash for m in cls.methods)
        sorted_parent_hashes = sorted(parent_hashes)
        combined = json.dumps(
            {"methods": sorted_method_hashes, "parents": sorted_parent_hashes},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        cls.parent_hash = sorted_parent_hashes[0] if len(sorted_parent_hashes) == 1 else hashlib.sha256(
            json.dumps(sorted_parent_hashes, separators=(",", ":")).encode()
        ).hexdigest()
        cls.class_hash = hashlib.sha256(combined).hexdigest()


# ─────────────────────────────────────────────
#  Similarity
# ─────────────────────────────────────────────

def class_similarity(a: ClassInfo, b: ClassInfo) -> ClassSimilarityPair:
    """
    Compute percentage similarity between two classes based on
    how many of their method hashes match.

    Returns a ClassSimilarityPair with:
    - similarity: 0.0 to 1.0
    - matching_methods: pairs of methods that are structurally identical
    - only_in_a / only_in_b: methods unique to each class
    """
    a_by_hash: Dict[str, MethodInfo] = {m.hash: m for m in a.methods}
    b_by_hash: Dict[str, MethodInfo] = {m.hash: m for m in b.methods}

    matching_hashes = set(a_by_hash.keys()) & set(b_by_hash.keys())
    only_a_hashes = set(a_by_hash.keys()) - matching_hashes
    only_b_hashes = set(b_by_hash.keys()) - matching_hashes

    matching_methods = [(a_by_hash[h], b_by_hash[h]) for h in matching_hashes]
    only_in_a = [a_by_hash[h] for h in only_a_hashes]
    only_in_b = [b_by_hash[h] for h in only_b_hashes]

    total = max(len(a.methods), len(b.methods))
    similarity = len(matching_hashes) / total if total > 0 else 0.0

    return ClassSimilarityPair(
        class_a=a,
        class_b=b,
        similarity=similarity,
        matching_methods=matching_methods,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
    )


# ─────────────────────────────────────────────
#  Scanner
# ─────────────────────────────────────────────

def scan_for_class_dupes(
    classes: List[ClassInfo],
    min_similarity: float = 1.0,
    apply_inheritance: bool = True,
) -> Tuple[List[ClassDuplicateCluster], List[ClassSimilarityPair]]:
    """
    Scan a list of ClassInfo objects for duplicates.

    Args:
        classes: output of extract_classes() across one or more files
        min_similarity: 0.0-1.0. 1.0 = exact duplicates only (binary mode).
                        Lower values find partial duplicates.
        apply_inheritance: if True, fold parent class hashes into child hashes
                           before comparing (Merkle inheritance)

    Returns:
        exact_dupes: list of ClassDuplicateCluster (classes with identical Merkle hash)
        similar_pairs: list of ClassSimilarityPair (classes above min_similarity threshold,
                       excluding exact duplicates if min_similarity < 1.0)
    """
    if apply_inheritance:
        apply_inheritance_hashes(classes)

    # ── Exact duplicates (binary) ──
    groups: Dict[str, List[ClassInfo]] = defaultdict(list)
    for cls in classes:
        groups[cls.class_hash].append(cls)

    exact_clusters = [
        ClassDuplicateCluster(class_hash=h, members=members)
        for h, members in groups.items()
        if len(members) >= 2
    ]

    # ── Partial duplicates (similarity) ──
    similar_pairs: List[ClassSimilarityPair] = []
    if min_similarity < 1.0:
        seen = set()
        for i, a in enumerate(classes):
            for j, b in enumerate(classes):
                if j <= i:
                    continue
                pair_key = tuple(sorted([f"{a.file}:{a.name}", f"{b.file}:{b.name}"]))
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                # Skip if they're already exact duplicates
                if a.class_hash == b.class_hash:
                    continue

                pair = class_similarity(a, b)
                if pair.similarity >= min_similarity:
                    similar_pairs.append(pair)

        similar_pairs.sort(key=lambda p: -p.similarity)

    return exact_clusters, similar_pairs


# ─────────────────────────────────────────────
#  Multi-file scanner
# ─────────────────────────────────────────────

def scan_files_for_classes(
    file_sources: Dict[str, str],
    min_similarity: float = 1.0,
    apply_inheritance: bool = True,
    ai_backend: str = "ollama",
    ai_api_key: str = "",
    ai_ollama_model: str = "codellama:7b",
    ai_ollama_host: str = "http://localhost:11434",
    ai_use_cache: bool = True,
) -> Tuple[List[ClassDuplicateCluster], List[ClassSimilarityPair], int]:
    """
    Scan multiple files for class-level duplicates.

    Python files (.py) are processed natively. All other file types are
    translated to Python class-by-class via AI before hashing.

    Args:
        file_sources:      {filename: source_code}
        min_similarity:    threshold for partial matching (1.0 = exact only)
        apply_inheritance: apply Merkle inheritance hashing
        ai_backend:        "ollama" or "anthropic" (for non-Python files)
        ai_api_key:        Anthropic API key (if backend == "anthropic")
        ai_ollama_model:   Ollama model name
        ai_ollama_host:    Ollama host URL
        ai_use_cache:      use .sir_cache/ for translation caching

    Returns:
        (exact_clusters, similar_pairs, total_classes_found)
    """
    ai_kwargs = dict(
        backend=ai_backend,
        api_key=ai_api_key,
        ollama_model=ai_ollama_model,
        ollama_host=ai_ollama_host,
        use_cache=ai_use_cache,
    )

    all_classes: List[ClassInfo] = []
    for filename, src in file_sources.items():
        if Path(filename).suffix.lower() == ".py":
            all_classes.extend(extract_classes(src, filename))
        else:
            language = _detect_language(filename)
            if language:
                all_classes.extend(
                    extract_classes_ai(src, filename, language, **ai_kwargs)
                )

    exact, similar = scan_for_class_dupes(
        all_classes,
        min_similarity=min_similarity,
        apply_inheritance=apply_inheritance,
    )
    return exact, similar, len(all_classes)


# ─────────────────────────────────────────────
#  AI TRANSLATION — non-Python class support
# ─────────────────────────────────────────────
#
# Design principle (from skills/hashing.md):
#   Translate at the class level — the entire class body at once — so that
#   the relationship between methods and instance variables is preserved.
#   Method-by-method translation would lose shared state context.

_EXTENSION_TO_LANG: Dict[str, str] = {
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".swift": "Swift", ".cs": "C#", ".scala": "Scala",
    ".dart": "Dart", ".php": "PHP",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".h": "C++", ".hpp": "C++",
    ".rs": "Rust", ".go": "Go",
    ".rb": "Ruby",
    ".lua": "Lua",
}

# Language-specific notes injected into the translation prompt
_CLASS_LANG_HINTS: Dict[str, str] = {
    "Java":   "Translate 'this.x' to 'self.x'. Ignore access modifiers (public/private/protected). Translate generics as plain variables. Constructor becomes __init__.",
    "Kotlin": "Translate 'this.x' to 'self.x'. val/var become regular assignments. Ignore nullable types (?). Constructor/init block becomes __init__. when becomes if/elif.",
    "Swift":  "Translate 'self.x' literally. guard becomes if. Ignore optionals (?/!). Ignore access modifiers. Initializer becomes __init__.",
    "C#":     "Translate 'this.x' to 'self.x'. Ignore access modifiers. Properties become self.x. Constructor becomes __init__. Ignore async/await for structural purposes.",
    "Scala":  "Translate 'this.x' to 'self.x'. case classes as regular classes. Option[T] as a plain value. Primary constructor body becomes __init__.",
    "Dart":   "Translate 'this.x' to 'self.x'. Ignore nullable types (?). Constructor becomes __init__. Ignore access modifiers.",
    "PHP":    "Translate '$this->x' to 'self.x'. __construct becomes __init__. Ignore access modifiers.",
    "C++":    "Translate 'this->x' to 'self.x'. Ignore templates and memory management. Constructor becomes __init__. Destructor can be omitted.",
    "Rust":   "Translate 'self.x' literally. Ignore lifetimes and ownership. impl block methods become class methods. 'new' becomes __init__ with Self as return replaced by self assignment.",
    "Go":     "Translate receiver field access (e.g. 's.x') to 'self.x'. Struct methods become class methods. 'NewFoo(...)' constructor pattern becomes __init__.",
    "Ruby":   "Translate '@x' instance variables to 'self.x'. initialize becomes __init__. attr_accessor/attr_reader become plain self.x assignments in __init__.",
    "Lua":    "Translate 'self.x' literally. Constructor function becomes __init__. Method calls via colon syntax become regular method definitions.",
}

CLASS_TRANSLATE_PROMPT = """\
You are a code translation engine. Convert this {language} class to an equivalent Python 3 class.

RULES:
1. Preserve exact logical structure — do NOT simplify, optimize, or restructure
2. Translate ALL methods including the constructor
3. Translate instance variable access to self.x
4. Translate control flow literally (for loops stay for loops, while stays while)
5. Use only built-in Python — no external libraries
6. Keep all method names and variable names identical to the original
7. Output ONLY a valid Python class definition — no explanation, no markdown, no backticks, no comments
8. The output must start with 'class '{hints}

Input ({language}):
{code}

Python class translation:"""


def _detect_language(filename: str) -> Optional[str]:
    """Return the human-readable language name for a filename, or None if not supported."""
    return _EXTENSION_TO_LANG.get(Path(filename).suffix.lower())


# ── Raw class extraction from non-Python source ──────────────────────────────

def _extract_brace_classes(src: str) -> List[Tuple[str, int, str]]:
    """
    Extract brace-delimited class blocks.
    Handles: Java, C#, Kotlin, Swift, C++, Dart, PHP, Scala.
    """
    results: List[Tuple[str, int, str]] = []
    # Matches: class/interface Foo<T> extends Bar implements Baz {
    pattern = re.compile(
        r'\bclass\s+(\w+)(?:\s*(?:<[^>]*>|\([^)]*\)))?\s*(?:(?:extends|implements|:)\s*[\w,\s<>]+)?\s*\{',
        re.MULTILINE,
    )
    for match in pattern.finditer(src):
        name = match.group(1)
        lineno = src[:match.start()].count("\n") + 1
        brace_start = src.find("{", match.start())
        depth, end = 0, brace_start
        for i in range(brace_start, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        results.append((name, lineno, src[match.start():end]))
    return results


def _extract_ruby_classes(src: str) -> List[Tuple[str, int, str]]:
    """Extract Ruby class blocks (class ... end)."""
    results: List[Tuple[str, int, str]] = []
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*class\s+(\w+)", lines[i])
        if m:
            name = m.group(1)
            lineno = i + 1
            block = [lines[i]]
            i += 1
            depth = 1
            while i < len(lines) and depth > 0:
                ln = lines[i]
                if re.match(r"\s*(?:class|def|do|begin|if|unless|while|until|for|case|module)\b", ln):
                    depth += 1
                if re.match(r"\s*end\s*$", ln):
                    depth -= 1
                block.append(ln)
                i += 1
            results.append((name, lineno, "\n".join(block)))
        else:
            i += 1
    return results


def _extract_rust_classes(src: str) -> List[Tuple[str, int, str]]:
    """
    Extract Rust struct + impl pairs.
    For each struct, find its impl block and combine them into a pseudo-class.
    """
    results: List[Tuple[str, int, str]] = []

    struct_pat = re.compile(r"\bstruct\s+(\w+)[^{]*\{", re.MULTILINE)
    impl_pat = re.compile(r"\bimpl(?:\s*<[^>]*>)?\s+(\w+)\s*\{", re.MULTILINE)

    def _extract_brace_block(start: int) -> str:
        brace = src.find("{", start)
        if brace == -1:
            return ""
        depth, end = 0, brace
        for i in range(brace, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return src[start:end]

    structs: Dict[str, Tuple[int, str]] = {}
    for m in struct_pat.finditer(src):
        name = m.group(1)
        lineno = src[:m.start()].count("\n") + 1
        structs[name] = (lineno, _extract_brace_block(m.start()))

    impls: Dict[str, str] = {}
    for m in impl_pat.finditer(src):
        name = m.group(1)
        if name in structs:
            impls[name] = _extract_brace_block(m.start())

    for name, (lineno, struct_src) in structs.items():
        impl_src = impls.get(name, "")
        combined = f"// struct {name}\n{struct_src}\n// impl {name}\n{impl_src}".strip()
        if combined:
            results.append((name, lineno, combined))

    return results


def _extract_go_classes(src: str) -> List[Tuple[str, int, str]]:
    """
    Extract Go type ... struct definitions plus all associated methods.
    Combines them into a pseudo-class block for translation.
    """
    results: List[Tuple[str, int, str]] = []

    struct_pat = re.compile(r"\btype\s+(\w+)\s+struct\s*\{", re.MULTILINE)
    method_pat = re.compile(r"\bfunc\s*\(\s*\w+\s+\*?(\w+)\s*\)\s*\w+", re.MULTILINE)

    def _extract_brace_block(start: int) -> str:
        brace = src.find("{", start)
        if brace == -1:
            return ""
        depth, end = 0, brace
        for i in range(brace, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return src[start:end]

    structs: Dict[str, Tuple[int, str]] = {}
    for m in struct_pat.finditer(src):
        name = m.group(1)
        lineno = src[:m.start()].count("\n") + 1
        structs[name] = (lineno, _extract_brace_block(m.start()))

    methods: Dict[str, List[str]] = {name: [] for name in structs}
    for m in method_pat.finditer(src):
        receiver_type = m.group(1)
        if receiver_type in methods:
            methods[receiver_type].append(_extract_brace_block(m.start()))

    for name, (lineno, struct_src) in structs.items():
        parts = [f"// type {name} struct\n{struct_src}"]
        parts.extend(f"// method\n{ms}" for ms in methods[name] if ms)
        combined = "\n".join(parts).strip()
        if combined:
            results.append((name, lineno, combined))

    return results


def extract_raw_classes(src: str, language: str) -> List[Tuple[str, int, str]]:
    """
    Extract raw class blocks from non-Python source.
    Returns [(class_name, lineno, raw_class_source)].
    """
    lang = language.lower()
    if "ruby" in lang:
        return _extract_ruby_classes(src)
    elif "rust" in lang:
        return _extract_rust_classes(src)
    elif "go" in lang:
        return _extract_go_classes(src)
    else:
        return _extract_brace_classes(src)


# ── Translation helpers ───────────────────────────────────────────────────────

def _clean_class_translation(raw: str) -> str:
    """Strip markdown fences and return starting from the first 'class ' line."""
    raw = re.sub(r"```[\w]*", "", raw)
    raw = re.sub(r"`", "", raw)
    raw = raw.strip()
    for i, line in enumerate(raw.splitlines()):
        if line.strip().startswith("class "):
            return "\n".join(raw.splitlines()[i:]).strip()
    return raw.strip()


def _validate_python_class(code: str) -> Tuple[bool, str]:
    """
    Check that code parses as valid Python and contains at least one
    class definition with at least one method.
    """
    if not code or not code.strip():
        return False, "Empty translation"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, str(e)

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not classes:
        return False, "No class definition found in translation"
    has_methods = any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        for cls in classes
        for item in cls.body
    )
    if not has_methods:
        return False, "No methods found in translated class"
    return True, ""


def _call_backend(prompt: str, backend: str, api_key: str,
                  ollama_model: str, ollama_host: str) -> str:
    """Route a prompt to the configured AI backend and return the raw response."""
    try:
        from sir_ai_translate import call_ollama, call_anthropic
    except ImportError:
        raise RuntimeError("sir_ai_translate.py not found — required for AI class translation")

    if backend == "ollama":
        return call_ollama(prompt, model=ollama_model, host=ollama_host)
    elif backend == "anthropic":
        return call_anthropic(prompt, api_key=api_key)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")


def _class_merkle_hash_from_src(py_src: str) -> Optional[str]:
    """
    Parse translated Python, extract the first class, return its Merkle hash.
    Used for confidence scoring without touching the final ClassInfo pipeline.
    """
    try:
        classes = extract_classes(py_src, "<confidence_check>")
        if classes:
            return classes[0].class_hash
    except Exception:
        pass
    return None


# ── Main translation entry point ─────────────────────────────────────────────

def translate_class_to_python(
    class_src: str,
    language: str,
    backend: str = "ollama",
    api_key: str = "",
    ollama_model: str = "codellama:7b",
    ollama_host: str = "http://localhost:11434",
    use_cache: bool = True,
    confidence_check: bool = True,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """
    Translate a single non-Python class to Python with validation, caching,
    and confidence scoring.

    The entire class body is sent in one prompt so that method-to-instance-variable
    relationships are preserved across the translation.

    Returns:
        {
            "python_src":  str   — translated Python class
            "confidence":  str   — HIGH / MEDIUM / LOW / FAILED
            "cache_hit":   bool
            "error":       str   — set on FAILED
        }
    """
    try:
        from sir_ai_translate import cache_get, cache_set
        _cache_available = True
    except ImportError:
        _cache_available = False

    cache_key_prefix = "class::"  # distinguish from function-level cache keys

    def _do_cache_get() -> Optional[dict]:
        if use_cache and _cache_available:
            return cache_get(cache_key_prefix + class_src, language)
        return None

    def _do_cache_set(entry: dict) -> None:
        if use_cache and _cache_available:
            cache_set(cache_key_prefix + class_src, language, entry)

    def _fail(msg: str) -> dict:
        r = {"python_src": "", "confidence": "FAILED", "cache_hit": False, "error": msg}
        _do_cache_set(r)
        return r

    # ── Cache check ──────────────────────────────────────────────────────────
    cached = _do_cache_get()
    if cached:
        return {**cached, "cache_hit": True}

    # ── Build prompt ─────────────────────────────────────────────────────────
    hint = _CLASS_LANG_HINTS.get(language, "")
    hints_line = f"\n9. LANGUAGE NOTES: {hint}" if hint else ""
    prompt = CLASS_TRANSLATE_PROMPT.format(
        language=language, code=class_src, hints=hints_line
    )

    kw = dict(backend=backend, api_key=api_key,
              ollama_model=ollama_model, ollama_host=ollama_host)

    # ── First translation with retries ───────────────────────────────────────
    py1, last_err = "", ""
    for attempt in range(max_retries):
        try:
            raw = _call_backend(prompt, **kw)
            cleaned = _clean_class_translation(raw)
            valid, err = _validate_python_class(cleaned)
            if valid:
                py1 = cleaned
                break
            last_err = err
        except Exception as e:
            last_err = str(e)

    if not py1:
        return _fail(f"Invalid Python after {max_retries} attempts: {last_err}")

    hash1 = _class_merkle_hash_from_src(py1)

    # ── Confidence check — second translation ────────────────────────────────
    confidence, error = "MEDIUM", ""
    if confidence_check and hash1:
        try:
            raw2 = _call_backend(prompt, **kw)
            cleaned2 = _clean_class_translation(raw2)
            valid2, _ = _validate_python_class(cleaned2)
            if valid2:
                hash2 = _class_merkle_hash_from_src(cleaned2)
                if hash1 and hash2 and hash1 == hash2:
                    confidence = "HIGH"
                elif hash2:
                    confidence = "LOW"
                    error = "Hash mismatch between two translations — review manually."
        except Exception:
            pass

    result: Dict[str, Any] = {
        "python_src": py1,
        "confidence": confidence,
        "cache_hit": False,
        "error": error,
    }

    # Cache HIGH and MEDIUM only — LOW is non-deterministic
    if confidence in ("HIGH", "MEDIUM"):
        _do_cache_set(result)

    return result


# ── AI-powered class extraction (non-Python files) ───────────────────────────

def extract_classes_ai(
    src: str,
    filename: str,
    language: str,
    backend: str = "ollama",
    api_key: str = "",
    ollama_model: str = "codellama:7b",
    ollama_host: str = "http://localhost:11434",
    use_cache: bool = True,
    confidence_check: bool = True,
) -> List[ClassInfo]:
    """
    Extract ClassInfo objects from a non-Python source file by:
      1. Extracting raw class blocks (language-specific regex)
      2. Translating each class body to Python in one shot (preserves method/field relationships)
      3. Running the standard V2 Merkle hash pipeline on the translated Python

    Each returned ClassInfo has:
      - ai_translated = True
      - ai_confidence = HIGH / MEDIUM / LOW
      - original_language = language
      - name / lineno = from the original source (not the translated Python)
    """
    raw_classes = extract_raw_classes(src, language)
    results: List[ClassInfo] = []

    tr_kwargs = dict(
        backend=backend, api_key=api_key,
        ollama_model=ollama_model, ollama_host=ollama_host,
        use_cache=use_cache, confidence_check=confidence_check,
    )

    for orig_name, orig_lineno, class_src in raw_classes:
        translation = translate_class_to_python(class_src, language, **tr_kwargs)

        if translation["confidence"] == "FAILED":
            continue

        py_src = translation["python_src"]
        translated_classes = extract_classes(py_src, filename)

        for cls in translated_classes:
            # Override name and lineno with values from the original source
            cls.name = orig_name
            cls.lineno = orig_lineno
            cls.ai_translated = True
            cls.ai_confidence = translation["confidence"]
            cls.original_language = language
            results.append(cls)

    return results
