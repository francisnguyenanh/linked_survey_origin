"""Flask application factory for DSAF."""

import logging
import os
from pathlib import Path

from flask import Flask, render_template

from app.config import config_map


def create_app(config_name: str = None) -> Flask:
    """
    Flask application factory.

    Args:
        config_name: One of 'development', 'production', 'testing'. Defaults to FLASK_ENV env var.

    Returns:
        Configured Flask application instance.
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="../static",
        static_url_path="/static",
    )
    app.config.from_object(config_map.get(config_name, config_map["development"]))

    # Ensure all data directories exist
    for dir_attr in ["DATA_DIR", "MAPS_DIR", "PATTERNS_DIR", "RESULTS_DIR",
                     "SCREENSHOTS_DIR", "LOGS_DIR"]:
        Path(app.config[dir_attr]).mkdir(parents=True, exist_ok=True)

    # Configure logging
    _configure_logging(app)

    # Initialize extensions
    from app.extensions import socketio
    socketio.init_app(app)

    # Register blueprints
    from app.routes.mapper import mapper_bp
    from app.routes.configurator import configurator_bp
    from app.routes.executor import executor_bp

    app.register_blueprint(mapper_bp, url_prefix="/api/mapper")
    app.register_blueprint(configurator_bp, url_prefix="/api/config")
    app.register_blueprint(executor_bp, url_prefix="/api/executor")

    # Register page routes
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/mapper")
    def mapper_page():
        return render_template("mapper.html")

    @app.route("/configurator")
    def configurator_page():
        return render_template("configurator.html")

    @app.route("/executor")
    def executor_page():
        return render_template("executor.html")

    return app


def _configure_logging(app: Flask):
    """Set up file and console logging handlers."""
    log_level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO)
    log_dir = Path(app.config["LOGS_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    logging.basicConfig(level=log_level, handlers=[console_handler])
    app.logger.setLevel(log_level)
