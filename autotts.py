#!/usr/bin/env python3
"""Small dependency-free Codex notify -> macOS speech adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
CONFIG_PATH = APP_DIR / "config.json"
QUEUE_PATH = APP_DIR / "queue.jsonl"
SEEN_PATH = APP_DIR / "seen.json"
SPEECH_STATE_PATH = APP_DIR / "speech-state.json"
ENQUEUE_LOCK = APP_DIR / "enqueue.lock"
WORKER_LOCK = APP_DIR / "worker.lock"
PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
PROVIDER_LOG = APP_DIR / "provider.log"
# The existing notify command is captured during `install`; never bake a
# machine-specific integration path into the repository defaults.
DEFAULT_NOTIFY: list[str] = []
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_CONFIG_BACKUP = Path.home() / ".codex" / "config.toml.autotts-backup"


def defaults() -> dict[str, Any]:
    return {
        "enabled": True,
        "provider": "system_say",
        "fallback_provider": "system_say",
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
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_speech_state() -> dict[str, Any]:
    try:
        value = json.loads(SPEECH_STATE_PATH.read_text())
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
        return False
    text = clean_text(text, max_chars if max_chars is not None else int(config.get("max_chars", 1800)))
    if not text:
        return False
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(ENQUEUE_LOCK):
        return False
    try:
        item_id = request_id(event, text)
        now = time.time()
        window = max(0, int(config.get("dedupe_seconds", 30)))
        seen = {key: stamp for key, stamp in _load_seen().items() if now - float(stamp) <= window}
        if item_id in seen:
            return False
        item = {
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
        SEEN_PATH.write_text(json.dumps(seen))
        return True
    finally:
        _release_lock(ENQUEUE_LOCK)


def enqueue_update(text: str, config: dict[str, Any], priority: str = "normal", replace: bool = True) -> dict[str, Any]:
    if priority not in ("normal", "important"):
        return {"status": "skipped", "reason": "invalid_priority"}
    cleaned = clean_update_text(text)
    if not cleaned:
        return {"status": "skipped", "reason": "empty"}
    maximum = int(config.get("spoken_max_chars", 200))
    if len(cleaned) > maximum:
        return {"status": "skipped", "reason": "too_long", "length": len(cleaned), "max_chars": maximum}
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(ENQUEUE_LOCK):
        return {"status": "skipped", "reason": "busy"}
    try:
        now = time.time()
        state = _load_speech_state()
        if priority == "normal":
            cooldown = max(0, int(config.get("normal_cooldown_seconds", 12)))
            elapsed = now - float(state.get("last_update_at", 0))
            if elapsed < cooldown:
                return {"status": "skipped", "reason": "cooldown", "retry_after": round(cooldown - elapsed, 1)}
        item_id = request_id("model-update", cleaned)
        seen = _load_seen()
        window = max(0, int(config.get("dedupe_seconds", 30)))
        seen = {key: stamp for key, stamp in seen.items() if now - float(stamp) <= window}
        if item_id in seen:
            return {"status": "skipped", "reason": "duplicate"}
        item = {
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
        SEEN_PATH.write_text(json.dumps(seen))
        SPEECH_STATE_PATH.write_text(
            json.dumps({"last_update_at": now, "last_text_hash": item_id, "priority": priority})
        )
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


def worker() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(WORKER_LOCK, attempts=100):
        return
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
                        continue
                    item_config = item.get("config", config)
                    speak_with_provider(item["text"], item_config)
                except (KeyError, OSError):
                    continue
    finally:
        _release_lock(WORKER_LOCK)


def spawn_worker() -> None:
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def system_say(text: str, config: dict[str, Any]) -> bool:
    command = [
        "/usr/bin/say",
        "-v",
        str(config.get("voice", "Tingting")),
        "-r",
        str(config.get("rate", 190)),
        text,
    ]
    return subprocess.run(command, check=False).returncode == 0


def volcengine_api_key() -> str:
    from volcengine_tts import load_env

    return os.environ.get("VOLCENGINE_TTS_API_KEY", "") or load_env(ENV_PATH).get(
        "VOLCENGINE_TTS_API_KEY", ""
    )


def _log_provider_error(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with PROVIDER_LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")


def volcengine_speak(text: str, config: dict[str, Any]) -> bool:
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
        _log_provider_error(f"Volcengine synthesis failed: {exc}")
        return False
    try:
        _log_provider_error(
            f"Volcengine synthesis succeeded: bytes={result['bytes']} log_id={result.get('log_id', '')}"
        )
        return subprocess.run(["/usr/bin/afplay", str(output)], check=False).returncode == 0
    finally:
        output.unlink(missing_ok=True)


def speak_with_provider(text: str, config: dict[str, Any]) -> bool:
    provider = config.get("provider", "system_say")
    if provider == "volcengine" and volcengine_speak(text, config):
        return True
    if provider != "system_say" and config.get("fallback_provider", "system_say") != "system_say":
        return False
    return system_say(text, config)


def forward_notify(config: dict[str, Any], argv: list[str]) -> None:
    command = config.get("forward_notify")
    if not command:
        return
    try:
        subprocess.Popen([str(x) for x in command] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print(f"autotts: unable to forward notify: {exc}", file=sys.stderr)


def doctor() -> int:
    config = load_config()
    print(f"config: {CONFIG_PATH} ({'present' if CONFIG_PATH.exists() else 'using defaults'})")
    print(f"speech: {'available' if Path('/usr/bin/say').exists() else 'missing /usr/bin/say'}")
    print(f"voice: {config['voice']}")
    print(f"provider: {config.get('provider', 'system_say')}")
    print(f"speech updates: max={config.get('spoken_max_chars', 200)} cooldown={config.get('normal_cooldown_seconds', 12)}s")
    print(f"final notify: {config.get('final_notify_mode', 'if_not_spoken')}")
    print(f"volcengine key: {'configured' if volcengine_api_key() else 'missing'}")
    print(f"volcengine resource: {config.get('volcengine_resource_id', 'seed-tts-2.0')}")
    print(f"volcengine speaker: {config.get('volcengine_speaker')}")
    print(f"provider log: {PROVIDER_LOG}")
    command = config.get("forward_notify")
    print(f"forward: {' '.join(command) if command else 'disabled'}")
    if command and not Path(command[0]).exists():
        print("warning: forward command does not exist")
        return 1
    return 0


def enable_volcengine() -> int:
    if not volcengine_api_key():
        print(f"autotts: VOLCENGINE_TTS_API_KEY is missing from {ENV_PATH}", file=sys.stderr)
        return 1
    config = load_config()
    config["provider"] = "volcengine"
    config["fallback_provider"] = "system_say"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)
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
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)
    print(f"Provider set to {provider} in {CONFIG_PATH}")
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
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    sub.add_parser("install")
    sub.add_parser("uninstall")
    provider = sub.add_parser("provider", help="select the speech provider")
    provider.add_argument("name", choices=("system_say", "volcengine"))
    test_volcengine = sub.add_parser("volcengine-test")
    test_volcengine.add_argument("text", nargs="*", default=["你好，豆包语音合成模型二点零已经接入 AutoTTS。"])
    sub.add_parser("volcengine-enable")
    sub.add_parser("_worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "speak":
        config = load_config()
        if enqueue(" ".join(args.text), config, "manual"):
            spawn_worker()
        return 0
    if args.command == "speak-update":
        config = load_config()
        result = enqueue_update(" ".join(args.text), config, args.priority, args.replace)
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "accepted":
            spawn_worker()
        return 0
    if args.command == "_worker":
        worker()
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    if args.command == "provider":
        return set_provider(args.name)
    if args.command == "volcengine-test":
        return 0 if volcengine_speak(" ".join(args.text), load_config()) else 1
    if args.command == "volcengine-enable":
        return enable_volcengine()
    event, text = parse_notify(args.event_and_payload)
    config = load_config()
    # Forward first so an audio failure cannot affect the existing integration.
    forward_notify(config, args.event_and_payload)
    if (
        event in ("agent-turn-complete", "turn-ended", "turn_completed", "turn_complete")
        and should_speak_final(config)
        and enqueue(
            text,
            config,
            event,
            source="notify",
            max_chars=int(config.get("final_spoken_max_chars", 200)),
        )
    ):
        spawn_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
