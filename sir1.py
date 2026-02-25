#!/usr/bin/env python3
"""
sir1.py — SIR-1 (Structured Intermediate Representation) prototype for Python code (v0.2)

What it does:
- encode: Python source -> canonical node-graph SIR JSON.
- hash:   print SHA-256 of canonical SIR (root + nodes).
- decode: SIR JSON -> Python source (via AST rebuild + ast.unparse()).

Modes:
- exact:    preserves original identifiers (still canonicalizes formatting because AST-based).
- semantic: alpha-renames functions/args/locals to canonical names AND stores a reversible name_map.

Best long-term option (hybrid):
- Use semantic mode for canonical structural equality + dedup.
- Use the embedded name_map to rehydrate original identifiers on decode when desired.

USAGE
-----
# Encode
python3 sir1.py encode path/to/file.py -o out.sir.json
python3 sir1.py encode path/to/file.py --mode semantic -o out.semantic.sir.json

# Hash (useful for equality testing)
python3 sir1.py hash file.py
python3 sir1.py hash file.py --mode semantic

# Decode SIR back to Python (format normalized)
python3 sir1.py decode out.sir.json -o restored.py

# Decode + rehydrate original names (only works if SIR contains name_map)
python3 sir1.py decode out.semantic.sir.json -o restored.py --rehydrate

NOTES
-----
- Requires Python 3.9+ for ast.unparse().
- Decoding recreates equivalent code, not original formatting/comments.
- Semantic decode WITHOUT rehydrate may change runtime behavior in reflection-heavy code.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union, Tuple


JSON = Union[None, bool, int, float, str, List["JSON"], Dict[str, "JSON"]]
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def b16_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CanonConfig:
    mode: str  # "exact" or "semantic"


class AlphaRenamer(ast.NodeTransformer):
    """
    Deterministic alpha-renaming for Python code.

    In --mode semantic, equivalent programs with different names should map to the same SIR:
      - Function names at module scope -> f0, f1, ...
      - Function args + assigned locals -> v0, v1, ...
      - Loads are renamed only if mapped in the current scope (avoids renaming globals/builtins)

    Also records a reversible name_map for later rehydration.
    """
    def __init__(self) -> None:
        super().__init__()
        self.scope_stack: List[Dict[str, str]] = []
        self.local_counters: List[int] = []
        self.func_counter: int = 0

        # Collected per-function maps, in encounter order.
        # Each entry: {"canon_func": "f0", "orig_func": "name", "canon_to_orig": {...}}
        self.name_map: Dict[str, Any] = {"functions": []}
        self._current_func_orig: List[str] = []
        self._current_func_canon: List[str] = []

    def _push_scope(self) -> None:
        self.scope_stack.append({})
        self.local_counters.append(0)

    def _pop_scope(self) -> Dict[str, str]:
        scope = self.scope_stack.pop()
        self.local_counters.pop()
        return scope

    def _alloc_local(self, orig: str) -> str:
        scope = self.scope_stack[-1]
        if orig in scope:
            return scope[orig]
        new = f"v{self.local_counters[-1]}"
        scope[orig] = new
        self.local_counters[-1] += 1
        return new

    def _lookup_local(self, orig: str) -> Optional[str]:
        if not self.scope_stack:
            return None
        return self.scope_stack[-1].get(orig)

    def visit_Module(self, node: ast.Module) -> Any:
        node.body = [self.visit(n) for n in node.body]
        return node

    def _enter_function(self, orig_name: str) -> str:
        canon_name = f"f{self.func_counter}"
        self.func_counter += 1
        self._current_func_orig.append(orig_name)
        self._current_func_canon.append(canon_name)
        self._push_scope()
        return canon_name

    def _exit_function(self) -> None:
        scope = self._pop_scope()  # orig->canon
        orig_name = self._current_func_orig.pop()
        canon_name = self._current_func_canon.pop()

        # Invert to canon->orig for rehydration
        canon_to_orig = {v: k for k, v in scope.items()}
        self.name_map["functions"].append(
            {"canon_func": canon_name, "orig_func": orig_name, "canon_to_orig": canon_to_orig}
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        orig_name = node.name
        node.name = self._enter_function(orig_name)

        node.args = self.visit(node.args)
        node.body = [self.visit(n) for n in node.body]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.returns = self.visit(node.returns) if node.returns else None

        self._exit_function()
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        orig_name = node.name
        node.name = self._enter_function(orig_name)

        node.args = self.visit(node.args)
        node.body = [self.visit(n) for n in node.body]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.returns = self.visit(node.returns) if node.returns else None

        self._exit_function()
        return node

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        # Lambdas get their own local scope mapping, but no function-name mapping
        self._push_scope()
        node.args = self.visit(node.args)
        node.body = self.visit(node.body)
        self._pop_scope()
        return node

    def visit_arguments(self, node: ast.arguments) -> Any:
        # Args are locals
        for a in node.posonlyargs:
            a.arg = self._alloc_local(a.arg)
        for a in node.args:
            a.arg = self._alloc_local(a.arg)
        if node.vararg:
            node.vararg.arg = self._alloc_local(node.vararg.arg)
        for a in node.kwonlyargs:
            a.arg = self._alloc_local(a.arg)
        if node.kwarg:
            node.kwarg.arg = self._alloc_local(node.kwarg.arg)

        node.defaults = [self.visit(d) for d in node.defaults]
        node.kw_defaults = [self.visit(d) if d else None for d in node.kw_defaults]
        return node

    def visit_Name(self, node: ast.Name) -> Any:
        if not self.scope_stack:
            return node

        if isinstance(node.ctx, (ast.Store, ast.Del, ast.Param)):
            node.id = self._alloc_local(node.id)
        elif isinstance(node.ctx, ast.Load):
            mapped = self._lookup_local(node.id)
            if mapped is not None:
                node.id = mapped
        return node

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        node.target = self.visit(node.target)
        node.iter = self.visit(node.iter)
        node.ifs = [self.visit(i) for i in node.ifs]
        return node


class Rehydrator(ast.NodeTransformer):
    """
    Rehydrate canonical names back to original names using the stored name_map.

    This runs on a decoded AST that is in semantic canonical form.
    """
    def __init__(self, name_map: Dict[str, Any]) -> None:
        super().__init__()
        self.func_maps: List[Dict[str, Any]] = name_map.get("functions", [])
        self.func_i = 0
        self.scope_stack: List[Dict[str, str]] = []  # canon->orig

    def _push(self, canon_to_orig: Dict[str, str]) -> None:
        self.scope_stack.append(canon_to_orig)

    def _pop(self) -> None:
        self.scope_stack.pop()

    def _lookup(self, canon: str) -> Optional[str]:
        if not self.scope_stack:
            return None
        return self.scope_stack[-1].get(canon)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if self.func_i >= len(self.func_maps):
            return node  # nothing to apply
        fm = self.func_maps[self.func_i]
        self.func_i += 1

        node.name = fm.get("orig_func", node.name)
        canon_to_orig = fm.get("canon_to_orig", {})
        self._push(canon_to_orig)

        node.args = self.visit(node.args)
        node.body = [self.visit(n) for n in node.body]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.returns = self.visit(node.returns) if node.returns else None

        self._pop()
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if self.func_i >= len(self.func_maps):
            return node
        fm = self.func_maps[self.func_i]
        self.func_i += 1

        node.name = fm.get("orig_func", node.name)
        canon_to_orig = fm.get("canon_to_orig", {})
        self._push(canon_to_orig)

        node.args = self.visit(node.args)
        node.body = [self.visit(n) for n in node.body]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.returns = self.visit(node.returns) if node.returns else None

        self._pop()
        return node

    def visit_arg(self, node: ast.arg) -> Any:
        mapped = self._lookup(node.arg)
        if mapped is not None:
            node.arg = mapped
        return node

    def visit_Name(self, node: ast.Name) -> Any:
        mapped = self._lookup(node.id)
        if mapped is not None:
            node.id = mapped
        return node


class SIRBuilder:
    """
    Build a content-addressed node graph:
      nodes: {node_id: {"t": <type>, "f": {field: value/ref}}}
      root: node_id

    Canonical properties:
    - node_id is sha256 over node content (type + normalized fields).
    - dict keys are sorted; stable JSON encoding.
    """
    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, JSON]] = {}

    def _freeze(self, obj: JSON) -> JSON:
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
            return self.add_node("Lit", {"v": repr(node), "k": "repr"})

        t = type(node).__name__
        fields: Dict[str, JSON] = {}

        for fname, value in ast.iter_fields(node):
            if fname in ("lineno", "col_offset", "end_lineno", "end_col_offset", "type_comment"):
                continue

            if isinstance(value, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context)):
                # Store as plain string (not a ref)
                fields[fname] = type(value).__name__
                continue

            if isinstance(value, (ast.AST, list)) or value is None or isinstance(value, (bool, int, float, str, bytes)):
                fields[fname] = self._visit(value)
            else:
                fields[fname] = self._visit(repr(value))

        return self.add_node(t, fields)


def load_text(path: str) -> str:
    if path == "-" or path == "/dev/stdin":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def dump_text(text: str, out_path: Optional[str]) -> None:
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    else:
        print(text)


def dump_json(obj: Any, out_path: Optional[str]) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    dump_text(text, out_path)


def encode_to_sir(source: str, cfg: CanonConfig) -> Dict[str, Any]:
    tree = ast.parse(source)

    name_map: Optional[Dict[str, Any]] = None
    if cfg.mode == "semantic":
        ren = AlphaRenamer()
        tree = ren.visit(tree)
        ast.fix_missing_locations(tree)
        name_map = ren.name_map

    builder = SIRBuilder()
    root = builder.build(tree)

    sir: Dict[str, Any] = {
        "format": "SIR-1",
        "version": "0.2",
        "mode": cfg.mode,
        "root": root,
        "nodes": builder.nodes,
        "source_sha256": b16_sha256(source.encode("utf-8")),
    }
    if name_map is not None:
        sir["name_map"] = name_map

    sir["sir_sha256"] = sir_hash(sir)
    return sir


def sir_hash(sir: Dict[str, Any]) -> str:
    data = json.dumps(
        {"root": sir["root"], "nodes": sir["nodes"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return b16_sha256(data)


def _is_ref(s: Any, nodes: Dict[str, Any]) -> bool:
    return isinstance(s, str) and HEX64_RE.match(s) is not None and s in nodes


def sir_to_ast(sir: Dict[str, Any]) -> ast.AST:
    nodes: Dict[str, Any] = sir["nodes"]
    root_id: str = sir["root"]
    memo: Dict[str, Any] = {}

    def decode(nid: str) -> Any:
        if nid in memo:
            return memo[nid]
        nd = nodes[nid]
        t = nd["t"]
        f = nd["f"]

        if t == "None":
            memo[nid] = None
            return None

        if t == "Lit":
            k = f.get("k")
            v = f.get("v")
            if k == "bytes_hex":
                out = bytes.fromhex(str(v))
            else:
                out = v
            memo[nid] = out
            return out

        if t == "List":
            items = [decode(x) for x in f.get("items", [])]
            memo[nid] = items
            return items

        # AST node
        cls = getattr(ast, t, None)
        if cls is None:
            raise ValueError(f"Unknown AST class in SIR: {t}")

        kwargs: Dict[str, Any] = {}
        for k, v in f.items():
            if _is_ref(v, nodes):
                kwargs[k] = decode(v)
            elif isinstance(v, list):
                kwargs[k] = [decode(x) if _is_ref(x, nodes) else x for x in v]
            elif isinstance(v, str) and not _is_ref(v, nodes):
                # operator/context stored as string class name
                op_cls = getattr(ast, v, None)
                if op_cls is not None and isinstance(op_cls, type) and issubclass(op_cls, ast.AST):
                    try:
                        kwargs[k] = op_cls()
                    except TypeError:
                        kwargs[k] = v
                else:
                    kwargs[k] = v
            else:
                kwargs[k] = v

        obj = cls(**kwargs)  # type: ignore[arg-type]
        memo[nid] = obj
        return obj

    root = decode(root_id)
    if not isinstance(root, ast.AST):
        raise ValueError("Decoded root is not an AST node")
    ast.fix_missing_locations(root)
    return root


def decode_sir_to_source(sir: Dict[str, Any], rehydrate: bool) -> str:
    tree = sir_to_ast(sir)

    if rehydrate:
        nm = sir.get("name_map")
        if not isinstance(nm, dict):
            raise ValueError("No name_map present in SIR; cannot --rehydrate.")
        tree = Rehydrator(nm).visit(tree)
        ast.fix_missing_locations(tree)

    # Use ast.unparse to regenerate source
    if not hasattr(ast, "unparse"):
        raise RuntimeError("ast.unparse not available. Use Python 3.9+.")
    return ast.unparse(tree)


def main() -> int:
    ap = argparse.ArgumentParser(prog="sir1.py", description="SIR-1 prototype for Python code (representation system).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    enc = sub.add_parser("encode", help="Encode Python source into SIR JSON")
    enc.add_argument("path", help="Path to .py file, or '-' for stdin")
    enc.add_argument("-o", "--out", help="Output JSON file (default: stdout)")
    enc.add_argument("--mode", choices=["exact", "semantic"], default="exact", help="Canonicalization mode")

    h = sub.add_parser("hash", help="Print SHA-256 hash of canonical SIR (useful for equality testing)")
    h.add_argument("path", help="Path to .py file, or '-' for stdin")
    h.add_argument("--mode", choices=["exact", "semantic"], default="exact", help="Canonicalization mode")

    dec = sub.add_parser("decode", help="Decode SIR JSON back to Python source (format normalized)")
    dec.add_argument("sir_json", help="Path to SIR JSON (produced by encode)")
    dec.add_argument("-o", "--out", help="Output .py path (default: stdout)")
    dec.add_argument("--rehydrate", action="store_true", help="Restore original names using embedded name_map (semantic mode)")

    args = ap.parse_args()

    try:
        if args.cmd in ("encode", "hash"):
            src = load_text(args.path)
            cfg = CanonConfig(mode=args.mode)
            sir = encode_to_sir(src, cfg)

            if args.cmd == "encode":
                dump_json(sir, args.out)
                return 0
            else:
                print(sir_hash(sir))
                return 0

        if args.cmd == "decode":
            raw = load_text(args.sir_json)
            sir = json.loads(raw)
            if not isinstance(sir, dict) or "nodes" not in sir or "root" not in sir:
                raise ValueError("Input does not look like a SIR JSON object.")
            out_src = decode_sir_to_source(sir, bool(args.rehydrate))
            dump_text(out_src, args.out)
            return 0

        ap.error("Unknown command")
        return 2

    except SyntaxError as e:
        print(f"SyntaxError: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"File not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
