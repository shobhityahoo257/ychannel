"""Deep-analysis videos: research -> fact ledger -> recommend angle/length -> write -> strict lint.

The AI proposes; code disposes. Every claim must quote its source sentence verbatim (checked in code), numbers
and quotes in the script must come from the ledger (checked in code), claims that are not independently
confirmed must be attributed by name (checked in code), and banned patterns (mind-reading, hearsay) are
rejected. A script that still fails after retries is flagged so you cannot approve it by accident."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .i18n import LANG_RULES, norm, t
from .ledger import (NEEDS_ATTRIBUTION, USABLE, Claim, Ledger, Source, assign_status, attribution_names, contains,
                     friendly, norm_text, numbers_in, verify_claim)
from .llm import call_tool
from .models import Scene, Script

WORDS_PER_SEC = 2.4
SECTIONS = ["hook", "recap", "context", "positions", "analysis", "counterpoint", "scenarios", "close"]
STANCES = ["neutral", "critical", "supportive"]

# ----------------------------------------------------------------------------- 1. claim extraction
EXTRACT_SYSTEM = """You extract verifiable claims from ONE source text (a news article or an official document).
Output only what the text EXPLICITLY states - never infer, never combine facts, never add background knowledge.
For each claim give:
- text: one neutral sentence, in English.
- kind: fact | number | quote | allegation.
  quote = a named person's words reported verbatim; allegation = an accusation, criticism or prediction by a named person/party.
- speaker: who said it (required for quote and allegation; otherwise empty).
- quote: for kind=quote only, the speaker's words COPIED EXACTLY (original language, character for character).
- evidence: ONE sentence or passage copied EXACTLY, character for character, from the text that supports the claim.
- date: the date it refers to, as written in the text, if any.
Every number in `text` must appear in `evidence`. Skip opinions of the author, adjectives and loaded language.
Prioritise what matters for political analysis: decisions, official actions, numbers, dates, statements, reactions, legal steps.
Return at most {n} claims."""

EXTRACT_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {
    "type": "object", "properties": {
        "text": {"type": "string"}, "kind": {"type": "string", "enum": ["fact", "number", "quote", "allegation"]},
        "speaker": {"type": "string"}, "quote": {"type": "string"}, "evidence": {"type": "string"},
        "date": {"type": "string"}}, "required": ["text", "kind", "evidence"]}}}, "required": ["claims"]}

MERGE_SYSTEM = """You compare claims extracted from different sources of the same story. Group claims that state the
SAME fact (even in different words/languages). Mark a group disputed=true if its members disagree on a material detail
(a number, date, who did what). Different facts must stay in different groups. Do not invent anything."""
MERGE_SCHEMA = {"type": "object", "properties": {"groups": {"type": "array", "items": {
    "type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}},
                                     "disputed": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["ids"]}}}, "required": ["groups"]}


def extract_claims(client: Any, model: str, source: Source, max_claims: int = 25) -> list[dict[str, Any]]:
    res = call_tool(client, model, EXTRACT_SYSTEM.format(n=max_claims),
                    f"Source: {source.title} ({source.outlet})\n\nTEXT:\n{source.text}", "submit_claims",
                    EXTRACT_SCHEMA, 6000)
    return res.get("claims", [])[:max_claims]


def merge_groups(client: Any, model: str, claims: list[Claim]) -> list[dict[str, Any]]:
    if client is None or len(claims) < 2:
        return [{"ids": [c.id]} for c in claims]
    listing = "\n".join(f"{c.id} [{','.join(c.source_ids)}] {c.text}" for c in claims)
    try:
        res = call_tool(client, model, MERGE_SYSTEM, listing, "group_claims", MERGE_SCHEMA, 6000)
    except Exception as exc:
        print(f"[deep] claim merging failed, keeping claims separate: {exc}")
        return [{"ids": [c.id]} for c in claims]
    seen: set[str] = set()
    groups = []
    for g in res.get("groups", []):
        ids = [i for i in g.get("ids", []) if i not in seen and any(c.id == i for c in claims)]
        if ids:
            seen.update(ids)
            groups.append({"ids": ids, "disputed": bool(g.get("disputed")), "note": g.get("note", "")})
    groups += [{"ids": [c.id]} for c in claims if c.id not in seen]
    return groups


def build_ledger(client: Any, model: str, topic: str, sources: list[Source], as_of: str = "",
                 log: Callable[[str], None] = print) -> Ledger:
    ledger = Ledger(topic=topic, sources=sources, as_of=as_of)
    raw: list[Claim] = []
    for s in sources:
        log(f"extracting claims from {s.outlet}…")
        try:
            items = extract_claims(client, model, s)
        except Exception as exc:
            log(f"[deep] extraction failed for {s.outlet}: {exc}")
            continue
        kept = 0
        for it in items:
            c = Claim(id=f"X{len(raw) + 1}", text=it["text"].strip(), kind=it.get("kind", "fact"),
                      speaker=it.get("speaker", "").strip(), quote=it.get("quote", "").strip(),
                      evidence=it["evidence"].strip(), date=it.get("date", "").strip(), source_ids=[s.id])
            why = verify_claim(c, s.text)
            if why:
                log(f"[deep] dropped a claim ({why}): {c.text[:70]}")
                continue
            raw.append(c)
            kept += 1
        log(f"  {kept} verified claims from {s.outlet}")
    log("cross-checking claims across sources…")
    by_id = {c.id: c for c in raw}
    final: list[Claim] = []
    for g in merge_groups(client, model, raw):
        members = [by_id[i] for i in g["ids"]]
        if g.get("disputed") and len(members) > 1:
            for m in members:
                m.note = f"Sources differ: {g.get('note', '')}".strip()
                final.append(m)
                assign_status(m, ledger, disputed=True)
            continue
        canon = sorted(members, key=lambda m: min((ledger.source(s).tier for s in m.source_ids), default=3))[0]
        canon.source_ids = sorted({s for m in members for s in m.source_ids})
        assign_status(canon, ledger)
        final.append(canon)
    for i, c in enumerate(final, 1):          # tidy, stable ids
        c.id = f"C{i}"
    ledger.claims = final
    return ledger


# ----------------------------------------------------------------------------- 2. recommendations
RECOMMEND_SYSTEM = """You advise a fact-based political analysis channel. From the verified claims below decide
(1) which angle the evidence best supports, and (2) how long the video should be.
Angles: neutral (evidence is mixed/thin or both sides are well documented), critical (the verified claims reveal gaps between
statements and documented facts, inconsistencies, unmet promises, unanswered questions or costs), supportive (the verified claims
document delivered results or sound reasoning).
Base this ONLY on the evidence balance, never on your own political view. Prefer neutral when unsure.
Length in minutes (5-15): roughly one minute per 3 well-supported claims, shorter if the material is thin."""
RECOMMEND_SCHEMA = {"type": "object", "properties": {
    "stance": {"type": "string", "enum": STANCES}, "why": {"type": "string"},
    "minutes": {"type": "integer"}, "minutes_why": {"type": "string"},
    "key_questions": {"type": "array", "items": {"type": "string"}}}, "required": ["stance", "why", "minutes"]}


def recommend_minutes(n_usable: int) -> int:
    return 5 if n_usable < 6 else 7 if n_usable < 10 else 9 if n_usable < 16 else 12 if n_usable < 24 else 15


def ledger_brief(ledger: Ledger, limit: int = 60) -> str:
    rows = []
    for c in ledger.usable()[:limit]:
        who = f" | by {c.speaker}" if c.speaker else ""
        rows.append(f"{c.id} [{c.status}] ({c.kind}) {c.text}{who} | {', '.join(friendly(o) for o in ledger.outlets_for(c))}")
    return "\n".join(rows)


def recommend(client: Any, model: str, ledger: Ledger) -> dict[str, Any]:
    n = len(ledger.usable())
    fallback = {"stance": "neutral", "why": "Not enough evidence to lean either way, so staying neutral.",
                "minutes": recommend_minutes(n), "minutes_why": f"{n} usable claims.", "key_questions": []}
    if client is None or n == 0:
        return fallback
    try:
        res = call_tool(client, model, RECOMMEND_SYSTEM, f"Topic: {ledger.topic}\n\nClaims:\n{ledger_brief(ledger)}",
                        "recommend_angle", RECOMMEND_SCHEMA, 1200)
    except Exception as exc:
        print(f"[deep] recommendation failed: {exc}")
        return fallback
    return {"stance": res["stance"] if res.get("stance") in STANCES else "neutral", "why": res.get("why", ""),
            "minutes": max(5, min(15, int(res.get("minutes") or fallback["minutes"]))),
            "minutes_why": res.get("minutes_why", ""), "key_questions": res.get("key_questions", [])[:5]}


# ----------------------------------------------------------------------------- 3. writing
STANCE_RULES = {
    "neutral": ("STANCE - neutral: present every documented position with equal weight. The analysis weighs the evidence without "
                "taking a side. The conclusion says what is established and what remains unknown."),
    "critical": ("STANCE - critical (but fair): scrutinise the decision/actor at the centre of the story. Show gaps between what was "
                 "claimed and what the documents show, inconsistencies, costs and unanswered questions. EVERY critical point must rest on "
                 "ledger claims. Criticise decisions and actions, never a person's character, and never attribute private motives or feelings. "
                 "Include a counterpoint scene with the strongest documented defence/response, or state that none was found."),
    "supportive": ("STANCE - supportive (complementary): explain the strengths, rationale and documented results of the decision/actor, "
                   "grounded in ledger claims. Do not hide documented problems: include caveats and open questions in a counterpoint scene."),
}

WRITE_SYSTEM = """You write a YouTube analysis video for an Indian political-news channel: confident, clear, conversational,
NOT a plain news bulletin - it explains what happened AND what it means. You may use ONLY the claims in the ledger.

{lang_rules}
Always write numbers as digits (so they can be fact-checked), e.g. "15 percent".

{stance_rules}

STRUCTURE - scenes in this order (a scene is ~20-35 seconds; use the sections as listed, several scenes per section where useful):
 hook (1 scene: a question or striking verified fact + stakes + promise; mention "as of <time>" if given) ->
 recap (what happened, dates, numbers) -> context (how we got here) -> positions (each side's documented statements; use quote scenes) ->
 analysis (2-4 scenes, each a different lens: incentives/politics, legal-administrative, precedent/data) -> counterpoint (other side / what is missing) ->
 scenarios (what could happen next: possibilities, not predictions; what to watch and when, only dates from the ledger) -> close (takeaway + a question + subscribe).
Use delayed-payoff and pattern-interrupts (a new angle or graphic every scene), but always pay off what you promise.

EVERY scene is a list of beats. Each beat has a type:
 fact (states a ledger claim; cite claim_ids) | quote (reports a statement; cite the quote claim; any words in "double quotes" must be word-for-word from the ledger) |
 analysis (your reasoning; cite the claim_ids it rests on; phrase it as interpretation - "iska matlab yeh ho sakta hai", "evidence yeh suggest karta hai" - never certainty about motives) |
 transition / question / cta (no claims, no facts, no numbers) | gap ("we could not find X": allowed without claims).
Rules the checker enforces automatically:
 - a fact/quote beat with claim status reported / alleged / quoted / disputed MUST name the outlet or speaker in `attributed_to` AND in the beat text
   (use the "attribute-as" names listed per claim); disputed claims: say the reports differ and cite both.
 - alleged claims are never stated as fact; say who alleged it.
 - numbers must come from the cited claims. No invented numbers, dates, names or quotes.
 - never: mind-reading ("he is scared", "they panicked", "sweating"), hearsay ("sources say", "it is said", "inside story"), insults.
Cards: for a scene you may add card = {{type: quote|number|timeline|sources, claim_ids: [...], label: short label}} - quote needs a quote claim, number a claim with a number,
timeline 3-6 claims that have dates, sources shows the outlets and headlines behind the cited claims (a copyright-safe way to show "what the reports say";
use it when you introduce the reporting). The card content is filled from the ledger by code.
Also give per scene: section, headline (max 50 chars), visual_query (2-4 English words for a stock photo, never a person's name).
Title: honest, curiosity-driven, max 70 chars. Description: 2-3 lines."""

WRITE_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "description": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
    "scenes": {"type": "array", "items": {"type": "object", "properties": {
        "section": {"type": "string", "enum": SECTIONS},
        "headline": {"type": "string"}, "visual_query": {"type": "string"},
        "card": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["none", "quote", "number", "timeline", "sources"]},
            "claim_ids": {"type": "array", "items": {"type": "string"}}, "label": {"type": "string"}}},
        "beats": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["fact", "quote", "analysis", "transition", "question", "cta", "gap"]},
            "text": {"type": "string"}, "claim_ids": {"type": "array", "items": {"type": "string"}},
            "attributed_to": {"type": "string"}}, "required": ["type", "text"]}}},
        "required": ["section", "headline", "beats"]}}},
    "required": ["title", "description", "tags", "scenes"]}

DEFAULT_BLOCK = [
    r"\b(ghabra\w*|dar gay[ei]|darr gay[ei]|dara hua|sweat\w*|paseen\w*|scared|afraid|terrified|panick?\w*|trembl\w*)\b",
    r"(sutron\s+(ke\s+)?(mutabik|anusar|ke hawale)|aisa kaha jata hai|kaha ja raha hai ki|andar ki khabar|inside (info|story|source)|sources? say|it is said|rumou?rs?)",
    r"(घबरा|डर गए|डर गई|पसीने|ऐसा कहा जाता है|सूत्रों|अंदर की खबर|अंदर की बात)",
]
DEFAULT_WARN = [r"\b(surrender(ed)?|on (his|her|their) knees|ghutno?n? par|over-?smart|dhoka|gaddar|tanashah)\b",
                r"(सरेंडर|घुटनों पर|गद्दार)"]


def _names(ledger: Ledger, ids: list[str]) -> list[str]:
    out: list[str] = []
    for i in ids:
        c = ledger.claim(i)
        if c and c.status in NEEDS_ATTRIBUTION:
            out += attribution_names(c, ledger)
    return out


def claims_block(ledger: Ledger) -> str:
    rows = []
    for c in ledger.usable():
        names = ", ".join(dict.fromkeys(attribution_names(c, ledger)[:4]))
        extra = f' | quote: "{c.quote}"' if c.quote else ""
        who = f" | speaker: {c.speaker}" if c.speaker else ""
        rows.append(f"{c.id} [{c.status}] ({c.kind}) {c.text}{who}{extra} | date: {c.date or '-'} | "
                    f"attribute-as: {names or '-'} | note: {c.note or '-'}")
    return "\n".join(rows)


def words_budget(minutes: float) -> tuple[int, int]:
    target = int(minutes * 60 * WORDS_PER_SEC * 0.92)       # some seconds are cards/pauses
    return int(target * 0.75), int(target * 1.2)


def lint(res_scenes: list[dict[str, Any]], ledger: Ledger, stance: str, minutes: float,
         block: list[str] | None = None, warn: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Return (violations, warnings). Violations make the script invalid."""
    bad: list[str] = []
    soft: list[str] = []
    blk = [re.compile(p, re.I) for p in (block or DEFAULT_BLOCK)]
    wrn = [re.compile(p, re.I) for p in (warn or DEFAULT_WARN)]
    total_words = 0
    sections = [s.get("section") for s in res_scenes]
    if not res_scenes or sections[0] != "hook":
        bad.append("The first scene must be section=hook.")
    if sections and sections[-1] != "close":
        bad.append("The last scene must be section=close.")
    if "analysis" not in sections:
        bad.append("Include at least one section=analysis scene.")
    if stance in ("critical", "supportive") and "counterpoint" not in sections:
        bad.append(f"A {stance} video must include a section=counterpoint scene (the other side / what is missing).")
    for need in ("recap", "scenarios"):
        if need not in sections:
            bad.append(f"Include a section={need} scene.")
    for si, sc in enumerate(res_scenes):
        tag = f"Scene {si + 1} ({sc.get('section')})"
        beats = sc.get("beats") or []
        if not beats:
            bad.append(f"{tag} has no beats.")
        for bi, b in enumerate(beats):
            typ, text = b.get("type", ""), (b.get("text") or "").strip()
            ids = b.get("claim_ids") or []
            total_words += len(text.split())
            where = f"{tag}, beat {bi + 1} ({typ})"
            if not text:
                bad.append(f"{where} is empty.")
                continue
            for p in blk:
                if (m := p.search(text)):
                    bad.append(f"{where}: banned wording '{m.group(0)}' (mind-reading / hearsay / abuse).")
            for p in wrn:
                if (m := p.search(text)):
                    soft.append(f"{where}: loaded wording '{m.group(0)}' - consider a neutral word.")
            cited = [ledger.claim(i) for i in ids]
            if any(c is None for c in cited):
                bad.append(f"{where}: cites a claim id that is not in the ledger {ids}.")
                continue
            if typ in ("fact", "quote", "analysis") and not ids:
                bad.append(f"{where}: must cite at least one ledger claim.")
                continue
            for c in cited:
                if c.status not in USABLE:
                    bad.append(f"{where}: claim {c.id} is {c.status} and cannot be used.")
            if typ in ("fact", "quote") and any(c.kind == "allegation" for c in cited if c):
                if not any(w in text.lower() for w in ("alleg", "aarop", "accus", "claimed", "kaha", "said", "bola", "bataya", "criticis", "aalochana")):
                    soft.append(f"{where}: an allegation should clearly be framed as the speaker's allegation.")
            # numbers must come from the cited claims
            allowed: set[str] = set()
            for c in cited:
                allowed |= set(c.numbers) | numbers_in(c.text) | numbers_in(c.evidence) | numbers_in(c.date)
            if typ not in ("analysis", "transition", "question", "cta", "gap"):
                extra = numbers_in(text) - allowed
            else:
                extra = {n for n in numbers_in(text) - allowed if not (n.isdigit() and int(n) <= 10)}
            if extra:
                bad.append(f"{where}: number(s) {sorted(extra)} are not in the cited claims.")
            # attribution
            need = _names(ledger, ids) if typ in ("fact", "quote") else []
            if need:
                at = (b.get("attributed_to") or "").strip().lower()
                low = text.lower()
                ok = len(at) >= 3 and any(at in n.lower() or n.lower() in at for n in need) and at in low
                if not ok:
                    bad.append(f"{where}: claim needs attribution - name one of {sorted(set(need))[:4]} in attributed_to AND in the text.")
            # verbatim quotes
            for q in re.findall(r"[\"“”]([^\"“”]{12,})[\"“”]", text):
                if not any(contains(c.quote or c.evidence, q) or contains(c.evidence, q) for c in cited):
                    bad.append(f"{where}: quoted words \"{q[:50]}\" are not word-for-word in the cited claims.")
        card = sc.get("card") or {}
        if card.get("type") in ("quote", "number", "timeline", "sources"):
            cs = [ledger.claim(i) for i in card.get("claim_ids") or []]
            if any(c is None or c.status not in USABLE for c in cs) or not cs:
                bad.append(f"{tag}: card cites unknown/unusable claims.")
            elif card["type"] == "quote" and not all(c.quote for c in cs):
                bad.append(f"{tag}: a quote card needs quote claims.")
            elif card["type"] == "number" and not any(c.numbers for c in cs):
                bad.append(f"{tag}: a number card needs a claim with a number.")
            elif card["type"] == "timeline" and sum(1 for c in cs if c.date) < 2:
                bad.append(f"{tag}: a timeline card needs 2+ dated claims.")
            elif card["type"] == "sources" and not any(ledger.source(sid) and ledger.source(sid).outlet != "user notes"
                                                       for c in cs for sid in c.source_ids):
                bad.append(f"{tag}: a sources card needs claims that come from published sources.")
    lo, hi = words_budget(minutes)
    if not (lo * 0.8 <= total_words <= hi * 1.1):
        bad.append(f"Narration is {total_words} words; for {minutes} minutes it must be about {lo}-{hi}.")
    return bad, soft


SECTION_LABEL = {"recap": "facts", "context": "facts", "positions": "quote", "analysis": "analysis",
                 "counterpoint": "analysis", "scenarios": "analysis", "hook": "news", "close": "news"}


def source_tag(ledger: Ledger, ids: list[str], limit: int = 2) -> str:
    srcs: list[Source] = []
    for i in ids:
        c = ledger.claim(i)
        for sid in (c.source_ids if c else []):
            s = ledger.source(sid)
            if s and s not in srcs:
                srcs.append(s)
    srcs.sort(key=lambda s: s.tier)
    names = list(dict.fromkeys(friendly(s.outlet) for s in srcs if s.outlet != "user notes"))
    return " · ".join(names[:limit])


def to_script(res: dict[str, Any], ledger: Ledger, stance: str, lang: str) -> Script:
    scenes: list[Scene] = []
    n = len(res["scenes"])
    for i, sc in enumerate(res["scenes"]):
        beats = sc.get("beats") or []
        ids = list(dict.fromkeys(c for b in beats for c in (b.get("claim_ids") or [])))
        section = sc.get("section", "recap")
        card = sc.get("card") or {}
        scenes.append(Scene(
            narration=" ".join((b.get("text") or "").strip() for b in beats),
            headline=(sc.get("headline") or "")[:60], visual_query=sc.get("visual_query", ""),
            label=t(lang, SECTION_LABEL.get(section, "news")),
            kind="analysis" if section in ("analysis", "counterpoint", "scenarios") else ("outro" if i == n - 1 else "news"),
            section=section, claim_ids=ids, source_tag=source_tag(ledger, ids),
            card=({"type": card["type"], "claim_ids": card.get("claim_ids", []), "label": card.get("label", "")}
                  if card.get("type") in ("quote", "number", "timeline", "sources") else None)))
    return Script(title=res["title"].strip(), description=res["description"].strip(),
                  tags=[x.strip() for x in res.get("tags", [])][:12],
                  scenes=scenes, sources=sorted({friendly(s.outlet) for s in ledger.sources if s.outlet != "user notes"}),
                  stance=stance, language=lang)


@dataclass
class Written:
    script: Script
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def as_of_text(tz: str = "Asia/Kolkata", now: datetime | None = None) -> str:
    n = now or datetime.now(ZoneInfo(tz))
    return n.strftime("%d %b %Y, %I:%M %p IST").lstrip("0")


def write_analysis(client: Any, model: str, ledger: Ledger, stance: str, minutes: float, lang: str, channel: str,
                   cfg_analysis: dict | None = None, insights: str = "", as_of: str = "",
                   log: Callable[[str], None] = print) -> Written:
    ca = cfg_analysis or {}
    lo, hi = words_budget(minutes)
    system = WRITE_SYSTEM.format(lang_rules=LANG_RULES[norm(lang)], stance_rules=STANCE_RULES[stance])
    base = (f"Channel: {channel}\nTopic: {ledger.topic}\nTarget length: {minutes} minutes (about {lo}-{hi} words of narration in total)\n"
            f"As of: {as_of or ledger.as_of or 'now'}\n\nLEDGER (the only facts you may use):\n{claims_block(ledger)}"
            + (f"\n\nWhat has worked on this channel (style only, never facts):\n{insights}" if insights else ""))
    feedback, written = "", None
    for attempt in range(3):
        res = call_tool(client, model, system, base + feedback, "submit_analysis", WRITE_SCHEMA, 16000)
        bad, soft = lint(res.get("scenes", []), ledger, stance, minutes, ca.get("banned_patterns"), ca.get("warn_patterns"))
        written = Written(to_script(res, ledger, stance, lang), bad, soft, res)
        if not bad:
            break
        log(f"script check found {len(bad)} problem(s); asking for a fix (attempt {attempt + 1}/3)…")
        feedback = ("\n\nYour previous draft FAILED the automatic fact check. Fix every problem and return the full corrected script:\n- "
                    + "\n- ".join(bad[:25]))
    assert written is not None
    return written


# ----------------------------------------------------------------------------- 4. description
def _mmss(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(script: Script, starts: list[float], intro: float, lang: str) -> list[tuple[str, str]]:
    out: list[tuple[float, str]] = []
    for i, sc in enumerate(script.scenes):
        if not sc.section or (out and out[-1][1] == t(lang, f"ch_{sc.section}")):
            continue
        out.append((0.0 if not out else starts[i], t(lang, f"ch_{sc.section}")))
    # YouTube needs 3+ chapters, each at least 10 seconds long
    out = [c for k, c in enumerate(out) if k == len(out) - 1 or out[k + 1][0] - c[0] >= 10]
    return [(_mmss(sec), name) for sec, name in out] if len(out) >= 3 else []


def build_description(script: Script, ledger: Ledger, starts: list[float], intro: float, credits: list[str],
                      channel: dict, lang: str, as_of: str, ai_note: bool = True) -> str:
    lines = [script.description, ""]
    if as_of:
        lines += [f"{t(lang, 'asof')} {as_of}", ""]
    ch = chapters(script, starts, intro, lang)
    if ch:
        lines += [t(lang, "chapters")] + [f"{ts} {name}" for ts, name in ch] + [""]
    lines.append(t(lang, "d_sources"))
    for tier in (1, 2, 3):
        group = [s for s in ledger.sources if s.tier == tier and s.url]
        if group:
            lines.append(f"[{t(lang, f'tier{tier}')}]")
            lines += [f"• {friendly(s.outlet)}: {s.url}" for s in group]
    lines += ["", t(lang, "d_analysis"), t(lang, "d_unconfirmed")]
    if credits:
        lines += ["", t(lang, "d_credits")] + [f"• {c}" for c in credits]
    if ai_note:
        lines += ["", t(lang, "d_ai")]
    lines += ["", f"{channel['name']} {channel.get('handle', '')}".strip(),
              " ".join("#" + x.replace(" ", "") for x in script.tags[:4])]
    return "\n".join(lines)[:4900]
