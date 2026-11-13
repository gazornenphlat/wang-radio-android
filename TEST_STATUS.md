# Test status - what has and hasn't been verified

Last updated 2026-11-12. "Live" means run against the real station or real hardware.

| Feature | How it was checked | Live? |
|---|---|---|
| Sound effects splice, leveling | Unit (harness) | No |
| Plex-star filter | Unit with stub Plex object (harness) | No |
| Genre filter (case-insensitive) | Unit (harness) | No |
| Health page, stream, status, log | Integration (harness, fake Ollama and Plex) | No |
| DJ break end to end | Not in harness | No |
| Batched breaks (`break_batch`) | Stub test in session: JSON parse, cache, stale rejection. Harness: station runs with `break_batch = 2` without errors (does not check the batch content) | No |
| Crossfades (`crossfade_seconds`) | Stub test in session: 14.0 s total, fade shape. Harness: station runs with `crossfade_seconds = 1` without errors (does not check that a fade happened) | No |
| Similar-song picks | Removed 2026-11-06 (Plex index kept for ratings filter) | n/a |
| Sidechain ducking | Tone test only: 220 Hz measured under a voice, about -12 dB | No |
| Vocal-intro check | Loudness proxy only; not vocal detection | No |
| Two-computer failover | Session test: dead address first, live one answers; down marked | No (no two real hosts) |
| `allow_skip = false` | Harness: status reports it, server refuses next/back (403), normal channel still skips. Web hides buttons; Android hides controls (merged, build passed). Owner reports working live 2026-11-02 | Partly (owner report) |
| Plex health check and auto-restart (`PlexWatch`) | Harness: 5 watchdog scenarios (3-strike restart, reconnect, bad token never restarts, good answer resets count, cooldown), service call stubbed; harness: Plex health row up | No (`net stop`/`start` not run on Windows) |
| Tunnel restarts (cloudflared) | Fake cloudflared: link announced once over 10 restarts, last output logged, backoff grows. Real cloudflared not run | No |
| Restart button (`/restart`) | Harness: refused with no or wrong admin password (403), station keeps running. Successful restart not tested (it would stop the test station). Owner reports working live 2026-11-02 | Partly (owner report) |
| Android `allow_skip` hiding | Merged to `main` (PR #3); build passed | CI only |
| Android repo layout | Merged PR #4 (one Android project, server files at root); build passed | CI only |
| Android chat | CI compile only, not confirmed | No |

## To do before calling these verified
- Run the harness after every change: `python test_station.py`.
- Harness cases for batched break, crossfade, `allow_skip`, `/restart` and the Plex watchdog are in (31 of 31 passing). Still to do: a check that a crossfade actually happens and that a batch is used; a successful `/restart` on a throwaway copy.
- Android: `allow_skip` hiding is merged and built; test on a phone.
- Live: Plex-rating filter on the real library; sidechain on a
  real song with a real DJ line; two computers with Ollama on each.
