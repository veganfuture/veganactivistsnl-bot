from __future__ import annotations

import time

from loguru import logger
from pydantic import BaseModel, Field

from bot.config import WelcomeFeatureConfig
from bot.signal_cli import SignalClient, SignalGroup, SignalPayload


class WelcomeState(BaseModel):
    welcome_group_id: str
    welcome_group_members: list[str] = Field(default_factory=list)
    pending_welcome_members: list[str] = Field(default_factory=list)
    last_welcome_sent_at: float | None = None


class WelcomeFeature:
    name = "welcome"

    def __init__(
        self,
        config: WelcomeFeatureConfig,
        client: SignalClient,
    ) -> None:
        self.config = config
        self.client = client
        self.welcome_state: WelcomeState | None = None
        self.last_membership_reconcile_at = 0.0

    async def setup(self) -> None:
        """
        Initialize welcome state before entering the receive loop.

        Returns: None
        """
        self.welcome_state = self.load_welcome_state()
        if self.welcome_state:
            logger.info(
                f"Welcome state loaded from: {self.config.welcome_state_path}"
            )
        if not self.welcome_state:
            logger.info("No welcome state found, seeding")
            self.welcome_state = await self.seed_welcome_state()
            self.save_welcome_state()
            logger.info("Welcome state seeded")
            await self.discard_startup_backlog()
        self.last_membership_reconcile_at = time.monotonic()

    async def handle_payloads(
        self,
        payloads: list[SignalPayload],
        cycle_finished_at: float,
    ) -> None:
        """
        Reconcile welcome-group membership when relevant Signal updates arrive.

        Args:
        - payloads - payloads returned by Signal
        - cycle_finished_at - monotonic timestamp for the end of the receive cycle

        Returns: None
        """
        welcome_state = self.require_welcome_state()
        reconciled = False
        for payload in payloads:
            logger.debug(payload)
            if (
                payload.is_group_update()
                and payload.extract_group_id() == welcome_state.welcome_group_id
            ):
                await self.greet_new_welcome_group_members()
                self.last_membership_reconcile_at = cycle_finished_at
                reconciled = True
        if reconciled:
            return

        should_reconcile_membership = (
            self.config.periodic_membership_reconcile_interval_seconds > 0
            and cycle_finished_at - self.last_membership_reconcile_at
            >= self.config.periodic_membership_reconcile_interval_seconds
        )
        if not should_reconcile_membership:
            return

        await self.greet_new_welcome_group_members()
        self.last_membership_reconcile_at = cycle_finished_at

    async def on_cycle(self, cycle_finished_at: float) -> None:
        """
        Flush pending welcome messages after each receive cycle.

        Args:
        - cycle_finished_at - monotonic timestamp for the end of the receive cycle

        Returns: None
        """
        del cycle_finished_at
        await self.flush_pending_welcome_messages()

    def require_welcome_state(self) -> WelcomeState:
        """
        Return the initialized welcome state.

        Returns: current welcome state
        """
        if self.welcome_state is None:
            raise RuntimeError("Welcome state has not been initialized")
        return self.welcome_state

    def load_welcome_state(self) -> WelcomeState | None:
        """
        Load persisted welcome state if it exists and is still fresh enough.

        Returns: loaded welcome state, or None when reseeding is required
        """
        if not self.config.welcome_state_path.exists():
            return None
        age_seconds = time.time() - self.config.welcome_state_path.stat().st_mtime
        if age_seconds > self.config.welcome_state_max_age_seconds:
            logger.info("Welcome state file is stale ({}s), reseeding", int(age_seconds))
            return None

        return WelcomeState.model_validate_json(
            self.config.welcome_state_path.read_text()
        )

    def save_welcome_state(self) -> None:
        """
        Persist the current welcome state to disk.

        Returns: None
        """
        self.config.welcome_state_path.parent.mkdir(parents=True, exist_ok=True)
        welcome_state = self.require_welcome_state()
        encoded = WelcomeState(
            welcome_group_id=welcome_state.welcome_group_id,
            welcome_group_members=sorted(
                {str(member) for member in welcome_state.welcome_group_members}
            ),
            pending_welcome_members=sorted(
                {str(member) for member in welcome_state.pending_welcome_members}
            ),
            last_welcome_sent_at=welcome_state.last_welcome_sent_at,
        )
        self.config.welcome_state_path.write_text(encoded.model_dump_json(indent=2))

    async def seed_welcome_state(self) -> WelcomeState:
        """
        Build initial welcome state from the configured group membership.

        Returns: freshly seeded welcome state
        """
        group = await self.client.get_group_by_name(self.config.group_name)

        if group is None:
            groups = await self.client.list_groups()
            group_names = [group.name for group in groups]
            raise RuntimeError(
                f"Could not find listing for group: {self.config.group_name}. Groups found: {group_names}"
            )

        if group.resolved_id is None:
            raise RuntimeError(
                f"Could not resolve group_id for group: {self.config.group_name}"
            )

        return WelcomeState(
            welcome_group_id=group.resolved_id,
            welcome_group_members=sorted(group.get_member_ids()),
            pending_welcome_members=[],
            last_welcome_sent_at=None,
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
        welcome_state = self.require_welcome_state()
        group = await self.get_welcome_group()
        if group is None:
            logger.error("Could not resolve group info for welcome group!")
            return

        logger.debug(
            "Welcome group {} returned {} member(s) from signal-cli",
            welcome_state.welcome_group_id,
            len(group.members),
        )
        members = group.get_member_ids()
        known_members = {
            str(member_id) for member_id in welcome_state.welcome_group_members
        }
        if not known_members:
            welcome_state.welcome_group_members = sorted(members)
            self.save_welcome_state()
            return

        new_members = members - known_members
        removed_members = known_members - members
        pending_members = {
            str(member_id) for member_id in welcome_state.pending_welcome_members
        }
        logger.debug(
            "Group {} membership diff: known={}, current={}, new={}, removed={}, pending={}",
            welcome_state.welcome_group_id,
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
                welcome_state.welcome_group_id,
                ", ".join(sorted(removed_members)),
            )
        if new_members:
            pending_members |= new_members
        welcome_state.welcome_group_members = sorted(members)
        welcome_state.pending_welcome_members = sorted(pending_members)
        self.save_welcome_state()

        if not new_members:
            return

        now = time.time()
        duration_till_welcome_msg = _duration_till_welcome_msg(
            welcome_state.last_welcome_sent_at,
            now,
            self.config.message_min_interval_seconds,
        )
        if duration_till_welcome_msg is not None:
            logger.info(
                f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
            )
            return

        await self.send_welcome_messages(pending_members, now, group)

    async def flush_pending_welcome_messages(self) -> None:
        """
        Send queued welcome messages once any wait windows have elapsed.

        Returns: None
        """
        welcome_state = self.require_welcome_state()
        pending_members = {
            str(member_id) for member_id in welcome_state.pending_welcome_members
        }
        if not pending_members:
            return

        now = time.time()
        duration_till_welcome_msg = _duration_till_welcome_msg(
            welcome_state.last_welcome_sent_at,
            now,
            self.config.message_min_interval_seconds,
        )
        if duration_till_welcome_msg is not None:
            logger.debug(
                f"{len(pending_members)} pending members queued for another {duration_till_welcome_msg:.0f} seconds"
            )
            return

        await self.send_welcome_messages(pending_members, now, None)

    async def send_welcome_messages(
        self,
        new_members: set[str],
        now: float,
        group: SignalGroup | None,
    ) -> None:
        """
        Send the welcome message once pending members are stable enough to greet.

        Args:
        - new_members - pending member ids to greet
        - now - current timestamp
        - group - optional already-fetched group snapshot

        Returns: None
        """
        welcome_state = self.require_welcome_state()
        resolved_group = group
        if resolved_group is None:
            resolved_group = await self.get_welcome_group()
        group = resolved_group
        if group is None:
            logger.error("Could not resolve group info for pending welcomes!")
            return

        current_member_ids = group.get_member_ids()
        new_members &= current_member_ids
        welcome_state.welcome_group_members = sorted(current_member_ids)
        if not new_members:
            welcome_state.pending_welcome_members = []
            self.save_welcome_state()
            return

        await self.client.send_group_message(
            welcome_state.welcome_group_id,
            self.config.message,
        )
        welcome_state.pending_welcome_members = []
        welcome_state.last_welcome_sent_at = now
        self.save_welcome_state()
        logger.info(
            "Sent welcome message to group {} for {} member(s)",
            welcome_state.welcome_group_id,
            len(new_members),
        )

    async def get_welcome_group(self) -> SignalGroup | None:
        """
        Fetch the configured welcome group.

        Returns: welcome group details, if found
        """
        welcome_state = self.require_welcome_state()
        return await self.client.get_group_by_id(welcome_state.welcome_group_id)


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
