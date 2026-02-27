# SIR Engine
### Semantic Intermediate Representation — Cross-Language Structural Duplicate Detection

SIR Engine finds functions that do the same thing even if they look completely different — across any programming language.

**[Try it live →](https://sri-engine-7amwtce7a23k7q34cpnxem.streamlit.app)**

---

## How it works

Every function gets stripped down to its pure logical structure. Variable names, formatting, and comments are removed. What's left gets hashed with SHA-256.

Two functions with the same hash have the same logic — regardless of what they're called, how they're formatted, or what language they're written in.

```python
def calculate_total(price, tax):    # hash: 4b67fc60...
    result = price + tax
    return result

def add_values(a, b):               # hash: 4b67fc60...  ← same hash, same logic
    result = a + b
    return result
```

This is called **alpha equivalence** — a concept from lambda calculus (1936). SIR Engine makes it practical.

---

## The novel part — LLM as universal IR frontend

Instead of writing a hand-crafted parser for every language, SIR Engine uses an LLM to translate any language to Python first, then runs it through the same pipeline.

This means a Java function and a Python function that implement the same logic will produce the same structural hash. Only one parser was needed.

```java
// Java
int calculateTotal(int price, int tax) {
    int result = price + tax;
    return result;
}
```
```python
# Python  
def add_values(a, b):
    result = a + b
    return result
```
**Same hash. Detected as structurally identical.**

---

## Features

- **Scan** — find duplicate functions across any codebase with health score (0-100)
- **GitHub Scanner** — paste any public repo URL, scan without downloading
- **Merge** — automatically remove duplicates, rename call sites, consolidate into utils
- **Pack / Unpack** — compress codebases into deduplicated bundles
- **Diff** — compare two codebases structurally
- **Verify** — confirm restored files match original hashes
- **VS Code Extension** — live scanning, health score in status bar, auto-merge with undo
- **.sir_ignore** — mark intentional duplicates to skip

**Native support:** Python, JavaScript, TypeScript

**Via AI translation:** C, C++, Java, Rust, Go, Ruby, Swift, Kotlin, Scala, C#, PHP, Dart, Lua, Haskell, and more

---

## Run locally (free AI via Ollama)

```bash
git clone https://github.com/lflin00/SRI-ENGINE
cd SRI-ENGINE/SIR_MAIN
pip install streamlit
ollama pull codellama:7b
streamlit run sir_ui.py
```

Open http://localhost:8501 — AI translation runs locally for free. Your code never leaves your machine.

---

## VS Code Extension

Download [sir-engine-0.0.2.vsix](https://github.com/lflin00/SRI-ENGINE/raw/main/sir-engine-0.0.2.vsix)

Install: `Cmd+Shift+P` → `Install from VSIX` → select the file

- Scans on every save
- Health score in status bar
- Auto-merge with before/after diff preview
- Fully reversible with Cmd+Z

---

## Health Score

```
Health = (unique structures / total functions) × 100
```

100 = no duplicate logic anywhere. Lower = more redundant code.

---

## License

Business Source License 1.1 — free for individuals, students, researchers, and internal business use. Commercial hosting requires a license.

Converts to MIT on February 27, 2030.

---

*Built by Lucas Flinders · Biomedical Engineering student · 2026*
