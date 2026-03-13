"""
Configurator routes — Phase 2: Pattern configuration endpoints.

Blueprint prefix: /api/config
"""

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.services.pattern_service import PatternService

configurator_bp = Blueprint("configurator_bp", __name__)


def _pattern_svc() -> PatternService:
    return PatternService(current_app.config["PATTERNS_DIR"])


def _maps_dir() -> Path:
    return Path(current_app.config["MAPS_DIR"])


def _load_survey_map(survey_id: str) -> dict | None:
    path = _maps_dir() / f"{survey_id}.map.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@configurator_bp.route("/survey/<survey_id>/questions", methods=["GET"])
def get_questions(survey_id: str):
    """
    Return a flattened list of all questions across all pages for a survey.

    Response: list of question dicts enriched with page_id and page_index.
    """
    survey_map = _load_survey_map(survey_id)
    if not survey_map:
        return jsonify({"error": "Survey map not found"}), 404

    flat: list[dict] = []
    for page in survey_map.get("pages", []):
        for question in page.get("questions", []):
            flat.append({
                **question,
                "page_id": page.get("page_id"),
                "page_index": page.get("page_index", 0),
            })

    return jsonify(flat), 200


@configurator_bp.route("/patterns", methods=["POST"])
def create_pattern():
    """
    Create and save a new pattern.

    Body: Complete pattern dict per schema Section 2.2.
    Response: { "pattern_id": str }
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    try:
        pattern_id = _pattern_svc().save_pattern(data)
        return jsonify({"pattern_id": pattern_id}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@configurator_bp.route("/patterns", methods=["GET"])
def list_patterns():
    """List all saved patterns with their metadata."""
    patterns = _pattern_svc().list_patterns()
    summary = [
        {
            "pattern_id": p.get("pattern_id"),
            "pattern_name": p.get("pattern_name"),
            "linked_survey_id": p.get("linked_survey_id"),
            "created_at": p.get("created_at"),
            "uid_count": len(p.get("uid_pool", [])),
        }
        for p in patterns
    ]
    return jsonify(summary), 200


@configurator_bp.route("/patterns/<pattern_id>", methods=["GET"])
def get_pattern(pattern_id: str):
    """Retrieve a full pattern by ID."""
    pattern = _pattern_svc().get_pattern(pattern_id)
    if not pattern:
        return jsonify({"error": "Pattern not found"}), 404
    return jsonify(pattern), 200


@configurator_bp.route("/patterns/<pattern_id>", methods=["PUT"])
def update_pattern(pattern_id: str):
    """
    Replace a pattern entirely.

    Body: Updated pattern dict.
    Response: { "updated": bool }
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    data["pattern_id"] = pattern_id
    try:
        _pattern_svc().save_pattern(data)
        return jsonify({"updated": True}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@configurator_bp.route("/patterns/<pattern_id>", methods=["DELETE"])
def delete_pattern(pattern_id: str):
    """Delete a pattern by ID."""
    deleted = _pattern_svc().delete_pattern(pattern_id)
    return jsonify({"deleted": deleted}), 200 if deleted else 404


@configurator_bp.route("/patterns/<pattern_id>/validate", methods=["POST"])
def validate_pattern(pattern_id: str):
    """
    Validate a pattern against its survey map.

    Body: { "survey_id": str }
    Response: { "valid": bool, "warnings": list[str] }
    """
    data = request.get_json(force=True)
    survey_id = data.get("survey_id")
    if not survey_id:
        return jsonify({"error": "survey_id is required"}), 400

    pattern = _pattern_svc().get_pattern(pattern_id)
    if not pattern:
        return jsonify({"error": "Pattern not found"}), 404

    survey_map = _load_survey_map(survey_id)
    if not survey_map:
        return jsonify({"error": "Survey map not found"}), 404

    warnings = _pattern_svc().validate_pattern(pattern, survey_map)
    return jsonify({"valid": len(warnings) == 0, "warnings": warnings}), 200
