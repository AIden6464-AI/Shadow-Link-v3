"""
shadow_link/clipboard.py

Best-effort cross-platform clipboard copy. Used to close the loop after a
ghost is /accept-ed — the operator can paste straight into Claude Code.

Order of preference:
  Windows: clip.exe (always present since XP)
  macOS:   pbcopy
  Linux:   wl-copy (Wayland) -> xclip (X11) -> xsel
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _copy_via(cmd: list[str], text: str) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            input=text,
            text=True,
            encoding="utf-8",
            timeout=2.0,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the OS clipboard. Returns True on success."""
    if not text:
        return False
    if sys.platform == "win32":
        return _copy_via(["clip"], text)
    if sys.platform == "darwin":
        return _copy_via(["pbcopy"], text)
    # Linux / *BSD
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return _copy_via(["wl-copy"], text)
    if shutil.which("xclip"):
        return _copy_via(["xclip", "-selection", "clipboard"], text)
    if shutil.which("xsel"):
        return _copy_via(["xsel", "--clipboard", "--input"], text)
    return False
