"""Photo scout: find RELEVANT, copyright-safe photos for a story and show them to you before the video is made.

1. The AI works out what the story actually needs to show (specific places, institutions, named officials, events),
   not generic stock words.
2. Those subjects are searched on copyright-safe sources: Wikimedia Commons and Openverse (no key needed), plus
   Pexels and Pixabay when you add their free keys.
3. Only licenses that allow commercial reuse are kept (public domain / CC0, CC BY with credit, the Pexels and
   Pixabay licenses; CC BY-SA only if you enable it). "NonCommercial" and "NoDerivatives" are always rejected.
4. Every candidate gets a relevance score from the AI looking at the picture AND its caption. People are never
   identified from faces: a photo of a named person is accepted only if its caption or file name says who it is.
5. You approve the photos you want; approved photos go into your library with their credit and license."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image

from .config import Config
from .llm import call_tool

UA = {"User-Agent": "NewsChannelBot/0.1 (photo scouting; contact: channel owner)"}
TAG = re.compile(r"<[^>]+>")
GENERIC = {"pexels", "pixabay"}        # generic stock: not photos of the actual event
_STOP = {"the", "and", "for", "with", "from", "india", "indian", "photo", "image", "picture", "news", "new"}


@dataclass
class Candidate:
    id: str
    provider: str
    title: str
    description: str
    creator: str
    license: str
    license_url: str
    page_url: str
    thumb_url: str
    full_url: str
    width: int = 0
    height: int = 0
    categories: str = ""
    need: str = ""
    text_score: float = 0.0
    score: float = 0.0
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    recommended: bool = False

    @property
    def generic(self) -> bool:
        return self.provider in GENERIC


def cid(provider: str, url: str) -> str:
    return hashlib.sha1(f"{provider}|{url}".encode()).hexdigest()[:10]


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", TAG.sub(" ", s or "")).strip()


# ----------------------------------------------------------------------------- licenses
def license_ok(short: str, allow_sa: bool = False) -> tuple[bool, str]:
    """(allowed?, tidy label). Never allows NonCommercial / NoDerivatives (we monetize and we crop/animate)."""
    s = (short or "").strip()
    low = s.lower()
    if not low:
        return False, ""
    if re.search(r"(^|[\s\-_])(nc|nd)([\s\-_\d]|$)|non-?commercial|no-?deriv", low):
        return False, s
    if low.startswith("cc0") or low.startswith("public domain") or low in ("pd", "pdm") or low.startswith("pd-") or low.startswith("pdm"):
        return True, "CC0 / Public domain"
    if re.match(r"^cc[ -]by[ -]sa", low):
        return allow_sa, s
    if re.match(r"^cc[ -]by([ -]\d|$)", low) or low.startswith("attribution") or low.startswith("godl"):
        return True, s
    if "pexels license" in low or "pixabay" in low:
        return True, s
    return False, s


# ----------------------------------------------------------------------------- providers
def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 25) -> Any:
    r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def search_wikimedia(query: str, n: int = 6, allow_sa: bool = False, min_width: int = 1280) -> list[Candidate]:
    data = _get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrsearch": f"{query} filetype:bitmap",
        "gsrlimit": 25, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920})
    out: list[Candidate] = []
    for page in (data.get("query", {}).get("pages", {})).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        ok, label = license_ok(meta.get("LicenseShortName", {}).get("value", ""), allow_sa)
        if not ok or info.get("width", 0) < min_width:
            continue
        full = info.get("thumburl") or info.get("url")
        if not full:
            continue
        thumb = full.replace("/1920px-", "/320px-") if "/1920px-" in full else full
        out.append(Candidate(
            cid("wikimedia", full), "wikimedia", clean(page.get("title", "")).replace("File:", ""),
            clean(meta.get("ImageDescription", {}).get("value", ""))[:400], clean(meta.get("Artist", {}).get("value", "")) or "Unknown",
            label, meta.get("LicenseUrl", {}).get("value", ""), info.get("descriptionurl", ""), thumb, full,
            info.get("thumbwidth") or info.get("width", 0), info.get("thumbheight") or info.get("height", 0),
            clean(meta.get("Categories", {}).get("value", "")).replace("|", ", ")[:300]))
        if len(out) >= n:
            break
    return out


def search_openverse(query: str, n: int = 6, allow_sa: bool = False, min_width: int = 1280) -> list[Candidate]:
    lic = "cc0,pdm,by" + (",by-sa" if allow_sa else "")
    data = _get("https://api.openverse.org/v1/images/", {"q": query, "license": lic, "page_size": 20, "mature": "false",
                                                           "extension": "jpg,jpeg,png"})
    out: list[Candidate] = []
    for r in data.get("results", []):
        w = r.get("width") or 0
        if w and w < min_width:
            continue
        lic_code = (r.get("license") or "").lower()
        label = {"cc0": "CC0", "pdm": "Public domain"}.get(lic_code) or f"CC {lic_code.upper()} {r.get('license_version', '')}".strip()
        ok, label = license_ok(label, allow_sa)
        if not ok or not r.get("url"):
            continue
        out.append(Candidate(
            cid("openverse", r["url"]), "openverse", clean(r.get("title", "")), clean(" ".join(
                t.get("name", "") for t in (r.get("tags") or []))), r.get("creator") or "Unknown", label,
            r.get("license_url", ""), r.get("foreign_landing_url", ""), r.get("thumbnail") or r["url"], r["url"], w,
            r.get("height") or 0, r.get("source") or r.get("provider") or ""))
        if len(out) >= n:
            break
    return out


def search_pexels(query: str, n: int = 6, allow_sa: bool = False, min_width: int = 1280) -> list[Candidate]:
    key = Config.env("PEXELS_API_KEY")
    if not key:
        raise KeyError("no PEXELS_API_KEY")
    data = _get("https://api.pexels.com/v1/search", {"query": query, "per_page": 15, "orientation": "landscape"},
                {"Authorization": key})
    return [Candidate(cid("pexels", p["src"]["large2x"]), "pexels", clean(p.get("alt") or query), clean(p.get("alt", "")),
                      p.get("photographer", "Unknown"), "Pexels License", "https://www.pexels.com/license/", p.get("url", ""),
                      p["src"].get("medium") or p["src"]["large2x"], p["src"]["large2x"], p.get("width", 0), p.get("height", 0))
            for p in data.get("photos", []) if p.get("width", 0) >= min_width][:n]


def search_pixabay(query: str, n: int = 6, allow_sa: bool = False, min_width: int = 1280) -> list[Candidate]:
    key = Config.env("PIXABAY_API_KEY")
    if not key:
        raise KeyError("no PIXABAY_API_KEY")
    data = _get("https://pixabay.com/api/", {"key": key, "q": query, "image_type": "photo", "orientation": "horizontal",
                                              "safesearch": "true", "per_page": 15})
    return [Candidate(cid("pixabay", h["largeImageURL"]), "pixabay", clean(h.get("tags", query)), clean(h.get("tags", "")),
                      h.get("user", "Unknown"), "Pixabay Content License", "https://pixabay.com/service/license-summary/",
                      h.get("pageURL", ""), h.get("webformatURL") or h["largeImageURL"], h["largeImageURL"],
                      h.get("imageWidth", 0), h.get("imageHeight", 0))
            for h in data.get("hits", []) if h.get("imageWidth", 0) >= min_width][:n]


PROVIDERS: dict[str, Callable[..., list[Candidate]]] = {
    "wikimedia": search_wikimedia, "openverse": search_openverse, "pexels": search_pexels, "pixabay": search_pixabay}
PROVIDER_LABEL = {"wikimedia": "Wikimedia Commons", "openverse": "Openverse", "pexels": "Pexels", "pixabay": "Pixabay"}


def credit_for(c: Candidate) -> str:
    if c.provider == "pexels":
        return f"Photo by {c.creator} on Pexels (Pexels License)"
    if c.provider == "pixabay":
        return f"Photo by {c.creator} on Pixabay (Pixabay Content License)"
    src = PROVIDER_LABEL.get(c.provider, c.provider)
    link = f" - {c.page_url}" if c.page_url else ""
    return f"{c.creator} / {src} ({c.license}){link}"


# ----------------------------------------------------------------------------- planning what to look for
PLAN_SYSTEM = """You are the photo editor of a news channel. From the story, decide which real-world visuals the video should show and how to find
them in free photo archives (Wikimedia Commons, Openverse, stock sites).
Rules:
- 4-8 DISTINCT visuals. Prefer SPECIFIC real subjects named in the story: the institution or building (e.g. "Parliament House New Delhi",
  "Supreme Court of India building"), the place, the event, a document or symbol, a crowd or setting that matches what happened.
- People: include a person ONLY if the story names them and they are central; put their full name in named_entity. Never guess who someone is.
- Each visual gets 1-3 short ENGLISH search queries. Archive search is keyword-based: use the proper name or a plain description,
  not poetic phrases. Avoid generic stock cliches (handshakes, light bulbs) unless the story is about that.
- Describe in `label` what the picture should show, and in `avoid` what would make it misleading or wrong."""
PLAN_SCHEMA = {"type": "object", "properties": {"needs": {"type": "array", "items": {
    "type": "object", "properties": {
        "label": {"type": "string"}, "kind": {"type": "string", "enum": ["person", "place", "institution", "event", "document", "crowd", "concept"]},
        "named_entity": {"type": "string"}, "queries": {"type": "array", "items": {"type": "string"}}, "avoid": {"type": "string"}},
    "required": ["label", "kind", "queries"]}}}, "required": ["needs"]}


def plan_needs(client: Any, model: str, story: str, hints: list[str] | None = None) -> list[dict[str, Any]]:
    needs: list[dict[str, Any]] = []
    if client is not None:
        try:
            res = call_tool(client, model, PLAN_SYSTEM, f"STORY:\n{story[:6000]}\n\nHints from the script: {', '.join(hints or [])[:400]}",
                            "plan_photos", PLAN_SCHEMA, 2000)
            needs = res.get("needs", [])[:8]
        except Exception as exc:
            print(f"[scout] could not plan photo needs: {exc}")
    if not needs:                                    # offline fallback: search the story's opening words
        head = re.sub(r"\s+", " ", story).strip()[:80]
        needs = [{"label": head, "kind": "concept", "queries": [head] + list(hints or [])[:2], "named_entity": "", "avoid": ""}]
    out = []
    for i, n in enumerate(needs, 1):
        qs = [q.strip() for q in n.get("queries", []) if q.strip()][:3]
        if qs:
            out.append({"id": f"n{i}", "label": n.get("label", qs[0]), "kind": n.get("kind", "concept"),
                        "named_entity": (n.get("named_entity") or "").strip(), "queries": qs, "avoid": n.get("avoid", "")})
    return out


# ----------------------------------------------------------------------------- relevance
def name_in(text: str, name: str) -> bool:
    toks = [t for t in re.findall(r"\w+", name.lower()) if len(t) > 1]
    low = text.lower()
    return bool(toks) and all(t in low for t in toks) if len(toks) >= 2 else bool(toks and toks[0] in low)


def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 2 and w not in _STOP}


def text_score(need: dict[str, Any], c: Candidate) -> float:
    want = tokens(need["label"] + " " + " ".join(need["queries"]))
    have = tokens(f"{c.title} {c.description} {c.categories}")
    if not want:
        return 0.0
    return round(10 * min(1.0, len(want & have) / max(1, min(len(want), 5))), 1)


RATE_SYSTEM = """You rate candidate photos for ONE visual need of a news video. For each photo give a score 0-10:
10 = clearly shows exactly the needed subject, documentary quality; 7 = good fit; 4 = loosely related; 0 = wrong.
Judge using BOTH the picture and its caption/title. Rules:
- NEVER identify a real person from their face. If the need is a named person, a photo is a good fit only if its caption/title
  says it is that person; otherwise score it 3 or less.
- Penalise (and flag) watermarks, logos or text banners of other outlets (flag: watermark), heavy text overlays (text_overlay),
  blurry or tiny subjects (low_quality), photos that could mislead about the story (misleading), graphic or sexual content (nsfw).
- Generic stock photos (pexels/pixabay) are never photos of the real event: score them at most 6 unless the need is a generic concept.
Give a short reason (max 14 words)."""
RATE_SCHEMA = {"type": "object", "properties": {"ratings": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "score": {"type": "number"}, "reason": {"type": "string"},
                                     "flags": {"type": "array", "items": {"type": "string"}}}, "required": ["id", "score"]}}},
    "required": ["ratings"]}
DROP_FLAGS = {"watermark", "nsfw"}


def _thumb_bytes(raw: bytes, side: int = 360) -> bytes | None:
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def fetch_bytes(url: str, limit: int = 30_000_000) -> bytes:
    r = requests.get(url, headers=UA, timeout=40, stream=True)
    r.raise_for_status()
    data = r.raw.read(limit + 1, decode_content=True)
    if len(data) > limit:
        raise ValueError("image too large")
    return data


def rate(client: Any, model: str, need: dict[str, Any], cands: list[Candidate], thumbs: dict[str, bytes]) -> None:
    import base64
    content: list[dict] = [{"type": "text", "text": f"NEED: {need['label']} (kind: {need['kind']}"
                                                    + (f", named person: {need['named_entity']}" if need.get("named_entity") else "")
                                                    + f")\nAvoid: {need.get('avoid') or '-'}\n\nCANDIDATES:"}]
    for c in cands:
        content.append({"type": "text", "text": f"\nid={c.id} provider={c.provider} title={c.title!r} caption={c.description[:200]!r}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(thumbs[c.id]).decode()}})
    res = call_tool(client, model, RATE_SYSTEM, content, "rate_photos", RATE_SCHEMA, 2500)
    by = {c.id: c for c in cands}
    for r in res.get("ratings", []):
        c = by.get(r.get("id", ""))
        if c:
            c.score = max(0.0, min(10.0, float(r.get("score", 0))))
            c.reason = (r.get("reason") or "")[:140]
            c.flags = [f for f in r.get("flags", []) if f]


# ----------------------------------------------------------------------------- the scout
def collect(cfg: Config, client: Any, needs: list[dict[str, Any]], thumbs_dir: Path, log: Callable[[str], None] = print,
            providers: dict[str, Callable[..., list[Candidate]]] | None = None,
            download: Callable[[str], bytes] | None = None, seen: set[str] | None = None) -> tuple[list[Candidate], dict[str, str]]:
    """Search, filter, thumbnail and rate candidates for the given needs."""
    ic = cfg["images"].get("scout", {})
    search = providers or PROVIDERS
    names = [p for p in ic.get("providers", list(PROVIDERS)) if p in search]
    allow_sa, min_w, per_need = ic.get("allow_cc_by_sa", False), ic.get("min_width", 1280), ic.get("per_need", 6)
    min_rec = ic.get("recommend_from", 7)
    download = download or fetch_bytes
    model = cfg.models()[1]
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    seen = set(seen or ())

    log(f"searching {', '.join(PROVIDER_LABEL.get(n, n) for n in names)}…")
    tasks = [(need, q, p) for need in needs for q in need["queries"] for p in names]

    def run(task):
        need, q, p = task
        try:
            return need, p, search[p](q, per_need, allow_sa, min_w), None
        except KeyError:
            return need, p, [], "skipped: add a free API key in Settings"
        except Exception as exc:
            return need, p, [], f"failed: {type(exc).__name__}"

    found: dict[str, dict[str, Candidate]] = {n["id"]: {} for n in needs}
    counts: dict[str, int] = {p: 0 for p in names}
    with ThreadPoolExecutor(6) as ex:
        for need, p, cands, err in ex.map(run, tasks):
            if err:
                status[p] = err
            for c in cands:
                c.need = need["id"]
                if need["kind"] == "person" and need.get("named_entity") and not name_in(
                        f"{c.title} {c.description} {c.categories}", need["named_entity"]):
                    continue                           # a photo of a named person must say it is that person
                if c.id in seen:
                    continue
                seen.add(c.id)
                c.text_score = text_score(need, c)
                found[need["id"]][c.id] = c
                counts[p] += 1
    for p in names:
        status.setdefault(p, f"ok ({counts[p]} found)")

    log("rating photos…")
    all_c: list[Candidate] = []
    for need in needs:
        pool = sorted(found[need["id"]].values(), key=lambda c: -c.text_score)[:per_need * 2]
        thumbs: dict[str, bytes] = {}
        for c in pool:
            try:
                t = _thumb_bytes(download(c.thumb_url))
            except Exception:
                t = None
            if t:
                thumbs[c.id] = t
                (thumbs_dir / f"{c.id}.jpg").write_bytes(t)
        pool = [c for c in pool if c.id in thumbs]
        if client is not None and pool:
            try:
                rate(client, model, need, pool, thumbs)
            except Exception as exc:
                log(f"[scout] AI rating failed for '{need['label']}': {exc}")
                for c in pool:
                    c.score, c.reason = min(c.text_score, 5.0), "AI rating unavailable; keyword match only"
        else:
            for c in pool:
                c.score, c.reason = min(c.text_score, 6.0), "keyword match only (no AI rating)"
        pool = [c for c in pool if not (set(c.flags) & DROP_FLAGS)]
        for c in pool:
            if c.generic and need["kind"] != "concept":
                c.score = min(c.score, 6.0)
            if set(c.flags) & {"text_overlay", "low_quality", "misleading"}:
                c.score = min(c.score, 4.0)
            c.recommended = c.score >= min_rec
        all_c += sorted(pool, key=lambda c: -c.score)[:per_need]
    return all_c, status


def _write(folder: Path, result: dict[str, Any]) -> None:
    import json
    (folder / "candidates.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def scout(cfg: Config, client: Any, story: str, hints: list[str] | None = None, extra_queries: list[str] | None = None,
          folder: Path | None = None, log: Callable[[str], None] = print,
          providers: dict[str, Callable[..., list[Candidate]]] | None = None,
          download: Callable[[str], bytes] | None = None, needs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if folder is None:
        folder = Path(cfg.root) / "output" / "_scout" / uuid.uuid4().hex[:10]
    sid = folder.name
    folder.mkdir(parents=True, exist_ok=True)
    if needs is None:
        log("planning photo needs…")
        needs = plan_needs(client, cfg.models()[0], story, hints)
    for q in extra_queries or []:
        needs.append({"id": f"x{len(needs) + 1}", "label": q, "kind": "concept", "named_entity": "", "queries": [q], "avoid": ""})
    cands, status = collect(cfg, client, needs, folder / "thumbs", log, providers, download)
    result = {"sid": sid, "needs": needs, "candidates": [asdict(c) for c in cands], "providers": status,
              "recommend_from": cfg["images"].get("scout", {}).get("recommend_from", 7)}
    _write(folder, result)
    return result


def search_more(cfg: Config, client: Any, folder: Path, query: str, need_id: str | None = None,
                log: Callable[[str], None] = print, providers: dict[str, Callable[..., list[Candidate]]] | None = None,
                download: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """Add candidates for your own search words (optionally for an existing need). Returns the updated session."""
    session = load_session(folder)
    base = next((n for n in session["needs"] if n["id"] == need_id), None)
    need = {"id": need_id or f"x{len(session['needs']) + 1}", "label": base["label"] if base else query,
            "kind": base["kind"] if base else "concept", "named_entity": base.get("named_entity", "") if base else "",
            "queries": [query], "avoid": base.get("avoid", "") if base else ""}
    cands, status = collect(cfg, client, [need], folder / "thumbs", log, providers, download,
                            seen={c["id"] for c in session["candidates"]})
    if base is None:
        session["needs"].append(need)
    session["candidates"] += [asdict(c) for c in cands]
    session["providers"].update(status)
    _write(folder, session)
    return session


def load_session(folder: Path) -> dict[str, Any]:
    import json
    return json.loads((folder / "candidates.json").read_text(encoding="utf-8"))


def approve(folder: Path, ids: list[str], library: Any, min_side: int = 700,
            download: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print) -> list[Any]:
    """Download the full-size photos you approved into your library (with credit + license). Returns library entries."""
    download = download or fetch_bytes
    session = load_session(folder)
    by = {c["id"]: Candidate(**c) for c in session["candidates"]}
    needs = {n["id"]: n for n in session["needs"]}
    out = []
    for i in ids:
        c = by.get(i)
        if not c:
            continue
        tmp = folder / f"_full_{c.id}.jpg"
        try:
            tmp.write_bytes(download(c.full_url))
            tags = sorted(tokens(needs.get(c.need, {}).get("label", "")))[:6]
            e, _ = library.add_file(tmp, caption=(c.title if not c.description else f"{c.title}. {c.description}")[:300],
                                    credit=credit_for(c), source="stock", tags=tags, min_side=min_side,
                                    license=c.license, page_url=c.page_url)
            if e:
                out.append(e)
        except Exception as exc:
            log(f"[scout] could not import {c.title[:40]}: {exc}")
        finally:
            tmp.unlink(missing_ok=True)
    return out
