# Repository Guidelines

## Project Structure & Module Organization

AgentRelay is a small Python 3.11+ CLI for macOS speech notifications. `agentrelay.py` is the main entry point and owns configuration, queueing, Codex integration, and provider selection. `volcengine_tts.py` implements the optional cloud provider, while `volcengine_protocol.py` handles its wire protocol. Tests live in `tests/`, currently in `tests/test_agentrelay.py`. Keep user-facing examples in `examples/` and design or operational notes in `docs/`. Runtime files belong under `~/.config/agentrelay/` or an isolated `AGENTRELAY_HOME`, never in the repository.

## Build, Test, and Development Commands

The default provider has no third-party runtime dependencies.

```sh
python3 -m unittest discover -v
python3 -m py_compile agentrelay.py volcengine_protocol.py volcengine_tts.py
python3 agentrelay.py doctor
python3 agentrelay.py speak "AgentRelay test"
```

The first command runs the complete test suite; the second catches syntax and import-time compilation errors. `doctor` checks local configuration and macOS speech tools. For Volcengine work, install optional dependencies with `python3 -m pip install -r requirements-volcengine.txt`, then use `python3 agentrelay.py volcengine-test "test"`.

## Coding Style & Naming Conventions

Use four-space indentation, standard-library APIs where practical, and type hints for public or non-obvious interfaces. Follow existing Python naming: `snake_case` for functions and variables, `UPPER_SNAKE_CASE` for module constants, and `PascalCase` for classes. Keep the CLI dependency-light and provider-specific behavior outside the core queueing path. No formatter or linter is configured, so match nearby code and keep imports grouped according to standard Python conventions.

## Testing Guidelines

Tests use the standard `unittest` framework and `unittest.mock`. Name test methods `test_<behavior>` and isolate filesystem state with `tempfile.TemporaryDirectory()` plus patched module paths. Add focused regression tests for parsing, cleanup, deduplication, queue policy, configuration changes, and provider fallback. There is no stated coverage threshold; new behavior should include meaningful success and failure cases.

## Commit & Pull Request Guidelines

Keep commit subjects short and imperative. Keep commits focused and avoid committing `.env`, API keys, generated audio, or runtime state. Pull requests should explain the behavior change, list verification commands, note macOS or cloud-provider requirements, and link relevant issues. Include terminal output for CLI contract changes; screenshots are only useful for changes with a visual surface.

## Security & Configuration

Copy `.env.example` to `.env` for local cloud testing. Secrets must remain in environment variables or the ignored `.env` file and must never appear in logs, fixtures, or committed configuration.
