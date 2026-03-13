from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import SignalCliClient, SignalPayload


def run_bot(
    account: str,
    state_path: Path,
    welcome_group: str,
    welcome_message: str,
    state_max_age_seconds: int,
    sync_on_startup: bool,
) -> None:
    resolved_path = Path(state_path)
    try:
        asyncio.run(
            _run(
                account=account,
                state_path=resolved_path,
                welcome_group=welcome_group,
                welcome_message=welcome_message,
                state_max_age_seconds=state_max_age_seconds,
                sync_on_startup=sync_on_startup,
            )
        )
    except KeyboardInterrupt:
        pass


async def _run(
    account: str,
    state_path: Path,
    welcome_group: str,
    welcome_message: str,
    state_max_age_seconds: int,
    sync_on_startup: bool,
) -> None:
    logger.info("Starting signal bot for account {}", account)

    client = SignalCliClient(account)
    if sync_on_startup:
        logger.info("Requesting Signal sync on startup")
        await client.send_sync_request()

    state = _load_state(state_path, state_max_age_seconds)
    if state:
        logger.info(f"Bot state loaded from: {state_path}")
    if not state:
        logger.info("No bot state found, seeding")
        state = await _seed_state(client, welcome_group)
        _save_state(state, state_path)
        logger.info("Bot state seeded")

    while True:
        async for payload in client.receive_events():
            logger.debug(payload)
            if is_welcome_group_update(payload, state):
                try:
                    await _greet_new_welcome_group_members(
                        client,
                        state,
                        state_path,
                        welcome_message,
                    )
                except RuntimeError as exc:
                    logger.error("Error handling group update: {}", exc)
        await asyncio.sleep(2)


class BotState(BaseModel):
    welcome_group_id: str
    welcome_group_members: list[str] = Field(default_factory=list)
    """
    list of member ids (uuid or number)
    """


async def _seed_state(client: SignalCliClient, welcome_group: str) -> BotState:
    group = await client.get_group_by_name(welcome_group)

    if group is None:
        groups = await client.list_groups()
        group_names = [group.name for group in groups]
        raise RuntimeError(
            f"Could not find listing for group: {welcome_group}. Groups found: {group_names}"
        )

    if group.resolved_id is None:
        raise RuntimeError(f"Could not resolve group_id for group: {welcome_group}")

    return BotState(
        welcome_group_id=group.resolved_id,
        welcome_group_members=sorted(group.get_member_ids()),
    )


def is_welcome_group_update(payload: SignalPayload, state: BotState) -> bool:
    if not payload.is_group_update():
        return False
    group_id = payload.extract_group_id()
    return group_id == state.welcome_group_id


async def _greet_new_welcome_group_members(
    client: SignalCliClient,
    state: BotState,
    state_path: Path,
    welcome_message: str,
) -> None:
    # Get group info
    group_id = state.welcome_group_id
    group = await client.get_group_by_id(group_id)
    if group is None:
        logger.error(f"Could not resolve group info for welcome group!")
        return

    # Check whether we have new members
    members = group.get_member_ids()
    known_members = {str(member_id) for member_id in state.welcome_group_members}
    if not known_members:
        state.welcome_group_members = sorted(members)
        _save_state(state, state_path)
        return
    new_members = members - known_members
    if not new_members:
        state.welcome_group_members = sorted(members)
        _save_state(state, state_path)
        return

    # Greet new members
    group_members = await client.group_members(group_id)
    new_member_names = [
        member.name or member.number or member.uuid
        for member in group_members
        if (member.uuid or member.number) in new_members
    ]
    rendered_names = ", ".join(name for name in new_member_names if name)
    message = welcome_message.replace("{{newusers}}", rendered_names)
    await client.send_group_message(group_id, message)
    logger.info("Sent welcome message to group {}", group_id)
    state.welcome_group_members = sorted(members)
    _save_state(state, state_path)


def _save_state(state: BotState, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = BotState(
        welcome_group_id=state.welcome_group_id,
        welcome_group_members=sorted(
            {str(member) for member in state.welcome_group_members}
        ),
    )
    state_path.write_text(encoded.model_dump_json(indent=2))


def _load_state(
    state_path: Path,
    state_max_age_seconds: int,
) -> BotState | None:
    if not state_path.exists():
        return None
    age_seconds = time.time() - state_path.stat().st_mtime
    if age_seconds > state_max_age_seconds:
        logger.info("State file is stale ({}s), reseeding", int(age_seconds))
        return None

    return BotState.model_validate_json(state_path.read_text())
