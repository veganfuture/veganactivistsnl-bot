import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import (
    SignalCliClient,
    extract_group_id,
    normalize_member_set,
    should_check_group,
)


STATE_MAX_AGE_SECONDS = 15 * 60
WELCOME_MESSAGE = "Welcome {{newusers}} to the group!"


class GroupState(BaseModel):
    groups: dict[str, list[str]] = Field(default_factory=dict)


def _load_state(state_path: Path) -> dict[str, set[str]]:
    if not state_path.exists():
        return {}
    age_seconds = time.time() - state_path.stat().st_mtime
    if age_seconds > STATE_MAX_AGE_SECONDS:
        logger.info("State file is stale ({}s), reseeding", int(age_seconds))
        return {}
    try:
        parsed = GroupState.model_validate_json(state_path.read_text())
    except (ValueError, json.JSONDecodeError):
        return {}
    return {
        group_id: {str(m) for m in members}
        for group_id, members in parsed.groups.items()
    }


def _save_state(state: dict[str, set[str]], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = GroupState(
        groups={group_id: sorted(members) for group_id, members in state.items()}
    )
    state_path.write_text(encoded.model_dump_json(indent=2, sort_keys=True))


async def _list_groups(client: SignalCliClient) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for group in await client.list_groups():
        group_id = group.resolved_id
        if not group_id:
            continue
        groups[group_id] = normalize_member_set(group.members)
    return groups


async def _send_group_message(client: SignalCliClient, group_id: str, message: str) -> None:
    await client.send_group_message(group_id, message)


async def _handle_group_event(
    client: SignalCliClient,
    group_id: str,
    state: dict[str, set[str]],
    state_path: Path,
) -> None:
    groups = await _list_groups(client)
    members = groups.get(group_id)
    if members is None:
        return
    if group_id not in state:
        state[group_id] = members
        _save_state(state, state_path)
        return
    new_members = members - state[group_id]
    if not new_members:
        state[group_id] = members
        _save_state(state, state_path)
        return
    group_members = await client.group_members(group_id)
    new_member_names = [
        member.name or member.number or member.uuid
        for member in group_members
        if (member.uuid or member.number) in new_members
    ]
    rendered_names = ", ".join(name for name in new_member_names if name)
    message = WELCOME_MESSAGE.replace("{{newusers}}", rendered_names)
    await _send_group_message(client, group_id, message)
    logger.info("Sent welcome message to group {}", group_id)
    state[group_id] = members
    _save_state(state, state_path)


async def _receive_loop(
    client: SignalCliClient, state: dict[str, set[str]], state_path: Path
) -> None:
    async for payload in client.receive_events():
        group_id = extract_group_id(payload)
        if not group_id:
            continue
        if not should_check_group(payload):
            continue
        try:
            await _handle_group_event(client, group_id, state, state_path)
        except RuntimeError as exc:
            logger.error("Error handling group update: {}", exc)


async def _seed_state(
    client: SignalCliClient, state: dict[str, set[str]], state_path: Path
) -> None:
    groups = await _list_groups(client)
    if not groups:
        return
    state.update(groups)
    _save_state(state, state_path)
    logger.info("Seeded group state for {} groups", len(groups))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal welcome bot")
    parser.add_argument(
        "--state-path",
        default=os.environ.get("BOT_STATE_FILE", "data/group_state.json"),
        help="Path to the JSON state file",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    state_path = Path(args.state_path)
    account = os.environ.get("SIGNAL_ACCOUNT")
    if not account:
        logger.error("SIGNAL_ACCOUNT must be set (e.g. +123456789).")
        raise SystemExit(1)

    state = _load_state(state_path)
    client = SignalCliClient(account)
    logger.info("Starting signal bot for account {}", account)
    if not state:
        try:
            await _seed_state(client, state, state_path)
        except RuntimeError as exc:
            logger.error("Unable to seed group state: {}", exc)

    while True:
        try:
            await _receive_loop(client, state, state_path)
        except RuntimeError as exc:
            logger.error("signal-cli receive failed: {}", exc)
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
