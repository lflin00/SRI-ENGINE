"""
SIR Engine Core (extracted from sir1.py v0.2)

This module provides an importable API for the SIR-1 representation system.
It contains the encoding/canonicalization, hashing, and decoding logic without CLI parsing.

Public API:
- encode(source_text, mode="exact"|"semantic") -> dict (SIR JSON)
- encode_file(path, mode=...) -> dict
- hash_source(source_text, mode=...) -> str (semantic/exact SIR hash)
- hash_file(path, mode=...) -> str
- decode_sir(sir_obj, rehydrate=False) -> str
- decode_file(sir_json_path, rehydrate=False) -> str

Notes:
- Requires Python 3.9+ for ast.unparse().
- Decoding regenerates equivalent code, not original formatting/comments.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


JSON = Union[None, bool, int, float, str, List["JSON"], Dict[str, "JSON"]]


def b16_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CanonConfig:
    mode: str  # "exact" or "semantic"


class AlphaRenamer(ast.NodeTransformer):
    """
    Deterministic alpha-renaming for local variables and function arguments.

    Goal: make equivalent programs with different variable names map closer together.
    Caveat: This is conservative and does NOT attempt full scope analysis for
    complex cases (globals/nonlocals/class attrs). It focuses on:
      - function args
      - local variable names (Store/Del contexts)
      - comprehension targets
    """
    def __init__(self) -> None:
        super().__init__()
        self.stack: List[Dict[str, str]] = []
        self.counters: List[int] = []

    def _push_scope(self) -> None:
        self.stack.append({})
        self.counters.append(0)

    def _pop_scope(self) -> None:
        self.stack.pop()
        self.counters.pop()

    def _map_name(self, name: str) -> str:
        if not self.stack:
            return name
        scope = self.stack[-1]
        if name in scope:
            return scope[name]
        new = f"v{self.counters[-1]}"
        scope[name] = new
        self.counters[-1] += 1
        return new

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._push_scope()
        node.args = self.visit(node.args)
        node.body = [self.visit(n) for n in node.body]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.returns = self.visit(node.returns) if node.returns else None
        self._pop_scope()
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return self.visit_FunctionDef(node)  # same handling

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        self._push_scope()
        node.args = self.visit(node.args)
        node.body = self.visit(node.body)
        self._pop_scope()
        return node

    def visit_arguments(self, node: ast.arguments) -> Any:
        for a in node.posonlyargs:
            a.arg = self._map_name(a.arg)
        for a in node.args:
            a.arg = self._map_name(a.arg)
        if node.vararg:
            node.vararg.arg = self._map_name(node.vararg.arg)
        for a in node.kwonlyargs:
            a.arg = self._map_name(a.arg)
        if node.kwarg:
            node.kwarg.arg = self._map_name(node.kwarg.arg)
        node.defaults = [self.visit(d) for d in node.defaults]
        node.kw_defaults = [self.visit(d) if d else None for d in node.kw_defaults]
        return node

    def visit_Name(self, node: ast.Name) -> Any:
        # Only rename local-like names. Avoid changing attribute access (handled by ast.Attribute).
        if isinstance(node.ctx, (ast.Store, ast.Del, ast.Param, ast.Load)):
            if self.stack:
                node.id = self._map_name(node.id)
        return node

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        node.target = self.visit(node.target)
        node.iter = self.visit(node.iter)
        node.ifs = [self.visit(i) for i in node.ifs]
        return node


class SIRBuilder:
    """
    Build a node graph:
      nodes: {node_id: {"t": <type>, "f": {field: value/ref}}}
      root: node_id

    Canonical properties:
    - node_id is sha256 over node content (type + normalized fields) => stable, content-addressed.
    - fields use a deterministic ordering.
    """
    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, JSON]] = {}

    def _freeze(self, obj: JSON) -> JSON:
        # Ensure all dict keys are sorted deterministically.
        if isinstance(obj, dict):
            return {k: self._freeze(obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, list):
            return [self._freeze(x) for x in obj]
        return obj

    def _node_id(self, t: str, f: Dict[str, JSON]) -> str:
        payload = {"t": t, "f": self._freeze(f)}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return b16_sha256(data)

    def add_node(self, t: str, f: Dict[str, JSON]) -> str:
        f_frozen = self._freeze(f)
        nid = self._node_id(t, f_frozen)
        if nid not in self.nodes:
            self.nodes[nid] = {"t": t, "f": f_frozen}
        return nid

    def build(self, node: ast.AST) -> str:
        return self._visit(node)

    def _visit(self, node: Any) -> str:
        if node is None:
            return self.add_node("None", {})
        if isinstance(node, (bool, int, float, str)):
            return self.add_node("Lit", {"v": node, "k": type(node).__name__})
        if isinstance(node, bytes):
            return self.add_node("Lit", {"v": node.hex(), "k": "bytes_hex"})

        if isinstance(node, list):
            items = [self._visit(x) for x in node]
            return self.add_node("List", {"items": items})

        if not isinstance(node, ast.AST):
            # Fallback: represent unknown types as string
            return self.add_node("Lit", {"v": repr(node), "k": "repr"})

        t = type(node).__name__
        fields: Dict[str, JSON] = {}

        # Common fields from ast.AST
        for fname, value in ast.iter_fields(node):
            # Skip location metadata for canonicalization
            if fname in ("lineno", "col_offset", "end_lineno", "end_col_offset", "type_comment"):
                continue

            # Represent operator/context singletons by their class name
            if isinstance(value, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context)):
                fields[fname] = type(value).__name__
                continue

            # Regular recursion
            if isinstance(value, ast.AST) or isinstance(value, list) or value is None or isinstance(value, (bool, int, float, str, bytes)):
                fields[fname] = self._visit(value)
            else:
                fields[fname] = self._visit(repr(value))

        return self.add_node(t, fields)


def load_source(path: str) -> str:
    if path == "-" or path == "/dev/stdin":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def dump_json(obj: Any, out_path: Optional[str]) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
    else:
        print(text)


def encode_to_sir(source: str, cfg: CanonConfig) -> Dict[str, Any]:
    tree = ast.parse(source)

    if cfg.mode == "semantic":
        tree = AlphaRenamer().visit(tree)
        ast.fix_missing_locations(tree)

    builder = SIRBuilder()
    root = builder.build(tree)

    sir = {
        "format": "SIR-1",
        "version": "0.1",
        "mode": cfg.mode,
        "root": root,
        "nodes": builder.nodes,  # content-addressed graph
    }
    return sir


def sir_hash(sir: Dict[str, Any]) -> str:
    # Hash only the canonical content: root + nodes (sorted keys, stable separators).
    data = json.dumps(
        {"root": sir["root"], "nodes": sir["nodes"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return b16_sha256(data)



# ---------------- Public, stable API ----------------

def encode(source_text: str, mode: str = "exact") -> Dict[str, Any]:
    """Encode Python source into SIR JSON dict."""
    cfg = CanonConfig(mode=mode)
    return encode_to_sir(source_text, cfg)


def encode_file(path: str, mode: str = "exact") -> Dict[str, Any]:
    """Encode a .py file (or "-" for stdin) into SIR JSON dict."""
    src = load_source(path)
    return encode(src, mode=mode)


def hash_source(source_text: str, mode: str = "exact") -> str:
    """Compute the canonical SIR hash for Python source (matches sir1.py hash output)."""
    sir = encode(source_text, mode=mode)
    return sir_hash(sir)


def hash_file(path: str, mode: str = "exact") -> str:
    """Compute the canonical SIR hash for a .py file (or "-" for stdin)."""
    return hash_source(load_source(path), mode=mode)


def decode_sir(sir_obj: Dict[str, Any], rehydrate: bool = False) -> str:
    """Decode SIR JSON dict back to Python source string."""
    return decode_sir_to_source(sir_obj, rehydrate=rehydrate)


def decode_file(sir_json_path: str, rehydrate: bool = False) -> str:
    """Decode SIR JSON from file path into Python source string."""
    raw = load_source(sir_json_path)
    obj = json.loads(raw)
    if not isinstance(obj, dict) or "nodes" not in obj or "root" not in obj:
        raise ValueError("Input does not look like a SIR JSON object.")
    return decode_sir(obj, rehydrate=rehydrate)

