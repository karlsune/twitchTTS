# Contributing to Twitch TTS Engine

Thanks for considering a contribution! This is a small, focused project, so
keep changes tight and consistent with the existing style.

## Setup

1. Clone the repository.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # Linux/macOS
   pip install -r requirements.txt
   ```

3. Run the app: `python app.py` (or `start_tts.bat` on Windows).
   `config.json` is auto-created from `config.example.json` on first run —
   edit it with your Twitch channel name.

## Development notes

- The project deliberately stays small: `app.py` (server + IRC + SSE),
  `audio.py` (host playback queue), `emotes.py` (BTTV/FFZ/7TV emote names),
  `sanitize.py` (chat text cleaning). Don't split things up without a good
  reason.
- Python 3.10+, type hints used where they help, stdlib-first.
- There is no automated test suite yet; run the app and exercise the feature
  you touched (web UI at `http://localhost:8080/index.html`).
- Keep `config.json` out of commits — it holds personal settings. Change
  shared defaults in `config.example.json` instead.

## Pull requests

1. Fork the repository and create a branch: `git checkout -b fix/your-fix`.
2. Make your change, keeping the diff minimal.
3. Update `CHANGELOG.md` under an "Unreleased" heading.
4. Open a PR against `master` and describe what changed and why.
5. Sign off your commits (Developer Certificate of Origin): append
   `Signed-off-by: Your Name <you@example.com>` to each commit message
   (`git commit -s`). This certifies you wrote the change or have the
   right to contribute it under the project's MIT license. Contributions
   are licensed to the project under the MIT License.
6. Be patient — this is a hobby project, reviews may take a while.

## Reporting issues

Include: what you did, what you expected, what happened instead, your OS and
Python version, and any relevant log output.
