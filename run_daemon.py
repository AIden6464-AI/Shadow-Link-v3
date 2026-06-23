"""
Shadow Link v3 Daemon Launcher.
Runs the background daemon (API Server + LiveWatcher tailer).
"""
import os
import sys
from pathlib import Path

# Add local path so we import shadow_link natively
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shadow_link.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "daemon"]
    main()
