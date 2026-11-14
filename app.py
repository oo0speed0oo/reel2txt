#!/usr/bin/env python3
"""reel2txt v2: paste links into a web page; the box downloads the video (or reads the
text/image post), transcribes it, grabs frames, writes a short summary, says which of your
projects it could help, and keeps a browsable library (index.html, projects.html, index.txt).

Run:  python3 app.py        (see README.md for settings and install)
"""
import base64
import email.utils
import hmac
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import Flask, Response, abort, jsonify, request, send_from_directory

# --------------------------------------------------------------------- config


def env(name, default=""):
    return os.environ.get(name, default)


APP_DIR = Path(__file__).resolve().parent
DATA = Path(env("R2T_DATA", str(Path.home() / "reel2txt-data"))).expanduser()
LIB = DATA / "library"
WORK = DATA / "work"
STATE = DATA / "items.json"
PROJECTS_FILE = DATA / "projects.md"
CATEGORIES_FILE = DATA / "categories.md"

HOST = env("R2T_HOST", "0.0.0.0")
PORT = int(env("R2T_PORT", "8090"))
PASSWORD = env("R2T_PASSWORD", "")  # optional; empty = no login
COOKIES_BROWSER = env("R2T_COOKIES_BROWSER", "firefox")  # "" to disable
COOKIES_FILE = env("R2T_COOKIES_FILE", "")  # optional cookies.txt instead
WHISPER_MODEL = env("R2T_WHISPER_MODEL", "small")
WHISPER_DEVICE = env("R2T_DEVICE", "auto")  # auto | cuda | cpu
OLLAMA_URL = env("R2T_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = env("R2T_OLLAMA_MODEL", "")  # empty = pick smallest installed
FRAME_EVERY = float(env("R2T_FRAME_EVERY", "4"))
MAX_FRAMES = int(env("R2T_MAX_FRAMES", "24"))
MAX_HEIGHT = int(env("R2T_MAX_HEIGHT", "720"))
MIN_FREE_GB = float(env("R2T_MIN_FREE_GB", "5"))
PAUSE_BETWEEN = float(env("R2T_PAUSE", "6"))
BLOCK_TRANSCRIPT_CHARS = int(env("R2T_BLOCK_CHARS", "12000"))
ALLOW_THIRD_PARTY = env("R2T_ALLOW_THIRD_PARTY", "0") == "1"  # fxtwitter.com fallback for X posts
UA = "Mozilla/5.0 (X11; Linux x86_64) reel2txt/2.6"
IMPORT_ROOT = Path(env("R2T_IMPORT_ROOT", str(Path.home()))).expanduser().resolve()  # import only from inside this
DEFAULT_IMPORT = env("R2T_IMPORT_DIR", "~/Downloads")
SECRETS_FILE = Path(env("R2T_SECRETS", "~/.reel2txt-secrets")).expanduser()  # IG_USER= / IG_PASS= lines, chmod 600
IG_SESSION = DATA / "ig-session-cookies.txt"  # refreshed automatically from the login above
IG_REFRESH_DAYS = float(env("R2T_IG_REFRESH_DAYS", "6"))
ig_state = {"msg": ""}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

for d in (DATA, LIB, WORK):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
lock = threading.RLock()
items = []
wake = threading.Event()


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ state


def load_state():
    global items
    if STATE.exists():
        try:
            items = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            items = []
    for it in items:
        if it.get("status") == "running":
            it["status"] = "queued"
            it["step"] = "restarted, waiting"


def save_state():
    with lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE)


def update(item, **kw):
    with lock:
        item.update(kw)
        save_state()


# ---------------------------------------------------------------- helpers


def safe(s, n=40):
    s = re.sub(r"[^\w\-]+", "_", s or "", flags=re.UNICODE).strip("_")
    return s[:n] or "unknown"


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def free_gb():
    return shutil.disk_usage(DATA).free / 1e9


def hashtags(text):
    return list(dict.fromkeys(re.findall(r"#[\w\u3040-\u30ff\u4e00-\u9fff]+", text or "")))


def platform_of(url):
    h = (urlparse(url).hostname or "").lower()
    for key, name in (("instagram", "Instagram"), ("tiktok", "TikTok"), ("youtube", "YouTube"), ("youtu.be", "YouTube"),
                      ("twitter", "X"), ("x.com", "X"), ("facebook", "Facebook"), ("threads", "Threads")):
        if key in h:
            return name
    return h or "Web"


def is_x(url):
    return platform_of(url) == "X"


# ----------------------------------------------------------- projects (v2)

_proj_cache = {"mtime": None, "projects": []}
_cat_cache = {"mtime": None, "projects": []}


def _norm(n):
    return re.sub(r"\s+", " ", str(n or "").strip().lower())


def _parse_md(path):
    """'## Name' then 'What:', 'Needs:', 'Keywords:', 'Status:' lines."""
    out, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            cur = {"name": line[3:].strip(), "what": "", "needs": "", "keywords": [], "status": ""}
            if not cur["name"].lower().startswith("not sure"):
                out.append(cur)
            else:
                cur = None
        elif cur is not None:
            m = re.match(r"(What|Needs|Keywords|Status):\s*(.*)", line)
            if m:
                k, v = m.group(1).lower(), m.group(2).strip()
                if k == "keywords":
                    cur["keywords"] = [x.strip().lower() for x in v.split(",") if x.strip()]
                else:
                    cur[k] = v
    return out


def merge_shipped(path, shipped_name):
    """Copy the shipped list if you have none; otherwise append entries you do not have yet (never edits yours)."""
    shipped = APP_DIR / shipped_name
    if not shipped.exists():
        return
    if not path.exists():
        shutil.copy(shipped, path)
        return
    have = {_norm(p["name"]) for p in _parse_md(path)}
    text = shipped.read_text(encoding="utf-8")
    chunks = re.split(r"(?m)^(?=## )", text)
    add = [c for c in chunks if c.startswith("## ") and _norm(c.splitlines()[0][3:]) not in have
           and not c.lower().startswith("## not sure")]
    if add:
        shutil.copy(path, path.with_suffix(".md.bak"))
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(c.rstrip() + "\n" for c in add))
        log(f"added {len(add)} new entries to {path.name}")


def load_projects():
    if not PROJECTS_FILE.exists():
        merge_shipped(PROJECTS_FILE, "projects.md")
        if not PROJECTS_FILE.exists():
            return []
    mt = PROJECTS_FILE.stat().st_mtime
    if _proj_cache["mtime"] != mt:
        _proj_cache.update(mtime=mt, projects=_parse_md(PROJECTS_FILE))
    return _proj_cache["projects"]


def load_categories():
    if not CATEGORIES_FILE.exists():
        merge_shipped(CATEGORIES_FILE, "categories.md")
        if not CATEGORIES_FILE.exists():
            return []
    mt = CATEGORIES_FILE.stat().st_mtime
    if _cat_cache["mtime"] != mt:
        _cat_cache.update(mtime=mt, projects=_parse_md(CATEGORIES_FILE))
    return _cat_cache["projects"]


def match_name(name, entries):
    """Exact name (any case) first, then a unique prefix. Returns the real name or None."""
    n = _norm(name)
    if not n:
        return None
    for p in entries:
        if _norm(p["name"]) == n:
            return p["name"]
    hits = [p["name"] for p in entries if _norm(p["name"]).startswith(n)]
    return hits[0] if len(hits) == 1 else None


def _score(entries, text, top):
    text = (text or "").lower()
    scored = []
    for p in entries:
        hits, score = [], 0
        for kw in p["keywords"]:
            n = len(re.findall(r"(?<![\w])" + re.escape(kw), text))
            if n:
                hits.append(kw)
                score += min(n, 3)
        if score >= 2:
            scored.append((score, p["name"], hits[:4]))
    scored.sort(reverse=True)
    return scored[:top]


def keyword_projects(text):
    return [{"name": n, "why": "matches words like: " + ", ".join(h)} for _, n, h in _score(load_projects(), text, 2)]


def keyword_category(text):
    r = _score(load_categories(), text, 1)
    return r[0][1] if r else ("Other" if match_name("Other", load_categories()) else "")


_fs_cache = {"t": 0, "text": ""}


def few_shot_examples(n=6):
    """Recent items you or Claude sorted by hand, shown to the local AI as examples of how you sort."""
    if time.time() - _fs_cache["t"] < 60:
        return _fs_cache["text"]
    rows = [m for m in all_metas() if m.get("projects_by") in ("you", "claude") or m.get("category_by") in ("you", "claude")]
    rows.sort(key=lambda m: (m.get("projects_by") == "you" or m.get("category_by") == "you", m.get("added", "")), reverse=True)
    lines = []
    for m in rows[:n]:
        ps = "; ".join(p["name"] for p in m.get("projects") or []) or "none"
        lines.append(f'- "{(m.get("summary") or m.get("title") or "")[:140]}" -> projects: {ps}; category: {m.get("category", "")}')
    _fs_cache.update(t=time.time(), text="\n".join(lines))
    return _fs_cache["text"]


def classify(title, caption, plain, ocr):
    """Returns (projects, how, category). Local Ollama first, keywords as backup."""
    projs = load_projects()
    cats = load_categories()
    material = (f"Title: {title}\nCaption/post text: {(caption or '')[:1500]}\n"
                f"Spoken: {(plain or '')[:2500]}\nOn-screen text: {' '.join(ocr or [])[:500]}")
    if not projs and not cats:
        return [], "none", ""
    listing = "\n".join(f"{i + 1}. {p['name']}: {p['what'][:170]} (keywords: {', '.join(p['keywords'][:8])})"
                        for i, p in enumerate(projs))
    catlist = "\n".join(f"- {c['name']}: {c['what'][:100]}" for c in cats)
    ex = few_shot_examples()
    prompt = ("You sort saved videos and posts. Rule: if the item could genuinely help one of the user's projects, "
              "list that project. Every item also gets exactly one category.\n\nProjects:\n" + listing +
              "\n\nCategories:\n" + catlist +
              (("\n\nExamples of how the user sorts:\n" + ex) if ex else "") +
              "\n\nSaved item:\n" + material +
              "\n\nChoose at most 2 projects (empty list if none really fits) with one short sentence on what would help, "
              "and exactly one category name. Reply with JSON only: "
              '{"matches":[{"project":"<exact project name>","why":"<one short sentence>"}],"category":"<exact category name>"}')
    try:
        body = json.dumps({"model": pick_ollama_model(), "stream": False, "format": "json", "keep_alive": "2m",
                           "options": {"num_ctx": 4096, "temperature": 0.1}, "prompt": prompt}).encode()
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as r:
            data = json.loads(json.load(r).get("response", "{}"))
        out = []
        for m in data.get("matches", [])[:2]:
            n = match_name(m.get("project"), projs)
            if n and n not in [o["name"] for o in out]:
                out.append({"name": n, "why": str(m.get("why", ""))[:200]})
        cat = match_name(data.get("category"), cats) or keyword_category(material)
        return out, "ai", cat
    except Exception as e:
        log("sorting by AI unavailable, using keywords:", e)
        return keyword_projects(material), "keywords", keyword_category(material)


# ------------------------------------------------------------- video path


def download(url, workdir, use_cookies=True):
    import yt_dlp

    opts = {
        "quiet": True, "noprogress": True, "no_warnings": True, "noplaylist": True,
        "retries": 3, "socket_timeout": 30,
        "format": f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "video.%(ext)s"),
    }
    if use_cookies:
        if COOKIES_FILE and Path(COOKIES_FILE).expanduser().exists():
            opts["cookiefile"] = str(Path(COOKIES_FILE).expanduser())
        elif IG_SESSION.exists() and platform_of(url) == "Instagram":
            shutil.copy(IG_SESSION, workdir / "ig-cookies.txt")  # a copy, so the original is never rewritten
            opts["cookiefile"] = str(workdir / "ig-cookies.txt")
        elif COOKIES_BROWSER:
            opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        vid = safe(str(info.get("id", "x")), 24)
        uploader = safe(info.get("uploader") or info.get("channel") or "", 24)
        d = info.get("upload_date") or datetime.now().strftime("%Y%m%d")
        folder = f"{d[:4]}-{d[4:6]}-{d[6:8]}_{uploader}_{vid}"
        if (LIB / folder / "meta.json").exists():
            return info, folder, None
        if free_gb() < MIN_FREE_GB:
            raise RuntimeError(f"Low disk space ({free_gb():.1f} GB free, need {MIN_FREE_GB}).")
        ydl.process_ie_result(info, download=True)
    vids = sorted(workdir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
    vids = [v for v in vids if v.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")]
    if not vids:
        raise RuntimeError("No video file came back (maybe an image post).")
    return info, folder, vids[0]


def get_video(url, workdir):
    try:
        return download(url, workdir, use_cookies=True)
    except Exception as e:
        msg = str(e).lower()
        if (COOKIES_BROWSER or COOKIES_FILE) and any(w in msg for w in ("cookie", "firefox", "profile", "keyring")):
            log("cookie load failed, retrying without cookies:", e)
            return download(url, workdir, use_cookies=False)
        raise


_wmodel = {}
_good_device = None


def devices_to_try():
    if WHISPER_DEVICE in ("cuda", "cpu"):
        return [WHISPER_DEVICE]
    out = []
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            out.append("cuda")
    except Exception:
        pass
    out.append("cpu")
    return out


def load_audio(video):
    """Decode audio with ffmpeg to 16 kHz mono floats (does not depend on the 'av' package)."""
    import numpy as np

    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "f32le", "-"], capture_output=True, timeout=900)
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, dtype=np.float32)


def transcribe(video, workdir):
    """Returns (language, [(start, text), ...], device_used)."""
    global _good_device
    from faster_whisper import WhisperModel

    audio = load_audio(video)
    if audio is None:
        return "", [], "none (no audio track)"
    errors = []
    order = [_good_device] if _good_device else devices_to_try()
    for dev in order:
        try:
            if dev not in _wmodel:
                _wmodel[dev] = WhisperModel(WHISPER_MODEL, device=dev, compute_type="float16" if dev == "cuda" else "int8")
            segs, info = _wmodel[dev].transcribe(audio, vad_filter=True, beam_size=1)
            out = [(s.start, s.text.strip()) for s in segs if s.text.strip()]
            _good_device = dev
            return info.language, out, dev
        except Exception as e:
            errors.append(f"{dev}: {e}")
            _wmodel.pop(dev, None)
            log("whisper failed on", dev, e)
    raise RuntimeError("Transcription failed: " + " | ".join(errors))


def extract_frames(video, outdir, duration):
    outdir.mkdir(exist_ok=True)
    interval = max(FRAME_EVERY, (duration or 60) / MAX_FRAMES)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
         f"fps=1/{interval:.2f},scale=720:-2", "-q:v", "4", str(outdir / "f_%03d.jpg")])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
         "select=gt(scene\\,0.4),scale=720:-2", "-vsync", "vfr", "-frames:v", "8", "-q:v", "4",
         str(outdir / "scene_%02d.jpg")])
    return sorted(outdir.glob("*.jpg"))


def ocr_frames(frames):
    if not shutil.which("tesseract") or not frames:
        return []
    langs = run(["tesseract", "--list-langs"]).stdout
    lang = "jpn+eng" if "jpn" in langs else "eng"
    step = max(1, len(frames) // 6)
    lines, seen = [], set()
    for f in frames[::step][:6]:
        try:
            t = run(["tesseract", str(f), "stdout", "-l", lang], timeout=60).stdout
        except Exception:
            continue
        for ln in t.splitlines():
            ln = ln.strip()
            if len(ln) >= 4 and ln not in seen:
                seen.add(ln)
                lines.append(ln)
    return lines[:40]


_omodel = None


def pick_ollama_model():
    global _omodel
    if OLLAMA_MODEL:
        return OLLAMA_MODEL
    if _omodel:
        return _omodel
    with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
        models = json.load(r).get("models", [])
    models = [m for m in models if "embed" not in m["name"].lower()]
    if not models:
        raise RuntimeError("no ollama models")
    _omodel = min(models, key=lambda m: m.get("size", 1e18))["name"]
    return _omodel


def summarize(title, caption, transcript):
    """One generic line: what is this about. Local Ollama if possible, else a plain fallback."""
    material = f"Title: {title}\nCaption: {caption}\nSpoken or on screen: {transcript[:3000]}"
    try:
        body = json.dumps({
            "model": pick_ollama_model(), "stream": False, "keep_alive": "2m",
            "options": {"num_ctx": 4096, "temperature": 0.2},
            "prompt": ("Write ONE plain sentence (max 25 words, English) saying what this saved video, post, web page or document is "
                       "about and what it would be useful for. No preamble, no quotes.\n\n" + material),
        }).encode()
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            s = json.load(r).get("response", "").strip().splitlines()[0].strip()
        if s:
            return s[:300]
    except Exception as e:
        log("ollama summary unavailable:", e)
    base = re.sub(r"#\S+", "", caption or "").strip() or transcript.strip() or title or "(no text found)"
    return base.replace("\n", " ")[:160]


# -------------------------------------------------------- post path (v2)
# For posts with no video: X text/image posts, Instagram image posts and carousels.


def _to36(n):
    digs = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip = int(n)
    fp = n - ip
    s = ""
    while ip:
        s = digs[ip % 36] + s
        ip //= 36
    frac = ""
    for _ in range(11):
        fp *= 36
        d = int(fp)
        frac += digs[d]
        fp -= d
    return (s or "0") + "." + frac


def _http_get(url, timeout=30, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else json.loads(data.decode("utf-8"))


def _save_images(urls, workdir):
    files = []
    for i, u in enumerate(urls, 1):
        try:
            p = workdir / f"raw_{i:02d}"
            p.write_bytes(_http_get(u, binary=True))
            files.append(p)
        except Exception as e:
            log("image download failed:", u, e)
    return files


def x_post_embed(url, workdir):
    """X's own public embed data (works for public posts, no login)."""
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        raise RuntimeError("not a post link")
    tid = m.group(1)
    token = re.sub(r"(0+|\.)", "", _to36(int(tid) / 1e15 * math.pi))
    data = _http_get(f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=en&token={token}")
    text = (data.get("text") or "").strip()
    if not text and not data.get("photos") and not data.get("mediaDetails"):
        raise RuntimeError("post is not available (deleted, private or blocked)")
    for u in (data.get("entities") or {}).get("urls", []):
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    q = data.get("quoted_tweet")
    if q and q.get("text"):
        text += f"\n\n[Quoted post by @{(q.get('user') or {}).get('screen_name', '?')}] {q['text']}"
    photos = [p.get("url") for p in data.get("photos", []) if p.get("url")]
    if not photos:
        photos = [d.get("media_url_https") for d in data.get("mediaDetails", []) if d.get("type") == "photo"]
    photos = [p + ("&" if "?" in p else "?") + "name=large" for p in photos if p]
    user = data.get("user") or {}
    return {"platform": "X", "id": tid, "uploader": user.get("screen_name", ""), "text": text,
            "date": (data.get("created_at") or "")[:10], "image_files": _save_images(photos, workdir)}


def x_post_third_party(url, workdir):
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        raise RuntimeError("not a post link")
    tid = m.group(1)
    t = _http_get(f"https://api.fxtwitter.com/i/status/{tid}").get("tweet") or {}
    if not t:
        raise RuntimeError("fxtwitter had nothing")
    try:
        date = email.utils.parsedate_to_datetime(t.get("created_at", "")).strftime("%Y-%m-%d")
    except Exception:
        date = ""
    photos = [p.get("url") for p in (t.get("media") or {}).get("photos", []) if p.get("url")]
    return {"platform": "X", "id": tid, "uploader": (t.get("author") or {}).get("screen_name", ""),
            "text": t.get("text", ""), "date": date, "image_files": _save_images(photos, workdir)}


def _first(d, *keys):
    for k in keys:
        v = d
        for part in k.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v:
            return v
    return ""


def run_gallery_dl(url, gdir, use_cookies):
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(gdir), "-f", "{num}.{extension}",
           "--write-metadata", "-o", "text-tweets=true"]
    if use_cookies:
        if COOKIES_FILE and Path(COOKIES_FILE).expanduser().exists():
            cmd += ["--cookies", str(Path(COOKIES_FILE).expanduser())]
        elif IG_SESSION.exists() and platform_of(url) == "Instagram":
            cmd += ["--cookies", str(IG_SESSION)]
        elif COOKIES_BROWSER:
            cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    r = run(cmd + [url], timeout=240)
    return r


def gallery_post(url, workdir):
    gdir = workdir / "g"
    gdir.mkdir(exist_ok=True)
    r = run_gallery_dl(url, gdir, True)
    err = (r.stderr or "").lower()
    if r.returncode != 0 and not list(gdir.iterdir()) and any(w in err for w in ("cookie", "firefox", "profile", "keyring")):
        log("gallery-dl cookie load failed, retrying without cookies")
        r = run_gallery_dl(url, gdir, False)
    imgs = sorted(p for p in gdir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"))
    metas = sorted(gdir.glob("*.json"))
    if not imgs and not metas:
        tail = (r.stderr or r.stdout or "nothing returned").strip().splitlines()[-1][:200]
        raise RuntimeError("gallery-dl found nothing: " + tail)
    meta = {}
    if metas:
        try:
            meta = json.loads(metas[0].read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    pid = str(_first(meta, "shortcode", "post_shortcode", "tweet_id", "post_id", "id") or safe(url.split("/")[-2], 20))
    date = str(_first(meta, "date", "post_date", "created_at") or "")[:10]
    return {"platform": platform_of(url), "id": pid,
            "uploader": str(_first(meta, "username", "author.name", "user.name", "author.nick") or ""),
            "text": str(_first(meta, "description", "content", "caption", "title") or "").strip(),
            "date": date, "image_files": imgs}


def ig_creds():
    """Reads the burner login from the private secrets file. Never shown, never logged."""
    try:
        user = pw = ""
        for ln in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition("=")
            if k.strip() == "IG_USER":
                user = v.strip()
            elif k.strip() == "IG_PASS":
                pw = v.rstrip("\r\n")
        return (user, pw) if user and pw else ("", "")
    except OSError:
        return ("", "")


def scrub(text):
    _, pw = ig_creds()
    return str(text).replace(pw, "***") if pw else str(text)


def ensure_instagram_session(url, force=False):
    """Logs in with the burner account (through gallery-dl) and saves fresh session cookies.
    Runs only for Instagram links, only if a login is stored, and only when the session is old."""
    if platform_of(url) != "Instagram":
        return
    user, pw = ig_creds()
    if not user:
        ig_state["msg"] = ""
        return
    fresh = IG_SESSION.exists() and time.time() - IG_SESSION.stat().st_mtime < IG_REFRESH_DAYS * 86400
    if fresh and not force:
        return
    cfg = WORK / "gdl-login.json"
    try:
        cfg.write_text(json.dumps({"extractor": {"instagram": {"username": user, "password": pw}}}), encoding="utf-8")
        os.chmod(cfg, 0o600)
        tmp = DATA / "ig-session.tmp"
        tmp.unlink(missing_ok=True)
        r = run([sys.executable, "-m", "gallery_dl", "--config-json", str(cfg), "-s",
                 "--cookies-export", str(tmp), url], timeout=240)
        if tmp.exists() and "sessionid" in tmp.read_text(encoding="utf-8", errors="replace"):
            os.chmod(tmp, 0o600)
            tmp.replace(IG_SESSION)
            ig_state["msg"] = "logged in"
            log("instagram session refreshed")
        else:
            tail = scrub((r.stderr or r.stdout or "no answer").strip().splitlines()[-1] if (r.stderr or r.stdout) else "no answer")[:160]
            ig_state["msg"] = "login failed: " + tail
            log("instagram login failed:", tail)
    except Exception as e:
        ig_state["msg"] = "login failed: " + scrub(e)[:160]
        log("instagram login error:", scrub(e))
    finally:
        cfg.unlink(missing_ok=True)


def ig_status():
    if not ig_creds()[0]:
        return "no login stored (using cookies if set)"
    if ig_state["msg"]:
        return ig_state["msg"]
    if IG_SESSION.exists():
        return f"logged in, session {int((time.time() - IG_SESSION.stat().st_mtime) / 86400)} days old (refreshes by itself)"
    return "login stored, will sign in on the next Instagram link"


def fetch_post(url, workdir):
    errors = []
    if is_x(url):
        try:
            return x_post_embed(url, workdir)
        except Exception as e:
            errors.append(f"X embed: {e}")
        if ALLOW_THIRD_PARTY:
            try:
                return x_post_third_party(url, workdir)
            except Exception as e:
                errors.append(f"fxtwitter: {e}")
    try:
        return gallery_post(url, workdir)
    except Exception as e:
        errors.append(f"gallery-dl: {e}")
    raise RuntimeError("Could not read this post. " + " | ".join(errors))


def convert_images(files, outdir):
    outdir.mkdir(exist_ok=True)
    out = []
    for i, f in enumerate(files, 1):
        dst = outdir / f"img_{i:02d}.jpg"
        r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(f), "-vf", "scale='min(1080,iw)':-2",
                 "-frames:v", "1", "-q:v", "3", str(dst)])
        if r.returncode == 0 and dst.exists():
            out.append(dst)
    return out


# ----------------------------------------------------------- page output

COPY_JS = """<script>
function copyText(id,btn){var t=document.getElementById(id).value;
function done(){var o=btn.dataset.o||btn.textContent;btn.dataset.o=o;btn.textContent='Copied';setTimeout(function(){btn.textContent=o},1500)}
function old(){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity=0;
document.body.appendChild(a);a.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(a);done()}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done,old)}else{old()}}
function saveProj(f){var v=[document.getElementById('p1').value,document.getElementById('p2').value].filter(Boolean);
fetch('/api/projects/'+f,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:v,category:(document.getElementById('c1')||{}).value||''})})
.then(function(r){return r.json()}).then(function(){location.reload()})}
</script>"""

CSS = """<style>
:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--card:#f4f4f6;--line:#dcdce0;--acc:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#9a9aa3;--card:#1f1f23;--line:#33333a;--acc:#6b9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1.1rem;margin:1.4em 0 .4em}
a{color:var(--acc)}.mut{color:var(--mut);font-size:.9rem}
textarea,input[type=search],select{width:100%;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit}
select{width:auto;max-width:100%}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer}
button.sec{background:var(--card);color:var(--fg);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top;font-size:.93rem}
.tag{display:inline-block;border-radius:99px;padding:1px 9px;font-size:.8rem;background:var(--card);border:1px solid var(--line);margin:1px 2px 1px 0}
.use{border-left:4px solid var(--acc);background:var(--card);padding:8px 12px;border-radius:6px;margin:10px 0}
.frames img{height:110px;margin:3px;border-radius:6px;border:1px solid var(--line)}
.frames.big img{height:auto;max-width:100%;max-height:420px}
pre{white-space:pre-wrap;background:var(--card);padding:12px;border-radius:8px;border:1px solid var(--line);overflow-wrap:anywhere}
video{max-width:100%;border-radius:8px}
</style>"""


def read_plain(outdir):
    p = outdir / "plain.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    t = outdir / "transcript.txt"
    if t.exists():
        return " ".join(re.sub(r"^\[\d+:\d+\]\s*", "", ln) for ln in t.read_text(encoding="utf-8").splitlines()
                        if not ln.startswith("(no speech"))
    return ""


def build_block(meta, plain):
    kind = meta.get("kind")
    post = kind == "post"
    doc = kind == "doc"
    cut = ""
    parts = ["Please look through our past chats for the project this relates to, and tell me whether it could "
             "improve what we are working on. Frames or images are attached if I added them.", ""]
    parts += [f"# {'Post' if post else 'Document' if doc else 'Video'}: {meta.get('title') or meta['folder']}",
              f"Source: {meta['url']}",
              (f"Author: {meta.get('uploader', '')} | Posted: {meta.get('date', '')} | Images: {len(meta.get('images', []))}"
               if post else
               f"Type: {meta.get('platform', '')} | From: {meta.get('uploader', '')} | Date: {meta.get('date', '')} | Pages: {meta.get('pages') or 'n/a'} | Characters: {meta.get('chars', 0)}"
               if doc else
               f"Uploader: {meta.get('uploader', '')} | Posted: {meta.get('date', '')} | Length: {mmss(meta.get('duration'))} | Speech language: {meta.get('language') or 'n/a'}"),
              f"One-line summary: {meta.get('summary', '')}"]
    projs = meta.get("projects") or []
    parts += ["", "## Where the app thinks it fits (my projects)"]
    parts += [f"- {p['name']}: {p.get('why', '')}" for p in projs] or ["- No project fit"]
    parts += [f"Category: {meta.get('category') or 'not set'}"]
    if doc:
        t = plain or "(no text)"
        if len(t) > BLOCK_TRANSCRIPT_CHARS:
            t = t[:BLOCK_TRANSCRIPT_CHARS]
            cut = "\n[text cut for length; the full text is in text.txt]"
        parts += ["", "## Document text", t + cut]
        if meta.get("note"):
            parts += ["", "Note: " + meta["note"]]
        return "\n".join(parts)
    parts += ["", "## Post text" if post else "## Post caption", meta.get("caption") or "(none)"]
    if not post:
        t = plain if plain else "(no speech detected)"
        if len(t) > BLOCK_TRANSCRIPT_CHARS:
            t = t[:BLOCK_TRANSCRIPT_CHARS]
            cut = "\n[transcript cut for length; full text is in transcript.txt]"
        parts += ["", "## Transcript", t + cut]
    if meta.get("ocr"):
        parts += ["", "## On-screen text (OCR, may be rough)", "\n".join(meta["ocr"])]
    return "\n".join(parts)


def write_item_files(outdir, meta, plain):
    e = html.escape
    post = meta.get("kind") == "post"
    doc = meta.get("kind") == "doc"
    block = build_block(meta, plain)
    (outdir / "summary.md").write_text(block, encoding="utf-8")
    projs = meta.get("projects") or []
    by = BYLABEL.get(meta.get("projects_by", ""), "")
    chips = "".join(f'<span class="tag">{e(p["name"])}</span> <span class="mut">{e(p.get("why", ""))}</span><br>' for p in projs)
    use = (f'<div class="use"><b>You can use this in:</b><br>{chips}<span class="mut">{e(by)}</span></div>' if projs else
           '<div class="use"><b>No project fit yet.</b> <span class="mut">It is filed by category. Pick a project below if you see one.</span></div>')
    names = [p["name"] for p in load_projects()]
    cur = [p["name"] for p in projs] + ["", ""]

    def sel(idn, chosen):
        opts = '<option value="">(none)</option>' + "".join(
            f'<option value="{e(n)}"{" selected" if n == chosen else ""}>{e(n)}</option>' for n in names)
        return f'<select id="{idn}">{opts}</select>'

    catnames = [c["name"] for c in load_categories()]
    cat = meta.get("category", "")
    catsel = ('<select id="c1"><option value="">(none)</option>' + "".join(
        f'<option value="{e(n)}"{" selected" if n == cat else ""}>{e(n)}</option>' for n in catnames) + '</select>')
    form = (f'<p><b>Category:</b> {e(cat or "not set")} <span class="mut">{e(BYLABEL.get(meta.get("category_by", ""), ""))}</span></p>'
            f'<p class="mut">Change it: {sel("p1", cur[0])} {sel("p2", cur[1])} {catsel} '
            f'<button class="sec" onclick="saveProj(\'{quote(meta["folder"])}\')">Save</button></p>')
    if doc:
        imgs = "".join(f'<a href="images/{quote(f)}"><img src="images/{quote(f)}" loading="lazy"></a>' for f in meta.get("images", []))
        prev = (plain or "")[:3000] + ("\n[...]" if len(plain or "") > 3000 else "")
        media = (f'<h2>Text</h2><pre>{e(prev or "(no text)")}</pre><p><a href="text.txt">Open the full text</a></p>' +
                 (f'<h2>First page</h2><div class="frames big">{imgs}</div>' if imgs else "") +
                 (f'<p class="mut">{e(meta["note"])}</p>' if meta.get("note") else ""))
    elif post:
        imgs = "".join(f'<a href="images/{quote(f)}"><img src="images/{quote(f)}" loading="lazy"></a>' for f in meta.get("images", []))
        media = (f'<h2>Post text</h2><pre>{e(meta.get("caption") or "(no text)")}</pre>'
                 f'<h2>Images</h2><div class="frames big">{imgs or "<span class=mut>none</span>"}</div>')
    else:
        imgs = "".join(f'<a href="frames/{quote(f)}"><img src="frames/{quote(f)}" loading="lazy"></a>' for f in meta.get("frames", []))
        vid = (f'<video controls preload="metadata" src="{quote(meta["video_file"])}"></video>' if meta.get("has_video") else
               '<p class="mut">Video was not kept for this one.</p>')
        media = f'<h2>Frames</h2><div class="frames">{imgs or "<span class=mut>none</span>"}</div><h2>Video</h2>{vid}'
    sub = (f'{e(meta.get("uploader", ""))} &middot; {e(meta.get("date", ""))} &middot; ' +
           (f'{meta.get("pages") or ""} {"pages" if meta.get("pages") else "web page"}' if doc else
            f'{len(meta.get("images", []))} image(s)' if post else mmss(meta.get("duration"))))
    files = ('<a href="summary.md">summary.md</a> &middot; <a href="meta.json">meta.json</a>' +
             (' &middot; <a href="text.txt">text.txt</a>' + (' &middot; <a href="original.pdf">original.pdf</a>' if meta.get("doc_file") else '')
              if doc else '' if post else ' &middot; <a href="transcript.txt">transcript.txt</a>'))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(meta.get('title') or meta['folder'])}</title>{CSS}</head><body><main>
<p><a href="../../index.html">&larr; Full library</a> &middot; <a href="../../projects.html">By project</a></p>
<h1>{e(meta.get('title') or meta['folder'])}</h1>
<p class="mut">{sub} &middot; {('<a href="' + e(meta['url']) + '">original link</a>') if str(meta['url']).startswith('http') else ('file: ' + e(meta['url']))}</p>
<p>{e(meta.get('summary', ''))}</p>
{use}{form}
<p><button onclick="copyText('blk',this)">Copy for Claude</button></p>
<textarea id="blk" readonly rows="10">{e(block)}</textarea>
{media}
<h2>Files</h2><p>{files}</p>
</main>{COPY_JS}</body></html>"""
    (outdir / "index.html").write_text(page, encoding="utf-8")


def all_metas():
    metas = []
    for p in LIB.glob("*/meta.json"):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    metas.sort(key=lambda m: m.get("added", ""), reverse=True)
    return metas


def rebuild_index():
    metas = all_metas()
    e = html.escape
    txt = [f"reel2txt library: {len(metas)} items (newest first)", ""]
    rows = []
    for m in metas:
        ps = m.get("projects") or []
        kind = m.get("kind") if m.get("kind") in ("post", "doc") else "video"
        txt.append(f"{m['folder']}\n    {m.get('platform', '')} {kind} | {m.get('uploader', '')} | {m.get('date', '')}\n"
                   f"    {m.get('summary', '')}\n    Use in: {', '.join(p['name'] for p in ps) or 'No project'}\n    Category: {m.get('category') or 'not set'}\n")
        f = quote(m["folder"])
        chips = "".join(f'<span class="tag">{e(p["name"])}</span>' for p in ps) or '<span class="mut">No project</span>'
        rows.append(
            f'<tr data-t="{e((m["folder"] + " " + m.get("summary", "") + " " + m.get("uploader", "") + " " + " ".join(p["name"] for p in ps) + " " + m.get("category", "")).lower())}">'
            f'<td>{e(m.get("added", "")[:10])}</td>'
            f'<td><a href="library/{f}/index.html">{e(m["folder"])}</a></td>'
            f'<td>{e(m.get("platform", ""))} {kind}</td><td>{e(m.get("summary", ""))}</td><td>{chips}</td><td class="mut">{e(m.get("category", ""))}</td>'
            f'<td><a href="library/{f}/summary.md">summary</a></td></tr>')
    (DATA / "index.txt").write_text("\n".join(txt), encoding="utf-8")
    nav = '<p><a href="/">&larr; Add links</a> &middot; <a href="projects.html">By project</a> &middot; <a href="index.txt">index.txt</a></p>'
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>reel2txt library</title>{CSS}</head><body><main>{nav}
<h1>Library ({len(metas)})</h1>
<input type="search" id="q" placeholder="Search folder, summary, uploader, project" oninput="f()">
<table><tr><th>Added</th><th>Folder</th><th>Source</th><th>What it is</th><th>Use in</th><th>Category</th><th>Files</th></tr>{''.join(rows)}</table>
<script>function f(){{var q=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('tr[data-t]').forEach(function(r){{r.style.display=r.dataset.t.indexOf(q)<0?'none':''}})}}</script>
</main></body></html>"""
    (DATA / "index.html").write_text(page, encoding="utf-8")

    # by-project page: projects first, then everything with no project, by category
    def rowhtml(m, why=""):
        return (f'<tr><td>{e(m.get("added", "")[:10])}</td>'
                f'<td><a href="library/{quote(m["folder"])}/index.html">{e(m.get("title") or m["folder"])}</a>'
                f'<div class="mut">{e(m.get("summary", ""))}</div></td><td class="mut">{e(why)}</td></tr>')

    projs = load_projects()
    body = []
    for p in projs:
        its = [m for m in metas if any(x["name"] == p["name"] for x in (m.get("projects") or []))]
        li = "".join(rowhtml(m, next((x.get("why", "") for x in m["projects"] if x["name"] == p["name"]), "")) for m in its)
        sug = ' <span class="mut">(suggested)</span>' if p.get("status", "").lower().startswith("suggest") else ""
        body.append(f'<h2>{e(p["name"])}{sug} <span class="tag">{len(its)}</span></h2><p class="mut">{e(p["what"])}</p>' +
                    (f"<table>{li}</table>" if li else '<p class="mut">Nothing yet.</p>'))
    rest = [m for m in metas if not (m.get("projects") or [])]
    body.append(f'<hr><h1>Everything else, by category <span class="tag">{len(rest)}</span></h1>')
    seen = set()
    for c in load_categories() + [{"name": "", "what": "Not categorised yet"}]:
        its = [m for m in rest if (m.get("category") or "") == c["name"] and c["name"] not in seen]
        seen.add(c["name"])
        if its:
            body.append(f'<h2>{e(c["name"] or "Not categorised yet")} <span class="tag">{len(its)}</span></h2><table>' +
                        "".join(rowhtml(m) for m in its) + "</table>")
    odd = [m for m in rest if (m.get("category") or "") not in {c["name"] for c in load_categories()} | {""}]
    if odd:
        body.append(f'<h2>Other categories <span class="tag">{len(odd)}</span></h2><table>' + "".join(rowhtml(m) for m in odd) + "</table>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>reel2txt by project</title>{CSS}</head><body><main>
<p><a href="/">&larr; Add links</a> &middot; <a href="index.html">Full library</a> &middot; <a href="/edit-projects">Edit projects</a> &middot; <a href="/edit-categories">Edit categories</a> &middot; <a href="/apply">Apply a sorting file</a></p>
<h1>By project</h1><p class="mut">A link can appear under two projects. Items that help no project are listed further down by category. Open an item to change where it belongs.</p>
{''.join(body)}</main></body></html>"""
    (DATA / "projects.html").write_text(page, encoding="utf-8")
    write_for_claude(metas, projs)


def write_for_claude(metas, projs):
    """One plain file to paste into a chat: the instructions, the project and category lists, then every item in one line."""
    cats = load_categories()
    t = ["READ ME FIRST (for Claude): this is the owner's saved-links library from their Linux box.",
         "Job: sort items, answer questions about them, or reply with corrections in this exact line format, one per line:",
         "    folder | project 1; project 2 | category | one short reason",
         "Use project and category names exactly as listed below. Leave the project part as - if no project fits.", "",
         "PROJECTS (name: what it is)"]
    t += [f"- {p['name']}{' [suggested]' if p.get('status', '').lower().startswith('suggest') else ''}: {p['what'][:200]}" for p in projs]
    t += ["", "CATEGORIES (used when no project fits, and as a second label)"] + [f"- {c['name']}: {c['what'][:120]}" for c in cats]
    t += ["", f"ITEMS ({len(metas)}), newest first: folder | projects | category | summary"]
    for m in metas:
        t.append(f"{m['folder']} | {'; '.join(p['name'] for p in m.get('projects') or []) or '-'} | {m.get('category') or '-'} | {(m.get('summary') or '')[:220]}")
    (DATA / "for-claude.txt").write_text("\n".join(t), encoding="utf-8")


def new_meta_common(folder, url, info_title, uploader, date, caption, summary, ocr, platform, kind):
    return {"folder": folder, "url": url, "title": info_title, "uploader": uploader, "date": date,
            "added": datetime.now().isoformat(timespec="seconds"), "caption": caption,
            "hashtags": hashtags(caption), "summary": summary, "ocr": ocr, "platform": platform, "kind": kind}


# ------------------------------------------------------------- pipelines


def process_video(item, url, info, folder, video, workdir, move=True):
    outdir = LIB / folder
    outdir.mkdir(parents=True, exist_ok=True)
    duration = info.get("duration") or 0
    if not duration:
        try:
            duration = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", str(video)]).stdout.strip() or 0)
        except Exception:
            duration = 0
    caption = (info.get("description") or "").strip()
    title = (info.get("title") or "").strip()

    update(item, step="listening (speech to text)")
    lang, segs, device = transcribe(video, workdir)
    plain = " ".join(t for _, t in segs)
    (outdir / "transcript.txt").write_text(
        "\n".join(f"[{mmss(s)}] {t}" for s, t in segs) or "(no speech detected)", encoding="utf-8")
    (outdir / "plain.txt").write_text(plain, encoding="utf-8")

    update(item, step="grabbing frames")
    frames = extract_frames(video, outdir / "frames", duration)
    update(item, step="reading on-screen text")
    ocr = ocr_frames(frames)
    update(item, step="writing summary")
    summary = summarize(title, caption, plain)
    update(item, step="matching your projects")
    projs, by, cat = classify(title, caption, plain, ocr)

    has_video = False
    if item.get("keep_video", True):
        (shutil.move if move else shutil.copy2)(str(video), str(outdir / f"video{video.suffix}"))
        has_video = True
    d = info.get("upload_date") or ""
    meta = new_meta_common(folder, url, title, info.get("uploader") or info.get("channel") or "",
                           f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else "", caption, summary, ocr,
                           info.get("extractor_key", ""), "video")
    meta.update(duration=duration, language=lang, frames=[f.name for f in frames], has_video=has_video,
                video_file=f"video{video.suffix}" if has_video else "", speech_device=device,
                projects=projs, projects_by=by, category=cat, category_by=by)
    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    write_item_files(outdir, meta, plain)
    rebuild_index()
    update(item, status="done", step="finished", folder=folder, summary=summary)


def process_post(item, url, workdir, video_msg):
    update(item, step="no video found, reading the post")
    try:
        post = fetch_post(url, workdir)
    except Exception as e:
        if platform_of(url) not in SOCIAL:
            try:
                return process_web(item, url, workdir)
            except Exception as e2:
                log("web page read failed, printing it to PDF:", str(e2)[:150])
                try:
                    return process_rendered(item, url, workdir)
                except Exception as e3:
                    raise RuntimeError(f"Not a video, an image post or a readable page ({e2}). Printing it to PDF also failed: {e3}")
        raise RuntimeError(f"{e} (the video downloader said: {video_msg[:120]})")
    finish_post(item, url, post)


def finish_post(item, url, post):
    date = post.get("date") or datetime.now().strftime("%Y-%m-%d")
    folder = post.get("folder") or f"{date}_{safe(post.get('uploader', ''), 24)}_{safe(str(post['id']), 24)}"
    if (LIB / folder / "meta.json").exists():
        update(item, status="done", step="already in library", folder=folder)
        return
    if free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"Low disk space ({free_gb():.1f} GB free, need {MIN_FREE_GB}).")
    text = (post.get("text") or "").strip()
    outdir = LIB / folder
    outdir.mkdir(parents=True, exist_ok=True)
    update(item, step="saving images")
    imgs = convert_images(post.get("image_files", []), outdir / "images")
    if not text and not imgs:
        shutil.rmtree(outdir, ignore_errors=True)
        raise RuntimeError("The post had no text and no images I could save.")
    update(item, step="reading on-screen text")
    ocr = ocr_frames(imgs)
    title = (post.get("title") or (text.splitlines()[0] if text else f"{post['platform']} post"))[:80]
    update(item, step="writing summary")
    summary = summarize(title, text, " ".join(ocr))
    update(item, step="matching your projects")
    projs, by, cat = classify(title, text, "", ocr)
    meta = new_meta_common(folder, url, title, post.get("uploader", ""), date, text, summary, ocr,
                           post["platform"], "post")
    meta.update(images=[i.name for i in imgs], projects=projs, projects_by=by, category=cat, category_by=by, duration=0, language="",
                frames=[], has_video=False, video_file="")
    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "plain.txt").write_text("", encoding="utf-8")
    write_item_files(outdir, meta, "")
    rebuild_index()
    update(item, status="done", step="finished", folder=folder, summary=summary)


# ------------------------------------------- web pages and PDFs (v2.4)

from html.parser import HTMLParser

PDF_EXT = {".pdf"}
SOCIAL = {"Instagram", "TikTok", "YouTube", "X", "Facebook", "Threads"}
DOC_MAX_BYTES = int(env("R2T_DOC_MAX_MB", "60")) * 1024 * 1024
PDF_TEXT_PAGES = int(env("R2T_PDF_PAGES", "400"))  # read at most this many pages
PDF_OCR_PAGES = int(env("R2T_PDF_OCR_PAGES", "15"))  # scanned PDFs: OCR this many pages
MAX_TEXT_CHARS = 1_500_000


def is_pdf_url(url):
    return urlparse(url).path.lower().endswith(".pdf")


class _Readable(HTMLParser):
    """Pulls the readable text out of a web page: prefers <article>/<main>, drops menus, scripts and footers."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "iframe", "template", "button"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "main",
             "header", "blockquote", "pre", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.focus = 0
        self.all, self.foc = [], []
        self.title = ""
        self.in_title = False
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content"):
                self.meta.setdefault(k, a["content"].strip())
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
        if tag in ("article", "main"):
            self.focus += 1
        if tag in self.BLOCK:
            self._put("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in ("article", "main") and self.focus:
            self.focus -= 1
        if tag in self.BLOCK:
            self._put("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self._put(data)

    def _put(self, s):
        self.all.append(s)
        if self.focus:
            self.foc.append(s)


def _clean(parts):
    t = "".join(parts)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def html_to_doc(raw, ctype_header=""):
    m = re.search(r"charset=([\w\-]+)", ctype_header or "", re.I) or re.search(rb"charset=[\"']?([\w\-]+)", raw[:4000], re.I)
    enc = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
    try:
        s = raw.decode(enc, errors="replace")
    except LookupError:
        s = raw.decode("utf-8", errors="replace")
    p = _Readable()
    p.feed(s)
    focus, whole = _clean(p.foc), _clean(p.all)
    text = focus if len(focus) >= 400 else whole
    title = (p.meta.get("og:title") or p.title or "").strip()
    title = re.sub(r"\s+", " ", title)
    return {"title": title, "text": text, "site": p.meta.get("og:site_name", ""),
            "author": p.meta.get("author") or p.meta.get("article:author", ""),
            "date": (p.meta.get("article:published_time") or p.meta.get("og:updated_time") or "")[:10]}


def fetch_url(url, maxbytes=DOC_MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) reel2txt/2.6",
                                               "Accept": "text/html,application/pdf,*/*;q=0.8",
                                               "Accept-Language": "en,ja;q=0.8"})
    with urllib.request.urlopen(req, timeout=40) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(maxbytes + 1)
        final = r.geturl()
    if len(data) > maxbytes:
        raise RuntimeError(f"That file is bigger than {maxbytes // 1048576} MB, so I skipped it.")
    return data, ctype, final


def _tesseract_lang():
    langs = run(["tesseract", "--list-langs"]).stdout
    return "jpn+eng" if "jpn" in langs else "eng"


def pdf_extract(path, workdir):
    """Text of a PDF (OCR for scanned pages). Returns dict with text, title, pages, thumb, note."""
    note = ""
    title = ""
    text = ""
    pages = 0
    thumb = None
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        doc = pymupdf.open(str(path))
        if doc.needs_pass:
            raise RuntimeError("This PDF is password protected.")
        pages = len(doc)
        title = ((doc.metadata or {}).get("title") or "").strip()
        parts = []
        for i, pg in enumerate(doc):
            if i >= PDF_TEXT_PAGES:
                note = f"Only the first {PDF_TEXT_PAGES} of {pages} pages were read."
                break
            parts.append(pg.get_text())
        text = "\n\n".join(parts).strip()
        try:
            pix = doc[0].get_pixmap(dpi=70)
            thumb = workdir / "thumb.jpg"
            pix.save(str(thumb))
        except Exception:
            thumb = None
        read_pages = min(pages, PDF_TEXT_PAGES)
        if read_pages and len(re.sub(r"\s", "", text)) < 40 * read_pages:  # looks scanned
            if not shutil.which("tesseract"):
                note = (note + " This PDF looks scanned and tesseract is not installed, so there is little text.").strip()
            else:
                lang = _tesseract_lang()
                ocr = []
                for i in range(min(pages, PDF_OCR_PAGES)):
                    png = workdir / f"ocr_{i:03d}.png"
                    doc[i].get_pixmap(dpi=150).save(str(png))
                    ocr.append(run(["tesseract", str(png), "stdout", "-l", lang], timeout=120).stdout)
                    png.unlink(missing_ok=True)
                text = "\n\n".join(ocr).strip()
                note = (note + f" Scanned PDF: read {min(pages, PDF_OCR_PAGES)} of {pages} pages with OCR.").strip()
    except ImportError:
        if not shutil.which("pdftotext"):
            raise RuntimeError("PDF support is missing. Run update.sh again (it installs pymupdf).")
        text = run(["pdftotext", "-enc", "UTF-8", str(path), "-"], timeout=300).stdout.strip()
        note = "Read with pdftotext (no thumbnail, no OCR)."
    if not title:
        title = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 4), "")[:90]
    return {"text": text, "title": title, "pages": pages, "thumb": thumb, "note": note}


def process_web(item, url, workdir):
    """Any non-social link that is not a video: a web page or a PDF."""
    update(item, step="reading the web page")
    data, ctype, final = fetch_url(url)
    if "pdf" in ctype.lower() or data[:5] == b"%PDF-" or is_pdf_url(final):
        pdf = workdir / "download.pdf"
        pdf.write_bytes(data)
        return process_pdf(item, url, pdf, workdir, keep=item.get("keep_video", True), uploader=urlparse(final).hostname or "")
    if "html" not in ctype.lower() and not data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError(f"I can read web pages and PDFs, but this link returns {ctype or 'something else'}.")
    d = html_to_doc(data, ctype)
    if len(d["text"]) < 200:
        raise RuntimeError("This page has almost no readable text (it may need JavaScript or a login).")
    host = urlparse(final).hostname or "web"
    import hashlib

    h = hashlib.sha1(final.encode()).hexdigest()[:8]
    date = d["date"] or datetime.now().strftime("%Y-%m-%d")
    finish_doc(item, url, {"platform": "Web", "folder": f"{date}_{safe(host, 24)}_{h}", "uploader": d["author"] or d["site"] or host,
                           "title": d["title"] or host, "text": d["text"], "date": date, "pages": 0, "thumb": None,
                           "pdf": None, "note": ""})


def process_pdf(item, url, pdf, work, keep=True, uploader="", date=None, folder=None, platform="PDF", note=""):
    import hashlib

    st = pdf.stat()
    h = hashlib.sha1((url + str(st.st_size)).encode()).hexdigest()[:8]
    stem = safe(Path(urlparse(url).path).stem or pdf.stem, 28) if url.startswith("http") else safe(pdf.stem, 28)
    date = date or datetime.now().strftime("%Y-%m-%d")
    folder = folder or f"{date}_{stem}_{h}"
    if (LIB / folder / "meta.json").exists():
        update(item, status="done", step="already in library", folder=folder)
        return
    update(item, step="reading the PDF")
    r = pdf_extract(pdf, work)
    if len(r["text"]) < 40:
        raise RuntimeError("I could not find any text in this PDF. " + r["note"])
    finish_doc(item, url, {"platform": platform, "folder": folder, "uploader": uploader, "title": r["title"] or stem,
                           "text": r["text"], "date": date, "pages": r["pages"], "thumb": r["thumb"],
                           "pdf": pdf if keep else None, "note": (note + " " + r["note"]).strip()})


def finish_doc(item, url, doc):
    folder = doc["folder"]
    if (LIB / folder / "meta.json").exists():
        update(item, status="done", step="already in library", folder=folder)
        return
    if free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"Low disk space ({free_gb():.1f} GB free, need {MIN_FREE_GB}).")
    outdir = LIB / folder
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        text = doc["text"][:MAX_TEXT_CHARS]
        title = (doc["title"] or folder)[:100]
        (outdir / "text.txt").write_text(text, encoding="utf-8")
        (outdir / "plain.txt").write_text(text, encoding="utf-8")
        imgs = []
        if doc.get("thumb") and Path(doc["thumb"]).exists():
            (outdir / "images").mkdir(exist_ok=True)
            shutil.copy(doc["thumb"], outdir / "images" / "page_01.jpg")
            imgs = ["page_01.jpg"]
        pdf_name = ""
        if doc.get("pdf"):
            pdf_name = "original.pdf"
            shutil.copy2(str(doc["pdf"]), str(outdir / pdf_name))
        update(item, step="writing summary")
        summary = summarize(title, "", text[:3000])
        update(item, step="matching your projects")
        projs, by, cat = classify(title, "", text, [])
        meta = new_meta_common(folder, url, title, doc.get("uploader", ""), doc["date"], "", summary, [],
                               doc["platform"], "doc")
        meta.update(images=imgs, projects=projs, projects_by=by, category=cat, category_by=by, duration=0, language="",
                    frames=[], has_video=False, video_file="", pages=doc.get("pages", 0), chars=len(text),
                    doc_file=pdf_name, note=doc.get("note", ""))
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        write_item_files(outdir, meta, text)
    except Exception:
        shutil.rmtree(outdir, ignore_errors=True)
        raise
    rebuild_index()
    update(item, status="done", step="finished", folder=folder, summary=summary)


# ------------------------------------- pages that will not read: print to PDF (v2.6)

RENDER_TIMEOUT = int(env("R2T_RENDER_SECONDS", "90"))


def render_pdf(url, out):
    """Opens the page in a real (headless) browser, scrolls it so lazy parts load, and prints it to a PDF."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("The page-to-PDF helper is not installed. Run update.sh again.")
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:
            raise RuntimeError("The page-to-PDF browser is not ready (" + str(e).splitlines()[0][:120] + "). On the box run: "
                               "cd ~/reel2txt && ./venv/bin/playwright install chromium && sudo ./venv/bin/playwright install-deps chromium")
        try:
            ctx = br.new_context(user_agent=REDDIT_UA, viewport={"width": 1280, "height": 1800}, locale="en-US")
            pg = ctx.new_page()
            pg.set_default_timeout(RENDER_TIMEOUT * 1000)
            resp = pg.goto(url, wait_until="domcontentloaded")
            try:
                pg.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            for _ in range(12):  # scroll down so comments and lazy content load
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(500)
            pg.evaluate("window.scrollTo(0, 0)")
            if resp is not None and resp.status >= 400 and resp.status != 404:
                raise RuntimeError(f"the site answered HTTP {resp.status} even to a browser")
            pg.emulate_media(media="screen")
            pg.pdf(path=str(out), format="A4", print_background=True, margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"})
        finally:
            br.close()


def process_rendered(item, url, workdir, why=""):
    update(item, step="could not read it directly, printing the page to PDF")
    pdf = workdir / "page.pdf"
    render_pdf(url, pdf)
    host = urlparse(url).hostname or "web"
    process_pdf(item, url, pdf, workdir, keep=item.get("keep_video", True), uploader=host, platform="Web (printed to PDF)",
                note="The box could not read this page directly, so it printed the page to PDF and read that.")


# ------------------------------------------------------- Reddit (v2.5)

REDDIT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def is_reddit(url):
    h = (urlparse(url).hostname or "").lower()
    return h == "redd.it" or h.endswith("reddit.com")


def reddit_path(url):
    u = urlparse(url)
    if (u.hostname or "").lower() == "redd.it":
        return "/comments/" + u.path.strip("/").split("/")[0]
    return u.path.rstrip("/")


def reddit_json(url):
    path = reddit_path(url)
    if "/comments/" not in path:
        raise RuntimeError("Only Reddit post links (with /comments/ in them) can be read.")
    errs = []
    for host in ("www.reddit.com", "old.reddit.com"):
        try:
            req = urllib.request.Request(f"https://{host}{path}.json?limit=500&raw_json=1",
                                         headers={"User-Agent": REDDIT_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            errs.append(f"{host}: {e}")
    raise RuntimeError("; ".join(errs))


def reddit_to_doc(data):
    post = data[0]["data"]["children"][0]["data"]
    lines, count = [], [0]

    def walk(children, depth):
        for c in children:
            if c.get("kind") != "t1":
                continue
            d = c["data"]
            body = (d.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]"):
                count[0] += 1
                pad = "  " * min(depth, 6)
                lines.append(f"{pad}[{d.get('score', 0)}] u/{d.get('author', '?')}: " + body.replace("\n", "\n" + pad + "  "))
            rep = d.get("replies")
            if isinstance(rep, dict):
                walk(rep.get("data", {}).get("children", []), depth + 1)

    if len(data) > 1:
        walk(data[1]["data"]["children"], 0)
    sub = post.get("subreddit", "")
    date = datetime.fromtimestamp(post.get("created_utc", 0)).strftime("%Y-%m-%d") if post.get("created_utc") else ""
    head = [f"r/{sub} | u/{post.get('author', '?')} | score {post.get('score', 0)} | {date}", post.get("title", ""), ""]
    if post.get("selftext"):
        head += [post["selftext"].strip(), ""]
    if not post.get("is_self") and post.get("url_overridden_by_dest"):
        head += ["Link: " + post["url_overridden_by_dest"], ""]
    text = "\n".join(head + [f"--- Comments ({count[0]}) ---"] + lines)
    return {"title": post.get("title", "")[:100], "text": text, "sub": sub, "author": post.get("author", ""),
            "date": date, "id": post.get("id", ""), "is_video": bool(post.get("is_video"))}


def process_reddit(item, url, workdir):
    """Reads the post text and comments. (A video in the post is not downloaded; the thread text is what matters here.)"""
    update(item, step="reading the Reddit thread")
    try:
        d = reddit_to_doc(reddit_json(url))
    except Exception as e:
        # Reddit often blocks scripts. Fall back to the plain page on old.reddit.com.
        try:
            path = reddit_path(url)
            data, ctype, _ = fetch_url(f"https://old.reddit.com{path}/?limit=500", 8 * 1024 * 1024)
            h = html_to_doc(data, ctype)
            if len(h["text"]) < 200:
                raise RuntimeError("empty")
            pid = path.split("/comments/")[1].split("/")[0]
            date = datetime.now().strftime("%Y-%m-%d")
            finish_doc(item, url, {"platform": "Reddit", "folder": f"{date}_reddit_{pid}", "uploader": h["author"] or "Reddit",
                                   "title": h["title"] or "Reddit thread", "text": h["text"], "date": date, "pages": 0,
                                   "thumb": None, "pdf": None, "note": "Read from the plain page (comments may be less tidy)."})
            return True
        except Exception:
            raise RuntimeError(
                f"Reddit would not let the box read this thread ({str(e)[:150]}). Workaround: open the thread in your browser, "
                "print it to PDF (Cmd or Ctrl+P, Save as PDF), put the PDF in the import folder and use Import with "
                "'Include PDF documents'.")
    sid = safe(d["id"] or reddit_path(url).split("/comments/")[1].split("/")[0], 20)
    finish_doc(item, url, {"platform": "Reddit", "folder": f"{d['date'] or datetime.now().strftime('%Y-%m-%d')}_{safe(d['sub'], 24)}_{sid}",
                           "uploader": f"u/{d['author']} in r/{d['sub']}", "title": d["title"], "text": d["text"],
                           "date": d["date"] or datetime.now().strftime("%Y-%m-%d"), "pages": 0, "thumb": None, "pdf": None,
                           "note": "Reddit thread: post and comments (score in brackets)."})
    return True


# ------------------------------------------------- import local files (v2.1)


def resolve_import_dir(text):
    p = Path(text or DEFAULT_IMPORT).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"{p} is not a folder on this box")
    if p != IMPORT_ROOT and IMPORT_ROOT not in p.parents:
        raise ValueError(f"Only folders inside {IMPORT_ROOT} can be imported")
    if p == DATA or DATA in p.parents:
        raise ValueError("That is the library itself")
    return p


def local_folder_name(f):
    import hashlib

    st = f.stat()
    h = hashlib.sha1(f"{f}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:8]
    return f"{datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d')}_{safe(f.stem, 28)}_{h}", h


def scan_files(folder, recursive, images, pdfs=False):
    base = resolve_import_dir(folder)
    exts = VIDEO_EXT | (IMAGE_EXT if images else set()) | (PDF_EXT if pdfs else set())
    out = []
    for f in (base.rglob("*") if recursive else base.iterdir()):
        try:
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            rel = f.relative_to(base).parts
            if any(part.startswith(".") for part in rel) or f.stat().st_size == 0:
                continue
            name, _ = local_folder_name(f)
            out.append({"path": str(f), "name": f.name, "size": f.stat().st_size,
                        "kind": "video" if f.suffix.lower() in VIDEO_EXT else "pdf" if f.suffix.lower() in PDF_EXT else "image",
                        "mtime": f.stat().st_mtime, "done": (LIB / name / "meta.json").exists()})
        except OSError:
            continue
        if len(out) >= 5000:
            break
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return base, out


def sidecar_info(f):
    """yt-dlp and similar tools leave NAME.info.json or NAME.description next to the video."""
    info = {}
    j = f.with_name(f.stem + ".info.json")
    d = f.with_name(f.stem + ".description")
    try:
        if j.exists():
            info = json.loads(j.read_text(encoding="utf-8"))
        elif d.exists():
            info = {"description": d.read_text(encoding="utf-8", errors="replace")}
    except Exception:
        info = {}
    return info


def process_local(item):
    f = Path(item["file"])
    if not f.is_file():
        raise RuntimeError("The file is gone or moved.")
    folder, h = local_folder_name(f)
    if (LIB / folder / "meta.json").exists():
        update(item, status="done", step="already in library", folder=folder)
        return
    side = sidecar_info(f)
    url = side.get("webpage_url") or str(f)
    st_date = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
    if f.suffix.lower() in IMAGE_EXT:
        finish_post(item, url, {"platform": "Local image", "id": h, "folder": folder, "uploader": safe(side.get("uploader", ""), 24) if side.get("uploader") else "",
                                "title": side.get("title") or f.stem, "text": side.get("description", ""),
                                "date": st_date, "image_files": [f]})
        return
    if free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"Low disk space ({free_gb():.1f} GB free, need {MIN_FREE_GB}).")
    workdir = WORK / item["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    if f.suffix.lower() in PDF_EXT:
        d = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        process_pdf(item, url, f, workdir, keep=bool(item.get("keep_video", False)), date=d, folder=folder)
        return
    ud = str(side.get("upload_date") or st_date.replace("-", ""))
    info = {"title": side.get("title") or f.stem, "uploader": side.get("uploader") or side.get("channel") or "",
            "description": side.get("description") or "", "duration": side.get("duration") or 0,
            "upload_date": ud, "extractor_key": "Local file"}
    process_video(item, url, info, folder, f, workdir, move=False)


NO_VIDEO = re.compile(r"no video|no media|there is no video|unsupported url|photo|image|carousel|no formats", re.I)


def process(item):
    url = item["url"]
    if item.get("file"):
        try:
            process_local(item)
        except Exception as e:
            log("FAILED", url, e)
            update(item, status="error", step="failed", error=str(e)[:500])
        finally:
            shutil.rmtree(WORK / item["id"], ignore_errors=True)
        return
    workdir = WORK / item["id"]
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    try:
        if is_reddit(url):
            try:
                process_reddit(item, url, workdir)
            except Exception as e:
                log("Reddit direct read failed, printing the page to PDF:", str(e)[:150])
                try:
                    process_rendered(item, url, workdir)
                except Exception as e2:
                    raise RuntimeError(f"{e} | Printing the page to PDF also failed: {e2}")
            return
        if is_pdf_url(url):
            process_web(item, url, workdir)
            return
        for attempt in (1, 2):
            if platform_of(url) == "Instagram" and ig_creds()[0]:
                update(item, step="signing in to Instagram" if attempt == 2 or not IG_SESSION.exists() else "downloading")
                ensure_instagram_session(url, force=(attempt == 2))
            try:
                update(item, step="downloading")
                try:
                    info, folder, video = get_video(url, workdir)
                except Exception as e:
                    if NO_VIDEO.search(str(e)):
                        process_post(item, url, workdir, str(e))
                        return
                    raise
                if video is None:
                    update(item, status="done", step="already in library", folder=folder)
                    return
                process_video(item, url, info, folder, video, workdir)
                return
            except Exception as e:
                if (attempt == 1 and platform_of(url) == "Instagram" and ig_creds()[0]
                        and re.search(r"login|log in|checkpoint|rate.?limit|redirect", str(e), re.I)):
                    log("Instagram login problem, signing in again and retrying once")
                    continue
                raise
    except Exception as e:
        m = scrub(e)
        log("FAILED", url, m)
        low = m.lower()
        hint = ""
        if platform_of(url) == "Instagram" and ("login" in low or "rate-limit" in low or "not available" in low or "empty media response" in low):
            hint = (" (Instagram needs a logged-in burner account. Run set-instagram.sh on the box, or it is rate limiting. "
                    "Wait a while and retry.)")
        update(item, status="error", step="failed", error=(m[:500] + hint))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def worker():
    while True:
        with lock:
            nxt = next((i for i in items if i["status"] == "queued"), None)
            if nxt:
                nxt["status"] = "running"
                save_state()
        if not nxt:
            wake.wait(timeout=5)
            wake.clear()
            continue
        log("processing", nxt["url"])
        process(nxt)
        if not nxt.get("file"):
            time.sleep(PAUSE_BETWEEN)


# ---------------------------------------------------- re-sorting (v2)

BYLABEL = {"ai": "sorted by the local AI", "keywords": "sorted by keywords", "you": "set by you", "claude": "sorted by Claude in chat"}
HAND = ("you", "claude")  # sorted by hand or by Claude in chat: the automatic re-sort leaves these alone
resort_state = {"running": False, "done": 0, "total": 0}


def save_meta_and_pages(outdir, meta):
    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    write_item_files(outdir, meta, read_plain(outdir))


def resort_all():
    with lock:
        if resort_state["running"]:
            return False
        resort_state.update(running=True, done=0, total=0)

    def job():
        try:
            metas = [m for m in all_metas() if m.get("projects_by") not in HAND or m.get("category_by") not in HAND]
            resort_state["total"] = len(metas)
            for m in metas:
                outdir = LIB / m["folder"]
                try:
                    projs, by, cat = classify(m.get("title", ""), m.get("caption", ""), read_plain(outdir), m.get("ocr", []))
                    if m.get("projects_by") not in HAND:
                        m["projects"], m["projects_by"] = projs, by
                    if m.get("category_by") not in HAND:
                        m["category"], m["category_by"] = cat, by
                    save_meta_and_pages(outdir, m)
                except Exception as e:
                    log("re-sort failed for", m.get("folder"), e)
                resort_state["done"] += 1
            rebuild_index()
        finally:
            resort_state["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return True


# --------------------------------------------------------------- web app


@app.before_request
def auth():
    if not PASSWORD:
        return
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            if hmac.compare_digest(pw, PASSWORD):
                return
        except Exception:
            pass
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="reel2txt"'})


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>reel2txt</title>%CSS%</head><body><main>
<h1>reel2txt</h1>
<p class="mut">Paste links (TikTok, Instagram, YouTube, X, web pages, PDF links...). Videos, text posts, image posts, articles and PDFs all work. The box reads them, files them in the library and says which of your projects they could help.</p>
<textarea id="urls" rows="5" placeholder="One or many links, any text around them is fine"></textarea>
<p><label><input type="checkbox" id="keep" checked> Keep the video file</label></p>
<p><button onclick="add()">Add to queue</button> <a href="/index.html">Full library</a> &middot; <a href="/projects.html">By project</a> &middot; <a href="/edit-projects">Edit projects</a> &middot; <a href="/import">Import files</a> &middot; <a href="/apply">Apply sorting</a> &middot; <a href="/index.txt">index.txt</a></p>
<p id="msg" class="mut"></p>
<h2>Queue</h2>
<table><tbody id="rows"></tbody></table>
<p class="mut" id="status"></p>
<script>
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function add(){
  fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:document.getElementById('urls').value,keep_video:document.getElementById('keep').checked})})
  .then(function(r){return r.json()}).then(function(j){
    document.getElementById('msg').textContent=j.added+' added'+(j.skipped?(', '+j.skipped+' already in the list'):'');
    document.getElementById('urls').value='';load()})}
function act(p){fetch(p,{method:'POST'}).then(load)}
function load(){
  fetch('/api/items').then(function(r){return r.json()}).then(function(j){
    var h='';j.items.slice().reverse().forEach(function(it){
      var right='';
      if(it.status==='done'&&it.folder){right='<a href="/library/'+encodeURIComponent(it.folder)+'/index.html">open</a>'}
      if(it.status==='error'){right='<button class="sec" onclick="act(\\'/api/retry/'+it.id+'\\')">Retry</button>'}
      if(it.status!=='running'){right+=' <button class="sec" onclick="act(\\'/api/remove/'+it.id+'\\')">x</button>'}
      h+='<tr><td><span class="tag">'+esc(it.status)+'</span></td><td>'+esc(it.url.slice(0,70))+
         '<div class="mut">'+esc(it.status==='error'?it.error:(it.summary||it.step))+'</div></td><td>'+right+'</td></tr>'});
    document.getElementById('rows').innerHTML=h||'<tr><td class="mut">Nothing yet</td></tr>';
    var s=j.status;document.getElementById('status').textContent='Free disk: '+s.free_gb+' GB | speech model: '+s.whisper+
      ' | login cookies: '+s.cookies+' | summaries: '+s.summaries+' | projects loaded: '+s.projects+' | Instagram: '+s.instagram})}
load();setInterval(load,3000);
</script></main></body></html>""".replace("%CSS%", CSS)


@app.get("/")
def home():
    return PAGE


@app.post("/api/add")
def api_add():
    data = request.get_json(force=True, silent=True) or {}
    urls = [u.rstrip(").,;\"'") for u in re.findall(r"https?://\S+", data.get("text", ""))]
    added = skipped = 0
    with lock:
        have = {i["url"] for i in items if i["status"] != "error"}
        for u in dict.fromkeys(urls):
            if u in have:
                skipped += 1
                continue
            items.append({"id": f"{int(time.time() * 1000)}{added}", "url": u, "status": "queued", "step": "waiting",
                          "keep_video": bool(data.get("keep_video", True)),
                          "added": datetime.now().isoformat(timespec="seconds")})
            added += 1
        save_state()
    wake.set()
    return jsonify(added=added, skipped=skipped)


@app.get("/api/items")
def api_items():
    with lock:
        out = list(items)
    return jsonify(items=out, status={
        "free_gb": round(free_gb(), 1),
        "whisper": f"{WHISPER_MODEL} ({_good_device or WHISPER_DEVICE})",
        "cookies": (COOKIES_BROWSER or ("file" if COOKIES_FILE else "off")),
        "summaries": OLLAMA_MODEL or "auto (smallest Ollama model)",
        "projects": len(load_projects()), "instagram": ig_status()})


@app.post("/api/retry/<iid>")
def api_retry(iid):
    with lock:
        for i in items:
            if i["id"] == iid and i["status"] == "error":
                i.update(status="queued", step="waiting", error="")
        save_state()
    wake.set()
    return jsonify(ok=True)


@app.post("/api/remove/<iid>")
def api_remove(iid):
    with lock:
        items[:] = [i for i in items if not (i["id"] == iid and i["status"] != "running")]
        save_state()
    return jsonify(ok=True)


@app.post("/api/projects/<folder>")
def api_set_projects(folder):
    if not re.fullmatch(r"[\w\-]+", folder) or not (LIB / folder / "meta.json").exists():
        abort(404)
    names = (request.get_json(force=True, silent=True) or {}).get("names", [])
    valid = {p["name"] for p in load_projects()}
    outdir = LIB / folder
    meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
    meta["projects"] = [{"name": n, "why": "set by you"} for n in dict.fromkeys(names) if n in valid][:2]
    meta["projects_by"] = "you"
    cat = match_name((request.get_json(force=True, silent=True) or {}).get("category"), load_categories())
    if cat:
        meta["category"], meta["category_by"] = cat, "you"
    _fs_cache["t"] = 0
    save_meta_and_pages(outdir, meta)
    rebuild_index()
    return jsonify(ok=True)


def _fmt_size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


@app.post("/api/scan")
def api_scan():
    d = request.get_json(force=True, silent=True) or {}
    try:
        base, files = scan_files(d.get("folder"), bool(d.get("recursive", True)), bool(d.get("images", False)), bool(d.get("pdfs", False)))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    new = [f for f in files if not f["done"]]
    return jsonify(folder=str(base), total=len(files), new=len(new), done=len(files) - len(new),
                   videos=sum(1 for f in new if f["kind"] == "video"), images=sum(1 for f in new if f["kind"] == "image"),
                   pdfs=sum(1 for f in new if f["kind"] == "pdf"),
                   size=_fmt_size(sum(f["size"] for f in new)),
                   sample=[{"name": f["name"], "size": _fmt_size(f["size"]), "kind": f["kind"], "done": f["done"]} for f in files[:60]])


@app.post("/api/import")
def api_import():
    d = request.get_json(force=True, silent=True) or {}
    try:
        _, files = scan_files(d.get("folder"), bool(d.get("recursive", True)), bool(d.get("images", False)), bool(d.get("pdfs", False)))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    added = 0
    with lock:
        queued = {i.get("file") for i in items if i.get("file") and i["status"] in ("queued", "running")}
        for f in files:
            if f["done"] or f["path"] in queued:
                continue
            items.append({"id": f"{int(time.time() * 1000)}L{added}", "url": "file://" + f["path"], "file": f["path"],
                          "status": "queued", "step": "waiting", "keep_video": bool(d.get("copy", False)),
                          "added": datetime.now().isoformat(timespec="seconds")})
            added += 1
        save_state()
    wake.set()
    return jsonify(added=added)


IMPORT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Import files</title>%CSS%</head><body><main>
<p><a href="/">&larr; Add links</a> &middot; <a href="/projects.html">By project</a></p>
<h1>Import files already on this box</h1>
<p class="mut">Pick a folder on the Linux box. The app first shows what it found. Nothing is queued until you press Import.
Your original files are only read, never changed or deleted.</p>
<p>Folder on the box (inside %ROOT%):</p>
<input type="search" id="f" value="%DEF%">
<p><label><input type="checkbox" id="rec" checked> Include sub-folders</label><br>
<label><input type="checkbox" id="img"> Include pictures (.jpg .png .webp), not only videos</label><br>
<label><input type="checkbox" id="pdf"> Include PDF documents</label><br>
<label><input type="checkbox" id="cp"> Copy the videos and PDFs into the library (uses disk space; off = just record them)</label></p>
<p><button onclick="scan()">Scan this folder</button></p>
<div id="out"></div>
<script>
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function body(){return JSON.stringify({folder:document.getElementById('f').value,recursive:document.getElementById('rec').checked,
  images:document.getElementById('img').checked,pdfs:document.getElementById('pdf').checked,copy:document.getElementById('cp').checked})}
function scan(){document.getElementById('out').innerHTML='<p class="mut">Looking...</p>';
fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:body()}).then(function(r){return r.json().then(function(j){return [r.ok,j]})}).then(function(a){
var j=a[1];if(!a[0]){document.getElementById('out').innerHTML='<p>'+esc(j.error)+'</p>';return}
var h='<h2>Found in '+esc(j.folder)+'</h2><p><b>'+j.new+' new</b> ('+j.videos+' videos, '+j.images+' pictures, '+j.pdfs+' PDFs, '+esc(j.size)+') and '+j.done+' already imported.</p>';
if(j.new){h+='<p><button onclick="imp()">Import the '+j.new+' new files</button> <span class="mut">Each takes about a minute on this box. They run one after another in the queue.</span></p>'}
h+='<table>';j.sample.forEach(function(x){h+='<tr><td>'+esc(x.name)+'</td><td class="mut">'+x.kind+' '+esc(x.size)+'</td><td>'+(x.done?'<span class="tag">already in library</span>':'')+'</td></tr>'});
h+='</table>'+(j.total>60?'<p class="mut">Showing the newest 60 of '+j.total+'.</p>':'');document.getElementById('out').innerHTML=h})}
function imp(){fetch('/api/import',{method:'POST',headers:{'Content-Type':'application/json'},body:body()}).then(function(r){return r.json()}).then(function(j){
document.getElementById('out').innerHTML='<p><b>'+j.added+' files added to the queue.</b> Watch them on the <a href="/">main page</a>.</p>'})}
</script></main></body></html>""".replace("%CSS%", CSS)


@app.get("/import")
def import_page():
    return IMPORT_PAGE.replace("%DEF%", html.escape(DEFAULT_IMPORT)).replace("%ROOT%", html.escape(str(IMPORT_ROOT)))


@app.post("/api/resort")
def api_resort():
    return jsonify(started=resort_all())


@app.get("/api/resort-status")
def api_resort_status():
    return jsonify(resort_state)


EDIT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Edit projects</title>%CSS%</head><body><main>
<p><a href="/">&larr; Add links</a> &middot; <a href="/projects.html">By project</a></p>
<h1>Edit projects</h1>
<p class="mut">This is the list the app uses to decide where each link could help. Each project starts with <code>## Name</code>, then
<code>What:</code>, <code>Needs:</code> and <code>Keywords:</code> lines. Keywords help when the AI is not available.</p>
<textarea id="t" rows="26" spellcheck="false">%TEXT%</textarea>
<p><label><input type="checkbox" id="rs" checked> Re-sort the whole library after saving (links you set by hand are kept)</label></p>
<p><button onclick="save()">Save</button> <span id="m" class="mut"></span></p>
<script>
function save(){fetch('/api/projects-file',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({text:document.getElementById('t').value,resort:document.getElementById('rs').checked})})
.then(function(r){return r.json()}).then(function(j){document.getElementById('m').textContent='Saved. '+j.projects+' projects.'+(j.resort?' Re-sorting in the background.':'');})}
</script></main></body></html>""".replace("%CSS%", CSS)


@app.get("/edit-projects")
def edit_projects():
    load_projects()
    text = PROJECTS_FILE.read_text(encoding="utf-8") if PROJECTS_FILE.exists() else ""
    return EDIT_PAGE.replace("%TEXT%", html.escape(text))


@app.post("/api/projects-file")
def api_projects_file():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    if len(text) > 100000:
        abort(400)
    if PROJECTS_FILE.exists():
        shutil.copy(PROJECTS_FILE, PROJECTS_FILE.with_suffix(".md.bak"))
    PROJECTS_FILE.write_text(text, encoding="utf-8")
    _proj_cache["mtime"] = None
    n = len(load_projects())
    started = resort_all() if data.get("resort") else False
    if not started:
        rebuild_index()
    return jsonify(projects=n, resort=started)


def apply_sorting(text, by="claude"):
    """Lines: folder | project 1; project 2 | category | why.  Returns a report."""
    projs, cats = load_projects(), load_categories()
    metas = {m["folder"]: m for m in all_metas()}
    rep = {"applied": 0, "problems": [], "unknown_folders": []}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        key = re.sub(r"^\s*\d+[.)]\s+", "", parts[0]).strip("` ")
        folder = key if key in metas else None
        if not folder:
            c = [f for f in metas if f.endswith(key) or key in f] if len(key) >= 8 else []
            folder = c[0] if len(c) == 1 else None
        if not folder:
            rep["unknown_folders"].append(key[:80])
            continue
        pnames, bad = [], []
        for x in (parts[1] if len(parts) > 1 else "").split(";"):
            x = x.strip()
            if x and x not in ("-", "none", "None"):
                n = match_name(x, projs)
                (pnames.append(n) if n else bad.append(x))
        cat = ""
        if len(parts) > 2 and parts[2] and parts[2] != "-":
            cat = match_name(parts[2], cats) or ""
            if not cat:
                bad.append("category: " + parts[2])
        why = parts[3] if len(parts) > 3 else ""
        if bad:
            rep["problems"].append(f"{folder[:50]}: not in your lists: {', '.join(bad)}")
        m = metas[folder]
        m["projects"] = [{"name": n, "why": why or "sorted by Claude"} for n in dict.fromkeys(pnames)][:2]
        m["projects_by"] = by
        if cat:
            m["category"], m["category_by"] = cat, by
        save_meta_and_pages(LIB / folder, m)
        rep["applied"] += 1
    _fs_cache["t"] = 0
    rebuild_index()
    return rep


@app.post("/api/apply-sort")
def api_apply_sort():
    d = request.get_json(force=True, silent=True) or {}
    text = d.get("text", "")
    if len(text) > 400000:
        abort(400)
    return jsonify(apply_sorting(text))


APPLY_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apply a sorting file</title>%CSS%</head><body><main>
<p><a href="/">&larr; Add links</a> &middot; <a href="/projects.html">By project</a> &middot; <a href="/for-claude.txt">for-claude.txt</a></p>
<h1>Apply a sorting file</h1>
<p class="mut">Paste Claude's sorting here. One item per line:<br><code>folder | project 1; project 2 | category | why</code><br>
Use <code>-</code> when no project fits. Names must match your project and category lists. Items set this way are kept as they are
(the automatic re-sort will not touch them). Anything that does not match is listed so you can fix it.</p>
<textarea id="t" rows="14" spellcheck="false" placeholder="paste here"></textarea>
<p><button onclick="go()">Apply</button> <button class="sec" onclick="rs()">Re-sort everything not set by hand</button> <span id="m" class="mut"></span></p>
<div id="o"></div>
<p class="mut">Want Claude to sort the library? Open <a href="/for-claude.txt">for-claude.txt</a>, copy all of it into a chat, and paste the answer back here.</p>
<script>
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function go(){document.getElementById('m').textContent='Working...';
fetch('/api/apply-sort',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('t').value})})
.then(function(r){return r.json()}).then(function(j){document.getElementById('m').textContent='Applied '+j.applied+' items.';
var h='';if(j.unknown_folders.length){h+='<h2>Folders not found ('+j.unknown_folders.length+')</h2><pre>'+esc(j.unknown_folders.join('\\n'))+'</pre>'}
if(j.problems.length){h+='<h2>Names to fix ('+j.problems.length+')</h2><pre>'+esc(j.problems.join('\\n'))+'</pre>'}
document.getElementById('o').innerHTML=h})}
function rs(){fetch('/api/resort',{method:'POST'}).then(function(r){return r.json()}).then(function(j){document.getElementById('m').textContent=j.started?'Re-sorting in the background.':'Already running.'})}
</script></main></body></html>""".replace("%CSS%", CSS)


@app.get("/apply")
def apply_page():
    return APPLY_PAGE


@app.get("/for-claude.txt")
def for_claude():
    if not (DATA / "for-claude.txt").exists():
        rebuild_index()
    return send_from_directory(DATA, "for-claude.txt", mimetype="text/plain; charset=utf-8")


CAT_PAGE = EDIT_PAGE.replace("Edit projects", "Edit categories").replace("/api/projects-file", "/api/categories-file") \
    .replace("%TEXT%", "%TEXT%")


@app.get("/edit-categories")
def edit_categories():
    load_categories()
    text = CATEGORIES_FILE.read_text(encoding="utf-8") if CATEGORIES_FILE.exists() else ""
    page = CAT_PAGE.replace("This is the list the app uses to decide where each link could help.",
                            "Categories are used to file every item that helps no project. Each item gets one.")
    return page.replace("%TEXT%", html.escape(text))


@app.post("/api/categories-file")
def api_categories_file():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    if len(text) > 50000:
        abort(400)
    if CATEGORIES_FILE.exists():
        shutil.copy(CATEGORIES_FILE, CATEGORIES_FILE.with_suffix(".md.bak"))
    CATEGORIES_FILE.write_text(text, encoding="utf-8")
    _cat_cache["mtime"] = None
    n = len(load_categories())
    started = resort_all() if data.get("resort") else False
    if not started:
        rebuild_index()
    return jsonify(projects=n, resort=started)


@app.get("/index.html")
def idx_html():
    if not (DATA / "index.html").exists():
        rebuild_index()
    return send_from_directory(DATA, "index.html")


@app.get("/projects.html")
def idx_projects():
    if not (DATA / "projects.html").exists():
        rebuild_index()
    return send_from_directory(DATA, "projects.html")


@app.get("/index.txt")
def idx_txt():
    if not (DATA / "index.txt").exists():
        rebuild_index()
    return send_from_directory(DATA, "index.txt", mimetype="text/plain; charset=utf-8")


@app.get("/library/<path:p>")
def lib(p):
    if p.endswith("/") or "." not in p.rsplit("/", 1)[-1]:
        p = p.rstrip("/") + "/index.html"
    if not (LIB / p).resolve().is_relative_to(LIB.resolve()):
        abort(404)
    ctype = "text/plain; charset=utf-8" if p.endswith((".md", ".txt")) else None
    return send_from_directory(LIB, p, mimetype=ctype, conditional=True)


if __name__ == "__main__":
    load_state()
    merge_shipped(PROJECTS_FILE, "projects.md")
    merge_shipped(CATEGORIES_FILE, "categories.md")
    load_projects()
    load_categories()
    rebuild_index()
    threading.Thread(target=worker, daemon=True).start()
    log(f"reel2txt v2.6 on http://{HOST}:{PORT}  data={DATA}")
    app.run(host=HOST, port=PORT, threaded=True)
