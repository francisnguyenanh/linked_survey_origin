"""
Mapper routes — Phase 1: Survey mapping and branch discovery endpoints.

Blueprint prefix: /api/mapper
"""

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.services.browser_service import BrowserService
from app.services.mapper_service import BranchingMapperService, MapperService
from app.services.auto_mapping import AutoMappingEngine

logger = logging.getLogger(__name__)

mapper_bp = Blueprint("mapper_bp", __name__)

# In-process session store: session_id -> {browser_service, mapper, page, context}
_active_sessions: dict[str, dict] = {}


def _maps_dir() -> Path:
    return Path(current_app.config["MAPS_DIR"])


def _load_or_init_map(survey_id: str) -> dict:
    """Load an existing map JSON or return a blank scaffold."""
    path = _maps_dir() / f"{survey_id}.map.json"
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {
        "schema_version": "1.1",
        "survey_id": survey_id,
        "base_url": "",
        "url_params": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pages": [],
        "branch_tree": {"root_page_id": None, "nodes": {}},
        "discovery_sessions": [],
        "coverage_stats": {},
    }


def _save_map(survey_id: str, survey_map: dict):
    path = _maps_dir() / f"{survey_id}.map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(survey_map, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Standard mapping endpoints
# ---------------------------------------------------------------------------

@mapper_bp.route("/start", methods=["POST"])
def start_mapping():
    """
    Launch visible browser and navigate to the survey URL.

    Body: { "survey_url": str, "headless": bool }
    Response: { "session_id": str, "status": "ready" }
    """
    data = request.get_json(force=True)
    survey_url = data.get("survey_url", "")
    headless = data.get("headless", False)

    if not survey_url:
        return jsonify({"error": "survey_url is required"}), 400

    session_id = str(uuid.uuid4())[:12]
    browser_service = BrowserService(headless=headless)

    loop = asyncio.new_event_loop()

    async def _start():
        context, page = await browser_service.create_context()
        await browser_service.navigate_with_retry(page, survey_url)
        return context, page

    try:
        context, page = loop.run_until_complete(_start())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    _active_sessions[session_id] = {
        "browser_service": browser_service,
        "context": context,
        "page": page,
        "loop": loop,
        "survey_url": survey_url,
        "pages_recorded": [],
    }

    return jsonify({"session_id": session_id, "status": "ready"}), 200


@mapper_bp.route("/scan-page", methods=["POST"])
def scan_page():
    """
    Scan the current page in an active mapping session.

    Body: { "session_id": str }
    Response: { "page_data": dict, "fingerprint": str }
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = _active_sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    loop = session["loop"]
    page = session["page"]
    browser_service = session["browser_service"]
    mapper = MapperService(browser_service)

    try:
        page_data = loop.run_until_complete(mapper.scan_current_page(page))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({
        "page_data": page_data,
        "fingerprint": page_data.get("page_fingerprint"),
    }), 200


@mapper_bp.route("/record-page", methods=["POST"])
def record_page():
    """
    Append a scanned page to the in-progress survey map for a session.

    Body: { "session_id": str, "page_data": dict }
    Response: { "pages_recorded": int }
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    page_data = data.get("page_data", {})
    session = _active_sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    session["pages_recorded"].append(page_data)
    return jsonify({"pages_recorded": len(session["pages_recorded"])}), 200


@mapper_bp.route("/finalize", methods=["POST"])
def finalize_map():
    """
    Save the complete survey map to disk.

    Body: { "session_id": str, "survey_id": str }
    Response: { "filepath": str, "total_pages": int, "total_questions": int }
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    survey_id = data.get("survey_id", f"survey_{session_id}")
    session = _active_sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    pages = session.get("pages_recorded", [])
    total_questions = sum(len(p.get("questions", [])) for p in pages)

    survey_map = {
        "schema_version": "1.1",
        "survey_id": survey_id,
        "base_url": session.get("survey_url", ""),
        "url_params": {"uid": "{uid_placeholder}"},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pages": [
            dict(p, page_id=f"page_{i+1:03d}", page_index=i)
            for i, p in enumerate(pages)
        ],
        "branch_tree": {"root_page_id": None, "nodes": {}},
        "discovery_sessions": [],
        "coverage_stats": {},
    }

    filepath = str(_maps_dir() / f"{survey_id}.map.json")
    _save_map(survey_id, survey_map)

    # Clean up session
    loop = session["loop"]
    browser_service = session["browser_service"]
    loop.run_until_complete(browser_service.close_all())
    loop.close()
    del _active_sessions[session_id]

    return jsonify({
        "filepath": filepath,
        "total_pages": len(pages),
        "total_questions": total_questions,
    }), 200


@mapper_bp.route("/maps", methods=["GET"])
def list_maps():
    """List all saved survey maps."""
    maps = []
    for fp in sorted(_maps_dir().glob("*.map.json")):
        try:
            with open(fp, encoding="utf-8") as fh:
                data = json.load(fh)
            maps.append({
                "survey_id": data.get("survey_id"),
                "base_url": data.get("base_url"),
                "created_at": data.get("created_at"),
                "page_count": len(data.get("pages", [])),
                "coverage_pct": data.get("coverage_stats", {}).get("estimated_coverage_pct", 0),
            })
        except Exception:
            pass
    return jsonify(maps), 200


@mapper_bp.route("/maps/<survey_id>", methods=["DELETE"])
def delete_map(survey_id: str):
    """Delete a survey map by ID."""
    path = _maps_dir() / f"{survey_id}.map.json"
    if path.exists():
        path.unlink()
        return jsonify({"deleted": True}), 200
    return jsonify({"deleted": False}), 404


# ---------------------------------------------------------------------------
# Branch discovery endpoints
# ---------------------------------------------------------------------------

@mapper_bp.route("/session/start", methods=["POST"])
def session_start():
    """
    Start a new branch discovery session.

    Body: { "survey_id": str }
    Response: { "session_id": str, "existing_branch_count": int }
    """
    data = request.get_json(force=True)
    survey_id = data.get("survey_id")
    if not survey_id:
        return jsonify({"error": "survey_id is required"}), 400

    survey_map = _load_or_init_map(survey_id)
    browser_service = BrowserService(headless=False)
    mapper = BranchingMapperService(browser_service, survey_map)

    session_id = mapper.start_discovery_session()

    # Reuse the existing session store under the discovery session_id
    _active_sessions[session_id] = {
        "type": "discovery",
        "survey_id": survey_id,
        "mapper": mapper,
        "browser_service": browser_service,
        "loop": asyncio.new_event_loop(),
        "survey_map": survey_map,
    }

    nodes = survey_map.get("branch_tree", {}).get("nodes", {})
    total_branches = sum(len(n.get("outgoing_branches", [])) for n in nodes.values())

    return jsonify({
        "session_id": session_id,
        "existing_branch_count": total_branches,
    }), 200


@mapper_bp.route("/session/end", methods=["POST"])
def session_end():
    """
    End a discovery session and save updated map.

    Body: { "session_id": str, "result": str }
    Response: { "new_pages": int, "new_branches": int, "coverage_pct": float }
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    result = data.get("result", "aborted")

    session = _active_sessions.get(session_id)
    if not session or session.get("type") != "discovery":
        return jsonify({"error": "Discovery session not found"}), 404

    mapper: BranchingMapperService = session["mapper"]
    mapper.end_discovery_session(result)

    survey_id = session["survey_id"]
    _save_map(survey_id, mapper.survey_map)

    stats = mapper.survey_map.get("coverage_stats", {})
    del _active_sessions[session_id]

    return jsonify({
        "new_pages": stats.get("total_pages_discovered", 0),
        "new_branches": stats.get("total_branches_discovered", 0),
        "coverage_pct": stats.get("estimated_coverage_pct", 0),
    }), 200


@mapper_bp.route("/page/record-with-answers", methods=["POST"])
def record_page_with_answers():
    """
    Scan current page, check branch status, update branch_tree.

    Body: {
        "session_id": str,
        "previous_page_id": str | null,
        "answers_on_previous_page": dict | null,
        "current_page_data": dict
    }
    Response: {
        "status": "new"|"known"|"conflict",
        "page_id": str,
        "is_new_branch": bool,
        "unexplored_options": list,
        "suggested_next_attempts": list
    }
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    previous_answers = data.get("answers_on_previous_page")

    session = _active_sessions.get(session_id)
    if not session or session.get("type") != "discovery":
        return jsonify({"error": "Discovery session not found"}), 404

    mapper: BranchingMapperService = session["mapper"]
    current_page_data = data.get("current_page_data", {})

    # Patch fingerprint into page data if not present
    if not current_page_data.get("page_fingerprint"):
        current_page_data["page_fingerprint"] = mapper.compute_fingerprint(
            current_page_data.get("questions", [])
        )

    # Simulate record_page_with_branch_check without a live page
    fingerprint = current_page_data["page_fingerprint"]
    nodes = mapper.survey_map["branch_tree"].get("nodes", {})

    existing_node = next(
        (n for n in nodes.values() if n.get("fingerprint") == fingerprint), None
    )
    parent_page_id = mapper.current_session_path[-1] if mapper.current_session_path else None
    status = "new" if not existing_node else "known"
    is_new_branch = False

    if existing_node is None:
        page_id = f"page_{len(nodes) + 1:03d}"
        nodes[page_id] = {
            "page_id": page_id,
            "fingerprint": fingerprint,
            "discovered_count": 1,
            "parent_branch_ids": [],
            "outgoing_branches": [],
            "page_data": current_page_data,
        }
        if not mapper.survey_map["branch_tree"].get("root_page_id"):
            mapper.survey_map["branch_tree"]["root_page_id"] = page_id
        if parent_page_id and previous_answers:
            branch_id = mapper.merge_new_branch(parent_page_id, previous_answers, current_page_data)
            nodes[page_id]["parent_branch_ids"].append(branch_id)
            is_new_branch = True
        mapper.current_session_path.append(page_id)
    else:
        page_id = existing_node["page_id"]
        existing_node["discovered_count"] = existing_node.get("discovered_count", 0) + 1
        mapper.current_session_path.append(page_id)
        if parent_page_id and previous_answers:
            known = [b.get("trigger_answers") for b in existing_node.get("outgoing_branches", [])]
            if previous_answers not in known:
                mapper.merge_new_branch(parent_page_id, previous_answers, current_page_data)
                is_new_branch = True

    survey_id = session["survey_id"]
    mapper.survey_map["coverage_stats"] = mapper.compute_coverage_stats()
    _save_map(survey_id, mapper.survey_map)

    unexplored = mapper.get_unexplored_options(page_id)

    return jsonify({
        "status": status,
        "page_id": page_id,
        "is_new_branch": is_new_branch,
        "unexplored_options": unexplored,
        "suggested_next_attempts": unexplored[:3],
    }), 200


@mapper_bp.route("/coverage/<survey_id>", methods=["GET"])
def get_coverage(survey_id: str):
    """Return coverage statistics and branch tree summary for a survey."""
    path = _maps_dir() / f"{survey_id}.map.json"
    if not path.exists():
        return jsonify({"error": "Survey map not found"}), 404

    with open(path, encoding="utf-8") as fh:
        survey_map = json.load(fh)

    browser_service = BrowserService()
    mapper = BranchingMapperService(browser_service, survey_map)
    stats = mapper.compute_coverage_stats()
    summary = mapper.export_branch_tree_summary()

    return jsonify({"coverage_stats": stats, "branch_tree_summary": summary}), 200


@mapper_bp.route("/branch/promote-to-pattern", methods=["POST"])
def promote_to_pattern():
    """
    Convert a branch path into a pattern JSON.

    Body: { "survey_id": str, "branch_path": list, "pattern_name": str, "auto_fill": bool }
    Response: { "pattern_id": str, "pattern": dict }
    """
    from app.services.pattern_service import PatternService

    data = request.get_json(force=True)
    survey_id = data.get("survey_id")
    branch_path = data.get("branch_path", [])
    pattern_name = data.get("pattern_name", "auto_pattern")
    auto_fill = data.get("auto_fill", True)

    path = _maps_dir() / f"{survey_id}.map.json"
    if not path.exists():
        return jsonify({"error": "Survey map not found"}), 404

    with open(path, encoding="utf-8") as fh:
        survey_map = json.load(fh)

    browser_service = BrowserService()
    mapper = BranchingMapperService(browser_service, survey_map)
    pattern = mapper.promote_branch_to_pattern(branch_path, pattern_name, auto_fill)

    pattern_service = PatternService(current_app.config["PATTERNS_DIR"])
    pattern_id = pattern_service.save_pattern(pattern)

    return jsonify({"pattern_id": pattern_id, "pattern": pattern}), 201


@mapper_bp.route("/branch/unexplored/<survey_id>", methods=["GET"])
def get_unexplored(survey_id: str):
    """Return pages with unexplored answer combinations and suggestions."""
    path = _maps_dir() / f"{survey_id}.map.json"
    if not path.exists():
        return jsonify({"error": "Survey map not found"}), 404

    with open(path, encoding="utf-8") as fh:
        survey_map = json.load(fh)

    browser_service = BrowserService()
    mapper = BranchingMapperService(browser_service, survey_map)
    nodes = survey_map.get("branch_tree", {}).get("nodes", {})

    result = []
    for page_id in nodes:
        unexplored = mapper.get_unexplored_options(page_id)
        if unexplored:
            result.append({"page_id": page_id, "suggested_answers": unexplored[:5]})

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Auto-Mapping endpoints (Section 3.2c)
# ---------------------------------------------------------------------------

# In-process job store: job_id -> {status, engine, result, error, ...}
_auto_jobs: dict[str, dict] = {}


def _patterns_dir() -> Path:
    return Path(current_app.config["PATTERNS_DIR"])


def _run_auto_mapping_thread(
    job_id: str,
    survey_url: str,
    safe_uid: str,
    survey_id: str,
    max_depth: int,
    max_branches: int,
    uid_pool: list,
    maps_dir: Path,
    patterns_dir: Path,
    socketio,
) -> None:
    """Background thread that runs the async AutoMappingEngine."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        engine = AutoMappingEngine(
            maps_dir=maps_dir,
            patterns_dir=patterns_dir,
            socketio=socketio,
            headless=True,
        )
        _auto_jobs[job_id]["engine"] = engine
        _auto_jobs[job_id]["status"] = "running"

        result = loop.run_until_complete(
            engine.run(
                job_id=job_id,
                survey_url=survey_url,
                safe_uid=safe_uid,
                survey_id=survey_id,
                max_depth=max_depth,
                max_branches=max_branches,
                uid_pool_for_patterns=uid_pool,
            )
        )
        _auto_jobs[job_id]["status"] = "complete"
        _auto_jobs[job_id]["result"] = result
    except Exception as exc:
        _auto_jobs[job_id]["status"] = "error"
        _auto_jobs[job_id]["error"] = str(exc)
        if socketio:
            socketio.emit("mapping_error", {"job_id": job_id, "message": str(exc)})
    finally:
        loop.close()


@mapper_bp.route("/auto/start", methods=["POST"])
def auto_start():
    """
    Launch Auto-Mapping Engine in the background.

    Body: {
      "survey_url":  str,       # Full survey URL to map
      "safe_uid":    str,       # Dedicated mapping UID (not a real respondent UID)
      "survey_id":   str,       # Slug used for output filenames
      "max_depth":   int,       # optional, default 20
      "max_branches": int,      # optional, default 200
      "uid_pool":    list[str], # optional, UIDs to embed in generated patterns
    }
    Response: { "job_id": str, "status": "started" }
    """
    from app.extensions import socketio as sio

    data = request.get_json(force=True)
    survey_url = data.get("survey_url", "").strip()
    safe_uid = data.get("safe_uid", "").strip()
    survey_id = data.get("survey_id", "").strip()

    if not survey_url:
        return jsonify({"error": "survey_url is required"}), 400
    if not safe_uid:
        return jsonify({"error": "safe_uid is required"}), 400
    if not survey_id:
        survey_id = f"auto_{uuid.uuid4().hex[:8]}"

    max_depth = int(data.get("max_depth", 20))
    max_branches = int(data.get("max_branches", 200))
    uid_pool = data.get("uid_pool", [])

    job_id = str(uuid.uuid4())[:12]
    _auto_jobs[job_id] = {
        "job_id": job_id,
        "survey_id": survey_id,
        "survey_url": survey_url,
        "safe_uid": safe_uid,
        "status": "queued",
        "branches_explored": 0,
        "pages_found": 0,
        "result": None,
        "error": None,
        "engine": None,
    }

    maps_dir = _maps_dir()
    patterns_dir = _patterns_dir()

    t = threading.Thread(
        target=_run_auto_mapping_thread,
        args=(
            job_id, survey_url, safe_uid, survey_id,
            max_depth, max_branches, uid_pool,
            maps_dir, patterns_dir, sio,
        ),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "started", "survey_id": survey_id}), 202


@mapper_bp.route("/auto/status/<job_id>", methods=["GET"])
def auto_status(job_id: str):
    """
    Return current status of an auto-mapping job.

    Response: {
      "job_id": str,
      "status": "queued|running|complete|error",
      "survey_id": str,
      "branches_explored": int,
      "pages_found": int,
      "elapsed_seconds": float,
      "result": dict | null,
      "error": str | null,
    }
    """
    job = _auto_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Pull live stats from the engine if running
    engine: AutoMappingEngine | None = job.get("engine")
    result = job.get("result") or {}

    return jsonify({
        "job_id": job_id,
        "survey_id": job.get("survey_id"),
        "status": job.get("status", "unknown"),
        "branches_explored": result.get("branches_explored", 0),
        "pages_found": result.get("total_pages", 0),
        "result": result if job.get("status") == "complete" else None,
        "error": job.get("error"),
    }), 200


@mapper_bp.route("/auto/stop/<job_id>", methods=["POST"])
def auto_stop(job_id: str):
    """
    Send a graceful stop signal to a running auto-mapping job.

    Response: { "stopped": bool }
    """
    job = _auto_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    engine: AutoMappingEngine | None = job.get("engine")
    stopped = False
    if engine:
        stopped = engine.stop(job_id)
        job["status"] = "stopping"

    return jsonify({"stopped": stopped}), 200


@mapper_bp.route("/auto/preview/<job_id>", methods=["GET"])
def auto_preview(job_id: str):
    """
    Return an ASCII tree representation of the graph being built.

    Response: { "tree_summary": str, "graph_stats": dict }
    """
    job = _auto_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Try to read the partial graph from disk
    survey_id = job.get("survey_id", "")
    graph_path = _maps_dir() / f"{survey_id}.graph.json"
    tree_summary = "(no data yet)"
    graph_stats: dict = {}

    if graph_path.exists():
        try:
            from app.services.auto_mapping.survey_graph import SurveyGraph
            sg = SurveyGraph.load(graph_path)
            tree_summary = sg.to_text_tree()
            graph_stats = sg.get_stats()
        except Exception as exc:
            tree_summary = f"(error reading graph: {exc})"

    return jsonify({"tree_summary": tree_summary, "graph_stats": graph_stats}), 200


@mapper_bp.route("/auto/estimate", methods=["POST"])
def auto_estimate():
    """
    Estimate how many branches and minutes a mapping job would take.

    Body: { "trigger_option_matrix": { q_id: [option_values, ...], ... } }
    Response: { "estimated_branches": int, "estimated_minutes": float, "warning": str|null }
    """
    data = request.get_json(force=True)
    matrix = data.get("trigger_option_matrix", {})

    engine = AutoMappingEngine(
        maps_dir=_maps_dir(),
        patterns_dir=_patterns_dir(),
    )
    estimate = engine.estimate_time(matrix)
    return jsonify(estimate), 200


# ---------------------------------------------------------------------------
# Hybrid-Mapping routes
# ---------------------------------------------------------------------------

_hybrid_jobs: dict[str, dict] = {}


def _run_hybrid_thread(
    job_id: str,
    survey_url: str,
    uid_pool: list[str],
    survey_id: str,
    headless: bool,
    maps_dir: Path,
    socketio,
) -> None:
    """Background thread that runs the async HybridMapper."""
    from app.services.auto_mapping.hybrid_mapper import HybridMapper
    from app.services.browser_service import BrowserService

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        browser = BrowserService(headless=headless)

        async def _run():
            try:
                mapper = HybridMapper(
                    browser_service=browser,
                    uid_pool=uid_pool,
                    socketio=socketio,
                    headless=headless,
                )
                _hybrid_jobs[job_id]["mapper"] = mapper
                _hybrid_jobs[job_id]["status"] = "running"

                graph = await mapper.map_survey(
                    survey_url=survey_url,
                    survey_id=survey_id,
                    save_dir=maps_dir,
                )
                stats = graph.get_stats()
                _hybrid_jobs[job_id]["status"] = "complete"
                _hybrid_jobs[job_id]["result"] = {
                    "pages": stats.get("total_pages", 0),
                    "branches": stats.get("total_branches", 0),
                    "uids_used": mapper.get_used_uids(),
                    "uids_remaining": mapper.get_unused_uids(),
                }
            finally:
                await browser.close_all()

        loop.run_until_complete(_run())
    except Exception as exc:
        _hybrid_jobs[job_id]["status"] = "error"
        _hybrid_jobs[job_id]["error"] = str(exc)
        if socketio:
            socketio.emit("mapping_error", {"job_id": job_id, "message": str(exc)})
    finally:
        loop.close()


@mapper_bp.route("/hybrid/start", methods=["POST"])
def hybrid_start():
    """
    Launch HybridMapper in the background.

    Body: {
      "survey_url":  str,
      "uid_pool":    list[str],   # e.g. ["re1280","re1281","re1282"]
      "survey_id":   str,         # optional slug for output filenames
      "headless":    bool,        # optional, default true
    }
    Response: { "job_id": str, "survey_id": str }
    """
    from app.extensions import socketio as sio

    data = request.get_json(force=True)
    survey_url = data.get("survey_url", "").strip()
    uid_pool = data.get("uid_pool", [])
    survey_id = data.get("survey_id", "").strip()
    headless = bool(data.get("headless", True))

    if not survey_url:
        return jsonify({"error": "survey_url is required"}), 400
    if not uid_pool:
        return jsonify({"error": "uid_pool must contain at least one UID"}), 400
    if not survey_id:
        survey_id = f"hybrid_{uuid.uuid4().hex[:8]}"

    job_id = str(uuid.uuid4())[:12]
    _hybrid_jobs[job_id] = {
        "job_id": job_id,
        "survey_id": survey_id,
        "survey_url": survey_url,
        "uid_pool": uid_pool,
        "status": "queued",
        "mapper": None,
        "result": None,
        "error": None,
    }

    t = threading.Thread(
        target=_run_hybrid_thread,
        args=(job_id, survey_url, uid_pool, survey_id, headless, _maps_dir(), sio),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "survey_id": survey_id}), 202


@mapper_bp.route("/hybrid/status/<job_id>", methods=["GET"])
def hybrid_status(job_id: str):
    """
    Return current status of a hybrid-mapping job.

    Response: {
      "job_id": str,
      "status": "queued|running|complete|error",
      "pages_found": int,
      "uids_used": list[str],
      "uids_remaining": list[str],
      "current_strategy": str | null,
      "result": dict | null,
      "error": str | null,
    }
    """
    job = _hybrid_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    mapper = job.get("mapper")
    result = job.get("result") or {}

    pages_found = 0
    uids_used: list = []
    uids_remaining: list = []

    if mapper is not None:
        uids_used = mapper.get_used_uids()
        uids_remaining = mapper.get_unused_uids()
        pages_found = mapper.graph.get_stats().get("total_pages", 0)
    elif result:
        uids_used = result.get("uids_used", [])
        uids_remaining = result.get("uids_remaining", [])
        pages_found = result.get("pages", 0)

    return jsonify({
        "job_id": job_id,
        "survey_id": job.get("survey_id"),
        "status": job.get("status", "unknown"),
        "pages_found": pages_found,
        "uids_used": uids_used,
        "uids_remaining": uids_remaining,
        "current_strategy": None,
        "result": result if job.get("status") == "complete" else None,
        "error": job.get("error"),
    }), 200


@mapper_bp.route("/hybrid/uid-report/<job_id>", methods=["GET"])
def hybrid_uid_report(job_id: str):
    """
    Return the UID usage report for a completed hybrid-mapping job.

    Response: {
      "used_for_mapping":        list[str],
      "available_for_execution": list[str],
      "patterns_discovered":     int,
    }
    """
    job = _hybrid_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    mapper = job.get("mapper")
    result = job.get("result") or {}

    patterns_discovered = 0
    survey_id = job.get("survey_id", "")
    patterns_path = _patterns_dir() / f"{survey_id}.patterns.json"
    if patterns_path.exists():
        try:
            import json as _json
            patterns = _json.loads(patterns_path.read_text(encoding="utf-8"))
            patterns_discovered = len(patterns) if isinstance(patterns, list) else 0
        except Exception:
            pass

    if mapper is not None:
        used = mapper.get_used_uids()
        available = mapper.get_unused_uids()
    else:
        used = result.get("uids_used", job.get("uid_pool", []))
        available = result.get("uids_remaining", [])

    return jsonify({
        "used_for_mapping": used,
        "available_for_execution": available,
        "patterns_discovered": patterns_discovered,
    }), 200


# ---------------------------------------------------------------------------
# Shadow-Mode routes
# ---------------------------------------------------------------------------

_shadow_sessions: dict[str, dict] = {}


def _run_shadow_thread(
    session_id: str,
    survey_url: str,
    uid: str,
    survey_id: str,
    assisted: bool,
    maps_dir: Path,
    socketio,
) -> None:
    """Background thread that runs a ShadowMappingSession."""
    from app.services.auto_mapping.shadow_observer import ShadowMappingSession
    from app.services.auto_mapping.survey_graph import SurveyGraph

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        graph = SurveyGraph()
        save_path = maps_dir / f"{survey_id}.graph.json"

        session = ShadowMappingSession(
            survey_graph=graph,
            socketio=socketio,
            save_path=save_path,
            assisted=assisted,
        )
        _shadow_sessions[session_id]["session"] = session
        _shadow_sessions[session_id]["status"] = "running"

        result = loop.run_until_complete(session.start(survey_url, uid))

        _shadow_sessions[session_id]["status"] = "complete"
        _shadow_sessions[session_id]["result"] = result
        _shadow_sessions[session_id]["pattern"] = result.get("pattern", {})
    except Exception as exc:
        _shadow_sessions[session_id]["status"] = "error"
        _shadow_sessions[session_id]["error"] = str(exc)
        if socketio:
            socketio.emit("shadow_error", {"session_id": session_id, "message": str(exc)})
    finally:
        # Best-effort: close visible browser
        sess = _shadow_sessions.get(session_id, {}).get("session")
        if sess:
            try:
                loop.run_until_complete(sess.close_browser())
            except Exception:
                pass
        loop.close()


@mapper_bp.route("/shadow/start", methods=["POST"])
def shadow_start():
    """
    Open a visible browser for the user to navigate while the bot observes.

    Body: {
      "survey_url": str,
      "uid":        str,
      "survey_id":  str,   # optional slug for output filenames
      "assisted":   bool,  # true = inject overlay with hints
    }
    Response: { "session_id": str, "status": "browser_opened" }
    """
    from app.extensions import socketio as sio

    data = request.get_json(force=True)
    survey_url = data.get("survey_url", "").strip()
    uid = data.get("uid", "").strip()
    survey_id = data.get("survey_id", "").strip()
    assisted = bool(data.get("assisted", True))

    if not survey_url:
        return jsonify({"error": "survey_url is required"}), 400
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    if not survey_id:
        survey_id = f"shadow_{uuid.uuid4().hex[:8]}"

    session_id = str(uuid.uuid4())[:12]
    _shadow_sessions[session_id] = {
        "session_id": session_id,
        "survey_id": survey_id,
        "survey_url": survey_url,
        "uid": uid,
        "assisted": assisted,
        "status": "starting",
        "session": None,
        "result": None,
        "pattern": None,
        "error": None,
    }

    t = threading.Thread(
        target=_run_shadow_thread,
        args=(session_id, survey_url, uid, survey_id, assisted, _maps_dir(), sio),
        daemon=True,
    )
    t.start()

    return jsonify({"session_id": session_id, "status": "browser_opened", "survey_id": survey_id}), 202


@mapper_bp.route("/shadow/live/<session_id>", methods=["GET"])
def shadow_live(session_id: str):
    """
    Real-time status snapshot for the running shadow session.

    Response: {
      "pages_found": int,
      "current_page": str | null,
      "coverage_pct": int,
      "unexplored_suggestions": list,
      "session_path_length": int,
      "status": str
    }
    """
    entry = _shadow_sessions.get(session_id)
    if not entry:
        return jsonify({"error": "Session not found"}), 404

    sess = entry.get("session")
    base = {
        "session_id": session_id,
        "survey_id": entry.get("survey_id"),
        "status": entry.get("status", "unknown"),
    }

    if sess:
        base.update(sess.get_live_status())
    else:
        base.update({"pages_found": 0, "current_page": None,
                     "coverage_pct": 0, "unexplored_suggestions": [],
                     "session_path_length": 0})

    return jsonify(base), 200


@mapper_bp.route("/shadow/stop/<session_id>", methods=["POST"])
def shadow_stop(session_id: str):
    """
    Stop the shadow session and return the generated pattern.

    Response: {
      "pattern_saved": bool,
      "pattern_id": str | null,
      "pages_mapped": int,
      "coverage_pct": int,
      "unexplored_remaining": int
    }
    """
    from app.extensions import socketio as sio

    entry = _shadow_sessions.get(session_id)
    if not entry:
        return jsonify({"error": "Session not found"}), 404

    sess = entry.get("session")
    if sess:
        # Signal the asyncio event loop inside the background thread
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(sess.stop())
            loop.close()
        except Exception:
            pass

    # Poll briefly for completion
    import time as _time
    for _ in range(20):
        if entry.get("status") in ("complete", "error"):
            break
        _time.sleep(0.3)

    result = entry.get("result") or {}
    pattern = entry.get("pattern") or {}
    coverage = result.get("coverage", {"coverage_pct": 0, "total_pages": 0})

    # Save pattern to disk if we have one
    pattern_saved = False
    pattern_id = pattern.get("pattern_id")
    if pattern:
        try:
            survey_id = entry.get("survey_id", "shadow")
            patterns_file = _patterns_dir() / f"{survey_id}.patterns.json"
            existing: list = []
            if patterns_file.exists():
                import json as _j
                existing = _j.loads(patterns_file.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            existing.append(pattern)
            import json as _j
            patterns_file.write_text(
                _j.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            pattern_saved = True
        except Exception as exc:
            logger.warning("shadow_stop: pattern save failed: %s", exc)

    # Count remaining unexplored
    unexplored_remaining = 0
    if sess and sess.observer:
        for node_id, node_data in sess.observer.graph.G.nodes(data=True):
            page_data = {"questions": node_data.get("questions", [])}
            unexplored_remaining += len(
                sess.observer._get_unexplored_suggestions(node_id, page_data)
            )

    return jsonify({
        "pattern_saved": pattern_saved,
        "pattern_id": pattern_id,
        "pages_mapped": coverage.get("total_pages", 0),
        "coverage_pct": coverage.get("coverage_pct", 0),
        "unexplored_remaining": unexplored_remaining,
    }), 200


@mapper_bp.route("/shadow/save-pattern/<session_id>", methods=["POST"])
def shadow_save_pattern(session_id: str):
    """
    Explicitly save the session pattern with a custom name.

    Body: { "pattern_name": str }
    Response: { "pattern_id": str, "pattern_file": str }
    """
    entry = _shadow_sessions.get(session_id)
    if not entry:
        return jsonify({"error": "Session not found"}), 404

    pattern = entry.get("pattern") or {}
    if not pattern:
        return jsonify({"error": "No pattern available for this session"}), 400

    data = request.get_json(force=True) or {}
    custom_name = data.get("pattern_name", "").strip()
    if custom_name:
        pattern["pattern_name"] = custom_name

    survey_id = entry.get("survey_id", "shadow")
    patterns_file = _patterns_dir() / f"{survey_id}.patterns.json"
    existing: list = []
    if patterns_file.exists():
        try:
            import json as _j
            existing = _j.loads(patterns_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []

    # Replace pattern with same ID if already present
    pid = pattern.get("pattern_id")
    existing = [p for p in existing if p.get("pattern_id") != pid]
    existing.append(pattern)

    import json as _j
    patterns_file.write_text(
        _j.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return jsonify({
        "pattern_id": pid,
        "pattern_file": str(patterns_file),
    }), 200
