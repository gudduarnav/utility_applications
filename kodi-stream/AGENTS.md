# Repository Guidelines

## Project Structure & Module Organization
Use `src/kodi_stream/` for the Python package, mirroring Kodi components (e.g., `client.py`, `streams/playlist_loader.py`). Helper bash utilities should live in `scripts/`, and reusable configuration templates (sample `.env`, JSON payloads, playlist stubs) belong under `assets/`. Keep all automated checks in `ci/` once they are introduced. Document any directory you add in `README.md` so future agents can reason about the topology quickly.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create an isolated environment before installing anything.
- `pip install -r requirements.txt`: install runtime + dev dependencies; regenerate this file whenever `pyproject.toml` changes.
- `python -m kodi_stream.cli --config configs/dev.yaml`: run the stream publisher locally with a specific profile.
- `pytest`: execute the full automated suite; pass `-k` filters when debugging.
- `bash scripts/smoke_check.sh`: run the lightweight health script prior to sending a PR (add stub if it does not exist yet).

## Coding Style & Naming Conventions
Adopt Black-compatible formatting (88 char lines, 4-space indents) and run `ruff check src tests` to enforce lint rules before pushing. Modules and packages use snake_case; public classes should be PascalCase (`StreamSession`), and functions/CLI flags stay snake_case. Keep environment variables upper snake (`STREAM_BACKEND_URL`). Type-hint new code, and gate experimental logic behind feature flags in `config/*.yaml`.

## Testing Guidelines
Write PyTest cases under `tests/` mirroring the source tree; file names should match `test_<module>.py`. Use fixtures for Kodi API stubs and prefer `responses` or `pytest-httpx` over raw mocks. Target ≥85% branch coverage, measured via `pytest --cov=kodi_stream`. Include table-driven tests for playlist parsing edge cases and snapshot expected JSON payloads in `tests/fixtures/`.

## Commit & Pull Request Guidelines
Craft focused commits with imperative messages (`feat: add playlist poller`, `fix: guard idle timeout`). Reference related GitHub issues using `Fixes #ID` in the body. Every PR should describe the behavior change, manual verification steps, and include screenshots of Kodi UI changes if applicable. Rebase onto `main` before requesting review, ensure CI is green, and tag at least one maintainer familiar with the touched area.

## Configuration & Secrets
Store editable defaults in `configs/` and keep `.env` reserved for local secrets that are excluded via `.gitignore`. Never commit API keys or Kodi tokens; instead, document how to obtain them in `CONFIGURATION.md` and provide redacted examples in `assets/examples/`.
