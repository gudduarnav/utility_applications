# Repository Guidelines

## Project Structure & Module Organization

- Place GUI code under `src/` per tool when modular (e.g., `youtube_download480p`); standalone utilities like `youtube_audio_mp3.py` live at the repo root.
- Add tests in `tests/`, mirroring the `src` layout (e.g., `src/.../utils.py` → `tests/test_utils.py`).
- Keep configuration and metadata in the project root (e.g., `pyproject.toml`, `.env.example`, CI configs).

## Build, Test, and Development Commands

- Install dependencies in editable mode: `python -m pip install -e .`.
- Launch the GUIs with `python -m youtube_download480p.app` or `python -m youtube_audio_mp3.app`, or use the console entry points.
- Run the automated tests (when present) with `python -m pytest`.
- Use `python -m compileall src` for a quick syntax validation if GUI testing is not possible.

## Coding Style & Naming Conventions

- Use Python 3, 4-space indentation, and type hints for public functions and classes.
- Name modules and functions with `snake_case`, classes with `PascalCase`, and constants with `UPPER_SNAKE_CASE`.
- Prefer small, single-responsibility functions; keep network and filesystem access isolated behind well-named helpers.
- Where configured, run `black src tests` and `ruff src tests` before committing to maintain consistent style.

## Testing Guidelines

- Write tests with `pytest`; name files `test_*.py` and test functions `test_<behavior>()`.
- Aim to cover core download flows, error handling (network issues, invalid URLs), and CLI argument parsing.
- Avoid hitting real YouTube endpoints in tests; use fixtures, mocking, or sample HTML/JSON where needed.

## Commit & Pull Request Guidelines

- Use clear, imperative commit summaries (e.g., `Add basic CLI entry point`, `Fix retry logic for downloads`).
- Keep pull requests focused and include: a short description, testing steps, and notes on user-facing changes.
- Link related issues and attach logs or screenshots when behavior or UX changes.
- Do not commit secrets or local configuration; update `.env.example` instead when new settings are required.

## Agent-Specific Instructions

- Follow these guidelines when generating or modifying code, and prefer minimal, focused diffs.
- Match existing patterns and tools instead of introducing new dependencies or structures without clear justification.
