"""
Flask Web Dashboard for Flight Price Checker
Local:   venv/bin/python app.py  →  http://localhost:5050
Vercel:  deployed automatically, cron hits /api/check every 6h
"""

import os
import sys
import json
import time
import logging
import threading
import schedule
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv

# ── Data directory: /tmp on serverless, local dir otherwise ──────────────────
IS_VERCEL = os.environ.get("VERCEL") == "1"
DATA_DIR  = Path("/tmp") if IS_VERCEL else Path(__file__).parent
load_dotenv(Path(__file__).parent / ".env")

# ── Import core checker as a module ──────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import flight_checker as fc

# Override data file paths for serverless environment
fc.HISTORY_FILE = DATA_DIR / "price_history.json"
fc.STATUS_FILE  = DATA_DIR / "status.json"

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
log = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────
_state = {
    "checking":      False,
    "next_check_at": None,
    "check_count":   0,
    "sse_listeners": [],
}
_state_lock = threading.Lock()

import queue

# ── SSE helpers ───────────────────────────────────────────────────────────────

def _broadcast(event: str, data: dict) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _state_lock:
        dead = []
        for q in _state["sse_listeners"]:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _state["sse_listeners"].remove(q)


# ── Background checker ────────────────────────────────────────────────────────

def _do_check() -> None:
    with _state_lock:
        if _state["checking"]:
            return
        _state["checking"] = True

    _broadcast("checking", {"checking": True})
    try:
        result = fc.run_check()
        with _state_lock:
            _state["check_count"] += 1
        if result:
            _broadcast("result", {
                "price_sek":  result["price_sek"],
                "airlines":   result["airlines"],
                "is_deal":    result["is_deal"],
                "timestamp":  result["timestamp"],
                "all_offers": result.get("all_offers", []),
            })
    finally:
        with _state_lock:
            _state["checking"] = False
        _broadcast("checking", {"checking": False})


def _scheduler_loop() -> None:
    # Schedule periodic checks but do NOT run one immediately on startup.
    # Running immediately on startup causes a race: Render kills the process
    # mid-check (SIGTERM), the finally block never fires, and `checking` stays
    # stuck at True until the next cold-start — blocking every manual trigger.
    next_time = datetime.now() + timedelta(hours=fc.CHECK_EVERY_HOURS)
    with _state_lock:
        _state["next_check_at"] = next_time.strftime("%Y-%m-%d %H:%M")
    schedule.every(fc.CHECK_EVERY_HOURS).hours.do(_do_check)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Startup: only run background scheduler when NOT on Vercel ────────────────
if not IS_VERCEL:
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        origin=fc.ORIGIN,
        destination=fc.DESTINATION,
        depart_date=fc.DEPART_DATE,
        return_date=fc.RETURN_DATE,
        price_limit=fc.PRICE_LIMIT,
        check_hours=fc.CHECK_EVERY_HOURS,
    )


@app.route("/api/status")
def api_status():
    status  = fc.load_status()
    history = fc.load_history()
    with _state_lock:
        checking      = _state["checking"]
        next_check_at = _state["next_check_at"]
        check_count   = _state["check_count"]

    return jsonify({
        "config": {
            "origin":      fc.ORIGIN,
            "destination": fc.DESTINATION,
            "depart_date": fc.DEPART_DATE,
            "return_date": fc.RETURN_DATE,
            "price_limit": fc.PRICE_LIMIT,
            "check_hours": fc.CHECK_EVERY_HOURS,
        },
        "current":       status,
        "history":       history[-50:],
        "checking":      checking,
        "next_check_at": next_check_at,
        "check_count":   check_count,
    })


@app.route("/api/check", methods=["POST", "GET"])
def api_check():
    """Trigger a price check. Always runs synchronously and returns the result.
    Running in-request (not a background thread) means the result is returned
    directly and there is no in-memory state that can get stuck between restarts."""
    with _state_lock:
        if _state["checking"]:
            # A scheduled check is already running — return latest saved status.
            return jsonify({"status": "already_running", "result": fc.load_status()})

    _do_check()   # blocks ~20-30 s; fine within gunicorn's 120 s timeout
    return jsonify({"status": "done", "result": fc.load_status()})


@app.route("/api/history")
def api_history():
    return jsonify(fc.load_history())


@app.route("/stream")
def stream():
    """SSE endpoint — only useful in local mode."""
    q: queue.Queue = queue.Queue(maxsize=20)
    with _state_lock:
        _state["sse_listeners"].append(q)

    def generate():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            with _state_lock:
                try:
                    _state["sse_listeners"].remove(q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Entry point (local dev only) ──────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n  ✈  Flight Price Monitor  –  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
