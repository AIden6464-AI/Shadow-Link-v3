"""
shadow_link/tui.py

Claude Code-Style Interactive Shell Console for Shadow Link v3.
Provides a scrolling terminal-native interface with autocomplete, stats tables,
and direct simulation matching.
"""

from __future__ import annotations

import os
import time
import html
import uuid
import threading
import urllib.error
import urllib.parse
import urllib.request
import json
from pathlib import Path

from collections import deque

from prompt_toolkit import PromptSession, print_formatted_text as _print_formatted_text, HTML
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from shadow_link.clipboard import copy_to_clipboard

from shadow_link.config import Config
from shadow_link.database import Database
from shadow_link.engine import Engine
from shadow_link.ingester import Ingester, classify_multi

# Shadow Link brand palette: neon signal green on a near-black terminal.
tui_style = Style.from_dict({
    "brand": "#5ff26d bold",
    "header": "#f4f7f8 bold",
    "info": "#63e875",
    "warn": "#f4c95d",
    "error": "#ff6b6b bold",
    "command": "#8af895 bold",
    "ghost": "#5ff26d bold",
    "score": "#b5f5bd bold",
    "border": "#1e3a2b",
    "table-header": "#5ff26d bold",
    "table-cell": "#dde5e3",
    "table-decay": "#b5f5bd",
    "table-bar": "#4ce861",
    "help-cmd": "#5ff26d bold",
    "help-desc": "#dde5e3",
    "alternate": "#a9bbb5",
    "separator": "#183124",
    "splash-bg": "bg:#020604",
    "splash-dim": "#14752a bg:#020604",
    "splash-mid": "#2fd94c bg:#020604",
    "splash-glow": "#5fff68 bg:#020604 bold",
    "splash-scan": "#c7ffcb bg:#020604 bold",
    "splash-prompt": "#8aff92 bg:#020604 bold",
    "prompt-logo": "#5fff68 bold",
    "prompt-pulse": "#b9ffc0 bold",
    "prompt-chevron": "#2fd94c bold",
    "toolbar": "#8aa299 bg:#07110b",
    "toolbar-accent": "#5fff68 bg:#07110b bold",
    "toolbar-ok": "#74f288 bg:#07110b bold",
    "toolbar-warn": "#f4c95d bg:#07110b bold",
    "toolbar-key": "#c7ffcb bg:#07110b bold",
    "completion-menu": "bg:#06120b #a9bbb5",
    "completion-menu.completion": "bg:#06120b #b8c9c2",
    "completion-menu.completion.current": "bg:#123f22 #7dff8d bold",
    "completion-menu.meta.completion": "bg:#08170e #6f9582 italic",
    "completion-menu.meta.completion.current": "bg:#123f22 #c7ffcb",
    "completion-kind": "#2fd94c bold",
    "completion-command": "#b9ffc0",
    "scrollbar.background": "bg:#08170e",
    "scrollbar.button": "bg:#2fd94c",
})

# Terminal-safe raster of the linked-S artwork in docs/assets. Braille cells
# provide a 2x4 subpixel grid, retaining the mark's curves and circuit traces.
TUI_BRAND_MARK = "\n".join((
    "                 ⢀⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃",
    "                ⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋",
    "              ⣠⣾⣿⣿⣿⣿⣿⡿⠿⠿⠿⠿⠿⠿⠿⠿⠿⠿⠛",
    "            ⣠⣾⣿⣿⣿⣿⣿⡿⠋",
    "           ⣼⣿⣿⣿⣿⣿⣿⠟⠁",
    "           ⣿⣿⣿⣿⣿⣿⠁     ⢀⣤⣶⣶⣶⣶⣶⣶⣶⣶⣶⣤⡀",
    "           ⣿⣿⣿⣿⣿⣿⣷⡀    ⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀",
    "           ⠈⢿⣿⣿⣿⣿⣿⣿⣦⡀  ⠘⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀",
    "             ⠙⢿⣿⣿⣿⣿⣿⣿⣦⡀  ⢲⣄⡀    ⠹⣿⣿⣿⣿⣿⣿⣿⣄",
    "               ⠙⢿⣿⣿⣿⣿⣿⣿⣦⡀⠘⠿⢇⣀⣄⡀  ⠈⠻⣿⣿⣿⣿⣿⣿⣷⣄",
    "                 ⠙⢿⣿⣿⣿⣿⣿⣿⣦⡀⠘⣿⣿⣿    ⠈⠻⣿⣿⣿⣿⣿⣿⣦",
    "     ⣴⠶⣦⣀⣀⣀⣀⣀⣀⣀⣀   ⠻⣿⣿⣿⣿⣿⣿⣿⣆⠈⠉⠙⠿⣦⡀   ⠈⢻⣿⣿⣿⣿⣿",
    "     ⠻⠶⠋⠉⠉⠉⠉⠉⠉⠉⠁    ⠈⠻⣿⣿⣿⣿⣿⣿⣷⣄  ⠘⠇    ⢸⣿⣿⣿⣿⣿",
    "⣠⣤⡄         ⣠⠶⠶⠒⠂     ⠈⠻⣿⣿⣿⣿⣿⣿⣿⣶⣶⣶⣶⡄  ⢸⣿⣿⣿⣿⣿",
    "⢿⣤⡟⠛⠛⠛⠛⠛⠛⠛⠛⠛⠁  ⢀⣀⡀      ⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿  ⢸⣿⣿⣿⣿⣿",
    "     ⢀⣄⡀     ⢀⡴⠋⠉⠁        ⠉⠻⠿⠿⠿⠿⠿⠿⠋⠁  ⢸⣿⣿⣿⣿⣿",
    "     ⣿⣉⡿⠶⠶⠶⠶⠖⠚                        ⣸⣿⣿⣿⣿⣿",
    "     ⠈⠉                             ⢀⣾⣿⣿⣿⣿⣿⣿",
    "                                  ⢀⣴⣿⣿⣿⣿⣿⣿⠟⠁",
    "                        ⣠⣶⣶⣶⣶⣶⣶⣶⣶⣿⣿⣿⣿⣿⣿⣿⠟⠁",
    "                      ⣠⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⠁",
    "                    ⢀⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⠋",
))

# Full terminal raster of the supplied Claude Shadow artwork. The source is
# monochrome neon green on near-black, so three palette bands retain its glow
# without requiring an image library at runtime. Each Braille cell carries a
# 2x4 subpixel mask; rows are padded and centered by the splash window.
CLAUDE_SHADOW_BRAILLE = (
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣀⣀⣀⣀⣀⣀⣀⡀⠀⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢤⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣄⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⠝⠋⠙⠛⠛⠛⠛⠛⠃⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⡿⠛",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⡏⠁⠀⠀⠀⣀⣶⣴⣷⣶⣤⣴⣤⣤⣤⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠻⣿⣿⣿⣿⣿⣇⠀⠀⠀⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿⣿⣿⣦⡀⠀⠘⢩⠋⠉⠉⠉⠙⢿⣿⣿⣿⣿⣷⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠿⣿⣿⣿⣿⣿⣄⡀⠘⢿⣤⣤⡀⠀⠀⠙⢿⣿⣿⣿⣿⣧⡄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⢤⡀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠻⣿⣿⣿⣿⣷⣤⡈⠿⣿⣿⡀⠀⠀⠀⠙⢿⣿⣿⣿⣿⣦",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠣⠤⠋⠉⠉⠉⠉⠉⠉⠉⠉⠀⠀⠈⢻⣿⣿⣿⣿⣷⣄⠀⠉⠙⢦⠀⠀⠀⠀⢹⣿⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⠀⢠⠤⡄⠀⠀⠀⠀⠀⠀⠀⠀⢀⠔⠒⠒⠒⠂⠀⠀⠀⠙⠻⣿⣿⣿⣿⣷⣤⣤⣼⣤⣤⠀⠀⢸⣿⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⠀⠱⠤⠏⠉⠉⠉⠉⠉⠉⠉⠉⠁⠀⠀⣀⠀⠀⠀⠀⠀⠀⠀⠘⢿⣿⣿⣿⣿⣿⣿⣿⣿⠄⠀⢸⣿⣿⣿⣿⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡴⠢⣄⣀⣀⣀⣀⣠⠜⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠛⠛⠛⠛⠻⠃⠀⠀⢸⣿⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠓⠖⠉⠉⠉⠉⠉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⣿⣿⣿⡿",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣀⣀⣀⡀⠀⠀⣠⣾⣿⣿⣿⣿⠿⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣲⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⠛⠛⠛⠛⠛⠛⠛⠛⠛⠛⠋⠉",
    "",
    "⠀⠀⠀⠀⢀⣀⣀⣀⣀⣀⣀⣀⡀⠀⣀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀",
    "⠀⠀⢠⣾⣿⡿⠿⠿⠿⠿⠿⠿⠇⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⡇",
    "⠀⠀⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⣶⣶⣿⣿⣿⣷⣶⣄⠀⠀⢰⣶⡆⠀⠀⠀⠀⣶⣶⠀⠀⢀⣴⣶⣿⣿⣿⣶⣾⣿⡇⠀⠀⣴⣶⣾⣿⣿⣿⣶⣦",
    "⠀⠀⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⣀⣀⣤⣤⣤⣤⣿⣿⡄⠀⢸⣿⡇⠀⠀⠀⠀⣿⣿⠀⠀⣼⣿⠋⠀⠀⠀⠀⢹⣿⡇⠀⢸⣿⣏⣀⣀⣀⣀⣹⣿⡇",
    "⠀⠀⢿⣿⣇⡀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⢸⣿⡟⠉⠉⠉⠉⣿⣿⡇⠀⢸⣿⡇⠀⠀⠀⠀⣿⣿⠀⠀⢿⣿⡄⠀⠀⠀⠀⣸⣿⡇⠀⢸⣿⡟⠛⠛⠛⠛⠛⠋⠁",
    "⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠹⢿⣿⣿⠀⠘⠿⣿⣶⣶⣶⠿⢿⣿⡇⠀⠘⠿⣿⣿⣿⣿⠿⢿⣿⠀⠀⠘⠿⣿⣿⣿⣿⡿⢿⣿⡇⠀⠀⠻⢿⣿⣿⣶⣿⣿⣿",
    "",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⠤⠤⠄⠀⠀⠀⣤⠀⠀⡄⠀⠀⠀⠀⡤⡄⠀⠀⠀⠀⡤⠤⢤⡀⠀⠀⢀⡤⠤⢤⠀⠀⠀⢤⠀⣠⡀⢠",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠒⠂⠀⠀⣙⣛⣓⡆⠀⠀⠀⣿⠛⠛⡇⠀⠀⠀⣼⠧⠽⡄⠀⠀⠀⣇⣀⣸⠇⠀⠀⢸⣇⣀⣸⠀⠀⠀⠘⣧⠇⢧⡏⠀⠀⠐⠒⠐",
)

CLAUDE_SHADOW_BRAILLE_COMPACT = (
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣤⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶⣦⠄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢿⠋⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣶⣿⣿⣿⡿⠟⠉",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣏⡁⠀⠀⢠⣰⣤⣶⣶⣤⣦⣤⣤⣤⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠛⣿⣿⣿⣿⣷⣤⡀⠈⠻⢿⠿⠿⠿⠿⢿⣿⣿⣿⣦⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠻⣿⣿⣿⣿⣦⡀⠈⢶⣤⣄⡀⠀⠙⠻⣿⣿⣿⣷⣄⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⠒⡦⠤⠤⠤⠤⠤⠤⠄⠀⠙⢿⣿⣿⣿⣷⣄⠙⠻⠿⢄⠀⠀⠈⠛⣿⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⢀⠤⡀⠀⠀⠉⠀⠀⠀⠀⡠⠤⠤⠤⠀⠀⠀⠈⠻⣿⣿⣿⣿⣦⣤⣤⣇⣄⠀⠀⢸⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⠘⠒⠉⠉⠉⣉⠉⠉⠉⠉⠀⢀⡠⠒⠒⠒⠂⠀⠀⠀⠙⠿⣿⣿⣿⣿⣿⡿⠃⠀⢸⣿⣿⣿",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⠤⠗⠒⠒⠒⠒⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⣿⣿⣿⠄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣤⣤⣤⣤⣀⣠⣴⣾⣿⣿⡿⠟⠃",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣲⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠛⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠙⠛⠛⠛⠛⠛⠛⠛⠛⠋⠉",
    "",
    "⠀⠀⣴⣿⠿⠿⠿⠿⠿⠿⠿⠀⢸⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿",
    "⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⠀⠀⠀⠘⠛⠛⠛⠛⠛⣿⡆⠀⢸⣿⡇⠀⠀⠀⢸⣿⡇⠀⣴⣿⠛⠛⠛⠛⢻⣿⠀⠀⣴⣿⠛⠛⠛⠛⣿⣆",
    "⠀⠀⣿⣿⣀⣀⣀⣀⣀⣀⣀⠀⢸⣿⡀⠀⠀⣾⣿⠛⠛⠛⢛⣿⡇⠀⢸⣿⣇⠀⠀⠀⣸⣿⡇⠀⣿⣿⡀⠀⠀⢀⣸⣿⠀⠀⣿⣿⠛⠛⠛⠛⠛⠃",
    "⠀⠀⠈⠙⠛⠛⠛⠛⠛⠛⠛⠀⠈⠛⠛⠛⠀⠙⠛⠛⠛⠛⠛⠛⠃⠀⠀⠙⠛⠛⠛⠛⠛⠛⠃⠀⠈⠛⠛⠛⠛⠛⠙⠛⠀⠀⠈⠛⠛⠛⠛⠛⠛⠃",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⠤⠤⠄⠀⠀⠀⡄⠀⣤⠀⠀⠀⢀⣄⠀⠀⠀⠀⡤⠤⣀⠀⠀⢀⡤⠤⣀⠀⠀⢠⠀⣠⠀⡤",
    "⠀⠈⠉⠉⠉⠉⠉⠉⠁⠀⠨⠭⠭⠇⠀⠀⠀⠏⠉⠿⠀⠀⠠⠟⠚⠣⠀⠀⠀⠧⠤⠟⠀⠀⠘⠧⠤⠟⠀⠀⠀⠷⠃⠷⠁⠀⠀⠉⠉⠉⠉⠉⠉⠉",
)

BRAILLE_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

COMMAND_CATALOG: dict[str, str] = {
    "/status": "connection and model health",
    "/stats": "corpus and decay overview",
    "/query": "search stored reply memory",
    "/predict": "draft your likely reaction",
    "/ingest": "teach a situation and reply",
    "/pairs": "browse learned reaction pairs",
    "/live": "watcher state and activity",
    "/claude-shadow": "open the full-screen mirror",
    "/proxy": "manage Claude Code traffic",
    "/accept": "use and reinforce a ghost",
    "/reject": "reject and down-weight a ghost",
    "/edit": "approve after reshaping",
    "/fix": "store a corrected response",
    "/refresh": "reconnect to the daemon",
    "/clear": "clear the terminal",
    "/help": "show the command guide",
    "/exit": "disconnect Shadow Link",
    "/quit": "disconnect Shadow Link",
}

PROXY_COMMAND_CATALOG: dict[str, str] = {
    "status": "show listener and ownership",
    "start": "start the managed proxy",
    "stop": "stop the managed proxy",
    "view": "view recent proxy traffic",
    "setup": "start and create a launcher",
}


class PremiumCommandCompleter(Completer):
    """Slash-command palette with descriptions and nested proxy actions."""

    def __init__(self, commands: dict[str, str], nested: dict[str, dict[str, str]]):
        self.commands = commands
        self.nested = nested

    @staticmethod
    def _matches(options: dict[str, str], current: str, nested: bool = False):
        needle = current.lower()
        for option, description in options.items():
            if option.lower().startswith(needle):
                marker = "↳" if nested else "◆"
                yield Completion(
                    option,
                    start_position=-len(current),
                    display=FormattedText([
                        ("class:completion-kind", f"{marker} "),
                        ("class:completion-command", option),
                    ]),
                    display_meta=description,
                )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        if " " not in text:
            yield from self._matches(self.commands, text)
            return

        command, _, remainder = text.partition(" ")
        options = self.nested.get(command)
        if options is None:
            return
        # Once a complete action is followed by a space, the next token is an
        # argument (port/count), not another completion-menu level.
        if remainder.endswith(" ") and remainder.strip():
            return
        current = remainder.strip()
        if " " in current:
            return
        yield from self._matches(options, current, nested=True)


def print_formatted_text(value=""):
    """Render standalone console output with the Shadow Link palette."""
    _print_formatted_text(value, style=tui_style)


class DaemonAPIError(RuntimeError):
    """The daemon responded, but rejected the requested operation."""


_FEEDBACK_BUMPS = {"up": 0.25, "up_edit": 0.10, "down": -0.5, "down_edit": -0.5}


class ShadowLinkConsole:
    """Connected terminal console with daemon-first routing and local fallback."""

    def __init__(self):
        self.config = Config()
        self.db = Database(self.config.db_path)
        self.matcher = Engine(self.db)
        self.ingester = Ingester(
            self.db,
            provider=self.config.embedding_provider,
            url=self.config.embedding_api_url,
            model=self.config.embedding_model,
        )
        self.daemon_url = self._resolve_daemon_url()
        self.daemon_online = False
        self.daemon_status = "UNCHECKED"
        self.daemon_stats = None
        self.daemon_error = None
        self._last_daemon_check = 0.0
        self.last_prediction = None
        self._toolbar_stats: dict = {}
        self._toolbar_stats_at = 0.0

        self.commands = list(COMMAND_CATALOG)
        self.completer = PremiumCommandCompleter(
            COMMAND_CATALOG, {"/proxy": PROXY_COMMAND_CATALOG}
        )
        self.session = PromptSession(
            completer=self.completer,
            complete_style=CompleteStyle.COLUMN,
            complete_while_typing=True,
            reserve_space_for_menu=14,
            style=tui_style,
        )

    @staticmethod
    def _h(value) -> str:
        return html.escape(str(value), quote=False)

    @staticmethod
    def _short(value, limit=180) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _env_flag(name: str, default="0") -> bool:
        return os.getenv(name, default).lower() not in ("0", "", "false", "no", "off")

    @staticmethod
    def _resolve_daemon_url() -> str:
        configured = os.getenv("SHADOW_LINK_URL", "").strip()
        if configured:
            return configured.rstrip("/")
        host = os.getenv("SHADOW_LINK_HOST", "localhost")
        if host in ("0.0.0.0", "::", "[::]"):
            host = "localhost"
        port = os.getenv("SHADOW_LINK_PORT", "5000")
        return f"http://{host}:{port}"

    @staticmethod
    def _animation_frame(rate: float = 8.0) -> int:
        return int(time.monotonic() * rate)

    def _prompt_text(self):
        """Animated command prompt using the same Braille motion language."""
        pulse = BRAILLE_SPINNER[self._animation_frame() % len(BRAILLE_SPINNER)]
        return FormattedText([
            ("class:prompt-pulse", f"{pulse} "),
            ("class:prompt-logo", "shadow-link"),
            ("class:prompt-chevron", "  › "),
        ])

    def _bottom_toolbar_text(self):
        """Cached live telemetry; safe to redraw several times per second."""
        now = time.monotonic()
        if now - self._toolbar_stats_at >= 3.0:
            try:
                if self.daemon_online and isinstance(self.daemon_stats, dict):
                    self._toolbar_stats = self.daemon_stats
                else:
                    self._toolbar_stats = self.db.stats()
            except Exception:
                self._toolbar_stats = {}
            self._toolbar_stats_at = now

        online_style = "class:toolbar-ok" if self.daemon_online else "class:toolbar-warn"
        online_label = "DAEMON" if self.daemon_online else "LOCAL"
        prompt_count = self._toolbar_stats.get("total_prompts", "—")
        lm_label = "LM ON" if self._env_flag("SHADOW_LM_MODE", "1") else "RETRIEVAL"
        embed_label = "SEMANTIC" if self._env_flag("SHADOW_QUERY_EMBED") else "LEXICAL"
        pulse = "◆" if self._animation_frame(2.0) % 2 else "◇"
        return FormattedText([
            (online_style, f" {pulse} {online_label} "),
            ("class:toolbar", "│ "),
            ("class:toolbar-accent", f"{prompt_count} memories"),
            ("class:toolbar", " │ "),
            ("class:toolbar", lm_label),
            ("class:toolbar", " │ "),
            ("class:toolbar", embed_label),
            ("class:toolbar", " │ "),
            ("class:toolbar-key", "Tab"),
            ("class:toolbar", " commands  "),
        ])

    def _request_json(self, method, path, payload=None, timeout=2.0):
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.daemon_url}{path}", data=body, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise DaemonAPIError(detail) from exc
        if not isinstance(data, dict):
            raise RuntimeError("daemon returned a non-object JSON response")
        return data

    def _set_daemon_offline(self, error=None):
        self.daemon_online = False
        self.daemon_status = "OFFLINE — local fallback"
        self.daemon_stats = None
        self.daemon_error = str(error) if error else None

    def check_daemon(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_daemon_check < 3.0:
            return self.daemon_online
        self._last_daemon_check = now
        try:
            self.daemon_stats = self._request_json("GET", "/stats", timeout=0.8)
            self.daemon_online = True
            self.daemon_status = f"ONLINE — {self.daemon_url}"
            self.daemon_error = None
        except Exception as exc:
            self._set_daemon_offline(exc)
        return self.daemon_online

    def _remote(self, method, path, payload=None, timeout=2.0):
        if not self.check_daemon():
            return None
        try:
            return self._request_json(method, path, payload, timeout)
        except DaemonAPIError:
            raise
        except Exception as exc:
            self._set_daemon_offline(exc)
            print_formatted_text(HTML(
                f"<warn>Daemon connection lost ({self._h(exc)}); using the local database.</warn>"
            ))
            return None

    def _local_stats(self):
        from shadow_link.decay import BUCKET_DECAY_CLASS, _HALF_LIVES, effective_weight_for_row

        prompts = self.db.all_prompts()
        core = self.db.stats()
        buckets = []
        for name in ("request", "reminder", "rejection", "correction", "pivot"):
            rows = [row for row in prompts if row.get("bucket") == name]
            weights = [effective_weight_for_row(row) for row in rows]
            decay_class = BUCKET_DECAY_CLASS.get(name, "tactical")
            half_life = _HALF_LIVES.get(decay_class, 12.0)
            buckets.append({
                "name": name,
                "prompt_count": len(rows),
                "active_count": sum(weight > 0.5 for weight in weights),
                "average_weight": sum(weights) / len(weights) if weights else 1.0,
                "decay_class": getattr(decay_class, "value", str(decay_class)),
                "half_life": "Infinity" if half_life == float("inf") else f"{half_life}h",
            })
        return {
            "database_path": str(self.db.db_path),
            "total_prompts": core["total_prompts"],
            "total_edges": core["total_edges"],
            "jaccard_threshold": self.config.jaccard_threshold,
            "buckets": buckets,
        }

    def _stats(self):
        remote = self._remote("GET", "/stats")
        return (remote, "daemon") if remote is not None else (self._local_stats(), "local")

    def print_welcome(self):
        self.check_daemon(force=True)
        mode_style = "info" if self.daemon_online else "warn"
        mode_badge = "● DAEMON LINKED" if self.daemon_online else "◐ LOCAL FALLBACK"
        welcome_msg = (
            "\n"
            f"<brand>{TUI_BRAND_MARK}</brand>\n\n"
            "<header>SHADOW</header> <brand>LINK</brand> <alternate>v3</alternate>\n"
            "<alternate>THE SHADOW OF YOU. // LOCAL COGNITIVE MIRROR</alternate>\n"
            f"<{mode_style}>{mode_badge}</{mode_style}>\n"
            "<separator>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</separator>\n"
            f"Database: <info>{self._h(self.db.db_path)}</info>\n"
            f"Connection: <{mode_style}>{self._h(self.daemon_status)}</{mode_style}>\n"
            "Enter an AI situation to predict your reaction, or use <command>/help</command>.\n"
        )
        print_formatted_text(HTML(welcome_msg))

    def cmd_help(self, _args=""):
        print_formatted_text(HTML(
            "\n<header>CONNECTED COMMANDS</header>\n"
            "<alternate>────────────────────────────────────────────────────────────</alternate>\n"
            "<table-header>  CORE MEMORY</table-header>\n"
            "  <help-cmd>/status</help-cmd>              connection, models, watcher, and corpus state\n"
            "  <help-cmd>/stats</help-cmd>               corpus and decay statistics\n"
            "  <help-cmd>/query &lt;text&gt;</help-cmd>        search stored reply text\n"
            "  <help-cmd>/predict &lt;situation&gt;</help-cmd> predict your reaction to AI output\n"
            "  <help-cmd>/ingest &lt;situation&gt; | &lt;reply&gt;</help-cmd> learn a reaction pair\n"
            "  <help-cmd>/pairs [count]</help-cmd>       show recent learned reaction pairs\n"
            "\n<table-header>  LIVE MIRROR</table-header>\n"
            "  <help-cmd>/live</help-cmd>                watcher state, active ghost, and activity\n"
            "  <help-cmd>/claude-shadow</help-cmd>        Claude Shadow splash, session picker, transcript + ghost controls\n"
            "  <help-cmd>/proxy status|start|stop [port]</help-cmd> manage the Claude Code proxy\n"
            "  <help-cmd>/proxy view [count]</help-cmd> recent proxy traffic and token usage\n"
            "  <help-cmd>/proxy setup [port]</help-cmd> start proxy and create Claude launcher\n"
            "\n<table-header>  FEEDBACK LOOP</table-header>\n"
            "  <help-cmd>/accept [id]</help-cmd>         use the ghost as-is, reinforce (+0.25)\n"
            "  <help-cmd>/reject [id]</help-cmd>         do not use, down-weight (-0.5)\n"
            "  <help-cmd>/edit &lt;text&gt;</help-cmd>          good shape but reshape it; partial credit + store edit\n"
            "  <help-cmd>/fix &lt;text&gt;</help-cmd>           wrong; down-weight ghost + store your correction\n"
            "\n<table-header>  SYSTEM</table-header>\n"
            "  <help-cmd>/refresh</help-cmd>             reconnect to the configured daemon\n"
            "  <help-cmd>/clear</help-cmd>               clear the terminal\n"
            "  <help-cmd>/exit</help-cmd>                exit the console\n\n"
            "Plain text is the same as <help-cmd>/predict</help-cmd>."
        ))

    def cmd_status(self, _args=""):
        online = self.check_daemon(force=True)
        stats, source = self._stats()
        live = None
        if online:
            try:
                live = self._request_json("GET", "/live", timeout=1.0)
            except Exception:
                live = None
        try:
            if online:
                proxy = self._request_json("GET", "/proxy/status", timeout=1.0)
            else:
                from shadow_link.proxy_runtime import PROXY_RUNTIME
                proxy = PROXY_RUNTIME.status()
        except Exception:
            proxy = {"running": False, "proxy_url": "unavailable"}
        mode = "DAEMON" if online else "LOCAL FALLBACK"
        mode_style = "info" if online else "warn"
        embed_mode = "enabled" if self._env_flag("SHADOW_QUERY_EMBED") else "off on query path"
        lm_mode = "enabled" if self._env_flag("SHADOW_LM_MODE") else "retrieval only"
        watcher = "active" if live and live.get("active") else "inactive"
        transcript = (live or {}).get("transcript") or "none detected"
        print_formatted_text(HTML(
            "\n<header>SHADOW LINK CONNECTIONS</header>\n"
            f"  Mode:       <{mode_style}>{mode}</{mode_style}>\n"
            f"  API:        <info>{self._h(self.daemon_url)}</info>\n"
            f"  Database:   <info>{self._h(stats.get('database_path', self.db.db_path))}</info>\n"
            f"  Corpus:     <info>{stats.get('total_prompts', 0)}</info> prompts / "
            f"<info>{stats.get('total_edges', 0)}</info> edges ({source})\n"
            f"  Embeddings: <info>{self._h(self.config.embedding_provider)}</info> — {embed_mode}\n"
            f"  Embed API:  <alternate>{self._h(self.config.embedding_api_url)}</alternate>\n"
            f"  LM ghost:   <info>{lm_mode}</info>\n"
            f"  Chat API:   <alternate>{self._h(os.getenv('CHAT_SERVER_URL', 'not configured'))}</alternate>\n"
            f"  Proxy:      <info>{'running' if proxy.get('running') else 'stopped'}</info> — "
            f"{self._h(proxy.get('proxy_url', 'unavailable'))}\n"
            f"  Watcher:    <info>{watcher}</info> — {self._h(transcript)}"
        ))
        if not online and self.daemon_error:
            print_formatted_text(HTML(
                f"  <warn>Daemon detail: {self._h(self._short(self.daemon_error))}</warn>\n"
                "  Start it with <command>python -m shadow_link.cli daemon</command>."
            ))

    def cmd_stats(self, _args=""):
        try:
            stats, source = self._stats()
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Stats failed: {self._h(exc)}</error>"))
            return
        print_formatted_text(HTML(
            "\n<header>SHADOW LINK CORPUS</header>\n"
            f"  Source: <info>{source}</info>\n"
            f"  Database: <info>{self._h(stats.get('database_path', self.db.db_path))}</info>\n"
            f"  Prompts: <score>{stats.get('total_prompts', 0)}</score>  "
            f"Edges: <score>{stats.get('total_edges', 0)}</score>  "
            f"Threshold: <score>{float(stats.get('jaccard_threshold', 0.0)):.2f}</score>\n\n"
            "  <table-header>Bucket       Count  Active  Decay        Half-life  Weight</table-header>"
        ))
        for bucket in stats.get("buckets", []):
            avg = float(bucket.get("average_weight", 0.0))
            filled = max(0, min(8, round(avg / 5.0 * 8)))
            bar = "█" * filled + "░" * (8 - filled)
            print_formatted_text(HTML(
                f"  <table-cell>{self._h(bucket.get('name', 'unknown')).capitalize():12}</table-cell> "
                f"{int(bucket.get('prompt_count', 0)):5}  {int(bucket.get('active_count', 0)):6}  "
                f"{self._h(bucket.get('decay_class', '')):11}  "
                f"{self._h(bucket.get('half_life', '')):9}  <table-bar>{bar}</table-bar> {avg:.2f}"
            ))

    @staticmethod
    def _remote_match(item):
        prompt = item.get("prompt") or {}
        buckets = prompt.get("buckets") or {}
        return {
            "id": prompt.get("id"),
            "text": prompt.get("content", ""),
            "bucket": next(iter(buckets), "request"),
            "score": float(item.get("score", 0.0)),
            "jaccard": float(item.get("jaccard", 0.0)),
            "semantic": float(item.get("semantic", -1.0)),
        }

    def cmd_query(self, query):
        query = query.strip()
        if not query:
            print_formatted_text(HTML("<error>Usage: /query &lt;text&gt;</error>"))
            return
        try:
            data = self._remote("POST", "/query", {"text": query, "top_k": 5})
            if data is not None:
                matches = [self._remote_match(item) for item in data.get("matches", [])]
                source = "daemon"
            else:
                matches = [{
                    "id": match.prompt_id, "text": match.text, "bucket": match.bucket,
                    "score": match.combined_score, "jaccard": match.jaccard_sim,
                    "semantic": match.cosine_sim,
                } for match in self.matcher.match(query, top_k=5)]
                source = "local"
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Query failed: {self._h(exc)}</error>"))
            return
        if not matches:
            print_formatted_text(HTML("<warn>No stored replies matched above their thresholds.</warn>"))
            return
        print_formatted_text(HTML(
            f"\n<header>REPLY SEARCH</header> <alternate>({source})</alternate>\n"
            f"  Query: <info>{self._h(query)}</info>"
        ))
        for index, match in enumerate(matches, 1):
            semantic = match["semantic"]
            sem_text = f"{semantic:.2f}" if semantic >= 0 else "N/A"
            print_formatted_text(HTML(
                f"  <brand>[{index}]</brand> #{match['id']} "
                f"<score>{match['score']:.2f}</score> — "
                f"<info>{self._h(match['bucket']).upper()}</info> "
                f"<alternate>(J {match['jaccard']:.2f} / S {sem_text})</alternate>\n"
                f"      {self._h(self._short(match['text']))}"
            ))

    def _predict(self, situation):
        data = self._remote("POST", "/predict", {"question": situation}, timeout=35.0)
        if data is not None:
            return data.get("ghost"), data.get("alternates", []), "daemon"
        from shadow_link.live import build_prediction
        ghost, alternates = build_prediction(str(self.db.db_path), situation, top_k=5)
        return ghost, alternates, "local"

    def cmd_predict(self, situation):
        situation = situation.strip()
        if not situation:
            print_formatted_text(HTML("<error>Usage: /predict &lt;AI situation or output&gt;</error>"))
            return
        try:
            ghost, alternates, source = self._predict(situation)
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Prediction failed: {self._h(exc)}</error>"))
            return
        if not ghost:
            self.last_prediction = None
            print_formatted_text(HTML(
                "<warn>No situation-matched reaction has enough signal yet. "
                "Teach one with /ingest.</warn>"
            ))
            return
        ghost = dict(ghost)
        ghost.setdefault("situation", situation)
        self.last_prediction = {
            "ghost": ghost, "alternates": list(alternates), "situation": situation,
        }
        prompt_id = ghost.get("id")
        id_text = f"#{prompt_id}" if isinstance(prompt_id, int) else "generated"
        print_formatted_text(HTML(
            f"\n<header>PREDICTED REACTION</header> <alternate>({source})</alternate>\n"
            f"  <ghost>{self._h(ghost.get('text', ''))}</ghost>\n"
            f"  <score>{float(ghost.get('confidence', 0.0)):.2f}</score> confidence · "
            f"<info>{self._h(ghost.get('bucket', 'unknown')).upper()}</info> · "
            f"<alternate>{self._h(id_text)} / {self._h(ghost.get('source', 'retrieved'))}</alternate>\n"
            "  <command>/accept</command> · <command>/reject</command> · <command>/edit &lt;text&gt;</command> · <command>/fix &lt;text&gt;</command>"
        ))
        if alternates:
            print_formatted_text(HTML("  <header>Alternates</header>"))
            for alt in alternates[:4]:
                print_formatted_text(HTML(
                    f"    <alternate>#{alt.get('id', '—')} "
                    f"[{float(alt.get('confidence', 0.0)):.2f}] "
                    f"{self._h(self._short(alt.get('text', '')))}</alternate>"
                ))

    def cmd_ingest(self, arg_str):
        if "|" not in arg_str:
            print_formatted_text(HTML(
                "<error>Usage: /ingest &lt;AI situation&gt; | &lt;your reply&gt;</error>"
            ))
            return
        situation, reply = (part.strip() for part in arg_str.split("|", 1))
        if not situation or not reply:
            print_formatted_text(HTML("<error>Situation and reply must both contain text.</error>"))
            return
        request_id = f"tui-{uuid.uuid4().hex[:12]}"
        try:
            data = self._remote("POST", "/ingest", {
                "question": situation, "response": reply, "request_id": request_id,
            })
            if data is not None:
                prompt_id, source = data.get("prompt_id"), "daemon"
            else:
                prompt_id = self.ingester.ingest_interaction(situation, reply, request_id)
                source = "local"
            print_formatted_text(HTML(
                f"<info>Learned reaction #{prompt_id} through {source}.</info> "
                f"Buckets: <alternate>{self._h(', '.join(classify_multi(reply)))}</alternate>"
            ))
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Ingestion failed: {self._h(exc)}</error>"))

    def cmd_pairs(self, arg_str=""):
        try:
            limit = int(arg_str.strip() or "5")
            limit = max(1, min(limit, 20))
        except ValueError:
            print_formatted_text(HTML("<error>Usage: /pairs [count]</error>"))
            return
        query = urllib.parse.urlencode({"limit": limit})
        data = self._remote("GET", f"/pairs?{query}")
        if data is not None:
            pairs, total, source = data.get("pairs", []), data.get("total", 0), "daemon"
        else:
            pairs = []
            rows = list(reversed(self.db.all_prompts()))
            for row in rows[:limit]:
                try:
                    meta = json.loads(row.get("metadata") or "{}")
                except Exception:
                    meta = {}
                pairs.append({
                    "id": row["id"], "reply": row["text"], "bucket": row["bucket"],
                    "situation": meta.get("agent_output") or meta.get("question") or "",
                    "source": meta.get("source", "unknown"), "weight": row.get("weight", 1.0),
                })
            total, source = len(rows), "local"
        print_formatted_text(HTML(
            f"\n<header>RECENT REACTION PAIRS</header> "
            f"<alternate>({len(pairs)} of {total}, {source})</alternate>"
        ))
        if not pairs:
            print_formatted_text(HTML("  <warn>No learned pairs yet.</warn>"))
            return
        for pair in pairs:
            print_formatted_text(HTML(
                f"  <brand>#{pair.get('id')}</brand> "
                f"<info>{self._h(pair.get('bucket', '')).upper()}</info> "
                f"<alternate>{self._h(pair.get('source', 'unknown'))} / "
                f"w={float(pair.get('weight', 1.0)):.2f}</alternate>\n"
                f"    AI:  {self._h(self._short(pair.get('situation') or '—', 120))}\n"
                f"    You: <ghost>{self._h(self._short(pair.get('reply', ''), 120))}</ghost>"
            ))

    def cmd_live(self, _args=""):
        if not self.check_daemon(force=True):
            print_formatted_text(HTML(
                "Daemon is <error>OFFLINE</error>; the live transcript watcher runs inside it.\n"
                "Start it in another terminal with "
                "<command>python -m shadow_link.cli daemon</command>."
            ))
            return
        try:
            data = self._request_json("GET", "/live", timeout=1.5)
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Live state failed: {self._h(exc)}</error>"))
            return
        if not data.get("active"):
            print_formatted_text(HTML("<warn>Daemon is online, but its live watcher is inactive.</warn>"))
            return
        print_formatted_text(HTML(
            "\n<header>LIVE TRANSCRIPT WATCHER</header>\n"
            f"  Transcript: <info>{self._h(data.get('transcript') or 'none detected')}</info>\n"
            f"  Learned: <score>{data.get('learned_this_session', 0)}</score> this session · "
            f"Corpus: <score>{data.get('corpus_total', 0)}</score>"
        ))
        ghost = data.get("ghost")
        if ghost:
            print_formatted_text(HTML(
                f"  Current ghost: <ghost>{self._h(self._short(ghost.get('text', '')))}</ghost>"
            ))
        activity = data.get("activity", [])[:10]
        if not activity:
            print_formatted_text(HTML("  <alternate>No activity recorded yet.</alternate>"))
            return
        print_formatted_text(HTML("  <header>Recent activity</header>"))
        for item in activity:
            kind = str(item.get("kind", "info")).upper()
            ts = time.strftime("%H:%M:%S", time.localtime(item.get("ts", time.time())))
            style = "warn" if kind == "CLAUDE" else "info" if kind == "LEARN" else "brand"
            print_formatted_text(HTML(
                f"    {ts} <{style}>{self._h(kind)}</{style}> "
                f"{self._h(self._short(item.get('text', '')))}"
            ))

    def _proxy_action(self, action, port):
        source = "daemon"
        try:
            if action == "status":
                data = self._remote("GET", "/proxy/status")
            elif action == "start":
                data = self._remote("POST", "/proxy/start", {"port": port}, timeout=5.0)
            else:
                data = self._remote("POST", "/proxy/stop", {}, timeout=5.0)
        except DaemonAPIError as exc:
            detail = str(exc).lower()
            if "not found" not in detail and "endpoint" not in detail:
                raise
            data = None
            print_formatted_text(HTML(
                "<warn>The running daemon predates proxy controls; using TUI-local management. "
                "Restart the daemon for persistent proxy ownership.</warn>"
            ))
        if data is not None:
            return data, source
        source = "TUI process"
        from shadow_link.proxy_runtime import PROXY_RUNTIME
        if action == "start":
            data = PROXY_RUNTIME.start(port)
        elif action == "stop":
            data = PROXY_RUNTIME.stop()
        else:
            data = PROXY_RUNTIME.status(port)
        return data, source

    def _render_proxy_status(self, data, source, port):
        state = "RUNNING" if data.get("running") else "STOPPED"
        state_style = "info" if data.get("running") else "warn"
        owner = source if data.get("managed") else (
            "external process" if data.get("listening") else "none"
        )
        proxy_url = data.get("proxy_url", f"http://127.0.0.1:{port}")
        print_formatted_text(HTML(
            "\n<header>CLAUDE CODE PROXY</header>\n"
            f"  State:     <{state_style}>{state}</{state_style}>\n"
            f"  URL:       <info>{self._h(proxy_url)}</info>\n"
            f"  Owner:     <alternate>{self._h(owner)}</alternate>\n"
            f"  PID:       <alternate>{self._h(data.get('pid') or '—')}</alternate>\n"
            f"  mitmdump:  <info>{'available' if data.get('available') else 'not found'}</info>\n"
            f"  Detail:    {self._h(data.get('message', 'status refreshed'))}\n\n"
            "  <header>PowerShell environment</header>\n"
            f"  <command>$env:HTTPS_PROXY='{self._h(proxy_url)}'</command>\n"
            "  <command>$env:NODE_EXTRA_CA_CERTS=\"$HOME\\.mitmproxy\\mitmproxy-ca-cert.pem\"</command>\n"
            "  <command>$env:NO_PROXY='localhost,127.0.0.1,::1'</command>"
        ))

    def _write_proxy_launcher(self, port):
        path = Path(self.config.db_path).parent / "claude-proxy.ps1"
        script = f'''param(
    [int]$Port = {port},
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"
$cert = Join-Path $HOME ".mitmproxy\\mitmproxy-ca-cert.pem"
if (-not (Test-Path -LiteralPath $cert)) {{
    throw "mitmproxy certificate not found. Run /proxy setup once while the proxy is available."
}}

$env:HTTPS_PROXY = "http://127.0.0.1:$Port"
$env:NODE_EXTRA_CA_CERTS = $cert
$env:NO_PROXY = "localhost,127.0.0.1,::1"

& claude @ClaudeArgs
exit $LASTEXITCODE
'''
        path.write_text(script, encoding="utf-8")
        return path

    def _proxy_view(self, count):
        query = urllib.parse.urlencode({"limit": count})
        try:
            data = self._remote("GET", f"/proxy/audit?{query}")
        except DaemonAPIError as exc:
            detail = str(exc).lower()
            if "not found" not in detail and "endpoint" not in detail:
                raise
            data = None
        source = "daemon"
        if data is None:
            from shadow_link.proxy_runtime import read_proxy_audit
            data, source = read_proxy_audit(count), "local"
        events = data.get("events", [])
        print_formatted_text(HTML(
            "\n<header>PROXY TRAFFIC</header> "
            f"<alternate>({source}, newest first)</alternate>\n"
            f"  Audit: <info>{self._h(data.get('audit_dir', 'unknown'))}</info>"
        ))
        if not events:
            print_formatted_text(HTML(
                "  <warn>No Claude Code proxy traffic has been recorded yet.</warn>"
            ))
            return
        for event in events:
            timestamp = str(event.get("timestamp", ""))[11:19] or "--:--:--"
            phase = str(event.get("phase", "event")).upper()
            if phase == "REQUEST":
                original = int(event.get("original_bytes", 0))
                optimized = int(event.get("optimized_bytes", 0))
                saved = original - optimized
                detail = f"{original:,} → {optimized:,} bytes ({saved:+,} saved)"
                style = "brand"
            else:
                usage = event.get("usage") or {}
                status = event.get("status_code", "—")
                input_tokens = usage.get("input_tokens", "—")
                output_tokens = usage.get("output_tokens", "—")
                cache = usage.get("cache_read_input_tokens", "—")
                detail = f"HTTP {status} · in {input_tokens} · out {output_tokens} · cache {cache}"
                style = "info"
            print_formatted_text(HTML(
                f"  {self._h(timestamp)} <{style}>{self._h(phase):8}</{style}> "
                f"{self._h(detail)}"
            ))

    def cmd_proxy(self, arg_str=""):
        parts = arg_str.lower().split()
        action = parts[0] if parts else "status"
        action = {"veiw": "view"}.get(action, action)
        if action not in ("status", "start", "stop", "view", "setup"):
            print_formatted_text(HTML(
                "<error>Usage: /proxy [status|start|stop|view|setup] [port|count]</error>"
            ))
            return
        if action == "view":
            try:
                count = max(1, min(int(parts[1]) if len(parts) > 1 else 10, 50))
                self._proxy_view(count)
            except Exception as exc:
                print_formatted_text(HTML(f"<error>Proxy view failed: {self._h(exc)}</error>"))
            return
        try:
            port = int(parts[1]) if len(parts) > 1 else int(
                os.getenv("SHADOW_LINK_PROXY_PORT", "8080")
            )
            if not 1 <= port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            effective_action = "start" if action == "setup" else action
            data, source = self._proxy_action(effective_action, port)
            self._render_proxy_status(data, source, port)
            if action == "setup":
                launcher = self._write_proxy_launcher(port)
                print_formatted_text(HTML(
                    "\n  <info>Setup complete.</info> Claude launcher created at:\n"
                    f"  <command>{self._h(launcher)}</command>\n"
                    f"  Run: <command>&amp; '{self._h(launcher)}'</command>\n"
                    "  <alternate>Keep the daemon running while Claude Code uses the proxy.</alternate>"
                ))
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Proxy {action} failed: {self._h(exc)}</error>"))

    def _feedback_target(self, arg_str, vote):
        value = arg_str.strip()
        # Edit votes: arg_str IS the edited text (not an id). Require last_prediction.
        if vote in ("up_edit", "down_edit"):
            if not value:
                raise ValueError(f"Usage: /{ 'edit' if vote == 'up_edit' else 'fix' } <your edited text>")
            if not self.last_prediction:
                raise ValueError("no prediction selected; run /predict first")
            ghost = self.last_prediction["ghost"]
            situation = self.last_prediction["situation"]
            return ghost, situation, value  # third value is edited_text
        # Binary votes: arg_str is optional id, behaves like before
        if value:
            try:
                prompt_id = int(value.lstrip("#"))
            except ValueError:
                raise ValueError("ID must be an integer")
            return {"id": prompt_id, "text": "", "grounding_ids": []}, "", ""
        if not self.last_prediction:
            raise ValueError("no prediction selected; run /predict first or provide an ID")
        return self.last_prediction["ghost"], self.last_prediction["situation"], ""

    def _local_feedback(self, ghost, situation, vote, edited_text=""):
        bump = _FEEDBACK_BUMPS.get(vote, 0.0)
        prompt_id = ghost.get("id")
        stored_id = None
        new_weight = None
        if isinstance(prompt_id, int) and bump != 0.0:
            try:
                new_weight = self.db.reinforce(prompt_id, bump=bump)
            except ValueError:
                pass
        else:
            text = str(ghost.get("text") or "").strip()
            if vote == "up" and text:
                buckets = classify_multi(text)
                stored_id = self.db.add_prompt(
                    text=text, bucket=buckets[0],
                    metadata={"buckets": buckets, "source": "approved_generation",
                              "agent_output": situation[:1000]},
                )
            for grounding_id in ghost.get("grounding_ids", []):
                if isinstance(grounding_id, int):
                    try:
                        self.db.reinforce(grounding_id, bump=bump)
                    except ValueError:
                        pass
        # Edit branches: store user's edited text as a new reaction
        if vote in ("up_edit", "down_edit") and edited_text.strip():
            from shadow_link.live import is_real_reply
            if is_real_reply(edited_text):
                buckets = classify_multi(edited_text)
                source_label = "approved_edit" if vote == "up_edit" else "correction"
                weight = 1.0 if vote == "up_edit" else 1.25
                stored_id = self.db.add_prompt(
                    text=edited_text, bucket=buckets[0],
                    weight=weight,
                    metadata={"buckets": buckets, "source": source_label,
                              "agent_output": situation[:1000]},
                )
                pending = Path.home() / ".claude" / "shadow_pending.txt"
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_text(edited_text, encoding="utf-8")
        return {"ok": True, "stored_id": stored_id, "new_weight": new_weight}

    def _feedback(self, arg_str, vote):
        try:
            ghost, situation, edited_text = self._feedback_target(arg_str, vote)
            payload = {
                "id": ghost.get("id"), "vote": vote, "text": ghost.get("text", ""),
                "situation": situation, "grounding_ids": ghost.get("grounding_ids", []),
                "edited_text": edited_text,
            }
            data = self._remote("POST", "/feedback", payload)
            source = "daemon"
            if data is None:
                data, source = self._local_feedback(ghost, situation, vote, edited_text), "local"
            action_map = {"up": "Reinforced", "down": "Down-weighted",
                          "up_edit": "Stored edit", "down_edit": "Stored correction"}
            action = action_map.get(vote, "Feedback")
            detail = data.get("stored_id") or ghost.get("id") or "grounding set"
            print_formatted_text(HTML(
                f"<info>{action} {self._h(detail)} through {source}.</info>"
            ))
        except Exception as exc:
            print_formatted_text(HTML(f"<error>Feedback failed: {self._h(exc)}</error>"))

    def cmd_accept(self, arg_str=""):
        self._feedback(arg_str, "up")

    def cmd_reject(self, arg_str=""):
        self._feedback(arg_str, "down")

    def cmd_edit(self, arg_str=""):
        self._feedback(arg_str, "up_edit")

    def cmd_fix(self, arg_str=""):
        self._feedback(arg_str, "down_edit")

    def cmd_claude_shadow(self, _arg_str=""):
        """Enter the full-screen Claude Shadow surface. Q exits back here."""
        try:
            view = WatchView(self)
            status = view.run()
            print_formatted_text(HTML(f"<info>{self._h(status)}</info>"))
        except Exception as exc:
            print_formatted_text(HTML(
                f"<error>claude-shadow failed: {self._h(exc)}</error>"
            ))

    def cmd_refresh(self, _args=""):
        online = self.check_daemon(force=True)
        style = "info" if online else "warn"
        print_formatted_text(HTML(
            f"Connection: <{style}>{self._h(self.daemon_status)}</{style}>"
        ))

    def cmd_clear(self, _args=""):
        os.system("cls" if os.name == "nt" else "clear")

    def run(self):
        self.print_welcome()
        handlers = {
            "/help": self.cmd_help, "/status": self.cmd_status,
            "/stats": self.cmd_stats, "/query": self.cmd_query,
            "/predict": self.cmd_predict, "/ingest": self.cmd_ingest,
            "/pairs": self.cmd_pairs, "/live": self.cmd_live,
            "/claude-shadow": self.cmd_claude_shadow,
            "/proxy": self.cmd_proxy, "/poxy": self.cmd_proxy,
            "/accept": self.cmd_accept, "/reject": self.cmd_reject,
            "/edit": self.cmd_edit, "/fix": self.cmd_fix,
            "/refresh": self.cmd_refresh, "/clear": self.cmd_clear,
        }
        # patch_stdout lets the background /claude-shadow thread print ghosts
        # without garbling the active prompt line.
        with patch_stdout(raw=True):
            while True:
                try:
                    text = self.session.prompt(
                        self._prompt_text,
                        bottom_toolbar=self._bottom_toolbar_text,
                        refresh_interval=0.12,
                    ).strip()
                    if not text:
                        continue
                    if not text.startswith("/"):
                        self.cmd_predict(text)
                        continue
                    command, _, args = text.partition(" ")
                    command = command.lower()
                    if command in ("/exit", "/quit"):
                        break
                    handler = handlers.get(command)
                    if handler is None:
                        print_formatted_text(HTML(
                            f"<error>Unknown command: {self._h(command)}</error> — use <command>/help</command>."
                        ))
                    else:
                        handler(args)
                except KeyboardInterrupt:
                    print_formatted_text("")
                except EOFError:
                    break
        print_formatted_text(HTML("\n<brand>Shadow Link disconnected.</brand>"))

class WatchView:
    """Full-screen Claude Shadow surface.

    Replaces the bottom-of-screen text-input area with the four-signal action
    bar; the GHOST hovers in a framed panel directly above it. The transcript
    of the live Claude Code session scrolls in the top half.
    """

    def __init__(self, console: "ShadowLinkConsole"):
        self.console = console
        self.watcher = None
        self.transcript_path = ""
        self.turns: list[tuple[float, str, str]] = []
        self.ghost: dict | None = None
        self.alternates: list[dict] = []
        self.pick_index: int = 0  # 0 = top ghost; 1..N = alternates
        self._last_turn_signature: str = ""
        self.proxy_running: bool = False
        self.proxy_url: str = ""
        self.state_source: str = "—"  # "daemon" or "local"
        self.viewer_mode: bool = False  # [V] toggles transcript ↔ full-claude
        self.picker_mode: bool = False  # [s] opens session selector overlay
        self.picker_items: list[dict] = []
        self.picker_index: int = 0
        self.first_pick: bool = True   # True until the operator chooses a session
        self.splash_mode: bool = True  # Enter advances to the initial picker
        self._splash_frame: int = 0
        self.ghost_updated_at: float = 0.0
        self.edit_mode: str | None = None
        self.status_msg: str = ""
        self.status_history: deque = deque(maxlen=3)
        self.feedback_count: int = 0
        self.edit_buffer = Buffer(multiline=False)
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._animation_thread: threading.Thread | None = None
        self._build_layout()
        self._build_keys()
        self.app: Application = Application(
            layout=Layout(self.root, focused_element=self.idle_focus),
            key_bindings=self.kb,
            full_screen=True,
            mouse_support=False,
            style=tui_style,
        )

    @staticmethod
    def _short(text, limit) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _splash_text(self):
        """Render the supplied Claude Shadow artwork as centered Braille."""
        try:
            terminal_rows = get_app().output.get_size().rows
        except Exception:
            terminal_rows = 40
        compact = terminal_rows < 36
        raster = CLAUDE_SHADOW_BRAILLE_COMPACT if compact else CLAUDE_SHADOW_BRAILLE
        logo_end, title_end = (13, 18) if compact else (19, 26)
        width = max(len(row) for row in raster)
        scan_row = (self._splash_frame % (len(raster) + 6)) - 3
        parts: list[tuple[str, str]] = []
        if not compact:
            parts.append(("class:splash-bg", "\n"))
        for index, row in enumerate(raster):
            if index < logo_end:
                style = "class:splash-mid"
            elif index < title_end:
                style = "class:splash-glow"
            else:
                style = "class:splash-dim"
            if row.strip("⠀ ") and abs(index - scan_row) <= 1:
                style = "class:splash-scan"
            elif row.strip("⠀ ") and abs(index - scan_row) <= 3:
                style = "class:splash-glow"
            parts.append((style, row.ljust(width) + "\n"))
        spinner = BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]
        parts.extend((
            ("class:splash-bg", "\n"),
            ("class:splash-prompt", f"{spinner}  PRESS ENTER TO CONTINUE  {spinner}"),
        ))
        return FormattedText(parts)

    def _animate_ui(self):
        """Drive lightweight visual motion without touching daemon polling."""
        while not self._stop.wait(0.10 if self.splash_mode else 0.25):
            self._splash_frame = (self._splash_frame + 1) % 10_000
            try:
                self.app.invalidate()
            except Exception:
                return

    def _continue_from_splash(self):
        """Advance from the brand gate to the Claude session picker."""
        self.splash_mode = False
        self._open_session_picker()

    def _header_text(self):
        if self.transcript_path:
            p = Path(self.transcript_path)
            session = p.stem
            # Keep the session id intact; collapse the project segment.
            project = p.parent.name
            if len(project) > 24:
                project = "…" + project[-24:]
            watching = f"{project}/{session}.jsonl"
        else:
            watching = "no transcript discovered"
        proxy_label = "ON" if self.proxy_running else "OFF"
        proxy_style = "info" if self.proxy_running else "warn"
        source_style = "info" if self.state_source == "daemon" else "warn"
        link_label = "MIRRORING" if self.transcript_path else "STANDBY"
        link_style = "info" if self.transcript_path else "alternate"
        pulse = BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]
        feedback = f"  fb {self.feedback_count}" if self.feedback_count else ""
        mode = " · VIEWING" if self.viewer_mode else ""
        return HTML(
            f"<brand>{pulse} CLAUDE SHADOW</brand>  "
            f"<info>{html.escape(watching)}</info>  "
            f"<alternate>link:</alternate><{link_style}>{link_label}</{link_style}>  "
            f"<alternate>src:</alternate><{source_style}>{html.escape(self.state_source)}</{source_style}>  "
            f"<alternate>proxy:</alternate><{proxy_style}>{proxy_label}</{proxy_style}>"
            f"<alternate>{html.escape(feedback)}{html.escape(mode)}</alternate>"
        )

    def _picker_text(self):
        welcome: list[tuple[str, str]] = []
        if self.first_pick:
            welcome = [
                ("class:brand", "\n  CLAUDE SHADOW\n"),
                ("class:alternate", "  Choose which Claude Code session to mirror.\n"),
                ("class:alternate", "  Shadow Link will draft your likely reply on each Claude turn.\n\n"),
            ]
        if not self.picker_items:
            return FormattedText(welcome + [
                ("class:warn", "  No Claude Code transcripts found under ~/.claude/projects\n"),
                ("class:alternate", "  Start a Claude Code session, then re-enter /claude-shadow.\n\n"),
                ("class:alternate", "  [Esc] exit"),
            ])
        verb = "Enter watch" if self.first_pick else "Enter switch"
        cancel = "Esc exit" if self.first_pick else "Esc cancel"
        lines: list[tuple[str, str]] = welcome + [
            ("class:header", f"PICK A CLAUDE CODE SESSION  [{len(self.picker_items)} FOUND]\n"),
            ("class:alternate", f"  ↑/↓ navigate · 1-9 jump · {verb} · {cancel}\n\n"),
        ]
        now = time.time()
        for i, item in enumerate(self.picker_items[:20]):
            mt = float(item.get("mtime", 0))
            delta = max(0, int(now - mt))
            if delta < 60:
                age = f"{delta}s ago"
            elif delta < 3600:
                age = f"{delta // 60}m ago"
            elif delta < 86400:
                age = f"{delta // 3600}h ago"
            else:
                age = f"{delta // 86400}d ago"
            project = item.get("project", "")
            if len(project) > 32:
                project = "…" + project[-32:]
            sid = item.get("session_id", "")
            preview = item.get("last_user") or item.get("last_assistant") or "(empty)"
            preview = self._short(preview, 100)
            marker = (
                f"{BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]} "
                if i == self.picker_index else "  "
            )
            row_style = "class:ghost" if i == self.picker_index else "class:table-cell"
            number = f"[{i+1}]" if i < 9 else "   "
            lines.append((row_style, f"{marker}{number} {project}/{sid[:8]}…  "))
            lines.append(("class:alternate", f"{age}\n"))
            lines.append(("class:alternate", f"        {preview}\n"))
        return FormattedText(lines)

    def _last_claude_turn(self) -> tuple[float, str] | None:
        for ts, role, text in reversed(self.turns):
            if role == "assistant":
                return (ts, text)
        return None

    def _transcript_text(self):
        if self.picker_mode:
            return self._picker_text()
        if self.viewer_mode:
            last = self._last_claude_turn()
            if not last:
                return HTML(
                    "  <warn>No claude turn to view yet.</warn>\n"
                    "  <alternate>Press [V] again to return to the scrolling transcript.</alternate>"
                )
            ts, text = last
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            return FormattedText([
                ("class:header", f"FULL CLAUDE TURN  [{stamp}]\n\n"),
                ("class:warn", text + "\n"),
            ])
        if not self.turns:
            pulse = BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]
            return HTML(
                f"  <info>{pulse}</info> <warn>Waiting for Claude Code turns…</warn>\n"
                "  <alternate>Start a Claude session; the transcript will scroll here as it grows.</alternate>"
            )
        lines: list[tuple[str, str]] = []
        # Show the tail; terminal scrollback handles the rest.
        for ts, role, text in self.turns[-40:]:
            stamp = time.strftime("%H:%M", time.localtime(ts))
            if role == "user":
                lines.append(("class:command", f"[{stamp}] you    "))
                lines.append(("class:table-cell", self._short(text, 240) + "\n"))
            else:
                lines.append(("class:info", f"[{stamp}] claude "))
                lines.append(("class:warn", self._short(text, 240) + "\n"))
        return FormattedText(lines)

    def _picks(self) -> list[dict]:
        return ([self.ghost] if self.ghost else []) + list(self.alternates or [])

    def _current_pick(self) -> dict | None:
        picks = self._picks()
        if not picks:
            return None
        idx = self.pick_index % len(picks)
        return picks[idx]

    def _ghost_text(self):
        picks = self._picks()
        if not picks:
            pulse = BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]
            return HTML(
                f"  <info>{pulse}</info> <warn>(waiting — no ghost yet)</warn>\n"
                "  <alternate>Ghost appears the moment Claude finishes its next turn.</alternate>"
            )
        g = self._current_pick() or picks[0]
        confidence = max(0.0, min(1.0, float(g.get("confidence", 0))))
        filled = round(confidence * 10)
        confidence_bar = "█" * filled + "░" * (10 - filled)
        prompt_id = g.get("id")
        id_text = f"#{prompt_id}" if isinstance(prompt_id, int) else "generated"
        pos = (self.pick_index % len(picks)) + 1
        position = f"{pos}/{len(picks)}"
        age = ""
        if self.ghost_updated_at:
            delta = max(0, int(time.time() - self.ghost_updated_at))
            if delta < 60:
                age = f" · {delta}s ago"
            elif delta < 3600:
                age = f" · {delta // 60}m ago"
            else:
                age = " · stale"
        return HTML(
            f"  <ghost>{html.escape(str(g.get('text', '')))}</ghost>\n"
            f"  <score>{confidence_bar}</score> <score>{confidence:.2f}</score> conf · "
            f"<info>{html.escape(str(g.get('bucket', 'unknown')).upper())}</info> · "
            f"<alternate>{html.escape(id_text)} / {html.escape(str(g.get('source', '')))} / "
            f"tier {html.escape(str(g.get('tier', '—')))} · {position}{html.escape(age)}</alternate>"
        )

    def _action_text(self):
        if self.edit_mode:
            label = "edit (partial approval)" if self.edit_mode == "edit" else "fix (correction)"
            return HTML(
                f"  <info>{html.escape(label.upper())}:</info> "
                f"<alternate>type below · Enter submit · Esc cancel</alternate>"
            )
        if self.picker_mode:
            return HTML(
                "  <info>SESSION PICKER:</info> "
                "<alternate>↑/↓ move · 1-9 jump · Enter switch · Esc cancel</alternate>"
            )
        status = f"   <alternate>· {html.escape(self.status_msg)}</alternate>" if self.status_msg else ""
        pulse = BRAILLE_SPINNER[self._splash_frame % len(BRAILLE_SPINNER)]
        proxy_key = "<info>[P]</info>roxy-stop" if self.proxy_running else "<info>[P]</info>roxy-start"
        view_key = "<info>[V]</info>iewer-off" if self.viewer_mode else "<info>[V]</info>iew-full"
        return HTML(
            f"  <ghost>{pulse}</ghost>  <info>[a]</info>ccept  "
            "<info>[r]</info>eject  "
            "<info>[e]</info>dit  "
            "<info>[f]</info>ix  "
            "<info>[n]</info>ext  "
            "<info>[p]</info>rev  "
            "<info>[s]</info>ession  "
            f"{view_key}  "
            f"{proxy_key}  "
            "<info>[q]</info>uit" + status
        )

    def _status_history_text(self):
        if not self.status_history:
            return HTML(" ")
        # Show previous status entries dimly (current is in action bar).
        prev = list(self.status_history)[:-1] if self.status_msg else list(self.status_history)
        if not prev:
            return HTML(" ")
        body = "  ·  ".join(html.escape(self._short(s, 70)) for s in reversed(prev))
        return HTML(f"  <alternate>past: {body}</alternate>")

    def _push_status(self, msg: str):
        msg = (msg or "").strip()
        if not msg:
            return
        self.status_msg = msg
        self.status_history.append(msg)

    def _build_layout(self):
        show_splash = Condition(lambda: self.splash_mode)
        show_workspace = Condition(lambda: not self.splash_mode)
        splash = Window(
            FormattedTextControl(self._splash_text),
            align=WindowAlign.CENTER,
            wrap_lines=False,
            style="class:splash-bg",
        )
        header = Window(
            FormattedTextControl(self._header_text),
            height=1,
            style="class:header reverse",
        )
        separator = Window(height=1, char="─", style="class:separator")
        transcript = Window(
            FormattedTextControl(self._transcript_text),
            wrap_lines=True,
        )
        ghost_window = Window(
            FormattedTextControl(self._ghost_text),
            height=Dimension(min=3, max=6),
            wrap_lines=True,
        )
        ghost_frame = Frame(ghost_window, title="GHOST", style="class:ghost")
        action = Window(
            FormattedTextControl(self._action_text),
            height=1,
            style="class:command reverse",
        )
        status_history = Window(
            FormattedTextControl(self._status_history_text),
            height=1,
        )
        edit_input = ConditionalContainer(
            Window(
                BufferControl(buffer=self.edit_buffer),
                height=1,
                style="class:ghost",
            ),
            filter=Condition(lambda: self.edit_mode is not None),
        )
        # A 0-line idle focus target keeps key bindings alive when the edit
        # input is hidden; we focus it on exit from edit mode.
        self.idle_focus = Window(height=0, width=0, always_hide_cursor=True)
        workspace = HSplit([
            header,
            separator,
            transcript,
            ghost_frame,
            action,
            status_history,
            edit_input,
            self.idle_focus,
        ])
        self.root = HSplit([
            ConditionalContainer(splash, filter=show_splash),
            ConditionalContainer(workspace, filter=show_workspace),
        ])

    def _build_keys(self):
        kb = KeyBindings()
        is_splash = Condition(lambda: self.splash_mode)
        is_editing = Condition(lambda: self.edit_mode is not None)
        is_picking = Condition(lambda: self.picker_mode)
        is_idle = Condition(
            lambda: not self.splash_mode and self.edit_mode is None and not self.picker_mode
        )

        @kb.add("enter", filter=is_splash)
        def _splash_continue(event):
            self._continue_from_splash()
            event.app.invalidate()

        @kb.add("q", filter=is_idle)
        def _quit(event):
            event.app.exit()

        @kb.add("c-c")
        def _ctrl_c(event):
            if self.edit_mode is not None:
                self._exit_edit_mode(event.app)
            else:
                event.app.exit()

        @kb.add("n", filter=is_idle)
        def _next(event):
            picks = self._picks()
            if not picks:
                self._push_status("no picks to cycle")
                return
            self.pick_index = (self.pick_index + 1) % len(picks)
            self._push_status(f"pick {self.pick_index + 1}/{len(picks)}")

        @kb.add("p", filter=is_idle)
        def _prev(event):
            picks = self._picks()
            if not picks:
                self._push_status("no picks to cycle")
                return
            self.pick_index = (self.pick_index - 1) % len(picks)
            self._push_status(f"pick {self.pick_index + 1}/{len(picks)}")

        @kb.add("P", filter=is_idle)
        def _toggle_proxy(event):
            self._toggle_proxy()

        @kb.add("V", filter=is_idle)
        def _toggle_viewer(event):
            self.viewer_mode = not self.viewer_mode
            self._push_status("viewer on" if self.viewer_mode else "viewer off")

        @kb.add("s", filter=is_idle)
        def _open_picker(event):
            self._open_session_picker()

        @kb.add("up", filter=is_picking)
        def _picker_up(event):
            if self.picker_items:
                self.picker_index = (self.picker_index - 1) % len(self.picker_items)

        @kb.add("down", filter=is_picking)
        def _picker_down(event):
            if self.picker_items:
                self.picker_index = (self.picker_index + 1) % len(self.picker_items)

        for digit in "123456789":
            @kb.add(digit, filter=is_picking)
            def _picker_digit(event, _d=digit):
                target = int(_d) - 1
                if 0 <= target < len(self.picker_items):
                    self.picker_index = target

        @kb.add("enter", filter=is_picking)
        def _picker_confirm(event):
            if not self.picker_items:
                # Nothing to pick — bail out of picker mode. If this was the
                # initial welcome gate, exit the whole view too.
                self.picker_mode = False
                if self.first_pick:
                    event.app.exit()
                return
            chosen = self.picker_items[self.picker_index]
            self.picker_mode = False
            self._switch_session(chosen)
            self.first_pick = False

        @kb.add("escape", filter=is_picking)
        def _picker_cancel(event):
            self.picker_mode = False
            # Esc on the welcome screen exits the view entirely.
            if self.first_pick:
                event.app.exit()
                return
            self._push_status("session pick cancelled")

        @kb.add("a", filter=is_idle)
        def _accept(event):
            self._do_feedback("up")

        @kb.add("r", filter=is_idle)
        def _reject(event):
            self._do_feedback("down")

        @kb.add("e", filter=is_idle)
        def _edit(event):
            if not self.ghost:
                self._push_status("no ghost to edit")
                return
            self._enter_edit_mode(event.app, "edit")

        @kb.add("f", filter=is_idle)
        def _fix(event):
            if not self.ghost:
                self._push_status("no ghost to fix")
                return
            self._enter_edit_mode(event.app, "fix")

        @kb.add("enter", filter=is_editing)
        def _submit(event):
            text = self.edit_buffer.text
            mode = self.edit_mode
            self._exit_edit_mode(event.app)
            if text.strip():
                vote = "up_edit" if mode == "edit" else "down_edit"
                self._do_feedback(vote, edited_text=text)
            else:
                self._push_status("edit cancelled (empty)")

        @kb.add("escape", filter=is_editing)
        def _cancel(event):
            self._exit_edit_mode(event.app)
            self._push_status("edit cancelled")

        self.kb = kb

    def _enter_edit_mode(self, app, mode):
        self.edit_mode = mode
        self.edit_buffer.text = ""
        try:
            app.layout.focus(self.edit_buffer)
        except Exception:
            pass

    def _exit_edit_mode(self, app):
        self.edit_mode = None
        self.edit_buffer.text = ""
        try:
            app.layout.focus(self.idle_focus)
        except Exception:
            pass

    def _do_feedback(self, vote, edited_text=""):
        pick = self._current_pick()
        if not pick:
            self._push_status("no ghost selected")
            return
        # Reuse console feedback path so we stay one source of truth for
        # the four-signal bumps (+0.25 / -0.5 / +0.10 w=1.0 / -0.5 w=1.25).
        situation = self.turns[-1][2] if self.turns else self.console.last_prediction["situation"] if self.console.last_prediction else ""
        self.console.last_prediction = {
            "ghost": dict(pick),
            "alternates": list(self.alternates or []),
            "situation": situation,
        }
        ghost = self.console.last_prediction["ghost"]
        payload = {
            "id": ghost.get("id"), "vote": vote, "text": ghost.get("text", ""),
            "situation": situation,
            "grounding_ids": ghost.get("grounding_ids", []),
            "edited_text": edited_text,
        }
        try:
            data = self.console._remote("POST", "/feedback", payload)
            if data is None:
                data = self.console._local_feedback(ghost, situation, vote, edited_text)
            action_map = {
                "up": "reinforced", "down": "down-weighted",
                "up_edit": "stored edit", "down_edit": "stored correction",
            }
            detail = data.get("stored_id") or ghost.get("id") or "ok"
            base = f"{action_map.get(vote, 'feedback')} ({detail})"
            self.feedback_count += 1
            # Close the loop: when the operator approves a ghost (accept) or
            # submits an edit, push the text onto the system clipboard so they
            # can paste it into Claude Code without alt-tabbing through a file.
            clip_suffix = ""
            if vote in ("up", "up_edit"):
                clip_text = edited_text if vote == "up_edit" else ghost.get("text", "")
                if clip_text:
                    if copy_to_clipboard(clip_text):
                        clip_suffix = f" · copied {len(clip_text)} chars"
                    else:
                        clip_suffix = " · clipboard unavailable"
            self._push_status(base + clip_suffix)
        except Exception as exc:
            self._push_status(f"feedback error: {self._short(str(exc), 60)}")

    def _read_transcript_turns(self):
        if not self.transcript_path:
            return
        path = Path(self.transcript_path)
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return
        turns: list[tuple[float, str, str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = ""
            text = text.strip()
            if not text:
                continue
            ts_raw = obj.get("timestamp")
            ts = time.time()
            if isinstance(ts_raw, str):
                try:
                    from datetime import datetime as _dt
                    ts = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
            elif isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            turns.append((ts, role, text))
        self.turns = turns

    def _refresh_state(self):
        # During the welcome gate, suppress state pulls so a daemon-side
        # watcher (auto-started by `cli daemon` on the most-recent session)
        # does not leak its ghost/transcript into the picker. The proxy
        # badge still updates so the user can see ON/OFF on the welcome.
        if self.first_pick:
            self._refresh_proxy_status()
            return
        state = self._fetch_daemon_state()
        if state is not None:
            self.state_source = "daemon"
        elif self.watcher is not None:
            try:
                state = self.watcher.get_state()
                self.state_source = "local"
            except Exception:
                return
        else:
            return
        self.transcript_path = state.get("transcript", "") or self.transcript_path
        new_ghost = state.get("ghost")
        new_alts = state.get("alternates") or []
        # If the underlying Claude turn changed, reset cycling to the top pick.
        signature = (state.get("claude_output") or "")[:200]
        if signature != self._last_turn_signature:
            self.pick_index = 0
            self._last_turn_signature = signature
        self.ghost = new_ghost
        self.alternates = new_alts
        self.ghost_updated_at = float(state.get("updated_at") or 0.0)
        self._read_transcript_turns()
        self._refresh_proxy_status()

    def _open_session_picker(self):
        from shadow_link.live import discover_all_transcripts
        self.picker_items = discover_all_transcripts(limit=20)
        self.picker_index = 0
        # Preselect the currently-watched session if it's in the list.
        if self.transcript_path:
            for i, item in enumerate(self.picker_items):
                if item.get("path") == self.transcript_path:
                    self.picker_index = i
                    break
        self.picker_mode = True
        self._push_status(f"found {len(self.picker_items)} session(s)")

    def _switch_session(self, item: dict):
        path = item.get("path")
        if not path:
            self._push_status("invalid session selection")
            return
        sid_short = (item.get("session_id", "") or "")[:8]
        # Prefer the daemon if we're sourcing from it (single point of truth
        # + proxy push visibility). If the daemon is offline OR running an
        # older build without /watch/select, fall through to the local path.
        if self.state_source == "daemon":
            daemon_error = None
            try:
                data = self.console._remote(
                    "POST", "/watch/select", {"path": path}, timeout=3.0,
                )
            except Exception as exc:
                data = None
                daemon_error = str(exc)
            if data is not None:
                self.transcript_path = path
                self.turns = []
                self.ghost = None
                self.alternates = []
                self._last_turn_signature = ""
                self._push_status(f"switched (daemon) → {sid_short}…")
                return
            # Daemon rejected or is offline; fall back to a local watcher and
            # surface why so the operator knows the daemon needs a restart.
            note = "daemon offline" if daemon_error is None else self._short(daemon_error, 40)
            self._push_status(
                f"daemon select unavailable ({note}); falling back to local"
            )
            self.state_source = "local"
        # Local watcher path: stop the old one, start a fresh one on the new file.
        import shadow_link.live as live_mod
        from shadow_link.live import LiveWatcher
        try:
            if live_mod.ACTIVE_WATCHER is not None:
                live_mod.ACTIVE_WATCHER.stop()
            live_mod.ACTIVE_WATCHER = LiveWatcher(
                str(self.console.db.db_path), path,
            )
            live_mod.ACTIVE_WATCHER.start()
            self.watcher = live_mod.ACTIVE_WATCHER
            self.transcript_path = path
            self.turns = []
            self.ghost = None
            self.alternates = []
            self._last_turn_signature = ""
            self._push_status(f"switched (local) → {sid_short}…")
        except Exception as exc:
            self._push_status(f"local switch failed: {self._short(str(exc), 40)}")

    def _fetch_daemon_state(self) -> dict | None:
        """Pull watcher state from the daemon's /live endpoint.

        Returns the state dict when the daemon is online AND has an active
        watcher (so the proxy push path is visible here). Returns None to
        signal "fall back to local watcher".
        """
        try:
            data = self.console._remote("GET", "/live", timeout=0.8)
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("active"):
            return None
        return data

    def _toggle_proxy(self):
        """Start or stop the Claude Code proxy from inside /claude-shadow.

        Reuses the same code path as the /proxy TUI command so daemon-managed
        and TUI-managed proxies stay consistent.
        """
        action = "stop" if self.proxy_running else "start"
        try:
            port = int(os.getenv("SHADOW_LINK_PROXY_PORT", "8080"))
            data, source = self.console._proxy_action(action, port)
            self.proxy_running = bool(data.get("running"))
            self.proxy_url = data.get("proxy_url", "") or ""
            msg = data.get("message") or (
                f"proxy {action}ed ({source})"
            )
            self._push_status(self._short(msg, 60))
        except Exception as exc:
            self._push_status(f"proxy {action} failed: {self._short(str(exc), 40)}")

    def _refresh_proxy_status(self):
        try:
            data = self.console._remote("GET", "/proxy/status", timeout=0.5)
            if data is None:
                from shadow_link.proxy_runtime import PROXY_RUNTIME
                data = PROXY_RUNTIME.status()
            self.proxy_running = bool(data.get("running"))
            self.proxy_url = data.get("proxy_url", "") or ""
        except Exception:
            self.proxy_running = False

    def _poll_loop(self):
        while not self._stop.wait(1.0):
            try:
                self._refresh_state()
                self.app.invalidate()
            except Exception:
                pass

    def run(self) -> str:
        """Returns a short status string for the caller to print after exit.

        On entry, shows the Braille Claude Shadow splash. Enter opens the
        session picker. The watcher does not start until the operator confirms
        a session; Esc on the picker exits without starting one.
        """
        # Decide upfront which source we'll talk to so the picker confirm
        # knows whether to POST /watch/select or swap a local watcher.
        daemon_state = self._fetch_daemon_state()
        self.state_source = "daemon" if daemon_state is not None else "local"
        self.watcher = None  # set on first session pick

        # The brand gate advances to the picker only after Enter.
        self.splash_mode = True
        self._splash_frame = 0
        self.picker_mode = False
        self.first_pick = True

        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self._animation_thread = threading.Thread(target=self._animate_ui, daemon=True)
        self._animation_thread.start()
        try:
            self.app.run()
        finally:
            self._stop.set()
            if self._animation_thread is not None:
                self._animation_thread.join(timeout=0.5)
        if self.first_pick:
            return "Left claude-shadow (no session picked)."
        return f"Left claude-shadow ({self.feedback_count} feedback sent · source: {self.state_source})."


def run_tui():
    console = ShadowLinkConsole()
    console.run()

if __name__ == "__main__":
    run_tui()
