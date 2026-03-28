from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from loguru import logger
from pydantic import BaseModel, Field

from bot.signal_cli import ContactRecipient, GroupMember, SignalGroup, SignalClient, create_signal_client


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
    unresolved_name_retry_delay_seconds: float
    periodic_membership_reconcile_cycles: int


class BotState(BaseModel):
    welcome_group_id: str
    welcome_group_members: list[str] = Field(default_factory=list)
    pending_welcome_members: list[str] = Field(default_factory=list)
    last_welcome_sent_at: float | None = None
    pending_name_retry_at: float | None = None
    """
    list of member ids (uuid or number)
    """


def run_bot(config: BotConfig) -> None:
    try:
        asyncio.run(Bot(config).run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Signal bot crashed")
        raise


class Bot:
    def __init__(
        self,
        config: BotConfig,
        client: SignalClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or create_signal_client(
            account=config.account,
            command_timeout_seconds=config.signal_cli_timeout_seconds,
            receive_timeout_seconds=config.signal_receive_timeout_seconds,
            daemon_socket_path=config.signal_daemon_socket_path,
        )
        self.state: BotState | None = None

    async def run(self) -> None:
        """
        Run the bot event loop until shutdown or failure.

        Returns: None
        """
        logger.info("Starting signal bot for account {}", self.config.account)
        logger.info("Bot config: {}", self.config)

        try:
            if self.config.sync_on_startup:
                logger.info("Requesting Signal sync on startup")
                await self.client.send_sync_request()

            self.state = self.load_state()
            if self.state:
                logger.info(f"Bot state loaded from: {self.config.state_path}")
            if not self.state:
                logger.info("No bot state found, seeding")
                self.state = await self.seed_state()
                self.save_state()
                logger.info("Bot state seeded")
                await self.discard_startup_backlog()

            i = 0
            while True:
                try:
                    payloads = await self.client.receive_events()
                except Exception:
                    logger.exception("Failed while receiving Signal events")
                    raise
                should_reconcile_membership = (
                    self.config.periodic_membership_reconcile_cycles > 0
                    and i % self.config.periodic_membership_reconcile_cycles == 0
                )
                state = self.require_state()
                for payload in payloads:
                    logger.debug(payload)
                    if (
                        payload.is_group_update()
                        and payload.extract_group_id() == state.welcome_group_id
                    ):
                        should_reconcile_membership = False
                        try:
                            await self.greet_new_welcome_group_members()
                        except RuntimeError as exc:
                            logger.error("Error handling group update: {}", exc)
                if should_reconcile_membership:
                    try:
                        await self.greet_new_welcome_group_members()
                    except RuntimeError as exc:
                        logger.error(
                            "Error reconciling welcome group membership: {}",
                            exc,
                        )
                try:
                    await self.flush_pending_welcome_messages()
                except RuntimeError as exc:
                    logger.error("Error flushing pending welcomes: {}", exc)
                if i % 10 == 0:
                    logger.debug("Bot idling")
                i += 1
        finally:
            await self.client.close()
            logger.info("Shutting down bot")

    def require_state(self) -> BotState:
        """
        Return the initialized bot state.

        Returns: current bot state
        """
        if self.state is None:
            raise RuntimeError("Bot state has not been initialized")
        return self.state

    def load_state(self) -> BotState | None:
        """
        Load persisted bot state if it exists and is still fresh enough.

        Returns: loaded bot state, or None when reseeding is required
        """
        if not self.config.state_path.exists():
            return None
        age_seconds = time.time() - self.config.state_path.stat().st_mtime
        if age_seconds > self.config.state_max_age_seconds:
            logger.info("State file is stale ({}s), reseeding", int(age_seconds))
            return None

        return BotState.model_validate_json(self.config.state_path.read_text())

    def save_state(self) -> None:
        """
        Persist the current bot state to disk.

        Returns: None
        """
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = self.require_state()
        encoded = BotState(
            welcome_group_id=state.welcome_group_id,
            welcome_group_members=sorted(
                {str(member) for member in state.welcome_group_members}
            ),
            pending_welcome_members=sorted(
                {str(member) for member in state.pending_welcome_members}
            ),
            last_welcome_sent_at=state.last_welcome_sent_at,
            pending_name_retry_at=state.pending_name_retry_at,
        )
        self.config.state_path.write_text(encoded.model_dump_json(indent=2))

    async def seed_state(self) -> BotState:
        """
        Build initial bot state from the configured welcome group membership.

        Returns: freshly seeded bot state
        """
        group = await self.client.get_group_by_name(self.config.welcome_group)

        if group is None:
            groups = await self.client.list_groups()
            group_names = [group.name for group in groups]
            raise RuntimeError(
                f"Could not find listing for group: {self.config.welcome_group}. Groups found: {group_names}"
            )

        if group.resolved_id is None:
            raise RuntimeError(
                f"Could not resolve group_id for group: {self.config.welcome_group}"
            )

        return BotState(
            welcome_group_id=group.resolved_id,
            welcome_group_members=sorted(group.get_member_ids()),
            pending_welcome_members=[],
            last_welcome_sent_at=None,
            pending_name_retry_at=None,
        )

    async def discard_startup_backlog(self) -> None:
        """
        Drain any queued Signal events after seeding initial state.

        Returns: None
        """
        discarded_event_count = 0
        discarded_batch_count = 0
        while True:
            payloads = await self.client.receive_events()
            if not payloads:
                break
            discarded_batch_count += 1
            discarded_event_count += len(payloads)
        logger.info(
            "Discarded {} queued Signal event(s) across {} startup batch(es) after seeding",
            discarded_event_count,
            discarded_batch_count,
        )

    async def greet_new_welcome_group_members(self) -> None:
        """
        Reconcile welcome-group membership and queue or send greetings for joins.

        Returns: None
        """
        state = self.require_state()

        group_id = state.welcome_group_id
        group = await self.get_welcome_group(force_refresh=True)
        if group is None:
            logger.error("Could not resolve group info for welcome group!")
            return

        members = group.get_member_ids()
        known_members = {str(member_id) for member_id in state.welcome_group_members}
        if not known_members:
            state.welcome_group_members = sorted(members)
            self.save_state()
            return

        # Update membership state based on members coming or going.
        new_members = members - known_members
        removed_members = known_members - members
        pending_members = {
            str(member_id) for member_id in state.pending_welcome_members
        }
        logger.debug(
            "Group {} membership diff: known={}, current={}, new={}, removed={}, pending={}",
            group_id,
            len(known_members),
            len(members),
            len(new_members),
            len(removed_members),
            len(pending_members),
        )
        if removed_members:
            pending_members -= removed_members
            logger.info(
                "Members left group {}: {}",
                group_id,
                ", ".join(sorted(removed_members)),
            )
        if new_members:
            pending_members |= new_members
        state.welcome_group_members = sorted(members)
        state.pending_welcome_members = sorted(pending_members)
        if not pending_members:
            state.pending_name_retry_at = None
        self.save_state()

        if not new_members:
            return

        # Respect welcome-message throttling and deferred name retries.
        now = time.time()
        duration_till_welcome_msg = _duration_till_welcome_msg(
            state.last_welcome_sent_at,
            now,
            self.config.welcome_message_min_interval_seconds,
        )
        if duration_till_welcome_msg is not None:
            logger.info(
                f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
            )
            return

        if (
            state.pending_name_retry_at is not None
            and now < state.pending_name_retry_at
        ):
            logger.debug(
                "Pending member-name retry for group {} in another {:.0f} seconds",
                state.welcome_group_id,
                state.pending_name_retry_at - now,
            )
            return

        await self.send_welcome_messages(
            pending_members,
            now,
            group,
            self.config.unresolved_name_retry_delay_seconds,
        )

    async def flush_pending_welcome_messages(self) -> None:
        """
        Send queued welcome messages once any wait windows have elapsed.

        Returns: None
        """
        state = self.require_state()
        pending_members = {
            str(member_id) for member_id in state.pending_welcome_members
        }
        if not pending_members:
            return

        now = time.time()
        duration_till_welcome_msg = _duration_till_welcome_msg(
            state.last_welcome_sent_at,
            now,
            self.config.welcome_message_min_interval_seconds,
        )
        if duration_till_welcome_msg is not None:
            logger.debug(
                f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
            )
            return

        if (
            state.pending_name_retry_at is not None
            and now < state.pending_name_retry_at
        ):
            logger.debug(
                "Pending member-name retry for group {} in another {:.0f} seconds",
                state.welcome_group_id,
                state.pending_name_retry_at - now,
            )
            return

        await self.send_welcome_messages(
            pending_members,
            now,
            None,
            self.config.unresolved_name_retry_delay_seconds,
        )

    async def send_welcome_messages(
        self,
        new_members: set[str],
        now: float,
        group: SignalGroup | None,
        unresolved_name_retry_delay_seconds: float = 0.0,
    ) -> None:
        """
        Send the welcome message once pending members are stable enough to greet.

        Args:
        - new_members - pending member ids to greet
        - now - current timestamp
        - group - optional already-fetched group snapshot
        - unresolved_name_retry_delay_seconds - retry delay when names are unresolved

        Returns: None
        """
        state = self.require_state()
        resolved_group = group
        if resolved_group is None:
            resolved_group = await self.get_welcome_group(force_refresh=False)
        group = resolved_group
        if group is None:
            logger.error("Could not resolve group info for pending welcomes!")
            return

        current_member_ids = group.get_member_ids()
        new_members &= current_member_ids
        state.welcome_group_members = sorted(current_member_ids)
        if not new_members:
            state.pending_welcome_members = []
            self.save_state()
            return

        # Resolve names for the members we are about to mention in the message.
        group_members = group.members
        recipient_ids = _member_recipient_ids(group_members, new_members)
        contacts_by_id = await self.get_contacts_by_id()
        pending_member_names = [
            _render_member_name(member, contacts_by_id)
            for member in group_members
            if (member.uuid or member.number) in new_members
        ]
        unresolved_names_present = any(
            candidate is None for candidate in pending_member_names
        )
        if unresolved_names_present:
            scoped_contacts_by_id = await self.get_contacts_by_id(recipient_ids)
            if scoped_contacts_by_id:
                _merge_contacts_by_id(
                    contacts_by_id,
                    list(scoped_contacts_by_id.values()),
                )
                pending_member_names = [
                    _render_member_name(member, contacts_by_id)
                    for member in group_members
                    if (member.uuid or member.number) in new_members
                ]
                unresolved_names_present = any(
                    candidate is None for candidate in pending_member_names
                )
        if (
            unresolved_names_present
            and unresolved_name_retry_delay_seconds > 0
            and state.pending_name_retry_at is None
        ):
            _log_unresolved_members(
                group_members,
                new_members,
                contacts_by_id,
            )
            logger.info(
                "Deferring unresolved member-name retry for group {} by {:.0f} seconds",
                state.welcome_group_id,
                unresolved_name_retry_delay_seconds,
            )
            state.pending_name_retry_at = now + unresolved_name_retry_delay_seconds
            self.save_state()
            return
        if unresolved_names_present:
            _log_unresolved_members(
                group_members,
                new_members,
                contacts_by_id,
            )

        # Render the welcome message with names when available.
        if any(name is None for name in pending_member_names):
            message = (
                self.config.welcome_message.replace(" {{newusers}}", "")
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
            message = self.config.welcome_message.replace(
                "{{newusers}}",
                rendered_names,
            )

        await self.client.send_group_message(state.welcome_group_id, message)
        state.pending_welcome_members = []
        state.last_welcome_sent_at = now
        state.pending_name_retry_at = None
        self.save_state()
        logger.info(
            "Sent welcome message to group {} for {} member(s)",
            state.welcome_group_id,
            len(new_members),
        )

    async def get_welcome_group(self, force_refresh: bool) -> SignalGroup | None:
        """
        Fetch the configured welcome group.

        Args:
        - force_refresh - unused compatibility flag for callers that always need a fresh lookup

        Returns: welcome group details, if found
        """
        state = self.require_state()
        return await self.client.get_group_by_id(state.welcome_group_id)

    async def get_contacts_by_id(
        self,
        recipients: Iterable[str] | None = None,
    ) -> dict[str, ContactRecipient]:
        """
        Fetch contacts for specific recipients and build a lookup.

        Args:
        - recipients - optional recipient ids to request from Signal

        Returns: mapping from uuid or number to contact
        """
        contacts_by_id: dict[str, ContactRecipient] = {}
        unique_recipients: list[str] | None = None
        if recipients is not None:
            unique_recipients = list(
                dict.fromkeys(recipient for recipient in recipients if recipient)
            )
            if not unique_recipients:
                return contacts_by_id
        _merge_contacts_by_id(contacts_by_id, await self.client.list_contacts(unique_recipients))
        return contacts_by_id


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
