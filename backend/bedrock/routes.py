"""HTTP surface for Bedrock crossplay.

Four small endpoints, all scoped to one server:

    GET  /api/servers/<id>/bedrock            snapshot (also feeds the poller)
    GET  /api/servers/<id>/bedrock/progress   worker progress
    POST /api/servers/<id>/bedrock/enable     download + tunnel + patch
    POST /api/servers/<id>/bedrock/disable    restore builtin signaling

``enable``/``disable`` validate synchronously (fast 400/409 for a running or
unsupported server) and then hand the long part to a worker thread, exactly
like the installer routes.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from backend.managed import is_managed
from backend.utils.routes import require_server

from . import progress, service

bedrock_bp = Blueprint("bedrock", __name__, url_prefix="/api/servers")


def _managed_response():
    return jsonify({"error": "Bedrock crossplay is disabled in managed mode"}), 403


@bedrock_bp.route("/<server_id>/bedrock", methods=["GET"])
@require_server
def bedrock_status(server_id, server):
    return jsonify(service.get_state(server))


@bedrock_bp.route("/<server_id>/bedrock/progress", methods=["GET"])
@require_server
def bedrock_progress(server_id, server):
    entry = progress.get(server_id)
    return jsonify({**entry, "active": progress.is_active(server_id)})


@bedrock_bp.route("/<server_id>/bedrock/enable", methods=["POST"])
@require_server
def bedrock_enable(server_id, server):
    if is_managed():
        return _managed_response()
    try:
        state = service.enable(server_id, server)
    except service.BedrockError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    return jsonify({"success": True, **state})


@bedrock_bp.route("/<server_id>/bedrock/disable", methods=["POST"])
@require_server
def bedrock_disable(server_id, server):
    if is_managed():
        return _managed_response()
    try:
        state = service.disable(server_id, server)
    except service.BedrockError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    return jsonify({"success": True, **state})
