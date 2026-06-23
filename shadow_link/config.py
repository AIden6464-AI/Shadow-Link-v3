"""
shadow_link/config.py

Loads configuration from environment variables or .env file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "EMBEDDING_PROVIDER": "ollama",
    "EMBEDDING_API_URL": "http://localhost:1235/v1/embeddings",
    "EMBEDDING_MODEL": "sentence-transformers/all-MiniLM-L6-v2",
    "DB_PATH": "shadow_link.db",
    "JACCARD_THRESHOLD": "0.6"
}

CONFIG_JSON_NAME = "config.json"


def _flag(value: bool, true: str = "1", false: str = "0") -> str:
    """Render a JSON bool as the string each env consumer expects."""
    return true if value else false


def apply_config_json(root_dir: Path | None = None) -> dict:
    """
    Load ``config.json`` (if present at the package root) and populate
    ``os.environ`` so every module — which reads its settings from the
    environment at import time — is driven by one human-friendly file.

    Real environment variables always win (we only ``setdefault``), so config
    precedence is:  OS environment  >  config.json  >  .env  >  hardcoded default.

    Returns the parsed dict (empty if no file / parse error).
    """
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent
    cfg_path = root_dir / CONFIG_JSON_NAME
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # malformed JSON shouldn't break startup
        print(f"Warning: failed to parse {cfg_path.name}: {e}")
        return {}

    def setenv(key: str, val) -> None:
        if val is None:
            return
        os.environ.setdefault(key, str(val))

    db = data.get("database", {})
    setenv("DB_PATH", db.get("path"))

    emb = data.get("embeddings", {})
    # One source of truth for both historical embedding env var names.
    setenv("EMBED_SERVER_URL", emb.get("server_url"))
    setenv("EMBEDDING_API_URL", emb.get("server_url"))
    setenv("EMBED_MODEL", emb.get("model"))
    setenv("EMBEDDING_MODEL", emb.get("model"))
    setenv("EMBEDDING_PROVIDER", emb.get("provider"))
    setenv("EMBED_TIMEOUT_SECONDS", emb.get("timeout_seconds"))
    if "use_on_hot_path" in emb:
        setenv("SHADOW_QUERY_EMBED", _flag(bool(emb["use_on_hot_path"])))

    chat = data.get("chat", {})
    setenv("CHAT_SERVER_URL", chat.get("server_url"))
    setenv("CHAT_MODEL", chat.get("model"))
    setenv("CHAT_TIMEOUT_SECONDS", chat.get("timeout_seconds"))
    setenv("SHADOW_GHOST_MAX_TOKENS", chat.get("max_tokens"))
    setenv("SHADOW_GHOST_EXAMPLES", chat.get("examples"))
    setenv("SHADOW_GHOST_SITUATION_CHARS", chat.get("situation_chars"))
    setenv("SHADOW_GHOST_REPLY_CHARS", chat.get("reply_chars"))
    if "lm_mode" in chat:
        setenv("SHADOW_LM_MODE", _flag(bool(chat["lm_mode"])))

    matching = data.get("matching", {})
    setenv("ALPHA_BASE", matching.get("alpha_base"))
    setenv("GLOBAL_THRESHOLD", matching.get("global_threshold"))
    setenv("JACCARD_THRESHOLD", matching.get("global_threshold"))
    setenv("GHOST_MIN_SIGNAL", matching.get("ghost_min_signal"))
    for bucket, thr in (matching.get("thresholds") or {}).items():
        setenv(f"THRESHOLD_{bucket.upper()}", thr)

    server = data.get("server", {})
    setenv("SHADOW_LINK_HOST", server.get("host"))
    setenv("SHADOW_LINK_PORT", server.get("port"))

    proxy = data.get("proxy", {})
    setenv("SHADOW_LINK_URL", proxy.get("daemon_url"))
    setenv("SHADOW_LINK_PROXY_PORT", proxy.get("port"))
    setenv("SHADOW_LINK_NUDGE_BUDGET", proxy.get("nudge_budget_seconds"))
    setenv("SHADOW_LINK_NUDGE_TIMEOUT", proxy.get("nudge_timeout_seconds"))
    if "inject_nudges" in proxy:
        setenv("SHADOW_LINK_INJECT", _flag(bool(proxy["inject_nudges"]), "true", "false"))
    if "inject_synthesized" in proxy:
        setenv("SHADOW_LINK_INJECT_SYNTHESIZED",
               _flag(bool(proxy["inject_synthesized"]), "true", "false"))
    if "learn" in proxy:
        setenv("SHADOW_LINK_LEARN", _flag(bool(proxy["learn"]), "true", "false"))

    return data

def load_dotenv(env_path: Path):
    """Loads key-value pairs from a .env file into os.environ."""
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key not in os.environ:
                            os.environ[key] = val
        except Exception as e:
            print(f"Warning: Failed to parse .env file: {e}")

class Config:
    def __init__(self, root_dir: Path | None = None):
        if root_dir is None:
            root_dir = Path(__file__).resolve().parent.parent
            
        env_file = root_dir / ".env"
        load_dotenv(env_file)

        self.embedding_provider = os.environ.get("EMBEDDING_PROVIDER", DEFAULT_CONFIG["EMBEDDING_PROVIDER"]).lower()
        self.embedding_api_url = os.environ.get("EMBEDDING_API_URL", DEFAULT_CONFIG["EMBEDDING_API_URL"])
        self.embedding_model = os.environ.get("EMBEDDING_MODEL", DEFAULT_CONFIG["EMBEDDING_MODEL"])
        
        # Override with exact DB_PATH if set in env
        db_path_str = os.environ.get("DB_PATH", DEFAULT_CONFIG["DB_PATH"])
        db_path = Path(db_path_str)
        if not db_path.is_absolute():
            self.db_path = root_dir / db_path
        else:
            self.db_path = db_path
            
        try:
            self.jaccard_threshold = float(os.environ.get("JACCARD_THRESHOLD", DEFAULT_CONFIG["JACCARD_THRESHOLD"]))
        except ValueError:
            self.jaccard_threshold = 0.6

    def __repr__(self):
        return (f"Config(provider={self.embedding_provider}, "
                f"url={self.embedding_api_url}, model={self.embedding_model}, "
                f"db={self.db_path}, threshold={self.jaccard_threshold})")
