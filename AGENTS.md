# Repository Guidelines

## Project Structure & Module Organization
- `app.py` holds the Flask backend, API proxy routes, comparison logic, and session file handling.
- `templates/index.html` is the single-page UI with embedded CSS/JS (no frontend framework).
- `static/` stores static assets such as `static/favicon.svg`.
- `uploads/` is runtime storage for generated Excel files (auto-created, session cleanup).
- `.env.example` documents required configuration; `.env` stays local.

## Build, Test, and Development Commands
- `python -m venv venv` creates a local virtual environment.
- `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (macOS/Linux) activates it.
- `pip install -r requirements.txt` installs dependencies.
- `python app.py` runs the Flask server at `http://localhost:5000`.

## Coding Style & Naming Conventions
- Python follows PEP 8 with clear, descriptive function names (snake_case).
- JavaScript uses ES6+ with semicolons and camelCase for variables/functions.
- CSS follows a BEM-like component naming pattern in `templates/index.html`.
- Prefer small, focused helpers (e.g., `normalize_value`, `fix_encoding`) and keep routes grouped by domain.

## Testing Guidelines
- There is no automated test suite yet; validate changes manually in the UI and API flows.
- When adding tests, align naming with standard `test_*.py` patterns and document how to run them.

## Commit & Pull Request Guidelines
- Commit messages in history are short, imperative, and capitalized (e.g., "Fix JS syntax after modal cleanup").
- PRs should include: a concise summary, testing notes (manual steps or commands), and UI screenshots for visible changes.
- Link related issues when applicable.

## Architecture Overview
- Single-page UI in `templates/index.html` calls Flask JSON endpoints for SentinelOne/NinjaRMM data.
- Backend normalizes and compares device names, then returns set diffs and stats to the UI.
- External API credentials live server-side only; the frontend never calls third-party APIs directly.

## Security & Configuration Tips
- Never commit `.env` or real credentials. Use `.env.example` as the template.
- Configure `FLASK_SECRET_KEY` for non-debug deployments and prefer Basic Auth for LAN access.
