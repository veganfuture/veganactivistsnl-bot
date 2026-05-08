from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, Field

from bot.config import EventCalendarFeatureConfig, GeminiConfig
from bot.signal_cli import ContactRecipient, SignalClient, SignalPayload


class CalendarEvent(BaseModel):
    date: str | None = None
    time: str | None = None
    location: str | None = None
    description: str | None = None


class CalendarParseResponse(BaseModel):
    events: list[CalendarEvent] = Field(default_factory=list)


class CalendarEventRecord(BaseModel):
    source_group: str
    source_group_id: str
    sender_name: str | None = None
    message_text: str
    parsed_at: str
    date: str | None = None
    time: str | None = None
    location: str | None = None
    description: str | None = None


class CalendarEventParser(Protocol):
    async def parse_events(self, message_text: str) -> list[CalendarEvent]: ...


class EventCalendarFeature:
    name = "event-calendar"

    def __init__(
        self,
        config: EventCalendarFeatureConfig,
        client: SignalClient,
        parser: CalendarEventParser,
    ) -> None:
        self.config = config
        self.client = client
        self.parser = parser
        self.group_id: str | None = None
        self.output_recipient: str | None = None

    async def setup(self) -> None:
        """
        Resolve the configured event group and output user before entering the receive loop.

        Returns: None
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
        self.group_id = group.resolved_id
        output_contact = await self.resolve_output_contact()
        self.output_recipient = output_contact.number
        if self.output_recipient is None:
            raise RuntimeError(
                f"Output user contact {self.config.output_user} does not expose a phone number"
            )
        logger.info(
            "Event calendar enabled for group {} ({}) and output user {}",
            self.config.group_name,
            self.group_id,
            self.output_recipient,
        )

    async def handle_payloads(
        self,
        payloads: list[SignalPayload],
        cycle_finished_at: float,
    ) -> None:
        """
        Parse event-like group messages into structured calendar items.

        Args:
        - payloads - payloads returned by Signal
        - cycle_finished_at - monotonic timestamp for the end of the receive cycle

        Returns: None
        """
        del cycle_finished_at
        if self.group_id is None:
            raise RuntimeError("Event calendar group has not been initialized")

        for payload in payloads:
            if payload.extract_group_id() != self.group_id:
                continue
            if payload.is_group_update():
                continue
            message_text = payload.extract_message_text()
            if message_text is None:
                continue

            events = await self.parser.parse_events(message_text)
            if not events:
                logger.debug(
                    "No calendar events parsed from message in group {}",
                    self.group_id,
                )
                continue

            await self.send_calendar_events(
                events,
                message_text,
                payload.sender_name(),
            )
            logger.info(
                "Parsed {} calendar item(s) from group {}",
                len(events),
                self.group_id,
            )

    async def on_cycle(self, cycle_finished_at: float) -> None:
        """
        Perform any per-cycle work for the event calendar feature.

        Args:
        - cycle_finished_at - monotonic timestamp for the end of the receive cycle
        """
        del cycle_finished_at

    async def send_calendar_events(
        self,
        events: list[CalendarEvent],
        message_text: str,
        sender_name: str | None,
    ) -> None:
        """
        Send parsed calendar items to the configured output user.

        Args:
        - events - parsed calendar items
        - message_text - raw Signal message text
        - sender_name - optional rendered sender name

        Returns: None
        """
        if self.group_id is None:
            raise RuntimeError("Event calendar group has not been initialized")
        if self.output_recipient is None:
            raise RuntimeError("Output user has not been initialized")

        parsed_at = datetime.now(ZoneInfo(self.config.gemini.timezone_name)).isoformat()
        for event in events:
            record = CalendarEventRecord(
                source_group=self.config.group_name,
                source_group_id=self.group_id,
                sender_name=sender_name,
                message_text=message_text,
                parsed_at=parsed_at,
                date=event.date,
                time=event.time,
                location=event.location,
                description=event.description,
            )
            await self.client.send_contact_message(
                self.output_recipient,
                record.model_dump_json(indent=2),
            )

    async def resolve_output_contact(self) -> ContactRecipient:
        """
        Resolve the configured output user from the Signal contacts.

        Returns: matching Signal contact
        """
        matching_contacts = await self.client.list_contacts([self.config.output_user])
        if not matching_contacts:
            matching_contacts = await self.client.list_contacts()

        normalized_output_user = _normalize_phone_number(self.config.output_user)
        for contact in matching_contacts:
            if _normalize_phone_number(contact.number) == normalized_output_user:
                return contact

        raise RuntimeError(
            f"Could not find output user in contacts: {self.config.output_user}"
        )


class GeminiCalendarEventParser:
    def __init__(self, config: GeminiConfig) -> None:
        self.config = config

    async def parse_events(self, message_text: str) -> list[CalendarEvent]:
        """
        Parse event-like message text into calendar items with Gemini.

        Args:
        - message_text - raw Signal message text

        Returns: parsed calendar items
        """
        parsed = await asyncio.wait_for(
            asyncio.to_thread(self._request_parse, message_text),
            timeout=self.config.timeout_seconds + 1,
        )
        return [
            event
            for event in parsed.events
            if any([event.date, event.time, event.location, event.description])
        ]

    def _request_parse(self, message_text: str) -> CalendarParseResponse:
        timezone = ZoneInfo(self.config.timezone_name)
        today = datetime.now(timezone).date().isoformat()
        client = genai.Client(
            api_key=self.config.api_key,
            http_options=types.HttpOptions(
                timeout=max(1, int(self.config.timeout_seconds))
            ),
        )
        response = client.models.generate_content(
            model=self.config.model,
            contents=f"Signal message:\n{message_text}",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Extract meetup or event information from the provided Signal "
                    "message. If the message is not an event, return an empty list. "
                    f"Interpret relative dates using timezone {self.config.timezone_name} "
                    f"and current date {today}."
                ),
                response_mime_type="application/json",
                response_schema=CalendarParseResponse,
                temperature=0.1,
            ),
        )
        if isinstance(response.parsed, CalendarParseResponse):
            return response.parsed
        if response.text is None:
            raise RuntimeError("Gemini response did not contain parseable content")
        return CalendarParseResponse.model_validate_json(response.text)


def _normalize_phone_number(number: str | None) -> str | None:
    if number is None:
        return None
    return "".join(character for character in number if character in "+0123456789")
