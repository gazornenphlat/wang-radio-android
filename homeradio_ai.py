"""
homeradio_ai.py - AI features for HomeRadio (standard library only).

What is in here
  1. AI DJ talk breaks (Ollama by default, hosted APIs optional) with a
     template fallback so a slow or broken model never causes dead air.
  2. Personas per channel, each with a notebook of recent lines.
  3. Verified music facts (Wikipedia), cached in facts.json. The DJ may only
     state facts whose source sentence was found word-for-word.
  4. Ratings: 1-star songs are never played again; 3/4/5-star songs make
     similar songs (via Plex) more likely; the DJ can mention favorites.
  5. Voices: piper, kokoro, chatterbox, espeak-ng, or any command.
  6. Pronunciation fixes applied before text-to-speech.
  7. Fake phone-in segments on a configurable schedule.

radio.py talks to this module through the Station class at the bottom.
Run `python3 homeradio_ai.py --help` for self-test commands.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, date

USER_AGENT = "HomeRadio/1.0 (self-hosted internet radio)"


_log = logging.getLogger("homeradio.ai")


def log(msg):
    _log.info("AI: %s", msg)


# --------------------------------------------------------------------------
# small file helpers
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    """Saves a small state file. Never raises: a sync tool (Syncthing, Dropbox, OneDrive)
    or antivirus can briefly lock the file on Windows, so retry, then write in place,
    and if even that fails, skip this save (the next one will catch up)."""
    text = json.dumps(data, indent=1, ensure_ascii=False)
    tmp = path + ".tmp"
    for attempt in range(6):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(0.25 * (attempt + 1))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError as e:
        log(f"couldn't save {os.path.basename(path)} ({e}); will try again next time")
        return False


def http_json(url, body=None, headers=None, timeout=30):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def norm_path(p):
    return str(p).replace("\\", "/").rstrip("/").lower()


def tail_key(p):
    """Album folder + file name. Matches a song even when Plex sees the music
    under a different root (another drive letter, a NAS share, a container)."""
    return "/".join(norm_path(p).split("/")[-2:])


# --------------------------------------------------------------------------
# 1. Model client: Ollama first, hosted APIs optional
# --------------------------------------------------------------------------

class Models:
    """Model names look like "provider:model", e.g. "ollama:llama3.1:8b".
    Only the first colon separates the provider name."""

    def __init__(self, cfg):
        self.providers = cfg.get("providers", {}) or {
            "ollama": {"type": "ollama", "url": "http://localhost:11434"}}
        self.jobs = cfg.get("models", {})
        self.timeout = float(cfg.get("timeout_seconds", 120))
        # background work (facts) isn't on a deadline, so it may take longer
        self.background_timeout = float(cfg.get("background_timeout_seconds", 120))
        self.on_air_busy = 0           # DJ/phone-in requests in flight; background jobs wait
        self.busy_lock = threading.Lock()

    def model_for(self, job):
        return self.jobs.get(job) or self.jobs.get("talk") or "ollama:llama3.1:8b"

    def ask(self, job, system, user, want_json=False, temperature=0.8):
        background = job == "facts"
        if not background:
            with self.busy_lock:
                self.on_air_busy += 1
        try:
            return self._ask(job, system, user, want_json, temperature,
                             self.background_timeout if background else self.timeout)
        finally:
            if not background:
                with self.busy_lock:
                    self.on_air_busy -= 1

    def _ask(self, job, system, user, want_json, temperature, timeout):
        spec = self.model_for(job)
        prov_name, _, model = spec.partition(":")
        prov = self.providers.get(prov_name)
        if not prov:
            raise RuntimeError(f"no provider named '{prov_name}' in [ai.providers]")
        kind = prov.get("type", "ollama")
        key = prov.get("api_key") or os.environ.get(prov.get("api_key_env", ""), "")
        if kind == "ollama":
            body = {"model": model, "stream": False,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "options": {"temperature": temperature}}
            if want_json:
                body["format"] = "json"
            url = prov.get("url", "http://localhost:11434").rstrip("/") + "/api/chat"
            return http_json(url, body, timeout=timeout)["message"]["content"]
        if kind == "openai":  # OpenAI, Groq, OpenRouter, vLLM, LM Studio...
            body = {"model": model, "temperature": temperature,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            if want_json and prov.get("json_mode", True):   # LM Studio: set json_mode = false
                body["response_format"] = {"type": "json_object"}
            url = prov["url"].rstrip("/") + "/chat/completions"
            r = http_json(url, body, {"Authorization": f"Bearer {key}"}, timeout)
            return r["choices"][0]["message"]["content"]
        if kind == "anthropic":
            body = {"model": model, "max_tokens": 800, "temperature": temperature,
                    "system": system, "messages": [{"role": "user", "content": user}]}
            url = prov.get("url", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
            r = http_json(url, body, {"x-api-key": key,
                                      "anthropic-version": "2023-06-01"}, timeout)
            return "".join(b.get("text", "") for b in r["content"])
        raise RuntimeError(f"unknown provider type '{kind}'")

    def ask_json(self, job, system, user, temperature=0.7):
        text = self.ask(job, system, user, want_json=True, temperature=temperature)
        m = re.search(r"\{.*\}", text, re.S)  # tolerate stray prose around JSON
        return json.loads(m.group(0) if m else text)


# --------------------------------------------------------------------------
# 6. Pronunciations
# --------------------------------------------------------------------------

class Pronouncer:
    def __init__(self, table):
        items = sorted((table or {}).items(), key=lambda kv: -len(kv[0]))
        self.rules = [(re.compile(r"(?<!\w)" + re.escape(k) + r"(?!\w)", re.I), v)
                      for k, v in items]

    def apply(self, text):
        for rx, say in self.rules:
            text = rx.sub(say, text)
        return text


# --------------------------------------------------------------------------
# 5. Voices
# --------------------------------------------------------------------------

class Voices:
    """voice config examples:
         {engine="piper", model="/opt/piper/en_US-ryan-high.onnx"}
         {engine="kokoro", voice="af_bella"}
         {engine="chatterbox", voice="dj_sample"}
         {engine="espeak", voice="en-us"}
         {engine="command", command="mytts --out {out}"}   (text on stdin)
    """

    def __init__(self, cfg, pronouncer, fallback=None):
        self.cfg = cfg
        self.pron = pronouncer
        self.fallback = fallback      # radio.py's own voice: fallback(text) -> wav path or None
        self.warned = set()

    def speak(self, text, voice, out_path):
        """Writes speech to out_path and returns it, or returns None if every
        voice failed. engine "default" uses the station's normal [tts] voice."""
        text = self.pron.apply(text)
        voice = dict(voice or {})
        engine = voice.get("engine", "default")
        if engine != "default":
            try:
                getattr(self, "_" + engine)(text, voice, out_path)
                if os.path.getsize(out_path) > 1000:
                    self.warned.discard(engine)
                    return out_path
                raise RuntimeError("empty or invalid audio")
            except Exception as e:
                if engine not in self.warned:        # say it once, not every break
                    log(f"voice '{engine}' failed ({e}); using the station's normal voice")
                    self.warned.add(engine)
        if self.fallback:
            wav = self.fallback(text)
            if wav:
                os.replace(wav, out_path)
                return out_path
            return None
        self._espeak(text, {}, out_path)
        return out_path

    def _espeak(self, text, v, out):
        subprocess.run(["espeak-ng", "-v", v.get("voice", "en-us"), "-s",
                        str(v.get("speed", 165)), "-w", out, text],
                       check=True, timeout=60)

    def _piper(self, text, v, out):
        exe = v.get("binary") or self.cfg.get("piper", {}).get("binary", "piper")
        subprocess.run([exe, "--model", v["model"], "--output_file", out],
                       input=text.encode(), check=True, timeout=120,
                       capture_output=True)

    def _command(self, text, v, out):
        subprocess.run(v["command"].format(out=out), shell=True, input=text.encode(),
                       check=True, timeout=180, capture_output=True)

    def _openai_speech(self, text, v, out, server):
        url = v.get("url") or server.get("url")
        body = {"model": v.get("model") or server.get("model", "tts-1"),
                "input": text, "voice": v.get("voice") or server.get("voice"),
                "response_format": "wav"}
        body.update(server.get("extra", {}))
        body.update(v.get("extra", {}))
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/audio/speech", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=float(server.get("timeout", 120))) as r:
            data = r.read()
        if not data.startswith(b"RIFF"):   # an error message, not audio
            raise RuntimeError(data[:200].decode("utf-8", "replace"))
        with open(out, "wb") as f:
            f.write(data)

    def _kokoro(self, text, v, out):
        self._openai_speech(text, v, out, self.cfg.get("kokoro", {
            "url": "http://localhost:8880", "model": "kokoro", "voice": "af_bella"}))

    def _chatterbox(self, text, v, out):
        self._openai_speech(text, v, out, self.cfg.get("chatterbox", {
            "url": "http://localhost:4123", "model": "chatterbox"}))


# --------------------------------------------------------------------------
# 2. Personas with a notebook
# --------------------------------------------------------------------------

class Persona:
    def __init__(self, key, cfg, data_dir):
        self.key = key
        self.name = cfg.get("name", "the DJ")
        self.style = cfg.get("style", "Warm, relaxed, a little dry. Sounds like a real late-night radio host.")
        self.talkativeness = max(0.0, min(1.0, float(cfg.get("talkativeness", 0.5))))
        self.voice = cfg.get("voice", {"engine": "default"})
        self.caller_voices = cfg.get("caller_voices", [])
        self.notebook_size = int(cfg.get("notebook_size", 25))
        self.path = os.path.join(data_dir, f"notebook_{key}.json")
        self.notebook = load_json(self.path, [])
        self.lock = threading.Lock()

    @property
    def max_words(self):
        return int(15 + 45 * self.talkativeness)

    def remember(self, line):
        with self.lock:
            self.notebook.append(line)
            self.notebook = self.notebook[-self.notebook_size:]
            save_json(self.path, self.notebook)


# --------------------------------------------------------------------------
# 3. Verified facts from Wikipedia
# --------------------------------------------------------------------------

def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


def _squash(s):
    return re.sub(r"\W+", " ", s.lower()).strip()


class Facts:
    def __init__(self, models, data_dir, enabled=True):
        self.models = models
        self.enabled = enabled
        self.path = os.path.join(data_dir, "facts.json")
        self.cache = load_json(self.path, {})
        self.queue = []
        self.lock = threading.Lock()
        if enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, artist):
        entry = self.cache.get((artist or "").lower())
        return entry.get("facts", []) if entry else []

    def want(self, artist):
        k = (artist or "").lower().strip()
        if self.enabled and k and k not in self.cache:
            with self.lock:
                if artist not in self.queue:
                    self.queue.append(artist)

    def _worker(self):
        while True:
            with self.lock:
                artist = self.queue.pop(0) if self.queue else None
            if not artist:
                time.sleep(5)
                continue
            while self.models.on_air_busy:       # the DJ's next break comes first
                time.sleep(2)
            try:
                facts, src = self.lookup(artist)
                log(f"facts: {artist}: {len(facts)} verified")
                self.cache[artist.lower()] = {"facts": facts, "source": src,
                                              "checked": date.today().isoformat()}
                save_json(self.path, self.cache)
                self.failures = 0
            except Exception as e:
                # not saved, so the artist is tried again the next time it plays
                self.failures = getattr(self, "failures", 0) + 1
                if self.failures <= 3 or self.failures % 20 == 0:
                    log(f"facts: {artist}: {e}" + ("" if self.failures < 3 else
                        " (further failures are logged only occasionally)"))
                time.sleep(min(300, 10 * self.failures))   # back off while Ollama is struggling
            time.sleep(2)  # be polite to Wikipedia

    def lookup(self, artist):
        api = "https://en.wikipedia.org/w/api.php?"
        q = urllib.parse.urlencode({"action": "query", "list": "search", "format": "json",
                                    "srlimit": 3, "srsearch": f"{artist} musician OR band OR singer"})
        try:
            hits = http_json(api + q, timeout=20)["query"]["search"]
        except Exception as e:
            raise RuntimeError(f"Wikipedia search failed ({e})")
        hit = next((h for h in hits if _squash(artist) in _squash(h["title"])), None)
        if not hit:
            return [], None
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "exintro": 1,
                                    "explaintext": 1, "format": "json", "redirects": 1,
                                    "titles": hit["title"]})
        try:
            pages = http_json(api + q, timeout=20)["query"]["pages"]
        except Exception as e:
            raise RuntimeError(f"Wikipedia page failed ({e})")
        extract = next(iter(pages.values())).get("extract", "")
        if not re.search(r"\b(music|band|singer|song|album|rapper|musician|composer|group)", extract, re.I):
            return [], None  # probably not the music page
        sents = _sentences(extract)[:12]
        if not sents:
            return [], None
        numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sents))
        try:
            out = self.models.ask_json(
                "facts",
            "You extract short, interesting radio-friendly facts about a music artist. "
            "Use ONLY the numbered sentences given. Return JSON: "
            '{"facts":[{"fact":"...","sentence":N}]} with 2 to 4 facts. '
            "Each fact must be fully supported by sentence N. No outside knowledge.",
                f"Artist: {artist}\nSentences:\n{numbered}", temperature=0.2)
        except Exception as e:
            raise RuntimeError(f"model didn't answer in time ({e})")
        facts = []
        for f in out.get("facts", [])[:4]:
            try:
                n = int(f["sentence"])
                fact = str(f["fact"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
            if 0 <= n < len(sents) and 10 < len(fact) < 200 and self._supported(fact, sents[n]):
                facts.append({"fact": fact, "source": sents[n]})
        url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(hit["title"].replace(" ", "_"))
        return facts, url

    @staticmethod
    def _supported(fact, sentence):
        """Every number and capitalised name in the fact must appear in the
        source sentence. Catches the common ways a model embellishes."""
        src = sentence.lower()
        for num in re.findall(r"\d+", fact):
            if num not in src:
                return False
        for name in re.findall(r"\b[A-Z][a-zA-Z'’]+", fact)[1:]:
            if name.lower() not in src:
                return False
        return True


# --------------------------------------------------------------------------
# 4. Ratings + Plex similarity -> weighted song choice
# --------------------------------------------------------------------------

class Ratings:
    """Star ratings as radio.py keeps them. `provider()` returns a list of
    (path, stars, thumbs_up, thumbs_down); it is re-read when ratings change."""

    def __init__(self, provider):
        self.provider = provider
        self.stars = {}      # tail_key(path) -> stars
        self.thumbs = {}     # tail_key(path) -> up - down
        self.full = {}       # tail_key(path) -> real path (seeds for similarity)
        self.dirty = True

    def refresh(self):
        if not self.dirty:
            return False
        self.dirty = False
        stars, thumbs, full = {}, {}, {}
        for path, s, up, down in self.provider():
            if not path:
                continue
            k = tail_key(path)
            full[k] = path
            if s and 1 <= int(s) <= 5:
                stars[k] = int(s)
            if up or down:
                thumbs[k] = (up or 0) - (down or 0)
        self.stars, self.thumbs, self.full = stars, thumbs, full
        return True

    def get(self, path):
        return self.stars.get(tail_key(path))


class PlexSimilar:
    """Finds songs similar to a rated song. Uses Plex sonic analysis
    (/nearest) when available, then Plex's similar-artist list, then the same
    artist."""

    def __init__(self, cfg):
        self.url = (cfg.get("url") or "http://127.0.0.1:32400").rstrip("/")
        self.token = (cfg.get("token") or "").strip()
        self.ok = bool(self.token) and cfg.get("enabled", True)
        self.tracks = []          # [{key, artist_key, artist, file}]
        self.by_tail = {}
        self.cache = {}           # track key -> (close, same)
        self.indexed_at = 0

    def _get(self, path, **params):
        params["X-Plex-Token"] = self.token
        return http_json(f"{self.url}{path}?{urllib.parse.urlencode(params)}", timeout=60)

    def index(self):
        tracks = []
        for sec in self._get("/library/sections")["MediaContainer"].get("Directory", []):
            if sec.get("type") != "artist":
                continue
            mc = self._get(f"/library/sections/{sec['key']}/all", type=10)["MediaContainer"]
            for t in mc.get("Metadata", []) or []:
                for m in t.get("Media", []) or []:
                    for p in m.get("Part", []) or []:
                        if p.get("file"):
                            tracks.append({"key": str(t["ratingKey"]),
                                           "artist_key": str(t.get("grandparentRatingKey", "")),
                                           "artist": t.get("originalTitle") or t.get("grandparentTitle", ""),
                                           "file": p["file"]})
        self.tracks = tracks
        self.by_tail = {tail_key(t["file"]): t for t in tracks}
        self.cache = {}
        self.indexed_at = time.time()
        log(f"Plex: indexed {len(tracks)} songs for similar-song picks")

    def find(self, local_path):
        return self.by_tail.get(tail_key(local_path))

    def similar_files(self, track, limit=40):
        """Returns (similar songs, same-artist songs) as Plex file paths."""
        if track["key"] in self.cache:
            return self.cache[track["key"]]
        close = []
        try:
            mc = self._get(f"/library/metadata/{track['key']}/nearest",
                           limit=limit, maxDistance=0.25)["MediaContainer"]
            for t in mc.get("Metadata", []) or []:
                for m in t.get("Media", []) or []:
                    for p in m.get("Part", []) or []:
                        if p.get("file"):
                            close.append(p["file"])
        except Exception:
            pass  # no sonic analysis on this server
        if not close and track["artist_key"]:
            try:
                meta = self._get(f"/library/metadata/{track['artist_key']}")["MediaContainer"]["Metadata"][0]
                names = {s["tag"].lower() for s in meta.get("Similar", []) or []}
                close = [t["file"] for t in self.tracks if t["artist"].lower() in names]
            except Exception:
                pass
        same = [t["file"] for t in self.tracks
                if t["artist_key"] == track["artist_key"] and t["key"] != track["key"]]
        self.cache[track["key"]] = (close, same)
        return close, same


class Rotation:
    def __init__(self, cfg, plex_cfg, ratings_provider):
        self.ratings = Ratings(ratings_provider)
        self.plex = PlexSimilar(plex_cfg)
        # how much more often songs similar to an N-star song come up
        b = cfg.get("similar_boost", {})
        self.boost_for = {3: float(b.get("3", 0.25)), 4: float(b.get("4", 0.6)),
                          5: float(b.get("5", 1.5))}
        self.same_artist_share = float(cfg.get("same_artist_share", 0.5))
        r = cfg.get("rating_weight", {})
        self.own_weight = {2: float(r.get("2", 0.4)), 3: float(r.get("3", 1.0)),
                           4: float(r.get("4", 1.2)), 5: float(r.get("5", 1.4))}
        self.boost = {}             # tail_key -> extra weight
        self.dirty = True

    def rating_changed(self):
        self.ratings.dirty = True
        self.dirty = True

    def banned(self, path):
        self.ratings.refresh()
        return self.ratings.get(path) == 1

    def weight(self, path):
        self.ratings.refresh()
        k = tail_key(path)
        s = self.ratings.stars.get(k)
        if s == 1:
            return 0.0
        w = self.own_weight.get(s, 1.0)
        t = self.ratings.thumbs.get(k, 0)           # old thumbs still count
        if t < 0:
            w *= 0.3
        elif t > 0:
            w *= 1.3
        return w * (1.0 + self.boost.get(k, 0.0))

    def choose(self, candidates, rng=random):
        """Pick one path from candidates. Never returns a 1-star song."""
        pool = [c for c in candidates if not self.banned(c)]
        if not pool:
            return None
        weights = [self.weight(c) for c in pool]
        return rng.choices(pool, weights=weights, k=1)[0]

    def start(self):
        if self.plex.ok:
            threading.Thread(target=self._loop, daemon=True, name="ai-rotation").start()

    def _loop(self):
        while True:
            try:
                stale = time.time() - self.plex.indexed_at > 6 * 3600
                if stale:
                    self.plex.index()
                if stale or self.dirty:
                    self.dirty = False
                    self._rebuild()
            except Exception as e:
                log(f"similar songs: Plex update failed ({e}); trying again later")
                time.sleep(600)
            time.sleep(30)

    def _rebuild(self):
        self.ratings.dirty = True
        self.ratings.refresh()
        boost = {}
        seeds = [(self.ratings.full[k], s) for k, s in self.ratings.stars.items() if s >= 3]
        found = 0
        for path, stars in seeds:
            track = self.plex.find(path)
            if not track:
                continue
            found += 1
            b = self.boost_for[stars]
            close, same = self.plex.similar_files(track)
            for f, amount in [(f, b) for f in close] + [(f, b * self.same_artist_share) for f in same]:
                k = tail_key(f)
                boost[k] = max(boost.get(k, 0.0), amount)
        self.boost = boost
        log(f"similar songs: {found} of {len(seeds)} rated songs found in Plex, "
            f"{len(boost)} similar songs boosted")


# --------------------------------------------------------------------------
# 1+2+3. The DJ: writes breaks in the background, never blocks air
# --------------------------------------------------------------------------

BANNED_TEXT = re.compile(r"https?://|www\.|[*#_`\[\]{}<>]|\bas an ai\b", re.I)


class DJ:
    def __init__(self, models, facts, rotation, enabled=True):
        self.models = models
        self.facts = facts
        self.rotation = rotation
        self.enabled = enabled
        self.pending = {}
        self.lock = threading.Lock()

    @staticmethod
    def song_line(s):
        if not s:
            return None
        return f"\"{s.get('title', '?')}\" by {s.get('artist', '?')}"

    def build_facts(self, ctx, persona):
        d = {"station": ctx.get("station"), "channel": ctx.get("channel"),
             "dj_name": persona.name, "part_of_day": ctx.get("part_of_day")}
        for slot in ("prev", "next"):
            s = ctx.get(slot)
            if not s:
                continue
            info = {"title": s.get("title"), "artist": s.get("artist")}
            stars = self.rotation.ratings.get(s.get("path", "")) if self.rotation else None
            if stars and stars >= 4:
                info["listener_rating"] = f"{stars} stars (a favorite)"
            verified = self.facts.get(s.get("artist")) if self.facts else []
            if verified:
                info["verified_facts"] = [f["fact"] for f in verified]
            d["just_played" if slot == "prev" else "coming_up"] = info
        for k in ("weather", "headline"):
            if ctx.get(k):
                d[k] = ctx[k]
        return d

    def system_prompt(self, persona):
        return (
            f"You are {persona.name}, the on-air host of an internet radio station. "
            f"Personality: {persona.style}\n"
            "Rules:\n"
            f"- Write only the words you will say, at most {persona.max_words} words.\n"
            "- Plain spoken English. No emojis, lists, markdown, stage directions or sound effects.\n"
            "- Only mention song titles and artists exactly as given.\n"
            "- Only state facts about music that appear under verified_facts. If there are none, "
            "just introduce or back-announce the song; never invent trivia, years, chart positions or stories.\n"
            "- The time, weather and news are read separately; do not mention them unless asked to below.\n"
            "- Do not repeat or closely echo anything from 'things you said recently'.\n"
            "- Do not mention being an AI or a computer."
        )

    def write(self, ctx, persona):
        facts = self.build_facts(ctx, persona)
        recent = "\n".join(persona.notebook[-12:]) or "(nothing yet)"
        user = (f"Things you said recently:\n{recent}\n\n"
                f"Facts for this break (JSON):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                "Write the break now.")
        text = self.models.ask("talk", self.system_prompt(persona), user).strip().strip('"')
        text = re.sub(r"\s+", " ", text)
        problem = self.check(text, ctx, persona)
        if problem:
            raise ValueError(problem)
        return text

    def check(self, text, ctx, persona):
        if not text:
            return "empty"
        if len(text.split()) > persona.max_words * 1.5 + 10:
            return "too long"
        if BANNED_TEXT.search(text):
            return "formatting or links"
        allowed = " ".join(_squash(str(ctx.get(s, {}).get(k, "")))
                           for s in ("prev", "next") for k in ("title", "artist"))
        allowed += " " + _squash(str(ctx.get("headline", ""))) + " " + _squash(str(ctx.get("station", "")))
        for quoted in re.findall(r"[\"“]([^\"”]{3,})[\"”]", text):
            if _squash(quoted) not in allowed:
                return f"mentions unknown title {quoted!r}"
        for line in persona.notebook[-25:]:
            if _squash(line) == _squash(text):
                return "repeat"
        return None

    def wants_to_talk(self, kind, persona):
        """After the top-of-hour news the DJ always introduces the next song.
        After a time check, usually. Between ordinary songs, only sometimes,
        and quieter personas talk less."""
        t = persona.talkativeness
        chance = {"top": 1.0, "quarter": 0.5 + 0.5 * t}.get(kind, 0.5 * t)
        return random.random() < chance

    # -- non-blocking API ------------------------------------------------------
    def prepare(self, key, ctx, persona):
        """Start writing a break in the background. Call during the song before."""
        for s in ("prev", "next"):
            if ctx.get(s) and self.facts:
                self.facts.want(ctx[s].get("artist"))
        result = {"text": None, "done": threading.Event()}

        def run():
            try:
                if self.enabled:
                    result["text"] = self.write(ctx, persona)
            except Exception as e:
                log(f"dj: model break rejected/failed ({e}); template will be used")
            result["done"].set()

        with self.lock:
            self.pending[key] = result
        threading.Thread(target=run, daemon=True).start()

    def collect(self, key, fallback_text, persona, wait=0.0):
        """Return the AI text if ready (waiting at most `wait` seconds),
        otherwise the template text. Never raises."""
        with self.lock:
            result = self.pending.pop(key, None)
        text = None
        if result and result["done"].wait(wait):
            text = result["text"]
        if text:
            persona.remember(text)
            return text, True
        return fallback_text, False


# --------------------------------------------------------------------------
# 7. Fake phone-in segments
# --------------------------------------------------------------------------

CALLER_NAMES = ["Marcus", "Dee", "Tommy", "Rosa", "Kev", "Lynn", "Jamal", "Pat",
                "Gloria", "Sam", "Ricky", "Bev", "Andre", "Carol", "Nate", "Tina"]

CALL_TOPICS = [
    "requests a song by the artist that just played and says why they love it",
    "tells the host the song that just played reminds them of something from their life",
    "calls to say they gave the song coming up five stars and tells a small personal reason",
    "complains good-naturedly about the weather and asks for something upbeat",
    "is driving home from a late shift and just wants to say hi to the host",
    "argues playfully that the last song is the best thing the artist ever did",
]


class PhoneIn:
    def __init__(self, cfg, models, voices, data_dir):
        self.enabled = bool(cfg.get("enabled", False))
        self.per_day = int(cfg.get("per_day", 1))
        start, end = cfg.get("between_hours", [10, 21])
        self.window = (int(start), int(end))
        self.min_gap = float(cfg.get("min_gap_hours", 3)) * 3600
        self.max_turns = int(cfg.get("max_turns", 6))
        self.phone_filter = cfg.get("phone_filter",
                                    "highpass=f=300,lowpass=f=3400,acompressor,volume=1.4")
        self.models = models
        self.voices = voices
        self.state_path = os.path.join(data_dir, "phone_in_state.json")
        self.state = load_json(self.state_path, {})

    def _plan_today(self, now):
        today = now.date().isoformat()
        if self.state.get("day") != today:
            start, end = self.window
            span = max(1, (end - start) * 60)
            slots = sorted(random.sample(range(span), min(self.per_day, span)))
            self.state = {"day": today, "slots": [start * 60 + m for m in slots],
                          "done": 0, "last": self.state.get("last", 0)}
            save_json(self.state_path, self.state)

    def due(self, now=None):
        """True when a call should air at the next break."""
        if not self.enabled or self.per_day <= 0:
            return False
        now = now or datetime.now()
        self._plan_today(now)
        done = self.state["done"]
        if done >= len(self.state["slots"]):
            return False
        minute = now.hour * 60 + now.minute
        return minute >= self.state["slots"][done] and time.time() - self.state["last"] > self.min_gap

    def mark_aired(self):
        self._plan_today(datetime.now())
        self.state["done"] += 1
        self.state["last"] = time.time()
        save_json(self.state_path, self.state)

    def write(self, ctx, persona, dj):
        caller = random.choice(CALLER_NAMES)
        topics = [t for t in CALL_TOPICS if ctx.get("weather") or "weather" not in t]
        topic = random.choice(topics)
        facts = dj.build_facts(ctx, persona)
        out = self.models.ask_json(
            "phone_in",
            dj.system_prompt(persona) +
            "\nNow write a short on-air phone call. Return JSON: "
            '{"lines":[{"who":"host"|"caller","text":"..."}]}. '
            f"Between 4 and {self.max_turns} lines total, starting and ending with the host. "
            "Each line under 35 words. The caller is an ordinary, friendly listener. "
            "Keep it natural and a little funny, never mean. The host ends by playing the next song.",
            f"Caller's first name: {caller}. The caller {topic}.\n"
            f"Facts (JSON): {json.dumps(facts, ensure_ascii=False)}")
        lines = []
        for ln in out.get("lines", [])[: self.max_turns + 2]:
            who = "caller" if str(ln.get("who", "")).lower().startswith("c") else "host"
            text = re.sub(r"\s+", " ", str(ln.get("text", ""))).strip()
            if text and not BANNED_TEXT.search(text) and len(text.split()) < 60:
                lines.append((who, text))
        if len(lines) < 3 or not any(w == "caller" for w, _ in lines):
            raise ValueError("phone-in script too short")
        return caller, lines

    def produce(self, ctx, persona, dj, out_path):
        """Writes and voices the whole call into one WAV. Raises on failure,
        so radio.py simply skips the call."""
        caller, lines = self.write(ctx, persona, dj)
        caller_voice = random.choice(persona.caller_voices) if persona.caller_voices else \
            {"engine": "default"}
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for i, (who, text) in enumerate(lines):
                raw = os.path.join(tmp, f"{i}_raw.wav")
                fixed = os.path.join(tmp, f"{i}.wav")
                if not self.voices.speak(text, persona.voice if who == "host" else caller_voice, raw):
                    raise RuntimeError("no voice available")
                af = ["-af", self.phone_filter] if who == "caller" else []
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, *af,
                                "-ar", "44100", "-ac", "2", fixed], check=True, timeout=60)
                parts.append(fixed)
            gap = os.path.join(tmp, "gap.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=stereo", "-t", "0.35", gap], check=True, timeout=30)
            listing = os.path.join(tmp, "list.txt")
            with open(listing, "w") as f:
                for p in parts:
                    f.write("file '{}'\nfile '{}'\n".format(p.replace("\\", "/"), gap.replace("\\", "/")))
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", listing, "-c:a", "pcm_s16le", out_path], check=True, timeout=120)
        persona.remember(f"(phone call with {caller}) " + lines[-1][1])
        log(f"phone-in: produced call with {caller}, {len(lines)} lines")
        return out_path


# --------------------------------------------------------------------------
# The one object radio.py uses
# --------------------------------------------------------------------------

class Station:
    """
    The one object radio.py uses. Everything here is safe to call from the
    station threads: it never raises and never blocks the air for long.

        ai = Station(cfg, here, ratings_provider, tts_fallback)
        song = ai.choose_song(pool)                    # weighted, never 1-star
        if ai.wants_to_talk(kind, persona):
            text = ai.write_break(ctx, persona)        # None = say nothing
            wav = ai.speak(text, persona, "x.wav")     # None = voice failed
            ai.aired(text, persona)                    # once it actually aired
        ai.phone_in_due() / ai.make_phone_in(ctx, persona, "call.wav")
        ai.rating_changed()
    """

    def __init__(self, cfg, data_dir, ratings_provider=lambda: [], tts_fallback=None):
        self.cfg = cfg
        ai = cfg.get("ai", {})
        self.enabled = bool(ai.get("enabled", True))
        self.data_dir = ai.get("data_dir") or data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.station_name = cfg.get("station", {}).get("name", "HomeRadio")
        self.models = Models(ai)
        self.pron = Pronouncer(cfg.get("pronunciations", {}))
        self.voices = Voices(ai.get("voices", {}), self.pron, tts_fallback)
        self.facts = Facts(self.models, self.data_dir, self.enabled and bool(ai.get("facts", True)))
        self.rotation = Rotation(cfg.get("ratings", {}), cfg.get("plex", {}), ratings_provider)
        self.rotation.start()
        self.dj = DJ(self.models, self.facts, self.rotation, enabled=self.enabled)
        self.phone = PhoneIn(ai.get("phone_in", {}) if self.enabled else {}, self.models,
                             self.voices, self.data_dir)
        self.personas = {k: Persona(k, v, self.data_dir) for k, v in ai.get("personas", {}).items()}
        if "default" not in self.personas:
            self.personas["default"] = Persona("default", {}, self.data_dir)
        if self.enabled:
            log(f"DJ on, writing with {self.models.model_for('talk')}; "
                f"personas: {', '.join(self.personas)}; "
                f"phone-ins: {f'{self.phone.per_day} a day' if self.phone.enabled else 'off'}")
        else:
            log("DJ off ([ai] enabled = false); ratings and similar songs still active")

    def persona(self, key=None):
        return self.personas.get(key or "default", self.personas["default"])

    def pronounce(self, text):
        return self.pron.apply(text)

    # songs
    def choose_song(self, pool):
        return self.rotation.choose(pool)

    def rating_changed(self):
        self.rotation.rating_changed()

    # talk
    def wants_to_talk(self, kind, persona=None):
        return self.enabled and self.dj.wants_to_talk(kind, self.persona(persona))

    def write_break(self, ctx, persona=None):
        """Blocking (run it in a background thread). Returns text or None."""
        if not self.enabled:
            return None
        ctx = dict(ctx, station=self.station_name)
        for s in ("prev", "next"):
            if ctx.get(s):
                self.facts.want(ctx[s].get("artist"))
        try:
            return self.dj.write(ctx, self.persona(persona))
        except Exception as e:
            log(f"DJ break not used ({e})")
            return None

    def aired(self, text, persona=None):
        try:
            self.persona(persona).remember(text)
        except Exception as e:
            log(f"couldn't note what the DJ said ({e})")

    def speak(self, text, persona, out_path):
        try:
            return self.voices.speak(text, self.persona(persona).voice, out_path)
        except Exception as e:
            log(f"voice failed ({e})")
            return None

    # phone-ins
    def phone_in_due(self):
        return self.enabled and self.phone.due()

    def make_phone_in(self, ctx, persona, out_path):
        ctx = dict(ctx, station=self.station_name)
        try:
            self.phone.produce(ctx, self.persona(persona), self.dj, out_path)
            self.phone.mark_aired()
            return out_path
        except Exception as e:
            log(f"phone-in skipped ({e})")
            return None


# --------------------------------------------------------------------------
# self-tests:  python3 homeradio_ai.py <command>
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="HomeRadio AI self-tests")
    ap.add_argument("command", choices=["break", "voice", "facts", "phone-in", "similar"])
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "radio.toml"))
    ap.add_argument("--persona", default="default")
    ap.add_argument("--artist", default="Fleetwood Mac")
    ap.add_argument("--text", default="You're listening to WANG Radio. It's seven fifteen.")
    a = ap.parse_args()
    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    here = os.path.dirname(os.path.abspath(a.config))

    def ratings_from_file():
        data = load_json(os.path.join(here, "ratings.json"), {})
        return [(e.get("path"), e.get("stars"), e.get("up", 0), e.get("down", 0)) for e in data.values()]

    st = Station(cfg, here, ratings_from_file)
    demo = {"part_of_day": "evening", "weather": "Clear tonight, low of 52",
            "prev": {"title": "Dreams", "artist": a.artist, "path": "/music/a.mp3"},
            "next": {"title": "Heart of Glass", "artist": "Blondie", "path": "/music/b.mp3"}}
    if a.command == "break":
        try:
            print(st.dj.write(dict(demo, station=st.station_name), st.persona(a.persona)))
        except Exception as e:
            print(f"model break failed: {e}")
    elif a.command == "voice":
        print(st.speak(a.text, a.persona, "test_voice.wav") or "voice failed (see log)")
    elif a.command == "facts":
        print(json.dumps(st.facts.lookup(a.artist), indent=1))
    elif a.command == "phone-in":
        print(st.make_phone_in(demo, a.persona, "test_call.wav") or "failed (see log)")
    elif a.command == "similar":
        if not st.rotation.plex.ok:
            print("No Plex token in [plex]")
        else:
            st.rotation.plex.index()
            st.rotation._rebuild()
            for k, b in sorted(st.rotation.boost.items(), key=lambda kv: -kv[1])[:25]:
                print(f"+{b:.2f}  {k}")
