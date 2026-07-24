from __future__ import annotations

from typing import Protocol

from bot.signal_cli import SignalPayload


class BotFeature(Protocol):
    name: str

    async def setup(self) -> None: ...

    """
    Called when the bot is ready.
    """

    async def handle_payloads(
        self,
        payloads: list[SignalPayload],
        cycle_finished_at: float,
    ) -> None: ...

    """
    Called when the bot receives signal messages or events.
    """

    async def on_cycle(self, cycle_finished_at: float) -> None: ...

    """
    Called after every cycle of the bot. The bot cycles when is receives
    a signal payload, but also when nothing happens within  
    `config.signal_receive_timeout_seconds`. 
    """
