from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import (
    ContactRecipient,
    GroupMember,
    SignalGroup,
    SignalClient,
    SignalPayload,
    create_signal_client,
)


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
    signal_daemon_socket_path: Path
    group_cache_ttl_seconds: float
    unresolved_name_retry_delay_seconds: float


@dataclass
class BotRuntime:
    group_cache_ttl_seconds: float
    cached_welcome_group: SignalGroup | None = None
    cached_welcome_group_fetched_at: float | None = None


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
    except Exception:
        logger.exception("Signal bot crashed")
        raise


async def _run(config: BotConfig) -> None:
    logger.info("Starting signal bot for account {}", config.account)
    logger.info("Bot config: {}", config)

    client = create_signal_client(
        account=config.account,
        command_timeout_seconds=config.signal_cli_timeout_seconds,
        receive_timeout_seconds=config.signal_receive_timeout_seconds,
        daemon_socket_path=config.signal_daemon_socket_path,
    )
    runtime = BotRuntime(
        group_cache_ttl_seconds=config.group_cache_ttl_seconds,
    )
    try:
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
            await _discard_startup_backlog(client)

        i = 0
        while True:
            try:
                payloads = await client.receive_events()
            except Exception:
                logger.exception("Failed while receiving Signal events")
                raise
            for payload in payloads:
                logger.debug(payload)
                if (
                    payload.is_group_update()
                    and payload.extract_group_id() == state.welcome_group_id
                ):
                    try:
                        await _greet_new_welcome_group_members(
                            client,
                            runtime,
                            state,
                            config.state_path,
                            config.welcome_message,
                            config.welcome_message_min_interval_seconds,
                            config.unresolved_name_retry_delay_seconds,
                        )
                    except RuntimeError as exc:
                        logger.error("Error handling group update: {}", exc)
            try:
                await _flush_pending_welcome_messages(
                    client,
                    runtime,
                    state,
                    config.state_path,
                    config.welcome_message,
                    config.welcome_message_min_interval_seconds,
                    config.unresolved_name_retry_delay_seconds,
                )
            except RuntimeError as exc:
                logger.error("Error flushing pending welcomes: {}", exc)
            if i % 10 == 0:
                logger.debug("Bot idling")
            i += 1
    finally:
        await client.close()
        logger.info("Shutting down bot")


async def _seed_state(client: SignalClient, welcome_group: str) -> BotState:
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


async def _discard_startup_backlog(client: SignalClient) -> None:
    discarded_event_count = 0
    discarded_batch_count = 0
    while True:
        payloads = await client.receive_events()
        if not payloads:
            break
        discarded_batch_count += 1
        discarded_event_count += len(payloads)
    logger.info(
        "Discarded {} queued Signal event(s) across {} startup batch(es) after seeding",
        discarded_event_count,
        discarded_batch_count,
    )


async def _greet_new_welcome_group_members(
    client: SignalClient,
    runtime: BotRuntime,
    state: BotState,
    state_path: Path,
    welcome_message: str,
    welcome_message_min_interval_seconds: int,
    unresolved_name_retry_delay_seconds: float,
) -> None:
    """
    Update welcome-group membership state and greet newly joined members.

    Args:
    - client - Signal client used by the bot
    - runtime - in-memory caches shared by the bot loop
    - state - persisted bot state
    - state_path - path to the state file
    - welcome_message - welcome message template
    - welcome_message_min_interval_seconds - minimum delay between greetings
    - unresolved_name_retry_delay_seconds - retry delay for recipient-scoped contact lookups

    Returns: None
    """
    # Get group info
    group_id = state.welcome_group_id
    group = await _get_welcome_group(
        client,
        runtime,
        state.welcome_group_id,
        force_refresh=True,
    )
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

    # Update state based on members coming or going
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
        contacts_by_id = await _get_contacts_by_id(client, sorted(removed_members))
    if removed_members:
        pending_members -= removed_members
        removed_member_names = [
            _render_member_name_by_id(member_id, contacts_by_id)
            for member_id in sorted(removed_members)
        ]
        resolved_removed_member_names = [
            name for name in removed_member_names if name is not None
        ]
        logger.info(
            "Members left group {}: {}",
            group_id,
            ", ".join(resolved_removed_member_names)
            if resolved_removed_member_names
            else (
                "an unnamed member" if len(removed_members) == 1 else "unnamed members"
            ),
        )
    if new_members:
        pending_members |= new_members
    state.welcome_group_members = sorted(members)
    state.pending_welcome_members = sorted(pending_members)
    _save_state(state, state_path)

    if not new_members:
        return

    # Can we send a welcome message already or do we need to wait to maintain
    # welcome_message_min_interval_seconds?
    now = time.time()
    duration_till_welcome_msg = _duration_till_welcome_msg(
        state.last_welcome_sent_at,
        now,
        welcome_message_min_interval_seconds,
    )
    if duration_till_welcome_msg is not None:
        logger.info(
            f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
        )
        return

    # Send!
    await _send_welcome_messages(
        client,
        runtime,
        state,
        state_path,
        welcome_message,
        pending_members,
        now,
        group,
        unresolved_name_retry_delay_seconds,
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
    client: SignalClient,
    runtime: BotRuntime,
    state: BotState,
    state_path: Path,
    welcome_message: str,
    welcome_message_min_interval_seconds: int,
    unresolved_name_retry_delay_seconds: float,
) -> None:
    """
    Send queued welcome messages once the rate-limit window has elapsed.

    Args:
    - client - Signal client used by the bot
    - runtime - in-memory caches shared by the bot loop
    - state - persisted bot state
    - state_path - path to the state file
    - welcome_message - welcome message template
    - welcome_message_min_interval_seconds - minimum delay between greetings
    - unresolved_name_retry_delay_seconds - retry delay for recipient-scoped contact lookups

    Returns: None
    """
    pending_members = {str(member_id) for member_id in state.pending_welcome_members}
    if not pending_members:
        return

    now = time.time()
    duration_till_welcome_msg = _duration_till_welcome_msg(
        state.last_welcome_sent_at,
        now,
        welcome_message_min_interval_seconds,
    )
    if duration_till_welcome_msg is not None:
        logger.debug(
            f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
        )
        return

    await _send_welcome_messages(
        client,
        runtime,
        state,
        state_path,
        welcome_message,
        pending_members,
        now,
        None,
        unresolved_name_retry_delay_seconds,
    )


async def _send_welcome_messages(
    client: SignalClient,
    runtime: BotRuntime,
    state: BotState,
    state_path: Path,
    welcome_message: str,
    new_members: set[str],
    now: float,
    group: SignalGroup | None,
    unresolved_name_retry_delay_seconds: float = 0.0,
) -> None:
    """
    Send the welcome message once pending members are stable enough to greet.

    This function handles the two most stateful pieces of logic in the bot:
    validating that pending members are still in the group, and retrying name
    resolution once when Signal initially does not expose user names.

    Args:
    - client - Signal client used by the bot
    - runtime - in-memory caches shared by the bot loop
    - state - persisted bot state
    - state_path - path to the state file
    - welcome_message - welcome message template
    - new_members - pending members to greet
    - now - current timestamp
    - group - optional already-fetched group snapshot
    - unresolved_name_retry_delay_seconds - retry delay when names are unresolved

    Returns: None
    """
    resolved_group = group
    if resolved_group is None:
        resolved_group = await _get_welcome_group(
            client,
            runtime,
            state.welcome_group_id,
            force_refresh=False,
        )
    group = resolved_group
    if group is None:
        logger.error("Could not resolve group info for pending welcomes!")
        return

    current_member_ids = group.get_member_ids()
    new_members &= current_member_ids
    state.welcome_group_members = sorted(current_member_ids)
    if not new_members:
        state.pending_welcome_members = []
        _save_state(state, state_path)
        return

    # Resolve names of pending members
    group_members = group.members
    recipient_ids = _member_recipient_ids(group_members, new_members)
    contacts_by_id = await _get_contacts_by_id(client, recipient_ids)
    pending_member_names = [
        _render_member_name(member, contacts_by_id)
        for member in group_members
        if (member.uuid or member.number) in new_members
    ]
    if unresolved_name_retry_delay_seconds > 0 and any(
        candidate is None for candidate in pending_member_names
    ):
        _log_unresolved_members(group_members, new_members, contacts_by_id)
        logger.info(
            "Retrying unresolved member names for group {} after delay",
            state.welcome_group_id,
        )
        await asyncio.sleep(unresolved_name_retry_delay_seconds)
        contacts_by_id = await _get_contacts_by_id(client, recipient_ids)
        pending_member_names = [
            _render_member_name(member, contacts_by_id)
            for member in group_members
            if (member.uuid or member.number) in new_members
        ]
        if any(candidate is None for candidate in pending_member_names):
            _log_unresolved_members(group_members, new_members, contacts_by_id)

    # Prepare welcome message
    if any(name is None for name in pending_member_names):
        message = (
            welcome_message.replace(" {{newusers}}", "")
            .replace("{{newusers}} ", "")
            .replace("{{newusers}}", "")
        )
    else:
        resolved_names = [name for name in pending_member_names if name is not None]
        if len(resolved_names) == 1:
            rendered_names = resolved_names[0]
        elif len(resolved_names) == 2:
            rendered_names = f"{resolved_names[0]} and {resolved_names[1]}"
        else:
            rendered_names = (
                f"{', '.join(resolved_names[:-1])} and {resolved_names[-1]}"
            )
        message = welcome_message.replace("{{newusers}}", rendered_names)

    # Send
    await client.send_group_message(state.welcome_group_id, message)
    state.pending_welcome_members = []
    state.last_welcome_sent_at = now
    _save_state(state, state_path)
    logger.info(
        "Sent welcome message to group {} for {} member(s)",
        state.welcome_group_id,
        len(new_members),
    )


def _log_unresolved_members(
    group_members: list[GroupMember],
    new_members: set[str],
    contacts_by_id: dict[str, ContactRecipient],
) -> None:
    for member in group_members:
        member_id = member.uuid or member.number
        if member_id is None or member_id not in new_members:
            continue
        rendered_name = _render_member_name(member, contacts_by_id)
        if rendered_name is not None:
            continue
        contact = contacts_by_id.get(member_id)
        logger.debug(
            "Could not resolve member name for id={} member.name={!r} member.username={!r} "
            "contact_present={} contact.name={!r} contact.username={!r} "
            "contact.profile.given={!r} contact.profile.family={!r} "
            "contact.given={!r} contact.family={!r} contact.nick={!r}",
            member_id,
            member.name,
            member.username,
            contact is not None,
            contact.name if contact else None,
            contact.username if contact else None,
            contact.profile.given_name if contact and contact.profile else None,
            contact.profile.family_name if contact and contact.profile else None,
            contact.given_name if contact else None,
            contact.family_name if contact else None,
            contact.nick_name if contact else None,
        )


async def _get_welcome_group(
    client: SignalClient,
    runtime: BotRuntime,
    welcome_group_id: str,
    force_refresh: bool,
) -> SignalGroup | None:
    """
    Fetch the welcome group, reusing a short-lived cache when safe.

    Args:
    - client - Signal client used by the bot
    - runtime - in-memory runtime caches
    - welcome_group_id - resolved Signal group id
    - force_refresh - whether to bypass the cache

    Returns: welcome group details, if found
    """
    now = time.time()
    if (
        not force_refresh
        and runtime.cached_welcome_group is not None
        and runtime.cached_welcome_group.resolved_id == welcome_group_id
        and runtime.cached_welcome_group_fetched_at is not None
        and now - runtime.cached_welcome_group_fetched_at
        <= runtime.group_cache_ttl_seconds
    ):
        return runtime.cached_welcome_group

    group = await client.get_group_by_id(welcome_group_id)
    if group is not None:
        runtime.cached_welcome_group = group
        runtime.cached_welcome_group_fetched_at = now
    return group


async def _get_contacts_by_id(
    client: SignalClient,
    recipients: Iterable[str],
) -> dict[str, ContactRecipient]:
    """
    Fetch contacts for specific recipients and build a lookup.

    Args:
    - client - Signal client used by the bot
    - recipients - recipient ids to request from Signal

    Returns: mapping from uuid or number to contact
    """
    unique_recipients = list(
        dict.fromkeys(recipient for recipient in recipients if recipient)
    )
    contacts_by_id: dict[str, ContactRecipient] = {}
    if not unique_recipients:
        return contacts_by_id
    _merge_contacts_by_id(contacts_by_id, await client.list_contacts(unique_recipients))
    return contacts_by_id


def _member_recipient_ids(
    group_members: list[GroupMember],
    new_members: set[str],
) -> list[str]:
    return list(
        dict.fromkeys(
            recipient_id
            for recipient_id in (
                _resolve_member_recipient(member)
                for member in group_members
                if (member.uuid or member.number) in new_members
            )
            if recipient_id is not None
        )
    )


def _merge_contacts_by_id(
    contacts_by_id: dict[str, ContactRecipient],
    contacts: list[ContactRecipient],
) -> None:
    for contact in contacts:
        if contact.uuid:
            contacts_by_id[contact.uuid] = contact
        if contact.number:
            contacts_by_id[contact.number] = contact


def _duration_till_welcome_msg(
    last_welcome_sent_at: float | None,
    now: float,
    welcome_message_min_interval_seconds: int,
) -> float | None:
    if last_welcome_sent_at is None:
        return None
    duration = max(
        0, welcome_message_min_interval_seconds - (now - last_welcome_sent_at)
    )
    if duration == 0:
        return None
    return duration


def _render_member_name(
    member: GroupMember,
    contacts_by_id: dict[str, ContactRecipient],
) -> str | None:
    member_id = member.uuid or member.number
    contact = contacts_by_id.get(member_id) if member_id else None

    contact_name = _preferred_contact_name(contact)
    if contact_name is not None:
        return contact_name

    if member.name and not _looks_like_phone_number(member.name):
        return member.name
    username = _normalize_username(
        contact.username if contact and contact.username else member.username
    )
    if username is not None:
        return username
    if contact and contact.name and not _looks_like_phone_number(contact.name):
        return contact.name
    return None


def _resolve_member_recipient(member: GroupMember) -> str | None:
    if member.uuid:
        return member.uuid
    if member.number:
        return member.number
    if member.username:
        normalized = member.username.strip()
        if normalized:
            if normalized.startswith("u:"):
                return normalized
            if normalized.startswith("@"):
                return f"u:{normalized[1:]}"
            return f"u:{normalized}"
    return None


def _render_member_name_by_id(
    member_id: str,
    contacts_by_id: dict[str, ContactRecipient],
) -> str | None:
    contact = contacts_by_id.get(member_id)
    contact_name = _preferred_contact_name(contact)
    if contact_name is not None:
        return contact_name
    username = _normalize_username(contact.username if contact else None)
    if username is not None:
        return username
    return None


def _preferred_contact_name(contact: ContactRecipient | None) -> str | None:
    if contact is None:
        return None

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
    return None


def _join_name_parts(first: str | None, last: str | None) -> str | None:
    parts = [part.strip() for part in [first, last] if part and part.strip()]
    if not parts:
        return None
    return " ".join(parts)


def _normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    normalized = username.strip()
    if not normalized:
        return None
    if normalized.startswith("@"):
        return normalized
    return f"@{normalized}"


def _looks_like_phone_number(value: str) -> bool:
    normalized = re.sub(r"[\s().-]", "", value)
    return bool(re.fullmatch(r"\+?\d{7,}", normalized))
