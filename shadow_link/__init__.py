"""
shadow_link v3 package
"""
from __future__ import annotations

# Load config.json into the environment BEFORE any submodule reads its settings.
# Importing any shadow_link.* module runs this first, so a single config.json at
# the package root drives the whole package.
from shadow_link.config import apply_config_json as _apply_config_json

_apply_config_json()
