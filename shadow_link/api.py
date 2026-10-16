"""
shadow_link/api.py

Local API Server for Shadow Link using standard library http.server.
Serves endpoints for querying, predicting, and interacting with the corpus.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from shadow_link.config import Config
from shadow_link.database import Database
from shadow_link.engine import Engine
from shadow_link.ingester import Ingester
from shadow_link.decay import effective_weight_for_row, BUCKET_DECAY_CLASS, _HALF_LIVES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_link.api")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}

class ShadowLinkAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(format % args)

    def _set_headers(self, status=200, content_type="application/json"):
        # No CORS headers: only local, non-browser clients (plugin, CLI, proxy,
        # TUI) talk to the daemon, so web pages must not read its responses.
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _reject_untrusted(self) -> bool:
        """403 for browser-originated or DNS-rebinding requests. Returns True if rejected."""
        host = (self.headers.get("Host") or "").strip().lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if hostname not in _LOCAL_HOSTS or self.headers.get("Origin"):
            self.send_error(403, "Forbidden")
            return True
        return False

    def send_error(self, code, message=None, explain=None):
        try:
            self._set_headers(code)
            err_msg = message or "An error occurred"
            if explain:
                err_msg += f": {explain}"
            self.wfile.write(json.dumps({"error": err_msg}).encode("utf-8"))
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_error(405, "Method not allowed")

    def do_GET(self):
        if self._reject_untrusted():
            return
        parsed_path = urlparse(self.path)
        path_str = parsed_path.path
        if path_str == "/stats":
            try:
                config = Config()
                db = Database(config.db_path)
                prompts = db.all_prompts()
                
                prompts_by_bucket = {}
                for p in prompts:
                    bucket = p.get("bucket", "request")
                    prompts_by_bucket.setdefault(bucket, []).append(p)
                
                buckets_data = []
                for bname in ("request", "reminder", "rejection", "correction", "pivot"):
                    bucket_prompts = prompts_by_bucket.get(bname, [])
                    count = len(bucket_prompts)
                    
                    active_count = 0
                    total_weight = 0.0
                    for p in bucket_prompts:
                        eff_w = effective_weight_for_row(p)
                        if eff_w > 0.5:
                            active_count += 1
                        total_weight += eff_w
                    
                    avg_weight = (total_weight / count) if count > 0 else 1.0
                    decay_cls = BUCKET_DECAY_CLASS.get(bname, "tactical")
                    half_life = _HALF_LIVES.get(decay_cls, 12.0)
                    half_life_str = "Infinity" if half_life == float('inf') else f"{half_life}h"
                    
                    buckets_data.append({
                        "name": bname,
                        "prompt_count": count,
                        "description": f"Bucket for {bname}s",
                        "active_count": active_count,
                        "average_weight": avg_weight,
                        "decay_class": str(decay_cls.value) if hasattr(decay_cls, 'value') else str(decay_cls),
                        "half_life": half_life_str
                    })
                
                db_file = Path(db.db_path)
                db_bytes = sum(p.stat().st_size for p in (db_file, Path(f"{db_file}-wal"))
                               if p.exists())
                response_data = {
                    "database_path": str(db.db_path),
                    "database_bytes": db_bytes,
                    "total_prompts": len(prompts),
                    "total_edges": db.stats()["total_edges"],
                    "jaccard_threshold": config.jaccard_threshold,
                    "buckets": buckets_data
                }
                self._set_headers(200)
                self.wfile.write(json.dumps(response_data).encode("utf-8"))
            except ConnectionError as e:
                logger.info(f"Connection closed by client during GET /stats: {e}")
            except Exception as e:
                logger.error(f"Error in GET /stats: {e}", exc_info=True)
                self.send_error(500, str(e))
        elif path_str == "/live":
            self.handle_live()
        elif path_str == "/pairs":
            self.handle_pairs(parsed_path)
        elif path_str == "/watcher/files":
            self.handle_watcher_files()
        elif path_str == "/proxy/status":
            self.handle_proxy_status()
        elif path_str == "/proxy/audit":
            self.handle_proxy_audit(parsed_path)
        elif path_str.startswith("/aca/"):
            params = {k: v[0] for k, v in parse_qs(parsed_path.query).items()}
            self.handle_aca(path_str, params)
        elif path_str == "/sessions" or path_str.startswith("/sessions/") or path_str == "/threads":
            params = {k: v[0] for k, v in parse_qs(parsed_path.query).items()}
            self.handle_sessions_get(path_str, params)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        if self._reject_untrusted():
            return
        parsed_path = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length and "application/json" not in self.headers.get("Content-Type", ""):
            self.send_error(415, "Content-Type must be application/json")
            return
        
        if content_length == 0:
            if parsed_path.path == "/watcher/clear":
                self.handle_watcher_clear()
                return
            self.send_error(400, "Empty request body")
            return
            
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            self.send_error(400, f"Invalid JSON body: {e}")
            return

        if not isinstance(data, dict):
            self.send_error(400, "JSON body must be a dictionary")
            return

        if parsed_path.path == "/query":
            self.handle_query(data)
        elif parsed_path.path == "/accept":
            self.handle_accept(data)
        elif parsed_path.path == "/feedback":
            self.handle_feedback(data)
        elif parsed_path.path == "/predict":
            self.handle_predict(data)
        elif parsed_path.path == "/nudge":
            self.handle_nudge(data)
        elif parsed_path.path == "/ingest":
            self.handle_ingest(data)
        elif parsed_path.path == "/watcher/simulate":
            self.handle_watcher_simulate(data)
        elif parsed_path.path == "/watcher/clear":
            self.handle_watcher_clear()
        elif parsed_path.path == "/proxy/start":
            self.handle_proxy_start(data)
        elif parsed_path.path == "/proxy/stop":
            self.handle_proxy_stop()
        elif parsed_path.path == "/watch/proxy_event":
            self.handle_watch_proxy_event(data)
        elif parsed_path.path == "/watch/select":
            self.handle_watch_select(data)
        elif parsed_path.path.startswith("/aca/"):
            self.handle_aca(parsed_path.path, data)
        elif parsed_path.path in ("/track/event", "/handoff", "/forget", "/sessions/rebuild") or (
                parsed_path.path.startswith("/sessions/") and parsed_path.path.endswith("/digest")):
            self.handle_sessions_post(parsed_path.path, data)
        else:
            self.send_error(404, "Endpoint not found")

    def _serialize_matches(self, matches, db):
        serialized_matches = []
        for match_res in matches:
            p = db.get_prompt(match_res.prompt_id)
            meta = {}
            if p and p.get("metadata"):
                try:
                    meta = json.loads(p["metadata"])
                except Exception:
                    pass
            
            created_time = p.get("created_at", time.time()) if p else time.time()
            timestamp_str = datetime.utcfromtimestamp(created_time).isoformat() + "Z"
            
            serialized_matches.append({
                "prompt": {
                    "id": match_res.prompt_id,
                    "content": match_res.text,
                    "prompt_type": meta.get("prompt_type", "user_prompt"),
                    "timestamp": meta.get("timestamp", timestamp_str),
                    "metadata": meta,
                    "buckets": {match_res.bucket: 1.0}
                },
                "score": match_res.combined_score,
                "jaccard": match_res.jaccard_sim,
                "semantic": match_res.cosine_sim
            })
        return serialized_matches

    def handle_query(self, data):
        query_text = data.get("text")
        if query_text is None or not isinstance(query_text, str) or not query_text.strip():
            self.send_error(400, "Parameter 'text' must be a non-empty string")
            return
            
        top_k = data.get("top_k", 5)
        threshold = data.get("threshold")
        if threshold is not None:
            try:
                threshold = float(threshold)
            except (ValueError, TypeError):
                self.send_error(400, "Parameter 'threshold' must be a float")
                return
        
        try:
            config = Config()
            db = Database(config.db_path)
            matcher = Engine(db)
            matches = matcher.match(query_text, top_k=top_k, threshold=threshold)
            serialized_matches = self._serialize_matches(matches, db)
            self._set_headers(200)
            self.wfile.write(json.dumps({"matches": serialized_matches}).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /query: {e}")
        except Exception as e:
            logger.error(f"Error in POST /query: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_predict(self, data):
        question = data.get("question")
        if question is None or not isinstance(question, str) or not question.strip():
            self.send_error(400, "Parameter 'question' must be a non-empty string")
            return
            
        try:
            config = Config()
            from shadow_link.live import build_prediction
            ghost, alternates = build_prediction(str(config.db_path), question, top_k=5)

            best_match = None
            if ghost:
                best_match = {
                    "score": ghost["confidence"],
                    "prompt": {
                        "content": ghost["text"]
                    }
                }

            # Past (situation -> reply) pairs nearest this situation, for a model
            # that drafts the reply in the user's own voice.
            examples = []
            try:
                n_examples = max(0, min(int(data.get("examples") or 0), 20))
            except (TypeError, ValueError):
                n_examples = 0
            if n_examples:
                db = Database(config.db_path)
                for r in Engine(db).predict_replies(question, top_k=n_examples):
                    p = db.get_prompt(r.prompt_id) or {}
                    try:
                        m = json.loads(p.get("metadata") or "{}")
                    except Exception:
                        m = {}
                    examples.append({
                        "id": r.prompt_id,
                        "situation": (m.get("agent_output") or m.get("question") or "")[:600],
                        "reply": r.text[:500],
                    })

            response_data = {
                "ghost": ghost,
                "alternates": alternates,
                "examples": examples,
                "ghost_suggestion": ghost["text"] if ghost else None,
                "confidence": ghost["confidence"] if ghost else 0.0,
                "source": ghost["source"] if ghost else None,
                "best_match": best_match,
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(response_data).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /predict: {e}")
        except Exception as e:
            logger.error(f"Error in POST /predict: {e}", exc_info=True)
            self.send_error(500, str(e))


    def handle_nudge(self, data):
        context = data.get("context")
        if context is None or not isinstance(context, str) or not context.strip():
            self.send_error(400, "Parameter 'context' must be a non-empty string")
            return

        synthesize = bool(data.get("synthesize", False))
        try:
            lm_timeout = float(data.get("lm_timeout_seconds") or 0.0) or None
        except (TypeError, ValueError):
            lm_timeout = None

        try:
            config = Config()
            db = Database(config.db_path)
            matcher = Engine(db)
            matches = matcher.match(
                context, top_k=5, threshold=0.0,
                buckets=["rejection", "correction", "reminder", "pivot"],
            )
            serialized_matches = self._serialize_matches(matches, db)
            ghost = None
            if synthesize:
                ghost = self._synthesize_ghost(
                    str(config.db_path), context,
                    serialized_matches, lm_timeout,
                )
            payload = {"nudges": serialized_matches, "ghost": ghost}
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /nudge: {e}")
        except Exception as e:
            logger.error(f"Error in POST /nudge: {e}", exc_info=True)
            self.send_error(500, str(e))

    def _synthesize_ghost(self, db_path, context, serialized_matches, lm_timeout):
        """Run synthesis; fall back to top retrieved nudge on failure / empty LM."""
        from shadow_link.live import build_prediction
        try:
            ghost, _alts = build_prediction(
                db_path, context, top_k=5,
                timeout_seconds=lm_timeout,
            )
        except Exception as exc:
            logger.debug("Ghost synthesis failed (%s); using retrieved fallback.", exc)
            ghost = None
        if ghost and ghost.get("source") == "generated" and ghost.get("text"):
            return {
                "text": ghost["text"],
                "source": "generated",
                "signal": ghost.get("signal"),
                "tier": ghost.get("tier"),
            }
        if serialized_matches:
            top = serialized_matches[0]
            prompt = top.get("prompt") or {}
            score = float(top.get("score") or 0.0)
            tier = "high" if score >= 0.55 else "medium" if score >= 0.30 else "low"
            return {
                "text": prompt.get("content", "") or "",
                "source": "retrieved",
                "signal": score,
                "tier": tier,
            }
        return None

    def handle_watch_select(self, data):
        """Switch the daemon's watcher to a specific Claude Code transcript."""
        path = data.get("path") or ""
        if not isinstance(path, str) or not path:
            self.send_error(400, "Parameter 'path' must be a non-empty string")
            return
        from pathlib import Path as _Path
        projects = (_Path.home() / ".claude" / "projects").resolve()
        resolved = _Path(path).resolve()
        if resolved.suffix != ".jsonl" or projects not in resolved.parents:
            self.send_error(400, "Transcript must be a .jsonl under ~/.claude/projects")
            return
        if not resolved.exists():
            self.send_error(400, f"Transcript not found: {path}")
            return
        try:
            import shadow_link.live as live_mod
            from shadow_link.live import LiveWatcher
            config = Config()
            if live_mod.ACTIVE_WATCHER is not None:
                try:
                    live_mod.ACTIVE_WATCHER.stop()
                except Exception:
                    pass
            live_mod.ACTIVE_WATCHER = LiveWatcher(str(config.db_path), path)
            live_mod.ACTIVE_WATCHER.start()
            self._set_headers(200)
            self.wfile.write(json.dumps({"selected": path}).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /watch/select: {e}")
        except Exception as e:
            logger.error(f"Error in POST /watch/select: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_watch_proxy_event(self, data):
        """Proxy push: Claude just answered. Run prediction and stash in
        ACTIVE_WATCHER state so /claude-shadow surfaces it without waiting
        on the JSONL tail.
        """
        claude_reply = data.get("claude_reply") or data.get("claude_output") or ""
        if not isinstance(claude_reply, str) or not claude_reply.strip():
            self.send_error(400, "Parameter 'claude_reply' must be a non-empty string")
            return
        try:
            import shadow_link.live as live_mod
            from shadow_link.live import LiveWatcher
            if live_mod.ACTIVE_WATCHER is None:
                config = Config()
                live_mod.ACTIVE_WATCHER = LiveWatcher(str(config.db_path))
                # No start() — proxy is feeding it; the tail loop is optional
            result = live_mod.ACTIVE_WATCHER.ingest_proxy_event(claude_reply)
            ghost = result.get("ghost")
            payload = {
                "received": True,
                "ghost": {
                    "text": ghost.get("text") if ghost else None,
                    "source": ghost.get("source") if ghost else None,
                    "tier": ghost.get("tier") if ghost else None,
                } if ghost else None,
                "skipped": result.get("skipped"),
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /watch/proxy_event: {e}")
        except Exception as e:
            logger.error(f"Error in POST /watch/proxy_event: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_ingest(self, data):
        question = data.get("question")
        response = data.get("response")
        request_id = data.get("request_id")
        
        if not question or not response or not request_id:
            self.send_error(400, "Missing parameters: 'question', 'response', or 'request_id'")
            return
            
        try:
            config = Config()
            db = Database(config.db_path)
            ingester = Ingester(
                db,
                provider=config.embedding_provider,
                url=config.embedding_api_url,
                model=config.embedding_model
            )
            prompt_id = ingester.ingest_interaction(question, response, request_id)
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "ingested", "prompt_id": prompt_id}).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /ingest: {e}")
        except Exception as e:
            logger.error(f"Error in POST /ingest: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_live(self):
        try:
            from shadow_link import live as live_mod
            watcher = getattr(live_mod, "ACTIVE_WATCHER", None)
            if watcher is None:
                self._set_headers(200)
                self.wfile.write(json.dumps({"active": False}).encode("utf-8"))
                return
            state = watcher.get_state()
            config = Config()
            db = Database(config.db_path)
            prompts = db.all_prompts()
            by_bucket = {}
            for p in prompts:
                by_bucket[p["bucket"]] = by_bucket.get(p["bucket"], 0) + 1
            state["active"] = True
            state["corpus_total"] = len(prompts)
            state["corpus_by_bucket"] = by_bucket
            self._set_headers(200)
            self.wfile.write(json.dumps(state).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET /live: {e}")
        except Exception as e:
            logger.error(f"Error in GET /live: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_pairs(self, parsed_path):
        try:
            from shadow_link.ingester import classify_multi
            qs = parse_qs(parsed_path.query)
            limit = int(qs.get("limit", ["200"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            search = (qs.get("q", [""])[0]).strip().lower()
            bucket_filter = qs.get("bucket", [None])[0]
            FLAG_BUCKETS = {"rejection", "correction", "pivot"}

            config = Config()
            db = Database(config.db_path)
            rows = db.all_prompts()

            items = []
            for r in rows:
                try:
                    meta = json.loads(r.get("metadata") or "{}")
                except Exception:
                    meta = {}
                situation = (meta.get("agent_output") or meta.get("question") or "").strip()
                tags = meta.get("buckets") or classify_multi(r["text"])
                eff_w = effective_weight_for_row(r)
                flagged = bool(FLAG_BUCKETS.intersection(tags) or r["bucket"] in FLAG_BUCKETS)
                created = r.get("created_at", 0) or 0
                item = {
                    "id": r["id"],
                    "reply": r["text"],
                    "situation": situation,
                    "has_situation": bool(situation),
                    "bucket": r["bucket"],
                    "tags": tags,
                    "weight": round(r.get("weight", 1.0), 3),
                    "effective_weight": round(eff_w, 3),
                    "flagged": flagged,
                    "source": meta.get("source", "unknown"),
                    "created_at": created,
                    "timestamp": (datetime.utcfromtimestamp(created).isoformat() + "Z") if created else None,
                }
                if bucket_filter and bucket_filter not in tags and r["bucket"] != bucket_filter:
                    continue
                if search and search not in r["text"].lower() and search not in situation.lower():
                    continue
                items.append(item)

            items.sort(key=lambda x: x["created_at"], reverse=True)
            total = len(items)
            page = items[offset:offset + limit]

            tag_counts = {}
            for it in items:
                for t in it["tags"]:
                    tag_counts[t] = tag_counts.get(t, 0) + 1

            self._set_headers(200)
            self.wfile.write(json.dumps({
                "pairs": page,
                "total": total,
                "offset": offset,
                "limit": limit,
                "tag_counts": tag_counts,
            }).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET /pairs: {e}")
        except Exception as e:
            logger.error(f"Error in GET /pairs: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_accept(self, data):
        prompt_id = data.get("id")
        text = data.get("text", "")
        try:
            config = Config()
            db = Database(config.db_path)
            new_weight = None
            if isinstance(prompt_id, int):
                try:
                    new_weight = db.reinforce(prompt_id)
                except ValueError:
                    new_weight = None
            if isinstance(text, str) and text.strip():
                pending = Path.home() / ".claude" / "shadow_pending.txt"
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_text(text, encoding="utf-8")
            self._set_headers(200)
            self.wfile.write(json.dumps({"accepted": True, "new_weight": new_weight}).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /accept: {e}")
        except Exception as e:
            logger.error(f"Error in POST /accept: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_feedback(self, data):
        prompt_id = data.get("id")
        vote = data.get("vote")
        text = data.get("text", "")
        situation = data.get("situation", "")
        grounding_ids = data.get("grounding_ids", [])
        edited_text = data.get("edited_text", "")
        BUMP_MAP = {"up": 0.25, "up_edit": 0.10, "down": -0.5, "down_edit": -0.5}
        bump = BUMP_MAP.get(vote, 0.0)
        try:
            from shadow_link.ingester import classify_multi
            config = Config()
            db = Database(config.db_path)
            stored_id = None
            new_weight = None
            grounded_ids = []

            if isinstance(prompt_id, int) and bump != 0.0:
                try:
                    new_weight = db.reinforce(prompt_id, bump=bump)
                except ValueError:
                    pass
            else:
                if vote == "up" and isinstance(text, str) and text.strip():
                    buckets = classify_multi(text)
                    meta = {"buckets": buckets, "source": "approved_generation",
                            "agent_output": (situation or "")[:1000]}
                    stored_id = db.add_prompt(text=text, bucket=buckets[0], metadata=meta)
                for gid in grounding_ids:
                    if isinstance(gid, int):
                        try:
                            db.reinforce(gid, bump=bump if bump else 0.0)
                            grounded_ids.append(gid)
                        except ValueError:
                            pass

            # Edit branches: store the user's edited text as a new reaction
            if vote in ("up_edit", "down_edit") and isinstance(edited_text, str) and edited_text.strip():
                from shadow_link.live import is_real_reply
                if is_real_reply(edited_text):
                    buckets = classify_multi(edited_text)
                    source_label = "approved_edit" if vote == "up_edit" else "correction"
                    weight = 1.0 if vote == "up_edit" else 1.25
                    edit_meta = {
                        "buckets": buckets,
                        "source": source_label,
                        "agent_output": (situation or "")[:1000],
                    }
                    stored_id = db.add_prompt(
                        text=edited_text, bucket=buckets[0],
                        weight=weight, metadata=edit_meta,
                    )
                    # Stage so it can be sent
                    pending = Path.home() / ".claude" / "shadow_pending.txt"
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    pending.write_text(edited_text, encoding="utf-8")

            if vote == "up" and isinstance(text, str) and text.strip():
                pending = Path.home() / ".claude" / "shadow_pending.txt"
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_text(text, encoding="utf-8")

            self._set_headers(200)
            self.wfile.write(json.dumps(
                {"ok": True, "vote": vote, "stored_id": stored_id,
                 "new_weight": new_weight, "grounding_ids": grounded_ids}
            ).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /feedback: {e}")
        except Exception as e:
            logger.error(f"Error in POST /feedback: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_watcher_files(self):
        try:
            config = Config()
            watch_dir = config.db_path.parent / "watch"
            watch_dir.mkdir(parents=True, exist_ok=True)
            
            files_data = []
            question_files = sorted(list(watch_dir.glob("*.question.txt")), key=os.path.getmtime, reverse=True)
            
            for q_file in question_files:
                req_id = q_file.name.split(".")[0]
                try:
                    question_content = q_file.read_text(encoding="utf-8").strip()
                except Exception:
                    question_content = "[Error reading file]"
                
                response_path = watch_dir / f"{req_id}.response.txt"
                suggested_path = watch_dir / f"{req_id}.ghost.txt"
                
                status = "pending"
                response_content = ""
                
                if response_path.exists():
                    status = "approved"
                    try:
                        response_content = response_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        response_content = "[Error reading response]"
                elif suggested_path.exists():
                    status = "suggested"
                    try:
                        response_content = suggested_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        response_content = "[Error reading suggestion]"
                
                try:
                    mtime = os.path.getmtime(q_file)
                except Exception:
                    mtime = time.time()
                
                files_data.append({
                    "id": req_id,
                    "question": question_content,
                    "status": status,
                    "response": response_content,
                    "timestamp": mtime
                })
                
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "watch_dir": str(watch_dir.resolve()),
                "files": files_data
            }).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET /watcher/files: {e}")
        except Exception as e:
            logger.error(f"Error in GET /watcher/files: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_watcher_simulate(self, data):
        question = data.get("question")
        if not question or not isinstance(question, str):
            self.send_error(400, "Parameter 'question' must be a non-empty string")
            return
            
        try:
            config = Config()
            watch_dir = config.db_path.parent / "watch"
            watch_dir.mkdir(parents=True, exist_ok=True)
            
            req_id = f"sim_{int(time.time() * 1000)}"
            q_file = watch_dir / f"{req_id}.question.txt"
            q_file.write_text(question, encoding="utf-8")
            
            db = Database(config.db_path)
            matcher = Engine(db)
            matches = matcher.match(question, top_k=3)
            
            best_score = 0.0
            suggested_reply = ""
            if matches:
                best_match = matches[0]
                suggested_reply = best_match.text
                best_score = best_match.combined_score
                
                if best_score >= 0.90:
                    response_path = watch_dir / f"{req_id}.response.txt"
                    response_path.write_text(suggested_reply, encoding="utf-8")
                else:
                    suggested_path = watch_dir / f"{req_id}.ghost.txt"
                    suggested_path.write_text(suggested_reply, encoding="utf-8")
            
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "status": "simulated",
                "id": req_id,
                "score": best_score,
                "response": suggested_reply
            }).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /watcher/simulate: {e}")
        except Exception as e:
            logger.error(f"Error in POST /watcher/simulate: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_watcher_clear(self):
        try:
            config = Config()
            watch_dir = config.db_path.parent / "watch"
            if watch_dir.exists():
                for f in watch_dir.glob("*.question.txt"):
                    try: os.unlink(f)
                    except: pass
                for f in watch_dir.glob("*.response.txt"):
                    try: os.unlink(f)
                    except: pass
                for f in watch_dir.glob("*.ghost.txt"):
                    try: os.unlink(f)
                    except: pass
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "cleared"}).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /watcher/clear: {e}")
        except Exception as e:
            logger.error(f"Error in POST /watcher/clear: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_proxy_status(self):
        try:
            from shadow_link.proxy_runtime import PROXY_RUNTIME
            port = int(os.getenv("SHADOW_LINK_PROXY_PORT", "8080"))
            self._set_headers(200)
            self.wfile.write(json.dumps(PROXY_RUNTIME.status(port)).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET /proxy/status: {e}")
        except Exception as e:
            logger.error(f"Error in GET /proxy/status: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_proxy_audit(self, parsed_path):
        try:
            from shadow_link.proxy_runtime import read_proxy_audit
            query = parse_qs(parsed_path.query)
            limit = int(query.get("limit", ["10"])[0])
            self._set_headers(200)
            self.wfile.write(json.dumps(read_proxy_audit(limit)).encode("utf-8"))
        except (TypeError, ValueError):
            self.send_error(400, "Audit limit must be an integer")
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET /proxy/audit: {e}")
        except Exception as e:
            logger.error(f"Error in GET /proxy/audit: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_proxy_start(self, data):
        try:
            from shadow_link.proxy_runtime import PROXY_RUNTIME
            port = int(data.get("port", os.getenv("SHADOW_LINK_PROXY_PORT", "8080")))
            if not 1 <= port <= 65535:
                self.send_error(400, "Proxy port must be between 1 and 65535")
                return
            result = PROXY_RUNTIME.start(port)
            self._set_headers(200)
            self.wfile.write(json.dumps(result).encode("utf-8"))
        except (TypeError, ValueError):
            self.send_error(400, "Proxy port must be an integer")
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /proxy/start: {e}")
        except Exception as e:
            logger.error(f"Error in POST /proxy/start: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_proxy_stop(self):
        try:
            from shadow_link.proxy_runtime import PROXY_RUNTIME
            result = PROXY_RUNTIME.stop()
            self._set_headers(200)
            self.wfile.write(json.dumps(result).encode("utf-8"))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST /proxy/stop: {e}")
        except Exception as e:
            logger.error(f"Error in POST /proxy/stop: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_aca(self, path, data):
        """Associative Context Activation endpoints (GET uses query params, POST a JSON body)."""
        from shadow_link.aca import open_aca
        try:
            aca = open_aca(Config().db_path)
            route = path[len("/aca/"):]
            if route == "event":
                if not data.get("text"):
                    self.send_error(400, "Parameter 'text' is required")
                    return
                out = aca.process(
                    data["text"], kind=data.get("kind", "prompt"), task_id=data.get("task_id"),
                    goal=data.get("goal", ""), refs=data.get("refs"), project=data.get("project", ""),
                    remember=data.get("remember"),
                )
            elif route == "node":
                out = {"id": aca.store.add_node(
                    data["content"], type=data.get("type", "memory"), title=data.get("title", ""),
                    importance=float(data.get("importance", 0.5)), trust=data.get("trust"),
                    project=data.get("project", ""), source=data.get("source", "api"),
                    metadata=data.get("metadata"))}
                out["contradicts"] = aca.detect_contradictions(out["id"])
                for link in data.get("links", []):
                    aca.store.link(out["id"], link["to"], link.get("rel", "related_to"),
                                   strength=float(link.get("strength", 0.5)), source="api")
            elif route == "link":
                aca.store.link(data["src"], data["dst"], data.get("rel", "related_to"),
                               strength=float(data.get("strength", 0.5)),
                               confidence=float(data.get("confidence", 0.8)), source="api")
                out = {"linked": True}
            elif route == "feedback":
                out = aca.feedback(data["task_id"], data["node_id"], bool(data.get("useful", True)))
            elif route == "hypothesis":
                out = {"id": aca.add_hypothesis(data["task_id"], data["text"],
                                                float(data.get("prior", 0.5)), data.get("about"))}
            elif route == "evidence":
                out = aca.add_evidence(data["task_id"], data["hypothesis_id"], data.get("text"),
                                       evidence_id=data.get("evidence_id"),
                                       supports=bool(data.get("supports", True)),
                                       weight=float(data.get("weight", 1.0)),
                                       kind=data.get("kind", "observation"))
            elif route == "action":
                aca.report_action(data["task_id"], data["action"], bool(data.get("success")),
                                  data.get("detail", ""))
                out = {"recorded": True}
            elif route == "close":
                out = aca.close_task(data["task_id"], result=data.get("result", ""),
                                     lesson=data.get("lesson", ""), procedure=data.get("procedure", ""))
            elif route == "brain":
                out = aca.snapshot(data["task_id"])
            elif route == "inspect":
                out = aca.inspect(data["task_id"], data["node_id"])
            elif route == "replay":
                out = aca.store.replay(data["task_id"], int(data.get("limit", 500)))
            elif route == "stats":
                out = aca.stats()
            else:
                self.send_error(404, f"Unknown ACA endpoint '{route}'")
                return
            self._set_headers(200)
            self.wfile.write(json.dumps(out, default=str).encode("utf-8"))
        except KeyError as e:
            self.send_error(400, f"Missing parameter {e}")
        except ValueError as e:
            self.send_error(400, str(e))
        except ConnectionError as e:
            logger.info(f"Connection closed by client during {path}: {e}")
        except Exception as e:
            logger.error(f"Error in {path}: {e}", exc_info=True)
            self.send_error(500, str(e))

    # ------------------------------------------------ Shadow Core: agent sessions
    def _send_json(self, payload, status=200):
        self._set_headers(status)
        self.wfile.write(json.dumps(payload, default=str).encode("utf-8"))

    def _sessions(self):
        from shadow_link.aca.store import CognitiveStore
        from shadow_link.tracker import SessionStore
        store = CognitiveStore(Config().db_path)
        return store, SessionStore(store)

    def handle_sessions_get(self, path, params):
        """GET /sessions, /sessions/<id>, /threads: what every agent is doing."""
        try:
            from shadow_link.tracker import session_summary, TRACKER
            _, sessions = self._sessions()
            if path == "/sessions":
                try:
                    limit = max(1, min(int(params.get("limit", 20)), 200))
                except ValueError:
                    self.send_error(400, "limit must be an integer")
                    return
                items = sessions.list(project=params.get("project", ""), agent=params.get("agent", ""),
                                      active_only=params.get("active") in ("1", "true"), limit=limit,
                                      exclude=params.get("exclude", ""))
                self._send_json({"sessions": items,
                                 "tracking": TRACKER is not None,
                                 "problems": dict(TRACKER.errors) if TRACKER else {}})
            elif path == "/threads":
                self._send_json({"threads": sessions.threads(project=params.get("project", ""))})
            else:
                rec = sessions.load(path[len("/sessions/"):])
                if rec is None:
                    self.send_error(404, "Unknown session")
                    return
                self._send_json({"session": session_summary(rec), "record": rec})
        except ConnectionError as e:
            logger.info(f"Connection closed by client during GET {path}: {e}")
        except Exception as e:
            logger.error(f"Error in GET {path}: {e}", exc_info=True)
            self.send_error(500, str(e))

    def handle_sessions_post(self, path, data):
        """POST /track/event, /sessions/<id>/digest, /handoff."""
        try:
            from shadow_link import tracker as tracker_mod
            store, sessions = self._sessions()
            if path == "/track/event":
                what = str(data.get("what") or data.get("text") or "")
                files = data.get("files") or []
                if not what and not files and not data.get("command"):
                    self.send_error(400, "Give 'what', 'files' or 'command'")
                    return
                tr = tracker_mod.TRACKER or tracker_mod.SessionTracker(Config().db_path)
                sid = tr.report(str(data.get("agent") or "agent"), what,
                                session=str(data.get("session") or ""),
                                project=str(data.get("project") or ""),
                                files=[str(f) for f in files if isinstance(f, str)],
                                command=str(data.get("command") or ""),
                                status=data.get("status") if data.get("status") in ("ok", "error") else None)
                self._send_json({"session": sid})
            elif path == "/sessions/rebuild":
                tr = tracker_mod.TRACKER or tracker_mod.SessionTracker(Config().db_path)
                self._send_json({"sessions": tr.rebuild()})
            elif path == "/forget":
                target = data.get("target")
                if not isinstance(target, str) or not target.strip():
                    self.send_error(400, "Parameter 'target' (session id or project folder) is required")
                    return
                out = (tracker_mod.TRACKER.forget(target.strip()) if tracker_mod.TRACKER
                       else sessions.forget(target.strip()))
                self._send_json(out)
            elif path == "/handoff":
                sid = data.get("session")
                if not isinstance(sid, str) or not sid:
                    self.send_error(400, "Parameter 'session' is required")
                    return
                packet = tracker_mod.build_handoff(store, sid, str(data.get("to_agent") or ""))
                if packet is None:
                    self.send_error(404, "Unknown session")
                    return
                self._send_json(packet)
            else:
                sid = path[len("/sessions/"):-len("/digest")]
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    self.send_error(400, "Parameter 'text' must be a non-empty string")
                    return
                by = str(data.get("by") or "client")
                rec = (tracker_mod.TRACKER.set_digest(sid, text.strip(), by) if tracker_mod.TRACKER
                       else sessions.set_digest(sid, text.strip(), by))
                if rec is None:
                    self.send_error(404, "Unknown session")
                    return
                self._send_json({"session": tracker_mod.session_summary(rec)})
        except ConnectionError as e:
            logger.info(f"Connection closed by client during POST {path}: {e}")
        except Exception as e:
            logger.error(f"Error in POST {path}: {e}", exc_info=True)
            self.send_error(500, str(e))


def run_api_server(host="localhost", port=None):
    if port is None:
        port = int(os.environ.get("SHADOW_LINK_PORT", 5000))
    # Threaded: a slow prediction (local LLM, embeddings) must not stall
    # captures, feedback or the proxy's short nudge lookups.
    server = ThreadingHTTPServer((host, port), ShadowLinkAPIHandler)
    server.daemon_threads = True
    logger.info(f"API Server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
