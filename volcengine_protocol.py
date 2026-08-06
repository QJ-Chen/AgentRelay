"""Volcengine bidirectional WebSocket framing.

Adapted from the official ``TTS Websocket Bidirection protocols`` sample
provided with the Volcengine Seed TTS 2.0 documentation.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class MsgType(IntEnum):
    FullClientRequest = 0b0001
    AudioOnlyClient = 0b0010
    FullServerResponse = 0b1001
    AudioOnlyServer = 0b1011
    Error = 0b1111


class Flag(IntEnum):
    NoSeq = 0
    PositiveSeq = 1
    NegativeSeq = 3
    WithEvent = 4


class EventType(IntEnum):
    None_ = 0
    StartConnection = 1
    FinishConnection = 2
    ConnectionStarted = 50
    ConnectionFailed = 51
    ConnectionFinished = 52
    StartSession = 100
    FinishSession = 102
    SessionStarted = 150
    SessionFinished = 152
    SessionFailed = 153
    UsageResponse = 154
    TaskRequest = 200


@dataclass
class Message:
    type: MsgType
    flag: Flag = Flag.NoSeq
    event: EventType | int = EventType.None_
    session_id: str = ""
    connect_id: str = ""
    sequence: int = 0
    error_code: int = 0
    payload: bytes = b""

    def marshal(self) -> bytes:
        buffer = io.BytesIO()
        # Version 1, four-byte header, JSON serialization, no compression.
        buffer.write(bytes((0x11, (self.type << 4) | self.flag, 0x10, 0x00)))
        if self.flag == Flag.WithEvent:
            buffer.write(struct.pack(">i", int(self.event)))
            if self.event not in (
                EventType.StartConnection,
                EventType.FinishConnection,
                EventType.ConnectionStarted,
                EventType.ConnectionFailed,
            ):
                session = self.session_id.encode()
                buffer.write(struct.pack(">I", len(session)))
                buffer.write(session)
        if self.type in (MsgType.FullClientRequest, MsgType.AudioOnlyClient):
            if self.flag in (Flag.PositiveSeq, Flag.NegativeSeq):
                buffer.write(struct.pack(">i", self.sequence))
        elif self.type == MsgType.Error:
            buffer.write(struct.pack(">I", self.error_code))
        buffer.write(struct.pack(">I", len(self.payload)))
        buffer.write(self.payload)
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        if len(data) < 4:
            raise ValueError("Volcengine frame is shorter than its header")
        buffer = io.BytesIO(data)
        version_size, type_flag, serialization, _ = buffer.read(4)
        if version_size >> 4 != 1 or version_size & 0x0F != 1:
            raise ValueError("Unsupported Volcengine frame version or header size")
        if serialization >> 4 not in (0, 1) or serialization & 0x0F != 0:
            raise ValueError("Unsupported Volcengine serialization or compression")
        msg = cls(type=MsgType(type_flag >> 4), flag=Flag(type_flag & 0x0F))
        if msg.type in (
            MsgType.FullServerResponse,
            MsgType.AudioOnlyServer,
            MsgType.FullClientRequest,
            MsgType.AudioOnlyClient,
        ) and msg.flag in (Flag.PositiveSeq, Flag.NegativeSeq):
            msg.sequence = _read_i32(buffer)
        elif msg.type == MsgType.Error:
            msg.error_code = _read_u32(buffer)
        if msg.flag == Flag.WithEvent:
            raw_event = _read_i32(buffer)
            try:
                msg.event = EventType(raw_event)
            except ValueError:
                msg.event = raw_event
            if msg.event not in (
                EventType.StartConnection,
                EventType.FinishConnection,
                EventType.ConnectionStarted,
                EventType.ConnectionFailed,
                EventType.ConnectionFinished,
            ):
                msg.session_id = _read_sized(buffer).decode()
            if msg.event in (
                EventType.ConnectionStarted,
                EventType.ConnectionFailed,
                EventType.ConnectionFinished,
            ):
                msg.connect_id = _read_sized(buffer).decode()
        msg.payload = _read_sized(buffer)
        if buffer.read():
            raise ValueError("Unexpected trailing data in Volcengine frame")
        return msg


def _read_exact(buffer: io.BytesIO, size: int) -> bytes:
    data = buffer.read(size)
    if len(data) != size:
        raise ValueError("Truncated Volcengine frame")
    return data


def _read_i32(buffer: io.BytesIO) -> int:
    return struct.unpack(">i", _read_exact(buffer, 4))[0]


def _read_u32(buffer: io.BytesIO) -> int:
    return struct.unpack(">I", _read_exact(buffer, 4))[0]


def _read_sized(buffer: io.BytesIO) -> bytes:
    return _read_exact(buffer, _read_u32(buffer))


async def send_event(websocket: Any, event: EventType, payload: bytes = b"{}", session_id: str = "") -> None:
    message = Message(
        type=MsgType.FullClientRequest,
        flag=Flag.WithEvent,
        event=event,
        session_id=session_id,
        payload=payload,
    )
    await websocket.send(message.marshal())


async def receive_message(websocket: Any) -> Message:
    data = await websocket.recv()
    if not isinstance(data, bytes):
        raise ValueError("Volcengine returned a text WebSocket message")
    return Message.from_bytes(data)


async def wait_for_event(websocket: Any, event: EventType) -> Message:
    message = await receive_message(websocket)
    if message.type == MsgType.Error:
        raise RuntimeError(f"Volcengine error {message.error_code}: {message.payload.decode(errors='replace')}")
    if message.type != MsgType.FullServerResponse or message.event != event:
        raise RuntimeError(f"Expected event {event.name}, received type={message.type.name}, event={message.event}")
    return message
