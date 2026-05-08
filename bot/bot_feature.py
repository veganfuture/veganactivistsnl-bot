from __future__ import annotations

from typing import Protocol

from bot.signal_cli import SignalPayload


class BotFeature(Protocol):
    name: str

    async def setup(self) -> None: ...

    async def handle_payloads(
        self,
        payloads: list[SignalPayload],
        cycle_finished_at: float,
    ) -> None: ...

    async def on_cycle(self, cycle_finished_at: float) -> None: ...
