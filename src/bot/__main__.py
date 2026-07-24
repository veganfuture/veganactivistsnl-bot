import argparse
import sys
from pathlib import Path

from loguru import logger

from bot.bot import run_bot
from bot.config import load_config


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    _configure_logging(config.verbose)
    run_bot(config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal community bot")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the bot config TOML file",
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


if __name__ == "__main__":
    main()
