import abc
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
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


class SignalClient(abc.ABC):
    def __init__(
        self,
        account: str,
        command_timeout_seconds: float = 30.0,
        receive_timeout_seconds: int = 5,
    ) -> None:
        self.account = account
        self.command_timeout_seconds = command_timeout_seconds
        self.receive_timeout_seconds = receive_timeout_seconds

    @abc.abstractmethod
    async def list_groups(self, group_id: str | None = None) -> list[SignalGroup]:
        raise NotImplementedError

    async def get_group_by_id(self, group_id: str) -> SignalGroup | None:
        all_groups = await self.list_groups()
        return next((g for g in all_groups if g.resolved_id == group_id), None)

    async def get_group_by_name(self, group_name: str) -> SignalGroup | None:
        all_groups = await self.list_groups()
        return next((g for g in all_groups if g.name == group_name), None)

    @abc.abstractmethod
    async def list_contacts(self) -> list[ContactRecipient]:
        raise NotImplementedError

    @abc.abstractmethod
    async def send_group_message(self, group_id: str, message: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def send_sync_request(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def receive_events(self) -> list[SignalPayload]:
        raise NotImplementedError

    async def close(self) -> None:
        """
        Close any resources held by the Signal client.

        Returns: None
        """
        return None

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


class SignalRpcClient(SignalClient):
    def __init__(
        self,
        account: str,
        socket_path: Path,
        command_timeout_seconds: float = 30.0,
        receive_timeout_seconds: int = 5,
    ) -> None:
        super().__init__(account, command_timeout_seconds, receive_timeout_seconds)
        self.socket_path = socket_path
        self.connect_retry_seconds = 30.0
        self.connect_retry_interval_seconds = 0.5
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._request_id = 0
        self._pending: dict[str, asyncio.Future[object]] = {}
        self._event_queue: asyncio.Queue[SignalPayload] = asyncio.Queue()
        self._read_failure: SignalCliError | None = None

    async def list_groups(self, group_id: str | None = None) -> list[SignalGroup]:
        data = await self._request("listGroups")
        groups = _parse_groups_from_object(data)
        if group_id:
            return [group for group in groups if group.resolved_id == group_id]
        return groups

    async def list_contacts(self) -> list[ContactRecipient]:
        data = await self._request("listContacts")
        if not isinstance(data, list):
            return []
        return [
            ContactRecipient.model_validate(item) for item in data if isinstance(item, dict)
        ]

    async def send_group_message(self, group_id: str, message: str) -> None:
        await self._request(
            "send",
            {
                "message": message,
                "groupId": group_id,
            },
        )

    async def send_sync_request(self) -> None:
        await self._request("sendSyncRequest")

    async def receive_events(self) -> list[SignalPayload]:
        await self._ensure_connected()
        self._raise_if_read_failed()
        events: list[SignalPayload] = []
        try:
            first_event = await asyncio.wait_for(
                self._event_queue.get(),
                timeout=self.receive_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._raise_if_read_failed()
            return []
        events.append(first_event)
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    async def close(self) -> None:
        """
        Close the daemon socket connection and background read task.

        Returns: None
        """
        read_task = self._read_task
        self._read_task = None
        if read_task is not None:
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
            except SignalCliError:
                pass

        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        self._fail_pending("signal-cli client closed")

    async def _ensure_connected(self) -> None:
        self._raise_if_read_failed()
        if self._writer is not None and not self._writer.is_closing():
            return
        deadline = time.monotonic() + self.connect_retry_seconds
        last_error: OSError | None = None
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            except OSError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    break
                logger.info(
                    "Waiting for signal-cli daemon socket {}: {}",
                    self.socket_path,
                    exc,
                )
                await asyncio.sleep(self.connect_retry_interval_seconds)
                continue
            self._reader = reader
            self._writer = writer
            self._read_task = asyncio.create_task(self._read_loop())
            self._read_task.add_done_callback(self._on_read_task_done)
            logger.info("Connected to signal-cli daemon socket {}", self.socket_path)
            return
        message = (
            f"Could not connect to signal-cli daemon socket {self.socket_path} "
            f"within {self.connect_retry_seconds:.1f}s"
        )
        if last_error is not None:
            message = f"{message}: {last_error}"
        raise SignalCliError(
            message,
            CommandResult(stdout="", stderr=message, returncode=-1),
        )

    async def _request(self, method: str, params: dict[str, object] | None = None) -> object:
        await self._ensure_connected()
        self._raise_if_read_failed()
        assert self._writer is not None
        self._request_id += 1
        request_id = str(self._request_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        rpc_message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params:
            rpc_message["params"] = params
        encoded = json.dumps(rpc_message) + "\n"
        logger.debug("signal-cli rpc -> {} {}", method, params or {})
        started_at = time.monotonic()
        async with self._write_lock:
            self._writer.write(encoded.encode("utf-8"))
            await self._writer.drain()
        try:
            result = await asyncio.wait_for(
                future,
                timeout=self.command_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            timeout_message = (
                f"signal-cli rpc timed out after {self.command_timeout_seconds:.1f}s: "
                f"{method}"
            )
            raise SignalCliError(
                timeout_message,
                CommandResult(stdout="", stderr=timeout_message, returncode=-1),
            ) from exc
        elapsed = time.monotonic() - started_at
        logger.debug("signal-cli rpc <- {} ({}s)", method, f"{elapsed:.2f}")
        return result

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    raise SignalCliError(
                        "signal-cli rpc socket closed",
                        CommandResult(stdout="", stderr="socket closed", returncode=-1),
                    )
                self._handle_message(line.decode("utf-8", errors="replace").strip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _coerce_signal_error("signal-cli rpc read loop failed", exc)
            self._read_failure = error
            self._reader = None
            writer = self._writer
            self._writer = None
            if writer is not None and not writer.is_closing():
                writer.close()
            self._fail_pending(str(error))
            logger.exception("signal-cli rpc read loop failed")
            raise

    def _handle_message(self, line: str) -> None:
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug("signal-cli rpc parse error: {}", exc)
            return
        if not isinstance(message, dict):
            return
        if "id" in message:
            request_id = str(message["id"])
            future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                error_message = str(error.get("message") or "signal-cli rpc error")
                future.set_exception(
                    SignalCliError(
                        error_message,
                        CommandResult(stdout="", stderr=error_message, returncode=-1),
                    )
                )
                return
            future.set_result(message.get("result"))
            return
        method = message.get("method")
        params = message.get("params")
        if method != "receive" or not isinstance(params, dict):
            logger.debug("signal-cli rpc notification: {}", message)
            return
        try:
            payload = SignalPayload.model_validate(params)
        except ValueError as exc:
            logger.debug("signal-cli rpc payload parse error: {}", exc)
            return
        logger.debug("signal-cli event type: {}", payload.describe_event())
        self._event_queue.put_nowait(payload)

    def _fail_pending(self, message: str) -> None:
        error = SignalCliError(
            message,
            CommandResult(stdout="", stderr=message, returncode=-1),
        )
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _raise_if_read_failed(self) -> None:
        if self._read_failure is not None:
            raise self._read_failure

    def _on_read_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except SignalCliError as exc:
            if self._read_failure is None:
                self._read_failure = exc
            logger.error("signal-cli rpc read task stopped: {}", exc)
        except Exception as exc:
            error = _coerce_signal_error("signal-cli rpc read task crashed", exc)
            self._read_failure = error
            logger.exception("signal-cli rpc read task crashed")


def create_signal_client(
    account: str,
    command_timeout_seconds: float,
    receive_timeout_seconds: int,
    daemon_socket_path: Path,
) -> SignalClient:
    """
    Create the Signal RPC client.

    Args:
    - account - Signal account number
    - command_timeout_seconds - timeout for request/command round-trips
    - receive_timeout_seconds - timeout when waiting for new events
    - daemon_socket_path - Unix socket path for the Signal RPC daemon

    Returns: configured Signal client
    """
    return SignalRpcClient(
        account,
        socket_path=daemon_socket_path,
        command_timeout_seconds=command_timeout_seconds,
        receive_timeout_seconds=receive_timeout_seconds,
    )


def _parse_groups_from_object(data: object) -> list[SignalGroup]:
    if isinstance(data, list):
        return [SignalGroup.model_validate(item) for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return GroupList.model_validate(data).groups
    return []


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


def _coerce_signal_error(message: str, exc: Exception) -> SignalCliError:
    if isinstance(exc, SignalCliError):
        return exc
    rendered_message = f"{message}: {exc}"
    return SignalCliError(
        rendered_message,
        CommandResult(stdout="", stderr=rendered_message, returncode=-1),
    )
