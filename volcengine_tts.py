"""Volcengine Seed TTS 2.0 bidirectional WebSocket client."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from volcengine_protocol import EventType, MsgType, receive_message, send_event, wait_for_event


URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def text_chunks(text: str, limit: int = 120) -> list[str]:
    pieces = re.split(r"(?<=[。！？!?；;\.])\s*", text.strip())
    chunks: list[str] = []
    for piece in pieces:
        while len(piece) > limit:
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if piece:
            chunks.append(piece)
    return chunks


async def synthesize(
    text: str,
    output: Path,
    api_key: str,
    speaker: str,
    resource_id: str = "seed-tts-2.0",
    audio_format: str = "mp3",
    sample_rate: int = 24000,
) -> dict[str, Any]:
    import websockets

    connection_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": connection_id,
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    websocket = await websockets.connect(
        URL,
        additional_headers=headers,
        max_size=10 * 1024 * 1024,
        proxy=None,
    )
    log_id = websocket.response.headers.get("x-tt-logid", "")
    session_id = str(uuid.uuid4())
    audio = bytearray()
    usage: dict[str, Any] = {}
    base_request = {
        "req_params": {
            "speaker": speaker,
            "audio_params": {"format": audio_format, "sample_rate": sample_rate},
        }
    }
    try:
        await send_event(websocket, EventType.StartConnection)
        await wait_for_event(websocket, EventType.ConnectionStarted)
        start = dict(base_request)
        start["event"] = EventType.StartSession
        await send_event(
            websocket,
            EventType.StartSession,
            json.dumps(start, ensure_ascii=False).encode(),
            session_id,
        )
        await wait_for_event(websocket, EventType.SessionStarted)

        async def send_text() -> None:
            for chunk in text_chunks(text):
                request = {
                    "event": EventType.TaskRequest,
                    "req_params": {**base_request["req_params"], "text": chunk},
                }
                await send_event(
                    websocket,
                    EventType.TaskRequest,
                    json.dumps(request, ensure_ascii=False).encode(),
                    session_id,
                )
            await send_event(websocket, EventType.FinishSession, session_id=session_id)

        sender = asyncio.create_task(send_text())
        while True:
            message = await receive_message(websocket)
            if message.type == MsgType.Error:
                raise RuntimeError(
                    f"Volcengine error {message.error_code}: {message.payload.decode(errors='replace')}"
                )
            if message.type == MsgType.AudioOnlyServer:
                audio.extend(message.payload)
            elif message.type == MsgType.FullServerResponse:
                if message.event == EventType.UsageResponse and message.payload:
                    usage = json.loads(message.payload)
                elif message.event == EventType.SessionFailed:
                    raise RuntimeError(message.payload.decode(errors="replace"))
                elif message.event == EventType.SessionFinished:
                    break
        await sender
        if not audio:
            raise RuntimeError("Volcengine returned no audio")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio)
        return {"bytes": len(audio), "log_id": log_id, "usage": usage}
    finally:
        try:
            await send_event(websocket, EventType.FinishConnection)
            await wait_for_event(websocket, EventType.ConnectionFinished)
        except Exception:
            pass
        finally:
            await websocket.close()


def synthesize_sync(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(synthesize(**kwargs))
