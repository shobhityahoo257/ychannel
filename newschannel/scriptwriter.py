from __future__ import annotations

from typing import Any

from .config import Format
from .llm import call_tool
from .models import Scene, Script, Topic

WORDS_PER_SEC = 2.4  # natural Hindi TTS pace

SYSTEM = """तुम एक अनुभवी हिंदी न्यूज़ एंकर और स्क्रिप्ट-राइटर हो। तुम्हारा चैनल भारतीय राजनीति पर है।

नियम (सख़्ती से मानो):
1. सिर्फ़ दिए गए स्रोतों में मौजूद तथ्य इस्तेमाल करो। कोई आँकड़ा, उद्धरण, तारीख़ या नाम अपनी तरफ़ से मत जोड़ो।
2. निष्पक्ष रहो: किसी पार्टी/नेता के बारे में निजी राय, गाली, आरोप या अपमानजनक भाषा नहीं। आरोप हो तो "…ने आरोप लगाया" और दोनों पक्ष बताओ।
3. हर अहम दावे के साथ स्रोत का ज़िक्र करो ("बीबीसी हिंदी के मुताबिक़…").
4. भाषा: सरल, बोलचाल की शुद्ध हिंदी, छोटे वाक्य, टीवी एंकर का लहजा। बोली जाने वाली लाइन में अंक शब्दों में लिखो (जैसे "तीन सौ", "पंद्रह प्रतिशत") ताकि उच्चारण सही रहे।
5. पहला दृश्य एक मज़बूत hook हो (पहले 3 सेकंड में बताओ क्या हुआ और क्यों मायने रखता है)।
6. एक दृश्य "विश्लेषण" (kind=analysis) ज़रूर रखो: संदर्भ, पृष्ठभूमि, और "इसका असर क्या हो सकता है" — यह चैनल का मौलिक योगदान है। विश्लेषण भी तथ्यों पर टिका हो, भविष्यवाणी नहीं।
7. आख़िरी दृश्य: एक पंक्ति का सार + दर्शकों से सवाल/सब्सक्राइब का निमंत्रण।
8. visual_query: अंग्रेज़ी में 2-4 शब्द का सामान्य फ़ोटो-सर्च (जैसे "Indian parliament building", "voters queue India"), कभी किसी व्यक्ति का नाम नहीं।
9. headline: स्क्रीन पर दिखने वाली छोटी हिंदी हेडलाइन (50 अक्षर तक)।
10. अगर वीडियो-क्लिप (भाषण/बयान) दिए गए हैं: हर क्लिप के लिए ठीक एक दृश्य kind="clip" बनाओ (clip_id के साथ, narration ख़ाली "")।
    क्लिप से ठीक पहले वाला दृश्य उसका परिचय दे (कौन, कहाँ, किस संदर्भ में), और क्लिप के बाद वाला दृश्य उसका सार/संदर्भ/विश्लेषण दे।
    क्लिप में जो बोला गया है उसे सिर्फ़ दिए गए transcript के आधार पर बताओ; अपनी तरफ़ से कोई बात उनके मुँह में मत डालो और संदर्भ से काटकर अर्थ मत बदलो।
    क्लिप का दृश्य पहला दृश्य न हो (पहले hook बोलो)।
11. title: YouTube शीर्षक, हिंदी, 70 अक्षर तक, सनसनीखेज़ झूठ/क्लिकबेट नहीं, पर जिज्ञासा जगाने वाला।"""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string", "description": "2-3 line Hindi summary for YouTube description"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "scenes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "narration": {"type": "string"},
                "headline": {"type": "string"},
                "visual_query": {"type": "string"},
                "label": {"type": "string"},
                "kind": {"type": "string", "enum": ["news", "analysis", "outro", "clip"]},
                "clip_id": {"type": "integer", "description": "Only for kind=clip: which supplied clip plays here"},
            },
            "required": ["narration", "headline", "kind"]}},
    },
    "required": ["title", "description", "tags", "scenes"],
}


def _source_pack(topic: Topic) -> str:
    parts = []
    for s in topic.stories[:6]:
        parts.append(f"[{s.source}] {s.title}\n{s.summary}")
    return "\n\n".join(parts)


def budget(fmt: Format, clip_seconds: float = 0.0) -> tuple[int, int]:
    """Word range for the spoken narration; original clip time is not narrated."""
    target = int(max(fmt.target_seconds * 0.35, fmt.target_seconds - clip_seconds) * WORDS_PER_SEC)
    return int(target * 0.7), int(target * 1.3)


def validate(script: Script, fmt: Format, clips: list | None = None) -> list[str]:
    lo, hi = budget(fmt, sum(c.duration for c in clips or []))
    problems = []
    if not (lo <= script.word_count <= hi):
        problems.append(f"Narration has {script.word_count} words; it must be between {lo} and {hi}.")
    if not (3 <= len(script.scenes) <= fmt.max_scenes):
        problems.append(f"Use between 3 and {fmt.max_scenes} scenes (got {len(script.scenes)}).")
    if not any(s.kind == "analysis" for s in script.scenes):
        problems.append("Include one scene with kind=analysis.")
    if len(script.title) > 100:
        problems.append("Title must be at most 100 characters.")
    if any(not s.narration.strip() for s in script.scenes if s.kind != "clip"):
        problems.append("A scene has empty narration.")
    n = len(clips or [])
    placed = sorted(-1 if s.clip_id is None else s.clip_id for s in script.scenes if s.kind == "clip")
    if placed != list(range(n)):
        problems.append(f"Provide exactly one kind=clip scene for each clip id 0..{n - 1} (got {placed}).")
    if script.scenes and script.scenes[0].kind == "clip":
        problems.append("The first scene must be spoken narration, not a clip.")
    return problems


def _clip_pack(clips: list) -> str:
    if not clips:
        return ""
    parts = ["\n\nवीडियो-क्लिप (मूल):"]
    for i, c in enumerate(clips):
        parts.append(f"clip_id={i} | स्रोत: {c.credit or 'अज्ञात'} | अवधि: {c.duration:.0f}s | संदर्भ: {c.note}\n"
                     f"जो बोला गया (transcript): {c.transcript[:1500] or '(उपलब्ध नहीं)'}")
    return "\n".join(parts)


def write_script(client: Any, model: str, topic: Topic, fmt: Format, channel: str,
                 clips: list | None = None, insights: str = "", urgent: bool = False) -> Script:
    clips = clips or []
    lo, hi = budget(fmt, sum(c.duration for c in clips))
    kind = "YouTube Short (vertical, fast, ~%ds)" % fmt.target_seconds if fmt.portrait \
        else "long-form news video (~%d minutes)" % round(fmt.target_seconds / 60)
    base = (f"चैनल: {channel}\nफ़ॉर्मैट: {kind}\nकुल बोली जाने वाली लंबाई: {lo}–{hi} शब्द, "
            f"अधिकतम {fmt.max_scenes} दृश्य।\nविषय: {topic.title}\n\nस्रोत:\n{_source_pack(topic)}{_clip_pack(clips)}"
            + ("\n\nयह ब्रेकिंग न्यूज़ है: पहली ही पंक्ति में सबसे ताज़ा घटना बताओ, सिर्फ़ पक्के तथ्य, "
               "'अभी जानकारी आ रही है' जैसी सावधानी के साथ, कोई अटकल नहीं।" if urgent else "")
            + (f"\n\nइस चैनल के एनालिटिक्स से सीख (सिर्फ़ शैली/hook के लिए, तथ्यों के लिए नहीं):\n{insights}" if insights else ""))
    feedback = ""
    script = None
    for _ in range(2):
        res = call_tool(client, model, SYSTEM, base + feedback, "submit_script", SCHEMA, 6000)
        script = Script(
            title=res["title"].strip(), description=res["description"].strip(),
            tags=[t.strip() for t in res.get("tags", [])][:12],
            scenes=[Scene(narration=s["narration"].strip(), headline=s["headline"].strip(),
                          visual_query=s.get("visual_query", ""), label=s.get("label", ""),
                          kind=s.get("kind", "news"),
                          clip_id=s.get("clip_id") if s.get("kind") == "clip" else None)
                    for s in res["scenes"]],
            sources=topic.sources,
        )
        problems = validate(script, fmt, clips)
        if not problems:
            break
        feedback = "\n\nपिछली स्क्रिप्ट में ये समस्याएँ थीं, ठीक करो:\n- " + "\n- ".join(problems)
    assert script is not None
    for s in script.scenes:
        s.label = s.label or {"analysis": "विश्लेषण", "clip": "मूल वीडियो"}.get(s.kind, "ताज़ा खबर")
    return script


def build_description(script: Script, topic: Topic, cfg_channel: dict, short: bool,
                      credits: list[str], ai_note: bool = True, clips: list | None = None) -> str:
    lines = [script.description, ""]
    lines.append("स्रोत / Sources:")
    seen = set()
    for st in topic.stories[:6]:
        if st.link not in seen:
            seen.add(st.link)
            lines.append(f"• {st.source}: {st.link}")
    if clips:
        lines += ["", "वीडियो अंश / Video excerpts:"] + [f"• {c.credit}: {c.note}" for c in clips]
    if credits:
        lines += ["", "तस्वीरें / Image credits:"] + [f"• {c}" for c in credits]
    if ai_note:
        lines += ["", "ℹ️ इस वीडियो की स्क्रिप्ट और आवाज़ AI की मदद से तैयार की गई है और प्रकाशन से पहले संपादकीय जाँच से गुज़री है। "
                      "यह सामग्री सूचना और विश्लेषण के उद्देश्य से है; किसी पार्टी या व्यक्ति का समर्थन/विरोध नहीं।"]
    lines += ["", f"{cfg_channel['name']} {cfg_channel.get('handle', '')}".strip()]
    tags = ["#" + t.replace(" ", "") for t in script.tags[:4]]
    if short:
        tags.insert(0, "#Shorts")
    lines.append(" ".join(tags))
    return "\n".join(lines)[:4900]


def fix_clip_scenes(script: Script, clips: list) -> None:
    """Guarantee every clip plays exactly once and never as the very first scene,
    even if the model got it wrong after retries."""
    seen: set[int] = set()
    keep = []
    for sc in script.scenes:
        if sc.kind == "clip":
            if sc.clip_id is None or not (0 <= sc.clip_id < len(clips)) or sc.clip_id in seen:
                continue
            seen.add(sc.clip_id)
            sc.narration = ""
        keep.append(sc)
    script.scenes = keep
    for i, c in enumerate(clips):
        if i not in seen:
            pos = min(len(script.scenes), 2 + len(seen))
            script.scenes.insert(pos, Scene("", (c.note or "मूल वीडियो")[:50], "", "मूल वीडियो", "clip", i))
            seen.add(i)
    if script.scenes and script.scenes[0].kind == "clip":
        script.scenes.insert(1 if len(script.scenes) > 1 else 0, script.scenes.pop(0))
