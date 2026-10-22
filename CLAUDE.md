# HomeRadio / WANG Radio

A self-hosted internet radio station that plays my own music collection like a real station
(time checks, weather, headlines, station IDs, an AI DJ), plus the WANG Radio Android app.

## Layout
- `radio.py` — the whole station: music library + rotation, breaks, TTS, MP3 streaming server
  (no Icecast), web player / station picker / settings pages, Plex rating sync, Cloudflare/Tailscale
  tunnel, ntfy notifications, Windows tray icon.
- `homeradio_ai.py` — optional AI module. radio.py imports it; if it's missing or fails, the station
  must run exactly as before. Entry point is its `Station` class.
- `radio.toml` — the real config (contains passwords/tokens; NOT committed). `radio.example.toml` is the template.
- `assets/` — next.mp3, back.mp3, record_scratch.mp3 sound effects.
- `android/` — WANG Radio Android app (Kotlin, Media3, Android Auto). Built by GitHub Actions
  (`.github/workflows/build-apk.yml`), not locally.

## Hard rules
- **Python standard library only** in radio.py and homeradio_ai.py (plus ffmpeg/ffprobe on PATH).
  No pip packages. Python 3.11+ (`tomllib`).
- **Runs on Windows** (`T:\sync\radio`, often with pythonw/no console). Test paths with both `\` and `/`;
  never assume espeak-ng exists; the default voice is Windows System.Speech via PowerShell.
- The station folder is inside a **sync tool's folder**: files can be briefly locked. Every state-file
  write must retry and never raise (see `save_json` in homeradio_ai.py). Nothing on a background
  thread may crash the playout loop.
- **Never cause dead air.** Anything slow (LLM, TTS servers, Plex, Wikipedia) runs in a background
  thread while the previous song plays; at the break, use it only if ready, otherwise skip the talk.
- **1-star songs never play again** (filtered in `Station.ensure_upcoming`, independent of
  `influence_rotation`). Rating 1 star also removes the song from "up next" and record-scratches it.
- Ratings live in `ratings.json` keyed by `track_id(path)` (sha1 of the absolute path), each entry
  has `path`, `stars`, `up`, `down`. Plex sync is one-way (station → Plex).
- **The DJ must not invent facts.** Music facts come only from `facts.json` (Wikipedia sentences,
  verified). Breaks that name songs not in the break, contain links/markdown, or repeat the
  notebook are rejected. Time, weather and headlines are read word-for-word by radio.py, not the LLM.

## AI module (homeradio_ai.py)
- Models are `"provider:model"` (e.g. `ollama:llama3.1:8b`, `lmstudio:<id>`). Providers in
  `[ai.providers.*]` with `type = "ollama" | "openai" | "anthropic"`; LM Studio uses
  `type = "openai"`, `url = "http://localhost:1234/v1"`, `json_mode = false`.
- Ollama is the default; hosted APIs are optional. All model timeouts default to 120 s.
- Voices: `engine = "default"` (radio.py's [tts]), `kokoro` (Kokoro-FastAPI :8880),
  `chatterbox` (:4123), `piper`, `command`. Failure falls back to the station's normal voice.
  Windows speech renders are serialized by a lock in `TTS.render` (parallel renders hang).
- Similar-song boost uses Plex (`/nearest` sonic analysis, then similar artists, then same artist);
  3★ boosts a little, 5★ a lot (`[ratings] similar_boost`). Plex paths are matched by
  album-folder + file name (`tail_key`), because Plex sees the files under a different root.
- Phone-ins: `[ai.phone_in] per_day / between_hours / min_gap_hours`, produced in the background.
- Self-tests: `python homeradio_ai.py break|voice|facts|phone-in|similar`.

## Web pages and settings
- Pages are HTML strings in radio.py (`PAGE`, `PICKER`, `SETTINGS`, shared `STYLE_HEAD`, `ICONS`).
- Design: "Parchment Brutalist" — parchment #F4EFE6, charcoal #111 3px borders, hard offset shadows
  (4px / 8px, no blur), 0 radius everywhere, ONE accent #FF8200 (play button, active stars, current
  station, changed settings), Fraunces serif (italic trailing words) + DM Sans bold uppercase
  labels with 0.2em tracking. Keep the Android app in the same design.
- `/settings` edits radio.toml line by line (`TomlFile`), keeping comments; every save is validated
  with tomllib before writing and backed up to `radio.toml.bak`. Changes apply after restart.
- Settings access (`settings_allowed`): home network only, never through the tunnel, unless
  `[stream] admin_password` is set (sent as `X-Admin-Password`). Secrets are never sent to clients.

## Android app (android/)
- Screens: `ChannelsActivity` (station list), `MainActivity` (player), `SettingsActivity`
  (connection + radio.toml settings via /settings.json), `RadioService` (Media3 + Android Auto).
- Can't be built locally here; push and let GitHub Actions build. Keep `@color/wang_red` (the app
  icon background uses it).
- Server API used by the app: `/channels.json`, `/c/<id>/status.json`, `/c/<id>/stream?noicy=1`,
  `/c/<id>/rate`, `/c/<id>/skip?dir=next|back`, `/settings.json`. Every request carries `?key=`.

## Testing
- Quick check: `python -m py_compile radio.py homeradio_ai.py`.
- For behaviour changes, run the station against a folder of short test MP3s with fake Ollama/Plex
  servers rather than the real library.
