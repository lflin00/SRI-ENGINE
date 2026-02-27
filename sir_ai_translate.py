"""
sir_ai_translate.py — AI-powered universal language translator for SIR Engine.

Supports two backends:
  1. Ollama (free, local) — runs on localhost:11434
  2. Anthropic API (cloud) — requires API key

Tools built into this version:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ VALIDATION LAYER                                                    │
  │ Every translation is parsed with Python's ast module before use.   │
  │ Invalid Python is caught immediately and retried automatically.     │
  │ Silent bad translations no longer produce wrong hashes.            │
  ├─────────────────────────────────────────────────────────────────────┤
  │ TRANSLATION CACHE                                                   │
  │ Translations are cached by source hash. The same function is never │
  │ translated twice. Cache persists to disk at .sir_cache/            │
  │ Saves API cost and makes repeated scans instant.                   │
  ├─────────────────────────────────────────────────────────────────────┤
  │ CONFIDENCE SCORER                                                   │
  │ Each function is translated twice. If both hashes match → HIGH.   │
  │ If they differ → LOW (function uses ambiguous idioms).             │
  │ Cache hits and single-pass → MEDIUM.                               │
  │ Results tagged so users know exactly which ones to trust.          │
  └─────────────────────────────────────────────────────────────────────┘

Confidence levels:
  🟢 HIGH   — translated twice, both hashes match. Structurally stable.
  🟡 MEDIUM — single translation, valid Python. Reliable but unverified.
  🔴 LOW    — translated twice, hashes differ. Review manually.
  ❌ FAILED — invalid Python output. Function could not be analyzed.
"""

from __future__ import annotations

import ast as _ast
import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
#  Language detection
# ─────────────────────────────────────────────

EXTENSION_TO_LANG = {
    '.c': 'C', '.cpp': 'C++', '.cc': 'C++', '.cxx': 'C++',
    '.h': 'C/C++ header', '.hpp': 'C++ header',
    '.java': 'Java', '.rs': 'Rust', '.go': 'Go',
    '.rb': 'Ruby', '.php': 'PHP', '.swift': 'Swift',
    '.kt': 'Kotlin', '.scala': 'Scala', '.cs': 'C#',
    '.r': 'R', '.lua': 'Lua', '.pl': 'Perl',
    '.dart': 'Dart', '.hs': 'Haskell',
    '.ex': 'Elixir', '.exs': 'Elixir',
    '.ml': 'OCaml', '.fs': 'F#', '.fsx': 'F#',
    '.jl': 'Julia', '.nim': 'Nim', '.zig': 'Zig',
}

AI_SUPPORTED_EXTENSIONS = set(EXTENSION_TO_LANG.keys())

# Language-specific prompt hints for known edge cases
LANG_HINTS = {
    'C++':     "Translate templates as generic functions. Treat pointers as regular variables. Ignore memory management.",
    'Rust':    "Ignore lifetimes, ownership, borrowing. Treat &str as str, Vec<T> as list. Translate ownership transfers as assignments.",
    'Java':    "Ignore access modifiers. Translate generics as plain variables. this.x becomes self_x (no self parameter).",
    'C#':      "Ignore access modifiers. Properties become variables. Ignore async/await for structural purposes.",
    'Haskell': "Translate pattern matching to if/elif chains. Ignore type signatures. Translate recursion literally.",
    'Scala':   "Translate case classes as dicts. Option[T] becomes a regular value. Pattern matching becomes if/elif.",
    'Kotlin':  "Ignore nullable types (?). val/var both become regular variables. when becomes if/elif.",
    'Swift':   "Ignore optionals (?/!). guard becomes if. Ignore access modifiers.",
}

# ─────────────────────────────────────────────
#  Prompts
# ─────────────────────────────────────────────

TRANSLATE_PROMPT = """You are a code translation engine. Convert this {language} function to equivalent Python 3.

RULES:
1. Preserve exact logical structure — do NOT simplify, optimize, or restructure
2. Translate control flow literally (for loops stay for loops, while stays while)
3. Use only built-in Python — no external libraries
4. Keep variable names identical to the original
5. Output ONLY a valid Python function definition — no explanation, no markdown, no backticks, no comments{hints}

Input:
{code}

Python translation:"""


# ─────────────────────────────────────────────
#  Translation cache
# ─────────────────────────────────────────────

CACHE_DIR = Path(".sir_cache")
CACHE_FILE = CACHE_DIR / "translations.json"
_memory_cache: Dict[str, dict] = {}
_cache_loaded = False


def _load_cache() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text())
            _memory_cache.update(data)
    except Exception:
        pass


def _save_cache() -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(_memory_cache, indent=2))
    except Exception:
        pass


def _cache_key(code: str, language: str) -> str:
    raw = f"{language}::{code}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cache_get(code: str, language: str) -> Optional[dict]:
    """Return cached result or None."""
    _load_cache()
    return _memory_cache.get(_cache_key(code, language))


def cache_set(code: str, language: str, entry: dict) -> None:
    """Store result in cache."""
    _load_cache()
    _memory_cache[_cache_key(code, language)] = {k: v for k, v in entry.items() if k != 'cache_hit'}
    _save_cache()


def cache_stats() -> dict:
    """Return cache statistics."""
    _load_cache()
    total = len(_memory_cache)
    by_conf = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "FAILED": 0}
    for v in _memory_cache.values():
        c = v.get("confidence", "MEDIUM")
        by_conf[c] = by_conf.get(c, 0) + 1
    return {"total": total, **by_conf}


def cache_clear() -> None:
    """Clear all cached translations."""
    global _memory_cache, _cache_loaded
    _memory_cache = {}
    _cache_loaded = True
    try:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Validation layer
# ─────────────────────────────────────────────

def validate_python(code: str) -> Tuple[bool, str]:
    """
    Validate that code is parseable Python containing at least one function.
    Returns (is_valid, error_message).
    """
    if not code or not code.strip():
        return False, "Empty translation"
    try:
        tree = _ast.parse(code)
        has_func = any(
            isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            for n in _ast.walk(tree)
        )
        if not has_func:
            return False, "No function definition found in translation"
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, str(e)


def clean_translation(raw: str) -> str:
    """Strip markdown artifacts and find the first function definition."""
    raw = re.sub(r'```\w*\n?', '', raw).strip()
    lines = raw.splitlines()
    # Find where the first def starts
    for i, line in enumerate(lines):
        if line.strip().startswith('def ') or line.strip().startswith('async def '):
            return '\n'.join(lines[i:]).strip()
    return raw.strip()


# ─────────────────────────────────────────────
#  Backend helpers
# ─────────────────────────────────────────────

def detect_language(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_TO_LANG.get(ext)


def is_ai_language(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in AI_SUPPORTED_EXTENSIONS


def check_ollama(host: str = "http://localhost:11434") -> bool:
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def get_ollama_models(host: str = "http://localhost:11434") -> List[str]:
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def call_ollama(prompt: str, model: str = "codellama:7b",
                host: str = "http://localhost:11434") -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1200}
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=90)
    data = json.loads(resp.read())
    return data.get("response", "").strip()


def call_anthropic(prompt: str, api_key: str) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()


def _raw_translate(code: str, language: str, backend: str,
                   api_key: str, ollama_model: str, ollama_host: str) -> str:
    """One translation call — returns cleaned raw string."""
    hint = LANG_HINTS.get(language, "")
    hints_line = f"\n6. LANGUAGE NOTES: {hint}" if hint else ""
    prompt = TRANSLATE_PROMPT.format(language=language, code=code, hints=hints_line)

    if backend == "ollama":
        raw = call_ollama(prompt, model=ollama_model, host=ollama_host)
    elif backend == "anthropic":
        raw = call_anthropic(prompt, api_key=api_key)
    else:
        return ""

    return clean_translation(raw)


# ─────────────────────────────────────────────
#  Main translation entry point
# ─────────────────────────────────────────────

def translate_to_python(
    code: str,
    language: str,
    backend: str = "ollama",
    api_key: str = "",
    ollama_model: str = "codellama:7b",
    ollama_host: str = "http://localhost:11434",
    confidence_check: bool = True,
    use_cache: bool = True,
    max_retries: int = 2,
) -> dict:
    """
    Translate source code to Python with validation, caching, and confidence scoring.

    Returns:
    {
        "python_src":  str   — translated Python
        "confidence":  str   — HIGH / MEDIUM / LOW / FAILED
        "cache_hit":   bool  — True if from cache
        "error":       str   — error message if FAILED
        "hash1":       str   — SIR hash of first translation
        "hash2":       str   — SIR hash of second translation (if confidence_check)
    }
    """
    kw = dict(backend=backend, api_key=api_key,
              ollama_model=ollama_model, ollama_host=ollama_host)

    def _sir_hash(py: str) -> str:
        try:
            from sir.core import hash_source
            return hash_source(py, mode="semantic")
        except Exception:
            return ""

    def _fail(msg: str) -> dict:
        r = {"python_src": "", "confidence": "FAILED",
             "cache_hit": False, "error": msg, "hash1": "", "hash2": ""}
        if use_cache:
            cache_set(code, language, r)
        return r

    # ── Cache check ────────────────────────────────────────────────────
    if use_cache:
        cached = cache_get(code, language)
        if cached:
            return {**cached, "cache_hit": True}

    # ── First translation with retries ─────────────────────────────────
    py1 = ""
    last_err = ""
    for attempt in range(max_retries):
        try:
            raw = _raw_translate(code, language, **kw)
            valid, err = validate_python(raw)
            if valid:
                py1 = raw
                break
            else:
                last_err = err
                if attempt < max_retries - 1:
                    time.sleep(0.5)
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries - 1:
                time.sleep(0.5)

    if not py1:
        return _fail(f"Invalid Python after {max_retries} attempts: {last_err}")

    hash1 = _sir_hash(py1)

    # ── Confidence check — second translation ──────────────────────────
    if confidence_check and hash1:
        try:
            raw2 = _raw_translate(code, language, **kw)
            valid2, _ = validate_python(raw2)
            if valid2:
                hash2 = _sir_hash(raw2)
                if hash1 and hash2 and hash1 == hash2:
                    confidence = "HIGH"
                    error = ""
                elif hash2:
                    confidence = "LOW"
                    error = "Hash mismatch between two translations — function may use ambiguous idioms. Review manually."
                else:
                    confidence = "MEDIUM"
                    error = ""
            else:
                hash2 = ""
                confidence = "MEDIUM"
                error = ""
        except Exception:
            hash2 = ""
            confidence = "MEDIUM"
            error = ""
    else:
        hash2 = ""
        confidence = "MEDIUM"
        error = ""

    result = {
        "python_src": py1,
        "confidence": confidence,
        "cache_hit": False,
        "error": error,
        "hash1": hash1,
        "hash2": hash2,
    }

    # Only cache HIGH and MEDIUM — not LOW (non-deterministic)
    if use_cache and confidence in ("HIGH", "MEDIUM"):
        cache_set(code, language, result)

    return result


# ─────────────────────────────────────────────
#  Function extraction
# ─────────────────────────────────────────────

FUNCTION_PATTERNS = {
    'java':    r'(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{',
    'c':       r'(?:[\w\*]+\s+)+(\w+)\s*\([^)]*\)\s*\{',
    'cpp':     r'(?:[\w\*:<>]+\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\{',
    'rust':    r'fn\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]&\s]+)?\s*\{',
    'go':      r'func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\([^)]*\)\s*(?:[\w\(\)\*,\s]*)?\{',
    'ruby':    r'^\s*def\s+(\w+[?!]?)\s*(?:\([^)]*\))?',
    'kotlin':  r'fun\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*\w+)?\s*\{',
    'swift':   r'func\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]?!]+)?\s*\{',
    'csharp':  r'(?:public|private|protected|static|virtual|override|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*\{',
    'scala':   r'def\s+(\w+)\s*(?:\[[^\]]*\])?\s*\([^)]*\)\s*(?::\s*\w+)?\s*=',
    'lua':     r'(?:local\s+)?function\s+(\w+)\s*\([^)]*\)',
    'php':     r'function\s+(\w+)\s*\([^)]*\)',
    'dart':    r'(?:[\w<>\[\]?]+\s+)+(\w+)\s*\([^)]*\)\s*(?:async\s*)?\{',
    'haskell': r'^(\w+)\s+::',
}


def extract_raw_functions(src: str, language: str) -> List[Tuple[str, int, str]]:
    """Extract raw function blocks. Returns [(name, lineno, raw_src)]."""
    lang_lower = language.lower().replace('+', 'p').replace('#', 'sharp').replace('/', '')
    lang_key = None
    for key in FUNCTION_PATTERNS:
        if key in lang_lower or lang_lower.startswith(key):
            lang_key = key
            break

    results = []
    lines = src.splitlines()

    if lang_key == 'ruby':
        i = 0
        while i < len(lines):
            m = re.match(FUNCTION_PATTERNS['ruby'], lines[i])
            if m:
                name = m.group(1)
                block = [lines[i]]
                i += 1
                depth = 1
                while i < len(lines) and depth > 0:
                    ln = lines[i]
                    if re.match(r'\s*def\s+', ln): depth += 1
                    if re.match(r'\s*end\s*$', ln): depth -= 1
                    block.append(ln)
                    i += 1
                results.append((name, len(results) + 1, '\n'.join(block)))
            else:
                i += 1
    elif lang_key:
        pattern = FUNCTION_PATTERNS[lang_key]
        for match in re.finditer(pattern, src, re.MULTILINE):
            name = match.group(1)
            brace_pos = src.find('{', match.start())
            if brace_pos == -1:
                continue
            depth = 0
            end = brace_pos
            for idx in range(brace_pos, len(src)):
                if src[idx] == '{': depth += 1
                elif src[idx] == '}':
                    depth -= 1
                    if depth == 0:
                        end = idx + 1
                        break
            raw = src[match.start():end]
            lineno = src[:match.start()].count('\n') + 1
            results.append((name, lineno, raw))
    else:
        for match in re.finditer(
            r'(?:func|fn|def|function|method)\s+(\w+)\s*\([^)]*\)',
            src, re.MULTILINE
        ):
            name = match.group(1)
            lineno = src[:match.start()].count('\n') + 1
            line_start = src[:match.start()].count('\n')
            block = lines[line_start:line_start + 25]
            results.append((name, lineno, '\n'.join(block)))

    return results


# ─────────────────────────────────────────────
#  Confidence display helpers
# ─────────────────────────────────────────────

CONFIDENCE_ICON = {
    "HIGH":   "🟢",
    "MEDIUM": "🟡",
    "LOW":    "🔴",
    "FAILED": "❌",
}

CONFIDENCE_DESCRIPTION = {
    "HIGH":   "Translated twice — both hashes match. Structurally stable.",
    "MEDIUM": "Translated once successfully. Reliable but not double-checked.",
    "LOW":    "Translated twice — hashes differ. Function uses ambiguous idioms. Review manually.",
    "FAILED": "Translation produced invalid Python. Function could not be analyzed.",
}
