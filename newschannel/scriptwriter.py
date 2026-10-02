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
10. title: YouTube शीर्षक, हिंदी, 70 अक्षर तक, सनसनीखेज़ झूठ/क्लिकबेट नहीं, पर जिज्ञासा जगाने वाला।"""

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
                "kind": {"type": "string", "enum": ["news", "analysis", "outro"]},
            },
            "required": ["narration", "headline", "visual_query", "kind"]}},
    },
    "required": ["title", "description", "tags", "scenes"],
}


def _source_pack(topic: Topic) -> str:
    parts = []
    for s in topic.stories[:6]:
        parts.append(f"[{s.source}] {s.title}\n{s.summary}")
    return "\n\n".join(parts)


def budget(fmt: Format) -> tuple[int, int]:
    target = int(fmt.target_seconds * WORDS_PER_SEC)
    return int(target * 0.7), int(target * 1.3)


def validate(script: Script, fmt: Format) -> list[str]:
    lo, hi = budget(fmt)
    problems = []
    if not (lo <= script.word_count <= hi):
        problems.append(f"Narration has {script.word_count} words; it must be between {lo} and {hi}.")
    if not (3 <= len(script.scenes) <= fmt.max_scenes):
        problems.append(f"Use between 3 and {fmt.max_scenes} scenes (got {len(script.scenes)}).")
    if not any(s.kind == "analysis" for s in script.scenes):
        problems.append("Include one scene with kind=analysis.")
    if len(script.title) > 100:
        problems.append("Title must be at most 100 characters.")
    if any(not s.narration.strip() for s in script.scenes):
        problems.append("A scene has empty narration.")
    return problems


def write_script(client: Any, model: str, topic: Topic, fmt: Format, channel: str) -> Script:
    lo, hi = budget(fmt)
    kind = "YouTube Short (vertical, fast, ~%ds)" % fmt.target_seconds if fmt.portrait \
        else "long-form news video (~%d minutes)" % round(fmt.target_seconds / 60)
    base = (f"चैनल: {channel}\nफ़ॉर्मैट: {kind}\nकुल बोली जाने वाली लंबाई: {lo}–{hi} शब्द, "
            f"अधिकतम {fmt.max_scenes} दृश्य।\nविषय: {topic.title}\n\nस्रोत:\n{_source_pack(topic)}")
    feedback = ""
    script = None
    for _ in range(2):
        res = call_tool(client, model, SYSTEM, base + feedback, "submit_script", SCHEMA, 6000)
        script = Script(
            title=res["title"].strip(), description=res["description"].strip(),
            tags=[t.strip() for t in res.get("tags", [])][:12],
            scenes=[Scene(narration=s["narration"].strip(), headline=s["headline"].strip(),
                          visual_query=s.get("visual_query", ""), label=s.get("label", ""),
                          kind=s.get("kind", "news")) for s in res["scenes"]],
            sources=topic.sources,
        )
        problems = validate(script, fmt)
        if not problems:
            break
        feedback = "\n\nपिछली स्क्रिप्ट में ये समस्याएँ थीं, ठीक करो:\n- " + "\n- ".join(problems)
    assert script is not None
    for s in script.scenes:
        s.label = s.label or ("विश्लेषण" if s.kind == "analysis" else "ताज़ा खबर")
    return script


def build_description(script: Script, topic: Topic, cfg_channel: dict, short: bool,
                      credits: list[str], ai_note: bool = True) -> str:
    lines = [script.description, ""]
    lines.append("स्रोत / Sources:")
    seen = set()
    for st in topic.stories[:6]:
        if st.link not in seen:
            seen.add(st.link)
            lines.append(f"• {st.source}: {st.link}")
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
