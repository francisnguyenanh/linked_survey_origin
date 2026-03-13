"""Configuration classes for DSAF application."""

import os
from pathlib import Path


class Config:
    """Base configuration."""

    SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "./data"))
    MAPS_DIR: Path = DATA_DIR / "maps"
    PATTERNS_DIR: Path = DATA_DIR / "patterns"
    RESULTS_DIR: Path = DATA_DIR / "results"
    SCREENSHOTS_DIR: Path = DATA_DIR / "screenshots"
    LOGS_DIR: Path = DATA_DIR / "logs"

    DEFAULT_HEADLESS: bool = os.environ.get("DEFAULT_HEADLESS", "true").lower() == "true"
    MAX_CONCURRENCY: int = int(os.environ.get("MAX_CONCURRENCY", 3))
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PROXY_LIST: list[str] = [
        p.strip() for p in os.environ.get("PROXY_LIST", "").split(",") if p.strip()
    ]

    SOCKETIO_ASYNC_MODE: str = "eventlet"
    JSON_AS_ASCII: bool = False  # Ensure Japanese characters render correctly


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG: bool = True
    TESTING: bool = False


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG: bool = False
    TESTING: bool = False


class TestingConfig(Config):
    """Testing configuration."""

    DEBUG: bool = True
    TESTING: bool = True
    DATA_DIR: Path = Path("./test_data")


config_map: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
