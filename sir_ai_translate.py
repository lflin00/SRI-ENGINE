"""
sir_ai_translate.py — AI-powered universal language translator for SIR Engine.

Supports two backends:
  1. Ollama (free, local) — runs on localhost:11434
  2. Anthropic API (cloud) — requires API key

Auto-detects which backend is available.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from typing import List, Tuple, Optional

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

TRANSLATE_PROMPT = """You are a code translation engine. Convert this {language} function to equivalent Python 3.

RULES:
1. Preserve exact logical structure — do NOT simplify or optimize
2. Translate control flow literally
3. Use only built-in Python — no external libraries
4. Keep variable names identical to the original
5. Output ONLY valid Python — no explanation, no markdown, no backticks

Input:
{code}

Python translation:"""


def detect_language(filename: str) -> Optional[str]:
    import os
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_TO_LANG.get(ext)


def is_ai_language(filename: str) -> bool:
    import os
    ext = os.path.splitext(filename)[1].lower()
    return ext in AI_SUPPORTED_EXTENSIONS


def check_ollama(host: str = "http://localhost:11434") -> bool:
    """Check if Ollama is running."""
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def get_ollama_models(host: str = "http://localhost:11434") -> List[str]:
    """Get list of installed Ollama models."""
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def call_ollama(prompt: str, model: str = "codellama:7b",
                host: str = "http://localhost:11434") -> str:
    """Call Ollama API."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1000}
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    return data.get("response", "").strip()


def call_anthropic(prompt: str, api_key: str) -> str:
    """Call Anthropic API."""
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


def translate_to_python(
    code: str,
    language: str,
    backend: str = "ollama",
    api_key: str = "",
    ollama_model: str = "codellama:7b",
    ollama_host: str = "http://localhost:11434"
) -> str:
    """Translate code to Python using selected backend."""
    prompt = TRANSLATE_PROMPT.format(language=language, code=code)
    result = ""
    if backend == "ollama":
        result = call_ollama(prompt, model=ollama_model, host=ollama_host)
    elif backend == "anthropic":
        result = call_anthropic(prompt, api_key=api_key)
    # Strip accidental markdown
    result = re.sub(r'```\w*\n?', '', result).strip()
    return result


FUNCTION_PATTERNS = {
    'java':   r'(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{',
    'c':      r'(?:[\w\*]+\s+)+(\w+)\s*\([^)]*\)\s*\{',
    'cpp':    r'(?:[\w\*:<>]+\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\{',
    'rust':   r'fn\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]&\s]+)?\s*\{',
    'go':     r'func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\([^)]*\)\s*(?:[\w\(\)\*,\s]*)?\{',
    'ruby':   r'^\s*def\s+(\w+[?!]?)\s*(?:\([^)]*\))?',
    'kotlin': r'fun\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*\w+)?\s*\{',
    'swift':  r'func\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]?!]+)?\s*\{',
    'csharp': r'(?:public|private|protected|static|virtual|override|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*\{',
    'scala':  r'def\s+(\w+)\s*(?:\[[^\]]*\])?\s*\([^)]*\)\s*(?::\s*\w+)?\s*=',
    'lua':    r'(?:local\s+)?function\s+(\w+)\s*\([^)]*\)',
    'php':    r'function\s+(\w+)\s*\([^)]*\)',
    'dart':   r'(?:[\w<>\[\]?]+\s+)+(\w+)\s*\([^)]*\)\s*(?:async\s*)?\{',
    'haskell':r'^(\w+)\s+::',
}


def extract_raw_functions(src: str, language: str) -> List[Tuple[str, int, str]]:
    lang_lower = language.lower().replace('+', 'p').replace('#', 'sharp').replace('/', '')
    lang_key = None
    for key in FUNCTION_PATTERNS:
        if key in lang_lower or lang_lower.startswith(key):
            lang_key = key
            break

    results = []
    lines = src.splitlines()

    if lang_key in ('ruby',):
        i = 0
        while i < len(lines):
            m = re.match(FUNCTION_PATTERNS['ruby'], lines[i])
            if m:
                name = m.group(1)
                block = [lines[i]]
                i += 1
                depth = 1
                while i < len(lines) and depth > 0:
                    l = lines[i]
                    if re.match(r'\s*def\s+', l): depth += 1
                    if re.match(r'\s*end\s*$', l): depth -= 1
                    block.append(l)
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
        # Fallback
        for match in re.finditer(
            r'(?:func|fn|def|function|method)\s+(\w+)\s*\([^)]*\)',
            src, re.MULTILINE
        ):
            name = match.group(1)
            lineno = src[:match.start()].count('\n') + 1
            line_start = src[:match.start()].count('\n')
            block = lines[line_start:line_start + 20]
            results.append((name, lineno, '\n'.join(block)))

    return results
