#!/usr/bin/env python3
"""
zenith_wrap.py — Unified encode/compress/decode tool for Zenith/Jarvis.

Three modes, chosen automatically by file type:

  .py files      → SIR mode (Structured Intermediate Representation)
                   Uses sir1.py to parse Python into a canonical AST node graph.
                   100% deterministic. Lossless. No LLM involved.

  other files    → zstd mode (default)
                   Compresses text directly with zstd level 22.
                   Fast, lossless, ~3-5% of original size on typical text.

  other + --llm  → LLM + zstd mode
                   phi3 pre-encodes the text first (reduces redundancy),
                   then zstd compresses the result for maximum squeeze.
                   Best-effort fidelity; use for archival not exact recovery.

All modes produce a .zwrap binary file. Only this tool can decode them.

Requirements:
    pip3 install zstandard requests
    ollama pull phi3       (only needed for --llm mode)
    ollama serve           (only needed for --llm mode)
    sir1.py                (must be alongside this script)

Usage:
    python3 zenith_wrap.py encode input.py    output.zwrap
    python3 zenith_wrap.py encode input.txt   output.zwrap
    python3 zenith_wrap.py encode input.txt   output.zwrap --llm
    python3 zenith_wrap.py decode input.zwrap output.txt
    python3 zenith_wrap.py info   input.zwrap
"""

from __future__ import annotations

import json
import sys
import hashlib
import subprocess
import tempfile
import os
import argparse
import time
from pathlib import Path
from typing import Any, Dict

try:
    import zstandard as zstd
except ImportError:
    print("[ERROR] zstandard not installed. Run: pip3 install zstandard")
    sys.exit(1)

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────

MAGIC = b"ZWRAP2"          # bumped from ZWRAP1 to distinguish old files
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "phi3"
CHUNK_SIZE = 1500
SIR1_PATH = Path(__file__).parent / "sir1.py"
ZSTD_LEVEL = 22            # max compression

MODE_SIR  = "sir"
MODE_ZSTD = "zstd"
MODE_LLM  = "llm+zstd"


# ─────────────────────────────────────────────
#  zstd helpers
# ─────────────────────────────────────────────

def zstd_compress(data: bytes) -> bytes:
    cctx = zstd.ZstdCompressor(level=ZSTD_LEVEL)
    return cctx.compress(data)


def zstd_decompress(data: bytes) -> bytes:
    dctx = zstd.ZstdDecompressor()
    return dctx.decompress(data)


# ─────────────────────────────────────────────
#  LLM helpers (phi3 via Ollama)
# ─────────────────────────────────────────────

ENCODE_PROMPT = """\
You are a lossless compact encoder. Rewrite the text below using these exact substitution rules:
  the->τ, and->&, is->=, with->w/, because->bc, information->info, function->fn,
  that->tt, this->ts, from->frm, have->hv, for->4, to->2, you->u, are->r, be->b,
  at->@, not->!, of->/, msg->message, txt->text, fl->file
- Remove filler words: very, just, really, quite, some, also
- Shorten words by removing vowels where clear (e.g. "message"->"msg")
- Do NOT change proper nouns, numbers, or punctuation
- Output ONLY the encoded text, nothing else

TEXT:
{text}

ENCODED:"""

DECODE_PROMPT = """\
You are a lossless decoder. Expand ALL codes back to full English words using this table:
  τ->the, &->and, =->is, w/->with, bc->because, info->information, fn->function,
  tt->that, ts->this, frm->from, hv->have, 4->for, 2->to, u->you, r->are, b->be,
  @->at, !->not, /->of, msg->message, txt->text, fl->file
- Restore missing vowels to reconstruct full words
- Reconstruct natural sentence structure
- Do NOT add, invent, or infer anything beyond what is encoded
- Output ONLY the decoded text, nothing else

ENCODED:
{text}

DECODED:"""


def ask_ollama(prompt: str) -> str:
    if not REQUESTS_AVAILABLE:
        print("[ERROR] requests not installed. Run: pip3 install requests")
        sys.exit(1)
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("\n[ERROR] Ollama not reachable. Run: ollama serve")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("\n[ERROR] Ollama timed out. Try reducing CHUNK_SIZE.")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Ollama: {e}")
        sys.exit(1)


def chunk_text(text: str, size: int) -> list:
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, size)
        if split_at == -1:
            split_at = size
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks


def llm_encode(text: str) -> str:
    chunks = chunk_text(text, CHUNK_SIZE)
    total = len(chunks)
    print(f"      phi3 encoding — {total} chunks...")
    parts = []
    for i, chunk in enumerate(chunks, 1):
        print(f"      Chunk {i}/{total}...", end="\r", flush=True)
        parts.append(ask_ollama(ENCODE_PROMPT.format(text=chunk)))
    print(f"      All {total} chunks encoded. ✓          ")
    return '\n'.join(parts)


def llm_decode(text: str) -> str:
    chunks = chunk_text(text, CHUNK_SIZE)
    total = len(chunks)
    print(f"      phi3 decoding — {total} chunks...")
    parts = []
    for i, chunk in enumerate(chunks, 1):
        print(f"      Chunk {i}/{total}...", end="\r", flush=True)
        parts.append(ask_ollama(DECODE_PROMPT.format(text=chunk)))
    print(f"      All {total} chunks decoded. ✓          ")
    return '\n'.join(parts)


# ─────────────────────────────────────────────
#  SIR helpers
# ─────────────────────────────────────────────

def sir_encode(source: str) -> Dict[str, Any]:
    if not SIR1_PATH.exists():
        print(f"[ERROR] sir1.py not found at {SIR1_PATH}")
        sys.exit(1)
    result = subprocess.run(
        ["python3", str(SIR1_PATH), "encode", "-", "--mode", "semantic"],
        input=source, text=True, capture_output=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] sir1.py encode failed:\n{result.stderr.strip()}")
        sys.exit(1)
    return json.loads(result.stdout)


def sir_decode(sir: Dict[str, Any], rehydrate: bool = True) -> str:
    if not SIR1_PATH.exists():
        print(f"[ERROR] sir1.py not found at {SIR1_PATH}")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sir.json", delete=False) as tmp:
        json.dump(sir, tmp)
        tmp_path = tmp.name
    try:
        cmd = ["python3", str(SIR1_PATH), "decode", tmp_path]
        if rehydrate and "name_map" in sir:
            cmd.append("--rehydrate")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[ERROR] sir1.py decode failed:\n{result.stderr.strip()}")
            sys.exit(1)
        return result.stdout
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────
#  Pack / Unpack
# ─────────────────────────────────────────────

def pack(payload_bytes: bytes, metadata: Dict[str, Any], output_path: str) -> None:
    """MAGIC | meta_len(4LE) | meta_json | zstd(payload)"""
    metadata["checksum_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    meta_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = zstd_compress(payload_bytes)

    with open(output_path, "wb") as f:
        f.write(MAGIC)
        f.write(len(meta_json).to_bytes(4, "little"))
        f.write(meta_json)
        f.write(compressed)

    orig  = len(payload_bytes)
    final = os.path.getsize(output_path)
    saved = orig - final
    ratio = 100 * final // max(orig, 1)
    print(f"      Original:   {orig:,} bytes")
    print(f"      Compressed: {final:,} bytes")
    print(f"      Saved:      {saved:,} bytes  ({100 - ratio}% reduction)")


def unpack(input_path: str):
    with open(input_path, "rb") as f:
        data = f.read()

    if not data.startswith(MAGIC):
        # Friendly message if someone tries an old .zwrap file
        if data.startswith(b"ZWRAP1"):
            print("[ERROR] This file was made with the old wrapper (ZWRAP1).")
            print("        Re-encode it with the current zenith_wrap.py to upgrade.")
        else:
            print("[ERROR] Not a valid .zwrap file.")
        sys.exit(1)

    offset = len(MAGIC)
    meta_len = int.from_bytes(data[offset:offset + 4], "little")
    offset += 4
    metadata = json.loads(data[offset:offset + meta_len].decode("utf-8"))
    offset += meta_len

    raw = zstd_decompress(data[offset:])

    actual = hashlib.sha256(raw).hexdigest()
    if actual != metadata.get("checksum_sha256"):
        print("      [WARNING] Checksum mismatch — file may be corrupted!")
    else:
        print("      Checksum OK ✓")

    return metadata, raw


# ─────────────────────────────────────────────
#  Encode
# ─────────────────────────────────────────────

def encode_file(input_path: str, output_path: str, use_llm: bool = False) -> None:
    _t0 = time.time()
    path = Path(input_path)
    is_python = path.suffix == ".py"

    if is_python:
        mode = MODE_SIR
        mode_label = "SIR (deterministic, lossless)"
    elif use_llm:
        mode = MODE_LLM
        mode_label = "LLM + zstd (phi3 pre-encode → zstd)"
    else:
        mode = MODE_ZSTD
        mode_label = "zstd (lossless)"

    print(f"[1/4] Reading '{input_path}'  →  mode: {mode_label}")
    source = path.read_text(encoding="utf-8")

    if not source.strip():
        print("[ERROR] Input file is empty.")
        sys.exit(1)

    if mode == MODE_SIR:
        print("[2/4] Encoding Python AST → SIR node graph...")
        sir = sir_encode(source)
        payload = json.dumps(sir, separators=(",", ":"), sort_keys=True).encode("utf-8")
        print(f"      SIR nodes:  {len(sir['nodes'])}")
        print(f"      sir_sha256: {sir.get('sir_sha256', 'n/a')}")
        metadata = {
            "mode": MODE_SIR,
            "original_file": path.name,
            "original_chars": len(source),
            "sir_sha256": sir.get("sir_sha256"),
            "sir_version": sir.get("version"),
            "has_name_map": "name_map" in sir,
        }

    elif mode == MODE_LLM:
        print("[2/4] LLM pre-encoding with phi3...")
        encoded_text = llm_encode(source)
        ratio = 100 * len(encoded_text) // max(len(source), 1)
        print(f"      LLM output: {len(encoded_text):,} chars ({ratio}% of original)")
        payload = encoded_text.encode("utf-8")
        metadata = {
            "mode": MODE_LLM,
            "model": MODEL,
            "original_file": path.name,
            "original_chars": len(source),
            "encoded_chars": len(encoded_text),
        }

    else:  # MODE_ZSTD
        print("[2/4] Preparing payload...")
        payload = source.encode("utf-8")
        print(f"      Text size: {len(payload):,} bytes")
        metadata = {
            "mode": MODE_ZSTD,
            "original_file": path.name,
            "original_chars": len(source),
        }

    print("[3/4] Compressing with zstd-22 + writing...")
    pack(payload, metadata, output_path)
    elapsed = time.time() - _t0
    print(f"[4/4] Done ✅  →  '{output_path}'  ({elapsed:.1f}s)")


# ─────────────────────────────────────────────
#  Decode
# ─────────────────────────────────────────────

def decode_file(input_path: str, output_path: str) -> None:
    _t0 = time.time()
    print(f"[1/4] Reading '{input_path}'...")
    metadata, raw = unpack(input_path)

    mode = metadata.get("mode")
    labels = {
        MODE_SIR:  "SIR (deterministic)",
        MODE_ZSTD: "zstd (lossless)",
        MODE_LLM:  "LLM + zstd",
    }
    print(f"      Mode: {labels.get(mode, mode)}")
    print(f"      Original file: {metadata.get('original_file', '?')}")

    if mode == MODE_SIR:
        print("[2/4] Deserialising SIR node graph...")
        sir = json.loads(raw.decode("utf-8"))
        print(f"      sir_sha256: {metadata.get('sir_sha256', 'n/a')}")
        print("[3/4] Reconstructing Python source...")
        source = sir_decode(sir, rehydrate=metadata.get("has_name_map", False))
        print(f"      Reconstructed: {len(source):,} chars")

    elif mode == MODE_ZSTD:
        print("[2/4] Decompressed successfully.")
        source = raw.decode("utf-8")
        print(f"[3/4] Restored: {len(source):,} chars  "
              f"(original was {metadata.get('original_chars', '?'):,} chars)")

    elif mode == MODE_LLM:
        encoded_text = raw.decode("utf-8")
        print(f"[2/4] Decompressed {len(encoded_text):,} chars of LLM-encoded text")
        print("[3/4] LLM decoding with phi3...")
        source = llm_decode(encoded_text)
        print(f"      Decoded: {len(source):,} chars  "
              f"(original was {metadata.get('original_chars', '?'):,} chars)")

    else:
        print(f"[ERROR] Unknown mode in file: '{mode}'")
        sys.exit(1)

    print(f"[4/4] Writing '{output_path}'...")
    Path(output_path).write_text(source, encoding="utf-8")
    elapsed = time.time() - _t0
    print(f"\nDone ✅  →  '{output_path}'  ({elapsed:.1f}s)")


# ─────────────────────────────────────────────
#  Info
# ─────────────────────────────────────────────

def info_file(input_path: str) -> None:
    with open(input_path, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        print("[ERROR] Not a valid .zwrap file.")
        sys.exit(1)
    offset = len(MAGIC)
    meta_len = int.from_bytes(data[offset:offset + 4], "little")
    offset += 4
    metadata = json.loads(data[offset:offset + meta_len].decode("utf-8"))

    print(f"\n── .zwrap metadata  ({input_path}) ──────────────")
    for k, v in sorted(metadata.items()):
        print(f"  {k:28s} {v}")
    print(f"  {'total_file_bytes':28s} {len(data):,}")
    print("──────────────────────────────────────────────────")


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="zenith_wrap.py", description="Zenith unified encode/decode tool.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    enc = sub.add_parser("encode", help="Encode + compress a file into .zwrap")
    enc.add_argument("input",  help="Input file (.py → SIR, anything else → zstd)")
    enc.add_argument("output", help="Output .zwrap file")
    enc.add_argument("--llm",  action="store_true", help="Pre-encode text with phi3 before zstd (non-.py only)")

    dec = sub.add_parser("decode", help="Decode a .zwrap file")
    dec.add_argument("input",  help="Input .zwrap file")
    dec.add_argument("output", help="Output file")

    inf = sub.add_parser("info", help="Inspect a .zwrap file without decoding")
    inf.add_argument("input", help="Input .zwrap file")

    args = ap.parse_args()

    if args.cmd == "encode":
        encode_file(args.input, args.output, use_llm=getattr(args, "llm", False))
    elif args.cmd == "decode":
        decode_file(args.input, args.output)
    elif args.cmd == "info":
        info_file(args.input)


if __name__ == "__main__":
    main()
