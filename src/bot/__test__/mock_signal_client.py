from __future__ import annotations

from unittest.mock import AsyncMock

from bot.signal_cli import ContactRecipient, SignalGroup, SignalPayload


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
