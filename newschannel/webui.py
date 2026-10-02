"""Local web UI: `python -m newschannel ui` -> http://127.0.0.1:8765

Runs only on your own computer (bound to 127.0.0.1). API keys are written to .env and are never
sent back to the browser."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import sources
from . import curate as C
from .config import ROOT, Config
from .llm import make_client
from .models import Topic
from .pipeline import manual_topic, produce
from .review import Store
from .tts import make_tts

KEY_NAMES = ["OPENAI_API_KEY", "OPENAI_VOICE", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY",
             "ELEVENLABS_VOICE_ID", "PEXELS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
STEPS = ["clips", "writing script", "synthesizing voice", "user images", "choosing and arranging",
         "rendering"]


class Job:
    def __init__(self, label: str):
        self.id = uuid.uuid4().hex[:10]
        self.label = label
        self.status = "queued"          # queued | running | done | error
        self.logs: list[str] = []
        self.error = ""
        self.result: dict[str, Any] | None = None
        self.started = time.time()

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))

    def progress(self) -> int:
        if self.status == "done":
            return 100
        hit = 0
        for i, key in enumerate(STEPS, 1):
            if any(key in line for line in self.logs):
                hit = i
        return int(100 * hit / (len(STEPS) + 1))

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "status": self.status, "logs": self.logs[-12:],
                "error": self.error, "progress": self.progress(), "result": self.result,
                "seconds": int(time.time() - self.started)}


def write_env(path: Path, updates: dict[str, str | None]) -> None:
    """Update/insert KEY=value lines in .env, keeping comments and other lines. None removes a key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in updates and "=" in line and not line.lstrip().startswith("#"):
            done.add(k)
            if updates[k] is not None:
                out.append(f"{k}={updates[k]}")
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in done and v is not None:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def create_app(config_path: str | None = None, env_path: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 ** 3
    env_file = env_path or ROOT / ".env"
    jobs: dict[str, Job] = {}
    topics: dict[str, Topic] = {}
    run_lock = threading.Lock()          # one render at a time: rendering uses all CPU cores

    def cfg() -> Config:
        return Config.load(config_path)

    def store() -> Store:
        c = cfg()
        return Store(c.path(c["youtube"]["output_dir"]))

    # ----- localhost-only protection: refuse requests that come from other sites
    @app.before_request
    def guard():
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin and origin.split("//")[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent / "web", "index.html")

    # ----- settings
    @app.get("/api/status")
    def status():
        import shutil as sh
        from PIL import features
        c = cfg()
        has = lambda k: bool(os.environ.get(k))  # noqa: E731
        return jsonify({
            "channel": c["channel"]["name"],
            "keys": {k: has(k) for k in KEY_NAMES},
            "llm_provider": c.llm_provider(), "tts_provider": c.tts_provider(),
            "checks": [
                {"name": "ffmpeg", "ok": bool(sh.which("ffmpeg")),
                 "help": "Terminal: brew install ffmpeg"},
                {"name": "Hindi text engine (raqm)", "ok": features.check("raqm"),
                 "help": "Terminal: brew install libraqm"},
                {"name": "AI key (OpenAI or Anthropic)", "ok": has("OPENAI_API_KEY") or has("ANTHROPIC_API_KEY"),
                 "help": "Add your OpenAI key in the Settings tab"},
                {"name": "Voice key (OpenAI or ElevenLabs)",
                 "ok": has("OPENAI_API_KEY") or (has("ELEVENLABS_API_KEY") and has("ELEVENLABS_VOICE_ID")),
                 "help": "An OpenAI key also covers the voice"},
            ]})

    @app.post("/api/keys")
    def save_keys():
        data = request.get_json(force=True) or {}
        updates = {k: (v.strip() if isinstance(v, str) else None) for k, v in data.items() if k in KEY_NAMES}
        updates = {k: v for k, v in updates.items() if v != ""}      # blank box = leave unchanged
        write_env(env_file, updates)
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return jsonify({"ok": True, "saved": sorted(updates)})

    # ----- news topics
    @app.post("/api/topics")
    def get_topics():
        c = cfg()
        try:
            stories = sources.fetch_stories(c["feeds"], c["curation"]["max_age_hours"])
            client = make_client(c, required=False)
            found = C.curate(client, c.models()[0], stories, c["curation"]["min_sources"],
                             [h["topic"] for h in store().history()], c["curation"]["candidates_for_llm"])
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        out = []
        for t in found:
            tid = uuid.uuid4().hex[:8]
            topics[tid] = t
            out.append({"id": tid, "title": t.title, "sources": t.sources, "why": t.why})
        return jsonify({"topics": out})

    # ----- create a video
    def run_job(job: Job, topic: Topic, fmt: str, img_dir: Path, clip_dir: Path | None, demo: bool) -> None:
        with run_lock:
            job.status = "running"
            try:
                c = cfg()
                if demo:
                    from . import demo as demo_mod
                    job.log("writing script (demo)")
                    video = demo_mod.run(c, fmt, store().root)
                    meta = next(m for m in store().runs() if m["video"] == video)
                else:
                    meta = produce(c, topic, fmt, make_client(c), make_tts(c), store(),
                                   [img_dir], log=job.log, clip_folders=[clip_dir] if clip_dir else None)
                job.result = {"id": meta["id"], "title": meta["title"], "issues": meta["issues"]}
                job.status = "done"
            except Exception as exc:
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(img_dir.parent, ignore_errors=True)

    @app.post("/api/create")
    def create():
        if run_lock.locked():
            return jsonify({"error": "A video is already being made. Please wait until it finishes."}), 409
        f = request.form
        fmt = f.get("format", "short")
        demo = f.get("demo") == "1"
        if fmt not in ("short", "long"):
            return jsonify({"error": "format must be short or long"}), 400
        if f.get("topic_id"):
            topic = topics.get(f["topic_id"])
            if topic is None:
                return jsonify({"error": "This topic list has expired. Click \"Get today's news\" again."}), 400
        else:
            if not demo and not f.get("headline", "").strip():
                return jsonify({"error": "A headline is required."}), 400
            topic = manual_topic(f.get("headline", "डेमो").strip(), f.get("text", "").strip())
        job = Job(topic.title)
        work = ROOT / "uploads" / job.id
        img_dir, clip_dir = work / "images", work / "clips"
        img_dir.mkdir(parents=True)
        for file in request.files.getlist("images"):
            ext = Path(file.filename or "").suffix.lower()
            if ext in IMG_EXT:
                file.save(img_dir / f"{uuid.uuid4().hex[:8]}{ext}")
        metas = json.loads(f.get("clip_meta", "[]"))
        spec = []
        for i, file in enumerate(request.files.getlist("clips")):
            ext = Path(file.filename or "").suffix.lower()
            if ext not in VID_EXT:
                continue
            clip_dir.mkdir(exist_ok=True)
            name = f"clip{i}{ext}"
            file.save(clip_dir / name)
            m = metas[i] if i < len(metas) else {}
            rng = f"{m['start']}-{m['end']}" if m.get("start") and m.get("end") else ""
            spec.append(f"{name}: {rng} | {m.get('credit', '')} | {m.get('note', '')}")
        if spec:
            (clip_dir / "clips.txt").write_text("\n".join(spec), encoding="utf-8")
        jobs[job.id] = job
        threading.Thread(target=run_job, args=(job, topic, fmt, img_dir, clip_dir if spec else None, demo),
                         daemon=True).start()
        return jsonify({"job": job.id})

    @app.get("/api/jobs/<jid>")
    def job_status(jid: str):
        j = jobs.get(jid)
        if not j:
            abort(404)
        return jsonify(j.public())

    # ----- my videos / review
    @app.get("/api/videos")
    def videos():
        out = []
        for m in reversed(store().runs()):
            out.append({k: m.get(k) for k in ("id", "title", "status", "format", "duration", "issues", "clips",
                                               "description", "video_id", "created", "title_options", "hook",
                                               "hook_options", "thumb_choice")}
                       | {"video_url": f"/files/{m['id']}/video.mp4", "thumb_url": f"/files/{m['id']}/thumbnail.jpg",
                          "thumb_variants": [{"url": f"/files/{m['id']}/{Path(t['file']).name}", "text": t["text"]}
                                             for t in m.get("thumbnails", [])]})
        return jsonify({"videos": out})

    @app.get("/files/<run_id>/<name>")
    def files(run_id: str, name: str):
        ok_names = {"video.mp4", "thumbnail.jpg", "thumbnail_1.jpg", "thumbnail_2.jpg", "thumbnail_3.jpg"}
        if name not in ok_names or "/" in run_id or ".." in run_id:
            abort(404)
        p = store().root / run_id / name
        if not p.exists():
            abort(404)
        return send_file(p, conditional=True)

    @app.post("/api/videos/<rid>/status")
    def set_status(rid: str):
        body = request.get_json(force=True)
        s = store()
        try:
            m = s.meta(rid)
        except OSError:
            abort(404)
        action = body.get("action")
        if action == "approve":
            blocks = [i for i in m.get("issues", []) if i["level"] == "block"]
            if blocks and not body.get("force"):
                return jsonify({"error": "blocked", "issues": blocks}), 409
            s.set_status(rid, "approved")
        elif action == "reject":
            s.set_status(rid, "rejected")
        else:
            abort(400)
        return jsonify({"ok": True})

    @app.post("/api/videos/<rid>/package")
    def package(rid: str):
        """Pick the title and/or thumbnail variant used when this video is uploaded."""
        body = request.get_json(force=True) or {}
        s = store()
        try:
            m = s.meta(rid)
        except OSError:
            abort(404)
        if m["status"] == "published":
            return jsonify({"error": "Already published."}), 400
        title = (body.get("title") or "").strip()
        if title:
            if len(title) > 100:
                return jsonify({"error": "YouTube titles can be at most 100 characters."}), 400
            m["title"] = title
        if body.get("thumbnail") is not None:
            i = int(body["thumbnail"])
            variants = m.get("thumbnails", [])
            if not 0 <= i < len(variants):
                return jsonify({"error": "No such thumbnail."}), 400
            shutil.copyfile(variants[i]["file"], m["thumbnail"])
            m["thumb_choice"] = i
        s.save_meta(rid, m)
        return jsonify({"ok": True, "title": m["title"]})

    @app.get("/api/insights")
    def get_insights():
        from . import learn
        ins = learn.insights(store())
        ins["advice"] = learn.prompt_hint(store())
        return jsonify(ins)

    @app.post("/api/insights/sync")
    def sync_insights():
        from . import learn
        c = cfg()
        secret = Config.env("YOUTUBE_CLIENT_SECRETS")
        if not secret or not c.path(secret).exists():
            return jsonify({"error": "Connect YouTube first (see the README) to download your stats."}), 400
        try:
            n = learn.sync(store())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "videos": n})

    @app.post("/api/videos/<rid>/publish")
    def publish(rid: str):
        c, s = cfg(), store()
        m = s.meta(rid)
        if m["status"] != "approved":
            return jsonify({"error": "Approve the video first."}), 400
        secret = Config.env("YOUTUBE_CLIENT_SECRETS")
        if not secret or not c.path(secret).exists():
            return jsonify({"error": "YouTube is not connected yet. For now, download the video and upload it in YouTube Studio."}), 400
        try:
            from .daily import publish_one
            vid = publish_one(c, s, m, (request.get_json(silent=True) or {}).get("publish_at"))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "url": f"https://youtu.be/{vid}"})

    return app


def serve(port: int = 8765, open_browser: bool = True, config_path: str | None = None) -> None:
    app = create_app(config_path)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  News Channel Studio is running: {url}\n  (press Ctrl+C to stop)\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
