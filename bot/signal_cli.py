import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Iterable, AsyncIterator

from loguru import logger
from pydantic import BaseModel, Field


class GroupMember(BaseModel):
    uuid: str | None = None
    number: str | None = None
    name: str | None = None


class SignalGroup(BaseModel):
    group_id: str | None = Field(default=None, alias="groupId")
    group_id_v2: str | None = Field(default=None, alias="groupIdV2")
    id: str | None = None
    name: str | None = None
    members: list[GroupMember] = Field(default_factory=list)

    @property
    def resolved_id(self) -> str | None:
        return self.group_id_v2 or self.group_id or self.id


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


class Envelope(BaseModel):
    data_message: DataMessage | None = Field(default=None, alias="dataMessage")


class SignalPayload(BaseModel):
    envelope: Envelope | None = None


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
    def __init__(self, account: str) -> None:
        self.account = account

    async def _run(self, *args: str, check: bool = True) -> CommandResult:
        logger.debug("signal-cli exec: {}", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
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

    async def list_groups(self) -> list[SignalGroup]:
        commands = [
            ["signal-cli", "-u", self.account, "-o", "json", "listGroupsV2"],
            ["signal-cli", "-u", self.account, "-o", "json", "listGroups"],
        ]
        last_error: Exception | None = None
        for cmd in commands:
            try:
                data = await self._run_json(*cmd)
            except (SignalCliError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            if isinstance(data, list):
                return [SignalGroup.model_validate(item) for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                parsed = GroupList.model_validate(data)
                return parsed.groups
        if last_error:
            raise last_error
        return []

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

    async def receive_events(self) -> AsyncIterator[SignalPayload]:
        proc = await asyncio.create_subprocess_exec(
            "signal-cli",
            "-u",
            self.account,
            "-o",
            "json",
            "receive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(self._log_stream("signal-cli: ", proc.stderr))
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    payload = SignalPayload.model_validate_json(
                        line.decode("utf-8", errors="replace")
                    )
                except ValueError as exc:
                    logger.debug("signal-cli payload parse error: {}", exc)
                    continue
                yield payload
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()

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

    @staticmethod
    async def _log_stream(prefix: str, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("{}{}", prefix, text)


def normalize_member_set(members: Iterable[GroupMember]) -> set[str]:
    return {
        member.uuid or member.number
        for member in members
        if member.uuid or member.number
    }


def extract_group_id(payload: SignalPayload) -> str | None:
    envelope = payload.envelope
    if not envelope or not envelope.data_message:
        return None
    data_message = envelope.data_message
    group_info = data_message.group_info
    if group_info and (group_info.group_id or group_info.group_id_v2):
        return group_info.group_id or group_info.group_id_v2
    group_v2 = data_message.group_v2
    if group_v2 and (group_v2.group_id or group_v2.group_id_v2):
        return group_v2.group_id or group_v2.group_id_v2
    return data_message.group_id


def should_check_group(payload: SignalPayload) -> bool:
    envelope = payload.envelope
    if not envelope or not envelope.data_message:
        return False
    data_message = envelope.data_message
    group_info = data_message.group_info
    if group_info and group_info.type == "UPDATE":
        return True
    if data_message.group_change or data_message.group_v2:
        return True
    return False
