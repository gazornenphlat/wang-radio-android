# HomeRadio / WANG Radio - knowledge base

Status as of 2026-11-02. Start here.

## What it is
A self-hosted radio station (`radio.py`, Python standard library only, Windows) that plays a personal
music library with time checks, weather, headlines, station IDs, an AI DJ, sound effects, phone-ins
and crossfades. Plus the WANG Radio Android app (Kotlin, Media3).

## Key files (in the zip / repo)
- `radio.py`, `homeradio_ai.py`, `homeradio_chat.py` - station, AI module, chat/research module
- `radio_ai_example.toml` - config template (copy the sections into `radio.toml`)
- `test_station.py` - test harness
- `CLAUDE.md` - project rules (read before changing code)
- `ANDROID_SPEC.md` - Android app specification
- `TEST_STATUS.md` - what is verified and what is not
- `.gitignore` - keeps secrets, runtime state and Android build output out of git

## Current feature set
- AI DJ breaks, batched planning (`break_batch`), voice-overs with ducking (sidechain by default)
- Crossfades (`crossfade_seconds`), used only when no talk, time check or station ID is due
- Per-channel: `dj_breaks`, `effects`, `filter_genre`, `filter_artist`, `filter_stars` (station or Plex
  ratings), `allow_skip = false`
- Several computers for AI and voice servers: `url` as a list or comma-separated string; first
  answering address wins; down ones rechecked every 30 s
- Restart button, health page, settings page (edits radio.toml with validation and backup)
- Station chat with layered screening; owner approval optional

## Verification status (short form; full table in TEST_STATUS.md)
- Verified by the harness (fake servers): effects splice and leveling, genre filter, health, stream, log.
- Unit or stub only: Plex-star filter, batched breaks, crossfades, two-computer failover, sidechain.
- Restart button and `allow_skip` server path: owner reports them working on the real station (2026-11-02); no harness case.
- Not verified: DJ break end to end, Android chat on a device, Android `allow_skip` controls (not in the zip).
- Nothing has been run on the real library or with two real computers yet.

## Rules that matter
- Standard library only. Network calls to Ollama, Plex, Kokoro and others are fine.
- Never cause dead air. 1-star songs never play again. The DJ must not invent facts.
- Listener chat text is data, never spoken as typed. Screening stays on.

## Open work
1. Android: hide Next/Back/Skip when `allow_skip` is false; show a status line when chat fails to load;
   confirm the chat card is visible after rebuild.
2. Add harness cases for batched breaks, crossfades, `allow_skip` and `/restart`.
3. Live checks: Plex-rating filter, sidechain on real music, two real Ollama computers.
4. Vocal-intro check is a loudness proxy; real vocal detection is not planned (stdlib only).
