"""Flask Blueprint for token endpoints (validate and persist)."""
from flask import Blueprint, request, jsonify
from services import token_service

bp = Blueprint("token", __name__, url_prefix="/api/token")


@bp.route("/validate", methods=["POST"])
def validate_token():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token")
    try:
        ok, detail = token_service.validate_token(token)
        status = "VALID" if ok else "INVALID"
        return jsonify({"ok": ok, "status": status, "detail": detail}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/persist", methods=["POST"])
def persist_token():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("token_input") or ""
    also_write_env = bool(payload.get("also_write_env", False))
    try:
        ok, message = token_service.persist_token(raw, also_write_env)
        status_code = 200 if ok else 400
        return jsonify({"ok": ok, "message": message}), status_code
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
