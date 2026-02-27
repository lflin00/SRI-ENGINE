"""
sir_ai_translate.py — AI-powered universal language translator for SIR Engine.

Uses Claude to convert any programming language into equivalent Python,
then passes the result through the existing Python SIR pipeline.

Supported input languages (anything Claude can translate):
  C, C++, Java, Rust, Go, Ruby, PHP, Swift, Kotlin, Scala,
  C#, R, MATLAB, Haskell, Lua, Perl, Dart, and more.

Limitations (noted for transparency):
  - Translation is AI-generated, not a formal parser. Results are
    highly reliable but not mathematically guaranteed like the Python/JS pipelines.
  - Very complex language-specific features (C++ templates, Rust lifetimes,
    Java generics) may be simplified during translation.
  - Two calls for the same function may occasionally produce slightly
    different Python if the function uses ambiguous idioms.
  - Best for pure logic functions. Functions with heavy I/O or
    platform-specific system calls may not translate cleanly.

Usage:
    from sir_ai_translate import translate_to_python, hash_any_language, extract_any_functions
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import List, Tuple, Optional, Dict


# ─────────────────────────────────────────────
#  Language detection
# ─────────────────────────────────────────────

EXTENSION_TO_LANG = {
    '.c':    'C',
    '.cpp':  'C++',
    '.cc':   'C++',
    '.cxx':  'C++',
    '.h':    'C/C++ header',
    '.hpp':  'C++ header',
    '.java': 'Java',
    '.rs':   'Rust',
    '.go':   'Go',
    '.rb':   'Ruby',
    '.php':  'PHP',
    '.swift':'Swift',
    '.kt':   'Kotlin',
    '.scala':'Scala',
    '.cs':   'C#',
    '.r':    'R',
    '.lua':  'Lua',
    '.pl':   'Perl',
    '.dart': 'Dart',
    '.m':    'MATLAB',
    '.hs':   'Haskell',
    '.ex':   'Elixir',
    '.exs':  'Elixir',
    '.ml':   'OCaml',
    '.fs':   'F#',
    '.fsx':  'F#',
    '.jl':   'Julia',
    '.nim':  'Nim',
    '.zig':  'Zig',
    '.v':    'V',
    '.cr':   'Crystal',
}

AI_SUPPORTED_EXTENSIONS = set(EXTENSION_TO_LANG.keys())


def detect_language(filename: str) -> Optional[str]:
    """Detect language from file extension."""
    import os
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_TO_LANG.get(ext)


def is_ai_language(filename: str) -> bool:
    """Check if this file needs AI translation."""
    import os
    ext = os.path.splitext(filename)[1].lower()
    return ext in AI_SUPPORTED_EXTENSIONS


# ─────────────────────────────────────────────
#  Claude API call
# ─────────────────────────────────────────────

TRANSLATE_PROMPT = """You are a code translation engine. Your job is to convert functions from any programming language into equivalent Python 3.

RULES:
1. Preserve the exact logical structure — do NOT simplify, optimize, or restructure
2. Translate control flow literally (loops stay loops, conditionals stay conditionals)
3. Use generic Python — no external libraries
4. Keep variable names identical to the original
5. Output ONLY valid Python function definitions — no explanations, no markdown, no backticks
6. One function per output block
7. If a construct has no Python equivalent, use the closest structural approximation

Input language: {language}

Input code:
{code}

Output only the Python translation, nothing else:"""


def call_claude(prompt: str) -> str:
    """Call Claude API and return text response."""
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )

    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read().decode("utf-8"))
    return "".join(block.get("text", "") for block in data.get("content", []))


# ─────────────────────────────────────────────
#  Function extraction (language-aware)
# ─────────────────────────────────────────────

# Regex patterns for extracting functions from various languages
FUNCTION_PATTERNS = {
    'java':    r'(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+\w+)?\s*\{',
    'c':       r'(?:[\w\*]+\s+)+(\w+)\s*\([^)]*\)\s*\{',
    'cpp':     r'(?:[\w\*:<>]+\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\{',
    'rust':    r'fn\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]&]+)?\s*\{',
    'go':      r'func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\([^)]*\)\s*(?:[\w\(\)\*,\s]*)?\{',
    'ruby':    r'def\s+(\w+(?:[?!])?)\s*(?:\([^)]*\))?\s*$',
    'kotlin':  r'fun\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*\w+)?\s*\{',
    'swift':   r'func\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[\w<>\[\]?!]+)?\s*\{',
    'csharp':  r'(?:public|private|protected|static|virtual|override|\s)+\s+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*\{',
    'scala':   r'def\s+(\w+)\s*(?:\[[^\]]*\])?\s*\([^)]*\)\s*(?::\s*\w+)?\s*=',
    'haskell': r'^(\w+)\s+::',
    'lua':     r'(?:local\s+)?function\s+(\w+)\s*\([^)]*\)',
    'php':     r'function\s+(\w+)\s*\([^)]*\)',
    'dart':    r'(?:[\w<>\[\]?]+\s+)+(\w+)\s*\([^)]*\)\s*(?:async\s*)?\{',
}


def extract_raw_functions(src: str, language: str) -> List[Tuple[str, int, str]]:
    """
    Extract raw function source blocks from any language.
    Returns list of (name, lineno, raw_source).
    Uses brace counting for C-style languages, line-based for others.
    """
    lang_lower = language.lower().replace('+', 'p').replace('#', 'sharp').replace('/', '')
    # Normalise language name to pattern key
    lang_key = None
    for key in FUNCTION_PATTERNS:
        if key in lang_lower or lang_lower in key:
            lang_key = key
            break

    results = []
    lines = src.splitlines()

    if lang_key in ('ruby', 'haskell'):
        # Line-based extraction
        pattern = FUNCTION_PATTERNS[lang_key]
        i = 0
        while i < len(lines):
            m = re.match(pattern, lines[i], re.MULTILINE)
            if m:
                name = m.group(1)
                start = i
                # Collect until 'end' keyword or blank line
                block = [lines[i]]
                i += 1
                depth = 1
                while i < len(lines) and depth > 0:
                    line = lines[i]
                    if re.match(r'\s*def\s+', line): depth += 1
                    if re.match(r'\s*end\s*$', line): depth -= 1
                    block.append(line)
                    i += 1
                results.append((name, start + 1, '\n'.join(block)))
            else:
                i += 1
    elif lang_key:
        # Brace-counting extraction
        pattern = FUNCTION_PATTERNS[lang_key]
        matches = list(re.finditer(pattern, src, re.MULTILINE))
        for match in matches:
            name = match.group(1)
            # Find opening brace
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
        # Fallback: try to find any function-like blocks using common patterns
        fallback = re.finditer(
            r'(?:func|fn|def|function|method|sub)\s+(\w+)\s*\([^)]*\)',
            src, re.MULTILINE
        )
        for match in fallback:
            name = match.group(1)
            lineno = src[:match.start()].count('\n') + 1
            # Grab next 20 lines as approximation
            line_start = src[:match.start()].count('\n')
            block_lines = lines[line_start:line_start + 20]
            results.append((name, lineno, '\n'.join(block_lines)))

    return results


# ─────────────────────────────────────────────
#  Main translation functions
# ─────────────────────────────────────────────

def translate_to_python(code: str, language: str) -> str:
    """
    Use Claude to translate a function from any language to Python.
    Returns Python source string.
    """
    prompt = TRANSLATE_PROMPT.format(language=language, code=code)
    result = call_claude(prompt)
    # Clean up any accidental markdown
    result = re.sub(r'```\w*\n?', '', result).strip()
    return result


def extract_any_functions(
    src: str,
    filename: str,
    api_key: Optional[str] = None
) -> List[Tuple[str, int, str, str]]:
    """
    Extract functions from any supported language file and translate to Python.

    Returns list of (name, lineno, python_src, original_src).
    Requires API key for translation.
    """
    language = detect_language(filename)
    if not language:
        return []

    raw_funcs = extract_raw_functions(src, language)
    results = []

    for name, lineno, raw_src in raw_funcs:
        try:
            python_src = translate_to_python(raw_src, language)
            if python_src.strip():
                results.append((name, lineno, python_src, raw_src))
        except Exception as e:
            # Translation failed — skip this function
            continue

    return results


def hash_any_language(src: str, filename: str) -> List[Tuple[str, int, str, str]]:
    """
    Full pipeline: extract → translate → hash.
    Returns list of (name, lineno, sir_sha256, original_src).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from sir.core import hash_source

    translated = extract_any_functions(src, filename)
    results = []

    for name, lineno, python_src, original_src in translated:
        try:
            h = hash_source(python_src, mode="semantic")
            results.append((name, lineno, h, original_src))
        except Exception:
            continue

    return results
