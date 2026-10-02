"""Fact ledger: every claim a deep-analysis video may state, with its sources, the exact supporting
sentence, and a verification status that CODE (not the AI) decides.

Status of a claim:
  confirmed  - stated by an official/primary source, OR by 2+ independent outlets
  reported   - one major outlet only: may be used if the video names that outlet
  quoted     - a verbatim statement by a named speaker (verified word-for-word in the source)
  alleged    - an accusation / prediction by a named speaker: must be attributed, never stated as fact
  disputed   - sources disagree: the video must say "reports differ"
  unverified - only weak sources or nothing to back it: never used
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

USABLE = {"confirmed", "reported", "quoted", "alleged", "disputed"}
NEEDS_ATTRIBUTION = {"reported", "quoted", "alleged", "disputed"}

DEFAULT_TIER1 = ["pib.gov.in", "pmindia.gov.in", "sansad.in", "eci.gov.in", "eci.nic.in", "rbi.org.in",
                 "indiabudget.gov.in", "sci.gov.in", "main.sci.gov.in", "egazette.gov.in", "mha.gov.in",
                 "finmin.nic.in", "mea.gov.in", "niti.gov.in", "loksabha.nic.in", "rajyasabha.nic.in",
                 "prsindia.org", "data.gov.in", "mospi.gov.in"]
DEFAULT_TIER2 = ["ptinews.com", "aninews.in", "reuters.com", "apnews.com", "thehindu.com", "indianexpress.com",
                 "hindustantimes.com", "ndtv.com", "khabar.ndtv.com", "bbc.com", "bbc.co.uk", "timesofindia.indiatimes.com",
                 "livemint.com", "business-standard.com", "economictimes.indiatimes.com", "theprint.in", "scroll.in",
                 "thewire.in", "deccanherald.com", "telegraphindia.com", "newindianexpress.com", "outlookindia.com",
                 "indiatoday.in", "aajtak.in", "jagran.com", "bhaskar.com", "amarujala.com", "livehindustan.com",
                 "jansatta.com", "dw.com", "aljazeera.com", "barandbench.com", "livelaw.in"]


FRIENDLY = {
    "pib.gov.in": "PIB", "pmindia.gov.in": "PMO India", "sansad.in": "Sansad", "eci.gov.in": "Election Commission",
    "eci.nic.in": "Election Commission", "rbi.org.in": "RBI", "indiabudget.gov.in": "Union Budget",
    "main.sci.gov.in": "Supreme Court", "sci.gov.in": "Supreme Court", "mha.gov.in": "Home Ministry",
    "mea.gov.in": "External Affairs Ministry", "prsindia.org": "PRS", "ptinews.com": "PTI", "aninews.in": "ANI",
    "reuters.com": "Reuters", "apnews.com": "AP", "thehindu.com": "The Hindu", "indianexpress.com": "The Indian Express",
    "hindustantimes.com": "Hindustan Times", "ndtv.com": "NDTV", "khabar.ndtv.com": "NDTV", "bbc.com": "BBC",
    "bbc.co.uk": "BBC", "timesofindia.indiatimes.com": "Times of India", "livemint.com": "Mint",
    "business-standard.com": "Business Standard", "economictimes.indiatimes.com": "Economic Times",
    "theprint.in": "ThePrint", "scroll.in": "Scroll", "thewire.in": "The Wire", "indiatoday.in": "India Today",
    "aajtak.in": "Aaj Tak", "jagran.com": "Dainik Jagran", "bhaskar.com": "Dainik Bhaskar", "livelaw.in": "LiveLaw",
    "barandbench.com": "Bar and Bench", "deccanherald.com": "Deccan Herald", "dw.com": "DW",
}


def friendly(outlet: str) -> str:
    if outlet in FRIENDLY:
        return FRIENDLY[outlet]
    for dom, name in FRIENDLY.items():
        if outlet.endswith("." + dom):
            return name
    return outlet.split(".")[0].replace("-", " ").title() if "." in outlet else outlet


def outlet_of(url: str) -> str:
    host = (urlparse(url).netloc or url).lower()
    return host[4:] if host.startswith("www.") else host


def classify_tier(url: str, tier1: list[str] | None = None, tier2: list[str] | None = None) -> int:
    host = outlet_of(url)
    if not host:
        return 3
    for d in tier1 or DEFAULT_TIER1:
        if host == d or host.endswith("." + d):
            return 1
    if host.endswith(".gov.in") or host.endswith(".nic.in"):
        return 1
    for d in tier2 or DEFAULT_TIER2:
        if host == d or host.endswith("." + d):
            return 2
    return 3


@dataclass
class Source:
    id: str
    url: str
    title: str
    outlet: str                # domain, or "user notes"
    tier: int                  # 1 official/primary, 2 major outlet/agency, 3 other
    published: str = ""
    text: str = ""


@dataclass
class Claim:
    id: str
    text: str                  # neutral one-sentence statement (English, internal)
    kind: str = "fact"         # fact | number | quote | allegation
    speaker: str = ""          # who said it (required for quote/allegation)
    quote: str = ""            # verbatim words (quote kind)
    numbers: list[str] = field(default_factory=list)
    date: str = ""
    evidence: str = ""         # exact sentence from the source
    source_ids: list[str] = field(default_factory=list)
    status: str = "unverified"
    note: str = ""


@dataclass
class Ledger:
    topic: str
    sources: list[Source] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    as_of: str = ""

    def source(self, sid: str) -> Source | None:
        return next((s for s in self.sources if s.id == sid), None)

    def claim(self, cid: str) -> Claim | None:
        return next((c for c in self.claims if c.id == cid), None)

    def usable(self) -> list[Claim]:
        return [c for c in self.claims if c.status in USABLE]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.claims:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def outlets_for(self, claim: Claim) -> list[str]:
        seen: list[str] = []
        for sid in claim.source_ids:
            s = self.source(sid)
            if s and s.outlet not in seen:
                seen.append(s.outlet)
        return seen

    def to_json(self, with_text: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not with_text:
            for s in d["sources"]:
                s["text"] = s["text"][:0]
        return d

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["topic"], [Source(**s) for s in d["sources"]], [Claim(**c) for c in d["claims"]],
                   d.get("created", 0.0), d.get("as_of", ""))


# ------------------------------------------------------------------ verification (code, not AI)
_WS = re.compile(r"\s+")
_DEV = str.maketrans("०१२३४५६७८९", "0123456789")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def norm_text(s: str) -> str:
    s = s.translate(_DEV).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return _WS.sub(" ", s).strip()


def contains(haystack: str, needle: str) -> bool:
    n = norm_text(needle).strip(" .\"'")
    return len(n) >= 12 and n in norm_text(haystack)


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text.translate(_DEV)):
        out.add(m.replace(",", "").rstrip("."))
    return out


def verify_claim(c: Claim, source_text: str) -> str | None:
    """Return None if the claim is supported by the source text, else the reason it is rejected."""
    if not c.evidence or not contains(source_text, c.evidence):
        return "evidence sentence not found in the source"
    if c.kind == "quote":
        if not c.quote or not contains(source_text, c.quote):
            return "quote is not word-for-word in the source"
        if not c.speaker:
            return "quote has no named speaker"
    if c.kind == "allegation" and not c.speaker:
        return "allegation has no named speaker"
    ev = numbers_in(c.evidence)
    bad = numbers_in(c.text) - ev
    if bad:
        return f"number(s) {sorted(bad)} are not in the evidence sentence"
    c.numbers = sorted(numbers_in(c.text) | numbers_in(c.evidence))
    return None


def assign_status(c: Claim, ledger: Ledger, disputed: bool = False) -> None:
    srcs = [s for sid in c.source_ids if (s := ledger.source(sid))]
    outlets = {s.outlet for s in srcs}
    has_t1 = any(s.tier == 1 for s in srcs)
    if disputed:
        c.status = "disputed" if any(s.tier <= 2 for s in srcs) else "unverified"
    elif c.kind == "allegation":
        c.status = "alleged" if c.speaker and any(s.tier <= 2 for s in srcs) else "unverified"
    elif c.kind == "quote":
        c.status = "quoted" if c.speaker and any(s.tier <= 2 for s in srcs) else "unverified"
    elif has_t1 or len({o for o in outlets if any(s.outlet == o and s.tier <= 2 for s in srcs)}) >= 2:
        c.status = "confirmed"
    elif any(s.tier == 2 for s in srcs):
        c.status = "reported"
    else:
        c.status = "unverified"


def attribution_names(c: Claim, ledger: Ledger) -> list[str]:
    """Names a script may use to attribute this claim (speaker and/or outlet names)."""
    names = []
    if c.speaker:
        names.append(c.speaker)
    for sid in c.source_ids:
        s = ledger.source(sid)
        if s:
            names += [friendly(s.outlet), s.outlet, s.outlet.split(".")[0]]
    return [n for n in names if n]
