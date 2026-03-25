from __future__ import annotations

import asyncio
import time
from pathlib import Path
import re

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import ContactRecipient, GroupMember, SignalCliClient, SignalPayload


def run_bot(
    account: str,
    state_path: Path,
    welcome_group: str,
    welcome_message: str,
    state_max_age_seconds: int,
    sync_on_startup: bool,
    signal_cli_timeout_seconds: float,
    signal_receive_timeout_seconds: int,
    receive_poll_delay_seconds: float,
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
                signal_cli_timeout_seconds=signal_cli_timeout_seconds,
                signal_receive_timeout_seconds=signal_receive_timeout_seconds,
                receive_poll_delay_seconds=receive_poll_delay_seconds,
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
    signal_cli_timeout_seconds: float,
    signal_receive_timeout_seconds: int,
    receive_poll_delay_seconds: float,
) -> None:
    logger.info("Starting signal bot for account {}", account)

    client = SignalCliClient(
        account,
        command_timeout_seconds=signal_cli_timeout_seconds,
        receive_timeout_seconds=signal_receive_timeout_seconds,
    )
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
        for payload in await client.receive_events():
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
        if receive_poll_delay_seconds > 0:
            logger.debug("sleep")
            await asyncio.sleep(receive_poll_delay_seconds)
            logger.debug("receive")


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
    removed_members = known_members - members
    logger.debug(
        "Group {} membership diff: known={}, current={}, new={}, removed={}",
        group_id,
        len(known_members),
        len(members),
        len(new_members),
        len(removed_members),
    )
    contacts = await client.list_contacts()
    contacts_by_id = _contacts_by_id(contacts)
    if removed_members:
        removed_member_names = [
            _render_member_name_by_id(member_id, contacts_by_id)
            for member_id in sorted(removed_members)
        ]
        logger.info(
            "Members left group {}: {}",
            group_id,
            _render_welcome_targets(removed_member_names, len(removed_members)),
        )
    if not new_members:
        state.welcome_group_members = sorted(members)
        _save_state(state, state_path)
        return

    # Greet new members
    group_members = await client.group_members(group_id)
    new_member_names = [
        _render_member_name(member, contacts_by_id)
        for member in group_members
        if (member.uuid or member.number) in new_members
    ]
    rendered_names = _render_welcome_targets(new_member_names, len(new_members))
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
