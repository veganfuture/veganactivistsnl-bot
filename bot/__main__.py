import argparse
import os
import sys
from pathlib import Path

from loguru import logger

from bot.bot import BotConfig, run_bot

DEFAULT_STATE_MAX_AGE_SECONDS = 15 * 60
DEFAULT_WELCOME_MESSAGE = "Welcome {{newusers}} to the group!"
DEFAULT_WELCOME_GROUP = "Intro - Vegan Activists NL"
DEFAULT_WELCOME_MESSAGE_MIN_INTERVAL_SECONDS = 90
DEFAULT_SYNC_ON_STARTUP = True
DEFAULT_SIGNAL_CLI_TIMEOUT_SECONDS = 30.0
DEFAULT_SIGNAL_RECEIVE_TIMEOUT_SECONDS = 2
DEFAULT_RECEIVE_POLL_DELAY_SECONDS = 0.2


def main() -> None:
    args = _parse_args()
    _configure_logging(args.verbose)
    if not args.account:
        logger.error("SIGNAL_ACCOUNT must be set (e.g. +123456789).")
        raise SystemExit(1)
    if not args.welcome_group:
        raise ValueError("WELCOME_GROUP is required, set it or use --welcome-group")
    if not args.welcome_message:
        raise ValueError("WELCOME_MESSAGE is required, set it or use --welcome-message")
    if args.welcome_message_min_interval_seconds <= 0:
        raise ValueError(
            "--welcome-message-min-interval-seconds must be greater than zero"
        )
    if args.state_max_age_seconds <= 0:
        raise ValueError("--state-max-age-seconds must be greater than zero")
    if args.signal_cli_timeout_seconds <= 0:
        raise ValueError("--signal-cli-timeout-seconds must be greater than zero")
    if args.signal_receive_timeout_seconds <= 0:
        raise ValueError("--signal-receive-timeout-seconds must be greater than zero")
    if args.receive_poll_delay_seconds < 0:
        raise ValueError("--receive-poll-delay-seconds must be zero or greater")
    config = BotConfig(
        account=args.account,
        state_path=args.state_path,
        welcome_group=args.welcome_group,
        welcome_message=args.welcome_message,
        welcome_message_min_interval_seconds=args.welcome_message_min_interval_seconds,
        state_max_age_seconds=args.state_max_age_seconds,
        sync_on_startup=args.sync_on_startup,
        signal_cli_timeout_seconds=args.signal_cli_timeout_seconds,
        signal_receive_timeout_seconds=args.signal_receive_timeout_seconds,
        receive_poll_delay_seconds=args.receive_poll_delay_seconds,
    )
    run_bot(config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal welcome bot")
    parser.add_argument(
        "--account",
        default=os.environ.get("SIGNAL_ACCOUNT"),
        help="Signal account number (or set SIGNAL_ACCOUNT)",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=os.environ.get("BOT_STATE_FILE", "data/group_state.json"),
        help="Path to the JSON state file (or set BOT_STATE_FILE)",
    )
    parser.add_argument(
        "--welcome-group",
        default=os.environ.get("WELCOME_GROUP", DEFAULT_WELCOME_GROUP),
        help="Group name to watch for new members (or set WELCOME_GROUP)",
    )
    parser.add_argument(
        "--welcome-message",
        default=os.environ.get("WELCOME_MESSAGE", DEFAULT_WELCOME_MESSAGE),
        help="Message template to send (or set WELCOME_MESSAGE)",
    )
    parser.add_argument(
        "--welcome-message-min-interval-seconds",
        type=int,
        default=int(
            os.environ.get(
                "WELCOME_MESSAGE_MIN_INTERVAL_SECONDS",
                DEFAULT_WELCOME_MESSAGE_MIN_INTERVAL_SECONDS,
            )
        ),
        help=(
            "Minimum interval between welcome messages in seconds "
            "(or set WELCOME_MESSAGE_MIN_INTERVAL_SECONDS)"
        ),
    )
    parser.add_argument(
        "--state-max-age-seconds",
        type=int,
        default=int(
            os.environ.get("STATE_MAX_AGE_SECONDS", DEFAULT_STATE_MAX_AGE_SECONDS)
        ),
        help=(
            "Max age for state file in seconds before reseeding "
            "(or set STATE_MAX_AGE_SECONDS)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=_parse_bool_env(os.environ.get("BOT_VERBOSE"), False),
        help="Enable debug logging (or set BOT_VERBOSE)",
    )
    parser.add_argument(
        "--sync-on-startup",
        action=argparse.BooleanOptionalAction,
        default=_parse_bool_env(
            os.environ.get("SIGNAL_SYNC_ON_STARTUP"),
            DEFAULT_SYNC_ON_STARTUP,
        ),
        help="Send a Signal sync request on startup (or set SIGNAL_SYNC_ON_STARTUP)",
    )
    parser.add_argument(
        "--signal-cli-timeout-seconds",
        type=float,
        default=float(
            os.environ.get(
                "SIGNAL_CLI_TIMEOUT_SECONDS",
                DEFAULT_SIGNAL_CLI_TIMEOUT_SECONDS,
            )
        ),
        help=(
            "Timeout for one-shot signal-cli commands in seconds "
            "(or set SIGNAL_CLI_TIMEOUT_SECONDS)"
        ),
    )
    parser.add_argument(
        "--signal-receive-timeout-seconds",
        type=int,
        default=int(
            os.environ.get(
                "SIGNAL_RECEIVE_TIMEOUT_SECONDS",
                DEFAULT_SIGNAL_RECEIVE_TIMEOUT_SECONDS,
            )
        ),
        help=(
            "Timeout for each receive polling cycle in seconds "
            "(or set SIGNAL_RECEIVE_TIMEOUT_SECONDS)"
        ),
    )
    parser.add_argument(
        "--receive-poll-delay-seconds",
        type=float,
        default=float(
            os.environ.get(
                "RECEIVE_POLL_DELAY_SECONDS",
                DEFAULT_RECEIVE_POLL_DELAY_SECONDS,
            )
        ),
        help=(
            "Delay between receive polling cycles in seconds "
            "(or set RECEIVE_POLL_DELAY_SECONDS)"
        ),
    )
    return parser.parse_args()


def _configure_logging(verbose: bool) -> None:
    """
    Configure loguru logging.

    Args:
    - verbose - whether to enable debug logging

    Returns: None
    """
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level)


def _parse_bool_env(value: str | None, default: bool) -> bool:
    """
    Parse a boolean environment value.

    Args:
    - value - environment value to parse
    - default - fallback value when the environment variable is missing

    Returns: parsed boolean
    """
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


if __name__ == "__main__":
    main()
