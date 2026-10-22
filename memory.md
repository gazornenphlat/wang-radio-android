# Project memory — HomeRadio / WANG Radio

What has been built, what was decided and why, and what's still open.
CLAUDE.md holds the rules; this file holds the history. Add to the log as things change.

## Owner preferences
- AI: **Ollama first**, local only by default. Hosted APIs (Anthropic, OpenAI-compatible) are optional
  extras, never the default. LM Studio is also used (OpenAI-compatible provider, `json_mode = false`).
- Not interested in Spotify or YouTube integration.
- 1-star rated songs must **never** play again.
- Similar-song weighting from ratings + Plex: 3★ a little more, 5★ a lot more.
- Wants Kokoro and Chatterbox as voice options, and fake phone-in segments with a configurable frequency.
- Every Ollama/model call gets a **2-minute** timeout.
- Web player and Android app share one design: "Parchment Brutalist" (see CLAUDE.md).
- Plain, step-by-step instructions; runs everything on a Windows PC.

## Environment
- Station runs on Windows from `T:\sync\radio` (a synced folder — files can be locked briefly).
- Python 3.14 (pythoncore-3.14-64), ffmpeg on PATH, default voice = Windows System.Speech.
- Kokoro runs in Docker: `docker run -d --name kokoro --restart unless-stopped -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu`.
  Test page: http://localhost:8880/web, voice list: http://localhost:8880/v1/audio/voices.
- Plex is connected (one-way rating sync, similar-song lookups).
- Android APK is built by GitHub Actions in the private `wang-radio` repo; nothing Android is built locally.
- Public access through a Cloudflare tunnel; ntfy notification carries the "Open in WANG Radio" setup link.

## Log
### 2026-10-18 — AI features (inspired by robert-dean/deadair)
- Reviewed deadair; took the ideas, not the architecture (it's Node/Postgres/Liquidsoap).
- Built `homeradio_ai.py` (stdlib only): AI DJ breaks with fallback, personas with a notebook of
  recent lines, Wikipedia-verified facts (`facts.json`), ratings + Plex similarity rotation,
  voices (Kokoro/Chatterbox/Piper/command), pronunciations, phone-ins.

### 2026-10-19 — Wired into radio.py, first live runs
- Hooks in radio.py: weighted song choice, 1-star ban, DJ talk prepared in a background thread
  while the previous song plays (`prepare_talk` / `air_talk`), DJ voice for time/weather/news,
  rating changes notify the AI module.
- Duplicate `[ratings]` table after pasting the example config → merged by hand.
- Fact lookups timed out (Ollama busy with DJ writing) → facts wait for on-air jobs, longer timeout,
  failures are retried instead of cached as "no facts". Advised deleting old `facts.json`.
- All model timeouts raised to 120 s (`timeout_seconds`, `background_timeout_seconds`).
- Windows speech timed out after 60 s → TTS renders serialized with a lock. Kokoro wasn't being
  used at the time (fallback voice was speaking) — cause not yet confirmed.

### 2026-10-21 — Design + settings
- Web player and station picker restyled to the Parchment Brutalist design (accent #FF8200 from
  the design tokens, not the "chartreuse" in its prose).
- New `/settings` page editing every radio.toml value in place (comments kept, validated, backup).
  Locked to the home network unless `[stream] admin_password` is set.
- Android app 2.0: same design (fonts bundled), new SettingsActivity (connection + all radio.toml
  settings). First CI build failed: launcher icon referenced removed `@color/wang_red` → restored.
- LM Studio support: `[ai.providers.lmstudio] type="openai" url="http://localhost:1234/v1" json_mode=false`.
- `notebook_default.json` save failed with WinError 5 (sync lock) and crashed a break →
  `save_json` now retries, falls back to a direct write, never raises; `aired()` can't break a break.
  Option: `[ai] data_dir = "C:/ProgramData/HomeRadio"` keeps AI state out of the synced folder.

### 2026-10-22 — Claude Code
- Created CLAUDE.md, a gitignore file and this file. The gitignore was written as `gitignore`, without the
  leading dot, so git never picked it up until it was renamed to `.gitignore` on 10-22.

### 2026-10-22 — Repo go-live and file cleanup (OpenCode)
- Installed GitHub CLI (`winget install GitHub.cli`, 2.103.0), logged in with a device code as
  `gazornenphlat`; token is in the Windows keyring, so pushes no longer need a password.
- Created the private repo `gazornenphlat/wang-radio` and pushed `main` (initial commit `cf3dbdc`,
  152 files). `origin` is HTTPS and `main` tracks it — later work is just `git push`.
- Git refused to work on the `T:` SMB share ("dubious ownership") → added `safe.directory` entries for
  `//10.120.1.147/t-tv7/sync/radio` and `T:/`. An older malformed `sync/MOVIES/yt-dlp` entry already in the
  global config prints a harmless warning on every git command.
- The gitignore file was named `gitignore` (no dot), so git had never seen it → renamed to `.gitignore` and
  extended: `radio*.toml` except `radio_ai_example.toml`, `public_url.txt`, `*.zip`, `*.apk`, `*.bak`,
  `*.log`, `.sync/`, and Android build output.
- **Secret found:** `radio - Copy.toml` contained a real Plex token in plain text → kept out of the repo
  (file still on disk; see open items). Grepped the rest of the tracked files first — no hardcoded keys.
- RadioLogos renamed to `RadioLogos1`–`RadioLogos5` (alphabetical by old name, original extensions kept so
  the PNG/JPEG content type still matches) — commit `d66186d`. The fallback `logo` path in radio.toml,
  radio.toml.bak and the copy now points at `RadioLogos5.jpg` (old `Untitled.jpg`); rewritten as UTF-8
  without BOM and re-parsed with `tomllib` to prove it's still valid.
- RadioIDs renamed to `RadioIDs01`–`RadioIDs23` — commit `e57381d`. Numbered chronologically by mtime, not
  alphabetically, because string order would put `2026-10-5` after `2026-10-17`. Two-digit padding, so it
  caps at 99. Nothing needs updating: `radio.py` lists the `station_ids` folder at runtime.
- Deleted `WANG-Radio-Android/wang-radio-android` — commit `86f6d14`. It was byte-identical to `android/`
  (55 files, same MD5s, nothing unique), verified before deleting; recoverable from commit `e57381d`.
- The random logo picker and ID picker re-read their folders, so renames take effect on the next pick; a
  station restart is the clean way to be sure.

## Open items / to verify
- Confirm the Android 2.0 build passes in GitHub Actions after the `wang_red` fix (nothing else
  was compile-checked; the build stopped at the first error).
- Confirm Kokoro is actually the DJ voice (look for `AI: voice 'kokoro' failed (...)` in radio.log;
  check the persona's `voice = { engine = "kokoro", voice = "af_bella" }`).
- Check `ollama ps` shows GPU, not CPU, if DJ breaks are often skipped.
- Settings changes need a station restart; a "restart station" button was considered but not built.
- Consider moving AI state files out of `T:\sync` with `[ai] data_dir`.
- The APK workflow is still at `android/.github/workflows/build-apk.yml`; GitHub ignores workflows that are
  not at the repo root, so no build runs until it moves to `.github/workflows/`.
- `memory.md` is untracked — decide whether it belongs in version control.
- Rotate the Plex token that sat in plain text in `radio - Copy.toml`.
