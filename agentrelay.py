#!/usr/bin/env python3
"""Small dependency-free Codex notify -> macOS speech adapter."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11+ is expected on current macOS tooling
    tomllib = None


APP_DIR = Path(os.environ.get("AGENTRELAY_HOME", Path.home() / ".config" / "agentrelay"))
SCHEMA_VERSION = 1
CONFIG_PATH = APP_DIR / "config.json"
QUEUE_PATH = APP_DIR / "queue.jsonl"
SEEN_PATH = APP_DIR / "seen.json"
SPEECH_STATE_PATH = APP_DIR / "speech-state.json"
PLAYBACK_STATE_PATH = APP_DIR / "playback-state.json"
LAST_RESULT_PATH = APP_DIR / "last-result.json"
METRICS_PATH = APP_DIR / "metrics.json"
SOCKET_PATH = APP_DIR / "agentrelay.sock"
STOP_STATE_PATH = APP_DIR / "stop-state.json"
DAEMON_LOCK_PATH = APP_DIR / "daemon.lock"
METRICS_LOCK = APP_DIR / "metrics.lock"
ENQUEUE_LOCK = APP_DIR / "enqueue.lock"
WORKER_LOCK = APP_DIR / "worker.lock"
PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
EVENT_LOG = APP_DIR / "events.jsonl"
PROVIDER_LOG = EVENT_LOG  # Backward-compatible public name.
# The existing notify command is captured during `install`; never bake a
# machine-specific integration path into the repository defaults.
DEFAULT_NOTIFY: list[str] = []
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_CONFIG_BACKUP = Path.home() / ".codex" / "config.toml.agentrelay-backup"
SUPPORTED_LANGUAGES = {
    "zh-CN": {"system_voice": "Tingting"},
    "en-US": {"system_voice": "Samantha"},
}


def defaults() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "provider": "system_say",
        "fallback_provider": "system_say",
        "language": "zh-CN",
        "voice": "Tingting",
        "rate": 190,
        "daemon_enabled": True,
        "daemon_idle_seconds": 60,
        "volcengine_speaker": "zh_female_gaolengyujie_uranus_bigtts",
        "volcengine_resource_id": "seed-tts-2.0",
        "volcengine_format": "mp3",
        "volcengine_sample_rate": 24000,
        "max_chars": 1800,
        "spoken_max_chars": 200,
        "normal_cooldown_seconds": 12,
        "max_queue_age_seconds": 30,
        "final_notify_mode": "if_not_spoken",
        "final_notify_suppress_seconds": 120,
        "final_spoken_max_chars": 200,
        "dedupe_seconds": 30,
        "queue_mode": "replace",  # replace avoids speaking stale turns
        "forward_notify": DEFAULT_NOTIFY,
    }


def load_config() -> dict[str, Any]:
    result = defaults()
    try:
        result.update(json.loads(CONFIG_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return result


@dataclass
class SpeakRequest:
    id: str
    text: str
    language: str = "zh-CN"
    voice: str = "Tingting"
    speed: float = 1.0
    interrupt: bool = True
    source: str = "manual"
    turn_id: str = ""
    content_hash: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_item(cls, item: dict[str, Any], fallback_config: dict[str, Any]) -> "SpeakRequest":
        config = item.get("config") if isinstance(item.get("config"), dict) else fallback_config
        text = str(item["text"])
        return cls(
            id=str(item.get("id", request_id(str(item.get("source", "request")), text))),
            text=text,
            language=str(config.get("language", "zh-CN")),
            voice=str(config.get("voice", "Tingting")),
            speed=float(config.get("speed", 1.0)),
            interrupt=bool(item.get("replace", True)),
            source=str(item.get("source", "request")),
            turn_id=str(item.get("turn_id", "")),
            content_hash=str(item.get("content_hash", content_id(text))),
            config=config,
        )


class AudioPlayer(Protocol):
    def play(self, command: list[str], provider: str, item_id: str = "") -> bool: ...

    def stop(self, reason: str = "requested") -> bool: ...


class TTSProvider(Protocol):
    name: str

    def speak(self, request: SpeakRequest, player: AudioPlayer) -> bool: ...


class ProcessAudioPlayer:
    def play(self, command: list[str], provider: str, item_id: str = "") -> bool:
        return _run_playback(command, provider, item_id)

    def stop(self, reason: str = "requested") -> bool:
        return _cancel_playback(reason)


class SystemSayProvider:
    name = "system_say"

    def speak(self, request: SpeakRequest, player: AudioPlayer) -> bool:
        default_voice = SUPPORTED_LANGUAGES.get(
            request.language, SUPPORTED_LANGUAGES["zh-CN"]
        )["system_voice"]
        rate = int(request.config.get("rate", round(190 * request.speed)))
        command = ["/usr/bin/say", "-v", request.voice or default_voice, "-r", str(rate), request.text]
        return player.play(command, self.name, request.id)


class VolcengineProvider:
    name = "volcengine"

    def speak(self, request: SpeakRequest, player: AudioPlayer) -> bool:
        return _volcengine_speak_request(request, player)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _log_event(component: str, event: str, status: str, **fields: Any) -> None:
    """Write metadata-only diagnostics; callers must not pass speech text or secrets."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "component": component,
            "event": event,
            "status": status,
            **fields,
        }
        with EVENT_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _record_result(item_id: str, provider: str, status: str, **fields: Any) -> None:
    try:
        _write_json(
            LAST_RESULT_PATH,
            {
                "schema_version": SCHEMA_VERSION,
                "timestamp": time.time(),
                "item_id": item_id,
                "provider": provider,
                "status": status,
                **fields,
            },
        )
    except OSError:
        pass


def clean_text(value: str, max_chars: int = 1800) -> str:
    """Turn a final Markdown response into compact speech-friendly text."""
    text = value.replace("\r\n", "\n")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Do not accidentally read common credential-shaped values aloud.
    text = re.sub(r"(?i)(api[_ -]?key|token|secret|password)\s*[:=]\s*\S+", r"\1 [redacted]", text)
    return text[:max_chars].rstrip() if max_chars > 0 else text


def clean_update_text(value: str) -> str:
    """Apply stricter cleanup to model-authored progress speech."""
    text = re.sub(r"```[\s\S]*?```", " ", value)
    text = re.sub(r"`[^`]+`", " ", text)
    text = clean_text(text, 0)
    text = re.sub(r"(?<!\w)(?:~?/|\.?\.?/)[^\s，。！？；、]+[，；、]?", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in (
            "last-assistant-message",
            "final_answer",
            "last_message",
            "assistant_text",
            "text",
            "message",
            "content",
        ):
            candidate = obj.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for value in obj.values():
            found = _find_text(value)
            if found:
                return found
    if isinstance(obj, list):
        for value in reversed(obj):
            found = _find_text(value)
            if found:
                return found
    return ""


def parse_notify(argv: list[str]) -> tuple[str, str]:
    """Accept Codex's single JSON argument or an explicit event and payload."""
    if not argv:
        return "", ""
    joined = " ".join(argv)
    try:
        data = json.loads(joined)
        if isinstance(data, dict):
            return str(data.get("type", "agent-turn-complete")), _find_text(data)
        return "agent-turn-complete", _find_text(data)
    except json.JSONDecodeError:
        pass
    event = argv[0]
    if len(argv) == 1:
        return event, ""
    joined = " ".join(argv[1:])
    try:
        data = json.loads(joined)
        return event, _find_text(data)
    except json.JSONDecodeError:
        return event, joined


def _find_turn_id(obj: Any) -> str:
    if isinstance(obj, dict):
        for key in ("turn_id", "turn-id", "turnId", "thread_id", "thread-id", "threadId"):
            value = obj.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        for value in obj.values():
            found = _find_turn_id(value)
            if found:
                return found
    if isinstance(obj, list):
        for value in reversed(obj):
            found = _find_turn_id(value)
            if found:
                return found
    return ""


def notify_turn_id(argv: list[str]) -> str:
    for candidate in (" ".join(argv), " ".join(argv[1:])):
        try:
            return _find_turn_id(json.loads(candidate))
        except (json.JSONDecodeError, TypeError):
            continue
    return ""


def _acquire_lock(path: Path, attempts: int = 50) -> bool:
    for _ in range(attempts):
        try:
            path.mkdir()
            return True
        except FileExistsError:
            time.sleep(0.01)
    return False


def _release_lock(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _load_seen() -> dict[str, float]:
    try:
        value = json.loads(SEEN_PATH.read_text())
        if isinstance(value, dict) and "items" in value:
            value = value["items"]
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_speech_state() -> dict[str, Any]:
    try:
        value = json.loads(SPEECH_STATE_PATH.read_text())
        if isinstance(value, dict) and "state" in value:
            value = value["state"]
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def enqueue(
    text: str,
    config: dict[str, Any],
    event: str = "agent-turn-complete",
    *,
    source: str = "notify",
    priority: str = "normal",
    replace: bool = True,
    max_chars: int | None = None,
    turn_id: str = "",
    item_id: str = "",
) -> bool:
    if not config.get("enabled", True):
        _log_event("enqueue", "request", "skipped", reason="disabled")
        return False
    text = clean_text(text, max_chars if max_chars is not None else int(config.get("max_chars", 1800)))
    if not text:
        _log_event("enqueue", "request", "skipped", reason="empty")
        return False
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(ENQUEUE_LOCK):
        _log_event("enqueue", "request", "skipped", reason="busy")
        return False
    try:
        item_id = item_id or request_id(event, text)
        now = time.time()
        window = max(0, int(config.get("dedupe_seconds", 30)))
        seen = {key: stamp for key, stamp in _load_seen().items() if now - float(stamp) <= window}
        if item_id in seen:
            _log_event("enqueue", "request", "skipped", item_id=item_id, reason="duplicate")
            return False
        item = {
            "schema_version": SCHEMA_VERSION,
            "id": item_id,
            "text": text,
            "created": now,
            "source": source,
            "turn_id": turn_id,
            "content_hash": content_id(text),
            "priority": priority,
            "replace": replace,
            "config": config,
        }
        with QUEUE_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        seen[item_id] = now
        _write_json(SEEN_PATH, {"schema_version": SCHEMA_VERSION, "items": seen})
        if replace:
            _cancel_playback("replacement_enqueued")
        _log_event("enqueue", "request", "accepted", item_id=item_id, source=source, priority=priority)
        return True
    finally:
        _release_lock(ENQUEUE_LOCK)


def enqueue_update(
    text: str,
    config: dict[str, Any],
    priority: str = "normal",
    replace: bool = True,
    turn_id: str = "",
) -> dict[str, Any]:
    if priority not in ("normal", "important"):
        _log_event("enqueue", "update", "skipped", reason="invalid_priority")
        return {"status": "skipped", "reason": "invalid_priority"}
    cleaned = clean_update_text(text)
    if not cleaned:
        _log_event("enqueue", "update", "skipped", reason="empty")
        return {"status": "skipped", "reason": "empty"}
    maximum = int(config.get("spoken_max_chars", 200))
    if len(cleaned) > maximum:
        _log_event("enqueue", "update", "skipped", reason="too_long", length=len(cleaned))
        return {"status": "skipped", "reason": "too_long", "length": len(cleaned), "max_chars": maximum}
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(ENQUEUE_LOCK):
        _log_event("enqueue", "update", "skipped", reason="busy")
        return {"status": "skipped", "reason": "busy"}
    try:
        now = time.time()
        state = _load_speech_state()
        if priority == "normal":
            cooldown = max(0, int(config.get("normal_cooldown_seconds", 12)))
            elapsed = now - float(state.get("last_update_at", 0))
            if elapsed < cooldown:
                _log_event("enqueue", "update", "skipped", reason="cooldown")
                return {"status": "skipped", "reason": "cooldown", "retry_after": round(cooldown - elapsed, 1)}
        item_id = request_id("model-update", cleaned)
        seen = _load_seen()
        window = max(0, int(config.get("dedupe_seconds", 30)))
        seen = {key: stamp for key, stamp in seen.items() if now - float(stamp) <= window}
        if item_id in seen:
            _log_event("enqueue", "update", "skipped", item_id=item_id, reason="duplicate")
            return {"status": "skipped", "reason": "duplicate"}
        item = {
            "schema_version": SCHEMA_VERSION,
            "id": item_id,
            "text": cleaned,
            "created": now,
            "source": "model-command",
            "turn_id": turn_id,
            "content_hash": content_id(cleaned),
            "priority": priority,
            "replace": replace,
            "config": config,
        }
        with QUEUE_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        seen[item_id] = now
        _write_json(SEEN_PATH, {"schema_version": SCHEMA_VERSION, "items": seen})
        recent = state.get("recent", []) if isinstance(state.get("recent"), list) else []
        recent = [entry for entry in recent if now - float(entry.get("created", 0)) <= 3600]
        recent.append(
            {
                "created": now,
                "turn_id": turn_id,
                "content_hash": item["content_hash"],
                "source": item["source"],
                "priority": priority,
            }
        )
        _write_json(
            SPEECH_STATE_PATH,
            {
                "schema_version": SCHEMA_VERSION,
                "state": {
                    "last_update_at": now,
                    "last_text_hash": item_id,
                    "priority": priority,
                    "recent": recent[-100:],
                },
            },
        )
        if replace:
            _cancel_playback("replacement_update_enqueued")
        _log_event("enqueue", "update", "accepted", item_id=item_id, priority=priority)
        return {"status": "accepted", "length": len(cleaned), "priority": priority}
    finally:
        _release_lock(ENQUEUE_LOCK)


def should_speak_final(config: dict[str, Any], turn_id: str = "", content_hash: str = "") -> bool:
    mode = config.get("final_notify_mode", "if_not_spoken")
    if mode == "off":
        return False
    if mode == "always":
        return True
    state = _load_speech_state()
    recent = state.get("recent", []) if isinstance(state.get("recent"), list) else []
    if turn_id and any(entry.get("turn_id") == turn_id for entry in recent):
        return False
    if content_hash and any(entry.get("content_hash") == content_hash for entry in recent):
        return False
    if turn_id or content_hash:
        return True
    suppress = max(0, int(config.get("final_notify_suppress_seconds", 120)))
    return time.time() - float(state.get("last_update_at", 0)) > suppress


def request_id(event: str, payload: str) -> str:
    return hashlib.sha256((event + "\0" + payload).encode()).hexdigest()


def content_id(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _cancel_playback(reason: str = "replaced") -> bool:
    try:
        state = json.loads(PLAYBACK_STATE_PATH.read_text(encoding="utf-8"))
        pid = int(state["pid"])
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        PLAYBACK_STATE_PATH.unlink(missing_ok=True)
        return False
    except OSError as exc:
        _log_event("player", "cancel", "failed", reason=reason, error=type(exc).__name__)
        return False
    _log_event("player", "cancel", "requested", reason=reason, pid=pid)
    return True


def _run_playback(command: list[str], provider: str, item_id: str = "") -> bool:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _write_json(
            PLAYBACK_STATE_PATH,
            {
                "schema_version": SCHEMA_VERSION,
                "pid": process.pid,
                "worker_pid": os.getpid(),
                "provider": provider,
                "item_id": item_id,
                "started_at": time.time(),
            },
        )
        return process.wait() == 0
    except OSError as exc:
        _log_event("player", "start", "failed", provider=provider, error=type(exc).__name__)
        return False
    finally:
        try:
            state = json.loads(PLAYBACK_STATE_PATH.read_text(encoding="utf-8"))
            if int(state.get("pid", -1)) == getattr(locals().get("process"), "pid", -2):
                PLAYBACK_STATE_PATH.unlink(missing_ok=True)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            pass


def worker(force_system_say: bool = False) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(WORKER_LOCK, attempts=100):
        _log_event("worker", "start", "skipped", reason="already_running")
        return
    _log_event("worker", "start", "started", pid=os.getpid())
    try:
        while QUEUE_PATH.exists() and QUEUE_PATH.stat().st_size:
            batch_path = APP_DIR / f"queue-{uuid.uuid4().hex}.jsonl"
            if not _acquire_lock(ENQUEUE_LOCK):
                break
            try:
                QUEUE_PATH.replace(batch_path)
            except FileNotFoundError:
                continue
            finally:
                _release_lock(ENQUEUE_LOCK)
            lines = batch_path.read_text(encoding="utf-8").splitlines()
            batch_path.unlink(missing_ok=True)
            config = load_config()
            items = []
            for line in lines:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    _log_event("worker", "queue_record", "skipped", reason="invalid_json")
                    continue
            if config.get("queue_mode") == "replace":
                replacement = next(
                    (index for index in range(len(items) - 1, -1, -1) if items[index].get("replace", True)),
                    None,
                )
                if replacement is not None:
                    items = items[replacement:]
            for item in items:
                try:
                    try:
                        stop_state = json.loads(STOP_STATE_PATH.read_text(encoding="utf-8"))
                        stopped_at = float(stop_state.get("stopped_at", 0))
                    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                        stopped_at = 0
                    if float(item.get("created", 0)) <= stopped_at:
                        _log_event("worker", "request", "skipped", item_id=item.get("id", ""), reason="stopped")
                        continue
                    max_age = max(0, int(config.get("max_queue_age_seconds", 30)))
                    if max_age and time.time() - float(item.get("created", 0)) > max_age:
                        _log_event("worker", "request", "skipped", item_id=item.get("id", ""), reason="expired")
                        continue
                    item_config = item.get("config", config)
                    if force_system_say:
                        item_config = {**item_config, "provider": "system_say", "fallback_provider": "system_say"}
                        item = {**item, "config": item_config}
                    provider = str(item_config.get("provider", "system_say"))
                    succeeded = speak_request(SpeakRequest.from_item(item, config))
                    _record_result(item.get("id", ""), provider, "succeeded" if succeeded else "failed")
                    _log_event(
                        "worker",
                        "request",
                        "succeeded" if succeeded else "failed",
                        item_id=item.get("id", ""),
                        provider=provider,
                    )
                except (KeyError, OSError, TypeError, ValueError) as exc:
                    _log_event("worker", "request", "failed", error=type(exc).__name__)
                    continue
    finally:
        _release_lock(WORKER_LOCK)
        _log_event("worker", "stop", "completed", pid=os.getpid())


def spawn_worker(force_system_say: bool = False) -> bool:
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "_worker"]
        if force_system_say:
            command.append("--fallback-system")
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        _log_event("worker", "spawn", "failed", error=type(exc).__name__)
        return False


def _socket_request(payload: dict[str, Any], timeout: float = 0.35) -> dict[str, Any] | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(SOCKET_PATH))
            client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            response = client.makefile("rb").readline(64 * 1024)
        return json.loads(response) if response else None
    except (OSError, json.JSONDecodeError):
        return None


def _daemon_alive() -> bool:
    response = _socket_request({"operation": "health"})
    return bool(response and response.get("status") == "ok")


def spawn_daemon() -> bool:
    if _daemon_alive():
        return True
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _log_event("daemon", "spawn", "failed", error=type(exc).__name__)
        return False
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if _daemon_alive():
            return True
        time.sleep(0.025)
    _log_event("daemon", "spawn", "failed", reason="startup_timeout")
    return False


def start_runtime(config: dict[str, Any]) -> bool:
    if not config.get("daemon_enabled", True):
        return spawn_worker()
    if spawn_daemon():
        response = _socket_request({"operation": "speak"})
        if response and response.get("status") == "accepted":
            return True
    return spawn_worker(force_system_say=True)


def _stop_runtime(clear_queue: bool = True) -> bool:
    stopped = _cancel_playback("stop_requested")
    if clear_queue:
        try:
            _write_json(
                STOP_STATE_PATH,
                {"schema_version": SCHEMA_VERSION, "stopped_at": time.time()},
            )
            QUEUE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def _daemon_process_queue(activity: dict[str, float]) -> None:
    activity["last"] = time.monotonic()
    worker()
    activity["last"] = time.monotonic()


def _handle_daemon_request(payload: dict[str, Any], activity: dict[str, float]) -> dict[str, Any]:
    operation = payload.get("operation")
    activity["last"] = time.monotonic()
    if operation == "health":
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "queue": _queue_length()}
    if operation == "speak":
        if "request" in payload:
            request_data = payload.get("request")
            if not isinstance(request_data, dict) or not isinstance(request_data.get("text"), str):
                return {"status": "error", "reason": "invalid_request"}
            config = load_config()
            for key in ("language", "voice"):
                if isinstance(request_data.get(key), str) and request_data[key]:
                    config[key] = request_data[key]
            if isinstance(request_data.get("speed"), (int, float)) and not isinstance(request_data["speed"], bool):
                config["speed"] = max(0.25, min(4.0, float(request_data["speed"])))
            accepted = enqueue(
                request_data["text"],
                config,
                event=str(request_data.get("id", "daemon")),
                source=str(request_data.get("source", "daemon")),
                replace=bool(request_data.get("interrupt", True)),
                turn_id=str(request_data.get("turn_id", "")),
                item_id=str(request_data.get("id", "")),
            )
            if not accepted:
                return {"status": "skipped", "reason": "not_enqueued"}
        threading.Thread(target=_daemon_process_queue, args=(activity,), daemon=True).start()
        return {"status": "accepted"}
    if operation == "stop":
        stopped = _stop_runtime(bool(payload.get("clear_queue", True)))
        return {"status": "stopped", "playback_was_active": stopped}
    return {"status": "error", "reason": "unsupported_operation"}


def daemon() -> int:
    config = load_config()
    idle_seconds = max(1, int(config.get("daemon_idle_seconds", 60)))
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_stream = DAEMON_LOCK_PATH.open("a")
    try:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_stream.close()
        return 0
    if SOCKET_PATH.exists():
        if _daemon_alive():
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
            lock_stream.close()
            return 0
        SOCKET_PATH.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    activity = {"last": time.monotonic()}
    try:
        try:
            server.bind(str(SOCKET_PATH))
        except OSError as exc:
            _log_event("daemon", "bind", "failed", error=type(exc).__name__)
            return 1
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(8)
        server.settimeout(0.25)
        _log_event("daemon", "start", "started", pid=os.getpid())
        if _queue_length():
            threading.Thread(target=_daemon_process_queue, args=(activity,), daemon=True).start()
        while True:
            idle = time.monotonic() - activity["last"] >= idle_seconds
            if idle and not WORKER_LOCK.exists() and not PLAYBACK_STATE_PATH.exists() and _queue_length() == 0:
                break
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    line = connection.makefile("rb").readline(64 * 1024)
                    payload = json.loads(line) if line else {}
                    response = _handle_daemon_request(payload, activity)
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    response = {"status": "error", "reason": "invalid_json"}
                connection.sendall(json.dumps(response, ensure_ascii=True).encode("utf-8") + b"\n")
        return 0
    finally:
        server.close()
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
        _log_event("daemon", "stop", "idle_exit", pid=os.getpid())


def system_say(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    request = SpeakRequest.from_item(
        {"id": item_id, "text": text, "config": config, "source": "compatibility"}, config
    )
    return SystemSayProvider().speak(request, ProcessAudioPlayer())


def volcengine_api_key() -> str:
    from volcengine_tts import load_env

    return os.environ.get("VOLCENGINE_TTS_API_KEY", "") or load_env(ENV_PATH).get(
        "VOLCENGINE_TTS_API_KEY", ""
    )


def _log_provider_error(message: str) -> None:
    _log_event("provider", "operation", "failed", detail=message)


def _update_metrics(provider: str, status: str, *, characters: int = 0, latency_ms: float = 0) -> None:
    if not _acquire_lock(METRICS_LOCK):
        return
    try:
        try:
            metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            metrics = {"schema_version": SCHEMA_VERSION, "providers": {}}
        providers = metrics.setdefault("providers", {})
        current = providers.setdefault(
            provider,
            {"requests": 0, "successes": 0, "failures": 0, "characters": 0, "latency_ms_total": 0},
        )
        current["requests"] = int(current.get("requests", 0)) + 1
        current["successes" if status == "succeeded" else "failures"] = int(
            current.get("successes" if status == "succeeded" else "failures", 0)
        ) + 1
        current["characters"] = int(current.get("characters", 0)) + max(0, characters)
        current["latency_ms_total"] = round(float(current.get("latency_ms_total", 0)) + latency_ms, 1)
        metrics["updated_at"] = time.time()
        _write_json(METRICS_PATH, metrics)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        _release_lock(METRICS_LOCK)


def _volcengine_speak_request(request: SpeakRequest, player: AudioPlayer) -> bool:
    from volcengine_tts import synthesize_sync

    config = request.config
    api_key = volcengine_api_key()
    if not api_key:
        _update_metrics("volcengine", "failed", characters=len(request.text))
        _log_provider_error("VOLCENGINE_TTS_API_KEY is missing")
        return False
    audio_dir = APP_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_format = str(config.get("volcengine_format", "mp3"))
    output = audio_dir / f"speech-{uuid.uuid4().hex}.{audio_format}"
    started = time.monotonic()
    try:
        result = synthesize_sync(
            text=request.text,
            output=output,
            api_key=api_key,
            speaker=str(config.get("volcengine_speaker", "zh_female_gaolengyujie_uranus_bigtts")),
            resource_id=str(config.get("volcengine_resource_id", "seed-tts-2.0")),
            audio_format=audio_format,
            sample_rate=int(config.get("volcengine_sample_rate", 24000)),
        )
    except Exception as exc:
        _update_metrics(
            "volcengine", "failed", characters=len(request.text), latency_ms=(time.monotonic() - started) * 1000
        )
        _log_event(
            "provider", "synthesis", "failed", provider="volcengine", error=type(exc).__name__
        )
        return False
    try:
        _log_event(
            "provider",
            "synthesis",
            "succeeded",
            provider="volcengine",
            bytes=result["bytes"],
            log_id=result.get("log_id", ""),
        )
        succeeded = player.play(["/usr/bin/afplay", str(output)], "volcengine", request.id)
        _update_metrics(
            "volcengine",
            "succeeded" if succeeded else "failed",
            characters=len(request.text),
            latency_ms=(time.monotonic() - started) * 1000,
        )
        return succeeded
    finally:
        output.unlink(missing_ok=True)


def volcengine_speak(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    request = SpeakRequest.from_item(
        {"id": item_id, "text": text, "config": config, "source": "compatibility"}, config
    )
    return VolcengineProvider().speak(request, ProcessAudioPlayer())


def speak_with_provider(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    provider = config.get("provider", "system_say")
    if provider == "volcengine" and volcengine_speak(text, config, item_id):
        return True
    if provider != "system_say" and config.get("fallback_provider", "system_say") != "system_say":
        return False
    return system_say(text, config, item_id)


def speak_request(request: SpeakRequest, player: AudioPlayer | None = None) -> bool:
    player = player or ProcessAudioPlayer()
    provider_name = request.config.get("provider", "system_say")
    provider: TTSProvider = VolcengineProvider() if provider_name == "volcengine" else SystemSayProvider()
    if provider_name == "volcengine" and provider.speak(request, player):
        return True
    if provider_name != "system_say" and request.config.get("fallback_provider", "system_say") != "system_say":
        return False
    return SystemSayProvider().speak(request, player)


def forward_notify(config: dict[str, Any], argv: list[str]) -> None:
    command = config.get("forward_notify")
    if not command:
        return
    try:
        subprocess.Popen([str(x) for x in command] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _log_event("notify", "forward", "started", command=str(command[0]))
    except OSError as exc:
        _log_event("notify", "forward", "failed", error=type(exc).__name__)
        print(f"agentrelay: unable to forward notify: {exc}", file=sys.stderr)


def _config_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("provider") not in ("system_say", "volcengine"):
        errors.append("provider must be system_say or volcengine")
    if config.get("fallback_provider") != "system_say":
        errors.append("fallback_provider must be system_say")
    if config.get("language") not in SUPPORTED_LANGUAGES:
        errors.append(f"language must be one of: {', '.join(SUPPORTED_LANGUAGES)}")
    if config.get("final_notify_mode") not in ("off", "if_not_spoken", "always"):
        errors.append("final_notify_mode must be off, if_not_spoken, or always")
    for key in ("rate", "max_chars", "spoken_max_chars", "normal_cooldown_seconds", "max_queue_age_seconds"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"{key} must be a non-negative integer")
    command = config.get("forward_notify")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        errors.append("forward_notify must be an array of strings")
    return errors


def _directory_writable(path: Path) -> bool:
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return os.access(target, os.W_OK | os.X_OK)


def doctor() -> int:
    config = load_config()
    errors = _config_errors(config)
    if CONFIG_PATH.exists():
        try:
            raw_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw_config, dict):
                errors.append("config root must be a JSON object")
            elif int(raw_config.get("schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
                errors.append("config schema is newer than this AgentRelay version")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            errors.append("config is not valid JSON")
    print(f"config: {CONFIG_PATH} ({'present' if CONFIG_PATH.exists() else 'using defaults'})")
    print(f"schema: {config.get('schema_version', 'legacy')} (supported: {SCHEMA_VERSION})")
    print(f"runtime directory: {APP_DIR} ({'writable' if _directory_writable(APP_DIR) else 'not writable'})")
    print(f"speech: {'available' if Path('/usr/bin/say').exists() else 'missing /usr/bin/say'}")
    print(f"player: {'available' if Path('/usr/bin/afplay').exists() else 'missing /usr/bin/afplay'}")
    print(f"language: {config.get('language', 'zh-CN')}")
    print(f"voice: {config.get('voice', 'Tingting')}")
    print(f"provider: {config.get('provider', 'system_say')}")
    print(f"daemon: {'available' if _daemon_alive() else 'not running'}")
    print(f"daemon socket: {SOCKET_PATH}")
    print(f"speech updates: max={config.get('spoken_max_chars', 200)} cooldown={config.get('normal_cooldown_seconds', 12)}s")
    print(f"final notify: {config.get('final_notify_mode', 'if_not_spoken')}")
    print(f"volcengine key: {'configured' if volcengine_api_key() else 'missing'}")
    try:
        import websockets  # noqa: F401

        cloud_dependency = "available"
    except ImportError:
        cloud_dependency = "missing"
    print(f"volcengine dependency: {cloud_dependency}")
    print(f"volcengine resource: {config.get('volcengine_resource_id', 'seed-tts-2.0')}")
    print(f"volcengine speaker: {config.get('volcengine_speaker')}")
    print(f"event log: {EVENT_LOG}")
    command = config.get("forward_notify")
    print(f"forward: {' '.join(command) if command else 'disabled'}")
    if not _directory_writable(APP_DIR):
        errors.append("runtime directory is not writable")
    if not Path("/usr/bin/say").exists():
        errors.append("/usr/bin/say is missing")
    if config.get("provider") == "volcengine":
        if not volcengine_api_key():
            errors.append("Volcengine provider requires VOLCENGINE_TTS_API_KEY")
        if cloud_dependency == "missing":
            errors.append("Volcengine provider requires the websockets package")
        if not Path("/usr/bin/afplay").exists():
            errors.append("Volcengine provider requires /usr/bin/afplay")
    if command and not Path(command[0]).exists():
        errors.append("forward command does not exist")
    for error in errors:
        print(f"error: {error}")
    print(f"result: {'ok' if not errors else 'issues found'}")
    return 0 if not errors else 1


def enable_volcengine() -> int:
    if not volcengine_api_key():
        print(f"agentrelay: VOLCENGINE_TTS_API_KEY is missing from {ENV_PATH}", file=sys.stderr)
        return 1
    config = load_config()
    config["provider"] = "volcengine"
    config["fallback_provider"] = "system_say"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(CONFIG_PATH, config)
    print(f"Enabled Volcengine Seed TTS 2.0 in {CONFIG_PATH}")
    return 0


def set_provider(provider: str) -> int:
    """Persist a supported provider without changing the Codex integration."""
    if provider not in ("system_say", "volcengine"):
        print(f"agentrelay: unsupported provider: {provider}", file=sys.stderr)
        return 2
    if provider == "volcengine" and not volcengine_api_key():
        print(f"agentrelay: VOLCENGINE_TTS_API_KEY is missing from {ENV_PATH}", file=sys.stderr)
        return 1
    config = load_config()
    config["provider"] = provider
    config["fallback_provider"] = "system_say"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(CONFIG_PATH, config)
    print(f"Provider set to {provider} in {CONFIG_PATH}")
    return 0


def set_language(language: str) -> int:
    """Persist a supported language and its default system voice."""
    if language not in SUPPORTED_LANGUAGES:
        print(f"agentrelay: unsupported language: {language}", file=sys.stderr)
        return 2
    config = load_config()
    previous_language = str(config.get("language", "zh-CN"))
    previous_default = SUPPORTED_LANGUAGES.get(previous_language, {}).get("system_voice")
    if not config.get("voice") or config.get("voice") == previous_default:
        config["voice"] = SUPPORTED_LANGUAGES[language]["system_voice"]
    config["language"] = language
    config["schema_version"] = SCHEMA_VERSION
    _write_json(CONFIG_PATH, config)
    print(f"Language set to {language} in {CONFIG_PATH}")
    return 0


def _queue_length() -> int:
    try:
        return sum(1 for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def status() -> int:
    config = load_config()
    try:
        result = json.loads(LAST_RESULT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        result = {}
    try:
        playback = json.loads(PLAYBACK_STATE_PATH.read_text(encoding="utf-8"))
        os.kill(int(playback["pid"]), 0)
        playback_status = f"active ({playback.get('provider', 'unknown')})"
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        playback_status = "idle"
    print(f"enabled: {str(bool(config.get('enabled', True))).lower()}")
    print(f"language: {config.get('language', 'zh-CN')}")
    print(f"provider: {config.get('provider', 'system_say')}")
    print(f"daemon: {'running' if _daemon_alive() else 'stopped'}")
    print(f"queue: {_queue_length()}")
    print(f"playback: {playback_status}")
    if result:
        print(f"last result: {result.get('status', 'unknown')} ({result.get('provider', 'unknown')})")
    else:
        print("last result: none")
    print(f"event log: {EVENT_LOG}")
    print(f"metrics: {METRICS_PATH}")
    return 0


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _replace_notify(source: str, notify_line: str) -> str:
    """Replace a top-level notify array, including its multiline form."""
    lines = source.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^notify\s*=", line)), None)
    if start is None:
        return notify_line + "\n" + source
    depth = 0
    end = start
    opened = False
    for end in range(start, len(lines)):
        for char in lines[end]:
            if char == "[":
                depth += 1
                opened = True
            elif char == "]":
                depth -= 1
        if opened and depth <= 0:
            break
    return "".join(lines[:start]) + notify_line + "\n" + "".join(lines[end + 1 :])


def install() -> int:
    if tomllib is None:
        print("agentrelay: install requires Python 3.11 or newer", file=sys.stderr)
        return 1
    try:
        source = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
        parsed = tomllib.loads(source)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"agentrelay: cannot read Codex config: {exc}", file=sys.stderr)
        return 1

    executable = str(Path(__file__).resolve())
    target = [executable, "codex-notify"]
    existing = parsed.get("notify")
    if existing == target:
        print("AgentRelay is already installed.")
        return 0
    if existing is not None and not isinstance(existing, list):
        print("agentrelay: Codex notify setting is not a command array", file=sys.stderr)
        return 1

    config = load_config()
    if existing:
        config["forward_notify"] = [str(part) for part in existing]
    APP_DIR.mkdir(parents=True, exist_ok=True)
    config["schema_version"] = SCHEMA_VERSION
    _write_json(CONFIG_PATH, config)
    shutil.copy2(CODEX_CONFIG_PATH, CODEX_CONFIG_BACKUP)

    notify_line = "notify = [" + ", ".join(_toml_string(part) for part in target) + "]"
    updated = _replace_notify(source, notify_line)
    CODEX_CONFIG_PATH.write_text(updated, encoding="utf-8")
    Path(executable).chmod(Path(executable).stat().st_mode | 0o111)
    print(f"Installed AgentRelay in {CODEX_CONFIG_PATH}")
    print(f"Preserved previous notify command in {CONFIG_PATH}")
    print(f"Backup: {CODEX_CONFIG_BACKUP}")
    return 0


def uninstall() -> int:
    if not CODEX_CONFIG_BACKUP.exists():
        print(f"agentrelay: backup not found: {CODEX_CONFIG_BACKUP}", file=sys.stderr)
        return 1
    shutil.copy2(CODEX_CONFIG_BACKUP, CODEX_CONFIG_PATH)
    print(f"Restored {CODEX_CONFIG_PATH} from {CODEX_CONFIG_BACKUP}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentrelay")
    sub = parser.add_subparsers(dest="command", required=True)
    speak = sub.add_parser("speak")
    speak.add_argument("text", nargs="+")
    update = sub.add_parser("speak-update")
    update.add_argument("text", nargs="+")
    update.add_argument("--priority", choices=("normal", "important"), default="normal")
    update.add_argument("--replace", action=argparse.BooleanOptionalAction, default=True)
    update.add_argument("--turn-id", default="")
    notify = sub.add_parser("codex-notify")
    notify.add_argument("event_and_payload", nargs="*")
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("daemon")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    provider = sub.add_parser("provider", help="select the speech provider")
    provider.add_argument("name", choices=("system_say", "volcengine"))
    language = sub.add_parser("language", help="select the speech language")
    language.add_argument("name", choices=tuple(SUPPORTED_LANGUAGES))
    test_volcengine = sub.add_parser("volcengine-test")
    test_volcengine.add_argument("text", nargs="*", default=["你好，豆包语音合成模型二点零已经接入 AgentRelay。"])
    sub.add_parser("volcengine-enable")
    worker_parser = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--fallback-system", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "speak":
        config = load_config()
        try:
            if enqueue(" ".join(args.text), config, "manual"):
                start_runtime(config)
            return 0
        except OSError as exc:
            _log_event("enqueue", "manual", "failed", error=type(exc).__name__)
            print(json.dumps({"status": "failed", "reason": "runtime_unavailable"}), file=sys.stderr)
            return 1
    if args.command == "speak-update":
        config = load_config()
        try:
            result = enqueue_update(" ".join(args.text), config, args.priority, args.replace, args.turn_id)
        except OSError as exc:
            _log_event("enqueue", "update", "failed", error=type(exc).__name__)
            result = {"status": "skipped", "reason": "runtime_unavailable"}
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "accepted":
            start_runtime(config)
        return 0
    if args.command == "_worker":
        worker(args.fallback_system)
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "status":
        return status()
    if args.command == "stop":
        response = _socket_request({"operation": "stop", "clear_queue": True})
        stopped = response is not None or _stop_runtime()
        print(json.dumps(response or {"status": "stopped", "playback_was_active": stopped}))
        return 0
    if args.command == "daemon":
        return daemon()
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    if args.command == "provider":
        return set_provider(args.name)
    if args.command == "language":
        return set_language(args.name)
    if args.command == "volcengine-test":
        return 0 if volcengine_speak(" ".join(args.text), load_config()) else 1
    if args.command == "volcengine-enable":
        return enable_volcengine()
    event, text = parse_notify(args.event_and_payload)
    turn_id = notify_turn_id(args.event_and_payload)
    config = load_config()
    final_content_hash = content_id(clean_text(text, int(config.get("final_spoken_max_chars", 200))))
    _log_event("notify", "received", "accepted", event=event or "unknown", has_text=bool(text))
    # Forward first so an audio failure cannot affect the existing integration.
    forward_notify(config, args.event_and_payload)
    try:
        supported_event = event in ("agent-turn-complete", "turn-ended", "turn_completed", "turn_complete")
        policy_allows = should_speak_final(config, turn_id, final_content_hash) if supported_event else False
        if not supported_event:
            _log_event("notify", "speech", "skipped", reason="unsupported_event")
        elif not policy_allows:
            _log_event("notify", "speech", "skipped", reason="final_notify_policy")
        elif enqueue(
            text,
            config,
            event,
            source="notify",
            max_chars=int(config.get("final_spoken_max_chars", 200)),
            turn_id=turn_id,
        ):
            start_runtime(config)
    except (OSError, TypeError, ValueError) as exc:
        _log_event("notify", "speech", "failed", error=type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
