"""Study a competitor / reference video from its pasted transcript: structure, pacing and the techniques used,
plus a flag list of the things our fact rules would NOT allow (so you copy the format, not the weaknesses)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# "0:07 7 seconds text" as copied from YouTube (the spelled-out time is glued on), or a plain "0:07 text"
_GLUED = re.compile(r"^(\d+):(\d\d)\d+\s*(?:minutes?|seconds?)(?:,\s*\d+\s*(?:seconds?|minutes?))?(.*)$")
_PLAIN = re.compile(r"^(\d+):(\d\d)\s+(.*)$")

PATTERNS: dict[str, tuple[str, str, list[str]]] = {
    # key: (label, kind, regexes)  kind = technique | risk
    "direct_address": ("Direct address to the viewer", "technique", [r"दोस्तों", r"\bdosto\b", r"\bfriends\b"]),
    "deferred_payoff": ("Delayed payoff ('I'll explain later')", "technique",
                        [r"आगे बताऊंगा", r"आ रहा हूं", r"आ रहा हूँ", r"बताने जा रहा", r"aage bataunga", r"later in (this|the) video"]),
    "first_person_authority": ("First-person authority ('I would say')", "technique", [r"मैं तो कहूंगा", r"मैं कह", r"mai(n)? kahunga"]),
    "hedge_you_can_say": ("'You can say...' hedging (claim without owning it)", "risk", [r"आप कह सकते हैं", r"aap kah sakte"]),
    "hearsay": ("Hearsay / unnamed sources", "risk", [r"ऐसा कहा जाता है", r"कहा जा रहा है", r"सूत्रों", r"खबरें आ रही", r"aisa kaha jata", r"sources say", r"sutron"]),
    "insider_claim": ("'Inside story' claims", "risk", [r"अंदर की (खबर|बात)", r"पर्दे के पीछे", r"inside (story|info)", r"andar ki (khabar|baat)"]),
    "mind_reading": ("Attributing fear / feelings to named people", "risk", [r"घबरा", r"डर गए", r"डर गई", r"पसीने", r"हिम्मत नहीं", r"ghabra", r"dar gay", r"scared", r"afraid"]),
    "loaded_words": ("Loaded wording", "risk", [r"सरेंडर", r"घुटनों", r"ओवर ?स्मार्ट", r"तख्त", r"surrender", r"on (his|their) knees"]),
    "cta": ("Call to action (subscribe / like / comment)", "technique", [r"सब्सक्राइब", r"subscribe", r"बेल आइकॉन", r"लाइक", r"कमेंट"]),
}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class Seg:
    t: float
    text: str


def parse(raw: str) -> list[Seg]:
    segs: list[Seg] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _GLUED.match(line) or _PLAIN.match(line)
        if m:
            segs.append(Seg(int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip()))
        elif segs:                                   # continuation line
            segs[-1].text += " " + line
    return segs


def mmss(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def analyze(raw: str) -> dict[str, Any]:
    segs = parse(raw)
    if not segs:
        raise ValueError("No timestamped lines found. Paste the transcript from YouTube (… > Show transcript).")
    text = " ".join(s.text for s in segs)
    words = len(text.split())
    duration = segs[-1].t + max(4.0, (segs[-1].t - segs[-2].t) if len(segs) > 1 else 6.0)
    per_minute: list[int] = [0] * (int(duration // 60) + 1)
    for s in segs:
        per_minute[int(s.t // 60)] += len(s.text.split())
    found: dict[str, list[float]] = {}
    for key, (_, _, pats) in PATTERNS.items():
        hits = [s.t for s in segs if any(re.search(p, s.text, re.I) for p in pats)]
        if hits:
            found[key] = hits
    numbers = [(mmss(s.t), n) for s in segs for n in _NUM.findall(s.text)]
    questions = [mmss(s.t) for s in segs if "?" in s.text or "क्या" in s.text[:30]]
    hook_end = next((s.t for s in segs if s.t >= 40), segs[-1].t)
    hook = " ".join(s.text for s in segs if s.t < hook_end)
    cta_start = min(found.get("cta", [duration]))
    return {"duration": duration, "words": words, "wpm": round(words / (duration / 60)), "per_minute": per_minute,
            "hook": hook, "found": found, "numbers": numbers, "questions": questions,
            "cta_at": cta_start, "segments": len(segs)}


def report(raw: str) -> str:
    a = analyze(raw)
    out = [f"# Format study", "",
           f"- Length: **{mmss(a['duration'])}**, {a['words']} words, about **{a['wpm']} words/minute** "
           f"({'fast' if a['wpm'] > 165 else 'conversational' if a['wpm'] > 125 else 'slow, deliberate'}).",
           f"- Words per minute of video: {a['per_minute']}",
           f"- Hook (first ~40s): {a['hook'][:260]}{'…' if len(a['hook']) > 260 else ''}",
           f"- Call to action starts at **{mmss(a['cta_at'])}** (last {mmss(a['duration'] - a['cta_at'])} of the video).",
           f"- Questions posed to the viewer at: {', '.join(a['questions'][:8]) or 'none'}", "",
           "## Techniques worth borrowing (they are about delivery, not about unsourced claims)"]
    for key, (label, kind, _) in PATTERNS.items():
        if kind == "technique" and key in a["found"]:
            h = a["found"][key]
            out.append(f"- {label}: {len(h)}x (first at {mmss(h[0])})")
    out += ["", "## What our fact rules would not allow (do NOT copy these)"]
    risks = [(k, PATTERNS[k][0], v) for k, v in a["found"].items() if PATTERNS[k][1] == "risk"]
    out += [f"- {label}: {len(h)}x, e.g. at {', '.join(mmss(t) for t in h[:4])}" for _, label, h in risks] or ["- none detected"]
    out += ["", "## Numbers mentioned (verify every one against a source before reusing)"]
    out.append(", ".join(f"{n} @{t}" for t, n in a["numbers"][:40]) or "none")
    out += ["", "## How to adapt", "- Keep: strong question hook, a delayed payoff you really pay off, direct address, a clear turn, a closing next-step.",
            "- Replace: 'inside story' and 'it is said' with a Confirmed / Reported / Alleged / Unknown breakdown, each item sourced.",
            "- Replace: statements about what a named person feels or fears with documented actions and dated statements.",
            "- Add: an analysis section that is labelled as interpretation and rests only on the verified claims."]
    return "\n".join(out)
