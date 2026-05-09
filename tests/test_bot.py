from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import NoReturn
from unittest.mock import AsyncMock

import httpx

from bot.bot import SignalBotRunner
from bot.config import (
    BotConfig,
    EventCalendarFeatureConfig,
    GeminiConfig,
    WelcomeFeatureConfig,
    load_config,
)
from bot.event_calendar_feature import (
    CalendarEvent,
    EventCalendarFeature,
    GeminiCalendarEventParser,
)
from bot.signal_cli import ContactRecipient, SignalGroup, SignalPayload, SignalRpcClient
from bot.welcome_feature import WelcomeFeature, WelcomeState


class MockSignalClient:
    def __init__(
        self,
        payload_batches: list[list[SignalPayload]],
        groups: list[SignalGroup] | None = None,
        group_snapshots: list[list[SignalGroup]] | None = None,
        contacts: list[ContactRecipient] | None = None,
    ) -> None:
        self._payload_batches = payload_batches
        self._groups = groups or []
        self._group_snapshots = group_snapshots
        self._contacts = contacts or []
        self.close_mock: AsyncMock = AsyncMock()
        self.sent_messages: list[tuple[str, str]] = []
        self.sent_contact_messages: list[tuple[str, str]] = []
        self.receive_events_calls = 0
        self.send_sync_request_calls = 0

    async def list_groups(self, group_id: str | None = None) -> list[SignalGroup]:
        groups = self._groups
        if self._group_snapshots is not None and self._group_snapshots:
            groups = self._group_snapshots.pop(0)
        if group_id is None:
            return groups
        return [group for group in groups if group.resolved_id == group_id]

    async def get_group_by_id(self, group_id: str) -> SignalGroup | None:
        groups = await self.list_groups()
        return next((group for group in groups if group.resolved_id == group_id), None)

    async def get_group_by_name(self, group_name: str) -> SignalGroup | None:
        groups = await self.list_groups()
        return next((group for group in groups if group.name == group_name), None)

    async def list_contacts(
        self,
        recipients: list[str] | None = None,
    ) -> list[ContactRecipient]:
        if recipients is None:
            return self._contacts
        return [
            contact
            for contact in self._contacts
            if contact.number in recipients
            or contact.uuid in recipients
            or contact.username in recipients
        ]

    async def send_contact_message(self, recipient: str, message: str) -> None:
        self.sent_contact_messages.append((recipient, message))

    async def send_group_message(self, group_id: str, message: str) -> None:
        self.sent_messages.append((group_id, message))

    async def send_sync_request(self) -> None:
        self.send_sync_request_calls += 1

    async def receive_events(self) -> list[SignalPayload]:
        self.receive_events_calls += 1
        if not self._payload_batches:
            raise RuntimeError("stop test loop")
        return self._payload_batches.pop(0)

    async def close(self) -> None:
        await self.close_mock()


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


class FakeCalendarParser:
    def __init__(self, events: list[CalendarEvent]) -> None:
        self.events = events
        self.messages: list[str] = []

    async def parse_events(self, message_text: str) -> list[CalendarEvent]:
        self.messages.append(message_text)
        return self.events


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


class WelcomeFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_welcome_messages_uses_static_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            feature = WelcomeFeature(
                _build_welcome_feature_config(Path(temp_dir) / "state.json"),
                MockSignalClient(
                    [], groups=[_group("welcome-group", "Intro", ["member-1"])]
                ),
            )
            feature.welcome_state = WelcomeState(
                welcome_group_id="welcome-group",
                welcome_group_members=["member-1"],
                pending_welcome_members=["member-1"],
            )

            await feature.send_welcome_messages(
                {"member-1"},
                now=1.0,
                group=_group("welcome-group", "Intro", ["member-1"]),
            )

            assert isinstance(feature.client, MockSignalClient)
            self.assertEqual(
                feature.client.sent_messages, [("welcome-group", "Welcome")]
            )  # type: ignore[attr-defined]

    async def test_multiple_members_are_batched_after_welcome_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            interval_seconds = 90
            now = time.time()
            client = MockSignalClient(
                [],
                group_snapshots=[
                    [_group("welcome-group", "Intro", ["existing-member", "member-1"])],
                    [
                        _group(
                            "welcome-group",
                            "Intro",
                            ["existing-member", "member-1", "member-2"],
                        )
                    ],
                    [
                        _group(
                            "welcome-group",
                            "Intro",
                            ["existing-member", "member-1", "member-2"],
                        )
                    ],
                ],
            )
            feature = WelcomeFeature(
                _build_welcome_feature_config(
                    state_path,
                    message_min_interval_seconds=interval_seconds,
                ),
                client,
            )
            feature.welcome_state = WelcomeState(
                welcome_group_id="welcome-group",
                welcome_group_members=["existing-member"],
                pending_welcome_members=[],
                last_welcome_sent_at=now,
            )

            await feature.greet_new_welcome_group_members()
            await feature.greet_new_welcome_group_members()

            self.assertEqual(client.sent_messages, [])
            self.assertEqual(
                feature.require_welcome_state().pending_welcome_members,
                ["member-1", "member-2"],
            )

            feature.require_welcome_state().last_welcome_sent_at = (
                now - interval_seconds - 1
            )
            await feature.flush_pending_welcome_messages()

            self.assertEqual(client.sent_messages, [("welcome-group", "Welcome")])
            self.assertEqual(
                feature.require_welcome_state().pending_welcome_members, []
            )

    async def test_username_only_member_join_and_leave_updates_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = MockSignalClient(
                [],
                group_snapshots=[
                    [
                        _group_custom(
                            "welcome-group",
                            "Intro",
                            [{"uuid": "existing-member"}, {"username": "u:alice"}],
                        )
                    ],
                    [
                        _group_custom(
                            "welcome-group", "Intro", [{"uuid": "existing-member"}]
                        )
                    ],
                ],
            )
            feature = WelcomeFeature(
                _build_welcome_feature_config(Path(temp_dir) / "state.json"),
                client,
            )
            feature.welcome_state = WelcomeState(
                welcome_group_id="welcome-group",
                welcome_group_members=["existing-member"],
                pending_welcome_members=[],
                last_welcome_sent_at=time.time(),
            )

            await feature.greet_new_welcome_group_members()
            self.assertEqual(
                feature.require_welcome_state().pending_welcome_members,
                ["u:alice"],
            )

            await feature.greet_new_welcome_group_members()
            self.assertEqual(
                feature.require_welcome_state().pending_welcome_members, []
            )
            self.assertEqual(
                feature.require_welcome_state().welcome_group_members,
                ["existing-member"],
            )

    async def test_stale_state_reseeds_on_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            stale_state = WelcomeState(
                welcome_group_id="welcome-group",
                welcome_group_members=["stale-member"],
            )
            state_path.write_text(stale_state.model_dump_json())
            stale_mtime = time.time() - 3600
            state_path.touch(exist_ok=True)
            os.utime(state_path, (stale_mtime, stale_mtime))

            client = MockSignalClient(
                [[]], groups=[_group("welcome-group", "Intro", ["fresh-member"])]
            )
            feature = WelcomeFeature(
                _build_welcome_feature_config(
                    state_path,
                    welcome_state_max_age_seconds=60,
                ),
                client,
            )

            await feature.setup()

            self.assertEqual(
                feature.require_welcome_state().welcome_group_members,
                ["fresh-member"],
            )

    async def test_discard_startup_backlog_drains_until_empty(self) -> None:
        client = MockSignalClient(
            [
                [_group_update_payload("welcome-group")],
                [_group_update_payload("welcome-group")],
                [],
            ]
        )
        feature = WelcomeFeature(
            _build_welcome_feature_config(Path("/tmp/unused-state.json")),
            client,
        )

        await feature.discard_startup_backlog()

        self.assertEqual(client.receive_events_calls, 3)


class EventCalendarFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_feature_parses_matching_group_message_and_sends_output_user_message(
        self,
    ) -> None:
        client = MockSignalClient(
            [],
            groups=[_group("events-group", "Events", ["member-1"])],
            contacts=[_contact("+31612345678", "Calendar User")],
        )
        parser = FakeCalendarParser(
            [
                CalendarEvent(
                    date="2026-05-10",
                    time="19:30",
                    location="Amsterdam",
                    description="Community meetup",
                )
            ]
        )
        feature = EventCalendarFeature(
            _build_event_feature_config("+31612345678"),
            client,
            parser,
        )

        await feature.setup()
        await feature.handle_payloads(
            [_message_payload("events-group", "See you Sunday at 19:30 in Amsterdam")],
            cycle_finished_at=1.0,
        )

        self.assertEqual(
            parser.messages,
            ["See you Sunday at 19:30 in Amsterdam"],
        )
        self.assertEqual(len(client.sent_contact_messages), 1)
        self.assertEqual(client.sent_contact_messages[0][0], "+31612345678")
        self.assertIn('"source_group": "Events"', client.sent_contact_messages[0][1])
        self.assertIn('"date": "2026-05-10"', client.sent_contact_messages[0][1])
        self.assertIn('"location": "Amsterdam"', client.sent_contact_messages[0][1])

    async def test_feature_ignores_other_groups_and_group_updates(self) -> None:
        client = MockSignalClient(
            [],
            groups=[_group("events-group", "Events", ["member-1"])],
            contacts=[_contact("+31612345678", "Calendar User")],
        )
        parser = FakeCalendarParser([CalendarEvent(date="2026-05-10", time="19:30")])
        feature = EventCalendarFeature(
            _build_event_feature_config("+31612345678"),
            client,
            parser,
        )

        await feature.setup()
        await feature.handle_payloads(
            [
                _message_payload("other-group", "Other message"),
                _group_update_payload("events-group"),
            ],
            cycle_finished_at=1.0,
        )

        self.assertEqual(parser.messages, [])
        self.assertEqual(client.sent_contact_messages, [])


class GeminiCalendarEventParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_events_returns_empty_list_on_connect_timeout(self) -> None:
        parser = GeminiCalendarEventParser(
            GeminiConfig(
                api_key="test-key",
                model="gemini-2.5-flash",
                timeout_seconds=30.0,
                timezone_name="Europe/Amsterdam",
            )
        )
        parser._request_parse = _raise_connect_timeout  # type: ignore[method-assign]

        events = await parser.parse_events("See you Sunday at 19:30 in Amsterdam")

        self.assertEqual(events, [])


class SignalPayloadTests(unittest.TestCase):
    def test_extract_message_text_prefers_message_body(self) -> None:
        payload = _message_payload("events-group", "Meetup tomorrow")
        self.assertEqual(payload.extract_message_text(), "Meetup tomorrow")


class ConfigLoadingTests(unittest.TestCase):
    def test_load_config_applies_defaults_and_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "verbose = true",
                        'signal_daemon_socket_path = "run/signal-cli.sock"',
                        "",
                        "[welcome_feature]",
                        'welcome_state_path = "data/welcome-state.json"',
                    ]
                )
            )

            config = load_config(config_path)

            self.assertTrue(config.verbose)
            self.assertEqual(
                config.signal_daemon_socket_path,
                config_dir / "run/signal-cli.sock",
            )
            if config.welcome_feature is None:
                self.fail("Expected welcome feature config to be loaded")
            self.assertEqual(
                config.welcome_feature.welcome_state_path,
                config_dir / "data/welcome-state.json",
            )
            self.assertEqual(
                config.welcome_feature.message_min_interval_seconds,
                90,
            )
            self.assertIsNone(config.event_calendar_feature)


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


def _build_welcome_feature_config(
    welcome_state_path: Path,
    message_min_interval_seconds: int = 90,
    welcome_state_max_age_seconds: int = 900,
) -> WelcomeFeatureConfig:
    return WelcomeFeatureConfig(
        welcome_state_path=welcome_state_path,
        group_name="Intro",
        message="Welcome",
        message_min_interval_seconds=message_min_interval_seconds,
        welcome_state_max_age_seconds=welcome_state_max_age_seconds,
        periodic_membership_reconcile_interval_seconds=30.0,
    )


def _build_event_feature_config(output_user: str) -> EventCalendarFeatureConfig:
    return EventCalendarFeatureConfig(
        group_name="Events",
        output_user=output_user,
        gemini=GeminiConfig(
            api_key="test-key",
            model="gemini-2.5-flash",
            timeout_seconds=30.0,
            timezone_name="Europe/Amsterdam",
        ),
    )


def _raise_connect_timeout(message_text: str) -> NoReturn:
    del message_text
    raise httpx.ConnectTimeout("TLS handshake timed out")


def _contact(number: str, name: str) -> ContactRecipient:
    return ContactRecipient.model_validate(
        {
            "number": number,
            "name": name,
        }
    )


def _group(
    group_id: str,
    group_name: str,
    member_ids: list[str],
) -> SignalGroup:
    return _group_custom(
        group_id,
        group_name,
        [{"uuid": member_id} for member_id in member_ids],
    )


def _group_custom(
    group_id: str,
    group_name: str,
    member_specs: list[dict[str, str]],
) -> SignalGroup:
    return SignalGroup.model_validate(
        {
            "groupId": group_id,
            "name": group_name,
            "members": member_specs,
        }
    )


def _group_update_payload(group_id: str) -> SignalPayload:
    return SignalPayload.model_validate(
        {
            "envelope": {
                "dataMessage": {
                    "groupInfo": {
                        "groupId": group_id,
                        "type": "UPDATE",
                    }
                }
            }
        }
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


if __name__ == "__main__":
    unittest.main()
