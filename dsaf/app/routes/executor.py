"""
Executor routes — Phase 3: Automation run control endpoints.

Blueprint prefix: /api/executor
"""

import asyncio
import json
import threading
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.extensions import socketio
from app.services.browser_service import BrowserService
from app.services.executor_service import ExecutorService
from app.services.pattern_service import PatternService

executor_bp = Blueprint("executor_bp", __name__)

# batch_id -> { status, total, completed, succeeded, failed, current_uid, executor }
_batches: dict[str, dict] = {}


def _maps_dir() -> Path:
    return Path(current_app.config["MAPS_DIR"])


def _load_survey_map(survey_id: str) -> dict | None:
    path = _maps_dir() / f"{survey_id}.map.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _run_batch_thread(
    batch_id: str,
    survey_map: dict,
    pattern: dict,
    uid_list: list,
    run_count: int,
    concurrency: int,
    proxy_url: str | None,
    data_dir: Path,
):
    """Thread target for running a batch asynchronously."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    browser_service = BrowserService(headless=True, proxy_url=proxy_url)
    executor = ExecutorService(browser_service, socketio, data_dir)
    _batches[batch_id]["executor"] = executor

    try:
        loop.run_until_complete(
            executor.run_batch(survey_map, pattern, uid_list, run_count, batch_id, concurrency)
        )
        _batches[batch_id]["status"] = "completed"
    except Exception as exc:
        _batches[batch_id]["status"] = "error"
        _batches[batch_id]["error"] = str(exc)
    finally:
        loop.run_until_complete(browser_service.close_all())
        loop.close()


@executor_bp.route("/run", methods=["POST"])
def start_run():
    """
    Start a batch of survey automation runs in a background thread.

    Body: {
        "survey_id": str,
        "pattern_id": str,
        "run_count": int,
        "concurrency": int,
        "proxy_url": str | null
    }
    Response: { "batch_id": str, "status": "started" }
    """
    data = request.get_json(force=True)
    survey_id = data.get("survey_id")
    pattern_id = data.get("pattern_id")
    run_count = max(1, min(int(data.get("run_count", 1)), 1000))
    concurrency = max(1, min(int(data.get("concurrency", 1)), 3))
    proxy_url = data.get("proxy_url") or None

    if not survey_id or not pattern_id:
        return jsonify({"error": "survey_id and pattern_id are required"}), 400

    survey_map = _load_survey_map(survey_id)
    if not survey_map:
        return jsonify({"error": "Survey map not found"}), 404

    pattern_svc = PatternService(current_app.config["PATTERNS_DIR"])
    pattern = pattern_svc.get_pattern(pattern_id)
    if not pattern:
        return jsonify({"error": "Pattern not found"}), 404

    batch_id = str(uuid.uuid4())[:12]
    uid_list = pattern.get("uid_pool", [])

    _batches[batch_id] = {
        "batch_id": batch_id,
        "survey_id": survey_id,
        "pattern_id": pattern_id,
        "total": run_count,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "status": "running",
        "current_uid": uid_list[0] if uid_list else "",
        "executor": None,
    }

    data_dir = Path(current_app.config["DATA_DIR"])
    thread = threading.Thread(
        target=_run_batch_thread,
        args=(batch_id, survey_map, pattern, uid_list, run_count,
              concurrency, proxy_url, data_dir),
        daemon=True,
    )
    thread.start()

    return jsonify({"batch_id": batch_id, "status": "started"}), 202


@executor_bp.route("/status/<batch_id>", methods=["GET"])
def batch_status(batch_id: str):
    """Return current status of a running or completed batch."""
    batch = _batches.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify({k: v for k, v in batch.items() if k != "executor"}), 200


@executor_bp.route("/stop/<batch_id>", methods=["POST"])
def stop_batch(batch_id: str):
    """
    Gracefully stop a running batch after the current run completes.

    Response: { "stopped": bool }
    """
    batch = _batches.get(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404

    executor: ExecutorService = batch.get("executor")
    if executor:
        executor.stop_batch(batch_id)
        batch["status"] = "stopping"
        return jsonify({"stopped": True}), 200

    return jsonify({"stopped": False}), 400


@executor_bp.route("/results/<batch_id>", methods=["GET"])
def batch_results(batch_id: str):
    """Return the list of RunResult objects for a completed batch."""
    results_path = Path(current_app.config["RESULTS_DIR"]) / f"{batch_id}.json"
    if not results_path.exists():
        return jsonify({"error": "Results not found"}), 404
    with open(results_path, encoding="utf-8") as fh:
        return jsonify(json.load(fh)), 200
