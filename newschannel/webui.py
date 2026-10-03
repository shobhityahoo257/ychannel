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
from .library import Library
from . import analysis as analysis_mod
from . import data as data_mod
from . import deep, explainer
from . import pagephotos
from . import workflow as wfm
from . import scout as scout_mod
from .models import Topic
from .pipeline import manual_topic, produce
from .review import Store
from .tts import make_tts

KEY_NAMES = ["OPENAI_API_KEY", "OPENAI_VOICE", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY",
             "ELEVENLABS_VOICE_ID", "PEXELS_API_KEY", "PIXABAY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
STEPS = ["clips", "writing script", "synthesizing voice", "user images", "choosing and arranging",
         "rendering"]
RESEARCH_STEPS = ["looking for official", "fetching sources", "extracting claims", "cross-checking", "deciding"]
WF_STEPS = ["writing", "polishing", "choosing and arranging", "synthesizing voice", "rendering"]
SCOUT_STEPS = ["planning photo needs", "searching", "rating photos"]
ANALYSIS_STEPS = ["writing the", "polishing", "synthesizing voice", "choosing and arranging", "rendering"]


def clean_data_specs(raw: Any) -> list[dict[str, Any]]:
    """Official-data requests from the browser: a few countries, each with known indicators only."""
    out: list[dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else [])[:4]:
        if not isinstance(item, dict):
            continue
        country = str(item.get("country") or "").strip()[:60]
        inds = [i for i in (item.get("indicators") or []) if i in data_mod.INDICATORS][:6]
        if country and inds:
            out.append({"country": country, "indicators": inds})
    return out


class Job:
    def __init__(self, label: str, steps: list[str] | None = None):
        self.steps = steps or STEPS
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
        for i, key in enumerate(self.steps, 1):
            if any(key in line for line in self.logs):
                hit = i
        return int(100 * hit / (len(self.steps) + 1))

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
            "workflow_default": c.get("workflow", {}).get("default", {"script": "manual", "photos": "manual"}),
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

    # ----- breaking-news watcher (background thread, started/stopped from the UI)
    watch = {"thread": None, "stop": threading.Event(), "watcher": None, "error": "", "checks": 0}

    def make_breaking(topic: Topic) -> dict[str, Any]:
        from .daily import notify
        with run_lock:                                   # never render two videos at once
            c = cfg()
            meta = produce(c, topic, "short", make_client(c), make_tts(c), store(), breaking=True)
        notify(c, meta)
        return meta

    def watch_loop() -> None:
        from .breaking import BreakingWatcher
        c = cfg()
        w = BreakingWatcher(c, make_client(c), store(), make=make_breaking, log=lambda m: None)
        watch["watcher"] = w
        every = c.get("breaking", {}).get("poll_minutes", 5) * 60
        while not watch["stop"].is_set():
            try:
                w.run_once()
                watch["error"] = ""
            except Exception as exc:
                watch["error"] = f"{type(exc).__name__}: {exc}"
            watch["checks"] += 1
            watch["stop"].wait(every)

    @app.get("/api/breaking")
    def breaking_status():
        w = watch["watcher"]
        t = watch["thread"]
        b = cfg().get("breaking", {})
        return jsonify({
            "running": bool(t and t.is_alive()), "checks": watch["checks"], "error": watch["error"],
            "last_check": w.state.last_check if w else 0,
            "alerts": [{"title": a["title"], "ts": a["ts"], "run_id": a["run_id"]}
                       for a in (w.state.alerted[-5:][::-1] if w else [])],
            "settings": {"poll_minutes": b.get("poll_minutes", 5), "min_sources": b.get("min_sources", 3),
                         "min_importance": b.get("min_importance", 7),
                         "max_alerts_per_day": b.get("max_alerts_per_day", 3)}})

    @app.post("/api/breaking")
    def breaking_control():
        action = (request.get_json(force=True) or {}).get("action")
        t = watch["thread"]
        if action == "start":
            if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
                return jsonify({"error": "Add an AI key in Settings first."}), 400
            if not (t and t.is_alive()):
                watch["stop"] = threading.Event()
                watch["thread"] = threading.Thread(target=watch_loop, daemon=True)
                watch["thread"].start()
        elif action == "stop":
            watch["stop"].set()
        else:
            abort(400)
        return jsonify({"ok": True})

    # ----- photo library (inventory)
    def library() -> Library:
        c = cfg()
        return Library(c.path(c["images"].get("library_dir", "library")))

    def entry_json(e) -> dict[str, Any]:
        return {"id": e.id, "caption": e.caption, "tags": e.tags, "credit": e.credit, "source": e.source,
                "used": e.used, "added": e.added, "w": e.width, "h": e.height, "ai_tagged": e.ai_tagged,
                "license": e.license, "page_url": e.page_url,
                "thumb": f"/library/thumbs/{e.id}.jpg", "full": f"/library/images/{e.id}.jpg"}

    def ai_available() -> bool:
        return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))

    def autotag_async() -> None:
        def work():
            try:
                c = cfg()
                library().autotag(make_client(c), c.models()[1], limit=30)
            except Exception:
                pass
        if ai_available():
            threading.Thread(target=work, daemon=True).start()

    @app.get("/api/library")
    def library_list():
        items = library().search(request.args.get("q", ""), request.args.get("source", ""))
        return jsonify({"photos": [entry_json(e) for e in items], "total": len(library().all()),
                        "untagged": sum(1 for e in library().all() if not e.caption)})

    @app.post("/api/library")
    def library_add():
        lib, c = library(), cfg()
        added = dupes = 0
        for file in request.files.getlist("files"):
            ext = Path(file.filename or "").suffix.lower()
            if ext not in IMG_EXT:
                continue
            tmp = ROOT / "uploads" / f"lib_{uuid.uuid4().hex[:8]}{ext}"
            tmp.parent.mkdir(exist_ok=True)
            file.save(tmp)
            try:
                e, new = lib.add_file(tmp, request.form.get("caption", ""), request.form.get("credit", ""),
                                      min_side=c["images"]["min_side_px"])
            finally:
                tmp.unlink(missing_ok=True)
            added += bool(e and new)
            dupes += bool(e and not new)
        if added and c["images"].get("auto_tag", True):
            autotag_async()
        return jsonify({"added": added, "duplicates": dupes})

    @app.post("/api/library/autotag")
    def library_autotag():
        if not ai_available():
            return jsonify({"error": "Add an AI key in Settings first."}), 400
        c = cfg()
        try:
            n = library().autotag(make_client(c), c.models()[1], limit=30)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"described": n})

    @app.post("/api/library/<eid>")
    def library_edit(eid: str):
        body = request.get_json(force=True) or {}
        tags = body.get("tags")
        if isinstance(tags, str):
            tags = [t for t in tags.split(",")]
        e = library().update(eid, body.get("caption"), tags, body.get("credit"))
        if not e:
            abort(404)
        return jsonify(entry_json(e))

    @app.delete("/api/library/<eid>")
    def library_delete(eid: str):
        if not library().delete(eid):
            abort(404)
        return jsonify({"ok": True})

    @app.get("/library/<kind>/<name>")
    def library_file(kind: str, name: str):
        eid = Path(name).stem
        e = library().get(eid)
        if kind not in ("thumbs", "images") or not e:
            abort(404)
        return send_file(library().thumb_path(e) if kind == "thumbs" else library().path(e), conditional=True,
                         max_age=3600)

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
    def run_job(job: Job, topic: Topic, fmt: str, img_dir: Path, clip_dir: Path | None, demo: bool,
                library_ids: list[str] | None = None, language: str | None = None) -> None:
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
                                   [img_dir], log=job.log, clip_folders=[clip_dir] if clip_dir else None,
                                   library_ids=library_ids, language=language, approved_only=True)
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
        lib_ids = [i for i in json.loads(f.get("library_ids", "[]")) if isinstance(i, str)]
        language = f.get("language") if f.get("language") in ("hinglish", "hindi", "english") else None
        mode = parse_mode(f)
        if mode and not demo:                     # step-by-step: stop for approval after the steps marked "manual"
            return start_workflow("news", topic.title, topic.slug, fmt,
                                  {"topic": wfm.topic_to_dict(topic), "format": fmt, "language": language,
                                   "library_ids": lib_ids}, mode, img_dir, clip_dir if spec else None)
        jobs[job.id] = job
        threading.Thread(target=run_job, args=(job, topic, fmt, img_dir, clip_dir if spec else None, demo, lib_ids, language),
                         daemon=True).start()
        return jsonify({"job": job.id})

    # ----- photo scout: find relevant copyright-safe photos and let the user approve them BEFORE the video is made
    def scout_dir(sid: str) -> Path:
        if not sid.isalnum():
            abort(404)
        d = store().root / "_scout" / sid
        if not (d / "candidates.json").exists():
            abort(404)
        return d

    @app.post("/api/scout")
    def scout_start():
        body = request.get_json(force=True) or {}
        story, hints, style = "", [], ""
        try:
            if body.get("research_id"):
                rid = str(body["research_id"])
                if not rid.isalnum():
                    abort(404)
                led, rec = analysis_mod.load_research(store(), rid)
                style = rec.get("style", "")
                from . import deep as deep_mod
                story = f"{led.topic}\n" + deep_mod.ledger_brief(led)
            elif body.get("workflow_id"):
                wid = str(body["workflow_id"])
                if not wid.replace("-", "").isalnum() or not wfm.Workflow.exists(store(), wid):
                    abort(404)
                w = wfm.Workflow(store(), wid)
                style = w.params().get("style", "")
                sc = w.script()
                story = sc.title + "\n" + "\n".join(f"{x.headline}. {x.narration[:220]}" for x in sc.scenes)
                if w.state["kind"] == "analysis":
                    from . import deep as deep_mod
                    story += "\n" + deep_mod.ledger_brief(w.ledger(), 40)
            elif body.get("topic_id") and topics.get(body["topic_id"]):
                t = topics[body["topic_id"]]
                story = t.title + "\n" + "\n".join(f"{x.title}. {x.summary}" for x in t.stories[:5])
            else:
                story = ((body.get("headline") or "").strip() + "\n" + (body.get("text") or "").strip()).strip()
        except OSError:
            return jsonify({"error": "Research not found. Run the research step again."}), 400
        if not story:
            return jsonify({"error": "Write the headline (or pick a story) first so the app knows what to look for."}), 400
        extra = [q.strip() for q in (body.get("queries") or []) if isinstance(q, str) and q.strip()][:5]
        sid = uuid.uuid4().hex[:10]
        job = Job(story.splitlines()[0][:80], SCOUT_STEPS)
        jobs[job.id] = job

        def work():
            job.status = "running"
            try:
                c = cfg()
                job.result = scout_mod.scout(c, make_client(c, required=False), story, hints, extra,
                                             folder=store().root / "_scout" / sid, log=job.log, style=style)
                job.status = "done"
            except Exception as exc:
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job.id, "sid": sid})

    @app.get("/api/scout/<sid>")
    def scout_get(sid: str):
        return jsonify(scout_mod.load_session(scout_dir(sid)))

    @app.post("/api/scout/<sid>/search")
    def scout_search(sid: str):
        body = request.get_json(force=True) or {}
        q = (body.get("query") or "").strip()
        if not q:
            return jsonify({"error": "Type what to search for."}), 400
        c = cfg()
        return jsonify(scout_mod.search_more(c, make_client(c, required=False), scout_dir(sid), q, body.get("need_id"),
                                             log=lambda m: None))

    @app.post("/api/scout/<sid>/page")
    def scout_page(sid: str):
        """Look for photos on a web page you give. Rights are labelled; only licensed / official ones can be used."""
        body = request.get_json(force=True) or {}
        url = (body.get("url") or "").strip()
        c = cfg()
        try:
            return jsonify(pagephotos.add_page(c, make_client(c, required=False), scout_dir(sid), url, body.get("need_id"),
                                               log=lambda m: None))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Could not read that page ({type(exc).__name__})."}), 502

    @app.post("/api/scout/<sid>/approve")
    def scout_approve(sid: str):
        body = request.get_json(force=True) or {}
        ids = [i for i in (body.get("ids") or []) if isinstance(i, str) and i.isalnum()]
        if not ids:
            return jsonify({"error": "Select at least one photo."}), 400
        c = cfg()
        lib = library()
        skipped: list[dict[str, str]] = []
        entries = scout_mod.approve(scout_dir(sid), ids, lib, c["images"]["min_side_px"], log=lambda m: None,
                                    confirm_official=bool(body.get("confirm_official")), skipped=skipped)
        return jsonify({"photos": [entry_json(e) for e in entries], "requested": len(ids), "skipped": skipped})

    @app.get("/api/scout/<sid>/thumb/<name>")
    def scout_thumb(sid: str, name: str):
        p = scout_dir(sid) / "thumbs" / name
        if not name.endswith(".jpg") or not name[:-4].isalnum() or not p.exists():
            abort(404)
        return send_file(p, conditional=True, max_age=3600)

    # ----- step-by-step approval workflow (script -> photos & media -> video)
    def get_wf(rid: str) -> "wfm.Workflow":
        if not rid.replace("-", "").isalnum() or not wfm.Workflow.exists(store(), rid):
            abort(404)
        return wfm.Workflow(store(), rid)

    def parse_mode(f: Any) -> dict[str, str] | None:
        raw = f.get("mode")
        if not raw:
            return None
        try:
            m = json.loads(raw)
        except ValueError:
            return None
        m = {"script": m.get("script", "auto"), "photos": m.get("photos", "auto")}
        return m if "manual" in m.values() and all(v in wfm.MODES for v in m.values()) else None

    def wf_job(wf: "wfm.Workflow", fn, will_render: bool) -> Any:
        """Run a stage in the background; render stages take the one-render-at-a-time lock."""
        import contextlib
        job = Job(wf.state["title"], WF_STEPS)
        jobs[job.id] = job

        def work():
            with (run_lock if will_render else contextlib.nullcontext()):
                job.status = "running"
                try:
                    c = cfg()
                    fn(c, make_client(c), make_tts(c), job.log)
                    job.result = {"workflow": wf.id, "status": wf.state["status"]}
                    job.status = "done"
                except Exception as exc:
                    job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job.id, "workflow": wf.id})

    def start_workflow(kind: str, title: str, slug: str, fmt: str, params: dict, mode: dict, img_dir: Path,
                       clip_dir: Path | None):
        wf = wfm.Workflow.create(store(), kind, title, slug, fmt, params, mode)
        inputs = wf.dir / "inputs"
        if img_dir.exists() and any(img_dir.iterdir()):
            shutil.copytree(img_dir, inputs / "images")
        if clip_dir and clip_dir.exists():
            shutil.copytree(clip_dir, inputs / "clips")
        shutil.rmtree(img_dir.parent, ignore_errors=True)
        will_render = mode["script"] == "auto" and mode["photos"] == "auto"
        return wf_job(wf, lambda c, cl, tts, log: wfm.advance(c, cl, tts, wf, log), will_render)

    def wf_view(wf: "wfm.Workflow") -> dict[str, Any]:
        st = wf.state
        out = {k: st.get(k) for k in ("id", "kind", "status", "title", "format", "mode", "error", "created")}
        out["params"] = {k: v for k, v in st["params"].items() if k in ("language", "stance", "minutes", "music", "breaking", "research_id", "style")}
        if (wf.dir / "script.json").exists() and st["status"] != "queued":
            out["script"] = wfm.describe_script(wf)
            out["check"] = wf.check() if st["kind"] == "analysis" else None
        out["media"] = wfm.describe_media(wf)
        if st["status"] == "done":
            out["video_id"] = st.get("video_id")
        return out

    @app.get("/api/workflows")
    def wf_list():
        return jsonify({"drafts": wfm.list_drafts(store())})

    @app.get("/api/workflows/<rid>")
    def wf_get(rid: str):
        return jsonify(wf_view(get_wf(rid)))

    @app.delete("/api/workflows/<rid>")
    def wf_delete(rid: str):
        wfm.discard(get_wf(rid))
        return jsonify({"ok": True})

    def need_status(wf: "wfm.Workflow", *allowed: str):
        if wf.state["status"] not in allowed:
            return jsonify({"error": f"This step is not available right now (status: {wf.state['status']})."}), 409
        return None

    @app.post("/api/workflows/<rid>/script")
    def wf_script_edit(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "script_review")):
            return err
        res = wfm.save_script_edits(cfg(), wf, request.get_json(force=True) or {})
        return jsonify({**wf_view(wf), "result": res})

    @app.post("/api/workflows/<rid>/revise")
    def wf_revise(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "script_review")):
            return err
        instruction = ((request.get_json(force=True) or {}).get("instruction") or "").strip()
        if not instruction:
            return jsonify({"error": "Tell the AI what to change."}), 400
        return wf_job(wf, lambda c, cl, tts, log: wfm.stage_script(c, cl, wf, log, instruction), False)

    @app.post("/api/workflows/<rid>/approve_script")
    def wf_approve_script(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "script_review")):
            return err
        body = request.get_json(silent=True) or {}
        try:
            wfm.approve_script_check(wf, bool(body.get("force")))
        except wfm.Blocked as exc:
            return jsonify({"error": str(exc), "violations": wf.check()["violations"]}), 409
        auto_rest = bool(body.get("auto_rest"))
        will_render = auto_rest or wf.state["mode"]["photos"] == "auto"
        return wf_job(wf, lambda c, cl, tts, log: wfm.approve_script(c, cl, tts, wf, log, auto_rest, bool(body.get("force"))),
                      will_render)

    @app.post("/api/workflows/<rid>/media")
    def wf_media_edit(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "media_review")):
            return err
        body = request.get_json(force=True) or {}
        wfm.save_plan(wf, body.get("plan") or [], body.get("music"))
        return jsonify(wf_view(wf))

    @app.post("/api/workflows/<rid>/assets")
    def wf_assets(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "media_review")):
            return err
        files = []
        for file in request.files.getlist("files"):
            ext = Path(file.filename or "").suffix.lower()
            if ext in IMG_EXT:
                tmp = ROOT / "uploads" / f"wf_{uuid.uuid4().hex[:8]}{ext}"
                tmp.parent.mkdir(exist_ok=True)
                file.save(tmp)
                files.append(tmp)
        ids = request.form.get("library_ids") or (request.get_json(silent=True) or {}).get("library_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        try:
            n = wfm.add_assets(cfg(), wf, [i for i in ids if isinstance(i, str)], files)
        finally:
            for f in files:
                f.unlink(missing_ok=True)
        return jsonify({**wf_view(wf), "added": n})

    @app.post("/api/workflows/<rid>/replan")
    def wf_replan(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "media_review")):
            return err
        return wf_job(wf, lambda c, cl, tts, log: wfm.replan(c, cl, wf, log), False)

    @app.post("/api/workflows/<rid>/reopen_script")
    def wf_reopen(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "media_review")):
            return err
        wfm.reopen_script(wf)
        return jsonify(wf_view(wf))

    @app.post("/api/workflows/<rid>/approve_media")
    def wf_approve_media(rid: str):
        wf = get_wf(rid)
        if (err := need_status(wf, "media_review")):
            return err
        if run_lock.locked():
            return jsonify({"error": "A video is already being rendered. Please wait until it finishes."}), 409
        return wf_job(wf, lambda c, cl, tts, log: wfm.approve_media(c, cl, tts, wf, log), True)

    @app.get("/api/workflows/<rid>/asset/<name>")
    def wf_asset(rid: str, name: str):
        wf = get_wf(rid)
        p = wfm.thumb_path(wf, name[:-4]) if name.endswith(".jpg") and name[:-4].isalnum() else None
        if not p:
            abort(404)
        return send_file(p, conditional=True, max_age=600)

    # ----- deep analysis: step 1 research + fact-check, step 2 create
    @app.post("/api/analysis/research")
    def analysis_research():
        body = request.get_json(force=True) or {}
        headline = (body.get("headline") or "").strip()
        urls = body.get("urls") or []
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.splitlines() if u.strip()]
        topic = topics.get(body.get("topic_id", "")) if body.get("topic_id") else None
        if topic is not None:
            headline = headline or topic.title
        if not headline:
            return jsonify({"error": "A headline / topic is required."}), 400
        style = body.get("style") or "analysis"
        if style not in deep.STYLES:
            return jsonify({"error": "Unknown video style."}), 400
        data_specs = clean_data_specs(body.get("data")) if style == "explainer" else []
        discover = bool(body.get("discover")) and style == "explainer"
        if not (urls or topic or (body.get("notes") or "").strip() or data_specs or discover):
            return jsonify({"error": "Add at least one source link, official data, or paste notes."}), 400
        job = Job(headline, RESEARCH_STEPS)
        jobs[job.id] = job

        def work():
            job.status = "running"
            try:
                c = cfg()
                res = analysis_mod.run_research(c, make_client(c), store(), headline, urls, body.get("notes") or "",
                                                topic, job.log, style=style, data=data_specs, discover=discover)
                job.result = res
                job.status = "done"
            except Exception as exc:
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job.id})

    @app.get("/api/analysis/research/<rid>")
    def analysis_research_get(rid: str):
        if not rid.isalnum():
            abort(404)
        try:
            led, rec = analysis_mod.load_research(store(), rid)
        except OSError:
            abort(404)
        return jsonify({"research_id": rid, "headline": led.topic, "recommend": rec, "counts": led.counts(),
                        "usable": len(led.usable()), "enough": len(led.usable()) >= analysis_mod.MIN_USABLE_CLAIMS,
                        "ledger": led.to_json()})

    @app.post("/api/analysis/create")
    def analysis_create():
        if run_lock.locked():
            return jsonify({"error": "A video is already being made. Please wait until it finishes."}), 409
        f = request.form
        rid = f.get("research_id", "")
        if not rid.isalnum():
            return jsonify({"error": "Run the research step first."}), 400
        try:
            led, _ = analysis_mod.load_research(store(), rid)
        except OSError:
            return jsonify({"error": "Research not found. Run the research step again."}), 400
        style = f.get("style") or "analysis"
        if style not in deep.STYLES:
            return jsonify({"error": "Unknown video style."}), 400
        stance, minutes = f.get("stance", "auto"), f.get("minutes", "auto")
        if stance not in ("auto", "neutral", "critical", "supportive"):
            return jsonify({"error": "Unknown angle."}), 400
        if minutes != "auto":
            try:
                float(minutes)
            except ValueError:
                return jsonify({"error": "Length must be a number of minutes or 'auto'."}), 400
        language = f.get("language") or None
        if language not in (None, "hinglish", "hindi", "english"):
            return jsonify({"error": "Language must be english, hinglish or hindi."}), 400
        job = Job(led.topic, ANALYSIS_STEPS)
        work_dir = ROOT / "uploads" / job.id
        img_dir = work_dir / "images"
        img_dir.mkdir(parents=True)
        for file in request.files.getlist("images"):
            ext = Path(file.filename or "").suffix.lower()
            if ext in IMG_EXT:
                file.save(img_dir / f"{uuid.uuid4().hex[:8]}{ext}")
        lib_ids = [i for i in json.loads(f.get("library_ids", "[]")) if isinstance(i, str)]
        mode = parse_mode(f)
        if style == "explainer":
            explainer.Ideas(store().root / "_ideas.json").mark_made(led.topic)
        if mode:
            return start_workflow("analysis", led.topic, "analysis", "analysis",
                                  {"research_id": rid, "stance": stance, "minutes": minutes, "language": language,
                                   "music": f.get("music") or None, "library_ids": lib_ids, "style": style}, mode, img_dir, None)
        jobs[job.id] = job

        def work():
            with run_lock:
                job.status = "running"
                try:
                    c = cfg()
                    meta = analysis_mod.make_video(c, make_client(c), make_tts(c), store(), rid, stance, minutes, language,
                                                   f.get("music") or None, [img_dir], lib_ids, log=job.log, approved_only=True,
                                                   style=style)
                    job.result = {"id": meta["id"], "title": meta["title"], "issues": meta["issues"]}
                    job.status = "done"
                except Exception as exc:
                    job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job": job.id})

    # ----- explainers: settings, topic ideas
    @app.get("/api/explainer/config")
    def explainer_config():
        c = cfg()
        ex = explainer.settings(c)
        return jsonify({"channel": ex["channel"], "language": ex["language"], "niche": ex["niche"], "discover": ex["discover"],
                        "series": explainer.SERIES, "indicators": data_mod.catalog(),
                        "countries": sorted({n.title() for n in data_mod.COUNTRIES if len(n) > 3})})

    @app.post("/api/explainer/ideas")
    def explainer_ideas():
        body = request.get_json(force=True) or {}
        try:
            n = max(3, min(12, int(body.get("n") or 8)))
        except (TypeError, ValueError):
            n = 8
        series = body.get("series") if body.get("series") in {s["id"] for s in explainer.SERIES} else ""
        c = cfg()
        try:
            out = explainer.suggest(c, make_client(c), explainer.Ideas(store().root / "_ideas.json"), n,
                                    str(body.get("focus") or "")[:300], series, store().history())
        except Exception as exc:
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"ideas": out})

    @app.get("/api/videos/<rid>/ledger")
    def video_ledger(rid: str):
        from .ledger import Ledger
        if not rid.replace("-", "").isalnum():
            abort(404)
        p = store().root / rid / "ledger.json"
        if not p.exists():
            return jsonify({"error": "This video has no fact ledger."}), 404
        m = store().meta(rid)
        return jsonify({"ledger": Ledger.load(p).to_json(), "analysis": m.get("analysis"), "issues": m.get("issues", [])})

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
                                               "hook_options", "thumb_choice", "category", "playlists",
                                               "playlist_error", "endscreen_done", "analysis", "language")}
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

    @app.get("/api/videos/<rid>/endscreen")
    def endscreen(rid: str):
        """Checklist for the one step YouTube only allows in Studio: end screens (plus a pinned comment draft)."""
        from . import playlists as P
        s = store()
        try:
            m = s.meta(rid)
        except OSError:
            abort(404)
        if not m.get("video_id"):
            return jsonify({"error": "Publish the video first."}), 400
        h = next((x for x in s.history() if x.get("video_id") == m["video_id"]), {"video_id": m["video_id"]})
        out = P.endscreen_helper(h, s.history(), cfg().get("playlists", {}), cfg().lang)
        out["done"] = bool(m.get("endscreen_done"))
        out["long"] = m.get("format") == "long"
        return jsonify(out)

    @app.post("/api/videos/<rid>/endscreen/done")
    def endscreen_done(rid: str):
        s = store()
        try:
            m = s.meta(rid)
        except OSError:
            abort(404)
        m["endscreen_done"] = bool((request.get_json(silent=True) or {}).get("done", True))
        s.save_meta(rid, m)
        return jsonify({"ok": True})

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
