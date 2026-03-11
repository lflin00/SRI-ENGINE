# SIR Web UI — Skill Reference

## Overview

`sir_ui.py` is a Streamlit app providing a browser-based interface to all V1 features. No CLI knowledge needed — users upload files and get results in-browser.

---

## Running Locally

```bash
streamlit run sir_ui.py
```

Opens at `http://localhost:8501`. Detects local vs Streamlit Cloud automatically and switches AI backends accordingly (Ollama locally, Anthropic API on cloud).

---

## Tabs

### Scan
- Upload `.py`, `.js`, or `.ts` files
- Detects duplicate functions using semantic hashing (V1)
- Shows duplicate clusters with file/line locations
- Supports Python (via `sir1.py`) and JS/TS (via `sir_js.py`)

### Pack
- Upload Python files, export semantic fingerprints as a `.sir.json` pack
- Pack is portable — share without sharing source code
- Uses `sir_pack.py` internally

### Diff
- Upload two `.sir.json` packs, compare them
- Reports: identical functions, added, removed
- Uses `sir_tools.py` diff logic

### Merge
- Upload files with detected duplicates
- **Auto-merge**: consolidates duplicates automatically, downloads cleaned files + HTML report
- **Manual merge**: user picks which duplicate to keep per cluster
- Downloads a zip of cleaned files and a human-readable HTML diff report

---

## Local vs Cloud Detection

```python
# sir_ui.py detects environment and picks AI backend:
# - Local: Ollama (free, runs on device) for ai-scan / cross-language
# - Cloud: Anthropic API (requires ANTHROPIC_API_KEY secret in Streamlit Cloud)
```

If running on Streamlit Cloud, set the secret `ANTHROPIC_API_KEY` in the app's settings.

---

## Key Implementation Details

- Each tab is a separate function called from the main `st.tabs([...])` block
- File uploads use `st.file_uploader(accept_multiple_files=True)`
- Results are cached in `st.session_state` to avoid re-scanning on every interaction
- Merge downloads use `st.download_button` with a zip built in memory via `io.BytesIO`
- HTML report is generated inline as a string, included in the zip

---

## What's Not Yet in the UI

- **V2 class-level detection** — `sir2_core.py` is implemented and tested but not wired into any UI tab
- **AI confidence scores** — `sir_ai_translate.py` computes HIGH/LOW confidence but this isn't surfaced in the Scan tab
- **JS/TS pack/diff/merge** — the `sir_js_pipeline.py` commands exist in CLI but the UI only supports Python pack/diff

---

## Adding a New Tab

```python
# In sir_ui.py, add to the tabs list:
tab_scan, tab_pack, tab_diff, tab_merge, tab_new = st.tabs([
    "Scan", "Pack", "Diff", "Merge", "New Tab"
])

with tab_new:
    render_new_tab()

def render_new_tab():
    st.header("New Feature")
    uploaded = st.file_uploader("Upload files", accept_multiple_files=True)
    if uploaded:
        # process...
        st.success("Done")
```

---

## Dependencies

```bash
pip install streamlit
# For local AI backend:
# Ollama must be running: https://ollama.ai
# For cloud AI backend:
pip install anthropic
```
