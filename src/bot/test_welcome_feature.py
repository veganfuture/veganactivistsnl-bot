from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from bot.welcome_feature import WelcomeFeature, WelcomeState
from bot.config import WelcomeFeatureConfig
from bot.__test__.mock_signal_client import MockSignalClient
from bot.signal_cli import SignalPayload, SignalGroup


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
