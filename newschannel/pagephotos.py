"""Photos from a web page YOU choose (a news article, a PIB release...) - but only where the rights allow it.

Seeing a photo on a news site does not make it free to use: most are owned by the photographer or agency, and using
them risks copyright claims, lost earnings and strikes. So every photo found on a page gets a rights status:

  licensed - the page states a Creative Commons / public-domain licence that allows commercial reuse and cropping
  official - the page is an official Government of India site (gov.in / nic.in / PIB ...): their standard policy generally allows
             free reuse with source credit and no misleading context, except material marked as third-party. You must confirm
  unknown  - no licence stated: shown for reference only. It can NEVER be imported; you can ask the owner for permission
  blocked  - a licence is stated but does not allow this use (NonCommercial, NoDerivatives, ShareAlike when disabled)
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from . import scout as S
from .config import Config
from .ledger import classify_tier, friendly, outlet_of

SKIP_NAME = re.compile(r"(logo|icon|sprite|avatar|pixel|tracking|favicon|badge|button|placeholder|spacer|/ads?/|advert|banner|share|social)", re.I)
IMG_EXT = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)
CC_URL = re.compile(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/([\d.]+)", re.I)


def public_url(url: str, resolve: Callable = socket.getaddrinfo) -> bool:
    """Only fetch real public web addresses (never localhost or private networks)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = resolve(p.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def license_from_url(u: str) -> str:
    m = CC_URL.search(u or "")
    if not m:
        return ""
    kind, code, ver = m.group(1).lower(), m.group(2).lower(), m.group(3)
    if kind == "publicdomain":
        return "CC0 / Public domain"
    return f"CC {code.upper()} {ver}"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.imgs: list[dict[str, Any]] = []
        self.licenses: list[str] = []
        self.jsonld: list[str] = []
        self.title = ""
        self._in_title = False
        self._in_ld = False
        self._ld: list[str] = []
        self._fig: dict[str, Any] | None = None
        self._in_cap = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag in ("link", "a") and "license" in a.get("rel", "").lower().split() and a.get("href"):
            self.licenses.append(a["href"])
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._ld = True, []
        elif tag == "figure":
            self._fig = {"caption": "", "imgs": []}
        elif tag == "figcaption":
            self._in_cap = True
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or a.get("data-original") or ""
            srcset = a.get("srcset") or a.get("data-srcset") or ""
            best, bw = "", 0
            for part in srcset.split(","):
                bits = part.strip().split()
                if bits:
                    m = re.match(r"(\d+)w", bits[1]) if len(bits) > 1 else None
                    w = int(m.group(1)) if m else 0
                    if w >= bw:
                        best, bw = bits[0], w
            img = {"src": best or src, "alt": a.get("alt", ""), "width": _num(a.get("width")), "height": _num(a.get("height")),
                   "caption": "", "fig": self._fig is not None}
            if bw:
                img["width"] = max(img["width"], bw)
            self.imgs.append(img)
            if self._fig is not None:
                self._fig["imgs"].append(img)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            self.jsonld.append("".join(self._ld))
        elif tag == "figcaption":
            self._in_cap = False
        elif tag == "figure" and self._fig is not None:
            for im in self._fig["imgs"]:
                im["caption"] = S.clean(self._fig["caption"])
            self._fig = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._ld.append(data)
        if self._in_cap and self._fig is not None:
            self._fig["caption"] += data


def _num(v: Any) -> int:
    m = re.match(r"\d+", str(v or ""))
    return int(m.group(0)) if m else 0


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _name(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or "")
    if isinstance(v, list):
        return ", ".join(filter(None, (_name(x) for x in v)))
    return str(v or "")


def find_photos(html: str, page_url: str, allow_sa: bool = False, tier1: list[str] | None = None,
                tier2: list[str] | None = None) -> list[S.Candidate]:
    p = _Page()
    p.feed(html)
    host_tier = classify_tier(page_url, tier1, tier2)
    page_lic = next((license_from_url(u) for u in p.licenses if license_from_url(u)), "")
    ld_info: dict[str, dict[str, str]] = {}                       # image url -> license / credit from JSON-LD
    for raw in p.jsonld:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for node in _walk(data):
            lic = license_from_url(str(node.get("license", ""))) if node.get("license") else ""
            if lic and not page_lic and node.get("@type") in ("NewsArticle", "Article", "WebPage"):
                page_lic = lic
            img = node.get("image") if isinstance(node.get("image"), (str, dict, list)) else None
            urls = [x if isinstance(x, str) else x.get("url", "") for x in (img if isinstance(img, list) else [img]) if x]
            for u in urls + ([node["url"]] if node.get("@type") == "ImageObject" and node.get("url") else []):
                ld_info[urljoin(page_url, u)] = {
                    "license": lic or ld_info.get(urljoin(page_url, u), {}).get("license", ""),
                    "credit": _name(node.get("creditText") or node.get("creator") or node.get("author") or node.get("copyrightHolder"))}
    meta_credit = p.meta.get("author") or p.meta.get("dc.creator") or ""
    title = S.clean(p.meta.get("og:title") or p.title)
    raw: list[tuple[str, str, str]] = []
    for key in ("og:image", "twitter:image", "og:image:url"):
        if p.meta.get(key):
            raw.append((urljoin(page_url, p.meta[key]), title, ""))
    for im in p.imgs:
        src = urljoin(page_url, im["src"]) if im["src"] else ""
        if not src or src.startswith("data:") or not IMG_EXT.search(src) or SKIP_NAME.search(src):
            continue
        if (im["width"] and im["width"] < 600) or (im["height"] and im["height"] < 300):
            continue
        raw.append((src, im["caption"] or im["alt"], im["alt"]))
    out: list[S.Candidate] = []
    seen: set[str] = set()
    for src, cap, alt in raw:
        key = src.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        info = ld_info.get(src, {})
        lic_label = info.get("license") or page_lic
        ok, label = S.license_ok(lic_label, allow_sa) if lic_label else (False, "")
        creator = info.get("credit") or meta_credit or friendly(outlet_of(page_url))
        if lic_label and ok:
            rights, note = "licensed", f"Licence stated on the page: {label}. Credit the creator."
        elif lic_label:
            rights, note = "blocked", f"The page says {lic_label}, which does not allow this use (commercial, cropped/edited or share-alike)."
        elif host_tier == 1:
            rights, label = "official", "Official source - check terms"
            note = ("Government of India site: its standard policy generally allows free reuse with source credit and without a "
                    "misleading context, except material marked as third-party. Confirm on the site's Copyright Policy page.")
        else:
            rights, label = "unknown", "Rights not stated"
            note = ("No licence is stated. This photo is probably owned by the outlet or photographer, so using it risks a copyright "
                    "claim. Ask for permission, or use one of the free-licence photos instead.")
        out.append(S.Candidate(S.cid("page", src), "page", (cap or title or "Photo from page")[:140], S.clean(cap or alt)[:400],
                               creator, label or "", "", page_url, src, src, 0, 0, "", "", 0.0, 0.0, "", [], False, rights, note))
        if len(out) >= 12:
            break
    return out


def fetch_html(url: str, limit: int = 3_000_000) -> str:
    r = requests.get(url, headers=S.UA, timeout=25, stream=True)
    r.raise_for_status()
    raw = r.raw.read(limit, decode_content=True)
    return raw.decode(r.encoding or "utf-8", errors="replace")


def add_page(cfg: Config, client: Any, folder: Path, url: str, need_id: str | None = None,
             log: Callable[[str], None] = print, html_fetch: Callable[[str], str] | None = None,
             download: Callable[[str], bytes] | None = None, resolve: Callable = socket.getaddrinfo) -> dict[str, Any]:
    """Find the photos on `url`, label their rights, rate their relevance, and add them to the scout session."""
    if not public_url(url, resolve):
        raise ValueError("That is not a public web address.")
    session = S.load_session(folder)
    ic = cfg["images"].get("scout", {})
    cfgan = cfg.get("analysis", {}).get("sources", {})
    cands = find_photos((html_fetch or fetch_html)(url), url, ic.get("allow_cc_by_sa", False), cfgan.get("tier1"), cfgan.get("tier2"))
    if not cands:
        raise ValueError("No usable photos found on that page.")
    base = next((n for n in session["needs"] if n["id"] == need_id), None)
    need = base or {"id": f"x{len(session['needs']) + 1}", "label": "Photos from " + friendly(outlet_of(url)),
                    "kind": "event", "named_entity": "", "queries": [url], "avoid": ""}
    have = {c["id"] for c in session["candidates"]}
    cands = [c for c in cands if c.id not in have]
    for c in cands:
        c.need = need["id"]
        c.text_score = S.text_score({**need, "queries": [need["label"]]}, c)
    rated = S.finish_need(client, cfg.models()[1], need, cands, folder / "thumbs", download or S.fetch_bytes, log,
                          ic.get("recommend_from", 7), 12)
    if base is None:
        session["needs"].append(need)
    session["candidates"] += [S.asdict(c) for c in rated]
    S._write(folder, session)
    return session
