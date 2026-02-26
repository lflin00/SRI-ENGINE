#!/usr/bin/env python3
"""
sir_universal.py — Universal cross-language SIR hasher.

Produces comparable hashes for equivalent functions across
Python, JavaScript, and TypeScript by compiling each language
down to a common canonical token sequence, then hashing that.

The universal canonical form:
  - All identifiers replaced with positional placeholders (v0, v1...)
  - All string literals replaced with STR
  - All number literals replaced with NUM
  - Language keywords normalised to universal equivalents
  - Whitespace and punctuation normalised
  - Comments stripped

This means:
  Python:     def add(x, y): result = x + y; return result
  JavaScript: function add(x, y) { const result = x + y; return result; }
  TypeScript: function add(x: number, y: number): number { const result = x + y; return result; }

All produce the same universal hash.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import os
from typing import Dict, List, Tuple, Optional

# Add SIR_MAIN to path for sir_js
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sir_js import extract_js_functions, tokenize as js_tokenize, strip_typescript


# ─────────────────────────────────────────────
#  Universal keyword normalisation
#  Maps language-specific keywords to universal tokens
# ─────────────────────────────────────────────

# JS/TS keywords → universal
JS_KEYWORD_MAP = {
    'const': 'LET', 'let': 'LET', 'var': 'LET',
    'function': 'FUNC', 'return': 'RETURN',
    'if': 'IF', 'else': 'ELSE',
    'for': 'FOR', 'while': 'WHILE',
    'true': 'TRUE', 'false': 'FALSE',
    'null': 'NULL', 'undefined': 'NULL',
    'new': 'NEW', 'this': 'SELF',
    'async': 'ASYNC', 'await': 'AWAIT',
    'class': 'CLASS', 'extends': 'EXTENDS',
    'import': 'IMPORT', 'export': 'EXPORT',
    'try': 'TRY', 'catch': 'CATCH', 'finally': 'FINALLY',
    'throw': 'THROW', 'typeof': 'TYPEOF',
}

# Python keywords → universal
PY_KEYWORD_MAP = {
    'def': 'FUNC', 'return': 'RETURN', 'yield': 'YIELD',
    'if': 'IF', 'elif': 'ELIF', 'else': 'ELSE',
    'for': 'FOR', 'while': 'WHILE',
    'True': 'TRUE', 'False': 'FALSE', 'None': 'NULL',
    'and': 'AND', 'or': 'OR', 'not': 'NOT',
    'in': 'IN', 'is': 'IS',
    'import': 'IMPORT', 'from': 'FROM',
    'class': 'CLASS',
    'try': 'TRY', 'except': 'CATCH', 'finally': 'FINALLY',
    'raise': 'THROW', 'with': 'WITH', 'as': 'AS',
    'lambda': 'LAMBDA', 'pass': 'PASS',
    'async': 'ASYNC', 'await': 'AWAIT',
    'global': 'GLOBAL', 'nonlocal': 'NONLOCAL',
    'del': 'DELETE', 'assert': 'ASSERT',
}

# Operator normalisation (both languages)
OP_MAP = {
    '===': '==', '!==': '!=',  # JS strict equality → universal
    '&&': 'AND', '||': 'OR', '!': 'NOT',
    '=>': 'ARROW',
}


# ─────────────────────────────────────────────
#  Python → Universal canonical tokens
# ─────────────────────────────────────────────

def python_func_to_universal(func_node: ast.FunctionDef, src: str) -> Optional[List[str]]:
    """
    Convert a Python function AST node to universal canonical token sequence.
    """
    rename: Dict[str, str] = {}
    counter = [0]

    def alloc(name: str) -> str:
        if name not in rename:
            rename[name] = f"v{counter[0]}"
            counter[0] += 1
        return rename[name]

    # Allocate params first
    for arg in func_node.args.args:
        alloc(arg.arg)

    tokens = []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node is func_node:
                tokens.append('FUNC')
                tokens.append('v_func')  # function name placeholder
                tokens.append('(')
                for arg in node.args.args:
                    tokens.append(alloc(arg.arg))
                tokens.append(')')
                tokens.append('{')
                for child in node.body:
                    visit(child)
                tokens.append('}')
            else:
                # Nested function
                tokens.append('FUNC')
                alloc(node.name)
                tokens.append(alloc(node.name))
                tokens.append('(')
                for arg in node.args.args:
                    tokens.append(alloc(arg.arg))
                tokens.append(')')
                tokens.append('{')
                for child in node.body:
                    visit(child)
                tokens.append('}')

        elif isinstance(node, ast.Return):
            tokens.append('RETURN')
            if node.value:
                visit(node.value)

        elif isinstance(node, ast.Assign):
            for t in node.targets:
                # Allocate name before visiting so it gets a canonical id
                if isinstance(t, ast.Name):
                    alloc(t.id)
                visit(t)
            tokens.append('=')
            visit(node.value)

        elif isinstance(node, ast.AugAssign):
            visit(node.target)
            tokens.append(type(node.op).__name__.upper() + '=')
            visit(node.value)

        elif isinstance(node, ast.AnnAssign):
            # x: int = value — strip annotation
            if node.target:
                visit(node.target)
            if node.value:
                tokens.append('=')
                visit(node.value)

        elif isinstance(node, ast.Name):
            n = node.id
            if n in PY_KEYWORD_MAP:
                tokens.append(PY_KEYWORD_MAP[n])
            elif n in ('True', 'False', 'None'):
                tokens.append({'True': 'TRUE', 'False': 'FALSE', 'None': 'NULL'}[n])
            else:
                tokens.append(rename.get(n, n))

        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                tokens.append('STR')
            elif isinstance(node.value, (int, float)):
                tokens.append('NUM')
            elif node.value is None:
                tokens.append('NULL')
            elif isinstance(node.value, bool):
                tokens.append('TRUE' if node.value else 'FALSE')
            else:
                tokens.append('CONST')

        elif isinstance(node, ast.BinOp):
            visit(node.left)
            tokens.append(type(node.op).__name__.upper())
            visit(node.right)

        elif isinstance(node, ast.UnaryOp):
            tokens.append(type(node.op).__name__.upper())
            visit(node.operand)

        elif isinstance(node, ast.BoolOp):
            op = 'AND' if isinstance(node.op, ast.And) else 'OR'
            for i, v in enumerate(node.values):
                if i > 0:
                    tokens.append(op)
                visit(v)

        elif isinstance(node, ast.Compare):
            visit(node.left)
            for op, comp in zip(node.ops, node.comparators):
                tokens.append(type(op).__name__.upper())
                visit(comp)

        elif isinstance(node, ast.Call):
            visit(node.func)
            tokens.append('(')
            for arg in node.args:
                visit(arg)
            tokens.append(')')

        elif isinstance(node, ast.If):
            tokens.append('IF')
            tokens.append('(')
            visit(node.test)
            tokens.append(')')
            tokens.append('{')
            for child in node.body:
                visit(child)
            tokens.append('}')
            if node.orelse:
                tokens.append('ELSE')
                tokens.append('{')
                for child in node.orelse:
                    visit(child)
                tokens.append('}')

        elif isinstance(node, ast.While):
            tokens.append('WHILE')
            tokens.append('(')
            visit(node.test)
            tokens.append(')')
            tokens.append('{')
            for child in node.body:
                visit(child)
            tokens.append('}')

        elif isinstance(node, ast.For):
            tokens.append('FOR')
            tokens.append('(')
            visit(node.target)
            tokens.append('IN')
            visit(node.iter)
            tokens.append(')')
            tokens.append('{')
            for child in node.body:
                visit(child)
            tokens.append('}')

        elif isinstance(node, ast.Expr):
            visit(node.value)

        elif isinstance(node, ast.Attribute):
            visit(node.value)
            tokens.append('.')
            tokens.append(node.attr)

        elif isinstance(node, ast.Subscript):
            visit(node.value)
            tokens.append('[')
            visit(node.slice)
            tokens.append(']')

        elif isinstance(node, ast.List):
            tokens.append('[')
            for elt in node.elts:
                visit(elt)
            tokens.append(']')

        elif isinstance(node, ast.Dict):
            tokens.append('{')
            for k, v in zip(node.keys, node.values):
                if k:
                    visit(k)
                tokens.append(':')
                visit(v)
            tokens.append('}')

        elif isinstance(node, ast.Tuple):
            tokens.append('(')
            for elt in node.elts:
                visit(elt)
            tokens.append(')')

        elif isinstance(node, ast.IfExp):
            visit(node.body)
            tokens.append('IF')
            visit(node.test)
            tokens.append('ELSE')
            visit(node.orelse)

        elif isinstance(node, (ast.Pass, ast.Break, ast.Continue)):
            tokens.append(type(node).__name__.upper())

        elif isinstance(node, ast.Raise):
            tokens.append('THROW')
            if node.exc:
                visit(node.exc)

        elif isinstance(node, ast.Try):
            tokens.append('TRY')
            tokens.append('{')
            for child in node.body:
                visit(child)
            tokens.append('}')
            for handler in node.handlers:
                tokens.append('CATCH')
                tokens.append('{')
                for child in handler.body:
                    visit(child)
                tokens.append('}')

        # Ignore type annotations, docstrings etc.

    visit(func_node)
    return tokens


# ─────────────────────────────────────────────
#  JavaScript/TypeScript → Universal canonical tokens
# ─────────────────────────────────────────────

def js_func_to_universal(params: List[str], body_src: str) -> List[str]:
    """
    Convert JS/TS function params + body to universal canonical token sequence.
    """
    rename: Dict[str, str] = {}
    counter = [0]

    def alloc(name: str) -> str:
        if name not in rename:
            rename[name] = f"v{counter[0]}"
            counter[0] += 1
        return rename[name]

    for p in params:
        alloc(p)

    raw_tokens = js_tokenize(body_src)

    # Find local assignments (after colon-filter would run, so use raw)
    for i, (kind, val, _) in enumerate(raw_tokens):
        if kind == 'WORD' and i + 1 < len(raw_tokens):
            nk, nv, _ = raw_tokens[i + 1]
            if nk == 'OP' and nv == '=':
                alloc(val)
        # Also catch: const/let/var result = ...
        if kind == 'KW' and val in ('const', 'let', 'var'):
            if i + 1 < len(raw_tokens) and raw_tokens[i+1][0] == 'WORD':
                alloc(raw_tokens[i+1][1])

    tokens = []
    tokens.append('FUNC')
    tokens.append('v_func')
    tokens.append('(')
    for p in params:
        tokens.append(alloc(p))
    tokens.append(')')

    # Filter out lone colon type tokens (TS remnants like `: number`)
    filtered_tokens = []
    i2 = 0
    while i2 < len(raw_tokens):
        k, v, ln = raw_tokens[i2]
        # Skip `: WORD` patterns (type annotations that slipped through)
        if k == 'PUNCT' and v == ':' and i2 + 1 < len(raw_tokens) and raw_tokens[i2+1][0] in ('WORD', 'KW'):
            i2 += 2  # skip colon and type name
            continue
        filtered_tokens.append((k, v, ln))
        i2 += 1
    raw_tokens = filtered_tokens

    for kind, val, _ in raw_tokens:
        if kind == 'KW':
            universal = JS_KEYWORD_MAP.get(val, val.upper())
            # Skip variable declaration keywords — Python has none
            if universal == 'LET':
                continue
            tokens.append(universal)
        elif kind == 'WORD':
            tokens.append(rename.get(val, val))
        elif kind == 'STRING':
            tokens.append('STR')
        elif kind == 'NUMBER':
            tokens.append('NUM')
        elif kind == 'OP':
            # Normalise operators to match Python AST names
            op_norm = {
                '+': 'ADD', '-': 'SUB', '*': 'MULT', '/': 'DIV',
                '%': 'MOD', '**': 'POW', '//': 'FLOORDIV',
                '==': 'EQ', '!=': 'NOTEQ', '<': 'LT', '>': 'GT',
                '<=': 'LTE', '>=': 'GTE', '===': 'EQ', '!==': 'NOTEQ',
                '&&': 'AND', '||': 'OR', '!': 'NOT',
                '=': '=', '+=': 'ADD=', '-=': 'SUB=',
                '=>': 'ARROW',
            }.get(val, val)
            tokens.append(op_norm)
        elif kind == 'ARROW':
            tokens.append('ARROW')
        elif kind == 'PUNCT' and val == ';':
            pass  # skip semicolons — Python has none
        else:
            tokens.append(val)

    return tokens


# ─────────────────────────────────────────────
#  Universal hash
# ─────────────────────────────────────────────

def universal_hash(tokens: List[str]) -> str:
    """Hash a universal canonical token sequence."""
    canonical = " ".join(tokens)
    return hashlib.sha256(
        json.dumps({"universal_sir": canonical}, sort_keys=True,
                   separators=(',', ':')).encode()
    ).hexdigest()


# ─────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────

def hash_python_functions(src: str, filename: str) -> List[Tuple[str, int, str]]:
    """Returns (qualname, lineno, universal_hash) for each Python function."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    results = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            tokens = python_func_to_universal(node, src)
            if tokens:
                h = universal_hash(tokens)
                results.append((node.name, node.lineno, h))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    tokens = python_func_to_universal(sub, src)
                    if tokens:
                        h = universal_hash(tokens)
                        results.append((f"{node.name}.{sub.name}", sub.lineno, h))
    return results


def hash_js_functions_universal(src: str, filename: str) -> List[Tuple[str, int, str]]:
    """Returns (qualname, lineno, universal_hash) for each JS/TS function."""
    if filename.endswith(('.ts', '.tsx')):
        src = strip_typescript(src)

    funcs = extract_js_functions(src, filename)
    results = []
    for name, lineno, params, body_src in funcs:
        tokens = js_func_to_universal(params, body_src)
        h = universal_hash(tokens)
        results.append((name, lineno, h))
    return results


def hash_file_universal(src: str, filename: str) -> List[Tuple[str, int, str]]:
    """Auto-detect language and return universal hashes."""
    if filename.endswith('.py'):
        return hash_python_functions(src, filename)
    elif filename.endswith(('.js', '.ts', '.jsx', '.tsx')):
        return hash_js_functions_universal(src, filename)
    return []


# ─────────────────────────────────────────────
#  CLI test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Test cross-language equivalence
    py_src = """
def add(x, y):
    result = x + y
    return result
"""
    js_src = """
function add(x, y) {
    const result = x + y;
    return result;
}
"""
    ts_src = """
function add(x: number, y: number): number {
    const result: number = x + y;
    return result;
}
"""

    py_results = hash_python_functions(py_src, "test.py")
    js_results = hash_js_functions_universal(js_src, "test.js")
    ts_results = hash_js_functions_universal(ts_src, "test.ts")

    print("Cross-language equivalence test:")
    print(f"  Python  add: {py_results[0][2][:32]}...")
    print(f"  JS      add: {js_results[0][2][:32]}...")
    print(f"  TS      add: {ts_results[0][2][:32]}...")
    print()
    if py_results[0][2] == js_results[0][2] == ts_results[0][2]:
        print("✅ ALL THREE MATCH — cross-language equivalence works!")
    else:
        print("❌ Hashes differ — canonical forms not identical yet")
        # Debug
        from sir_universal import python_func_to_universal, js_func_to_universal, extract_js_functions, js_tokenize, strip_typescript
        import ast
        tree = ast.parse(py_src.strip())
        py_tokens = python_func_to_universal(tree.body[0], py_src)
        print("\nPython tokens:", " ".join(py_tokens[:20]))

        src2 = strip_typescript(ts_src)
        funcs = extract_js_functions(src2, "test.ts")
        js_tokens = js_func_to_universal(funcs[0][2], funcs[0][3])
        print("JS tokens:    ", " ".join(js_tokens[:20]))
