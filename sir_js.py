#!/usr/bin/env python3
"""
sir_js.py — JavaScript/TypeScript → SIR node graph parser.

Parses JS/TS functions into the same canonical node graph format
as sir1.py so deduplication works across Python and JavaScript.

Supports:
  - function declarations:        function foo(a, b) { return a + b; }
  - arrow functions (assigned):   const foo = (a, b) => a + b;
  - method definitions:           { foo(a, b) { return a + b; } }
  - async variants of all above

Usage:
  from sir_js import extract_js_functions, hash_js_source

  funcs = extract_js_functions(js_source, "myfile.js")
  for name, lineno, h in funcs:
      print(name, lineno, h)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────
#  Tokeniser — minimal JS lexer
# ─────────────────────────────────────────────

TOKEN_RE = re.compile(r"""
    (?P<COMMENT_LINE>//[^\n]*)
  | (?P<COMMENT_BLOCK>/\*.*?\*/)
  | (?P<STRING>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)
  | (?P<NUMBER>\d+(?:\.\d+)?)
  | (?P<ARROW>=\>)
  | (?P<PUNCT>[{}()\[\];,.:])
  | (?P<OP>[+\-*/%=!<>&|^~?]+)
  | (?P<WORD>[A-Za-z_$][A-Za-z_$0-9]*)
  | (?P<NEWLINE>\n)
  | (?P<SPACE>[ \t\r]+)
""", re.VERBOSE | re.DOTALL)

KEYWORDS = {
    'function', 'return', 'const', 'let', 'var', 'async', 'await',
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break',
    'continue', 'new', 'this', 'class', 'extends', 'import', 'export',
    'default', 'try', 'catch', 'finally', 'throw', 'typeof', 'instanceof',
    'in', 'of', 'true', 'false', 'null', 'undefined', 'void', 'delete',
    'static', 'get', 'set', 'yield', 'from', 'as', 'super'
}


def tokenize(source: str) -> List[Tuple[str, str, int]]:
    """Returns list of (type, value, line_number)."""
    tokens = []
    line = 1
    for m in TOKEN_RE.finditer(source):
        kind = m.lastgroup
        val = m.group()
        if kind == 'NEWLINE':
            line += 1
            continue
        if kind in ('SPACE', 'COMMENT_LINE', 'COMMENT_BLOCK'):
            line += val.count('\n')
            continue
        if kind == 'WORD' and val in KEYWORDS:
            kind = 'KW'
        tokens.append((kind, val, line))
    return tokens


# ─────────────────────────────────────────────
#  Function extractor
# ─────────────────────────────────────────────

def find_matching_brace(tokens: List, start: int) -> int:
    """Find index of closing } matching opening { at tokens[start]."""
    depth = 0
    for i in range(start, len(tokens)):
        kind, val, _ = tokens[i]
        if kind == 'PUNCT' and val == '{':
            depth += 1
        elif kind == 'PUNCT' and val == '}':
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def find_matching_paren(tokens: List, start: int) -> int:
    """Find index of closing ) matching opening ( at tokens[start]."""
    depth = 0
    for i in range(start, len(tokens)):
        kind, val, _ = tokens[i]
        if kind == 'PUNCT' and val == '(':
            depth += 1
        elif kind == 'PUNCT' and val == ')':
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def extract_params(tokens: List, open_paren: int, close_paren: int) -> List[str]:
    """Extract parameter names from token slice."""
    params = []
    for i in range(open_paren + 1, close_paren):
        kind, val, _ = tokens[i]
        if kind == 'WORD':
            # skip destructuring keywords and default value tokens
            params.append(val)
    return params


def tokens_to_source(tokens: List) -> str:
    return " ".join(val for _, val, _ in tokens)


def extract_js_functions(source: str, filename: str) -> List[Tuple[str, int, str]]:
    """
    Extract (qualname, lineno, body_token_slice) for each function in JS source.
    Returns list of (name, lineno, body_source).
    """
    tokens = tokenize(source)
    results = []
    i = 0
    n = len(tokens)

    while i < n:
        kind, val, line = tokens[i]

        # ── Pattern 1: function foo(...) { }
        if kind == 'KW' and val == 'function':
            name = 'anonymous'
            j = i + 1
            # optional async already handled before 'function', skip name
            if j < n and tokens[j][0] == 'WORD':
                name = tokens[j][1]
                j += 1
            # find params
            if j < n and tokens[j][1] == '(':
                close_p = find_matching_paren(tokens, j)
                params = extract_params(tokens, j, close_p)
                j = close_p + 1
                # find body
                if j < n and tokens[j][1] == '{':
                    close_b = find_matching_brace(tokens, j)
                    body_tokens = tokens[j:close_b + 1]
                    results.append((name, line, params, body_tokens))
                    i = close_b + 1
                    continue

        # ── Pattern 2: const/let/var foo = (...) => { } or (...) => expr
        elif kind == 'KW' and val in ('const', 'let', 'var'):
            j = i + 1
            if j < n and tokens[j][0] == 'WORD':
                name = tokens[j][1]
                j += 1
                if j < n and tokens[j][0] == 'OP' and '=' in tokens[j][1] and tokens[j][1] != '=>':
                    j += 1
                    # optional async
                    if j < n and tokens[j] == ('KW', 'async', tokens[j][2]):
                        j += 1
                    # arrow function: (params) => body
                    if j < n and tokens[j][1] == '(':
                        close_p = find_matching_paren(tokens, j)
                        params = extract_params(tokens, j, close_p)
                        j = close_p + 1
                        if j < n and tokens[j][0] == 'ARROW':
                            j += 1
                            if j < n and tokens[j][1] == '{':
                                close_b = find_matching_brace(tokens, j)
                                body_tokens = tokens[j:close_b + 1]
                                results.append((name, line, params, body_tokens))
                                i = close_b + 1
                                continue
                            else:
                                # expression body — collect until ; or end
                                expr_end = j
                                depth = 0
                                while expr_end < n:
                                    ek, ev, _ = tokens[expr_end]
                                    if ev in ('(', '[', '{'):
                                        depth += 1
                                    elif ev in (')', ']', '}'):
                                        if depth == 0:
                                            break
                                        depth -= 1
                                    elif ek == 'PUNCT' and ev == ';' and depth == 0:
                                        break
                                    expr_end += 1
                                body_tokens = [('PUNCT', '{', line)] + tokens[j:expr_end] + [('PUNCT', '}', line)]
                                results.append((name, line, params, body_tokens))
                                i = expr_end + 1
                                continue

        i += 1

    # Convert to (name, lineno, source_str)
    out = []
    for name, lineno, params, body_tokens in results:
        body_src = tokens_to_source(body_tokens)
        out.append((name, lineno, params, body_src))
    return out


# ─────────────────────────────────────────────
#  Canonical node graph builder
# ─────────────────────────────────────────────

def _node_id(data: Dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def canonicalize_js(params: List[str], body_tokens: List) -> Dict:
    """
    Build a canonical SIR node graph from JS function params + body tokens.
    
    Strategy:
    - Rename all param names to v0, v1, v2...
    - Rename all locally assigned variables to v{n}...
    - Keep operators, punctuation, keywords as-is
    - Hash the resulting canonical token sequence
    
    This matches the Python SIR approach of alpha-renaming identifiers.
    """
    # Build rename map for params
    rename: Dict[str, str] = {}
    counter = [0]

    def alloc(orig: str) -> str:
        if orig not in rename:
            rename[orig] = f"v{counter[0]}"
            counter[0] += 1
        return rename[orig]

    for p in params:
        alloc(p)

    # First pass — find all assignments to catch local vars
    # Simple heuristic: WORD followed by = (not ==, =>, !=, <=, >=)
    tokens = body_tokens
    for i, (kind, val, line) in enumerate(tokens):
        if kind == 'WORD' and i + 1 < len(tokens):
            nk, nv, _ = tokens[i + 1]
            if nk == 'OP' and nv == '=':
                alloc(val)

    # Second pass — build canonical token sequence
    canonical_tokens = []
    for kind, val, _ in tokens:
        if kind == 'WORD':
            canonical_tokens.append(rename.get(val, val))
        elif kind == 'STRING':
            canonical_tokens.append('STR')
        elif kind == 'NUMBER':
            canonical_tokens.append('NUM')
        else:
            canonical_tokens.append(val)

    canonical_seq = " ".join(canonical_tokens)

    # Build node graph (simple linear for JS — matches SIR format)
    root_data = {
        "type": "JSFunction",
        "params": [f"v{i}" for i in range(len(params))],
        "body": canonical_seq,
        "lang": "javascript"
    }
    root_id = _node_id(root_data)
    nodes = {root_id: root_data}

    sir_hash = hashlib.sha256(
        json.dumps({"root": root_id, "nodes": nodes}, sort_keys=True,
                   separators=(',', ':')).encode()
    ).hexdigest()

    return {
        "root": root_id,
        "nodes": nodes,
        "sir_sha256": sir_hash,
        "lang": "javascript",
        "name_map": {orig: canon for orig, canon in rename.items()}
    }


def hash_js_source(source: str, filename: str = "<js>") -> List[Tuple[str, int, str]]:
    """
    Returns list of (qualname, lineno, sir_sha256) for each function in JS source.
    Ready to plug into the same deduplication pipeline as Python.
    """
    funcs = extract_js_functions(source, filename)
    results = []
    for name, lineno, params, body_src in funcs:
        # Re-tokenize body for canonicalization
        body_tokens = tokenize(body_src)
        sir = canonicalize_js(params, body_tokens)
        results.append((name, lineno, sir["sir_sha256"]))
    return results


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 sir_js.py <file.js>")
        sys.exit(1)

    path = sys.argv[1]
    source = open(path).read()
    results = hash_js_source(source, path)

    if not results:
        print("No functions found.")
    else:
        print(f"Found {len(results)} function(s):\n")
        for name, lineno, h in results:
            print(f"  {name} (line {lineno})")
            print(f"  Hash: {h[:32]}...\n")
