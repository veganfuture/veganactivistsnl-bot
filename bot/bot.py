from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
import re

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import ContactRecipient, GroupMember, SignalCliClient, SignalPayload


@dataclass
class BotConfig:
    account: str
    state_path: Path
    welcome_group: str
    welcome_message: str
    welcome_message_min_interval_seconds: int
    state_max_age_seconds: int
    sync_on_startup: bool
    signal_cli_timeout_seconds: float
    signal_receive_timeout_seconds: int
    receive_poll_delay_seconds: float


class BotState(BaseModel):
    welcome_group_id: str
    welcome_group_members: list[str] = Field(default_factory=list)
    pending_welcome_members: list[str] = Field(default_factory=list)
    last_welcome_sent_at: float | None = None
    """
    list of member ids (uuid or number)
    """


def run_bot(config: BotConfig) -> None:
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass


async def _run(config: BotConfig) -> None:
    logger.info("Starting signal bot for account {}", config.account)

    client = SignalCliClient(
        config.account,
        command_timeout_seconds=config.signal_cli_timeout_seconds,
        receive_timeout_seconds=config.signal_receive_timeout_seconds,
    )
    if config.sync_on_startup:
        logger.info("Requesting Signal sync on startup")
        await client.send_sync_request()

    state = _load_state(config.state_path, config.state_max_age_seconds)
    if state:
        logger.info(f"Bot state loaded from: {config.state_path}")
    if not state:
        logger.info("No bot state found, seeding")
        state = await _seed_state(client, config.welcome_group)
        _save_state(state, config.state_path)
        logger.info("Bot state seeded")

    while True:
        for payload in await client.receive_events():
            logger.debug(payload)
            if is_welcome_group_update(payload, state):
                try:
                    await _greet_new_welcome_group_members(
                        client,
                        state,
                        config.state_path,
                        config.welcome_message,
                        config.welcome_message_min_interval_seconds,
                    )
                except RuntimeError as exc:
                    logger.error("Error handling group update: {}", exc)
        try:
            await _flush_pending_welcome_messages(
                client,
                state,
                config.state_path,
                config.welcome_message,
                config.welcome_message_min_interval_seconds,
            )
        except RuntimeError as exc:
            logger.error("Error flushing pending welcomes: {}", exc)
        if config.receive_poll_delay_seconds > 0:
            logger.debug("sleep")
            await asyncio.sleep(config.receive_poll_delay_seconds)
            logger.debug("receive")


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
        pending_welcome_members=[],
        last_welcome_sent_at=None,
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
    welcome_message_min_interval_seconds: int,
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
    removed_members = known_members - members
    pending_members = {str(member_id) for member_id in state.pending_welcome_members}
    logger.debug(
        "Group {} membership diff: known={}, current={}, new={}, removed={}, pending={}",
        group_id,
        len(known_members),
        len(members),
        len(new_members),
        len(removed_members),
        len(pending_members),
    )
    contacts_by_id: dict[str, ContactRecipient] = {}
    if removed_members:
        contacts = await client.list_contacts()
        contacts_by_id = _contacts_by_id(contacts)
    if removed_members:
        pending_members -= removed_members
        removed_member_names = [
            _render_member_name_by_id(member_id, contacts_by_id)
            for member_id in sorted(removed_members)
        ]
        logger.info(
            "Members left group {}: {}",
            group_id,
            _render_welcome_targets(removed_member_names, len(removed_members)),
        )
    if new_members:
        pending_members |= new_members
        logger.info(
            "Queued {} new welcome member(s) for group {}",
            len(new_members),
            group_id,
        )
    state.welcome_group_members = sorted(members)
    state.pending_welcome_members = sorted(pending_members)
    _save_state(state, state_path)
    if not new_members and not removed_members:
        return
    await _flush_pending_welcome_messages(
        client,
        state,
        state_path,
        welcome_message,
        welcome_message_min_interval_seconds,
    )


def _save_state(state: BotState, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = BotState(
        welcome_group_id=state.welcome_group_id,
        welcome_group_members=sorted(
            {str(member) for member in state.welcome_group_members}
        ),
        pending_welcome_members=sorted(
            {str(member) for member in state.pending_welcome_members}
        ),
        last_welcome_sent_at=state.last_welcome_sent_at,
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


async def _flush_pending_welcome_messages(
    client: SignalCliClient,
    state: BotState,
    state_path: Path,
    welcome_message: str,
    welcome_message_min_interval_seconds: int,
) -> None:
    pending_members = {str(member_id) for member_id in state.pending_welcome_members}
    if not pending_members:
        return

    now = time.time()
    if not _can_send_welcome_message(
        state.last_welcome_sent_at,
        now,
        welcome_message_min_interval_seconds,
    ):
        if state.last_welcome_sent_at:
            duration_till_welcome_msg = welcome_message_min_interval_seconds - (
                now - state.last_welcome_sent_at
            )
            logger.debug(
                f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
            )
        return

    # Check that the pending members are still part of the group
    group = await client.get_group_by_id(state.welcome_group_id)
    if group is None:
        logger.error("Could not resolve group info for pending welcomes!")
        return

    current_member_ids = group.get_member_ids()
    pending_members &= current_member_ids
    state.welcome_group_members = sorted(current_member_ids)
    if not pending_members:
        state.pending_welcome_members = []
        _save_state(state, state_path)
        return

    # Resolve names of pending members
    contacts_by_id = _contacts_by_id(await client.list_contacts())
    group_members = await client.group_members(state.welcome_group_id)
    pending_member_names = [
        _render_member_name(member, contacts_by_id)
        for member in group_members
        if (member.uuid or member.number) in pending_members
    ]
    rendered_names = _render_welcome_targets(
        pending_member_names,
        len(pending_members),
    )

    # Send welcome message
    message = welcome_message.replace("{{newusers}}", rendered_names)
    await client.send_group_message(state.welcome_group_id, message)
    state.pending_welcome_members = []
    state.last_welcome_sent_at = now
    _save_state(state, state_path)
    logger.info(
        "Sent welcome message to group {} for {} member(s)",
        state.welcome_group_id,
        len(pending_members),
    )


def _can_send_welcome_message(
    last_welcome_sent_at: float | None,
    now: float,
    welcome_message_min_interval_seconds: int,
) -> bool:
    """
    Check whether the welcome cooldown has elapsed.

    Args:
    - last_welcome_sent_at - unix timestamp of the last sent welcome message
    - now - current unix timestamp
    - welcome_message_min_interval_seconds - minimum interval between sent welcomes

    Returns: True when a welcome message may be sent now
    """
    if last_welcome_sent_at is None:
        return True
    return now - last_welcome_sent_at >= welcome_message_min_interval_seconds


def _contacts_by_id(contacts: list[ContactRecipient]) -> dict[str, ContactRecipient]:
    """
    Build a contact lookup by Signal recipient id.

    Args:
    - contacts - contacts returned by signal-cli

    Returns: mapping from uuid or number to contact
    """
    result: dict[str, ContactRecipient] = {}
    for contact in contacts:
        if contact.uuid:
            result[contact.uuid] = contact
        if contact.number:
            result[contact.number] = contact
    return result


def _render_member_name(
    member: GroupMember,
    contacts_by_id: dict[str, ContactRecipient],
) -> str:
    """
    Render the preferred welcome name for a group member.

    Args:
    - member - group member returned by signal-cli
    - contacts_by_id - lookup of contacts by Signal recipient id

    Returns: rendered member name for the welcome message
    """
    member_id = member.uuid or member.number
    contact = contacts_by_id.get(member_id) if member_id else None

    contact_name = _preferred_contact_name(contact)
    if contact_name:
        return contact_name

    if member.name and not _looks_like_phone_number(member.name):
        return member.name
    username = _normalize_username(
        contact.username if contact and contact.username else member.username
    )
    if username:
        return username
    if contact and contact.name and not _looks_like_phone_number(contact.name):
        return contact.name
    return ""


def _render_member_name_by_id(
    member_id: str,
    contacts_by_id: dict[str, ContactRecipient],
) -> str:
    """
    Render the preferred name for a member when only the recipient id is known.

    Args:
    - member_id - Signal recipient id
    - contacts_by_id - lookup of contacts by Signal recipient id

    Returns: rendered member name for logs
    """
    contact = contacts_by_id.get(member_id)
    contact_name = _preferred_contact_name(contact)
    if contact_name:
        return contact_name
    username = _normalize_username(contact.username if contact else None)
    if username:
        return username
    return ""


def _render_welcome_targets(names: list[str], new_member_count: int) -> str:
    """
    Render the welcome target string without exposing phone numbers.

    Args:
    - names - candidate rendered names for the new members
    - new_member_count - number of members who joined

    Returns: welcome target text
    """
    rendered_names = [name for name in names if name]
    if rendered_names:
        return ", ".join(rendered_names)
    if new_member_count == 1:
        return "our new member"
    return "our new members"


def _preferred_contact_name(contact: ContactRecipient | None) -> str:
    """
    Extract the best non-phone human-readable name from a Signal contact.

    Args:
    - contact - contact returned by signal-cli

    Returns: preferred contact name, or an empty string
    """
    if contact is None:
        return ""

    candidates = [
        _join_name_parts(
            contact.profile.given_name if contact.profile else None,
            contact.profile.family_name if contact.profile else None,
        ),
        _join_name_parts(contact.given_name, contact.family_name),
        _join_name_parts(contact.nick_given_name, contact.nick_family_name),
        contact.nick_name,
        contact.name,
    ]
    for candidate in candidates:
        if candidate and not _looks_like_phone_number(candidate):
            return candidate
    return ""


def _join_name_parts(first: str | None, last: str | None) -> str:
    """
    Join optional name parts into a single display name.

    Args:
    - first - first or given name
    - last - last or family name

    Returns: combined display name, or an empty string
    """
    parts = [part.strip() for part in [first, last] if part and part.strip()]
    return " ".join(parts)


def _normalize_username(username: str | None) -> str | None:
    """
    Normalize a Signal username to @handle form.

    Args:
    - username - username returned by signal-cli

    Returns: username prefixed with @, or None
    """
    if not username:
        return None
    normalized = username.strip()
    if not normalized:
        return None
    if normalized.startswith("@"):
        return normalized
    return f"@{normalized}"


def _looks_like_phone_number(value: str) -> bool:
    """
    Detect whether a string is probably a phone number.

    Args:
    - value - rendered member value

    Returns: True when the value resembles a phone number
    """
    normalized = re.sub(r"[\s().-]", "", value)
    return bool(re.fullmatch(r"\+?\d{7,}", normalized))
