import asyncio
import json
from dataclasses import dataclass
from typing import Iterable

from loguru import logger
from pydantic import BaseModel, Field


class GroupMember(BaseModel):
    uuid: str | None = None
    number: str | None = None
    name: str | None = None
    username: str | None = None


class ContactRecipient(BaseModel):
    uuid: str | None = None
    number: str | None = None
    name: str | None = None
    username: str | None = None
    given_name: str | None = Field(default=None, alias="givenName")
    family_name: str | None = Field(default=None, alias="familyName")
    nick_name: str | None = Field(default=None, alias="nickName")
    nick_given_name: str | None = Field(default=None, alias="nickGivenName")
    nick_family_name: str | None = Field(default=None, alias="nickFamilyName")
    profile: "ContactProfile | None" = None


class ContactProfile(BaseModel):
    given_name: str | None = Field(default=None, alias="givenName")
    family_name: str | None = Field(default=None, alias="familyName")


class SignalGroup(BaseModel):
    group_id: str | None = Field(default=None, alias="groupId")
    group_id_v2: str | None = Field(default=None, alias="groupIdV2")
    id: str | None = None
    name: str | None = None
    members: list[GroupMember] = Field(default_factory=list)

    @property
    def resolved_id(self) -> str | None:
        return self.group_id_v2 or self.group_id or self.id

    def get_member_ids(self) -> set[str]:
        return normalize_member_set(self.members)


class GroupList(BaseModel):
    groups: list[SignalGroup] = Field(default_factory=list)


class GroupInfo(BaseModel):
    group_id: str | None = Field(default=None, alias="groupId")
    group_id_v2: str | None = Field(default=None, alias="groupIdV2")
    type: str | None = None


class GroupV2(BaseModel):
    group_id: str | None = Field(default=None, alias="groupId")
    group_id_v2: str | None = Field(default=None, alias="groupIdV2")


class DataMessage(BaseModel):
    group_info: GroupInfo | None = Field(default=None, alias="groupInfo")
    group_v2: GroupV2 | None = Field(default=None, alias="groupV2")
    group_change: dict | None = Field(default=None, alias="groupChange")
    group_id: str | None = Field(default=None, alias="groupId")


class SyncSentMessage(BaseModel):
    group_info: GroupInfo | None = Field(default=None, alias="groupInfo")
    group_v2: GroupV2 | None = Field(default=None, alias="groupV2")
    group_change: dict | None = Field(default=None, alias="groupChange")
    group_id: str | None = Field(default=None, alias="groupId")


class SyncMessage(BaseModel):
    sent_message: SyncSentMessage | None = Field(default=None, alias="sentMessage")


class Envelope(BaseModel):
    data_message: DataMessage | None = Field(default=None, alias="dataMessage")
    sync_message: SyncMessage | None = Field(default=None, alias="syncMessage")


class SignalPayload(BaseModel):
    envelope: Envelope | None = None

    def extract_group_id(self) -> str | None:
        message = self._group_message()
        if message is None:
            return None
        group_info = message.group_info
        if group_info and (group_info.group_id or group_info.group_id_v2):
            return group_info.group_id or group_info.group_id_v2
        group_v2 = message.group_v2
        if group_v2 and (group_v2.group_id or group_v2.group_id_v2):
            return group_v2.group_id or group_v2.group_id_v2
        return message.group_id

    def is_group_update(self) -> bool:
        message = self._group_message()
        if message is None:
            return False
        group_info = message.group_info
        if group_info and group_info.type == "UPDATE":
            return True
        if message.group_change or message.group_v2:
            return True
        return False

    def _group_message(self) -> DataMessage | SyncSentMessage | None:
        envelope = self.envelope
        if not envelope:
            return None
        if envelope.data_message:
            return envelope.data_message
        if envelope.sync_message and envelope.sync_message.sent_message:
            return envelope.sync_message.sent_message
        return None

    def describe_event(self) -> str:
        envelope = self.envelope
        if not envelope:
            return "no-envelope"
        if envelope.data_message:
            return "data-message"
        if envelope.sync_message and envelope.sync_message.sent_message:
            return "sync-sent-message"
        return "unhandled-envelope"


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class SignalCliError(RuntimeError):
    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


class SignalCliClient:
    def __init__(
        self,
        account: str,
        command_timeout_seconds: float = 30.0,
        receive_timeout_seconds: int = 5,
    ) -> None:
        self.account = account
        self.command_timeout_seconds = command_timeout_seconds
        self.receive_timeout_seconds = receive_timeout_seconds

    async def _run(self, *args: str, check: bool = True) -> CommandResult:
        logger.debug("signal-cli exec: {}", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.command_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            message = (
                f"signal-cli command timed out after "
                f"{self.command_timeout_seconds:.1f}s: {' '.join(args)}"
            )
            raise SignalCliError(
                message,
                CommandResult(stdout="", stderr=message, returncode=-1),
            ) from exc
        result = CommandResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
        )
        if check and result.returncode != 0:
            logger.debug("signal-cli error: {}", result.stderr.strip())
            raise SignalCliError(result.stderr.strip(), result)
        logger.debug("signal-cli ok: {}", result.stdout.strip())
        return result

    async def _run_json(self, *args: str) -> object:
        result = await self._run(*args)
        return json.loads(result.stdout)

    async def list_groups(self, group_id: str | None = None) -> list[SignalGroup]:
        base_command = ["signal-cli", "-u", self.account, "-o", "json", "listGroups"]
        if group_id:
            base_command.extend(["-g", group_id])
        commands = [base_command]
        last_error: Exception | None = None
        for cmd in commands:
            try:
                data = await self._run_json(*cmd)
            except (SignalCliError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            if isinstance(data, list):
                return [
                    SignalGroup.model_validate(item)
                    for item in data
                    if isinstance(item, dict)
                ]
            if isinstance(data, dict):
                parsed = GroupList.model_validate(data)
                return parsed.groups
        if last_error:
            raise last_error
        return []

    async def get_group_by_id(self, group_id: str) -> SignalGroup | None:
        all_groups = await self.list_groups()
        return next((g for g in all_groups if g.resolved_id == group_id), None)

    async def get_group_by_name(self, group_name: str) -> SignalGroup | None:
        all_groups = await self.list_groups()
        return next((g for g in all_groups if g.name == group_name), None)

    async def list_contacts(self) -> list[ContactRecipient]:
        data = await self._run_json(
            "signal-cli",
            "-u",
            self.account,
            "-o",
            "json",
            "listContacts",
            "--all-recipients",
        )
        if not isinstance(data, list):
            return []
        return [
            ContactRecipient.model_validate(item) for item in data if isinstance(item, dict)
        ]

    async def send_group_message(self, group_id: str, message: str) -> None:
        await self._run(
            "signal-cli",
            "-u",
            self.account,
            "send",
            "-m",
            message,
            "-g",
            group_id,
        )

    async def send_sync_request(self) -> None:
        await self._run(
            "signal-cli",
            "-u",
            self.account,
            "sendSyncRequest",
        )

    async def receive_events(self) -> list[SignalPayload]:
        proc = await asyncio.create_subprocess_exec(
            "signal-cli",
            "-u",
            self.account,
            "-o",
            "json",
            "receive",
            "--timeout",
            str(self.receive_timeout_seconds),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        self._log_output("signal-cli: ", stderr.decode("utf-8", errors="replace"))
        if proc.returncode not in (0, None):
            message = stderr.decode("utf-8", errors="replace").strip()
            raise SignalCliError(
                message or "signal-cli receive failed",
                CommandResult(
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                    returncode=proc.returncode,
                ),
            )
        events: list[SignalPayload] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                payload = SignalPayload.model_validate_json(line)
            except ValueError as exc:
                logger.debug("signal-cli payload parse error: {}", exc)
                continue
            logger.debug("signal-cli event type: {}", payload.describe_event())
            events.append(payload)
        return events

    @staticmethod
    def _log_output(prefix: str, text: str) -> None:
        for line in text.splitlines():
            rendered = line.rstrip()
            if rendered:
                logger.debug("{}{}", prefix, rendered)

    async def group_members(self, group_id: str) -> list[GroupMember]:
        groups = await self.list_groups()
        for group in groups:
            if group.resolved_id == group_id:
                return group.members
        return []

    async def group_member_keys(self, group_id: str) -> list[str]:
        members = await self.group_members(group_id)
        keys: list[str] = []
        for member in members:
            if member.uuid:
                keys.append(member.uuid)
            elif member.number:
                keys.append(member.number)
        return keys

def normalize_member_set(members: Iterable[GroupMember]) -> set[str]:
    result = set()
    for member in members:
        member_id = member.uuid or member.number
        if member_id is None:
            continue
        result.add(member_id)
    return result


def extract_group_id(payload: SignalPayload) -> str | None:
    return payload.extract_group_id()


def should_check_group(payload: SignalPayload) -> bool:
    return payload.is_group_update()
