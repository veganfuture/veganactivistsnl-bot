from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.bot import Bot, BotConfig, BotState
from bot.signal_cli import ContactRecipient, GroupMember, SignalClient, SignalGroup, SignalPayload


class MockSignalClient(SignalClient):
    def __init__(
        self,
        payload_batches: list[list[SignalPayload]],
        groups: list[SignalGroup] | None = None,
        group_snapshots: list[list[SignalGroup]] | None = None,
        contacts_by_request: dict[tuple[str, ...] | None, list[ContactRecipient]] | None = None,
    ) -> None:
        super().__init__(account="+31000000000")
        self._payload_batches = payload_batches
        self._groups = groups or []
        self._group_snapshots = group_snapshots
        self._contacts_by_request = contacts_by_request or {}
        self.close_mock: AsyncMock = AsyncMock()
        self.list_contacts_calls: list[tuple[str, ...] | None] = []
        self.sent_messages: list[tuple[str, str]] = []

    async def list_groups(self, group_id: str | None = None) -> list[SignalGroup]:
        groups = self._groups
        if self._group_snapshots is not None and self._group_snapshots:
            groups = self._group_snapshots.pop(0)
        if group_id is None:
            return groups
        return [group for group in groups if group.resolved_id == group_id]

    async def list_contacts(
        self,
        recipients: list[str] | None = None,
    ) -> list[ContactRecipient]:
        key = tuple(recipients) if recipients is not None else None
        self.list_contacts_calls.append(key)
        return self._contacts_by_request.get(key, [])

    async def send_group_message(self, group_id: str, message: str) -> None:
        self.sent_messages.append((group_id, message))

    async def send_sync_request(self) -> None:
        return None

    async def receive_events(self) -> list[SignalPayload]:
        if not self._payload_batches:
            raise RuntimeError("stop test loop")
        return self._payload_batches.pop(0)

    async def close(self) -> None:
        await self.close_mock()


class TestBot(Bot):
    def __init__(
        self,
        config: BotConfig,
        client: MockSignalClient,
        state: BotState,
        greet_mock: AsyncMock,
        flush_mock: AsyncMock,
    ) -> None:
        super().__init__(config, client=client)
        self._test_state = state
        self.greet_mock = greet_mock
        self.flush_mock = flush_mock

    def load_state(self) -> BotState | None:
        return self._test_state

    async def greet_new_welcome_group_members(self) -> None:
        await self.greet_mock()

    async def flush_pending_welcome_messages(self) -> None:
        await self.flush_mock()


class RunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_membership_reconcile_runs_without_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            client = MockSignalClient([[], [], [], []])
            config = _build_config(
                state_path=state_path,
                periodic_membership_reconcile_cycles=2,
            )
            state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["known-member"],
            )
            greet_mock = AsyncMock()
            flush_mock = AsyncMock()
            bot = TestBot(config, client, state, greet_mock, flush_mock)

            with self.assertRaisesRegex(RuntimeError, "stop test loop"):
                await bot.run()

            self.assertEqual(greet_mock.await_count, 2)
            self.assertEqual(flush_mock.await_count, 4)
            client.close_mock.assert_awaited_once()

    async def test_group_update_does_not_duplicate_periodic_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            client = MockSignalClient([[_group_update_payload("welcome-group")]])
            config = _build_config(
                state_path=state_path,
                periodic_membership_reconcile_cycles=1,
            )
            state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["known-member"],
            )
            greet_mock = AsyncMock()
            flush_mock = AsyncMock()
            bot = TestBot(config, client, state, greet_mock, flush_mock)

            with self.assertRaisesRegex(RuntimeError, "stop test loop"):
                await bot.run()

            self.assertEqual(greet_mock.await_count, 1)
            flush_mock.assert_awaited_once()
            client.close_mock.assert_awaited_once()


class WelcomeNameResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_welcome_messages_uses_full_contacts_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            group = _welcome_group(["member-1"])
            client = MockSignalClient(
                [],
                groups=[group],
                contacts_by_request={
                    None: [_contact("member-1", "Alice")],
                },
            )
            bot = Bot(_build_config(state_path=state_path, periodic_membership_reconcile_cycles=6), client=client)
            bot.state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["member-1"],
                pending_welcome_members=["member-1"],
            )

            await bot.send_welcome_messages({"member-1"}, now=1.0, group=group)

            self.assertEqual(client.list_contacts_calls, [None])
            self.assertEqual(client.sent_messages, [("welcome-group", "Welcome Alice")])

    async def test_send_welcome_messages_falls_back_to_recipient_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            group = _welcome_group(["member-1"])
            client = MockSignalClient(
                [],
                groups=[group],
                contacts_by_request={
                    None: [],
                    ("member-1",): [_contact("member-1", "Alice")],
                },
            )
            bot = Bot(_build_config(state_path=state_path, periodic_membership_reconcile_cycles=6), client=client)
            bot.state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["member-1"],
                pending_welcome_members=["member-1"],
            )

            await bot.send_welcome_messages({"member-1"}, now=1.0, group=group)

            self.assertEqual(client.list_contacts_calls, [None, ("member-1",)])
            self.assertEqual(client.sent_messages, [("welcome-group", "Welcome Alice")])

    async def test_send_welcome_messages_retries_once_then_sends_without_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            group = _welcome_group(["member-1"])
            client = MockSignalClient(
                [],
                groups=[group],
                contacts_by_request={
                    None: [],
                    ("member-1",): [],
                },
            )
            bot = Bot(_build_config(state_path=state_path, periodic_membership_reconcile_cycles=6), client=client)
            bot.state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["member-1"],
                pending_welcome_members=["member-1"],
                pending_name_retry_at=None,
            )

            await bot.send_welcome_messages(
                {"member-1"},
                now=1.0,
                group=group,
                unresolved_name_retry_delay_seconds=10.0,
            )

            self.assertEqual(client.sent_messages, [])
            self.assertEqual(bot.require_state().pending_name_retry_at, 11.0)

            await bot.send_welcome_messages(
                {"member-1"},
                now=12.0,
                group=group,
                unresolved_name_retry_delay_seconds=10.0,
            )

            self.assertEqual(
                client.list_contacts_calls,
                [None, ("member-1",), None, ("member-1",)],
            )
            self.assertEqual(client.sent_messages, [("welcome-group", "Welcome")])
            self.assertIsNone(bot.require_state().pending_name_retry_at)

    async def test_multiple_members_are_batched_after_welcome_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            interval_seconds = 90
            now = time.time()
            client = MockSignalClient(
                [],
                group_snapshots=[
                    [_welcome_group(["existing-member", "member-1"])],
                    [_welcome_group(["existing-member", "member-1", "member-2"])],
                    [_welcome_group(["existing-member", "member-1", "member-2"])],
                ],
                contacts_by_request={
                    None: [
                        _contact("member-1", "Alice"),
                        _contact("member-2", "Bob"),
                    ],
                },
            )
            bot = Bot(
                _build_config(
                    state_path=state_path,
                    periodic_membership_reconcile_cycles=6,
                    welcome_message_min_interval_seconds=interval_seconds,
                ),
                client=client,
            )
            bot.state = BotState(
                welcome_group_id="welcome-group",
                welcome_group_members=["existing-member"],
                pending_welcome_members=[],
                last_welcome_sent_at=now,
            )

            await bot.greet_new_welcome_group_members()
            await bot.greet_new_welcome_group_members()

            self.assertEqual(client.sent_messages, [])
            self.assertEqual(
                bot.require_state().pending_welcome_members,
                ["member-1", "member-2"],
            )

            bot.require_state().last_welcome_sent_at = now - interval_seconds - 1
            await bot.flush_pending_welcome_messages()

            self.assertEqual(
                client.sent_messages,
                [("welcome-group", "Welcome Alice and Bob")],
            )
            self.assertEqual(bot.require_state().pending_welcome_members, [])


def _build_config(
    state_path: Path,
    periodic_membership_reconcile_cycles: int,
    welcome_message_min_interval_seconds: int = 90,
) -> BotConfig:
    return BotConfig(
        account="+31000000000",
        state_path=state_path,
        welcome_group="Intro - Vegan Activists NL",
        welcome_message="Welcome {{newusers}}",
        welcome_message_min_interval_seconds=welcome_message_min_interval_seconds,
        state_max_age_seconds=900,
        sync_on_startup=False,
        signal_cli_timeout_seconds=30.0,
        signal_receive_timeout_seconds=5,
        signal_daemon_socket_path=Path("/tmp/signal-cli.sock"),
        unresolved_name_retry_delay_seconds=10.0,
        periodic_membership_reconcile_cycles=periodic_membership_reconcile_cycles,
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


def _welcome_group(member_ids: list[str]) -> SignalGroup:
    return SignalGroup.model_validate(
        {
            "groupId": "welcome-group",
            "name": "Intro - Vegan Activists NL",
            "members": [{"uuid": member_id} for member_id in member_ids],
        }
    )


def _contact(member_id: str, given_name: str) -> ContactRecipient:
    return ContactRecipient(uuid=member_id, name=given_name)


if __name__ == "__main__":
    unittest.main()
