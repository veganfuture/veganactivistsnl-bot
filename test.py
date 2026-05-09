import asyncio
from pathlib import Path
import sys
from loguru import logger

from bot.config import load_config
from bot.event_calendar_feature import GeminiCalendarEventParser


async def main() -> None:
    config = load_config(Path("configs/test.toml"))
    if config.event_calendar_feature is None:
        raise RuntimeError("Missing event_calendar_feature config")

    gemini = GeminiCalendarEventParser(config.event_calendar_feature.gemini)
    events = await gemini.parse_events(
        "Vegan potluck in Amsterdam Oosterpark. Join us near the old tree. "
        "Bring some food and we'll bring the rest! Saturday 9th of May, 14:00. "
        "Hope to see you there!"
    )
    print(events)


if __name__ == "__main__":
    logger.remove()
    level = "DEBUG"
    logger.add(sys.stderr, level=level)
    asyncio.run(main())
