"""
api/validate.py — SIR Engine license validation endpoint
Deployed as a Vercel serverless function at:
  https://api.sir-engine.com/validate  (POST)

REQUEST
  Content-Type: application/json
  { "key": "<raw license key>" }

RESPONSE
  200  { "valid": true,  "issued_to": "customer@example.com", "expires": "2027-01-01" }
  401  { "error": "invalid_key" }   — key not found
  401  { "error": "expired" }       — key exists but past expiry date
  400  { "error": "missing_key" }   — no key in request body
  500  { "error": "config_error" }  — SIR_LICENSE_KEYS env var is malformed

ENVIRONMENT VARIABLE (set in Vercel dashboard)
  SIR_LICENSE_KEYS — JSON object mapping SHA-256(key) → license metadata:

  {
    "<sha256_of_key>": {
      "issued_to": "customer@example.com",
      "expires":   "2027-01-01",
      "repo":      "*"
    }
  }

  Raw keys are never stored — only their SHA-256 hashes.
  Generate keys with: python3 scripts/generate_key.py customer@example.com
"""

from http.server import BaseHTTPRequestHandler
from datetime import date
import hashlib
import json
import os


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        # ── Parse request body ───────────────────────────────────────────────
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"error": "invalid_json"})
            return

        key = data.get("key", "").strip()
        if not key:
            self._respond(400, {"error": "missing_key"})
            return

        # ── Load license store from environment ──────────────────────────────
        raw_store = os.environ.get("SIR_LICENSE_KEYS", "{}")
        try:
            store = json.loads(raw_store)
        except json.JSONDecodeError:
            self._respond(500, {"error": "config_error"})
            return

        # ── Look up by hash ──────────────────────────────────────────────────
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        license_info = store.get(key_hash)

        if not license_info:
            self._respond(401, {"error": "invalid_key"})
            return

        # ── Check expiry ─────────────────────────────────────────────────────
        expires = license_info.get("expires", "")
        if expires and date.today().isoformat() > expires:
            self._respond(401, {"error": "expired"})
            return

        # ── Valid ────────────────────────────────────────────────────────────
        self._respond(200, {
            "valid":      True,
            "issued_to":  license_info.get("issued_to", ""),
            "expires":    expires,
        })

    def do_GET(self):
        # Health check — Vercel ping / uptime monitors
        self._respond(200, {"status": "ok"})

    def _respond(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Suppress default Apache-style access logs in Vercel output
        pass
