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
- Station runs on Windows from `T:\sync\PROJECTS\homeradio_files` (a synced folder — files can be locked briefly).
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
- Created CLAUDE.md, .gitignore (keeps radio.toml and state files out of git) and this file.

### 2026-10-26 — Listening-only playout, rotating DJs, faster skips, app dark mode
- Station plays only while someone is listening (`[stream] play_only_when_listening = true`,
  `stop_after_seconds = 15`); comes back on air with a station ID when someone tunes in.
- Channel `persona = "random"` (or a list) rotates DJs per break; each persona's jokes come from
  `jokes_file`, else `jokes_<persona>.txt`, else `jokes.txt`.
- Slow Next was client buffering (4 s connect burst + drift up to 30 s). Fix: station clears the
  burst on skip and clients reconnect ~0.4 s later. `[controls] skip_sounds = false` drops the
  next/back sound effect. `[stream] burst_seconds` is configurable.
- New `POST /c/<id>/skip?dir=upnext` swaps the up-next song (web + app "Skip" button).
- Android 2.1: Light / Dark / Match Android setting (values-night palette, `WangApp` applies it).

### 2026-11-01 — Play time, gendered callers, voice levels, voice-overs
- status.json gives `duration`/`elapsed`; queue entries have `duration`. Web + app show a progress
  bar and m:ss times.
- Phone-in callers get a gendered name (CALLER_NAMES female/male) and a voice from
  `[ai.voices] female = [...] / male = [...]` (else Kokoro af_/am_ names, else Windows Zira/David).
- Voices were much quieter than music: each speech clip is now measured and set to
  `[audio] voice_lufs` (default -15 when songs are normalized to -16, -11 when not) with
  two-pass linear loudnorm (`voice_filter`). `voice_boost_db` trims it.
- Voice-over breaks: `[ai] voiceover_chance` (0.3); DJ talks over the next song's start, music ducked
  `duck_db` (-12) with smooth ramps; songs shorter than talk + 8 s just play normally.
- Windows speech lock is shared between radio.py and homeradio_ai.py (`WINDOWS_SPEECH_LOCK`).

### 2026-11-01 (later) - Station chat, vibe requests, research
- New third module `homeradio_chat.py` (imported lazily by homeradio_ai.py's Station, because it
  imports helpers back from it). Missing/broken -> station and DJ unaffected.
- Chat: `/chat.json` (GET), `/chat` (POST), `/chat/approve` (POST, owner only via settings_allowed),
  `/notes.json`. Safety is layered: rate limit -> deterministic prefilter (slurs, sexual, self-harm,
  prompt injection, "say exactly", links, phone/email, repetition) -> model screen returning strict
  JSON -> the model's own gist re-run through the prefilter -> DJ writes the line -> clean_speech +
  DJ check(). Listener text is NEVER sent to TTS as typed. 5 blocked messages = 30 min quiet-list.
  Everything lands in chat_log.csv.
- Vibe requests: VibeIndex embeds every track (Ollama /api/embed, nomic-embed-text), int8-quantised
  and truncated to `vibe_dims`, stored in vibes.jsonl. Scores are scaled to the best match and
  anything under 0.6 is dropped - asking for neon must not return bluegrass. Falls back to word
  matching with no embedding model. Station.request_track() puts the winner next.
- Research: Wikipedia (artist + album + song) and MusicBrainz (structured, 1 req/s). Every claim
  keeps its source sentence and URL; supported() rejects any number or name not in the source.
  Per-host cooldowns instead of threading.Timer (a non-daemon Timer held the process open for 10 min).
  Nothing reachable -> nothing cached; half an answer caches for an hour, not 90 days.
- ShowNotes writes trivia/bio/transition ahead of time -> notes.json, dashboard panel, social_feed.jsonl.
- A shoutout the DJ can't use is retried twice then quietly dropped, so one message can't jam the queue.
- Android app does NOT have the chat yet.

### 2026-11-01 (later) — DJ breaks toggle, sound effects, filtered channels
- Per-channel `dj_breaks = false` silences all DJ talk, phone-ins and voiceovers for that channel.
  Time checks, weather, news and station IDs still play.
- Sound effects: `effects` folder path on a channel; `effects_per_hour` global rate limit;
  per-persona `effects_chance` (0-1). Effects are spliced INTO the DJ break: the talk is split at commas/sentences and a random clip goes between parts (up to 2 per break), loudness-matched to the voice (ffmpeg concat). A clip on its own plays between songs only when there is no talk.
- Channel filters: `filter_genre`, `filter_artist`, `filter_stars` on a [[channels]] entry. Genre and
  artist match against the track's ffprobe metadata tags (case-insensitive substring). Stars use the
  station's own ratings (not Plex ratings). All three can combine.

### 2026-11-01 (later still) - Restart, health, effect levels, Plex filters, test harness, Android chat
- Settings page: "Restart station" button. POST /restart (same access as saving settings) starts a
  fresh copy of radio.py and exits; the new copy waits 4 s and retries the web port for up to 20 s.
- Health page: GET /health (HTML, refreshes every 30 s) and /health.json. Rows for AI providers (Ollama
  checked via /api/tags; hosted APIs not checked), Kokoro and Chatterbox (only if used), Plex, the public
  link, ffmpeg, and each channel's music library. Probes count any HTTP answer as up.
- Effects are level-matched: each clip is converted once to a voice-level WAV in `effects_cache/`
  (single-pass loudnorm; originals untouched). Used both inside DJ breaks and on their own.
- Channel filter `filter_stars_from = "plex"` reads Plex ratings (out of 10, halved to stars) from the
  AI module's Plex index. If a filter matches nothing, the channel plays unfiltered and logs a warning.
- Test harness `test_station.py` (stdlib + ffmpeg): builds a temp station with fake Ollama and Plex
  servers, runs radio.py, checks health, stream audio and the log. Plus unit checks for splicing,
  leveling and filters. Live run does not yet exercise a DJ break (only the standalone effect).
- Android 2.2 chat: Station.kt (chat/sendChat/approveChat), MainActivity chat card polling every 5 s
  while started, layout chat_box below the running order, strings. Not yet compiled in CI.

### 2026-11-01 (latest) - Breaks, ducking, skips, crossfades, several computers
- Break batching: `[ai] break_batch` (1 = unchanged). One model call plans the next N breaks as JSON;
  each is checked by the same rules and cached per persona + next song. Stale ones are rejected.
- Sidechain ducking (`[audio] duck_mode = "sidechain"`, default) follows the voice; envelope kept.
- Vocal-intro check: `[ai] voiceover_quiet_only` + `voiceover_max_intro_db`. NOTE: this is a loudness
  proxy (mean dB of the first seconds), not real vocal detection; off by default.
- Crossfades: `[audio] crossfade_seconds`. Used only when no DJ talk, time check or station ID is due.
- `allow_skip = false` per channel: server refuses next/back and `/skip`; web hides the buttons.
- Several computers: `url` for `[ai.providers.*]` and `[ai.voices.*]` may be a list or comma-separated.
  First answering address wins; a down one is skipped and rechecked every 30 s; a computer failing
  mid-request is marked down and the next is tried. Health page lists each computer.
- Pending: `allow_skip` not yet hidden in the Android app. Android chat still not visible: the four
  changed Android files must be copied and rebuilt; chat card sits below the running order.

## Open items / to verify
- Confirm the Android chat build passes in GitHub Actions (first build with the chat card).
- Live tests not yet done: crossfade, batched breaks, two-computer failover (real hosts), sidechain on real music.
- Test the Plex-rating filter on the real station.
- Harness: no live DJ-break test yet (short run); the splice path is only unit-tested.
- Confirm the Android 2.0 build passes in GitHub Actions after the `wang_red` fix (nothing else
  was compile-checked; the build stopped at the first error).
- Confirm Kokoro is actually the DJ voice (look for `AI: voice 'kokoro' failed (...)` in radio.log;
  check the persona's `voice = { engine = "kokoro", voice = "af_bella" }`).
- Check `ollama ps` shows GPU, not CPU, if DJ breaks are often skipped.
- Restart button built 11-01; owner reports it working on the real station 2026-11-02 (no harness case).
- Consider moving AI state files out of `T:\sync` with `[ai] data_dir`.
