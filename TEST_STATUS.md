# Test status - what has and hasn't been verified

Last updated 2026-11-02. "Live" means run against the real station or real hardware.

| Feature | How it was checked | Live? |
|---|---|---|
| Sound effects splice, leveling | Unit (harness) | No |
| Plex-star filter | Unit with stub Plex object (harness) | No |
| Genre filter (case-insensitive) | Unit (harness) | No |
| Health page, stream, status, log | Integration (harness, fake Ollama and Plex) | No |
| DJ break end to end | Not in harness | No |
| Batched breaks (`break_batch`) | Stub test in session: JSON parse, cache, stale rejection | No |
| Crossfades (`crossfade_seconds`) | Stub test in session: 14.0 s total, fade shape | No |
| Similar-song picks | Removed 2026-11-06 (Plex index kept for ratings filter) | n/a |
| Sidechain ducking | Tone test only: 220 Hz measured under a voice, about -12 dB | No |
| Vocal-intro check | Loudness proxy only; not vocal detection | No |
| Two-computer failover | Session test: dead address first, live one answers; down marked | No (no two real hosts) |
| `allow_skip = false` | Server refuses next/back and web hides buttons; owner reports working live 2026-11-02; not in harness | Owner report only |
| Plex health check and auto-restart (`PlexWatch`) | Unit: 12 checks with a fake Plex server (401 vs down, 3-strike restart, cooldown, reconnect); harness: Plex health row up | No (`net stop`/`start` not run on Windows) |
| Tunnel restarts (cloudflared) | Fake cloudflared: link announced once over 10 restarts, last output logged, backoff grows. Real cloudflared not run | No |
| Restart button (`/restart`) | Owner reports working live 2026-11-02; not in harness | Owner report only |
| Android chat | CI compile only, not confirmed | No |

## To do before calling these verified
- Run the harness after every change: `python test_station.py`.
- Add harness cases for: a batched break, a crossfade, `allow_skip` (server refuses), `/restart`.
- Android: `allow_skip` hiding is NOT in the WANG-Radio-Android.zip (Kotlin files unchanged); still to do.
- Live: Plex-rating filter on the real library; sidechain on a
  real song with a real DJ line; two computers with Ollama on each.
