from __future__ import annotations

from pathlib import Path
from typing import Any

import toml
from pydantic import BaseModel, Field, ValidationError


class GeminiConfig(BaseModel):
    api_key: str = Field(min_length=1)
    model: str = "gemini-2.5-flash"
    timeout_seconds: float = Field(default=30.0, gt=0)
    timezone_name: str = "Europe/Amsterdam"


class EventCalendarFeatureConfig(BaseModel):
    group_name: str = "Events"
    output_user: str = Field(min_length=1)
    gemini: GeminiConfig


class WelcomeFeatureConfig(BaseModel):
    welcome_state_path: Path = Path("data/group_state.json")
    group_name: str = Field(default="Intro - Vegan Activists NL", min_length=1)
    message: str = (
        "Welcome to the Vegan Activists NL community 💚\n\n"
        "Here are our main Signal groups:\n"
        "• Chat – for general discussion: https://veganactivists.nl/chat\n"
        "• Events – for sharing and discovering events: https://veganactivists.nl/events\n"
        "• Amsterdam – for Amsterdam-based activism: https://veganactivists.nl/amsterdam\n\n"
        "We have many non-Dutch speakers, so we encourage using English to keep conversations inclusive. Dutch is of course also welcome!\n\n"
        "To help you get started, we’d love for you to introduce yourself and share your interest in activism. This way we can better support you.\n\n"
        "This intro group is just to welcome you and help you find your way. It’s not meant for ongoing discussion, so feel free to leave once you're ready to join the other groups."
    )
    message_min_interval_seconds: int = Field(default=90, gt=0)
    welcome_state_max_age_seconds: int = Field(default=15 * 60, gt=0)
    periodic_membership_reconcile_interval_seconds: float = Field(default=30.0, ge=0)


class BotConfig(BaseModel):
    account: str = Field(min_length=1)
    verbose: bool = False
    sync_on_startup: bool = True
    signal_cli_timeout_seconds: float = Field(default=30.0, gt=0)
    signal_receive_timeout_seconds: int = Field(default=5, gt=0)
    signal_daemon_socket_path: Path = Path("/srv/veganactivistsnl-bot/run/signal-cli.sock")
    welcome_feature: WelcomeFeatureConfig | None = None
    event_calendar_feature: EventCalendarFeatureConfig | None = None


def load_config(config_path: Path) -> BotConfig:
    """
    Load bot configuration from TOML.

    Args:
    - config_path - path to the TOML config file

    Returns: validated bot config
    """
    try:
        with config_path.open(encoding="utf-8") as file:
            raw_config = toml.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Config file not found: {config_path}") from exc

    normalized_config = _resolve_config_paths(raw_config, config_path.parent)
    try:
        config = BotConfig.model_validate(normalized_config)
    except ValidationError as exc:
        raise ValueError(f"Invalid config file {config_path}: {exc}") from exc
    if config.welcome_feature is None and config.event_calendar_feature is None:
        raise ValueError("Invalid config file: at least one bot feature must be configured")
    return config


def _resolve_config_paths(
    raw_config: dict[str, Any],
    config_dir: Path,
) -> dict[str, Any]:
    """
    Resolve relative path settings against the config file directory.

    Args:
    - raw_config - parsed TOML config
    - config_dir - directory containing the config file

    Returns: normalized config mapping
    """
    normalized_config = dict(raw_config)
    signal_socket_path = normalized_config.get("signal_daemon_socket_path")
    if isinstance(signal_socket_path, str):
        normalized_config["signal_daemon_socket_path"] = _resolve_path(
            signal_socket_path,
            config_dir,
        )

    welcome_feature = normalized_config.get("welcome_feature")
    if isinstance(welcome_feature, dict):
        normalized_welcome_feature = dict(welcome_feature)
        welcome_state_path = normalized_welcome_feature.get("welcome_state_path")
        if isinstance(welcome_state_path, str):
            normalized_welcome_feature["welcome_state_path"] = _resolve_path(
                welcome_state_path,
                config_dir,
            )
        normalized_config["welcome_feature"] = normalized_welcome_feature

    return normalized_config


def _resolve_path(path_str: str, config_dir: Path) -> Path:
    """
    Resolve one path string against the config file directory.

    Args:
    - path_str - raw path value from config
    - config_dir - directory containing the config file

    Returns: absolute or normalized path
    """
    path = Path(path_str)
    if path.is_absolute():
        return path
    return config_dir / path
