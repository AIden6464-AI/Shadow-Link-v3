"""
Shadow Link — local embedding server (OpenAI-compatible) for v3.

Exposes POST /v1/embeddings on port 1235.
Model: sentence-transformers/all-MiniLM-L6-v2 (384-dim, ~90MB, CPU-fine).
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModel, AutoTokenizer

MODEL_NAME = os.environ.get("EMBED_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
PORT = int(os.environ.get("EMBED_PORT", "1235"))

print(f"[embed] loading {MODEL_NAME} ...")
_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
_model = AutoModel.from_pretrained(MODEL_NAME)
_model.eval()
print("[embed] model ready.")

def _mean_pool(last_hidden, attention_mask):
    mask = attention_mask.unsqueeze(-1).expand(last_hidden.size()).float()
    summed = torch.sum(last_hidden * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts

@torch.no_grad()
def embed(texts: list[str]) -> list[list[float]]:
    enc = _tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="pt")
    out = _model(**enc)
    pooled = _mean_pool(out.last_hidden_state, enc["attention_mask"])
    pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
    return pooled.tolist()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/embeddings", "/embeddings"):
            self.send_error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self.send_error(400, f"Bad JSON: {e}")
            return

        raw = data.get("input", "")
        texts = [raw] if isinstance(raw, str) else list(raw)
        if not texts:
            self.send_error(400, "Empty input")
            return

        try:
            vectors = embed(texts)
        except Exception as e:
            self.send_error(500, f"Embed failed: {e}")
            return

        payload = {
            "object": "list",
            "model": data.get("model", MODEL_NAME),
            "data": [{"object": "embedding", "index": i, "embedding": v}
                     for i, v in enumerate(vectors)],
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"[embed] OpenAI-compatible embeddings on http://localhost:{PORT}/v1/embeddings")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()
