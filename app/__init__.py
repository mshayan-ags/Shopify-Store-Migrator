import logging
import os
from pathlib import Path

from flask import Flask

REPO_ROOT = Path(__file__).resolve().parent.parent


def create_app():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["RESULTS_DIR"] = os.getenv("APP_RESULTS_DIR", str(REPO_ROOT / "Results"))

    from app.routes.connections import bp as connections_bp
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.jobs import bp as jobs_bp
    from app.routes.audit import bp as audit_bp
    from app.routes.sources import bp as sources_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(connections_bp, url_prefix="/connections")
    app.register_blueprint(jobs_bp, url_prefix="/jobs")
    app.register_blueprint(audit_bp, url_prefix="/audit")
    app.register_blueprint(sources_bp, url_prefix="/sources")

    return app
