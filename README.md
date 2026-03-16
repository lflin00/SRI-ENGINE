# SIR Engine — Semantic Code Intelligence

**Detect when two functions implement identical logic — across any programming language.**

A Java function and a Python function that do the same thing produce the same hash. SIR Engine finds those matches, shows you exactly where the duplicates are, and lets you merge them out of your codebase in one click.

🌐 **Landing page:** [sir-engine.com](https://sir-engine.com)  
📦 **Web app:** [Live demo](https://sri-engine-7amwtce7a23k7q34cpnxem.streamlit.app)  
🔌 **VS Code extension:** [Download .vsix](https://github.com/lflin00/SIR-ENGINE/raw/main/sir-engine-0.0.2.vsix)

---

## How it works

Most duplicate detection tools compare tokens — they find copy-paste duplicates but miss functions that were rewritten, renamed, or translated between languages.

SIR Engine compares **logical structure**:

1. **Translate** — any language gets translated to Python first using an LLM. One parser handles 25+ languages.
2. **Canonicalize** — variable names, function names, and formatting are stripped. Only pure logical structure remains.
3. **Hash** — the canonical structure is hashed with SHA-256. Same hash means same logic, guaranteed.
4. **Match** — every hash is compared against every other. Matching pairs are structural duplicates regardless of language.
5. **Merge** — remove duplicates in one click. Auto merge or choose manually. Download cleaned files instantly.

This is based on **alpha equivalence** — a concept from mathematics — applied to source code.

---

## Features

| Feature | Description |
|---|---|
| 🌐 Web App | Upload files, scan instantly in the browser. No install required. |
| ⚡ CLI Tool | `sir scan ./src` from any terminal. CI/CD integration with `--strict` flag. |
| 🔌 VS Code Extension | Scans your workspace. Merge duplicates with diff preview. |
| 🤖 AI Translation | Cross-language detection via Ollama (local/free) or Claude API. |
| 📦 Pack & Diff | Export semantic fingerprints as JSON. Compare codebases without sharing source. |
| 🔀 Merge | Auto merge all duplicates or choose manually per cluster. |
| 🔒 Private by Default | Files processed in memory, never stored. Fully local with Ollama. |

---

## Supported Languages

**Native** (no AI needed): Python, JavaScript, TypeScript

**AI-powered** (via Ollama or Claude API): Java, Rust, Go, C, C++, C#, Swift, Kotlin, Scala, Ruby, PHP, Haskell, Elixir, Lua, Dart, Julia, R, Nim, Zig, and more.

---

## Quick Start

### Web App
Go to the [live demo](https://sri-engine-7amwtce7a23k7q34cpnxem.streamlit.app) — no install needed.

### CLI Tool

```bash
# Clone the repo
git clone https://github.com/lflin00/SIR-ENGINE.git
cd SIR-ENGINE

# Add alias
echo 'alias sir="python3 ~/path/to/SIR-ENGINE/sir_cli.py"' >> ~/.zshrc
source ~/.zshrc

# Scan a folder
sir scan ./my_project

# Scan with AI (requires Ollama running locally)
sir ai-scan ./my_project --backend ollama --model codellama:7b

# Check health score only
sir health ./my_project

# Compare two versions of a codebase
sir diff ./v1 ./v2

# CI/CD — exit code 1 if duplicates found
sir scan ./src --strict
```

### VS Code Extension

1. Download [sir-engine-0.0.2.vsix](https://github.com/lflin00/SIR-ENGINE/raw/main/sir-engine-0.0.2.vsix)
2. Open VS Code → Extensions → `...` menu → **Install from VSIX**
3. Select the downloaded file
4. Open any Python or JavaScript project and run **SIR: Scan Workspace** from the command palette

### AI Setup (for cross-language detection)

**Option 1 — Ollama (free, local):**
```bash
# Install Ollama from https://ollama.ai
ollama pull codellama:7b
# Then select "Ollama" as backend in the web app sidebar
```

**Option 2 — Claude API:**
Get an API key from [console.anthropic.com](https://console.anthropic.com) and enter it in the web app sidebar.

---

## CLI Reference

```
sir scan <path> [--min N] [--output file.json] [--strict] [--no-recurse]
sir ai-scan <path> [--backend ollama|anthropic] [--model MODEL]
sir health <path>
sir diff <path1> <path2>
```

| Flag | Description |
|---|---|
| `--min N` | Minimum cluster size to report (default: 2) |
| `--output FILE` | Save full report as JSON |
| `--strict` | Exit with code 1 if any duplicates found (for CI/CD) |
| `--no-recurse` | Don't scan subdirectories |

---

## GitHub Actions

Add SIR Engine as an automatic pull request check in any GitHub repository.

### Quick setup

Copy the workflow file into your repo and push:

```bash
mkdir -p .github/workflows
curl -sSL https://raw.githubusercontent.com/lflin00/SIR-ENGINE/main/.github/workflows/sir-scan.yml \
  -o .github/workflows/sir-scan.yml
git add .github/workflows/sir-scan.yml
git commit -m "Add SIR Engine semantic duplicate check"
git push
```

The check runs automatically on every pull request. No secrets required for native Python / JS / TS scanning.

### What it does

1. **Detects changed files** — diffs HEAD against the PR base, filters to `.py`, `.js`, `.ts`, `.jsx`, `.tsx`
2. **Scans the full configured path** — runs `sir scan` across the repo (or the directory you specify), so cross-file duplicates are caught even when only one side of the duplicate changed
3. **Posts a PR comment** — lists every duplicate cluster that touches a changed file, with function name, file path, and line number; the comment is updated in place on every new push so it never spams the thread
4. **Optionally fails the check** — set `SIR_STRICT: "true"` to block merges until duplicates are resolved

### Configuration

Edit the `env` block near the top of the workflow file:

| Variable | Default | Description |
|---|---|---|
| `SIR_STRICT` | `"false"` | `"true"` → fail the check if duplicates are found in changed files |
| `SIR_MIN_CLUSTER_SIZE` | `"2"` | Minimum copies to report as a duplicate cluster |
| `SIR_SCAN_PATH` | `"."` | Root directory to scan (relative to repo root) |
| `SIR_AI_BACKEND` | `""` | `"anthropic"` to also scan Java, Go, Rust, C, C#, Swift, Kotlin, and 20+ other languages |

### Strict mode

```yaml
# .github/workflows/sir-scan.yml
env:
  SIR_STRICT: "true"
```

The PR check turns red and blocks merging until all duplicate clusters in the changed files are resolved.

### AI-powered scanning (multi-language)

```yaml
env:
  SIR_AI_BACKEND: "anthropic"
```

Add your Anthropic API key as a repository secret named `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions → New repository secret). The action will translate Java, Go, Rust, C, C#, and other languages to Python before hashing, enabling cross-language duplicate detection across the whole PR.

### Reusable workflow

Instead of copying the file, call the workflow directly from the SIR Engine repository:

```yaml
# .github/workflows/pr-checks.yml  (in your repo)
name: PR Checks

on:
  pull_request:

jobs:
  sir-scan:
    uses: lflin00/SIR-ENGINE/.github/workflows/sir-scan.yml@main
    with:
      strict: true
      min_cluster_size: 2
      scan_path: "src"
      ai_backend: ""
      base_sha: ${{ github.event.pull_request.base.sha }}
      head_sha: ${{ github.sha }}
      pr_number: ${{ github.event.pull_request.number }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## Pack Format

SIR Engine can export semantic fingerprints of entire codebases as portable `.sir.json` files. This lets you:

- Compare two codebases without sharing source code
- Store a semantic snapshot of your codebase at a point in time
- Merge fingerprints from multiple codebases into a unified index

Use the **Pack** tab in the web app to create and manage bundles.

---

## Architecture

```
Source code (any language)
        │
        ▼
  AI Translation          ← Ollama / Claude API (for non-Python/JS)
        │
        ▼
  Python AST parse
        │
        ▼
  AlphaRenamer            ← strips variable names, function names
        │
        ▼
  SHA-256(ast.dump())     ← deterministic structural hash
        │
        ▼
  Hash comparison         ← same hash = same logic
```

---

## License

Business Source License (BSL). Free for personal and open-source use. Contact for commercial licensing.

---

## Contributing

Open an issue on the [Issues tab](https://github.com/lflin00/SIR-ENGINE/issues) — bug reports, feedback, and feature requests welcome.

Built by [Lucas Flinders](https://github.com/lflin00) — biomedical engineering student at Ohio State.
