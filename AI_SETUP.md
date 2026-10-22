# HomeRadio AI setup

## 1. Install
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b
```
Put `homeradio_ai.py` next to `radio.py`, and copy the sections from `radio_ai_example.toml` into `radio.toml`.

Optional voices (each runs as a small local server; if one is down, the station falls back to espeak-ng):
- **Kokoro**: Kokoro-FastAPI on port 8880. Runs fine on CPU.
- **Chatterbox**: chatterbox-tts-api on port 4123. Wants a GPU. It can clone a voice from a short sample.

## 2. Test each piece before wiring it in
```bash
python3 homeradio_ai.py break       # the DJ writes a sample break
python3 homeradio_ai.py voice       # writes test_voice.wav
python3 homeradio_ai.py facts --artist "Fleetwood Mac"
python3 homeradio_ai.py phone-in    # writes test_call.wav
python3 homeradio_ai.py similar     # shows songs boosted by your ratings
```

## 3. Hooks in radio.py
```python
from homeradio_ai import Station
ai = Station("radio.toml")
ai.set_library(all_song_paths)              # at startup and after each rescan

# picking songs: replaces random.choice over the shuffle pool
song = ai.choose_song(candidates)           # never returns a 1-star song

# when a song STARTS, write the break that follows it
ctx = {"kind": "top",                       # "top", "half", "quarter" or "song"
       "time": "7:00", "weather": forecast, "headline": headlines[0],
       "prev": {"title": t, "artist": a, "path": p},
       "next": {"title": t2, "artist": a2, "path": p2}}
if ai.wants_to_talk(ctx, channel):
    ai.prepare_break(key, ctx, channel)

# at the break: AI text if ready, otherwise your existing template
text, from_ai = ai.break_text(key, template_text, channel)
ai.speak(text, channel, "break.wav")

# phone-ins: check at a song boundary
if ai.phone_in_due():
    wav = ai.make_phone_in(ctx, channel, "call.wav")   # None means skip it

# after saving a star rating
ai.rating_changed()
```

## Notes
- **1-star songs** are never chosen again. Rating a song higher in the app lifts the ban.
- **Similar songs** use Plex sonic analysis when your server has it (a Plex Pass feature). Otherwise they use Plex's similar-artist list, and otherwise songs by the same artist.
- **The DJ** can only state facts found word-for-word on Wikipedia. Any break that names a song not in the break, or that has links or formatting, is thrown out and your template plays instead.
- **Headlines** are still read by your existing code. The DJ only mentions one if you pass it in.
