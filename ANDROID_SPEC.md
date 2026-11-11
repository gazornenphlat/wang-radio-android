# WANG Radio Android - specification

Status as of 2026-11-02. Built by GitHub Actions only (no local Gradle here).

## Purpose
Listen to a HomeRadio station from a phone, rate songs, skip, pick channels, edit radio.toml
settings, and chat with the station. Android Auto through the same playback service.

## Design
- "Parchment Brutalist" (see CLAUDE.md). Light / Dark / Match Android, stored in SharedPreferences.
- Fonts bundled: Fraunces (italic trailing words), DM Sans (bold uppercase labels, 0.2em tracking).
- Accent #FF8200. No rounded corners. 3px charcoal borders, hard shadows.

## Screens
1. **ChannelsActivity** - station list ("All stations"), picks a channel, shows live listener count.
2. **MainActivity** - player. Now playing, progress bar with m:ss times, running order
   ("I. Previously", "II. Up next"), Skip up next, Previous / Next, 1-5 star rating, Tune in / Stop,
   and the **Station chat card** (below the running order).
3. **SettingsActivity** - connection (station address, key) and every radio.toml setting, grouped and
   searchable. Gated: offline, not set up, locked to home network, or admin password.
4. **RadioService** - Media3 playback, notification, Android Auto browse and controls.

## Behaviour
- Setup: the station address and key come from the "Open in WANG Radio" link on the station's
  notification, or are typed in Settings.
- Playback: `/c/<id>/stream?noicy=1`. "Tuning in" while buffering. "Can't reach the station" when offline.
- Ratings: 1 star = "never play again", the app skips the song. Rating a break shows "Nothing to rate".
- Next / Back: `/skip?dir=next|back`. **Required, not yet done:** when the channel's status says
  `allow_skip == false`, hide Skip, Previous and Next in the player and ignore them in the service.
  The server already refuses the request, so this is only about the controls.
- Chat card: polls `/c/<id>/chat.json` every 5 s while started; shows the newest 30 messages with their
  states (pending, read, skipped, blocked). Sends with `POST /c/<id>/chat` (name + text). Owner
  approval (Read it / Skip it) uses `POST /c/<id>/chat/approve` and is only offered to the owner.
  Hides itself when chat is off. Shows "Couldn't update that message" on failed sends.
  **Required, not yet done:** show a visible status line when the chat fails to load (today it hides
  silently), and make the card reachable without scrolling past the running order.
- Settings: `/settings.json` GET and POST. Saves are written to radio.toml on the station and take
  effect after restart; the app shows "Saved - restart the station to apply".

## Known gaps
- Chat card not confirmed visible on the device yet. Check the four files were copied and rebuilt.
- `allow_skip` controls hiding not implemented.
- Android Auto with `allow_skip = false` not checked.
- No instrumented or UI tests. Only CI compile.

## Acceptance checks (manual, on a phone)
1. Open from the notification link: address and key fill in; channel list loads.
2. Play, pause, play. Progress bar advances.
3. Rate 1 star: song is skipped and does not come back.
4. Channel with `allow_skip = false`: no Skip, Previous or Next visible.
5. Chat: send a message; it shows as pending; owner approves; it is read on air.
6. Settings: change one value, save, restart station, value applied.
7. Light, Dark and Match Android all readable.
