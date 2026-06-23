"""
shadow_link/watcher.py

Monitors a target directory for OGI question files and outputs ghost responses.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Set

from shadow_link.engine import Engine

class FileWatcher:
    def __init__(
        self,
        watch_dir: str | Path,
        matcher: Engine,
        poll_interval: float = 1.0,
        auto_approve_threshold: float = 0.90
    ):
        self.watch_dir = Path(watch_dir)
        self.matcher = matcher
        self.poll_interval = poll_interval
        self.auto_approve_threshold = auto_approve_threshold
        self.processed_ids: Set[str] = set()
        
        self.watch_dir.mkdir(parents=True, exist_ok=True)

    def scan(self):
        """Scans the directory for unprocessed .question.txt files."""
        question_files = list(self.watch_dir.glob("*.question.txt"))
        
        for q_file in question_files:
            req_id = q_file.name.split(".")[0]
            if req_id in self.processed_ids:
                continue
                
            try:
                if not q_file.exists():
                    continue
                size1 = q_file.stat().st_size
                time.sleep(0.2)
                if not q_file.exists():
                    continue
                size2 = q_file.stat().st_size
                if size1 != size2 or size1 == 0:
                    continue
            except OSError:
                continue
                
            self.process_question(req_id, q_file)
            self.processed_ids.add(req_id)

    def process_question(self, req_id: str, q_file: Path):
        """Processes a single question file, computes matches, and handles output."""
        print(f"\n[Watcher] Detected new question: {q_file.name}")
        
        try:
            content = q_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"  Error reading {q_file.name}: {e}")
            return

        question_text = content
        if "Question:" in content:
            parts = content.split("Question:", 1)
            question_text = parts[1].strip()

        print(f"  Input Question: '{question_text}'")
        
        matches = self.matcher.match(question_text, top_k=3)
        
        if not matches:
            print("  No matches found above threshold.")
            return
            
        print("  Top Matches:")
        for idx, match_res in enumerate(matches):
            sem_str = f"{match_res.cosine_sim:.2f}" if match_res.cosine_sim >= 0 else "N/A"
            print(f"    {idx+1}. Score: {match_res.combined_score:.2f} (Jaccard: {match_res.jaccard_sim:.2f}, Semantic: {sem_str}) -> '{match_res.text}'")

        best_match = matches[0]
        suggested_reply = best_match.text
        best_score = best_match.combined_score
        
        suggested_path = self.watch_dir / f"{req_id}.ghost.txt"
        response_path = self.watch_dir / f"{req_id}.response.txt"
        
        if best_score >= self.auto_approve_threshold:
            try:
                response_path.write_text(suggested_reply, encoding="utf-8")
                print(f"  [AUTO-APPROVED] Confidence {best_score:.2f} >= {self.auto_approve_threshold:.2f}.")
                print(f"  Wrote reply directly to {response_path.name}")
            except Exception as e:
                print(f"  Failed to write response: {e}")
        else:
            try:
                suggested_path.write_text(suggested_reply, encoding="utf-8")
                print(f"  [SUGGESTION] Confidence {best_score:.2f} < {self.auto_approve_threshold:.2f}.")
                print(f"  Wrote suggested reply to {suggested_path.name}")
            except Exception as e:
                print(f"  Failed to write suggestion: {e}")

    def start_loop(self):
        """Starts a blocking watch loop."""
        print(f"[Watcher] Monitoring folder: {self.watch_dir.resolve()}")
        print(f"[Watcher] Auto-approve threshold: {self.auto_approve_threshold:.2f}")
        print("Press Ctrl+C to stop watching.")
        
        existing_questions = self.watch_dir.glob("*.question.txt")
        for f in existing_questions:
            req_id = f.name.split(".")[0]
            self.processed_ids.add(req_id)
            
        try:
            while True:
                self.scan()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("\n[Watcher] Stopped monitoring.")
