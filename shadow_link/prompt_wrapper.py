"""
shadow_link/prompt_wrapper.py

Interactive terminal prompt wrapper using prompt_toolkit.
Wraps any CLI agent subprocess, streams stdout/stderr character-by-character,
and offers inline zsh/fish-style ghost suggestions that can be accepted with Tab.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from shadow_link.config import Config
from shadow_link.database import Database
from shadow_link.engine import Engine
from shadow_link.ingester import classify_multi

# Global state to hold the current ghost suggestion for the AutoSuggest class
_current_ghost: str = ""

class ShadowLinkAutoSuggest(AutoSuggest):
    """Inline autocomplete suggestion provider for prompt_toolkit."""
    def get_suggestion(self, buffer, document) -> Suggestion | None:
        global _current_ghost
        if not _current_ghost:
            return None
        text = document.text
        # If what the user has typed is a prefix of our ghost text, suggest the rest
        if _current_ghost.startswith(text) and len(text) > 0:
            return Suggestion(_current_ghost[len(text):])
        return None

class PromptWrapper:
    def __init__(self, command: list[str], db_path: str | Path, output=None):
        self.command = command
        self.db_path = str(db_path)
        self.db = Database(self.db_path)
        self.engine = Engine(self.db)
        self.output = output
        
        # Buffer to accumulate recent agent stdout to act as situation context
        self.context_buffer = deque(maxlen=20)
        self.last_input_time = time.time()
        self.proc = None
        self.output_event = asyncio.Event()

    def get_current_context(self) -> str:
        """Join recent lines printed by the agent to form the context for matching."""
        return "\n".join(self.context_buffer).strip()

    async def stream_stream(self, stream, is_stderr: bool = False):
        """Asynchronously read from the subprocess stream and print to terminal."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                # Read character-by-character or small chunks for real-time responsiveness
                chunk = await stream.read(512)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="ignore")
                
                # Write to stdout cleanly (patch_stdout redirects this above the prompt)
                if is_stderr:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                else:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    # Append non-empty lines to context buffer for prediction
                    for line in text.splitlines():
                        line_stripped = line.strip()
                        if line_stripped and len(line_stripped) > 5:
                            # Filter out system logs or prompt markers
                            if not any(m in line_stripped for m in ("<command-name>", "<local-command", "│")):
                                self.context_buffer.append(line_stripped)
                
                self.output_event.set()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            sys.stderr.write(f"\n[Shadow Link Error] Stream read error: {e}\n")

    async def run(self):
        global _current_ghost
        print(f"[Shadow Link] Starting subprocess: {' '.join(self.command)}")
        
        # Start the target subprocess
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ
            )
        except Exception as e:
            print(f"[Shadow Link Error] Failed to start subprocess: {e}")
            return

        # Start stdout and stderr reader tasks in the background
        stdout_task = asyncio.create_task(self.stream_stream(self.proc.stdout))
        stderr_task = asyncio.create_task(self.stream_stream(self.proc.stderr, is_stderr=True))

        # Setup prompt_toolkit keybindings
        kb = KeyBindings()

        @kb.add("tab")
        def _(event):
            """Accept full ghost suggestion when Tab is pressed."""
            buffer = event.current_buffer
            suggestion = buffer.suggestion
            if suggestion:
                buffer.insert_text(suggestion.text)

        # Setup prompt session with our custom AutoSuggest
        session = PromptSession(
            auto_suggest=ShadowLinkAutoSuggest(),
            key_bindings=kb,
            output=self.output
        )

        # Main interactive loop
        with patch_stdout():
            while self.proc.returncode is None:
                # 1. Wait for the subprocess to finish printing its output (wait for quiet period)
                self.output_event.clear()
                while True:
                    try:
                        # Wait for output event with a timeout
                        await asyncio.wait_for(self.output_event.wait(), timeout=0.3)
                        self.output_event.clear()
                    except asyncio.TimeoutError:
                        # No new output for 300ms, assume the process is waiting for user input
                        break

                if self.proc.returncode is not None:
                    break

                # 2. Get the current situation context and calculate predictions
                context = self.get_current_context()
                _current_ghost = ""
                
                if context:
                    # Retrieve matches from our Jaccard / Cosine database
                    matches = self.engine.predict_replies(context, top_k=1)
                    if matches:
                        top = matches[0]
                        signal = top.combined_score * max(0.0, min(top.effective_weight, 2.0))
                        # Only show ghost if it passes a confidence/signal floor
                        if signal >= 0.15:
                            _current_ghost = top.text

                # 3. Prompt the user for input
                try:
                    user_input = await session.prompt_async("> ")
                except (KeyboardInterrupt, EOFError):
                    # Gracefully terminate subprocess on Ctrl+C or Ctrl+D
                    print("\n[Shadow Link] Terminating subprocess...")
                    break

                # 4. Ingest the interaction passively into the database
                user_reply = user_input.strip()
                if len(user_reply) >= 4 and not user_reply.startswith("/"):
                    try:
                        # If a ghost text was predicted, check if the user accepted or edited it
                        buckets = classify_multi(user_reply)
                        meta = {
                            "buckets": buckets,
                            "source": "integrated_cli",
                            "agent_output": context[:1000]
                        }
                        self.db.add_prompt(text=user_reply, bucket=buckets[0], metadata=meta)
                        
                        # Reinforce if they accepted the exact ghost
                        if _current_ghost and user_reply == _current_ghost:
                            # Search for the prompt ID of the matched suggestion to reinforce it
                            for match in self.engine.predict_replies(context, top_k=5):
                                if match.text == _current_ghost:
                                    self.db.reinforce(match.prompt_id)
                                    break
                    except Exception as e:
                        # Learning errors should be silent and not crash the execution
                        pass

                # 5. Write the user's input to the subprocess stdin
                if self.proc.returncode is None:
                    self.proc.stdin.write((user_input + "\n").encode("utf-8"))
                    await self.proc.stdin.drain()

            # Clean up tasks
            stdout_task.cancel()
            stderr_task.cancel()
            
            if self.proc.returncode is None:
                try:
                    self.proc.terminate()
                    await self.proc.wait()
                except Exception:
                    pass

        print(f"\n[Shadow Link] Subprocess exited with code {self.proc.returncode}")

def run_prompt_wrapper(command: list[str], db_path: str | Path):
    asyncio.run(PromptWrapper(command, db_path).run())
