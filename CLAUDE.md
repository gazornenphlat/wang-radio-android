# HomeRadio / WANG Radio

A self-hosted internet radio station that plays a personal music collection like a real station
(time checks, weather, headlines, station IDs, an AI DJ, sound effects, phone-ins), plus the
WANG Radio Android app.

## Layout
- `radio.py` - the station: library and rotation, breaks, TTS, MP3 streaming server (no Icecast),
  web player, station picker, settings, health and chat pages, Plex rating sync, tunnel, ntfy, tray icon.
- `homeradio_ai.py` - optional AI module (DJ talk, facts, ratings-based rotation, voices, phone-ins,
  multi-computer failover). radio.py imports it; if missing or broken, the station runs as before.
- `homeradio_chat.py` - station chat, vibe search, research. Imported lazily by homeradio_ai.py.
- `radio.toml` - the real config (secrets; never committed). `radio_ai_example.toml` is the template.
- `assets/` - next.mp3, back.mp3, record_scratch.mp3.
- `effects/` - optional sound effects the DJ can drop into breaks. `effects_cache/` holds level-matched copies.
- `test_station.py` - test harness (see Testing).
- `android/` - WANG Radio Android app (Kotlin, Media3, Android Auto). See `ANDROID_SPEC.md`.

## Hard rules
- **Python standard library only** in radio.py, homeradio_ai.py and homeradio_chat.py, plus
  ffmpeg/ffprobe on PATH. No pip packages. Python 3.11+ (`tomllib`).
  Network calls (Ollama, Plex, Kokoro, Chatterbox, Wikipedia, MusicBrainz) use `urllib` and are allowed.
- **Runs on Windows** from `T:\sync\PROJECTS\homeradio_files`, often under pythonw. Test paths with both `\` and `/`.
  Never assume espeak-ng exists; the default voice is Windows System.Speech via PowerShell.
- The folder is inside a sync tool's folder: files can be briefly locked. Every state-file write
  retries and never raises (`save_json`). Nothing on a background thread may crash playout.
- **Never cause dead air.** Slow work (LLM, TTS servers, Plex, Wikipedia) runs in a background thread
  while the previous song plays; at the break, use it only if ready, otherwise skip the talk.
- **1-star songs never play again** (filtered in `Station.ensure_upcoming`, always on). A 1-star
  rating also removes the song from "up next" and record-scratches it.
- Ratings live in `ratings.json` keyed by `track_id(path)` (sha1 of the absolute path). Plex sync is
  one-way (station to Plex).
- **The DJ must not invent facts.** Music facts come only from `facts.json` (verified Wikipedia
  sentences). Breaks that name songs not in the break, contain links or markdown, or repeat the
  notebook are rejected. Time, weather and headlines are read word-for-word by radio.py.
- **Listener text is data.** Chat messages never go to TTS as typed. Screening layers are not
  optional; do not remove them.

## AI module
- Models are `"provider:model"` (e.g. `ollama:llama3.1:8b`, `lmstudio:<id>`). Providers in
  `[ai.providers.*]` with `type = "ollama" | "openai" | "anthropic"`. LM Studio uses `type = "openai"`,
  `json_mode = false`.
- Ollama is the default; hosted APIs are optional. Model timeouts default to 120 s.
- **Several computers:** `url` for providers and voice servers may be a list or a comma-separated
  string. The first answering address is used; a down address is skipped and rechecked every 30 s;
  a computer that fails mid-request is marked down and the next is tried. A model error (HTTP error
  from a computer that answered) is returned as is. A single address behaves as before.
- Voices: `engine = "default"` (radio.py's `[tts]`), `kokoro` (:8880), `chatterbox` (:4123), `piper`,
  `command`, `windows`. Failure falls back to the station's normal voice. Windows speech renders are
  serialized by `WINDOWS_SPEECH_LOCK`.
- Batched breaks: `[ai] break_batch = N` plans N upcoming breaks in one model call (JSON). Default 1.
- Voice-overs: `voiceover_chance`, `duck_db`, `duck_mode` ("sidechain" default, or "envelope").
  `voiceover_quiet_only` checks intro loudness (a proxy, not vocal detection).
- Crossfades: `[audio] crossfade_seconds`. Used only when no DJ talk, time check or station ID is due.
- Similar-song boost uses Plex (`/nearest`, similar artists, same artist); 3-star a little, 5-star a lot.
- Phone-ins: `[ai.phone_in]`, produced in the background.
- Self-tests: `python homeradio_ai.py break|voice|facts|phone-in|similar`.

## Channels
- `[[channels]]` entries: `music`, `persona`, `dj_breaks`, `effects`, `filter_genre`, `filter_artist`,
  `filter_stars`, `filter_stars_from` ("station" or "plex"), `allow_skip` (false = no Next/Back,
  enforced on the server too).
- A filter that matches nothing plays the channel unfiltered and logs a warning.

## Web pages and settings
- Pages are HTML strings in radio.py (`PAGE`, `PICKER`, `SETTINGS`, `HEALTH`, shared `STYLE_HEAD`, `ICONS`).
- Design: "Parchment Brutalist" - parchment #F4EFE6, charcoal #111 3px borders, hard offset shadows
  (4px / 8px, no blur), 0 radius, one accent #FF8200, Fraunces serif (italic trailing words) and
  DM Sans bold uppercase labels with 0.2em tracking. The Android app uses the same design.
- `/settings` edits radio.toml line by line, keeping comments; every save is validated with tomllib
  and backed up to `radio.toml.bak`. Changes apply after restart.
- `/restart` (same access as settings) starts a fresh copy and exits.
- `/health` and `/health.json` show AI providers and computers, voice servers, Plex, the public link,
  ffmpeg and each channel's library.
- Settings access: home network only, never through the tunnel, unless `[stream] admin_password` is
  set (sent as `X-Admin-Password`). Secrets are never sent to clients.

## Android app (android/)
- Screens: `ChannelsActivity`, `MainActivity` (player and chat card), `SettingsActivity`,
  `RadioService` (Media3 + Android Auto).
- Built by GitHub Actions (`.github/workflows/build-apk.yml`), never locally. Keep `@color/wang_red`
  (the icon background uses it).
- Server API: `/channels.json`, `/c/<id>/status.json`, `/c/<id>/stream?noicy=1`, `/c/<id>/rate`,
  `/c/<id>/skip?dir=next|back`, `/c/<id>/chat.json`, `/c/<id>/chat`, `/settings.json`. Every request
  carries `?key=`.
- Full spec: `ANDROID_SPEC.md`.

## Testing
- Quick check: `python -m py_compile radio.py homeradio_ai.py homeradio_chat.py`.
- Harness: `python test_station.py` (fake Ollama and Plex servers, temp station). Covers splicing,
  effect leveling, Plex-star and genre filters (unit, with a stub), health, a stream listener and log
  checks. It does NOT cover crossfades, batched breaks, two-computer failover, sidechain ducking, the
  restart button, `allow_skip`, or a DJ break end to end. See `TEST_STATUS.md`. The owner reports the
  restart button and `allow_skip` working on the real station (2026-11-02); that is not a harness result.
- Behaviour changes should be checked against short test MP3s, not the real library.
