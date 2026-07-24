from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.bot import SignalBotRunner
from bot.config import (
    BotConfig,
)
from bot.__test__.mock_signal_client import MockSignalClient
from bot.signal_cli import SignalPayload, SignalRpcClient


class DummyFeature:
    def __init__(self, name: str) -> None:
        self.name = name
        self.setup_mock = AsyncMock()
        self.handle_mock = AsyncMock()
        self.cycle_mock = AsyncMock()

    async def setup(self) -> None:
        await self.setup_mock()

    async def handle_payloads(
        self,
        payloads: list[SignalPayload],
        cycle_finished_at: float,
    ) -> None:
        await self.handle_mock(payloads, cycle_finished_at)

    async def on_cycle(self, cycle_finished_at: float) -> None:
        await self.cycle_mock(cycle_finished_at)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_calls_composed_feature_lifecycle(self) -> None:
        client = MockSignalClient([[]])
        feature = DummyFeature("dummy")
        runner = SignalBotRunner(
            _build_bot_config(sync_on_startup=False),
            client,
            [feature],
        )

        with self.assertRaisesRegex(RuntimeError, "stop test loop"):
            await runner.run()

        feature.setup_mock.assert_awaited_once()
        feature.handle_mock.assert_awaited_once()
        feature.cycle_mock.assert_awaited_once()
        client.close_mock.assert_awaited_once()

    async def test_runner_requests_sync_when_enabled(self) -> None:
        client = MockSignalClient([[]])
        runner = SignalBotRunner(
            _build_bot_config(sync_on_startup=True),
            client,
            [DummyFeature("dummy")],
        )

        with self.assertRaisesRegex(RuntimeError, "stop test loop"):
            await runner.run()

        self.assertEqual(client.send_sync_request_calls, 1)


class SignalPayloadTests(unittest.TestCase):
    def test_extract_message_text_prefers_message_body(self) -> None:
        payload = _message_payload("events-group", "Meetup tomorrow")
        self.assertEqual(payload.extract_message_text(), "Meetup tomorrow")


class SignalRpcClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_group_by_id_requests_targeted_list_groups(self) -> None:
        client = SignalRpcClient(
            socket_path=Path("/tmp/signal-cli.sock"),
        )
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "groupId": "welcome-group",
                    "name": "Intro",
                    "members": [],
                }
            ]
        )

        group = await client.get_group_by_id("welcome-group")

        if group is None:
            self.fail("Expected welcome group to be resolved")
        self.assertEqual(group.resolved_id, "welcome-group")
        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "listGroups",
            {"groupId": "welcome-group"},
        )


def _build_bot_config(sync_on_startup: bool) -> BotConfig:
    return BotConfig(
        sync_on_startup=sync_on_startup,
        signal_cli_timeout_seconds=30.0,
        signal_receive_timeout_seconds=5,
        signal_daemon_socket_path=Path("/tmp/signal-cli.sock"),
    )


def _message_payload(group_id: str, message: str) -> SignalPayload:
    return SignalPayload.model_validate(
        {
            "envelope": {
                "sourceName": "Alice",
                "dataMessage": {
                    "groupId": group_id,
                    "message": message,
                },
            }
        }
    )
