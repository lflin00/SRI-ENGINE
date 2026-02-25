#!/usr/bin/env python3
"""
sir_js.py — JavaScript/TypeScript → SIR node graph parser.

Parses JS/TS functions into the same canonical node graph format
as sir1.py so deduplication works across Python and JavaScript/TypeScript.

Supports:
  - function declarations:        function foo(a, b) { return a + b; }
  - arrow functions (assigned):   const foo = (a, b) => a + b;
  - async variants of all above
  - TypeScript type annotations:  function foo(a: number, b: string): boolean
  - TypeScript generics:          function identity<T>(arg: T): T
  - TypeScript interfaces/types:  stripped before parsing
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────
#  TypeScript pre-processor
#  Strips TS-specific syntax before tokenising
# ─────────────────────────────────────────────

def strip_typescript(source: str) -> str:
    """
    Remove TypeScript-specific syntax to produce clean JS.
    Handles: type annotations, generics, interfaces, type aliases,
    access modifiers, decorators, non-null assertions.
    """
    # Remove single-line comments first to avoid false matches
    # (we restore them after — actually just remove them for analysis)
    lines = source.split('\n')
    cleaned = []
    in_block_comment = False

    for line in lines:
        if in_block_comment:
            if '*/' in line:
                line = line[line.index('*/') + 2:]
                in_block_comment = False
            else:
                cleaned.append('')
                continue
        if '/*' in line and '*/' not in line[line.index('/*'):]:
            line = line[:line.index('/*')]
            in_block_comment = True
        # Remove line comments
        if '//' in line:
            # Be careful not to strip URLs inside strings
            line = re.sub(r'(?<!:)//.*$', '', line)
        cleaned.append(line)

    source = '\n'.join(cleaned)

    # Remove decorators (@Something)
    source = re.sub(r'@\w+(?:\([^)]*\))?\s*\n', '\n', source)

    # Remove interface declarations
    source = re.sub(r'\binterface\s+\w+\s*\{[^}]*\}', '', source, flags=re.DOTALL)

    # Remove type alias declarations
    source = re.sub(r'\btype\s+\w+\s*=\s*[^;]+;', '', source)

    # Remove generic type parameters <T>, <T extends U>, <T, U>
    # Do multiple passes for nested generics
    for _ in range(3):
        source = re.sub(r'<[A-Za-z_,\s\[\]?|&.extends=\'"]+>', '', source)

    # Remove return type annotations: ): Type {  or ): Type =>
    source = re.sub(r'\)\s*:\s*[\w\[\]|&<>.,\s?]+(?=\s*[\{=])', ')', source)

    # Remove parameter type annotations: (a: Type, b: Type[])
    # Handle complex types: string[], number | null, Record<string, any>
    source = re.sub(r'(\w+)\s*\??\s*:\s*[\w\[\]|&<>.,\s?]+(?=[,)])', r'\1', source)

    # Remove access modifiers in class constructors
    source = re.sub(r'\b(public|private|protected|readonly)\s+', '', source)

    # Remove non-null assertions
    source = re.sub(r'(\w+)!\.', r'\1.', source)
    source = re.sub(r'(\w+)!(?=[,)\s;])', r'\1', source)

    # Remove 'as Type' casts
    source = re.sub(r'\bas\s+\w[\w<>\[\]|&.,\s]*', '', source)

    # Remove 'abstract' keyword
    source = re.sub(r'\babstract\s+', '', source)

    return source


# ─────────────────────────────────────────────
#  Tokeniser
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
    'static', 'get', 'set', 'yield', 'from', 'as', 'super',
    # TypeScript keywords (kept for awareness, stripped in pre-processor)
    'interface', 'type', 'enum', 'namespace', 'declare', 'abstract',
    'implements', 'readonly', 'keyof', 'infer', 'never', 'unknown'
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
    params = []
    for i in range(open_paren + 1, close_paren):
        kind, val, _ = tokens[i]
        if kind == 'WORD':
            params.append(val)
    return params


def tokens_to_source(tokens: List) -> str:
    return " ".join(val for _, val, _ in tokens)


def extract_js_functions(source: str, filename: str) -> List[Tuple[str, int, List[str], str]]:
    """
    Extract (qualname, lineno, params, body_source) for each function.
    Handles both JS and TS (strips TS annotations first).
    """
    # Detect TypeScript
    is_ts = filename.endswith(('.ts', '.tsx'))
    if is_ts:
        source = strip_typescript(source)

    tokens = tokenize(source)
    results = []
    i = 0
    n = len(tokens)

    while i < n:
        kind, val, line = tokens[i]

        # ── Pattern 1: [async] function name(...) { }
        is_async = kind == 'KW' and val == 'async'
        start_i = i
        if is_async and i + 1 < n:
            i += 1
            kind, val, line = tokens[i]

        if kind == 'KW' and val == 'function':
            name = 'anonymous'
            j = i + 1
            if j < n and tokens[j][0] == 'WORD':
                name = tokens[j][1]
                j += 1
            if j < n and tokens[j][1] == '(':
                close_p = find_matching_paren(tokens, j)
                params = extract_params(tokens, j, close_p)
                j = close_p + 1
                if j < n and tokens[j][1] == '{':
                    close_b = find_matching_brace(tokens, j)
                    body_tokens = tokens[j:close_b + 1]
                    results.append((name, line, params, body_tokens))
                    i = close_b + 1
                    continue
            i = start_i + 1
            continue

        # ── Pattern 2: const/let/var name = [async] (...) => body
        elif kind == 'KW' and val in ('const', 'let', 'var'):
            j = i + 1
            if j < n and tokens[j][0] == 'WORD':
                name = tokens[j][1]
                j += 1
                # expect = (not ==, =>, !=)
                if j < n and tokens[j][0] == 'OP' and tokens[j][1] == '=':
                    j += 1
                    # optional async
                    if j < n and tokens[j][0] == 'KW' and tokens[j][1] == 'async':
                        j += 1
                    # arrow function with parens
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
                                # expression body
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
                    # arrow with single param no parens: const foo = x => x + 1
                    elif j < n and tokens[j][0] == 'WORD':
                        param_name = tokens[j][1]
                        j += 1
                        if j < n and tokens[j][0] == 'ARROW':
                            j += 1
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
                            results.append((name, line, [param_name], body_tokens))
                            i = expr_end + 1
                            continue

        i += 1

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
    Build canonical SIR node graph from JS/TS function params + body tokens.
    Alpha-renames all identifiers to v0, v1, v2... matching Python SIR approach.
    """
    rename: Dict[str, str] = {}
    counter = [0]

    def alloc(orig: str) -> str:
        if orig not in rename:
            rename[orig] = f"v{counter[0]}"
            counter[0] += 1
        return rename[orig]

    for p in params:
        alloc(p)

    # Find local assignments
    tokens = body_tokens
    for i, (kind, val, line) in enumerate(tokens):
        if kind == 'WORD' and i + 1 < len(tokens):
            nk, nv, _ = tokens[i + 1]
            if nk == 'OP' and nv == '=':
                alloc(val)

    # Build canonical token sequence
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
    """Returns list of (qualname, lineno, sir_sha256) for each function."""
    funcs = extract_js_functions(source, filename)
    results = []
    for name, lineno, params, body_src in funcs:
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
        print("Usage: python3 sir_js.py <file.js|file.ts>")
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
