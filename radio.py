#!/usr/bin/env python3
"""
HomeRadio - an all-in-one internet radio station for your music collection.

  * Shuffles your music library (no repeats until much of the library has played)
  * Station ID at :00 and :30 (random file from your IDs folder)
  * Top of hour: ID, time, short weather forecast, top 3 headlines
  * Time check read between songs near each quarter hour (:15 / :30 / :45)
  * Picks songs that "fit" before the top of the hour so breaks land on time
  * Built-in streaming server (MP3 + ICY "now playing" metadata), no Icecast needed

Requires: Python 3.11+, ffmpeg/ffprobe, and a TTS engine (espeak-ng or piper).
"""
import collections, datetime as dt, hashlib, html, json, re, logging, os, queue, random, shlex
import shutil, subprocess, sys, tempfile, threading, time, tomllib, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                   # AI DJ, voices, phone-ins, similar songs (optional)
    import homeradio_ai
except Exception as _e:                # missing or broken: the station runs exactly as before
    homeradio_ai = None
    _AI_IMPORT_ERROR = _e

RATE, CHANNELS = 44100, 2
BYTES_PER_SEC = RATE * CHANNELS * 2
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".opus", ".wma", ".aiff"}
ICY_METAINT = 16000
log = logging.getLogger("homeradio")


# ---------------------------------------------------------------- helpers
def track_id(path):
    return hashlib.sha1(os.path.abspath(path).encode("utf-8", "replace")).hexdigest()[:16]


def probe(path):
    """Return (duration_seconds, 'Artist - Title', info dict) using ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
        fmt = json.loads(out).get("format", {})
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        name = os.path.splitext(os.path.basename(path))[0]
        title = tags.get("title", name)
        artist = tags.get("artist")
        year = re.sub(r"\D", "", str(tags.get("date") or tags.get("year") or ""))[:4]
        info = {"id": track_id(path), "title": title, "artist": artist or "",
                "album": tags.get("album", ""), "file": os.path.basename(path),
                "genre": tags.get("genre", ""), "year": year}
        return float(fmt.get("duration", 0) or 0), (f"{artist} - {title}" if artist else title), info
    except Exception:
        name = os.path.basename(path)
        return 0.0, name, {"id": track_id(path), "title": name, "artist": "", "album": "",
                           "file": name, "genre": "", "year": ""}


COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall", "artwork")


def find_art(path):
    """Album art for a song: embedded picture first, then cover.jpg/folder.jpg etc. next to it."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-an",
                            "-map", "0:v:0", "-frames:v", "1", "-vf", "scale='min(800,iw)':-2",
                            "-f", "image2", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=20)
        if r.returncode == 0 and len(r.stdout) > 500:
            return r.stdout, "image/jpeg"
    except Exception:
        pass
    folder = os.path.dirname(path)
    try:
        for n in sorted(os.listdir(folder)):
            base, ext = os.path.splitext(n.lower())
            if ext in (".jpg", ".jpeg", ".png") and (base in COVER_NAMES or base.startswith("albumart")):
                with open(os.path.join(folder, n), "rb") as f:
                    return f.read(), "image/png" if ext == ".png" else "image/jpeg"
    except OSError:
        pass
    return None, None


class Ratings:
    """Thumbs up / down from the app, saved to ratings.json (and every vote to ratings_log.csv)."""
    def __init__(self, folder):
        self.path = os.path.join(folder, "ratings.json")
        self.log_path = os.path.join(folder, "ratings_log.csv")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def vote(self, info, rating):
        with self.lock:
            e = self.data.setdefault(info["id"], {"up": 0, "down": 0})
            prev = e.get("rating")
            if prev == rating:             # pressing the same thumb again clears it
                e[rating] = max(0, e[rating] - 1)
                e["rating"] = None
            else:
                if prev:
                    e[prev] = max(0, e[prev] - 1)
                e[rating] += 1
                e["rating"] = rating
            self._save(e, info, e["rating"] or "cleared")
            return e["rating"]

    def set_stars(self, info, stars):
        """1-5 stars (0 clears). Returns the stars now saved."""
        with self.lock:
            e = self.data.setdefault(info["id"], {"up": 0, "down": 0})
            e["stars"] = stars or None
            self._save(e, info, f"{stars} stars" if stars else "stars cleared")
            return e["stars"]

    def _save(self, e, info, what):
        if True:
            e.update({k: info.get(k, "") for k in ("title", "artist", "album", "file")})
            e["path"] = info.get("path", e.get("path", ""))
            e["updated"] = dt.datetime.now().isoformat(timespec="seconds")
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
            new = not os.path.exists(self.log_path)
            with open(self.log_path, "a", encoding="utf-8", newline="") as f:
                if new:
                    f.write("time,vote,artist,title,album,file\n")
                row = [e["updated"], what, e["artist"], e["title"], e["album"], e["file"]]
                f.write(",".join('"' + str(x).replace('"', "'") + '"' for x in row) + "\n")

    def get(self, tid):
        return (self.data.get(tid) or {}).get("rating")

    def stars(self, tid):
        return (self.data.get(tid) or {}).get("stars")

    def score(self, tid):
        """Rotation weight: 1-2 stars play less often, 4-5 stars a bit more (old thumbs still count)."""
        e = self.data.get(tid)
        if not e:
            return 0
        stars = e.get("stars") or 0
        return e.get("up", 0) - e.get("down", 0) + ({1: -2, 2: -1, 4: 1, 5: 2}.get(stars, 0))


class Plex:
    """Sends star ratings to Plex (one way: station -> Plex).

    Songs are matched to Plex by file path (or file name, if Plex sees the files under a
    different path). Ratings are sent in the background so the app never waits on Plex."""

    def __init__(self, cfg):
        self.url = (cfg.get("url") or "http://127.0.0.1:32400").rstrip("/")
        self.token = cfg.get("token", "").strip()
        self.enabled = bool(cfg.get("enabled", True)) and bool(self.token)
        self.sections = None
        self.keys = {}                       # our track id -> Plex ratingKey
        self.jobs = queue.Queue()
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="plex").start()
            self.jobs.put(("check", None, None))

    def _req(self, path, params=None, method="GET"):
        q = dict(params or {}, **{"X-Plex-Token": self.token})
        req = urllib.request.Request(f"{self.url}{path}?{urllib.parse.urlencode(q)}", method=method,
                                     headers={"Accept": "application/json", "User-Agent": "HomeRadio/1.0",
                                              "X-Plex-Client-Identifier": "homeradio",
                                              "X-Plex-Product": "WANG Radio"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
        return json.loads(body) if body.strip().startswith(b"{") else {}

    def _music_sections(self):
        if self.sections is None:
            d = self._req("/library/sections").get("MediaContainer", {}).get("Directory", [])
            self.sections = [x["key"] for x in d if x.get("type") == "artist"]
        return self.sections

    @staticmethod
    def _norm(p):
        return p.replace("\\", "/").rstrip("/").lower()

    def find(self, info):
        if info["id"] in self.keys:
            return self.keys[info["id"]]
        want = self._norm(info.get("path", ""))
        base = re.split(r"[\\/]", want)[-1]
        stem = os.path.splitext(os.path.basename(info.get("path", "")))[0]
        fallback = None
        for term in dict.fromkeys(t for t in (info.get("title"), stem) if t):
            for sec in self._music_sections():
                res = self._req(f"/library/sections/{sec}/all", {"type": 10, "title": term})
                for m in res.get("MediaContainer", {}).get("Metadata", []) or []:
                    for media in m.get("Media", []) or []:
                        for part in media.get("Part", []) or []:
                            f = self._norm(part.get("file", ""))
                            if f == want:
                                self.keys[info["id"]] = m["ratingKey"]
                                return m["ratingKey"]
                            if f and re.split(r"[\\/]", f)[-1] == base and fallback is None:
                                fallback = m["ratingKey"]
            if fallback:
                break
        if fallback:
            self.keys[info["id"]] = fallback
        return fallback

    def rate(self, info, stars):
        if self.enabled:
            self.jobs.put(("rate", info, stars))

    def _worker(self):
        while True:
            job, info, stars = self.jobs.get()
            try:
                if job == "check":
                    n = len(self._music_sections())
                    log.info("Plex connected (%d music librar%s)", n, "y" if n == 1 else "ies")
                    if not n:
                        log.warning("Plex: no music libraries found - star ratings won't sync")
                    continue
                key = self.find(info)
                if not key:
                    log.warning("Plex: couldn't find '%s' in your Plex library", info.get("display") or info.get("title"))
                    continue
                self._req("/:/rate", {"key": key, "identifier": "com.plexapp.plugins.library",
                                      "rating": stars * 2 if stars else -1}, method="PUT")
                log.info("Plex: rated %s %s", info.get("display") or info.get("title"),
                         "\u2605" * stars if stars else "(cleared)")
            except urllib.error.HTTPError as e:
                log.warning("Plex said %s %s%s", e.code, e.reason,
                            " - check your Plex token" if e.code == 401 else "")
            except Exception as e:
                log.warning("Plex not reachable (%s): %s", self.url, e)


def _answers(url, path="", timeout=2.0):
    """(up, detail): any HTTP answer, even 401, counts as up. No answer at all is down."""
    try:
        req = urllib.request.Request(url.rstrip("/") + path, headers={"User-Agent": "HomeRadio/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"answered ({r.status})"
    except urllib.error.HTTPError as e:
        return True, f"answered ({e.code})"
    except Exception as e:
        return False, str(e)[:120]


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "HomeRadio/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_audio(folder):
    files = []
    if folder and os.path.isdir(folder):
        for root, _, names in os.walk(folder, followlinks=True):
            files += [os.path.join(root, n) for n in names
                      if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith(".")]
    return files


# ---------------------------------------------------------------- music library
class Library:
    """Your music, from one or more folders.

    Startup is instant: the song list from the last run is loaded from library_cache.json,
    and the folders are (re)scanned in the background. On the very first run, playback
    starts as soon as the first few songs are found while the scan keeps going."""

    def __init__(self, folders, rescan_minutes=30, cache_path=None, track_filter=None):
        if isinstance(folders, str):
            folders = [folders]
        self.folders = [os.path.expanduser(f) for f in folders if f]
        self.folder = ", ".join(self.folders)          # for log messages
        self.rescan_every = max(1, rescan_minutes) * 60
        self.cache_path = cache_path
        self.track_filter = track_filter               # callable(path, info) -> bool, or None
        self._warned_empty = False
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.files = []
        self.recent = collections.deque(maxlen=1)
        self.durations = {}
        self._load_cache()
        threading.Thread(target=self._scanner, daemon=True, name="library-scan").start()

    # -- cache
    def _load_cache(self):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if sorted(c.get("folders", [])) == sorted(self.folders) and c.get("files"):
                self.files = c["files"]
                self.ready.set()
                log.info("Library: %d tracks (from last run; rescanning in the background)", len(self.files))
        except (OSError, ValueError):
            pass

    def _save_cache(self, files):
        if not self.cache_path:
            return
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"folders": self.folders, "files": files}, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("Couldn't save library cache: %s", e)

    # -- background scanning
    def _scanner(self):
        while True:
            try:
                self.scan()
            except Exception as e:
                log.warning("Library scan problem: %s", e)
                self.ready.set()
            time.sleep(self.rescan_every)

    def scan(self):
        t0 = time.time()
        found, last_publish = [], time.time()
        for folder in self.folders:
            if not os.path.isdir(folder):
                log.warning("Music folder not found: %s", folder)
                continue
            for root, dirs, names in os.walk(folder, followlinks=True):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                found += [os.path.join(root, n) for n in names
                          if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith(".")]
                # First run (no cache): start playing early, and keep feeding new songs in.
                if not self.ready.is_set() and len(found) >= 20:
                    self._publish(found)
                    self.ready.set()
                    log.info("Library: starting with the first %d songs found; still scanning...", len(found))
                elif self.ready.is_set() and not self._had_cache and time.time() - last_publish > 5:
                    self._publish(found)
                    last_publish = time.time()
        self._publish(found)
        self.ready.set()
        self._had_cache = True
        self._save_cache(found)
        log.info("Library: %d tracks in %s (scan took %.0f s)", len(found), self.folder, time.time() - t0)

    _had_cache = False

    def _publish(self, found):
        with self.lock:
            self.files = list(found)

    # -- picking songs
    def duration(self, path):
        if path not in self.durations:
            self.durations[path] = probe(path)
        return self.durations[path]

    def _candidates(self):
        self.ready.wait()
        with self.lock:
            files = self.files
        if self.track_filter:
            kept = [f for f in files if self._passes_filter(f)]
            if kept:
                self._warned_empty = False
            elif files and not self._warned_empty:
                # Keep the station on the air rather than going silent; say why.
                log.warning("Channel filter matches no songs yet (%s); playing unfiltered for now",
                            self.folder)
                self._warned_empty = True
            files = kept or files
        size = max(1, len(files) * 2 // 3)
        if self.recent.maxlen != size:                 # don't repeat until ~2/3 of the library has played
            self.recent = collections.deque(self.recent, maxlen=size)
        recent = set(self.recent)
        return [f for f in files if f not in recent] or files

    def _passes_filter(self, path):
        """Check a track against the channel's genre/artist/star filter."""
        try:
            _, _, info = self.duration(path)
            return self.track_filter(path, info)
        except Exception:
            return True

    def next(self, max_seconds=None, ratings=None, banned=None, ai=None):
        """Pick a track; if max_seconds is given, try to find one that fits.
        banned: track ids that must never play (1-star songs).
        With ai: weighted by stars and by songs similar to the ones you rated.
        With ratings only: thumbs-down songs come up much less often, thumbs-up a bit more."""
        pool = self._candidates()
        if banned:
            pool = [f for f in pool if track_id(f) not in banned]
        if not pool:
            return None
        for _ in range(10):                            # skip files deleted since the last scan
            pick = (ai.choose_song(pool) if ai else None) or random.choice(pool)
            if os.path.isfile(pick):
                break
        else:
            return None
        if ratings and not ai:
            for _ in range(6):
                sc = ratings.score(track_id(pick))
                if sc < 0 and random.random() < 0.85:
                    pick = random.choice(pool)
                    continue
                if sc <= 0 and random.random() < 0.15:
                    up = [f for f in random.sample(pool, min(40, len(pool))) if ratings.score(track_id(f)) > 0]
                    if up:
                        pick = random.choice(up)
                break
        if max_seconds:
            deadline = time.time() + 5                 # don't hold up the station probing files
            for cand in random.sample(pool, min(25, len(pool))):
                if time.time() > deadline:
                    break
                d = self.duration(cand)[0]
                if 60 < d <= max_seconds:
                    pick = cand
                    break
        if not os.path.isfile(pick):
            return None
        self.recent.append(pick)
        return pick


# ---------------------------------------------------------------- weather / news / tts
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
       81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


class Weather:
    def __init__(self, cfg):
        self.cfg, self.cache, self.cache_t = cfg, None, 0

    def forecast_text(self):
        c = self.cfg
        if not c.get("enabled", True):
            return None
        if self.cache and time.time() - self.cache_t < 1200:
            return self.cache
        try:
            unit = c.get("units", "fahrenheit")
            q = urllib.parse.urlencode({
                "latitude": c["latitude"], "longitude": c["longitude"], "timezone": "auto",
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
                "temperature_unit": unit, "forecast_days": 2})
            d = json.loads(http_get("https://api.open-meteo.com/v1/forecast?" + q))
            cur, day = d["current"], d["daily"]
            place = c.get("location_name", "the area")
            now_t = round(cur["temperature_2m"])
            now_sky = WMO.get(cur["weather_code"], "")
            hour = dt.datetime.now().hour
            if hour < 15:
                txt = (f"Your forecast for {place}: right now it's {now_t} degrees and {now_sky}. "
                       f"Today, {WMO.get(day['weather_code'][0], '')} with a high of "
                       f"{round(day['temperature_2m_max'][0])}, and a low tonight around "
                       f"{round(day['temperature_2m_min'][1])}.")
                rain = day["precipitation_probability_max"][0]
            else:
                txt = (f"Your forecast for {place}: currently {now_t} degrees and {now_sky}. "
                       f"Tonight, a low around {round(day['temperature_2m_min'][1])}. Tomorrow, "
                       f"{WMO.get(day['weather_code'][1], '')} with a high of "
                       f"{round(day['temperature_2m_max'][1])}.")
                rain = day["precipitation_probability_max"][1]
            if rain and rain >= 20:
                txt += f" Chance of rain {rain} percent."
            self.cache, self.cache_t = txt, time.time()
            return txt
        except Exception as e:
            log.warning("Weather failed: %s", e)
            return self.cache


class News:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock, self.cache, self.cache_t = threading.Lock(), None, 0.0

    def headlines_text(self):
        with self.lock:                      # shared by all stations: fetch at most every 10 min
            if self.cache and time.time() - self.cache_t < 600:
                return self.cache
            text = self._fetch()
            if text:
                self.cache, self.cache_t = text, time.time()
            return text or self.cache

    def _fetch(self):
        c = self.cfg
        if not c.get("enabled", True):
            return None
        count, titles = int(c.get("count", 3)), []
        for feed in c.get("feeds", []):
            try:
                root = ET.fromstring(http_get(feed))
                items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                for it in items:
                    t = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or ""
                    t = html.unescape(t).strip()
                    if " - " in t and len(t.rsplit(" - ", 1)[1]) < 30:  # drop " - Source" suffix
                        t = t.rsplit(" - ", 1)[0]
                    if t and t not in titles:
                        titles.append(t)
                    if len(titles) >= count:
                        break
            except Exception as e:
                log.warning("News feed failed (%s): %s", feed, e)
            if len(titles) >= count:
                break
        if not titles:
            return None
        intro = c.get("intro", "Here are the top headlines this hour.")
        return intro + " " + " ".join(f"{t.rstrip('.')}." for t in titles)


class TTS:
    """Renders speech with any command-line engine.
    Template tokens: {out} = wav file to write, {text} = text (omit it to send text on stdin)."""
    def __init__(self, cfg):
        default = "windows" if os.name == "nt" else "espeak-ng -s 160 -w {out} {text}"
        self.template = cfg.get("command", default)
        self.voice = cfg.get("windows_voice", "")
        # Piper: put several voice models (.onnx + .onnx.json) in voices_dir and use {voice}
        # in the command; a random one is picked every time the station speaks.
        self.voices_dir = os.path.expanduser(cfg.get("voices_dir", "") or "")
        if self.template != "windows" and os.name == "nt":
            exe = shlex.split(self.template)[0] if self.template else ""
            if not shutil.which(exe) and not os.path.isfile(exe):
                log.warning("TTS program '%s' not found; using the built-in Windows voice", exe)
                self.template = "windows"

    def random_voice(self):
        if not os.path.isdir(self.voices_dir):
            return ""
        voices = [os.path.join(self.voices_dir, n) for n in os.listdir(self.voices_dir)
                  if n.lower().endswith(".onnx") and os.path.isfile(os.path.join(self.voices_dir, n + ".json"))]
        if not voices:
            return ""
        v = random.choice(voices)
        log.info("Voice: %s", os.path.basename(v))
        return v

    def _windows(self, text, out):
        # Built-in Windows speech (System.Speech), no install needed. Text goes via stdin.
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              + ("$v=$s.GetInstalledVoices()|?{$_.Enabled}|Get-Random;$s.SelectVoice($v.VoiceInfo.Name);"
                 if self.voice.lower() == "random" else
                 (f"$s.SelectVoice('{self.voice}');" if self.voice else "")) +
              "$s.Rate=0;$t=[Console]::In.ReadToEnd();"
              f"$s.SetOutputToWaveFile('{out}');$s.Speak($t);$s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text.encode("utf-8"),
                       capture_output=True, timeout=60, check=True)

    # one voice at a time: parallel Windows speech can hang (shared with homeradio_ai's voices)
    _lock = homeradio_ai.WINDOWS_SPEECH_LOCK if homeradio_ai and hasattr(homeradio_ai, "WINDOWS_SPEECH_LOCK") \
        else threading.Lock()

    def render(self, text):
        with TTS._lock:
            return self._render(text)

    def _render(self, text):
        fd, out = tempfile.mkstemp(suffix=".wav", prefix="homeradio_")
        os.close(fd)
        if self.template == "windows":
            try:
                self._windows(text, out)
                if os.path.getsize(out) > 0:
                    return out
            except Exception as e:
                log.warning("Windows TTS failed: %s", e)
            os.unlink(out)
            return None
        uses_text_arg = "{text}" in self.template
        voice = self.random_voice() if "{voice}" in self.template else ""
        if "{voice}" in self.template and not voice:
            log.warning("No voices (.onnx) found in voices_dir '%s'", self.voices_dir)
            os.unlink(out)
            return None
        args = [a.replace("{out}", out).replace("{text}", text).replace("{voice}", voice) for a in shlex.split(self.template, posix=os.name != "nt")]
        try:
            subprocess.run(args, input=None if uses_text_arg else text.encode(),
                           capture_output=True, timeout=60, check=True)
            if os.path.getsize(out) > 0:
                return out
        except Exception as e:
            log.warning("TTS failed: %s", e)
        os.unlink(out)
        return None


# ---------------------------------------------------------------- streaming
def voice_filter(path, target_lufs):
    """A filter that brings a speech clip to target_lufs. Measures the clip first (one-pass
    loudnorm can't judge a few seconds of speech, which left voices far quieter than the music),
    then applies the gain linearly with a true-peak ceiling."""
    fallback = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
                            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
        if float(m["input_i"]) < -70:                 # silence: nothing to measure
            return fallback
        return (f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:measured_I={m['input_i']}:"
                f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
                f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    except Exception:
        return fallback


class Broadcaster:
    """Fans encoded MP3 out to all connected listeners."""
    def __init__(self, burst_seconds=4, kbps=128):
        self.lock, self.listeners = threading.Lock(), set()
        self.burst = collections.deque()
        self.burst_bytes, self.burst_max = 0, burst_seconds * kbps * 125
        self.title = "Starting up"
        self.track = {"id": None, "title": "Starting up", "artist": "", "album": "", "kind": "station"}
        self.empty_since = time.monotonic() - 86400   # when the last listener left (None = someone's on)
        self.joined = threading.Event()            # set whenever a listener connects

    def clear_burst(self):
        """Forget the saved head start, so a player that reconnects (after Next) starts at live."""
        with self.lock:
            self.burst.clear()
            self.burst_bytes = 0

    def publish(self, chunk):
        with self.lock:
            self.burst.append(chunk)
            self.burst_bytes += len(chunk)
            while self.burst_bytes > self.burst_max:
                self.burst_bytes -= len(self.burst.popleft())
            for q in list(self.listeners):
                if q.qsize() > 4000:              # ~30+ s behind: give up on this client
                    self.listeners.discard(q)
                else:
                    q.put_nowait(chunk)

    def add(self):
        q = queue.Queue()
        with self.lock:
            if self.burst:
                q.put_nowait(b"".join(self.burst))
            self.listeners.add(q)
            self.empty_since = None
        self.joined.set()
        return q

    def remove(self, q):
        with self.lock:
            self.listeners.discard(q)
            if not self.listeners and self.empty_since is None:
                self.empty_since = time.monotonic()

    def idle_for(self):
        """Seconds since the last listener left (0 while someone is listening)."""
        with self.lock:
            if self.listeners:
                return 0.0
            if self.empty_since is None:
                self.empty_since = time.monotonic()
            return time.monotonic() - self.empty_since


class Encoder:
    """One continuous ffmpeg MP3 encoder; we feed it raw PCM in real time."""
    def __init__(self, bc, bitrate):
        self.bc = bc
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", bitrate,
             "-f", "mp3", "pipe:1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.t0, self.sent = time.monotonic(), 0
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._gap_filler, daemon=True).start()

    def _gap_filler(self):
        """If the station pauses (rendering speech, opening the next song, a slow network drive),
        keep the stream flowing with a little silence so players never run dry and stall."""
        chunk = b"\x00" * (BYTES_PER_SEC // 20 // 4 * 4)        # 50 ms
        while self.proc.poll() is None:
            time.sleep(0.05)
            with self.lock:
                ahead = self.sent / BYTES_PER_SEC - (time.monotonic() - self.t0)
                if ahead < 0.15:
                    try:
                        self._write(chunk)
                    except (BrokenPipeError, OSError, ValueError):
                        return

    def _pump(self):
        while True:
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                break
            self.bc.publish(chunk)

    def write_pcm(self, pcm):
        # Pace to real time (keep ~0.5 s of lead) so all listeners hear the same live stream.
        ahead = self.sent / BYTES_PER_SEC - (time.monotonic() - self.t0)
        if ahead > 0.5:
            time.sleep(ahead - 0.5)
        with self.lock:
            if self.sent / BYTES_PER_SEC - (time.monotonic() - self.t0) < -2:   # fell far behind; resync
                self.t0, self.sent = time.monotonic(), 0
            self._write(pcm)

    def _write(self, pcm):
        self.proc.stdin.write(pcm)
        self.proc.stdin.flush()
        self.sent += len(pcm)

    def silence(self, seconds):
        n = int(seconds * 10)
        for _ in range(n):
            self.write_pcm(b"\x00" * (BYTES_PER_SEC // 10 // 4 * 4))



# ---------------------------------------------------------------- settings page (edits radio.toml)
CONFIG_PATH = None                        # set by main()
SECRET_WORDS = ("password", "token", "api_key", "secret")


class TomlFile:
    """Reads radio.toml as a list of settings and changes values in place, keeping
    comments, order and layout. Every save is checked by parsing the whole file first."""
    KEY = re.compile(r'^(\s*)([A-Za-z0-9_\-]+|"[^"]*")(\s*=\s*)(.*)$')

    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.lines = f.read().split("\n")

    @staticmethod
    def _value(text):
        try:
            return True, tomllib.loads("x = " + text)["x"]
        except Exception:
            return False, None

    def _split(self, text):
        """'value  # comment' -> ('value', '# comment'). Handles # inside strings."""
        ok, _ = self._value(text)
        if not ok:
            return None, None
        for i, ch in enumerate(text):
            if ch == "#":
                head = text[:i].rstrip()
                if head and self._value(head)[0]:
                    return head, text[i:]
        return text.strip(), ""

    def entries(self):
        out, table, counts, notes = [], "", {}, []
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            st = line.strip()
            if st.startswith("#"):
                notes.append(st.lstrip("#").strip())
                i += 1
                continue
            if not st:
                notes = []
                i += 1
                continue
            if st.startswith("[[") and "]]" in st:
                name = st[2:st.index("]]")].strip()
                counts[name] = counts.get(name, 0) + 1
                table, notes = f"{name}#{counts[name]}", []
                i += 1
                continue
            if st.startswith("[") and "]" in st:
                table, notes = st[1:st.index("]")].strip(), []
                i += 1
                continue
            m = self.KEY.match(line)
            if not m:
                i += 1
                continue
            text, end = m.group(4), i                # values may run over several lines
            while not self._value(text)[0] and end + 1 < len(self.lines) and end - i < 200:
                end += 1
                text += "\n" + self.lines[end]
            raw, comment = self._split(text)
            if raw is not None:
                _, val = self._value(raw)
                key = m.group(2).strip('"')
                kind = ("bool" if isinstance(val, bool) else "number" if isinstance(val, (int, float))
                        else "secret" if isinstance(val, str) and any(w in key.lower() for w in SECRET_WORDS)
                        else "text" if isinstance(val, str) else "raw")
                out.append({"id": f"{i}", "table": table, "key": key, "kind": kind,
                            "value": "" if kind == "secret" else (raw if kind == "raw" else val),
                            "is_set": bool(val), "help": (" ".join(notes) + " " + comment.lstrip("#").strip()).strip(),
                            "start": i, "end": end, "indent": m.group(1), "eq": m.group(3), "comment": comment})
            notes = []
            i = end + 1
        return out

    def save(self, changes):
        """changes: {id: new value}. Returns an error message, or None when saved."""
        by_id = {e["id"]: e for e in self.entries()}
        edits = []
        for eid, v in changes.items():
            e = by_id.get(str(eid))
            if not e:
                return f"Unknown setting {eid} (the file changed - reload the page)"
            if e["kind"] == "bool":
                raw = "true" if v in (True, "true", "on", 1) else "false"
            elif e["kind"] == "number":
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    return f"{e['key']}: needs a number"
                raw = str(int(n)) if n.is_integer() and "." not in str(v) else str(n)
            elif e["kind"] in ("text", "secret"):
                if e["kind"] == "secret" and v == "":
                    continue                          # blank secret = keep the current one
                raw = json.dumps(str(v), ensure_ascii=False)
            else:
                raw = str(v).strip()
                if not self._value(raw)[0]:
                    return f"{e['key']}: that isn't valid (check brackets and quotes)"
            edits.append((e, raw))
        new = list(self.lines)
        for e, raw in sorted(edits, key=lambda x: -x[0]["start"]):
            line = e["indent"] + e["key"] + e["eq"] + raw + (("  " + e["comment"]) if e["comment"] else "")
            new[e["start"]:e["end"] + 1] = line.split("\n")
        text = "\n".join(new)
        try:
            tomllib.loads(text)
        except Exception as ex:
            return f"Not saved - the file would be invalid: {ex}"
        shutil.copyfile(self.path, self.path + ".bak")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)
        self.lines = new
        return None


def settings_allowed(handler, admin_password):
    """With [stream] admin_password: anyone who knows it. Without one: only from your own
    network, never through the internet tunnel."""
    if admin_password:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        return (handler.headers.get("X-Admin-Password") or qs.get("admin", [""])[0]) == admin_password
    if any(handler.headers.get(h) for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Tailscale-User-Login",
                                             "Tailscale-Funnel-Request", "X-Real-Ip")):
        return False
    ip = handler.client_address[0]
    try:
        import ipaddress
        return ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def make_handler(hub):
    password = hub.password
    admin_password = hub.cfg.get("stream", {}).get("admin_password", "")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _authed(self, qs):
            return not password or qs.get("key", [""])[0] == password

        def client_ip(self):
            return (self.headers.get("Cf-Connecting-Ip")
                    or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    or self.client_address[0])

        def _route(self, path):
            """'/c/<station>/rest' -> (station, '/rest');  old-style '/rest' -> (first station, '/rest')."""
            if path.startswith("/c/"):
                parts = path.split("/", 3)
                ch = hub.stations.get(parts[2]) if len(parts) > 2 else None
                return ch, "/" + (parts[3] if len(parts) > 3 else ""), len(parts) == 3
            return hub.default, path, False

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if not self._authed(qs):
                self.send_error(403, "Wrong or missing ?key=")
                return
            key = f"?key={urllib.parse.quote(password)}" if password else ""
            if u.path == "/":                                   # main screen: pick a station
                page = PICKER.replace("%TITLE%", html.escape(hub.title)).replace("%KEY%", key)
                return self.send(200, "text/html; charset=utf-8", page.replace("%STYLE%", STYLE_HEAD).encode())
            if u.path == "/health":
                return self.send(200, "text/html; charset=utf-8",
                                 health_page(hub, key).replace("%STYLE%", STYLE_HEAD).encode())
            if u.path == "/health.json":
                return self.send(200, "application/json", json.dumps({"checks": hub.health()}).encode())
            if u.path == "/settings":
                page = SETTINGS.replace("%TITLE%", html.escape(hub.title)).replace("%KEY%", key)
                return self.send(200, "text/html; charset=utf-8", page.replace("%STYLE%", STYLE_HEAD).encode())
            if u.path == "/settings.json":
                if not settings_allowed(self, admin_password):
                    return self.send(403, "application/json", json.dumps(
                        {"error": "admin" if admin_password else "local"}).encode())
                try:
                    items = TomlFile(CONFIG_PATH).entries()
                except OSError as e:
                    return self.send(500, "application/json", json.dumps({"error": str(e)}).encode())
                for it in items:
                    for k in ("start", "end", "indent", "eq", "comment"):
                        it.pop(k)
                return self.send(200, "application/json; charset=utf-8", json.dumps(
                    {"file": CONFIG_PATH, "items": items}, ensure_ascii=False).encode())
            if u.path == "/channels.json":
                return self.send(200, "application/json; charset=utf-8",
                                 json.dumps(hub.channels_info(), ensure_ascii=False).encode())
            ctx, path, bare = self._route(u.path)
            if not ctx:
                return self.send_error(404, "No such station")
            if bare:                                            # /c/rock -> /c/rock/
                self.send_response(301)
                self.send_header("Location", f"{ctx.base}/{key}")
                self.end_headers()
                return
            bc, station = ctx.bc, ctx.name
            if path in ("/stream", "/stream.mp3", "/live"):
                return self.stream(ctx, no_icy="noicy" in qs)
            if path == "/status.json":
                t = dict(bc.track)
                allow_skip = ctx.allow_skip
                t.pop("path", None)
                art_id = t.pop("art_id", None) or t.get("id") or "station"
                with ctx.state_lock:
                    cur, end = ctx.current_song, ctx.current_end
                if t.get("id") and cur:
                    t["duration"] = round(ctx.lib.duration(cur)[0])
                    t["elapsed"] = round(max(0.0, min(t["duration"], t["duration"] - (end - time.time()))))
                body = json.dumps({"station": station, "station_id": ctx.id, "now_playing": bc.title,
                                   "listeners": len(bc.listeners), "track": t,
                                   "rating": hub.ratings.get(t["id"]) if t.get("id") else None,
                                   "stars": hub.ratings.stars(t["id"]) if t.get("id") else None,
                                   "art": f"{ctx.base}/art?id={art_id}" + (f"&key={urllib.parse.quote(password)}" if password else ""),
                                   "previous": ctx.public(ctx.previous_path()),
                                   "next": ctx.public(ctx.next_path()),
                                   "allow_skip": allow_skip,
                                   }, ensure_ascii=False).encode()
                return self.send(200, "application/json; charset=utf-8", body)
            if path == "/art":
                data, ctype = ctx.art_for(qs.get("id", [""])[0])
                if not data:
                    return self.send_error(404, "No artwork")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                return self.wfile.write(data)
            if path == "/chat.json":
                chat = hub.ai.chat if hub.ai else None
                if not chat:
                    return self.send(200, "application/json; charset=utf-8",
                                     json.dumps({"off": True, "messages": []}).encode())
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                body = {"messages": chat.messages(ctx.id, since), "status": chat.status(),
                        "owner": settings_allowed(self, admin_password)}
                return self.send(200, "application/json; charset=utf-8",
                                 json.dumps(body, ensure_ascii=False).encode())
            if path == "/notes.json":
                out = {"now": None, "next": None}
                if hub.ai:
                    with ctx.state_lock:
                        cur = ctx.current_song
                    if cur:
                        out["now"] = hub.ai.notes_for(ctx.info_for(cur))
                    nxt = ctx.next_path()
                    if nxt:
                        out["next"] = hub.ai.notes_for(ctx.info_for(nxt))
                return self.send(200, "application/json; charset=utf-8",
                                 json.dumps(out, ensure_ascii=False).encode())
            if path == "/ratings.json":
                body = json.dumps(hub.ratings.data, indent=1, ensure_ascii=False).encode()
                return self.send(200, "application/json; charset=utf-8", body)
            if path in ("/listen.m3u", "/listen.pls"):
                host = self.headers.get("Host", "localhost")
                proto = self.headers.get("X-Forwarded-Proto", "http")
                body = f"#EXTM3U\n#EXTINF:-1,{station}\n{proto}://{host}{ctx.base}/stream{key}\n".encode()
                return self.send(200, "audio/x-mpegurl", body)
            if path == "/":
                page = (PAGE.replace("%STATION%", html.escape(station)).replace("%KEY%", key)
                        .replace("%BASE%", ctx.base))
                return self.send(200, "text/html; charset=utf-8", page.replace("%STYLE%", STYLE_HEAD).encode())
            self.send_error(404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if not self._authed(qs):
                return self.send_error(403, "Wrong or missing ?key=")
            if u.path == "/settings.json":
                if not settings_allowed(self, admin_password):
                    return self.send(403, "application/json", json.dumps({"error": "denied"}).encode())
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    changes = json.loads(self.rfile.read(n) or b"{}")
                    err = TomlFile(CONFIG_PATH).save(changes) if changes else None
                except (ValueError, OSError) as e:
                    err = str(e)
                if not err:
                    log.info("Settings saved from the web page (%d change%s); restart to apply",
                             len(changes), "" if len(changes) == 1 else "s")
                return self.send(400 if err else 200, "application/json",
                                 json.dumps({"ok": not err, "error": err}).encode())
            if u.path == "/restart":
                if not settings_allowed(self, admin_password):
                    return self.send(403, "application/json", json.dumps(
                        {"ok": False, "error": "Only the station owner can restart"}).encode())
                self.send(200, "application/json", json.dumps({"ok": True}).encode())
                t = threading.Timer(1.0, restart_station)   # let the reply go out first
                t.daemon = True
                t.start()
                return
            ctx, path, _ = self._route(u.path)
            if not ctx:
                return self.send_error(404, "No such station")
            bc = ctx.bc
            if path in ("/chat", "/chat/approve"):
                chat = hub.ai.chat if hub.ai else None
                if not chat:
                    return self.send(503, "application/json", json.dumps(
                        {"ok": False, "error": "Chat is off"}).encode())
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    req = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    req = {}
                if path == "/chat/approve":
                    if not settings_allowed(self, admin_password):
                        return self.send(403, "application/json", json.dumps(
                            {"ok": False, "error": "Only the station owner can do that"}).encode())
                    ok = chat.approve(ctx.id, req.get("id"), bool(req.get("ok", True)))
                    return self.send(200, "application/json", json.dumps({"ok": ok}).encode())
                res = chat.submit(ctx.id, req.get("name", ""), req.get("text", ""), self.client_ip())
                return self.send(200 if res.get("ok") else 429 if "try again" in str(res.get("error", ""))
                                 else 400, "application/json",
                                 json.dumps(res, ensure_ascii=False).encode())
            if path == "/skip":
                direction = qs.get("dir", ["next"])[0]
                if not ctx.allow_skip:
                    return self.send(403, "application/json", json.dumps(
                        {"ok": False, "error": "Skipping is off for this channel"}).encode())
                if direction == "upnext":
                    new = ctx.skip_upnext()
                    return self.send(200 if new else 409, "application/json",
                                     json.dumps({"ok": bool(new), "next": ctx.public(new)}).encode())
                if direction not in ("next", "back"):
                    return self.send_error(400, "dir must be next, back or upnext")
                ok = ctx.command(direction)
                return self.send(200 if ok else 409, "application/json", json.dumps({"ok": ok}).encode())
            if path != "/rate":
                return self.send_error(404)
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                req = {}
            rating = (req.get("rating") or qs.get("rating", [""])[0]).lower()
            tid = req.get("id") or qs.get("id", [""])[0] or bc.track.get("id")
            stars = req.get("stars", qs.get("stars", [None])[0])
            if stars is not None:
                try:
                    stars = int(stars)
                except (TypeError, ValueError):
                    stars = -1
                if not 0 <= stars <= 5:
                    return self.send_error(400, "stars must be 0-5")
                info = ctx.track_info(tid)
                if not info:
                    return self.send_error(409, "Nothing to rate right now (station break)")
                saved = hub.ratings.set_stars(info, stars)
                hub.plex.rate(info, saved or 0)
                if hub.ai:
                    hub.ai.rating_changed()
                if saved == 1:
                    ctx.drop_upcoming(tid)
                ctx.log.info("STARS %s: %s - %s", saved or "cleared", info.get("artist"), info.get("title"))
                if saved == 1 and tid == bc.track.get("id"):
                    ctx.command("scratch")     # 1 star: record scratch, then on to the next song
                return self.send(200, "application/json", json.dumps({"ok": True, "id": tid, "stars": saved}).encode())
            if rating not in ("up", "down"):
                return self.send_error(400, "rating must be up or down")
            info = ctx.track_info(tid)
            if not info:
                return self.send_error(409, "Nothing to rate right now (station break)")
            result = hub.ratings.vote(info, rating)
            if hub.ai:
                hub.ai.rating_changed()
            ctx.log.info("RATED %s: %s - %s", result or "cleared", info.get("artist"), info.get("title"))
            if result == "down" and tid == bc.track.get("id"):
                ctx.command("scratch")
            return self.send(200, "application/json", json.dumps({"ok": True, "id": tid, "rating": result}).encode())

        def send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def stream(self, ctx, no_icy=False):
            bc = ctx.bc
            icy = self.headers.get("Icy-MetaData") == "1" and not no_icy
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("icy-name", ctx.name)
            if icy:
                self.send_header("icy-metaint", str(ICY_METAINT))
            self.end_headers()
            q = bc.add()
            count, last_title = 0, None
            who = (self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                   or self.client_address[0])
            ua = self.headers.get("User-Agent", "?")
            kind = ("WANG Radio app (new)" if "WANGRadio" in ua else "WANG Radio app (OLD build)" if ("Dalvik" in ua or "ExoPlayer" in ua) else
                    "VLC" if "VLC" in ua else "browser" if "Mozilla" in ua else ua[:40])
            started, sent = time.time(), 0
            ctx.log.info("Listener connected: %s [%s] (%d total)", who, kind, len(bc.listeners))
            try:
                while True:
                    data = q.get(timeout=15)
                    sent += len(data)
                    if not icy:
                        self.wfile.write(data)
                        continue
                    while data:  # interleave ICY metadata every ICY_METAINT bytes
                        take = min(len(data), ICY_METAINT - count)
                        self.wfile.write(data[:take])
                        data, count = data[take:], count + take
                        if count == ICY_METAINT:
                            count = 0
                            if bc.title != last_title:
                                last_title = bc.title
                                m = f"StreamTitle='{bc.title.replace(chr(39), '')}';".encode()[:4000]
                                m += b"\x00" * (-len(m) % 16)
                                self.wfile.write(bytes([len(m) // 16]) + m)
                            else:
                                self.wfile.write(b"\x00")
            except Exception:
                pass
            finally:
                bc.remove(q)
                ctx.log.info("Listener left: %s [%s] after %.0f s (%d KB)", who, kind, time.time() - started, sent // 1024)
    return Handler


# ---------------------------------------------------------------- web pages (Parchment Brutalist design)
ICONS = {
 "back": '<svg viewBox="0 0 256 256"><path d="M224 128a8 8 0 0 1-8 8H59.3l58.4 58.3a8 8 0 0 1-11.4 11.4l-72-72a8 8 0 0 1 0-11.4l72-72a8 8 0 0 1 11.4 11.4L59.3 120H216a8 8 0 0 1 8 8z"/></svg>',
 "gear": '<svg viewBox="0 0 256 256"><path d="M128 80a48 48 0 1 0 48 48 48 48 0 0 0-48-48zm0 80a32 32 0 1 1 32-32 32 32 0 0 1-32 32zm109.9-52.8a8 8 0 0 0-3.9-5.4l-29.8-17-.1-33.6a8 8 0 0 0-2.8-6.1 111.9 111.9 0 0 0-36.7-20.6 8 8 0 0 0-6.5.6L128 41.9 98 25a8 8 0 0 0-6.5-.6 112 112 0 0 0-36.7 20.7 8 8 0 0 0-2.8 6.1l-.2 33.6-29.8 17a8 8 0 0 0-3.9 5.4 106.5 106.5 0 0 0 0 41.6 8 8 0 0 0 3.9 5.4l29.8 17 .1 33.6a8 8 0 0 0 2.8 6.1 111.9 111.9 0 0 0 36.7 20.6 8 8 0 0 0 6.5-.6l30.1-16.8 30 16.8a8 8 0 0 0 6.5.6 112 112 0 0 0 36.7-20.7 8 8 0 0 0 2.8-6.1l.2-33.6 29.8-17a8 8 0 0 0 3.9-5.4 106.5 106.5 0 0 0 0-41.5zm-15 34.9-28.6 16.3a8 8 0 0 0-3 3c-.6 1-1.2 2.1-1.8 3.1a8 8 0 0 0-1.2 4.1l-.2 32.2a96 96 0 0 1-24.2 13.6l-28.8-16.1a8 8 0 0 0-3.9-1h-4a8 8 0 0 0-4.1 1l-28.8 16.1a95.9 95.9 0 0 1-24.2-13.6l-.1-32.1a8 8 0 0 0-1.2-4.1c-.6-1-1.2-2-1.8-3.1a8 8 0 0 0-3-3l-28.6-16.3a90.5 90.5 0 0 1 0-27.2l28.5-16.3a8 8 0 0 0 3-3c.6-1 1.2-2.1 1.8-3.1a8 8 0 0 0 1.2-4.1l.2-32.2a96 96 0 0 1 24.2-13.6l28.8 16.1a8 8 0 0 0 4.1 1h3.8a8 8 0 0 0 4.1-1l28.8-16.1a95.9 95.9 0 0 1 24.2 13.6l.1 32.1a8 8 0 0 0 1.2 4.1c.6 1 1.2 2 1.8 3.1a8 8 0 0 0 3 3l28.6 16.3a90.5 90.5 0 0 1 .1 27.2z"/></svg>',
 "play": '<svg viewBox="0 0 256 256"><path id="pi" d="M240 128a15.7 15.7 0 0 1-7.6 13.5L88.3 229.6a16 16 0 0 1-24.3-13.5V39.9a16 16 0 0 1 24.3-13.5l144.1 88.1A15.7 15.7 0 0 1 240 128z"/></svg>',
 "prev": '<svg viewBox="0 0 256 256"><path d="M208 47.9v160.2a16 16 0 0 1-24.4 13.5L64 147.1V216a8 8 0 0 1-16 0V40a8 8 0 0 1 16 0v68.9l119.6-74.5A16 16 0 0 1 208 47.9z"/></svg>',
 "next": '<svg viewBox="0 0 256 256"><path d="M208 40v176a8 8 0 0 1-16 0v-68.9L72.4 221.6A16 16 0 0 1 48 208.1V47.9a16 16 0 0 1 24.4-13.5L192 108.9V40a8 8 0 0 1 16 0z"/></svg>',
 "star": '<svg viewBox="0 0 256 256"><path d="M234.3 114.9l-45 38.8 13.8 58a16.5 16.5 0 0 1-24.5 17.9L128 198.5l-50.6 31.1a16.5 16.5 0 0 1-24.5-17.9l13.8-58-45-38.8a16.5 16.5 0 0 1 9.4-29l59.1-4.8 22.7-55a16.4 16.4 0 0 1 30.4 0l22.7 55 59.1 4.8a16.5 16.5 0 0 1 9.4 29z"/></svg>',
 "ext": '<svg viewBox="0 0 256 256"><path d="M224 104a8 8 0 0 1-16 0V59.3l-66.3 66.4a8 8 0 0 1-11.4-11.4L196.7 48H152a8 8 0 0 1 0-16h64a8 8 0 0 1 8 8zm-40 24a8 8 0 0 0-8 8v72H48V80h72a8 8 0 0 0 0-16H48a16 16 0 0 0-16 16v128a16 16 0 0 0 16 16h128a16 16 0 0 0 16-16v-72a8 8 0 0 0-8-8z"/></svg>',
}

STYLE_HEAD = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;700&family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;1,9..144,300;1,9..144,400&display=swap" rel="stylesheet">
<meta name="theme-color" content="#F4EFE6">
<style>
:root{--primary:#FF8200;--on-primary:#4B4B4B;--surface:#F4EFE6;--card:#FFFFFF;--ink:#4B4B4B;--muted:#A3A099;--border:#111111;
 --serif:"Fraunces",Georgia,"Times New Roman",serif;--sans:"DM Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box;border-radius:0}
[hidden]{display:none!important}
html{background:var(--surface)}
body{margin:0;background:var(--surface);color:var(--ink);font:400 14px/1.45 var(--sans);-webkit-font-smoothing:antialiased}
a{color:inherit}
svg{width:24px;height:24px;fill:currentColor;flex:none}
.k{font:700 10px/1 var(--sans);letter-spacing:.2em;text-transform:uppercase}
.serif{font-family:var(--serif);font-weight:400}
.wrap{width:100%;max-width:480px;margin:0 auto;padding:24px 24px 48px}
.wide{max-width:980px}
.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 24px;border-bottom:3px solid var(--border);
 background:var(--surface);position:sticky;top:0;z-index:5}
.bar .who{display:flex;align-items:center;gap:12px;min-width:0}
.bar .who div{display:flex;flex-direction:column;gap:4px;min-width:0}
.bar .who i{font:italic 400 20px/1.15 var(--serif);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ib{width:48px;height:48px;border:3px solid var(--border);background:var(--card);display:grid;place-items:center;
 color:var(--ink);text-decoration:none;cursor:pointer;padding:0;flex:none;box-shadow:4px 4px 0 0 var(--border);transition:transform .08s,box-shadow .08s}
.ib:active{transform:translate(3px,3px);box-shadow:1px 1px 0 0 var(--border)}
.ib:disabled{color:var(--muted);cursor:default}
.card{background:var(--card);border:3px solid var(--border);box-shadow:4px 4px 0 0 var(--border)}
.card.hero{box-shadow:8px 8px 0 0 var(--border)}
.chip{display:inline-block;border:3px solid var(--border);background:var(--card);padding:6px 12px;font:700 10px/1 var(--sans);
 letter-spacing:.2em;text-transform:uppercase;color:var(--ink)}
.chip.on{background:var(--primary);color:var(--on-primary)}
.btn2{border:3px solid var(--border);background:var(--card);padding:12px 16px;box-shadow:4px 4px 0 0 var(--border);cursor:pointer;
 font:700 10px/1 var(--sans);letter-spacing:.2em;text-transform:uppercase;color:var(--ink);display:inline-flex;align-items:center;gap:8px;
 text-decoration:none;transition:transform .08s,box-shadow .08s}
.btn2:active{transform:translate(3px,3px);box-shadow:1px 1px 0 0 var(--border)}
.btn2 svg{width:16px;height:16px}
.press{transition:transform .08s,box-shadow .08s}
.press:active{transform:translate(6px,6px);box-shadow:2px 2px 0 0 var(--border)!important}
.notch{position:absolute;top:0;right:0;width:16px;height:16px;background:var(--border)}
.stripes{position:absolute;inset:0;opacity:.1;pointer-events:none;
 background-image:repeating-linear-gradient(45deg,#000 0 2px,transparent 2px 8px)}
#toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,20px);background:var(--card);border:3px solid var(--border);
 box-shadow:4px 4px 0 0 var(--border);padding:12px 16px;opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;
 max-width:calc(100% - 48px);font:700 10px/1.4 var(--sans);letter-spacing:.2em;text-transform:uppercase;z-index:20}
#toast.show{opacity:1;transform:translate(-50%,0)}
@media (max-width:420px){.wrap{padding:20px 16px 40px}.bar{padding:12px 16px}}
</style>"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%STATION%</title>
%STYLE%
<style>
.artcard{position:relative;margin-top:8px}
.art{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:var(--surface)}
.artcard .chip{position:absolute;top:16px;left:16px}
.np{margin-top:32px}
.title{font:400 32px/1.05 var(--serif);letter-spacing:-.02em;margin:10px 0 0;overflow:hidden;display:-webkit-box;
 -webkit-line-clamp:3;-webkit-box-orient:vertical}
.artist{margin:12px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prog{height:24px;border:3px solid var(--border);background:var(--surface);position:relative;margin-top:20px}
.prog .fill{position:absolute;top:0;left:0;height:100%;background:var(--primary);border-right:3px solid var(--border);
 min-width:0;transition:width .9s linear}
.prog .fill[style*="width: 0"]{border-right:0}
.time{display:flex;justify-content:space-between;margin-top:8px}
.controls{display:grid;grid-template-columns:64px 1fr 64px;gap:16px;align-items:stretch;margin-top:32px}
.controls .ib{width:64px;height:auto;min-height:64px}
#play{border:3px solid var(--border);background:var(--primary);color:var(--on-primary);box-shadow:8px 8px 0 0 var(--border);
 cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:16px 20px;text-align:left;min-height:96px}
#play .lbl{font:italic 400 32px/1 var(--serif);letter-spacing:-.02em}
#play .box{width:48px;height:48px;border:3px solid var(--border);background:var(--card);display:grid;place-items:center}
#play.on .box{background:var(--border);color:var(--card)}
.rate{margin-top:40px}
.stars{display:flex;border:3px solid var(--border);background:var(--card);margin-top:12px;box-shadow:4px 4px 0 0 var(--border)}
.stars button{flex:1;height:52px;border:0;border-right:3px solid var(--border);background:var(--card);color:var(--muted);
 cursor:pointer;display:grid;place-items:center;padding:0}
.stars button:last-child{border-right:0}
.stars button.on{background:var(--primary);color:var(--border)}
.stars button:disabled{cursor:default;opacity:.4}
.queue{margin-top:40px}
.qh{background:var(--border);color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center}
.qh i{font:italic 400 14px var(--serif)}
.q{display:flex;gap:16px;align-items:center;padding:16px 20px;border-top:3px solid var(--border);position:relative}
.q[hidden]{display:none}
.q img{width:52px;height:52px;object-fit:cover;border:3px solid var(--border);background:var(--surface);flex:none}
.q div{min-width:0}.q .btn2{flex:none;padding:10px 12px}
.q b{display:block;font:400 20px/1.2 var(--serif);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:6px}
.q span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:4px;color:var(--muted)}
.q.past{background:var(--surface);color:var(--muted)}
.q.past b{text-decoration:line-through;text-decoration-thickness:2px}
.q.past img{filter:grayscale(1)}
.foot{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;margin-top:40px}
.panel{margin-top:40px}
.ph{background:var(--border);color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.ph i{font:italic 400 14px var(--serif)}
.pb{padding:20px;border-top:3px solid var(--border)}
.note b{display:block;font:400 20px/1.25 var(--serif);margin-top:6px}
.note p{margin:10px 0 0}
.note .src{margin-top:12px}
.note .src a{color:var(--ink)}
.msgs{max-height:340px;overflow-y:auto}
.msg{padding:14px 20px;border-top:3px solid var(--border);position:relative}
.msg:first-child{border-top:0}
.msg .who{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.msg p{margin:8px 0 0;word-wrap:break-word;overflow-wrap:anywhere}
.msg.aired{background:var(--primary)}
.msg.blocked{background:var(--surface);color:var(--muted)}
.msg.blocked p{text-decoration:line-through;text-decoration-thickness:2px}
.msg .tag{white-space:nowrap}
.msg .ctl{display:flex;gap:8px;margin-top:12px}
.msg .ctl button{padding:8px 12px}
.say{display:flex;flex-direction:column;gap:12px;padding:20px;border-top:3px solid var(--border)}
.say .line{display:flex;gap:12px;min-width:0;flex-wrap:wrap}
.say input{font:400 14px/1.45 var(--sans);color:var(--ink);background:var(--card);border:3px solid var(--border);
 padding:10px 12px;min-width:0;outline:none;border-radius:0}
.say .hint{min-width:0}
.say input:focus{box-shadow:4px 4px 0 0 var(--border)}
.say #who{flex:1 1 7em}.say #what{flex:3 1 10em}
.say .hint{color:var(--muted)}
</style></head><body>
<header class="bar">
 <div class="who"><a class="ib" href="/%KEY%" title="All stations" aria-label="All stations">ICON_BACK</a>
  <div><span class="k">On the dial</span><i>%STATION%</i></div></div>
 <a class="ib" href="/settings%KEY%" title="Settings" aria-label="Settings">ICON_GEAR</a>
</header>
<main class="wrap">
<section class="card hero artcard"><img class="art" id="art" alt="Album artwork"><span class="chip on" id="status" hidden></span></section>
<section class="np"><div class="k">Now playing</div>
 <p class="title" id="title">%STATION%</p><p class="artist k" id="artist">&nbsp;</p>
 <div id="progBox" hidden><div class="prog" role="progressbar" aria-label="Song progress"><div class="fill" id="fill"></div></div>
 <div class="time k"><span id="elapsed">0:00</span><span id="length">0:00</span></div></div></section>
<div class="controls">
 <button class="ib" id="back" title="Previous song" aria-label="Previous song">ICON_PREV</button>
 <button id="play" class="press" title="Play" aria-label="Play"><span><span class="k" id="playk">Tune in</span><br><span class="lbl" id="playl">Listen <i>live</i></span></span>
  <span class="box"><svg viewBox="0 0 256 256"><path id="pi" d="M240 128a15.7 15.7 0 0 1-7.6 13.5L88.3 229.6a16 16 0 0 1-24.3-13.5V39.9a16 16 0 0 1 24.3-13.5l144.1 88.1A15.7 15.7 0 0 1 240 128z"/></svg></span></button>
 <button class="ib" id="next" title="Next song" aria-label="Next song">ICON_NEXT</button>
</div>
<section class="rate"><div class="k">Your rating</div><div class="stars" id="stars" role="group" aria-label="Star rating"></div></section>
<section class="card queue">
 <div class="qh"><span class="k">The running order</span><i>Before &amp; after</i></div>
 <div class="q past" id="prevRow" hidden><img id="prevArt" alt=""><div><span class="k">I. Previously</span><b id="prevTitle"></b><span id="prevArtist" class="k"></span></div></div>
 <div class="q" id="nextRow" hidden><img id="nextArt" alt=""><div style="flex:1"><span class="k">II. Up next</span><b id="nextTitle"></b><span id="nextArtist" class="k"></span></div>
  <button class="btn2" id="skipNext" title="Pick a different song to play next" aria-label="Skip the up next song">ICON_NEXT Skip</button></div>
</section>
<section class="card panel" id="notesPanel" hidden>
 <div class="ph"><span class="k">From the archive</span><i>Station notes</i></div>
 <div class="pb note" id="noteNow"></div>
 <div class="pb note" id="noteNext"></div>
</section>

<section class="card panel" id="chatPanel" hidden>
 <div class="ph"><span class="k">Station chat</span><i>Say hello, ask for a <b style="font-weight:400">vibe</b></i></div>
 <div class="msgs" id="msgs"></div>
 <form class="say" id="say">
  <div class="line"><input id="who" placeholder="Your name" maxlength="20" autocomplete="nickname">
   <input id="what" placeholder="Shoutout, or the mood you want&hellip;" maxlength="280" autocomplete="off"></div>
  <div class="line" style="justify-content:space-between;align-items:center">
   <span class="k hint" id="chatHint">Read on air by the DJ</span>
   <button class="btn2" type="submit" id="send">Send</button></div>
 </form>
</section>

<div class="foot"><span class="chip" id="listeners">&nbsp;</span>
 <a class="btn2" href="%BASE%/listen.m3u%KEY%">ICON_EXT Open in VLC</a></div>
</main><div id="toast"></div>
<audio id="audio" preload="none"></audio>
<script>
const KQ="%KEY%", BASE="%BASE%";
const U=(p,extra)=>{let q=KQ;if(extra)q+=(q?"&":"?")+extra;return BASE+p+q};
const $=id=>document.getElementById(id), audio=$("audio");
let cur=null, playing=false;
const PLAY="M240 128a15.7 15.7 0 0 1-7.6 13.5L88.3 229.6a16 16 0 0 1-24.3-13.5V39.9a16 16 0 0 1 24.3-13.5l144.1 88.1A15.7 15.7 0 0 1 240 128z",
 STOP="M216 56v144a16 16 0 0 1-16 16H56a16 16 0 0 1-16-16V56a16 16 0 0 1 16-16h144a16 16 0 0 1 16 16z";
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("show"),2500)}
function setImg(img,src){if(src&&img.dataset.src!==src){img.dataset.src=src;img.src=src}}
const fmt=s=>{s=Math.max(0,Math.round(s||0));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")};
function row(k,t){$(k+"Row").hidden=!t;if(!t)return;$(k+"Title").textContent=t.title;
 $(k+"Artist").textContent=[t.artist,t.duration?fmt(t.duration):""].filter(Boolean).join(" \\u00b7 ");setImg($(k+"Art"),t.art)}
let pos=null;   // {dur, el, at}: song length, elapsed, when we heard it
function paintProg(){if(!pos){$("progBox").hidden=true;return}$("progBox").hidden=false;
 const el=Math.min(pos.dur,pos.el+(Date.now()-pos.at)/1000);$("fill").style.width=(100*el/pos.dur).toFixed(2)+"%";
 $("elapsed").textContent=fmt(el);$("length").textContent=fmt(pos.dur)}
setInterval(paintProg,1000);
function status(t){const s=$("status");s.hidden=!t;s.textContent=t||""}
async function refresh(){
 try{const j=await (await fetch(U("/status.json"),{cache:"no-store"})).json();cur=j;
  document.querySelectorAll("#next,#back,#skipNext").forEach(e=>e.hidden=j.allow_skip===false);
  const t=j.track||{};$("title").textContent=t.title||j.now_playing;
  $("artist").textContent=[t.artist,t.album].filter(Boolean).join(" \\u00b7 ")||"\\u00a0";
  setImg($("art"),j.art);row("prev",j.previous);row("next",j.next);
  pos=t.duration?{dur:t.duration,el:t.elapsed||0,at:Date.now()}:null;paintProg();
  [...$("stars").children].forEach(b=>b.disabled=!t.id);paintStars(j.stars||0);
  $("listeners").textContent=j.listeners+" listening";
  if(!playing&&$("status").textContent==="Can't reach the station")status("");
  if("mediaSession" in navigator)navigator.mediaSession.metadata=new MediaMetadata({title:t.title||j.station,
   artist:t.artist||j.station,album:t.album||"",artwork:[{src:j.art,sizes:"512x512"}]});
 }catch(e){status("Can't reach the station")}}
function setPlaying(p){playing=p;$("pi").setAttribute("d",p?STOP:PLAY);$("play").classList.toggle("on",p);
 $("play").title=$("play").ariaLabel=p?"Stop":"Play";$("playk").textContent=p?"Tap to stop":"Tune in";
 $("playl").innerHTML=p?"On <i>air</i>":"Listen <i>live</i>";status(p?"\\u25cf On air":"")}
$("play").onclick=()=>{if(playing){audio.pause();audio.removeAttribute("src");audio.load();setPlaying(false)}
 else{audio.src=U("/stream","t="+Date.now());audio.play().catch(e=>toast("Couldn't start: "+e.message));setPlaying(true)}};
audio.addEventListener("waiting",()=>{if(playing)status("Tuning in\\u2026")});
audio.addEventListener("playing",()=>{if(playing)status("\\u25cf On air")});
audio.addEventListener("error",()=>{if(playing){status("Reconnecting\\u2026");
 setTimeout(()=>{if(playing){audio.src=U("/stream","t="+Date.now());audio.play().catch(()=>{})}},3000)}});
function retune(){if(!playing)return;audio.src=U("/stream","t="+Date.now());audio.play().catch(()=>{})}
async function skip(dir){const r=await fetch(U("/skip","dir="+dir),{method:"POST"}).catch(()=>null);
 toast(r&&r.ok?(dir==="next"?"Skipping\\u2026":"Going back\\u2026"):"Couldn't skip");
 if(r&&r.ok)setTimeout(retune,400);setTimeout(refresh,1500);setTimeout(refresh,4000)}
async function skipUpNext(){const r=await fetch(U("/skip","dir=upnext"),{method:"POST"}).catch(()=>null);
 if(!r||!r.ok)return toast("Couldn't change up next");const j=await r.json();row("next",j.next);toast("Up next changed")}
const STAR='ICON_STAR';
for(let i=1;i<=5;i++){const b=document.createElement("button");b.innerHTML=STAR;b.title=b.ariaLabel=i+" star"+(i>1?"s":"");
 b.onclick=()=>setStars(i);b.onmouseenter=()=>paintStars(i);b.onmouseleave=()=>paintStars(cur&&cur.stars||0);$("stars").appendChild(b)}
function paintStars(n){[...$("stars").children].forEach((b,i)=>b.classList.toggle("on",i<n))}
async function setStars(n){if(!cur||!cur.track||!cur.track.id)return toast("Nothing to rate during a station break");
 if(n===(cur.stars||0))n=0;
 const res=await fetch(U("/rate"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({id:cur.track.id,stars:n})}).catch(()=>null);
 if(!res||!res.ok)return toast("Couldn't save rating");const j=await res.json();cur.stars=j.stars;paintStars(j.stars||0);
 toast(j.stars===1?"1 star \\u2014 never again. Skipping":j.stars?j.stars+" stars saved":"Rating cleared");
 if(j.stars===1){setTimeout(refresh,1500);setTimeout(refresh,4000)}}
$("next").onclick=()=>skip("next");$("back").onclick=()=>skip("back");$("skipNext").onclick=skipUpNext;
if("mediaSession" in navigator){navigator.mediaSession.setActionHandler("nexttrack",()=>skip("next"));
 navigator.mediaSession.setActionHandler("previoustrack",()=>skip("back"))}
refresh();setInterval(refresh,5000);

/* ---- station notes: trivia and bios, written ahead of time ---- */
function paintNote(el,n,label){
 if(!n||!(n.trivia||n.bio)){el.hidden=true;return}
 el.hidden=false;
 const src=(n.sources||[]).slice(0,2).map((u,i)=>`<a href="${u}" target="_blank" rel="noopener noreferrer">Source ${i+1}</a>`).join(" \\u00b7 ");
 el.innerHTML=`<span class="k">${label}</span><b>${esc(n.title||"")} \\u00b7 ${esc(n.artist||"")}</b>`+
  (n.trivia?`<p>${esc(n.trivia)}</p>`:"")+(n.bio?`<p class="k" style="color:var(--muted)">${esc(n.bio)}</p>`:"")+
  (src?`<div class="src k">${src}</div>`:"")}
async function loadNotes(){
 try{const j=await (await fetch(U("/notes.json"),{cache:"no-store"})).json();
  paintNote($("noteNow"),j.now,"On air now");paintNote($("noteNext"),j.next,"Coming up");
  $("notesPanel").hidden=$("noteNow").hidden&&$("noteNext").hidden;
 }catch(e){}}
loadNotes();setInterval(loadNotes,20000);

/* ---- station chat ---- */
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));
let lastId=0,owner=false;
const STATE={checking:"Checking\\u2026",queued:"With the DJ",held:"Waiting for approval",
 aired:"On air",ok:"In the chat",blocked:"Not aired"};
try{$("who").value=localStorage.getItem("wang_name")||""}catch(e){}
function paintChat(list){
 const box=$("msgs"),bottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
 for(const m of list){
  let el=document.getElementById("m"+m.id);
  if(!el){el=document.createElement("div");el.id="m"+m.id;box.appendChild(el)}
  el.className="msg "+(m.state==="aired"?"aired":m.state==="blocked"?"blocked":"");
  el.innerHTML=`<div class="who"><span class="k">${esc(m.name)}</span>`+
   `<span class="k tag">${esc(STATE[m.state]||m.state)}</span></div>`+
   `<p>${esc(m.text)}</p>`+(m.note?`<div class="k" style="margin-top:8px;color:var(--muted)">${esc(m.note)}</div>`:"")+
   (owner&&m.state==="held"?`<div class="ctl"><button class="btn2" data-ok="1" data-id="${m.id}">Read it</button>`+
    `<button class="btn2" data-ok="0" data-id="${m.id}">Skip it</button></div>`:"");
  lastId=Math.max(lastId,m.id)}
 while(box.children.length>60)box.removeChild(box.firstChild);
 if(bottom)box.scrollTop=box.scrollHeight}
$("msgs").onclick=async e=>{const b=e.target.closest("button[data-id]");if(!b)return;
 await fetch(U("/chat/approve"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({id:+b.dataset.id,ok:b.dataset.ok==="1"})}).catch(()=>null);
 b.closest(".ctl").remove();setTimeout(loadChat,300)};
async function loadChat(){
 try{const j=await (await fetch(U("/chat.json","since="+lastId),{cache:"no-store"})).json();
  if(j.off){$("chatPanel").hidden=true;return}
  $("chatPanel").hidden=false;owner=!!j.owner;
  if(j.messages&&j.messages.length)paintChat(j.messages);
  const v=(j.status||{}).vibe||{};
  $("chatHint").textContent=((j.status||{}).requests&&v.indexed
   ?"Read on air \\u00b7 "+v.indexed+" tracks searchable":"Read on air by the DJ");
 }catch(e){}}
$("say").onsubmit=async e=>{e.preventDefault();
 const text=$("what").value.trim();if(!text)return;
 const name=$("who").value.trim();try{localStorage.setItem("wang_name",name)}catch(x){}
 $("send").disabled=true;
 const r=await fetch(U("/chat"),{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({name:name,text:text})}).catch(()=>null);
 $("send").disabled=false;
 const j=r?await r.json().catch(()=>({})):{};
 if(j.ok){$("what").value="";loadChat();setTimeout(loadChat,2500);setTimeout(loadChat,6000)}
 else toast(j.error||"Couldn\'t send that")};
loadChat();setInterval(loadChat,4000);
</script></body></html>"""


PICKER = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%TITLE%</title>
%STYLE%
<style>
.hello{margin:8px 0 40px}
.hello h1{font:400 48px/1.05 var(--serif);letter-spacing:-.02em;margin:12px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:32px 24px}
a.st{display:block;color:inherit;text-decoration:none;position:relative;background:var(--card);border:3px solid var(--border);
 box-shadow:8px 8px 0 0 var(--border)}
a.st .img{height:192px;border-bottom:3px solid var(--border);position:relative;overflow:hidden;background:var(--surface)}
a.st img{width:100%;height:100%;object-fit:cover;display:block;filter:grayscale(1);transition:filter .5s}
a.st:hover img{filter:grayscale(0)}
a.st .chip{position:absolute;top:16px;left:16px}
.info{padding:20px}
.name{font:400 24px/1.15 var(--serif);letter-spacing:-.01em;margin-top:8px}
.np{margin-top:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
</style></head><body>
<header class="bar">
 <div class="who"><div><span class="k">Live from home</span><i>%TITLE%</i></div></div>
 <a class="ib" href="/settings%KEY%" title="Settings" aria-label="Settings">ICON_GEAR</a>
</header>
<main class="wrap wide">
<section class="hello"><div class="k">Choose a station</div><h1>Pick a <i>frequency.</i></h1></section>
<div class="grid" id="grid"></div></main>
<script>
const KQ="%KEY%";
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ROMAN=["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"];
async function load(){try{const list=await (await fetch("/channels.json"+KQ,{cache:"no-store"})).json();
 document.getElementById("grid").innerHTML=list.map((c,i)=>`<a class="st press" href="${c.url}${KQ}"><span class="notch"></span>
  <div class="img"><img src="${c.art}" alt=""><span class="chip">\\u25cf Live \\u00b7 ${c.listeners}</span></div>
  <div class="info"><div class="k">${ROMAN[i]||i+1}. Station</div><div class="name">${esc(c.name)}</div>
  <div class="np k">${esc(c.now_playing)}</div></div></a>`).join("");
 }catch(e){}}
load();setInterval(load,10000);
</script></body></html>"""


SETTINGS = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Settings &middot; %TITLE%</title>
%STYLE%
<style>
.hello{margin:8px 0 24px}
.hello h1{font:400 48px/1.05 var(--serif);letter-spacing:-.02em;margin:12px 0 0}
.hello p{margin:16px 0 0;max-width:560px}
.tools{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:40px}
input,textarea,select{font:400 14px/1.45 var(--sans);color:var(--ink);background:var(--card);border:3px solid var(--border);
 padding:10px 12px;width:100%;outline:none}
input:focus,textarea:focus{box-shadow:4px 4px 0 0 var(--border)}
textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px;min-height:44px;resize:vertical}
#find{max-width:320px}
.group{margin-bottom:40px}
.gh{background:var(--border);color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.gh i{font:italic 400 16px var(--serif)}
.item{padding:20px;border-top:3px solid var(--border);display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:8px 24px;
 position:relative}
.item:first-of-type{border-top:0}
.item .name{font:400 20px/1.2 var(--serif);word-break:break-word}
.item .help{color:var(--muted);margin-top:6px}
.item.changed{background:var(--primary)}
.item.changed .help{color:var(--on-primary)}
.item.changed::before{content:"";position:absolute;inset:0;opacity:.1;pointer-events:none;
 background-image:repeating-linear-gradient(45deg,#000 0 2px,transparent 2px 8px)}
.item > *{position:relative}
.seg{display:flex;width:max-content;border:3px solid var(--border)}
.seg button{border:0;background:var(--card);padding:12px 18px;font:700 10px/1 var(--sans);letter-spacing:.2em;
 text-transform:uppercase;cursor:pointer;color:var(--muted)}
.seg button+button{border-left:3px solid var(--border)}
.seg button.on{background:var(--border);color:#fff}
.save{position:sticky;bottom:0;background:var(--surface);border-top:3px solid var(--border);padding:16px 24px;
 display:flex;justify-content:space-between;align-items:center;gap:16px;z-index:5}
#save{border:3px solid var(--border);background:var(--primary);color:var(--on-primary);box-shadow:8px 8px 0 0 var(--border);
 padding:14px 22px;cursor:pointer;font:italic 400 24px/1 var(--serif);letter-spacing:-.01em}
#save:disabled{background:var(--card);color:var(--muted);cursor:default;box-shadow:4px 4px 0 0 var(--border)}
.gate{padding:20px;margin-bottom:40px}
.gate h2{font:400 24px/1.15 var(--serif);margin:12px 0}
.gate form{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap}.gate input{flex:1;min-width:180px}
@media (max-width:640px){.item{grid-template-columns:1fr}.hello h1{font-size:40px}}
</style></head><body>
<header class="bar">
 <div class="who"><a class="ib" href="/%KEY%" title="Back to the stations" aria-label="Back">ICON_BACK</a>
  <div><span class="k">Station desk</span><i>%TITLE%</i></div></div>
</header>
<main class="wrap wide">
<section class="hello"><div class="k">radio.toml</div><h1>The <i>settings.</i></h1>
 <p>Every setting from your station's config file. Changes are written to the file (a backup is kept as radio.toml.bak)
 and take effect after you <b>restart the station</b>.</p></section>
<div id="gate"></div>
<div class="tools" id="tools" hidden><input id="find" placeholder="Find a setting&hellip;" aria-label="Find a setting">
 <span class="chip" id="count"></span><span class="k" id="file"></span></div>
<div id="groups"></div>
</main>
<div class="save" id="savebar" hidden><span class="k" id="msg">No changes</span><button id="restart" class="press" type="button">Restart <i>station</i></button><button id="save" class="press" disabled>Save <i>changes</i></button></div>
<div id="toast"></div>
<script>
const KQ="%KEY%", $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ROMAN=["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"];
let admin="", changes={}, items=[];
try{admin=sessionStorage.getItem("radio_admin")||""}catch(e){}
function toast(t){const e=$("toast");e.textContent=t;e.classList.add("show");clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("show"),3500)}
function nice(t){if(!t)return "General";const [n,i]=t.split("#");const name=n.split(".").map(w=>w.replace(/_/g," ")).join(" \\u203a ");
 return i?name+" "+(ROMAN[i-1]||i):name}
async function load(){
 const r=await fetch("/settings.json"+KQ,{cache:"no-store",headers:admin?{"X-Admin-Password":admin}:{}}).catch(()=>null);
 if(!r){$("gate").innerHTML='<section class="card gate"><div class="k">Offline</div><h2>Can\\'t reach the <i>station.</i></h2></section>';return}
 if(r.status===403){const j=await r.json().catch(()=>({}));gate(j.error);return}
 const j=await r.json();items=j.items;$("gate").innerHTML="";$("tools").hidden=false;$("savebar").hidden=false;
 $("file").textContent=j.file;render()}
function gate(why){
 if(why==="local"){$("gate").innerHTML=`<section class="card gate"><div class="k">Locked</div>
  <h2>Settings open only on your <i>home network.</i></h2><p>To change settings from anywhere, add
  <b>admin_password = "\\u2026"</b> under <b>[stream]</b> in radio.toml and restart the station.</p></section>`;return}
 $("gate").innerHTML=`<section class="card gate hero"><div class="k">Station desk</div><h2>Admin <i>password.</i></h2>
  <form id="af"><input type="password" id="ap" placeholder="admin_password from radio.toml" autocomplete="current-password">
  <button class="btn2" type="submit">Unlock</button></form></section>`;
 $("af").onsubmit=e=>{e.preventDefault();admin=$("ap").value;try{sessionStorage.setItem("radio_admin",admin)}catch(x){}
  if(why==="admin"&&admin)load()}}
function field(it){
 const v=it.id in changes?changes[it.id]:it.value;
 if(it.kind==="bool")return `<div class="seg" data-id="${it.id}"><button type="button" class="${v?"on":""}" data-v="1">On</button><button type="button" class="${v?"":"on"}" data-v="0">Off</button></div>`;
 if(it.kind==="number")return `<input type="number" step="any" data-id="${it.id}" value="${esc(v)}">`;
 if(it.kind==="secret")return `<input type="password" data-id="${it.id}" value="${esc(it.id in changes?v:"")}" placeholder="${it.is_set?"\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022 (unchanged) \\u2014 type to replace":"Not set"}" autocomplete="new-password">`;
 if(it.kind==="raw")return `<textarea data-id="${it.id}" rows="${Math.min(10,String(v).split("\\n").length)}" spellcheck="false">${esc(v)}</textarea>`;
 return `<input type="text" data-id="${it.id}" value="${esc(v)}" spellcheck="false">`}
function render(){
 const q=$("find").value.toLowerCase(), groups={};
 for(const it of items){if(q&&!(it.key+" "+it.table+" "+it.help).toLowerCase().includes(q))continue;(groups[it.table]=groups[it.table]||[]).push(it)}
 const keys=Object.keys(groups);
 $("groups").innerHTML=keys.map((t,i)=>`<section class="card group"><div class="gh"><span class="k">${ROMAN[i]||i+1}. ${esc(t?(t.includes("#")?"[["+t.split("#")[0]+"]] #"+t.split("#")[1]:"["+t+"]"):"top of file")}</span><i>${esc(nice(t))}</i></div>
  ${groups[t].map(it=>`<div class="item${it.id in changes?" changed":""}" id="it${it.id}"><div><div class="k">${esc(it.kind==="raw"?"list / table":it.kind)}</div>
  <div class="name">${esc(it.key.replace(/_/g," "))}</div>${it.help?`<div class="help">${esc(it.help)}</div>`:""}</div><div>${field(it)}</div></div>`).join("")}</section>`).join("")
  ||'<section class="card gate"><h2>Nothing <i>matches.</i></h2></section>';
 $("count").textContent=items.length+" settings";bind();sync()}
function mark(id,val){const it=items.find(x=>x.id===id);const same=it.kind==="secret"?val==="":String(val)===String(it.value);
 if(same)delete changes[id];else changes[id]=val;$("it"+id).classList.toggle("changed",!same);sync()}
function bind(){
 document.querySelectorAll(".seg").forEach(s=>s.querySelectorAll("button").forEach(b=>b.onclick=()=>{
  s.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));mark(s.dataset.id,b.dataset.v==="1")}));
 document.querySelectorAll("input[data-id],textarea[data-id]").forEach(e=>e.oninput=()=>mark(e.dataset.id,e.value))}
function sync(){const n=Object.keys(changes).length;$("save").disabled=!n;
 $("msg").textContent=n?n+" unsaved change"+(n>1?"s":""):"No changes"}
$("find").oninput=render;
$("save").onclick=async()=>{const r=await fetch("/settings.json"+KQ,{method:"POST",
  headers:Object.assign({"Content-Type":"application/json"},admin?{"X-Admin-Password":admin}:{}),body:JSON.stringify(changes)}).catch(()=>null);
 const j=r?await r.json().catch(()=>({})):{};
 if(!r||!j.ok)return toast(j.error||"Couldn't save");
 changes={};toast("Saved \\u2014 restart the station to apply");load()};
$("restart").onclick=async()=>{
 if(Object.keys(changes).length&&!confirm("You have unsaved changes. Restart anyway and lose them?"))return;
 const r=await fetch("/restart"+KQ,{method:"POST",headers:admin?{"X-Admin-Password":admin}:{}}).catch(()=>null);
 const j=r?await r.json().catch(()=>({})):{};
 if(!r||!j.ok)return toast(j.error||"Couldn't restart");
 toast("Restarting... this page reconnects in a few seconds");
 setTimeout(()=>location.reload(),8000)};
window.addEventListener("beforeunload",e=>{if(Object.keys(changes).length){e.preventDefault();e.returnValue=""}});
load();
</script></body></html>"""

HEALTH = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Health &middot; %TITLE%</title>
%STYLE%
</head><body>
<header class="bar"><div class="who"><div><span class="k">Station desk</span><i>%TITLE%</i></div></div>
<nav class="tabs"><a href="/%KEY%">Stations</a><a href="/settings%KEY%">Settings</a><a class="on" href="/health%KEY%">Health</a></nav></header>
<main class="wrap wide">
<section class="hello"><div class="k">Health</div><h1>What's <i>up.</i></h1>
<p>Each piece the station depends on. This page refreshes every 30 seconds. A piece that is OFF is not in use.</p></section>
%ROWS%
</main></body></html>"""


def health_page(hub, key):
    out = []
    for r in hub.health():
        state = "up" if r["up"] else "down" if r["up"] is False else "off"
        out.append(f'<section class="card item {state}"><div><div class="k">{state.upper()}</div>'
                   f'<div class="name">{html.escape(r["name"])}</div></div>'
                   f'<div class="help">{html.escape(r["detail"])}</div></section>')
    return (HEALTH.replace("%TITLE%", html.escape(hub.title)).replace("%KEY%", key)
            .replace("%ROWS%", "".join(out)))


for _name, _svg in ICONS.items():
    PAGE = PAGE.replace("ICON_" + _name.upper(), _svg)
    PICKER = PICKER.replace("ICON_" + _name.upper(), _svg)
    SETTINGS = SETTINGS.replace("ICON_" + _name.upper(), _svg)


# ---------------------------------------------------------------- tunnel (CG-NAT bypass)
class Tunnel:
    """Makes the station reachable from the internet without port forwarding.
    provider = "cloudflare"  -> free Cloudflare quick tunnel (random trycloudflare.com URL, changes on restart)
               "cloudflare" + token -> your own named Cloudflare tunnel (permanent URL on your domain)
               "tailscale"   -> Tailscale Funnel (permanent https://<pc>.<tailnet>.ts.net URL, free)"""
    def __init__(self, cfg, port, password, notify=None, station="HomeRadio"):
        self.cfg, self.port, self.password = cfg, port, password
        self.notify, self.station = notify or {}, station
        self.provider = cfg.get("provider", "none").lower()
        self.url, self.proc = None, None

    def start(self):
        if self.provider in ("", "none", "off"):
            return
        try:
            if self.provider == "cloudflare":
                self._cloudflare()
            elif self.provider == "tailscale":
                self._tailscale()
            else:
                log.error("Unknown tunnel provider '%s'", self.provider)
        except Exception as e:
            log.error("Tunnel failed to start: %s", e)

    def _exe(self, name):
        path = self.cfg.get(f"{name}_path") or shutil.which(name)
        if not path:
            hint = {"cloudflared": "winget install Cloudflare.cloudflared",
                    "tailscale": "winget install Tailscale.Tailscale"}[name]
            raise RuntimeError(f"{name} not found. Install it ({hint}) and open a new terminal.")
        return path

    def _cloudflare(self):
        exe = self._exe("cloudflared")
        token = self.cfg.get("cloudflare_token", "")
        if token:
            args = [exe, "tunnel", "--no-autoupdate", "run", "--token", token]
            host = self.cfg.get("public_hostname", "")
            self.url = f"https://{host}" if host else None
        else:
            args = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]
        # QUIC (UDP) tunnels keep dropping behind CG-NAT and many home routers, which cuts every
        # listener off every few seconds. HTTP/2 over TCP is much steadier for long audio streams.
        proto = self.cfg.get("cloudflare_protocol", "http2")
        if proto and proto != "auto":
            args[2:2] = ["--protocol", proto]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        found = threading.Event()

        def watch():
            import re
            announced = None
            for line in self.proc.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not token:
                    self.url = m.group(0)
                    if self.url != announced:          # only once per link (no repeat pushes)
                        announced = self.url
                        self._announce()
                    found.set()
                elif "Registered tunnel connection" in line and token:
                    if announced is None:
                        announced = "token"
                        self._announce()
                    found.set()
                elif " ERR " in line or "Retrying connection" in line:
                    log.warning("Tunnel: %s", line.strip()[:200])
            log.warning("cloudflared exited; restarting tunnel in 10 s")
            time.sleep(10)
            self._cloudflare()
        threading.Thread(target=watch, daemon=True).start()
        found.wait(30)

    def _tailscale(self):
        exe = self._exe("tailscale")
        r = subprocess.run([exe, "funnel", "--bg", str(self.port)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() +
                               "  (Make sure you're logged in to Tailscale and Funnel is enabled for your tailnet.)")
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)
        self.url = "https://" + st["Self"]["DNSName"].rstrip(".")
        self._announce()

    def _announce(self):
        if not self.url:
            log.info("Tunnel connected (see your Cloudflare dashboard for the hostname)")
            return
        key = f"?key={urllib.parse.quote(self.password)}" if self.password else ""
        msg = (f"\n{'=' * 64}\n  PUBLIC STATION LINK (works anywhere, through CG-NAT):\n"
               f"    Web player : {self.url}/{key}\n    Stream URL : {self.url}/stream{key}\n{'=' * 64}")
        log.info(msg)
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_url.txt"), "w") as f:
                f.write(f"{self.url}/{key}\n{self.url}/stream{key}\n")
        except OSError:
            pass
        app_link = "wangradio://setup?" + urllib.parse.urlencode({"url": self.url, "key": self.password})
        self._push(f"{self.url}/{key}", f"Player: {self.url}/{key}\nStream: {self.url}/stream{key}", app_link)
        if not self.password:
            log.warning("Your station is PUBLIC with no password. Set [stream] password in radio.toml.")

    def _push(self, click, body, app_link=None):
        """Send the links to your phone with ntfy (free app: ntfy.sh)."""
        n = self.notify
        topic = n.get("ntfy_topic", "").strip()
        if not n.get("enabled", True) or not topic:
            return
        server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        headers = {"Title": f"{self.station} is on the air", "Click": click,
                   "Tags": "radio", "User-Agent": "HomeRadio/1.0"}
        if app_link:
            headers["Actions"] = f"view, Open in WANG Radio, {app_link}; view, Web player, {click}"
        if n.get("ntfy_token"):
            headers["Authorization"] = f"Bearer {n['ntfy_token']}"
        try:
            req = urllib.request.Request(f"{server}/{urllib.parse.quote(topic)}",
                                         data=body.encode("utf-8"), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            log.info("Sent station links to your phone (ntfy topic '%s')", topic)
        except Exception as e:
            log.warning("Phone notification failed: %s", e)

    def stop(self):
        if self.proc:
            self.proc.terminate()


# ---------------------------------------------------------------- the station clock
def spoken_time(t):
    h = t.hour % 12 or 12
    ampm = "in the morning" if t.hour < 12 else ("in the afternoon" if t.hour < 17 else
           ("in the evening" if t.hour < 21 else "at night"))
    if t.minute == 0:
        return f"It's {h} o'clock {ampm}."
    return f"It's {h}:{t.minute:02d} {ampm}." if t.minute >= 10 else f"It's {h} oh {t.minute} {ampm}."


class StationLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['name']}] {msg}", kwargs


class Station:
    def __init__(self, cfg, hub, ch):
        self.cfg, self.hub = cfg, hub
        self.id, self.name = ch["id"], ch["name"]
        self.base = f"/c/{self.id}"
        self.log = StationLog(log, {"name": self.name}) if len(hub.channel_cfgs) > 1 else log
        self.tagline = ch["spoken_id"]
        here = os.path.dirname(os.path.abspath(__file__))
        cache = "library_cache.json" if ch.get("first") else f"library_cache_{self.id}.json"
        track_filter = self._build_filter(ch, hub)
        self.lib = Library(ch["music"], cfg.get("library", {}).get("rescan_minutes", 30),
                           os.path.join(here, cache), track_filter=track_filter)
        self.dj_breaks = bool(ch.get("dj_breaks", True))
        self.effects_dir = ch.get("effects", "")
        self.effects_per_hour = float(cfg.get("ai", {}).get("effects_per_hour", 6))
        self.effect_times = collections.deque()    # when each effect last played (for effects_per_hour)
        self.ids_dir = ch["station_ids"]
        self.id_interval = float(ch["id_interval"])
        self.id_script = ch["id_script"]
        self.last_id_at, self.last_line = 0.0, None
        self.weather, self.news, self.tts = hub.weather, hub.news, hub.tts
        self.ratings, self.plex = hub.ratings, hub.plex
        self.ai = hub.ai
        choice = ch.get("persona", "default")
        if self.ai and choice == "random":
            choice = list(self.ai.configured)
        self.persona_choices = [str(p) for p in choice] if isinstance(choice, list) else [str(choice)]
        self.persona = self.persona_choices[0]     # who presents the next break
        self.talk = None                           # DJ talk / phone-in being prepared for the next break
        self.phone_busy = False
        clock = cfg.get("clock", {})
        self.window = clock.get("time_window_minutes", 4)
        self.fit = clock.get("fit_top_of_hour", True)
        audio = cfg.get("audio", {})
        self.normalize = audio.get("normalize", True)
        # Speech loudness: a touch above the music (-16 LUFS when normalizing), or near typical
        # mastered-music loudness when songs play as-is. voice_boost_db nudges it either way.
        self.voice_target = float(audio.get("voice_lufs", -15 if self.normalize else -11)) + \
            float(audio.get("voice_boost_db", 0))
        self.voice_target = max(-30.0, min(-5.0, self.voice_target))
        # Voice-over breaks: the DJ talks over the start of the next song with the music ducked.
        aicfg = cfg.get("ai", {})
        self.voiceover_chance = float(aicfg.get("voiceover_chance", 0.3))
        self.duck_db = float(audio.get("duck_db", -12))
        self.duck_mode = str(audio.get("duck_mode", "sidechain")).lower()   # "sidechain" or "envelope"
        # Quiet-intro check for voice-overs (a loudness guide, not vocal detection). Off by default.
        self.voiceover_quiet_only = bool(aicfg.get("voiceover_quiet_only", False))
        self.voiceover_max_intro_db = float(aicfg.get("voiceover_max_intro_db", -24))
        self._intro_cache = {}
        # Crossfades between songs, in seconds (0 = off). Used only when no DJ talk is due.
        self.crossfade = float(audio.get("crossfade_seconds", 0))
        self.head_skip = None                  # (song, seconds) already played by a crossfade
        # Break batching: the queue keeps this many songs, so the DJ can plan their breaks in one call
        self.lookahead = max(1, int(aicfg.get("break_batch", 1))) if self.ai else 1
        self.voiceover_delay = float(audio.get("voiceover_delay", 1.0))
        self.voiceover = None          # (song, wav, text, persona) waiting for its song
        s = cfg.get("stream", {})
        self.bc = Broadcaster(burst_seconds=float(s.get("burst_seconds", 4)),
                              kbps=int(str(s.get("bitrate", "128k")).rstrip("k")))
        # Only play while someone is listening (stop a little after the last one leaves).
        self.idle_when_empty = bool(s.get("play_only_when_listening", True))
        self.idle_grace = float(s.get("stop_after_seconds", 15))
        self.skip_sounds = bool(cfg.get("controls", {}).get("skip_sounds", True))
        self.allow_skip = bool(ch.get("allow_skip", True))     # false = a "radio, not a playlist" channel
        self.enc = Encoder(self.bc, s.get("bitrate", "128k"))
        self.done = {"top": None, "half": None, "quarter": None}
        self.last_id = None
        self.use_ratings = cfg.get("ratings", {}).get("influence_rotation", True)
        self.logo = ch["logo"] if ch["logo"] and os.path.isfile(ch["logo"]) else None
        self.logos_dir = ch["logos"]
        self.logo_picks = collections.OrderedDict()   # "logo-N" -> chosen logo file
        self.logo_count, self.last_logo = 0, None
        self.known = collections.OrderedDict()     # id -> song info (for ratings, artwork)
        self.password = s.get("password", "")
        self.assets_dir = hub.assets_dir
        self.state_lock = threading.RLock()
        self.interrupt = threading.Event()
        self.pending = None
        self.history = []                          # songs played, oldest first
        self.upcoming = collections.deque()        # songs queued to play next
        self.current_song, self.current_end = None, 0.0
        self.art_cache = collections.OrderedDict()
        self.bc.track = {"id": None, "title": self.name, "artist": "", "album": "", "kind": "station"}

    @staticmethod
    def _build_filter(ch, hub):
        """Build a callable(path, info)->bool for genre/artist/star channel filters."""
        genre = (ch.get("filter_genre") or "").strip().lower()
        artist = (ch.get("filter_artist") or "").strip().lower()
        min_stars = int(ch.get("filter_stars") or 0)
        if not genre and not artist and not min_stars:
            return None
        # stars come from the station's own ratings, or from Plex's (which also has ratings
        # you made in Plexamp). Plex's list is loaded by the AI module, so it may be empty
        # for the first few minutes after startup.
        from_plex = (ch.get("filter_stars_from") or "station").lower() == "plex"
        plex = hub.ai.rotation.plex if (from_plex and hub.ai) else None
        ratings = None if from_plex else hub.ratings

        def accept(path, info):
            if genre and genre not in (info.get("genre") or "").lower():
                return False
            if artist and artist not in (info.get("artist") or "").lower():
                return False
            if min_stars:
                if from_plex:
                    track = plex.find(path) if plex else None
                    s = track.get("stars", 0) if track else 0
                else:
                    s = ratings.stars(info.get("id") or track_id(path))
                if not s or s < min_stars:
                    return False
            return True
        return accept

    def leveled_effect(self, path):
        """The clip at the voice loudness, as a cached WAV (made once per file; the original
        file is never changed). Falls back to the original if ffmpeg can't do it."""
        try:
            st = os.stat(path)
            key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{self.voice_target}".encode()).hexdigest()
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "effects_cache")
            os.makedirs(cache_dir, exist_ok=True)
            out = os.path.join(cache_dir, key[:16] + ".wav")
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
            tmp = out + ".tmp.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path,
                            "-af", f"loudnorm=I={self.voice_target:.1f}:TP=-1.5:LRA=11,"
                                   "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo",
                            "-ar", "44100", "-ac", "2", tmp],
                           capture_output=True, timeout=60, check=True)
            os.replace(tmp, out)
            return out
        except Exception as e:
            self.log.warning("Couldn't level effect %s (%s); playing it as it is", os.path.basename(path), e)
            return path

    def random_effect(self):
        """Pick a random sound effect from the effects folder, or None."""
        if not self.effects_dir or not os.path.isdir(self.effects_dir):
            return None
        files = [os.path.join(self.effects_dir, n)
                 for n in os.listdir(self.effects_dir)
                 if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith(".")]
        return random.choice(files) if files else None

    def effects_left(self):
        """How many more effects may play this hour (effects_per_hour is the cap)."""
        now = time.time()
        while self.effect_times and now - self.effect_times[0] > 3600:
            self.effect_times.popleft()
        return max(0, int(self.effects_per_hour) - len(self.effect_times))

    def note_effect(self):
        self.effect_times.append(time.time())

    def effect_gaps(self, persona_chance):
        """Roll for a DJ break: how many sound effects to place inside it (0, 1 or 2)."""
        if not self.effects_dir or self.effects_left() < 1:
            return 0
        if random.random() >= persona_chance:
            return 0
        return min(2, self.effects_left())

    def maybe_play_effect(self, persona_chance):
        """A sound effect on its own, between songs (no DJ talk this time)."""
        if self.effect_gaps(persona_chance) < 1:
            return False
        fx = self.random_effect()
        if not fx:
            return False
        self.log.info("EFFECT: %s", os.path.basename(fx))
        self.note_effect()
        self.play(self.leveled_effect(fx), voice=True)
        if not self.interrupt.is_set():
            self.enc.silence(0.3)
        return True

    def talk_with_effects(self, text, persona, out, gaps):
        """Voice a DJ break with sound effects between its parts, e.g.
        "this is Bob," [effect] "up next is," [effect] "a great song."
        Writes one WAV to `out`. Returns False if any part fails."""
        parts = split_for_effects(text, gaps)
        seq, temps = [], []
        try:
            for i, part in enumerate(parts):
                if i:
                    fx = self.random_effect()
                    if fx:
                        seq.append(self.leveled_effect(fx))
                        self.note_effect()
                fd, p = tempfile.mkstemp(suffix=".wav", prefix="homeradio_part_")
                os.close(fd)
                temps.append(p)
                if not self.ai.speak(part, persona, p):
                    return False
                seq.append(p)
            if len(seq) == 1:
                os.replace(seq[0], out)
                return True
            splice_wavs(seq, out)
            return True
        except Exception as e:
            self.log.warning("DJ break with sound effects failed: %s", e)
            return False
        finally:
            for p in temps:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # -- song info / history / up next
    def info_for(self, path):
        _, title, info = self.lib.duration(path)
        info = dict(info, path=path, kind="song", display=title)
        self.known[info["id"]] = info
        while len(self.known) > 300:
            self.known.popitem(last=False)
        return info

    def set_track(self, info):
        self.bc.track = info
        if info.get("id"):
            self.known[info["id"]] = info

    def track_info(self, tid):
        return self.known.get(tid) if tid else None

    def public(self, path):
        """Short song description for the app (no file paths)."""
        if not path:
            return None
        i = self.info_for(path)
        key = f"&key={urllib.parse.quote(self.password)}" if self.password else ""
        return {"id": i["id"], "title": i["title"], "artist": i["artist"], "album": i["album"],
                "duration": round(self.lib.duration(path)[0]), "art": f"/art?id={i['id']}{key}"}

    def previous_path(self):
        with self.state_lock:
            if self.current_song:
                return self.history[-2] if len(self.history) >= 2 else None
            return self.history[-1] if self.history else None

    def next_path(self):
        with self.state_lock:
            return self.upcoming[0] if self.upcoming else None

    def ensure_upcoming(self):
        """Keep the queue filled: "Up next" is the first song, and with break batching the
        DJ also plans its breaks for the songs behind it (see prepare_talk)."""
        with self.state_lock:
            if len(self.upcoming) >= self.lookahead:
                return
            remaining = max(0.0, self.current_end - time.time()) if self.current_song else 0.0
        start = dt.datetime.now() + dt.timedelta(seconds=remaining)
        to_top = (60 - start.minute) * 60 - start.second
        ratings = self.ratings if self.use_ratings else None
        ai = self.ai if self.use_ratings else None
        with self.ratings.lock:                    # 1-star songs never play again
            banned = {tid for tid, e in self.ratings.data.items() if e.get("stars") == 1}
        while True:
            with self.state_lock:
                if len(self.upcoming) >= self.lookahead:
                    break
                first = not self.upcoming
            if self.fit and first and to_top <= 600:   # near the top of the hour, prefer a song that ends before :00
                song = self.lib.next(max_seconds=to_top + 30, ratings=ratings, banned=banned, ai=ai)
            else:
                song = self.lib.next(ratings=ratings, banned=banned, ai=ai)
            if not song:
                break
            with self.state_lock:
                self.upcoming.append(song)
            self.info_for(song)

    def request_track(self, path):
        """A listener's request: put this song next. Returns its info, or None if
        it's banned, already queued or just played."""
        if not path or not os.path.isfile(path):
            return None
        tid = track_id(path)
        with self.ratings.lock:
            if (self.ratings.data.get(tid) or {}).get("stars") == 1:
                return None
        with self.state_lock:
            if path in self.upcoming or path == self.current_song or path in self.history[-5:]:
                return None
            self.upcoming.appendleft(path)
            song, remaining = self.current_song, max(0.0, self.current_end - time.time())
        info = self.info_for(path)
        self.log.info("REQUEST: %s", info["display"])
        if song and remaining > 20:          # the DJ's prepared intro is out of date now
            self.prepare_talk(song, remaining, keep_persona=True)
        return info

    def skip_upnext(self):
        """Swap the up-next song for another one before it plays."""
        with self.state_lock:
            old = self.upcoming.popleft() if self.upcoming else None
        self.ensure_upcoming()
        new = self.next_path()
        if old:
            self.log.info("UP NEXT changed: %s -> %s", self.info_for(old)["display"],
                          self.info_for(new)["display"] if new else "nothing")
        # the DJ's prepared intro was about the old song: write a new one
        with self.state_lock:
            song, remaining = self.current_song, max(0.0, self.current_end - time.time())
        if song and remaining > 20:
            self.prepare_talk(song, remaining, keep_persona=True)
        return new

    def drop_upcoming(self, tid):
        """A song just got 1 star: take it out of "up next" if it's there."""
        with self.state_lock:
            keep = [p for p in self.upcoming if track_id(p) != tid]
            if len(keep) != len(self.upcoming):
                self.upcoming = collections.deque(keep)
                self.log.info("Removed a 1-star song from up next")

    def random_logo(self):
        """A random logo from the logos folder (never the same one twice in a row)."""
        files = []
        if os.path.isdir(self.logos_dir):
            files = [os.path.join(self.logos_dir, n) for n in os.listdir(self.logos_dir)
                     if n.lower().endswith((".png", ".jpg", ".jpeg")) and not n.startswith(".")]
        if not files:
            return self.logo
        pick = random.choice([f for f in files if f != self.last_logo] or files)
        self.last_logo = pick
        return pick

    def new_logo_id(self):
        """Pick a logo now and give it an id, so the app keeps showing the same one for this break."""
        self.logo_count += 1
        lid = f"logo-{self.logo_count}"
        self.logo_picks[lid] = self.random_logo()
        while len(self.logo_picks) > 30:
            self.logo_picks.popitem(last=False)
        return lid

    def art_for(self, tid):
        if tid in self.art_cache:
            return self.art_cache[tid]
        if tid in self.logo_picks:
            info, logo = None, self.logo_picks[tid]
        else:
            info = self.track_info(tid)
            logo = None
        data, ctype = find_art(info["path"]) if info else (None, None)
        if not data:
            logo = logo or self.random_logo()      # song without cover art: random logo
            if logo and os.path.isfile(logo):
                with open(logo, "rb") as f:
                    data = f.read()
                ctype = "image/png" if logo.lower().endswith(".png") else "image/jpeg"
        if data:
            self.art_cache[tid] = (data, ctype)
            while len(self.art_cache) > 30:
                self.art_cache.popitem(last=False)
        return data, ctype

    def station_track(self):
        self.set_track({"id": None, "title": self.name, "artist": "", "album": "", "kind": "station",
                        "art_id": self.new_logo_id()})

    # -- listener controls (from the app): next, back, thumbs-down scratch
    def command(self, cmd):
        """Called from the web server thread. Interrupts whatever is on air."""
        if cmd not in ("next", "back", "scratch"):
            return False
        if cmd in ("next", "back") and not self.allow_skip:      # this channel doesn't allow listener skips
            return False
        with self.state_lock:
            if cmd == "back" and not self.history:
                return False
            self.pending = cmd
        self.interrupt.set()
        self.log.info("CONTROL: %s", cmd)
        return True

    def take_command(self):
        with self.state_lock:
            cmd, self.pending = self.pending, None
            self.interrupt.clear()
            return cmd

    def play_asset(self, name):
        path = os.path.join(self.assets_dir, name)
        if os.path.isfile(path):
            self.station_track()
            self.play(path, title=self.name, normalize=False, interruptible=False)
        else:
            self.log.warning("Sound effect not found: %s", path)

    def handle_command(self, cmd):
        # Players reconnect after a skip; without the old head start they land right on the new audio.
        self.bc.clear_burst()
        if self.skip_sounds or cmd == "scratch":
            self.play_asset({"next": "next.mp3", "back": "back.mp3", "scratch": "record_scratch.mp3"}[cmd])
        if cmd == "back":
            with self.state_lock:
                if self.current_song and len(self.history) >= 2:
                    # Going back from the current song: it becomes "up next" again
                    self.upcoming.appendleft(self.history.pop())
                song = self.history[-1] if self.history else None
                self.current_song = None
            if song:
                self.play_song(song, add_to_history=False)
        else:
            with self.state_lock:
                self.current_song = None
            self.play_song(None)

    # -- audio output
    def nobody_listening(self):
        return self.idle_when_empty and self.bc.idle_for() > self.idle_grace

    def wait_for_listeners(self):
        """Nothing plays to an empty room: park here until someone tunes in."""
        if not self.nobody_listening():
            return False
        self.log.info("Nobody listening - station paused until someone tunes in")
        self._drop_talk(self.talk)
        self.talk = None
        if self.voiceover:
            try:
                os.unlink(self.voiceover[1])
            except OSError:
                pass
            self.voiceover = None
        with self.state_lock:
            self.current_song = None
        self.bc.title = self.name
        self.station_track()
        while self.nobody_listening():
            self.bc.joined.wait(30)
            self.bc.joined.clear()
        self.log.info("Listener tuned in - back on the air")
        self.bc.clear_burst()
        return True

    def intro_is_quiet(self, song):
        """Rough check on a song's opening: quiet enough that a voice-over won't talk over
        singing. It measures loudness, not voices, so treat it as a guide. Cached per song."""
        if song in self._intro_cache:
            return self._intro_cache[song]
        ok = True
        try:
            secs = max(2.0, self.voiceover_delay + 6)
            r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-t", f"{secs:.1f}", "-i", song,
                                "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=20)
            m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
            if m:
                ok = float(m.group(1)) <= self.voiceover_max_intro_db
        except Exception:
            ok = True                                  # if we can't tell, keep the normal behaviour
        if len(self._intro_cache) > 2000:
            self._intro_cache.clear()
        self._intro_cache[song] = ok
        return ok

    def voiceover_args(self, song, wav):
        """ffmpeg inputs + filters that play `song` with `wav` spoken over its start and the music
        ducked underneath. None if the song is too short for it."""
        vlen = probe(wav)[0]
        slen = self.lib.duration(song)[0]
        d = max(0.0, self.voiceover_delay)
        if vlen <= 0 or slen < d + vlen + 8:
            return None
        music = "loudnorm=I=-16:TP=-1.5:LRA=11," if self.normalize else ""
        fmt = f"aresample={RATE},aformat=sample_fmts=s16:channel_layouts=stereo"
        ms = int(d * 1000)
        voice = f"[1:a]{voice_filter(wav, self.voice_target)},{fmt},adelay={ms}|{ms},apad"
        if self.duck_mode == "sidechain":
            # The voice itself drives the ducking: the music dips only while the DJ is loud.
            # The ratio is picked so the dip roughly matches duck_db (-12 dB -> about 4.8:1).
            ratio = max(2.0, min(20.0, -min(0.0, self.duck_db) / 2.5))
            fc = (f"[0:a]{music}{fmt}[m0];"
                  f"{voice}[v];"
                  f"[v]asplit=2[vs][vm];"
                  f"[m0][vs]sidechaincompress=threshold=0.03:ratio={ratio:.1f}:attack=30:release=600[md];"
                  f"[md][vm]amix=inputs=2:duration=first:dropout_transition=0,volume=2[out]")
        else:
            g = 10 ** (min(0.0, self.duck_db) / 20)            # music level while the DJ talks
            a, ramp, b = max(0.0, d - 0.6), 0.6, d + vlen + 0.9  # duck from a, back up by b
            env = (f"volume='1-{1 - g:.4f}*min(1,max(0,(t-{a:.2f})/{ramp}))*min(1,max(0,({b:.2f}-t)/{ramp}))'"
                   ":eval=frame")
            fc = (f"[0:a]{music}{fmt},{env}[m];"
                  f"{voice}[v];"
                  # both inputs last the whole song (apad), so amix's 1/2 scaling never changes
                  f"[m][v]amix=inputs=2:duration=first:dropout_transition=0,volume=2[out]")
        return ["-i", song, "-i", wav, "-filter_complex", fc, "-map", "[out]"]

    def play(self, path, title=None, normalize=None, interruptible=True, voice=False, over=None,
             start=0.0, trim_end=0.0):
        """voice=True: a speech clip, brought up to the voice loudness.
        over=wav: play `path` with that speech mixed over its start (voice-over break).
        start / trim_end: skip the first `start` seconds, or stop `trim_end` seconds early (crossfades)."""
        if interruptible and self.interrupt.is_set():
            return
        if title:
            self.bc.title = title
        args = self.voiceover_args(path, over) if over else None
        if args is None:
            if voice:
                af = ["-af", voice_filter(path, self.voice_target)]
            else:
                af = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] if (self.normalize if normalize is None else normalize) else []
            cut = []
            if start > 0:
                cut += ["-ss", f"{start:.3f}"]
            if trim_end > 0:
                cut += ["-t", f"{max(0.1, self.lib.duration(path)[0] - start - trim_end):.3f}"]
            args = [*cut, "-i", path, *af]
        self._pump(args, interruptible)

    def _pump(self, args, interruptible=True):
        """Runs ffmpeg with the given input args and writes its output to the encoder in real time."""
        p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "fatal", "-err_detect", "ignore_err", *args,
                              "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1"],
                             stdout=subprocess.PIPE)
        chunk = BYTES_PER_SEC // 10
        try:
            while True:
                if interruptible and self.interrupt.is_set():
                    break
                if self.nobody_listening():
                    break
                data = p.stdout.read(chunk)
                if not data:
                    break
                self.enc.write_pcm(data[: len(data) // 4 * 4])
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()
            p.wait()

    def crossfade_plan(self, song, dur, nxt, end):
        """Should the end of `song` overlap the start of `nxt`? Only when the next thing on air is
        the next song itself: no DJ talk, no time check, no station ID due."""
        xf = self.crossfade
        if xf <= 0 or not nxt or nxt == song or self.talk is not None:
            return False
        if self.predict_break(end) != "song":
            return False
        if self.id_interval > 0 and time.time() + dur - self.last_id_at >= self.id_interval * 60 - 30:
            return False
        if dur <= xf * 3 or self.lib.duration(nxt)[0] <= xf * 3:
            return False
        return True

    def play_crossfade(self, song, dur, nxt, xf):
        """The last xf seconds of `song` mixed with the first xf seconds of `nxt`."""
        loud = "loudnorm=I=-16:TP=-1.5:LRA=11," if self.normalize else ""
        fmt = f"aresample={RATE},aformat=sample_fmts=s16:channel_layouts=stereo"
        args = ["-ss", f"{max(0.0, dur - xf):.3f}", "-t", f"{xf:.3f}", "-i", song,
                "-t", f"{xf:.3f}", "-i", nxt,
                "-filter_complex",
                f"[0:a]{loud}{fmt}[a0];[1:a]{loud}{fmt}[a1];"
                f"[a0][a1]acrossfade=d={xf:.3f}:c1=tri:c2=tri[out]",
                "-map", "[out]"]
        self._pump(args, interruptible=True)

    def play_song(self, song=None, add_to_history=True):
        """Play a song (or the next one in line). Returns False if there's no music."""
        if song is None:
            self.ensure_upcoming()
            with self.state_lock:
                song = self.upcoming.popleft() if self.upcoming else None
        if not song:
            self.log.warning("No music found in %s - playing silence", self.lib.folder)
            self.bc.title = self.name
            self.enc.silence(10)
            return False
        info = self.info_for(song)
        dur = self.lib.duration(song)[0]
        with self.state_lock:
            if add_to_history:
                self.history.append(song)
                del self.history[:-200]
            self.current_song = song
            self.current_end = time.time() + dur
        self.set_track(info)
        self.ensure_upcoming()
        if self.ai:
            prev = self.history[-2] if len(self.history) >= 2 else None
            self.ai.note_track(info, self.info_for(prev) if prev else None)
            nxt = self.next_path()
            if nxt:
                self.ai.note_track(self.info_for(nxt), info)
        self.prepare_talk(song, dur)
        vo, self.voiceover = self.voiceover, None
        over = None
        if vo and vo[0] == song:
            over = vo[1]
            self.log.info("DJ %s (over the intro): %s", vo[3], vo[2])
        self.log.info("PLAY: %s", info["display"])
        start = 0.0
        if self.head_skip and self.head_skip[0] == song:      # its opening was already played in a crossfade
            start = self.head_skip[1]
        self.head_skip = None
        nxt = self.next_path() if (self.crossfade > 0 and not over and start == 0) else None
        end = dt.datetime.now() + dt.timedelta(seconds=dur)
        xf = self.crossfade if (nxt and self.crossfade_plan(song, dur, nxt, end)) else 0
        try:
            if xf:
                self.play(song, title=info["display"], trim_end=xf)      # all but the last xf seconds
                if not self.interrupt.is_set():
                    if self.next_path() == nxt:
                        self.play_crossfade(song, dur, nxt, xf)            # the overlap
                        self.head_skip = (nxt, xf)
                    else:                                                 # up next changed: play the tail as it is
                        self.play(song, start=max(0.0, dur - xf))
            else:
                self.play(song, title=info["display"], over=over, start=start)
            if over and self.ai and not self.interrupt.is_set():
                self.ai.aired(vo[2], vo[3])
        finally:
            if vo:
                try:
                    os.unlink(vo[1])
                except OSError:
                    pass
        if not self.interrupt.is_set():          # finished on its own (not skipped)
            with self.state_lock:
                self.current_song = None
        return True

    def say(self, text, dj=True):
        """dj=True: the channel's DJ voice (falls back to [tts]). dj=False: the normal
        [tts] voice, e.g. for script station IDs read in a random voice."""
        if not text or self.interrupt.is_set():
            return
        self.log.info("SAY: %s", text)
        if self.ai and dj:
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="homeradio_")
            os.close(fd)
            if not self.ai.speak(text, self.persona, wav):
                os.unlink(wav)
                wav = None
        else:
            wav = self.tts.render(self.ai.pronounce(text) if self.ai else text)
        if wav:
            try:
                self.bc.title = self.name
                self.station_track()
                self.play(wav, voice=True)
                if not self.interrupt.is_set():
                    self.enc.silence(0.4)
            finally:
                os.unlink(wav)

    def id_lines(self):
        """Spoken IDs: one per line in the script file (blank lines and # comments ignored)."""
        try:
            with open(self.id_script, encoding="utf-8-sig", errors="replace") as f:
                return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        except OSError:
            return []

    def station_id(self):
        """A random station ID: either an audio file from the IDs folder or a line from the
        script read in a random voice (each source has an even chance when both exist)."""
        if self.interrupt.is_set():
            return
        self.last_id_at = time.time()
        files, lines = list_audio(self.ids_dir), self.id_lines()
        sources = [name for name, items in (("file", files), ("script", lines)) if items]
        if not sources:
            self.say(self.tagline, dj=False)
            return
        if random.choice(sources) == "script":
            line = random.choice([l for l in lines if l != self.last_line] or lines)
            self.last_line = line
            self.log.info("ID (script): %s", line)
            self.say(line, dj=False)
        else:
            choices = [i for i in files if i != self.last_id] or files
            self.last_id = random.choice(choices)
            self.log.info("ID: %s", os.path.basename(self.last_id))
            self.station_track()
            self.play(self.last_id, title=self.name)

    # -- break logic, run between every song
    def run_break(self):
        now = dt.datetime.now()
        soon = now + dt.timedelta(minutes=3)
        top_key = soon.strftime("%Y%m%d%H")
        mins = now.minute + now.second / 60

        # Top of the hour (from :58 onward, or late up to :10): ID, time, weather, news
        if (now.minute >= 58 or now.minute < 10) and self.done["top"] != top_key:
            self.done["top"] = top_key
            self.done["quarter"] = top_key + "00"
            self.station_id()
            if self.interrupt.is_set():
                return
            spoken = soon.replace(minute=0) if now.minute >= 58 else now
            self.say(spoken_time(spoken) + " " + (self.weather.forecast_text() or ""))
            self.say(self.news.headlines_text())
            self.air_talk()
            return

        # Station ID every N minutes (10 by default)
        if self.id_interval > 0 and time.time() - self.last_id_at >= self.id_interval * 60 - 30:
            self.station_id()
            if self.interrupt.is_set():
                return

        # Quarter-hour time checks (:15, :45, and :30 if missed)
        nearest = round(mins / 15) * 15
        if nearest in (15, 30, 45) and abs(mins - nearest) <= self.window:
            q_key = now.strftime("%Y%m%d%H") + f"{nearest:02d}"
            if self.done["quarter"] != q_key:
                self.done["quarter"] = q_key
                self.say(spoken_time(now))

        self.air_talk()

    # -- AI DJ: written and voiced in the background while the song before plays
    def predict_break(self, t):
        """Which break will come after a song ending at time t (same rules as run_break)."""
        top_key = (t + dt.timedelta(minutes=3)).strftime("%Y%m%d%H")
        if (t.minute >= 58 or t.minute < 10) and self.done["top"] != top_key:
            return "top"
        mins = t.minute + t.second / 60
        nearest = round(mins / 15) * 15
        if nearest in (15, 30, 45) and abs(mins - nearest) <= self.window:
            if self.done["quarter"] != t.strftime("%Y%m%d%H") + f"{nearest:02d}":
                return "quarter"
        return "song"

    def song_ctx(self, path):
        i = self.info_for(path)
        return {"title": i["title"], "artist": i["artist"], "path": path}

    def _drop_talk(self, job):
        """Throw away prepared talk. If it's still being made, its thread deletes the audio."""
        if not job:
            return
        if job.get("shout") and self.ai and self.ai.chat:
            self.ai.chat.put_back(self.id, job.pop("shout"))
        job["abandoned"] = True
        if job["done"].is_set() and job["wav"]:
            try:
                os.unlink(job["wav"])
            except OSError:
                pass

    def prepare_talk(self, song, dur, keep_persona=False):
        self._drop_talk(self.talk)
        self.talk = None
        if not self.dj_breaks:
            return
        if not keep_persona and len(self.persona_choices) > 1:
            others = [p for p in self.persona_choices if p != self.persona]
            self.persona = random.choice(others or self.persona_choices)   # who hosts the next break
        nxt = self.next_path()
        if not self.ai or not nxt:
            return
        end = dt.datetime.now() + dt.timedelta(seconds=dur)
        kind = self.predict_break(end)
        chat = self.ai.chat if self.ai else None
        shout = chat.take_shoutout(self.id) if chat else None
        phone = kind == "song" and not self.phone_busy and not shout and self.ai.phone_in_due()
        if not phone and not shout and not self.ai.wants_to_talk(kind, self.persona):
            return
        persona = self.persona
        voiceover = not phone and not shout and random.random() < self.voiceover_chance
        check_intro = voiceover and self.voiceover_quiet_only
        gaps = 0 if phone else self.effect_gaps(self.ai.effects_chance(persona))
        later = []                                     # the songs queued behind this one, for batching
        if self.lookahead > 1 and not phone:
            with self.state_lock:
                queue = list(self.upcoming)[1:]
            prev_path = nxt
            for p in queue:
                later.append({"prev": self.song_ctx(prev_path), "next": self.song_ctx(p)})
                prev_path = p
        job = {"prev": song, "next": nxt, "text": None, "wav": None, "phone": phone,
               "persona": persona, "voiceover": voiceover, "shout": shout,
               "done": threading.Event()}
        self.talk = job
        h = end.hour
        part = "morning" if 5 <= h < 12 else "afternoon" if h < 17 else "evening" if h < 21 else "late night"

        def work():
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="homeradio_dj_")
            os.close(fd)
            try:
                vo_ok = voiceover
                if check_intro and not self.intro_is_quiet(nxt):
                    vo_ok = False                      # busy opening: talk between songs instead
                    job["voiceover"] = False
                ctx = {"part_of_day": part, "prev": self.song_ctx(song), "next": self.song_ctx(nxt),
                       "voiceover": vo_ok, "later": later}
                if shout:
                    ctx["shoutout"] = {"name": shout["name"], "note": shout["note"],
                                       "song_queued": shout.get("song")}
                if phone:
                    self.phone_busy = True
                    ctx["weather"] = self.weather.forecast_text()
                    if self.ai.make_phone_in(ctx, persona, wav):
                        job["wav"], wav = wav, None
                else:
                    text = self.ai.write_break(ctx, persona)
                    if text and self.talk_with_effects(text, persona, wav, gaps):
                        job["text"], job["wav"], wav = text, wav, None
            except Exception as e:
                self.log.warning("DJ talk failed: %s", e)
            finally:
                self.phone_busy = False if phone else self.phone_busy
                if wav:
                    os.unlink(wav)
                job["done"].set()
                if job.get("abandoned") and job["wav"]:   # skipped past while we were working
                    try:
                        os.unlink(job["wav"])
                    except OSError:
                        pass

        threading.Thread(target=work, daemon=True, name=f"dj-{self.id}").start()

    def air_talk(self):
        """Play the prepared DJ talk or phone-in, if it's ready and still about the right songs."""
        job, self.talk = self.talk, None
        if not job:
            # No DJ talk, but maybe a sound effect on its own
            ec = self.ai.effects_chance(self.persona) if self.ai else 0.3
            self.maybe_play_effect(ec)
            return
        with self.state_lock:
            prev = self.history[-1] if self.history else None
        ok = (not self.interrupt.is_set() and prev == job["prev"] and self.next_path() == job["next"]
              and job["done"].wait(3))
        if ok and job["wav"] and job.get("voiceover") and not self.interrupt.is_set():
            self.voiceover = (job["next"], job["wav"], job["text"], job["persona"])
            job["wav"] = None                          # play_song owns (and deletes) it now
            self._drop_talk(job)
            return
        if ok and job["wav"]:
            if job["phone"]:
                self.log.info("PHONE-IN (%s)", job["persona"])
            else:
                self.log.info("DJ %s: %s", job["persona"], job["text"])
            self.station_track()
            self.play(job["wav"], voice=True)
            if not self.interrupt.is_set():
                self.enc.silence(0.4)
                if job["text"]:
                    self.ai.aired(job["text"], job["persona"])
                if job.get("shout") and self.ai.chat:
                    self.ai.chat.shoutout_aired(self.id, job.pop("shout"), job["text"] or "")
        self._drop_talk(job)

    def run(self):
        started = False
        while True:
            try:
                if self.wait_for_listeners() or not started:
                    started = True
                    self.station_id()            # every time the station comes back on the air
                    continue
                cmd = self.take_command()
                if cmd:
                    self.handle_command(cmd)
                    continue
                self.run_break()
                if self.interrupt.is_set():
                    continue
                self.play_song()
            except BrokenPipeError:
                self.log.error("Encoder died; restarting")
                self.enc = Encoder(self.bc, self.cfg.get("stream", {}).get("bitrate", "128k"))
            except Exception as e:
                self.log.exception("Playout error: %s", e)
                time.sleep(1)

def split_for_effects(text, gaps):
    """Split a DJ break into at most gaps+1 parts at sentence and comma breaks."""
    text = text.strip()
    pieces = [p for p in re.split(r"(?<=[.!?,;:])\s+", text) if p]
    if gaps < 1 or len(pieces) < 2:
        return [text]
    n = min(gaps + 1, len(pieces))
    size = -(-len(pieces) // n)                 # ceiling division
    return [" ".join(pieces[i:i + size]) for i in range(0, len(pieces), size)]


def splice_wavs(parts, out):
    """parts: WAV paths, joined in order into one WAV. The caller has already leveled
    the effects, so every piece is at the same loudness."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for path in parts:
        cmd += ["-i", path]
    chains, labels = [], []
    for i in range(len(parts)):
        f = "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo"
        chains.append(f"[{i}:a]{f}[a{i}]")
        labels.append(f"[a{i}]")
    fc = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", "-ac", "2", "-ar", "44100", out]
    subprocess.run(cmd, capture_output=True, timeout=60, check=True)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "station"


def channel_configs(cfg):
    """Stations from [[channels]] in radio.toml; without it, one station from [paths] (old style)."""
    st, paths, idc = cfg.get("station", {}), cfg.get("paths", {}), cfg.get("ids", {})
    main_name = st.get("name", "HomeRadio")
    raw = cfg.get("channels") or [{"id": "main", "name": main_name, "music": paths.get("music", [])}]
    out, seen = [], set()
    for i, c in enumerate(raw):
        name = c.get("name") or f"{main_name} {i + 1}"
        cid = _slug(c.get("id") or name)
        while cid in seen:
            cid += "-2"
        seen.add(cid)
        ids_dir = os.path.expanduser(c.get("station_ids", paths.get("station_ids", "")) or "")
        script = os.path.expanduser(c.get("id_script", idc.get("script", "")) or "")
        out.append({
            "id": cid, "name": name, "first": i == 0, "persona": c.get("persona", "default"),
            "music": c.get("music") or paths.get("music", []),
            "station_ids": ids_dir,
            "id_script": script or (os.path.join(ids_dir, "ids.txt") if ids_dir else ""),
            "id_interval": c.get("id_interval_minutes", idc.get("interval_minutes", 10)),
            "logos": os.path.expanduser(c.get("logos", paths.get("logos", "")) or ""),
            "logo": os.path.expanduser(c.get("logo", paths.get("logo", "")) or ""),
            "spoken_id": c.get("spoken_id") or (st.get("spoken_id") if i == 0 and not cfg.get("channels")
                                                else f"You're listening to {name}."),
            "dj_breaks": c.get("dj_breaks", True),
            "effects": os.path.expanduser(c.get("effects", paths.get("effects", "")) or ""),
            "filter_genre": c.get("filter_genre", ""),
            "filter_artist": c.get("filter_artist", ""),
            "filter_stars": c.get("filter_stars", 0),
            "filter_stars_from": c.get("filter_stars_from", "station"),   # "station" or "plex"
            "allow_skip": c.get("allow_skip", True),   # false: no Next/Back for this channel
        })
    return out


class Hub:
    """Everything the stations share: the web server, tunnel, ratings, Plex, weather, news and voices."""
    def __init__(self, cfg):
        self.cfg = cfg
        here = os.path.dirname(os.path.abspath(__file__))
        st, paths, s = cfg.get("station", {}), cfg.get("paths", {}), cfg.get("stream", {})
        self.title = st.get("name", "HomeRadio")
        self.name = self.title
        self.password = s.get("password", "")
        self.assets_dir = os.path.join(here, os.path.expanduser(paths.get("assets", "assets")))
        self.ratings = Ratings(here)
        self.plex = Plex(cfg.get("plex", {}))
        self.weather, self.news, self.tts = Weather(cfg.get("weather", {})), News(cfg.get("news", {})), TTS(cfg.get("tts", {}))
        self.ai = None
        if homeradio_ai is None:
            log.info("AI features off (homeradio_ai.py not loaded: %s)", _AI_IMPORT_ERROR)
        else:
            try:
                self.ai = homeradio_ai.Station(cfg, here, self.rating_list, self.tts.render)
            except Exception as e:
                log.warning("AI features off - setup failed: %s", e)
        self.channel_cfgs = channel_configs(cfg)
        self.stations = collections.OrderedDict()
        for ch in self.channel_cfgs:
            self.stations[ch["id"]] = Station(cfg, self, ch)
        for cid, station in self.stations.items():
            if self.ai:
                self.ai.bind_station(cid, station)
        self.default = next(iter(self.stations.values()))
        log.info("Stations: %s", ", ".join(f"{x.name} ({x.base}/)" for x in self.stations.values()))
        host, port = s.get("host", "0.0.0.0"), int(s.get("port", 8000))
        for attempt in range(20):                # after a restart the old copy may still hold the port
            try:
                srv = ThreadingHTTPServer((host, port), make_handler(self))
                break
            except OSError:
                if attempt == 19:
                    raise
                time.sleep(1)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Streaming at http://%s:%s/", host, port)
        self.tunnel = Tunnel(cfg.get("tunnel", {}), port, self.password, cfg.get("notify", {}), self.title)
        threading.Thread(target=self.tunnel.start, daemon=True, name="tunnel").start()

    def health(self):
        """Everything the health page shows. up = True/False, or None when off or not used."""
        rows = []
        try:
            if self.ai:
                rows += self.ai.health()
            else:
                rows.append({"name": "AI DJ", "up": None, "detail": "off (homeradio_ai.py not loaded)"})
        except Exception as e:
            rows.append({"name": "AI DJ", "up": False, "detail": f"check failed: {e}"})
        if self.plex.enabled:
            up, detail = _answers(self.plex.url, "/identity")
        else:
            up, detail = None, "no Plex token set"
        rows.append({"name": "Plex", "up": up, "detail": detail})
        if self.tunnel.provider in ("", "none", "off"):
            rows.append({"name": "Public link", "up": None, "detail": "off (no tunnel)"})
        else:
            rows.append({"name": "Public link", "up": bool(self.tunnel.url),
                         "detail": self.tunnel.url or "waiting for the tunnel to come up"})
        rows.append({"name": "ffmpeg", "up": bool(shutil.which("ffmpeg")),
                     "detail": "on PATH" if shutil.which("ffmpeg") else "not found on PATH"})
        for st in self.stations.values():
            n = len(st.lib.files)
            if not st.lib.ready.is_set():
                up, detail = False, "still scanning the music folders"
            elif n:
                up, detail = True, f"{n} songs"
            else:
                up, detail = False, "no songs found"
            rows.append({"name": f"Music: {st.name}", "up": up, "detail": detail})
        return rows

    def rating_list(self):
        """For the AI rotation: (path, stars, thumbs up, thumbs down) for every rated song."""
        with self.ratings.lock:
            return [(e.get("path"), e.get("stars"), e.get("up", 0), e.get("down", 0))
                    for e in self.ratings.data.values()]

    def channels_info(self):
        key = f"?key={urllib.parse.quote(self.password)}" if self.password else ""
        out = []
        for c in self.stations.values():
            t = c.bc.track
            art_id = t.get("art_id") or t.get("id") or "station"
            out.append({"id": c.id, "name": c.name, "url": f"{c.base}/",
                        "now_playing": t.get("display") or t.get("title") or c.name,
                        "title": t.get("title") or c.name, "artist": t.get("artist", ""),
                        "art": f"{c.base}/art?id={art_id}" + (f"&key={urllib.parse.quote(self.password)}" if self.password else ""),
                        "listeners": len(c.bc.listeners)})
        return out

    def start(self):
        for c in self.stations.values():
            threading.Thread(target=c.run, daemon=True, name=f"station-{c.id}").start()

    def random_logo(self):
        return self.default.random_logo()


# ---------------------------------------------------------------- system tray (Windows)
def _no_console_windows():
    """When running without a console (pythonw), stop ffmpeg/ffprobe/etc. from flashing windows."""
    if os.name != "nt":
        return
    orig = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("creationflags", 0x08000000)       # CREATE_NO_WINDOW
        orig(self, *args, **kwargs)
    subprocess.Popen.__init__ = patched


def _message_box(text, title="WANG Radio"):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr or sys.__stderr__)


class LogWindow:
    """A simple window showing the station log. Minimizing or closing it hides it back to the tray."""
    def __init__(self, log_path, title):
        self.log_path, self.title = log_path, title
        self.thread, self.root = None, None

    def show(self):
        if self.thread and self.thread.is_alive() and self.root:
            self.root.after(0, self._raise)
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="log-window")
        self.thread.start()

    def _raise(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()

    def _run(self):
        import tkinter as tk
        from tkinter.scrolledtext import ScrolledText
        root = self.root = tk.Tk()
        root.title(f"{self.title} - log")
        root.geometry("900x480")
        txt = ScrolledText(root, font=("Consolas", 10), bg="#121212", fg="#e8e8e8", insertbackground="white")
        txt.pack(fill="both", expand=True)
        pos = {"n": 0}

        def refresh():
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if size < pos["n"]:
                        pos["n"] = 0
                    start = max(pos["n"], size - 200_000) if pos["n"] == 0 else pos["n"]
                    f.seek(start)
                    new = f.read()
                    pos["n"] = f.tell()
                if new:
                    at_bottom = txt.yview()[1] > 0.98
                    txt.insert("end", new)
                    if at_bottom:
                        txt.see("end")
            except OSError:
                pass
            root.after(1000, refresh)

        def hide(*_):
            root.withdraw()
        root.protocol("WM_DELETE_WINDOW", hide)
        root.bind("<Unmap>", lambda e: root.after(10, hide) if root.state() == "iconic" else None)
        refresh()
        root.mainloop()
        self.root = None


def _tray_icon_image(station):
    from PIL import Image, ImageDraw, ImageFont
    for src in filter(None, [station.random_logo() if hasattr(station, "random_logo") else None]):
        try:
            return Image.open(src).convert("RGBA").resize((64, 64))
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(226, 59, 46))
    try:
        font = ImageFont.truetype("arialbd.ttf", 38)
    except Exception:
        font = ImageFont.load_default()
    d.text((32, 33), "W", fill="white", font=font, anchor="mm")
    return img


def _console_window(show):
    """Show/hide the console window (works with the classic console; not Windows Terminal)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5 if show else 0)
            return True
    except Exception:
        pass
    return False


def run_tray(hub, cfg, log_path):
    """Runs the tray icon on the main thread; the station plays in the background."""
    import pystray
    import webbrowser
    tcfg = cfg.get("tray", {})
    port = int(cfg.get("stream", {}).get("port", 8000))
    pw = cfg.get("stream", {}).get("password", "")
    key = f"?key={urllib.parse.quote(pw)}" if pw else ""
    station = hub
    logwin = LogWindow(log_path, hub.name)
    state = {"console": not tcfg.get("start_hidden", True)}
    if not state["console"]:
        _console_window(False)

    def playing(c):
        t = c.bc.track
        return (t.get("display") or t.get("title") or c.name)[:80]

    def opener(c):
        return lambda icon, item: webbrowser.open(f"http://localhost:{port}{c.base}/{key}")

    def skipper(c):
        return lambda icon, item: c.command("next")

    def labeller(c):
        return lambda item: f"{c.name}  ♪ {playing(c)}"[:110]

    def station_items():
        for c in hub.stations.values():
            yield pystray.MenuItem(labeller(c), pystray.Menu(
                pystray.MenuItem("Open player", opener(c)),
                pystray.MenuItem("Skip song", skipper(c)),
            ))

    def public_link():
        return f"{station.tunnel.url}/{key}" if station.tunnel.url else f"http://localhost:{port}/{key}"

    def copy_link(icon, _):
        link = public_link()
        try:
            subprocess.run(["clip"], input=link.encode("utf-16-le"), check=True)
            icon.notify(link, "Link copied")
        except Exception:
            icon.notify(link, "Station link")

    def toggle_console(icon, _):
        state["console"] = not state["console"]
        if not _console_window(state["console"]):
            logwin.show()

    def quit_(icon, _):
        log.info("Signing off.")
        icon.stop()
        try:
            station.tunnel.stop()
        finally:
            os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open web player", lambda i, _: webbrowser.open(f"http://localhost:{port}/{key}"), default=True),
        pystray.MenuItem("Copy public link", copy_link, enabled=lambda _: bool(hub.tunnel.url)),
        pystray.Menu.SEPARATOR,
        *list(station_items()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Show log", lambda i, _: logwin.show()),
        pystray.MenuItem("Show/hide console window", toggle_console),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit WANG Radio", quit_),
    )
    icon = pystray.Icon("wangradio", _tray_icon_image(hub), hub.name, menu)

    def refresher():                       # keep the hover text / "now playing" current
        while True:
            time.sleep(5)
            try:
                icon.title = "\n".join([hub.name] + [f"{c.name}: {playing(c)}" for c in hub.stations.values()])[:127]
                icon.update_menu()
            except Exception:
                pass
    threading.Thread(target=refresher, daemon=True).start()
    if tcfg.get("notify_on_start", True):
        threading.Timer(3, lambda: icon.notify("Running in the system tray. Right-click the icon for options.",
                                               hub.name)).start()
    icon.run()


def restart_station():
    """Start a fresh copy of this program, then exit this one. The new copy waits a few
    seconds (HOMERADIO_RESTART) and retries the web port, so the old copy can let go of it."""
    log.info("Restarting the station (from the settings page)")
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    here = os.path.dirname(os.path.abspath(__file__))
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), CONFIG_PATH],
                         cwd=here, env=dict(os.environ, HOMERADIO_RESTART="1"),
                         creationflags=flags, close_fds=True)
    except OSError as e:
        log.error("Restart failed: %s", e)
        return
    os._exit(0)


def main():
    global CONFIG_PATH
    if os.environ.get("HOMERADIO_RESTART"):
        time.sleep(4)                                # the previous copy is still shutting down
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "radio.toml")
    CONFIG_PATH = os.path.abspath(cfg_path)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    here = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(here, "radio.log")
    headless = sys.stdout is None or sys.stderr is None          # started with pythonw (no console)
    handlers = []
    for stream in (sys.stdout, sys.stderr):        # Windows console: print accented titles safely
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if not headless:
        handlers.append(logging.StreamHandler())
    from logging.handlers import RotatingFileHandler
    handlers.append(RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)
    if headless:
        _no_console_windows()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            msg = (f"{tool} not found. Install ffmpeg and make sure it is on your PATH "
                   "(Windows: run  winget install ffmpeg  then open a new terminal).")
            _message_box(msg) if headless else None
            sys.exit(msg)

    tray = cfg.get("tray", {}).get("enabled", True) and os.name == "nt"
    if tray:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            tray = False
            log.warning("System tray needs two extra packages:  pip install pystray pillow")
            if headless:
                _message_box("WANG Radio's tray icon needs:  pip install pystray pillow\n"
                             "The station is running without it.")

    station = None
    try:
        station = Hub(cfg)
        station.start()
        if tray:
            run_tray(station, cfg, log_path)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Signing off.")
    except Exception as e:
        log.exception("Station stopped: %s", e)
        if headless:
            _message_box(f"WANG Radio stopped:\n{e}\n\nSee radio.log for details.")
        raise
    finally:
        if station:
            station.tunnel.stop()

if __name__ == "__main__":
    main()
