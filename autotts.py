#!/usr/bin/env python3
"""Small dependency-free Codex notify -> macOS speech adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11+ is expected on current macOS tooling
    tomllib = None


APP_DIR = Path(os.environ.get("AUTOTTS_HOME", Path.home() / ".config" / "autotts"))
SCHEMA_VERSION = 1
CONFIG_PATH = APP_DIR / "config.json"
QUEUE_PATH = APP_DIR / "queue.jsonl"
SEEN_PATH = APP_DIR / "seen.json"
SPEECH_STATE_PATH = APP_DIR / "speech-state.json"
PLAYBACK_STATE_PATH = APP_DIR / "playback-state.json"
LAST_RESULT_PATH = APP_DIR / "last-result.json"
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
CODEX_CONFIG_BACKUP = Path.home() / ".codex" / "config.toml.autotts-backup"
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
        item_id = request_id(event, text)
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


def enqueue_update(text: str, config: dict[str, Any], priority: str = "normal", replace: bool = True) -> dict[str, Any]:
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
            "priority": priority,
            "replace": replace,
            "config": config,
        }
        with QUEUE_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        seen[item_id] = now
        _write_json(SEEN_PATH, {"schema_version": SCHEMA_VERSION, "items": seen})
        _write_json(
            SPEECH_STATE_PATH,
            {
                "schema_version": SCHEMA_VERSION,
                "state": {"last_update_at": now, "last_text_hash": item_id, "priority": priority},
            },
        )
        if replace:
            _cancel_playback("replacement_update_enqueued")
        _log_event("enqueue", "update", "accepted", item_id=item_id, priority=priority)
        return {"status": "accepted", "length": len(cleaned), "priority": priority}
    finally:
        _release_lock(ENQUEUE_LOCK)


def should_speak_final(config: dict[str, Any]) -> bool:
    mode = config.get("final_notify_mode", "if_not_spoken")
    if mode == "off":
        return False
    if mode == "always":
        return True
    state = _load_speech_state()
    suppress = max(0, int(config.get("final_notify_suppress_seconds", 120)))
    return time.time() - float(state.get("last_update_at", 0)) > suppress


def request_id(event: str, payload: str) -> str:
    return hashlib.sha256((event + "\0" + payload).encode()).hexdigest()


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


def worker() -> None:
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
                    max_age = max(0, int(config.get("max_queue_age_seconds", 30)))
                    if max_age and time.time() - float(item.get("created", 0)) > max_age:
                        _log_event("worker", "request", "skipped", item_id=item.get("id", ""), reason="expired")
                        continue
                    item_config = item.get("config", config)
                    provider = str(item_config.get("provider", "system_say"))
                    succeeded = speak_with_provider(item["text"], item_config, item_id=item.get("id", ""))
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


def spawn_worker() -> bool:
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        _log_event("worker", "spawn", "failed", error=type(exc).__name__)
        return False


def system_say(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    language = str(config.get("language", "zh-CN"))
    default_voice = SUPPORTED_LANGUAGES.get(language, SUPPORTED_LANGUAGES["zh-CN"])["system_voice"]
    command = [
        "/usr/bin/say",
        "-v",
        str(config.get("voice") or default_voice),
        "-r",
        str(config.get("rate", 190)),
        text,
    ]
    return _run_playback(command, "system_say", item_id)


def volcengine_api_key() -> str:
    from volcengine_tts import load_env

    return os.environ.get("VOLCENGINE_TTS_API_KEY", "") or load_env(ENV_PATH).get(
        "VOLCENGINE_TTS_API_KEY", ""
    )


def _log_provider_error(message: str) -> None:
    _log_event("provider", "operation", "failed", detail=message)


def volcengine_speak(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    from volcengine_tts import synthesize_sync

    api_key = volcengine_api_key()
    if not api_key:
        _log_provider_error("VOLCENGINE_TTS_API_KEY is missing")
        return False
    audio_dir = APP_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_format = str(config.get("volcengine_format", "mp3"))
    output = audio_dir / f"speech-{uuid.uuid4().hex}.{audio_format}"
    try:
        result = synthesize_sync(
            text=text,
            output=output,
            api_key=api_key,
            speaker=str(config.get("volcengine_speaker", "zh_female_gaolengyujie_uranus_bigtts")),
            resource_id=str(config.get("volcengine_resource_id", "seed-tts-2.0")),
            audio_format=audio_format,
            sample_rate=int(config.get("volcengine_sample_rate", 24000)),
        )
    except Exception as exc:
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
        return _run_playback(["/usr/bin/afplay", str(output)], "volcengine", item_id)
    finally:
        output.unlink(missing_ok=True)


def speak_with_provider(text: str, config: dict[str, Any], item_id: str = "") -> bool:
    provider = config.get("provider", "system_say")
    if provider == "volcengine" and volcengine_speak(text, config, item_id):
        return True
    if provider != "system_say" and config.get("fallback_provider", "system_say") != "system_say":
        return False
    return system_say(text, config, item_id)


def forward_notify(config: dict[str, Any], argv: list[str]) -> None:
    command = config.get("forward_notify")
    if not command:
        return
    try:
        subprocess.Popen([str(x) for x in command] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _log_event("notify", "forward", "started", command=str(command[0]))
    except OSError as exc:
        _log_event("notify", "forward", "failed", error=type(exc).__name__)
        print(f"autotts: unable to forward notify: {exc}", file=sys.stderr)


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
                errors.append("config schema is newer than this AutoTTS version")
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
        print(f"autotts: VOLCENGINE_TTS_API_KEY is missing from {ENV_PATH}", file=sys.stderr)
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
        print(f"autotts: unsupported provider: {provider}", file=sys.stderr)
        return 2
    if provider == "volcengine" and not volcengine_api_key():
        print(f"autotts: VOLCENGINE_TTS_API_KEY is missing from {ENV_PATH}", file=sys.stderr)
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
        print(f"autotts: unsupported language: {language}", file=sys.stderr)
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
    print(f"queue: {_queue_length()}")
    print(f"playback: {playback_status}")
    if result:
        print(f"last result: {result.get('status', 'unknown')} ({result.get('provider', 'unknown')})")
    else:
        print("last result: none")
    print(f"event log: {EVENT_LOG}")
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
        print("autotts: install requires Python 3.11 or newer", file=sys.stderr)
        return 1
    try:
        source = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
        parsed = tomllib.loads(source)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"autotts: cannot read Codex config: {exc}", file=sys.stderr)
        return 1

    executable = str(Path(__file__).resolve())
    target = [executable, "codex-notify"]
    existing = parsed.get("notify")
    if existing == target:
        print("AutoTTS is already installed.")
        return 0
    if existing is not None and not isinstance(existing, list):
        print("autotts: Codex notify setting is not a command array", file=sys.stderr)
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
    print(f"Installed AutoTTS in {CODEX_CONFIG_PATH}")
    print(f"Preserved previous notify command in {CONFIG_PATH}")
    print(f"Backup: {CODEX_CONFIG_BACKUP}")
    return 0


def uninstall() -> int:
    if not CODEX_CONFIG_BACKUP.exists():
        print(f"autotts: backup not found: {CODEX_CONFIG_BACKUP}", file=sys.stderr)
        return 1
    shutil.copy2(CODEX_CONFIG_BACKUP, CODEX_CONFIG_PATH)
    print(f"Restored {CODEX_CONFIG_PATH} from {CODEX_CONFIG_BACKUP}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotts")
    sub = parser.add_subparsers(dest="command", required=True)
    speak = sub.add_parser("speak")
    speak.add_argument("text", nargs="+")
    update = sub.add_parser("speak-update")
    update.add_argument("text", nargs="+")
    update.add_argument("--priority", choices=("normal", "important"), default="normal")
    update.add_argument("--replace", action=argparse.BooleanOptionalAction, default=True)
    notify = sub.add_parser("codex-notify")
    notify.add_argument("event_and_payload", nargs="*")
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    provider = sub.add_parser("provider", help="select the speech provider")
    provider.add_argument("name", choices=("system_say", "volcengine"))
    language = sub.add_parser("language", help="select the speech language")
    language.add_argument("name", choices=tuple(SUPPORTED_LANGUAGES))
    test_volcengine = sub.add_parser("volcengine-test")
    test_volcengine.add_argument("text", nargs="*", default=["你好，豆包语音合成模型二点零已经接入 AutoTTS。"])
    sub.add_parser("volcengine-enable")
    sub.add_parser("_worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "speak":
        config = load_config()
        try:
            if enqueue(" ".join(args.text), config, "manual"):
                spawn_worker()
            return 0
        except OSError as exc:
            _log_event("enqueue", "manual", "failed", error=type(exc).__name__)
            print(json.dumps({"status": "failed", "reason": "runtime_unavailable"}), file=sys.stderr)
            return 1
    if args.command == "speak-update":
        config = load_config()
        try:
            result = enqueue_update(" ".join(args.text), config, args.priority, args.replace)
        except OSError as exc:
            _log_event("enqueue", "update", "failed", error=type(exc).__name__)
            result = {"status": "skipped", "reason": "runtime_unavailable"}
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "accepted":
            spawn_worker()
        return 0
    if args.command == "_worker":
        worker()
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "status":
        return status()
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
    config = load_config()
    _log_event("notify", "received", "accepted", event=event or "unknown", has_text=bool(text))
    # Forward first so an audio failure cannot affect the existing integration.
    forward_notify(config, args.event_and_payload)
    try:
        supported_event = event in ("agent-turn-complete", "turn-ended", "turn_completed", "turn_complete")
        policy_allows = should_speak_final(config) if supported_event else False
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
        ):
            spawn_worker()
    except (OSError, TypeError, ValueError) as exc:
        _log_event("notify", "speech", "failed", error=type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
