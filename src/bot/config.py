from __future__ import annotations

from pathlib import Path

import toml
from pydantic import BaseModel, Field, ValidationError


class WelcomeFeatureConfig(BaseModel):
    welcome_state_path: Path = Path("run/group_state.json")
    group_name: str
    message: str
    message_min_interval_seconds: int = Field(default=90, gt=0)
    welcome_state_max_age_seconds: int = Field(default=15 * 60, gt=0)
    periodic_membership_reconcile_interval_seconds: float = Field(default=30.0, ge=0)


class BotConfig(BaseModel):
    verbose: bool = False
    sync_on_startup: bool = True
    signal_cli_timeout_seconds: float = Field(default=30.0, gt=0)

    signal_receive_timeout_seconds: int = Field(default=5, gt=0)
    """
    Determines how long the bot waits for signal messages/events to appear. 
    If no signal payload is received within this time the bot spents a cycle,
    which is also an event to all the features.
    """

    signal_daemon_socket_path: Path = Path("run/signal-cli.sock")
    welcome_feature: WelcomeFeatureConfig | None = None


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
            return BotConfig.model_validate(raw_config)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Config file not found: {config_path}") from exc
    except ValidationError as exc:
        raise ValueError(f"Invalid config file {config_path}: {exc}") from exc
