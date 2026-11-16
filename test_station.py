#!/usr/bin/env python3
"""Smoke test for HomeRadio. Standard library plus ffmpeg/ffprobe.

It builds a throwaway station in a temp folder (short test MP3s with genre tags, a
sound-effects folder, station IDs), starts fake Ollama and fake Plex servers, runs
radio.py against them, and checks that:
  - the health page reports the fake Ollama and the music library as up,
  - a listener gets real audio from the stream,
  - the station logged no tracebacks or playout errors.
It also runs quick unit checks on the DJ splice, the effect leveling and the channel filters.

    python3 test_station.py            # run everything
    python3 test_station.py --keep     # keep the temp folder to look at radio.log
Exit code 0 = all passed.
"""
import argparse
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM_FILES = ["radio.py", "homeradio_ai.py", "homeradio_chat.py"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    return ok


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tone(path, secs, freq=440, volume=0.5, genre="", artist=""):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f={freq}:d={secs}",
           "-af", f"volume={volume}", "-ac", "2", "-ar", "44100"]
    if genre:
        cmd += ["-metadata", f"genre={genre}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    subprocess.run(cmd + [path], check=True, capture_output=True)


# ------------------------------------------------------------------ fake servers
class FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handlers):
        super().__init__(("127.0.0.1", free_port()), FakeHandler)
        self.handlers = handlers
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n:
            self.rfile.read(n)
        handler = self.server.handlers.get(self.path.split("?")[0])
        if not handler:
            return self._reply({"error": "not faked"}, 404)
        self._reply(handler(self))

    do_GET = _route
    do_POST = _route


def fake_ollama():
    text = "This is the test station. Here comes the next song, enjoy it."
    return FakeServer({
        "/api/tags": lambda h: {"models": []},
        "/api/chat": lambda h: {"model": "fake", "done": True,
                                "message": {"role": "assistant", "content": text}},
        "/api/embed": lambda h: {"embeddings": [[0.1] * 8]},
        "/api/embeddings": lambda h: {"embedding": [0.1] * 8},
    })


def fake_plex(music_files):
    def library(h):
        meta = []
        for i, path in enumerate(music_files, 1):
            meta.append({"ratingKey": i, "title": os.path.splitext(os.path.basename(path))[0],
                         "grandparentTitle": "Test Artist", "userRating": 8,
                         "Media": [{"Part": [{"file": path}]}]})
        return {"MediaContainer": {"Metadata": meta}}
    return FakeServer({
        "/identity": lambda h: {"MediaContainer": {"machineIdentifier": "test"}},
        "/library/sections": lambda h: {"MediaContainer": {"Directory": [{"key": "1", "type": "artist"}]}},
        "/library/sections/1/all": library,
        "/library/metadata/1/nearest": lambda h: {"MediaContainer": {"Metadata": []}},
    })


# ------------------------------------------------------------------ unit checks
def unit_checks(station_module):
    m = station_module
    check("split: 2 gaps on 3 sentences gives 3 parts",
          len(m.split_for_effects("This is Bob, up next is a song, enjoy it.", 2)) == 3)
    check("split: no gaps gives the whole text",
          m.split_for_effects("One. Two. Three.", 0) == ["One. Two. Three."])
    check("split: one sentence can't be split",
          m.split_for_effects("Just one sentence here.", 2) == ["Just one sentence here."])
    tmp = tempfile.mkdtemp(prefix="hr_unit_")
    try:
        a, b, out = (os.path.join(tmp, n) for n in ("a.wav", "b.wav", "out.wav"))
        tone(a, 1.0)
        tone(b, 0.5, freq=880)
        m.splice_wavs([a, b], out)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", out], capture_output=True, text=True).stdout or 0)
        check("splice: joined length is the sum of parts", abs(dur - 1.5) < 0.1, f"{dur:.2f}s")
        # effect leveling: a very quiet and a very loud clip come out the same loudness
        quiet, loud = os.path.join(tmp, "q.wav"), os.path.join(tmp, "l.wav")
        tone(quiet, 2, volume=0.05)
        tone(loud, 2, volume=0.9)
        stub = types.SimpleNamespace(voice_target=-15.0, log=m.log)
        lq = m.Station.leveled_effect(stub, quiet)
        ll = m.Station.leveled_effect(stub, loud)
        lufs = []
        for f in (lq, ll):
            r = subprocess.run(["ffmpeg", "-i", f, "-af", "ebur128", "-f", "null", "-"],
                               capture_output=True, text=True)
            import re
            found = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)   # integrated loudness
            lufs.append(float(found[-1]) if found else None)
        check("effects: quiet and loud clips level to the same loudness",
              None not in lufs and abs(lufs[0] - lufs[1]) < 1.0, f"{lufs}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # channel filters: genre and Plex star rating
    plex = types.SimpleNamespace(find=lambda p: {"stars": 4} if p.endswith("good.mp3") else {"stars": 1})
    hub = types.SimpleNamespace(ai=types.SimpleNamespace(rotation=types.SimpleNamespace(plex=plex)),
                                ratings=None)
    f = m.Station._build_filter({"filter_stars": 4, "filter_stars_from": "plex"}, hub)
    check("filter: Plex 4-star keeps the 4-star song only",
          f("/x/good.mp3", {"id": "1"}) and not f("/x/bad.mp3", {"id": "2"}))
    g = m.Station._build_filter({"filter_genre": "rock"}, hub)
    check("filter: genre match is case-insensitive",
          g("/x/a.mp3", {"genre": "Classic ROCK"}) and not g("/x/b.mp3", {"genre": "Jazz"}))
    plex_watch_checks(m)
    library_filter_checks(m)


def library_filter_checks(m):
    """Genre channels read tags in the background; the Plex index is fetched in pages."""
    tmp = tempfile.mkdtemp(prefix="hr_lib_")
    try:
        album = os.path.join(tmp, "Album")
        os.makedirs(album)
        tone(os.path.join(album, "blue.mp3"), 1, genre="Delta Blues")
        tone(os.path.join(album, "rock.mp3"), 1, freq=600, genre="Rock")
        f = m.Station._build_filter({"filter_genre": "blues"}, types.SimpleNamespace(ai=None, ratings=None))
        lib = m.Library([tmp], cache_path=os.path.join(tmp, "library_cache_t.json"), track_filter=f)
        end = time.time() + 30
        while len(lib.tags) < 2 and time.time() < end:
            time.sleep(0.2)
        picks = [os.path.basename(p) for p in lib._candidates()]
        check("library: genre channel plays only matching songs once tags are read",
              picks == ["blue.mp3"], f"{picks}")
        lib.durations = None                       # picking must not probe files itself
        try:
            ok = bool(lib._candidates())
        except Exception:
            ok = False
        check("library: picking a song for a filtered channel never probes files", ok)
        time.sleep(0.5)
        check("library: tags are remembered between runs", os.path.isfile(lib.tags_path))
        stars = m.Station._build_filter({"filter_stars": 5, "filter_stars_from": "plex"}, types.SimpleNamespace(
            ai=types.SimpleNamespace(rotation=types.SimpleNamespace(plex=types.SimpleNamespace(
                find=lambda p: {"stars": 5} if p.endswith("blue.mp3") else None))), ratings=None))
        check("library: a star filter needs no file tags", stars.needs_tags is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    import homeradio_ai as ai
    songs = [f"/music/Album/song{i}.mp3" for i in range(7)]
    calls = []

    def library(h):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        start, size = int(q["X-Plex-Container-Start"][0]), int(q["X-Plex-Container-Size"][0])
        calls.append(start)
        meta = [{"ratingKey": i, "grandparentTitle": "A", "userRating": 10,
                 "Media": [{"Part": [{"file": p}]}]} for i, p in enumerate(songs)][start:start + size]
        return {"MediaContainer": {"totalSize": len(songs), "Metadata": meta}}
    srv = FakeServer({
        "/library/sections": lambda h: {"MediaContainer": {"Directory": [{"key": "1", "type": "artist"}]}},
        "/library/sections/1/all": library})
    try:
        px = ai.PlexSimilar({"url": f"http://127.0.0.1:{srv.port}", "token": "t"})
        px.PAGE = 3
        px.index()
        check("plex index: a big library is fetched in pages",
              len(px.tracks) == 7 and calls == [0, 3, 6], f"{len(px.tracks)} songs, calls {calls}")
        check("plex index: 5-star lookup works after paging",
              (px.find("T:\\Music\\Album\\song6.mp3") or {}).get("stars") == 5)
    except Exception as e:
        check("plex index: a big library is fetched in pages", False, repr(e))
    finally:
        srv.shutdown()


def plex_watch_checks(m):
    """PlexWatch: restart after 3 failures in a row, never for a bad token, cooldown, reconnect."""
    real_time, real_state, real_restart = m.time, m.plex_state, m.PlexWatch._restart_service
    script, restarts, reconnects = [], [], []
    m.plex_state = lambda url, token, timeout=10: script.pop(0) if script else "down"
    m.PlexWatch._restart_service = lambda self: restarts.append(1)
    m.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)   # no 60 s waits
    hub = types.SimpleNamespace(plex=types.SimpleNamespace(
        url="http://plex.test", token="t", reconnect=lambda: reconnects.append(1)), ai=None)
    try:
        pw = m.PlexWatch({}, hub)
        script[:] = ["down", "down"]
        pw.check(); pw.check()
        check("plex watch: two failures in a row do not restart", not restarts)
        script[:] = ["down", "ok"]                # 3rd failure -> restart -> Plex answers again
        pw.check()
        check("plex watch: third failure restarts the Plex service once",
              len(restarts) == 1, f"restarts={len(restarts)}")
        check("plex watch: station reconnects to Plex after a successful restart",
              reconnects == [1] and pw.state == "ok")

        restarts.clear()
        pw2 = m.PlexWatch({}, hub)
        script[:] = ["bad token"] * 5
        for _ in range(5):
            pw2.check()
        check("plex watch: a rejected token never restarts Plex", not restarts)

        restarts.clear()
        pw3 = m.PlexWatch({}, hub)
        script[:] = ["down", "down", "ok", "down", "down"]
        for _ in range(5):
            pw3.check()
        check("plex watch: a good answer resets the failure count", not restarts)

        restarts.clear()
        pw4 = m.PlexWatch({"restart_cooldown_minutes": 30}, hub)
        pw4._last_restart = time.time()                            # restarted a moment ago
        script[:] = ["down"] * 4
        for _ in range(4):
            pw4.check()
        check("plex watch: restarts are spaced by the cooldown", not restarts)
    finally:
        m.time, m.plex_state, m.PlexWatch._restart_service = real_time, real_state, real_restart


# ------------------------------------------------------------------ integration
def integration(keep):
    work = tempfile.mkdtemp(prefix="hr_test_")
    proc = None
    servers = []
    try:
        for name in PROGRAM_FILES:
            shutil.copy(os.path.join(HERE, name), work)
        music = os.path.join(work, "music", "Test Album")
        os.makedirs(music)
        songs = []
        for i, (genre, secs) in enumerate([("Rock", 3), ("Jazz", 3), ("Rock", 4)], 1):
            p = os.path.join(music, f"song{i}.mp3")
            tone(p, secs, freq=300 + 100 * i, genre=genre, artist="Test Artist")
            songs.append(p)
        effects = os.path.join(work, "effects")
        os.makedirs(effects)
        tone(os.path.join(effects, "boop.wav"), 0.3, freq=1000)
        tone(os.path.join(effects, "beep.wav"), 0.2, freq=1500)
        ids = os.path.join(work, "ids")
        os.makedirs(ids)
        with open(os.path.join(ids, "ids.txt"), "w", encoding="utf-8") as f:
            f.write("This is Test Radio.\n")

        ollama = fake_ollama()
        plex = fake_plex(songs)
        servers += [ollama, plex]
        port = free_port()
        cfg = f'''
[station]
name = "Test Radio"
[[channels]]
id = "main"
name = "Test Main"
music = ["{os.path.join(work, 'music')}"]
station_ids = "{ids}"
id_interval_minutes = 1
persona = "default"
effects = "{effects}"
[[channels]]
id = "radio"
name = "Test Radio Only"
music = ["{os.path.join(work, 'music')}"]
station_ids = "{ids}"
id_interval_minutes = 1
persona = "default"
effects = "{effects}"
allow_skip = false
[stream]
host = "127.0.0.1"
port = {port}
admin_password = "harness-pass"
bitrate = "128k"
play_only_when_listening = true
stop_after_seconds = 15
[tunnel]
provider = "none"
[tray]
enabled = false
[tts]
command = "ffmpeg -y -v error -f lavfi -i anullsrc=r=22050:cl=mono -t 0.8 {{out}}"
[ai]
enabled = true
facts = false
vibe_search = false
research = false
show_notes = false
social_feed = false
effects_per_hour = 30
break_batch = 2
[audio]
crossfade_seconds = 1
voiceover_chance = 0.5
[ai.models]
talk = "ollama:fake"
[ai.providers.ollama]
type = "ollama"
url = "http://127.0.0.1:{ollama.port}"
[ai.phone_in]
enabled = false
[ai.personas.default]
name = "Dana"
talkativeness = 1.0
effects_chance = 1.0
humor = 0.0
[chat]
enabled = false
[plex]
url = "http://127.0.0.1:{plex.port}"
token = "test"
'''
        cfg_path = os.path.join(work, "test.toml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg)

        log_out = open(os.path.join(work, "stdout.txt"), "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "radio.py", cfg_path], cwd=work,
                                stdout=log_out, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"

        health, deadline = None, time.time() + 90
        while time.time() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(base + "/health.json", timeout=3) as r:
                    health = json.loads(r.read())
                break
            except Exception:
                time.sleep(1)
        check("station started and answered", health is not None,
              "gave up after 90 s; see stdout.txt")
        if health:
            rows = {r["name"]: r for r in health["checks"]}
            ai_row = next((r for n, r in rows.items() if n.startswith("AI: ")), {})
            check("health: AI provider up", ai_row.get("up") is True, ai_row.get("detail"))
            music_row = rows.get("Music: Test Main", {})
            check("health: music library up", music_row.get("up") is True, music_row.get("detail"))
            check("health: Plex up", rows.get("Plex", {}).get("up") is True, rows.get("Plex"))

        got = b""
        try:
            with urllib.request.urlopen(base + "/c/main/stream?noicy=1", timeout=30) as r:
                end = time.time() + 12
                while len(got) < 40000 and time.time() < end:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    got += chunk
        except Exception as e:
            check("stream: listener connects", False, str(e))
        check("stream: listener gets audio", len(got) >= 20000, f"{len(got)} bytes")

        with urllib.request.urlopen(base + "/c/main/status.json", timeout=5) as r:
            check("status: status.json answers", r.status == 200)

        def post(path, headers=None):
            req = urllib.request.Request(base + path, method="POST", data=b"", headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
            except Exception as e:
                return repr(e)

        with urllib.request.urlopen(base + "/c/radio/status.json", timeout=5) as r:
            check("allow_skip: status reports allow_skip false", json.loads(r.read()).get("allow_skip") is False)
        check("allow_skip: next is refused on a channel with allow_skip = false",
              post("/c/radio/skip?dir=next") == 403)
        check("allow_skip: back is refused on that channel", post("/c/radio/skip?dir=back") == 403)
        check("allow_skip: next still works on a normal channel",
              post("/c/main/skip?dir=next") in (200, 409))
        check("restart: refused without the admin password", post("/restart") == 403)
        check("restart: refused with a wrong admin password",
              post("/restart", {"X-Admin-Password": "nope"}) == 403)
        check("restart: station still running after the refused restarts", proc.poll() is None)

        time.sleep(4)
        log_path = os.path.join(work, "radio.log")
        text = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
        check("log: no tracebacks", "Traceback" not in text)
        check("log: no playout errors", "Playout error" not in text)
        check("log: a song was played", "PLAY:" in text)
        check("crossfade: station ran with crossfade_seconds = 1 without playout errors",
              "Playout error" not in text and "Traceback" not in text)
        check("batched breaks: station ran with break_batch = 2 without tracebacks",
              "Traceback" not in text)
    except Exception as e:
        check("integration run", False, repr(e))
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for s in servers:
            s.shutdown()
        if keep:
            print(f"Kept temp folder: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--unit-only", action="store_true", help="skip the live station run")
    args = ap.parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg and ffprobe must be on PATH")
        return 2
    sys.path.insert(0, HERE)
    import radio as station_module
    unit_checks(station_module)
    if not args.unit_only:
        integration(args.keep)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
