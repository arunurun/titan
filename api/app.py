"""Flask API factory with all blueprints registered."""
from flask import Flask
from api.routes_analysis import bp as analysis_bp
from api.routes_token import bp as token_bp


def create_app():
    app = Flask(__name__)

    # Register blueprints
    app.register_blueprint(analysis_bp)
    app.register_blueprint(token_bp)

    @app.route("/api/health", methods=["GET"])
    def health():
        return {"ok": True, "status": "healthy"}, 200

    @app.route("/api/sectors", methods=["GET"])
    def sectors():
        try:
            from services.sector_registry import list_active_sector_ids
            sector_list = [
                s for s in list_active_sector_ids(include_unknown=False)
                if s not in {"unknown", "non_equity"}
            ]
            return {"ok": True, "sectors": sector_list}, 200
        except Exception as exc:
            return {"ok": False, "error": str(exc)}, 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=8788)
