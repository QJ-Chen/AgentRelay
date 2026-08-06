import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import autotts
from volcengine_protocol import EventType, Message, MsgType, Flag
from volcengine_tts import load_env, text_chunks


class AutoTTSTest(unittest.TestCase):
    def test_defaults_do_not_contain_machine_specific_notify_path(self):
        self.assertEqual(autotts.defaults()["forward_notify"], [])

    def test_defaults_use_chinese_language(self):
        self.assertEqual(autotts.defaults()["language"], "zh-CN")

    def test_speak_request_builds_provider_neutral_request(self):
        config = autotts.defaults()
        request = autotts.SpeakRequest.from_item(
            {
                "id": "request-1",
                "text": "hello",
                "source": "test",
                "turn_id": "turn-1",
                "content_hash": "hash-1",
                "config": config,
            },
            config,
        )
        self.assertEqual(
            (request.id, request.language, request.voice, request.source, request.turn_id),
            ("request-1", "zh-CN", "Tingting", "test", "turn-1"),
        )

    def test_system_provider_uses_audio_player_contract(self):
        class FakePlayer:
            def __init__(self):
                self.command = []

            def play(self, command, provider, item_id=""):
                self.command = command
                self.provider = provider
                self.item_id = item_id
                return True

            def stop(self, reason="requested"):
                return True

        request = autotts.SpeakRequest(
            id="request-1", text="hello", language="en-US", voice="Samantha", config=autotts.defaults()
        )
        player = FakePlayer()
        self.assertTrue(autotts.SystemSayProvider().speak(request, player))
        self.assertEqual((player.provider, player.item_id), ("system_say", "request-1"))
        self.assertEqual(player.command[:4], ["/usr/bin/say", "-v", "Samantha", "-r"])

    def test_fake_provider_satisfies_provider_contract(self):
        class FakeProvider:
            name = "fake"

            def speak(self, request, player):
                return request.text == "hello" and player.stop("fake")

        class FakePlayer:
            def play(self, command, provider, item_id=""):
                return True

            def stop(self, reason="requested"):
                return reason == "fake"

        self.assertTrue(FakeProvider().speak(autotts.SpeakRequest(id="1", text="hello"), FakePlayer()))

    def test_volcengine_provider_uses_provider_contract(self):
        request = autotts.SpeakRequest(id="request-1", text="hello", config=autotts.defaults())
        player = object()
        with patch.object(autotts, "_volcengine_speak_request", return_value=True) as speak:
            self.assertTrue(autotts.VolcengineProvider().speak(request, player))
            speak.assert_called_once_with(request, player)

    def test_cleanup_removes_code_urls_and_markdown(self):
        self.assertEqual(
            autotts.clean_text("## Done\n- Read [docs](https://example.com)\n```py\nsecret\n```"),
            "Done Read docs",
        )

    def test_parse_json_final_answer(self):
        payload = json.dumps({"type": "agent-turn-complete", "last-assistant-message": "你好"})
        event, text = autotts.parse_notify([payload])
        self.assertEqual((event, text), ("agent-turn-complete", "你好"))

    def test_parse_nested_last_message(self):
        event, text = autotts.parse_notify(["turn-ended", json.dumps({"items": [{"text": "old"}, {"message": "new"}]})])
        self.assertEqual((event, text), ("turn-ended", "new"))

    def test_enqueue_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                autotts,
                APP_DIR=root,
                QUEUE_PATH=root / "queue.jsonl",
                SEEN_PATH=root / "seen.json",
                ENQUEUE_LOCK=root / "enqueue.lock",
            ):
                config = autotts.defaults()
                self.assertTrue(autotts.enqueue("hello", config))
                self.assertFalse(autotts.enqueue("hello", config))
                self.assertEqual(len(autotts.QUEUE_PATH.read_text().splitlines()), 1)

    def test_twenty_updates_do_not_leave_enqueue_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                autotts,
                APP_DIR=root,
                QUEUE_PATH=root / "queue.jsonl",
                SEEN_PATH=root / "seen.json",
                SPEECH_STATE_PATH=root / "speech-state.json",
                PLAYBACK_STATE_PATH=root / "playback-state.json",
                ENQUEUE_LOCK=root / "enqueue.lock",
                EVENT_LOG=root / "events.jsonl",
            ):
                config = autotts.defaults()
                config["normal_cooldown_seconds"] = 0
                for index in range(20):
                    self.assertEqual(autotts.enqueue_update(f"update {index}", config)["status"], "accepted")
                self.assertEqual(len(autotts.QUEUE_PATH.read_text().splitlines()), 20)
                self.assertFalse(autotts.ENQUEUE_LOCK.exists())

    def test_disabled_does_not_enqueue(self):
        config = autotts.defaults()
        config["enabled"] = False
        self.assertFalse(autotts.enqueue("hello", config))

    def test_secret_redaction(self):
        self.assertEqual(autotts.clean_text("token=abc123 continue"), "token [redacted] continue")

    def test_toml_string_escapes_paths(self):
        self.assertEqual(autotts._toml_string('/tmp/a "voice"'), '"/tmp/a \\"voice\\""')

    def test_replace_multiline_notify(self):
        source = 'model = "x"\nnotify = [\n  "/old",\n  "turn-ended"\n]\n\n[features]\na = true\n'
        result = autotts._replace_notify(source, 'notify = ["/new", "codex-notify"]')
        self.assertEqual(result.count("notify ="), 1)
        self.assertIn('model = "x"\nnotify = ["/new", "codex-notify"]\n\n[features]', result)

    def test_volcengine_falls_back_to_system_say(self):
        config = autotts.defaults()
        config["provider"] = "volcengine"
        with patch.object(autotts, "volcengine_speak", return_value=False) as cloud, patch.object(
            autotts, "system_say", return_value=True
        ) as fallback:
            self.assertTrue(autotts.speak_with_provider("hello", config))
            cloud.assert_called_once()
            fallback.assert_called_once()

    def test_volcengine_success_does_not_fall_back(self):
        config = autotts.defaults()
        config["provider"] = "volcengine"
        with patch.object(autotts, "volcengine_speak", return_value=True), patch.object(
            autotts, "system_say", return_value=True
        ) as fallback:
            self.assertTrue(autotts.speak_with_provider("hello", config))
            fallback.assert_not_called()

    def test_volcengine_protocol_session_round_trip(self):
        message = Message(
            type=MsgType.FullClientRequest,
            flag=Flag.WithEvent,
            event=EventType.TaskRequest,
            session_id="session-1",
            payload=b'{"text":"hello"}',
        )
        decoded = Message.from_bytes(message.marshal())
        self.assertEqual(decoded.event, EventType.TaskRequest)
        self.assertEqual(decoded.session_id, "session-1")
        self.assertEqual(decoded.payload, b'{"text":"hello"}')

    def test_env_parser_and_text_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text('IGNORED LINE\nVOLCENGINE_TTS_API_KEY="secret"\n')
            self.assertEqual(load_env(env_path)["VOLCENGINE_TTS_API_KEY"], "secret")
        self.assertEqual(text_chunks("第一句。第二句！"), ["第一句。", "第二句！"])

    def test_speak_update_accepts_then_applies_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                autotts,
                APP_DIR=root,
                QUEUE_PATH=root / "queue.jsonl",
                SEEN_PATH=root / "seen.json",
                SPEECH_STATE_PATH=root / "speech-state.json",
                ENQUEUE_LOCK=root / "enqueue.lock",
            ):
                config = autotts.defaults()
                self.assertEqual(autotts.enqueue_update("重要进展", config)["status"], "accepted")
                result = autotts.enqueue_update("另一个普通进展", config)
                self.assertEqual((result["status"], result["reason"]), ("skipped", "cooldown"))

    def test_important_update_bypasses_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                autotts,
                APP_DIR=root,
                QUEUE_PATH=root / "queue.jsonl",
                SEEN_PATH=root / "seen.json",
                SPEECH_STATE_PATH=root / "speech-state.json",
                ENQUEUE_LOCK=root / "enqueue.lock",
            ):
                config = autotts.defaults()
                autotts.enqueue_update("普通进展", config)
                result = autotts.enqueue_update("需要用户处理", config, priority="important")
                self.assertEqual(result["status"], "accepted")

    def test_speak_update_rejects_over_limit(self):
        config = autotts.defaults()
        config["spoken_max_chars"] = 4
        result = autotts.enqueue_update("这段内容太长", config)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "too_long"))

    def test_update_cleanup_removes_code_and_paths(self):
        self.assertEqual(
            autotts.clean_update_text("已修改 `token` 和 /tmp/secret.txt，准备验证。"),
            "已修改 和 准备验证。",
        )

    def test_recent_update_suppresses_final_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "speech-state.json"
            state.write_text(json.dumps({"last_update_at": 100.0}))
            with patch.object(autotts, "SPEECH_STATE_PATH", state), patch.object(autotts.time, "time", return_value=110.0):
                self.assertFalse(autotts.should_speak_final(autotts.defaults()))

    def test_turn_id_suppresses_only_matching_final_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "speech-state.json"
            queue = root / "queue.jsonl"
            seen = root / "seen.json"
            with patch.multiple(
                autotts,
                APP_DIR=root,
                SPEECH_STATE_PATH=state,
                QUEUE_PATH=queue,
                SEEN_PATH=seen,
                PLAYBACK_STATE_PATH=root / "playback-state.json",
                ENQUEUE_LOCK=root / "enqueue.lock",
                EVENT_LOG=root / "events.jsonl",
            ):
                config = autotts.defaults()
                self.assertEqual(autotts.enqueue_update("progress", config, turn_id="turn-1")["status"], "accepted")
                self.assertFalse(autotts.should_speak_final(config, turn_id="turn-1", content_hash="different"))
                self.assertTrue(autotts.should_speak_final(config, turn_id="turn-2", content_hash="different"))

    def test_notify_turn_id_extracts_nested_identifier(self):
        payload = json.dumps({"type": "agent-turn-complete", "thread": {"turn_id": "turn-42"}})
        self.assertEqual(autotts.notify_turn_id([payload]), "turn-42")

    def test_set_provider_persists_local_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with patch.object(autotts, "APP_DIR", Path(directory)), patch.object(
                autotts, "CONFIG_PATH", config_path
            ):
                self.assertEqual(autotts.set_provider("system_say"), 0)
                self.assertEqual(json.loads(config_path.read_text())["provider"], "system_say")

    def test_set_provider_rejects_cloud_without_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with patch.object(autotts, "APP_DIR", Path(directory)), patch.object(
                autotts, "CONFIG_PATH", config_path
            ), patch.object(autotts, "volcengine_api_key", return_value=""):
                self.assertEqual(autotts.set_provider("volcengine"), 1)
                self.assertFalse(config_path.exists())

    def test_set_language_persists_language_and_default_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            with patch.multiple(autotts, APP_DIR=root, CONFIG_PATH=config_path):
                self.assertEqual(autotts.set_language("en-US"), 0)
                config = json.loads(config_path.read_text())
                self.assertEqual((config["language"], config["voice"]), ("en-US", "Samantha"))

    def test_versioned_seen_state_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            seen_path = Path(directory) / "seen.json"
            seen_path.write_text(json.dumps({"schema_version": 1, "items": {"request": 10.0}}))
            with patch.object(autotts, "SEEN_PATH", seen_path):
                self.assertEqual(autotts._load_seen(), {"request": 10.0})

    def test_cancel_playback_terminates_recorded_process(self):
        with tempfile.TemporaryDirectory() as directory:
            playback_path = Path(directory) / "playback-state.json"
            playback_path.write_text(json.dumps({"schema_version": 1, "pid": 1234}))
            with patch.object(autotts, "PLAYBACK_STATE_PATH", playback_path), patch.object(
                autotts.os, "kill"
            ) as kill:
                self.assertTrue(autotts._cancel_playback())
                kill.assert_called_once_with(1234, autotts.signal.SIGTERM)

    def test_spawn_worker_failure_is_contained(self):
        with patch.object(autotts.subprocess, "Popen", side_effect=OSError("unavailable")), patch.object(
            autotts, "_log_event"
        ) as log:
            self.assertFalse(autotts.spawn_worker())
            log.assert_called_once_with("worker", "spawn", "failed", error="OSError")

    def test_codex_notify_contains_runtime_directory_failure(self):
        config = autotts.defaults()
        with patch.object(autotts, "load_config", return_value=config), patch.object(
            autotts, "forward_notify"
        ), patch.object(autotts, "enqueue", side_effect=OSError("read only")), patch.object(
            autotts, "_log_event"
        ) as log:
            payload = json.dumps({"type": "agent-turn-complete", "last-assistant-message": "done"})
            self.assertEqual(autotts.main(["codex-notify", payload]), 0)
            self.assertTrue(any(call.args[:3] == ("notify", "speech", "failed") for call in log.call_args_list))

    def test_speak_update_reports_runtime_directory_failure(self):
        with patch.object(autotts, "enqueue_update", side_effect=PermissionError("read only")), patch.object(
            autotts, "_log_event"
        ), patch("builtins.print") as output:
            self.assertEqual(autotts.main(["speak-update", "progress"]), 0)
            result = json.loads(output.call_args.args[0])
            self.assertEqual(result, {"status": "skipped", "reason": "runtime_unavailable"})

    def test_status_reports_queue_and_last_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue_path = root / "queue.jsonl"
            result_path = root / "last-result.json"
            queue_path.write_text("{}\n{}\n")
            result_path.write_text(json.dumps({"status": "succeeded", "provider": "system_say"}))
            with patch.multiple(
                autotts,
                APP_DIR=root,
                QUEUE_PATH=queue_path,
                LAST_RESULT_PATH=result_path,
                PLAYBACK_STATE_PATH=root / "playback-state.json",
                EVENT_LOG=root / "events.jsonl",
            ), patch("builtins.print") as output:
                self.assertEqual(autotts.status(), 0)
                rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
                self.assertIn("queue: 2", rendered)
                self.assertIn("last result: succeeded (system_say)", rendered)

    def test_daemon_health_speak_and_stop_protocol(self):
        activity = {"last": 0.0}
        self.assertEqual(autotts._handle_daemon_request({"operation": "health"}, activity)["status"], "ok")
        with patch.object(autotts.threading, "Thread") as thread:
            response = autotts._handle_daemon_request({"operation": "speak"}, activity)
            self.assertEqual(response["status"], "accepted")
            thread.assert_called_once()
        with patch.object(autotts, "_stop_runtime", return_value=True):
            response = autotts._handle_daemon_request({"operation": "stop"}, activity)
            self.assertEqual(response, {"status": "stopped", "playback_was_active": True})

    def test_daemon_idle_exit_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config = autotts.defaults()
            config["daemon_idle_seconds"] = 1
            config_path.write_text(json.dumps(config))
            with patch.multiple(
                autotts,
                APP_DIR=root,
                CONFIG_PATH=config_path,
                SOCKET_PATH=root / "autotts.sock",
                DAEMON_LOCK_PATH=root / "daemon.lock",
                QUEUE_PATH=root / "queue.jsonl",
                WORKER_LOCK=root / "worker.lock",
                PLAYBACK_STATE_PATH=root / "playback-state.json",
                EVENT_LOG=root / "events.jsonl",
            ):
                for _ in range(2):
                    daemon_thread = threading.Thread(target=autotts.daemon)
                    daemon_thread.start()
                    deadline = time.monotonic() + 1
                    response = None
                    while time.monotonic() < deadline:
                        response = autotts._socket_request({"operation": "health"}, timeout=0.05)
                        if response:
                            break
                        time.sleep(0.01)
                    if response is None:
                        daemon_thread.join(2)
                        self.skipTest("Unix socket operations are unavailable in this sandbox")
                    self.assertEqual(response["status"], "ok")
                    self.assertEqual(autotts.SOCKET_PATH.stat().st_mode & 0o777, 0o600)
                    daemon_thread.join(2)
                    self.assertFalse(daemon_thread.is_alive())
                    self.assertFalse(autotts.SOCKET_PATH.exists())

    def test_start_runtime_falls_back_to_worker(self):
        config = autotts.defaults()
        with patch.object(autotts, "spawn_daemon", return_value=False), patch.object(
            autotts, "spawn_worker", return_value=True
        ) as worker:
            self.assertTrue(autotts.start_runtime(config))
            worker.assert_called_once_with(force_system_say=True)

    def test_direct_mode_does_not_force_system_provider(self):
        config = autotts.defaults()
        config["daemon_enabled"] = False
        with patch.object(autotts, "spawn_worker", return_value=True) as worker:
            self.assertTrue(autotts.start_runtime(config))
            worker.assert_called_once_with()

    def test_daemon_speak_accepts_provider_neutral_fields(self):
        activity = {"last": 0.0}
        with patch.object(autotts, "enqueue", return_value=True) as enqueue, patch.object(
            autotts.threading, "Thread"
        ):
            response = autotts._handle_daemon_request(
                {
                    "operation": "speak",
                    "request": {
                        "id": "request-1",
                        "text": "hello",
                        "language": "en-US",
                        "voice": "Samantha",
                        "speed": 1.25,
                        "interrupt": True,
                        "turn_id": "turn-1",
                    },
                },
                activity,
            )
            self.assertEqual(response["status"], "accepted")
            call = enqueue.call_args
            self.assertEqual(call.args[1]["language"], "en-US")
            self.assertEqual(call.args[1]["speed"], 1.25)
            self.assertEqual(call.kwargs["turn_id"], "turn-1")
            self.assertEqual(call.kwargs["item_id"], "request-1")

    def test_stop_marker_discards_items_already_in_worker_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            item = {
                "schema_version": 1,
                "id": "old-item",
                "text": "old",
                "created": 10,
                "source": "test",
                "replace": False,
                "config": autotts.defaults(),
            }
            queue.write_text(json.dumps(item) + "\n")
            with patch.multiple(
                autotts,
                APP_DIR=root,
                CONFIG_PATH=root / "config.json",
                QUEUE_PATH=queue,
                STOP_STATE_PATH=root / "stop-state.json",
                METRICS_LOCK=root / "metrics.lock",
                WORKER_LOCK=root / "worker.lock",
                ENQUEUE_LOCK=root / "enqueue.lock",
                EVENT_LOG=root / "events.jsonl",
            ), patch.object(autotts, "speak_request") as speak:
                autotts._write_json(autotts.STOP_STATE_PATH, {"schema_version": 1, "stopped_at": 20})
                autotts.worker()
                speak.assert_not_called()

    def test_cloud_metrics_do_not_store_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics_path = root / "metrics.json"
            with patch.multiple(
                autotts, METRICS_PATH=metrics_path, METRICS_LOCK=root / "metrics.lock"
            ):
                autotts._update_metrics("volcengine", "succeeded", characters=12, latency_ms=25.5)
                metrics_text = metrics_path.read_text()
                metrics = json.loads(metrics_text)
                self.assertNotIn("secret spoken text", metrics_text)
                self.assertEqual(metrics["providers"]["volcengine"]["characters"], 12)
                self.assertEqual(metrics["providers"]["volcengine"]["successes"], 1)

    def test_doctor_rejects_malformed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text("{bad json")
            with patch.multiple(
                autotts,
                APP_DIR=root,
                CONFIG_PATH=config_path,
                EVENT_LOG=root / "events.jsonl",
            ), patch.object(autotts, "volcengine_api_key", return_value=""), patch("builtins.print") as output:
                self.assertEqual(autotts.doctor(), 1)
                rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
                self.assertIn("error: config is not valid JSON", rendered)

    def test_install_reinstall_and_uninstall_preserve_existing_notify(self):
        if autotts.tomllib is None:
            self.skipTest("tomllib unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_config = root / "config.toml"
            backup = root / "config.toml.autotts-backup"
            app_dir = root / "runtime"
            runtime_config = app_dir / "config.json"
            codex_config.write_text('model = "test"\nnotify = ["/old", "turn-ended"]\n')
            with patch.multiple(
                autotts,
                APP_DIR=app_dir,
                CONFIG_PATH=runtime_config,
                CODEX_CONFIG_PATH=codex_config,
                CODEX_CONFIG_BACKUP=backup,
            ):
                self.assertEqual(autotts.install(), 0)
                self.assertEqual(json.loads(runtime_config.read_text())["forward_notify"], ["/old", "turn-ended"])
                installed = codex_config.read_text()
                self.assertIn("codex-notify", installed)
                self.assertEqual(autotts.install(), 0)
                self.assertEqual(codex_config.read_text(), installed)
                self.assertEqual(autotts.uninstall(), 0)
                self.assertEqual(codex_config.read_text(), 'model = "test"\nnotify = ["/old", "turn-ended"]\n')


if __name__ == "__main__":
    unittest.main()
