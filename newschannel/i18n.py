"""Language support: 'hinglish' (Hindi + English words, Roman script - the default) and 'hindi' (Devanagari).

Everything the viewer sees or hears that is not the story itself (labels, tags, end-card text, description
boilerplate, TTS clean-up) comes from here, and so do the language rules given to the script writer."""
from __future__ import annotations

LANGS = ("hinglish", "hindi")

LABELS: dict[str, dict[str, str]] = {
    "hinglish": {
        "news": "Taaza Khabar", "analysis": "Vishleshan", "clip": "Original Video", "breaking": "BREAKING NEWS",
        "facts": "Confirmed Facts", "quote": "Statement", "timeline": "Timeline", "numbers": "By The Numbers",
        "ledger": "Kya pakka, kya nahi",
        "ticker": "Headlines", "source": "Source", "thumb_tag": "TAAZA KHABAR",
        "intro_tag": "Bharatiya rajneeti, seedhi aur saaf baat",
        "outro_title": "Channel ko subscribe karein", "outro_sub": "Roz taaza rajneetik khabrein",
        "endcard_title": "Agla video zaroor dekhein", "endcard_sub": "Subscribe karein",
        "d_sources": "Sources:", "d_clips": "Video excerpts:", "d_credits": "Image credits:",
        "d_ai": "Note: Is video ka script aur awaaz AI ki madad se bani hai aur publish se pehle editorial check se guzri hai. "
                "Yeh content jaankari aur vishleshan ke liye hai; kisi party ya vyakti ka samarthan ya virodh nahi.",
        "d_watch": "Watch next:", "d_playlist": "Full playlist:", "d_next": "Next:",
        "comment_q": "Aapki kya raay hai? Neeche comment mein bataiye.", "comment_next": "Agla video:",
        "comment_sub": "Aisi khabron ke liye channel ko subscribe karna na bhoolein.",
        "chapters": "Chapters:", "asof": "As of", "sources_tier": "Source quality",
        "tier1": "Official / primary", "tier2": "Major outlet / agency", "tier3": "Other",
    },
    "hindi": {
        "news": "ताज़ा खबर", "analysis": "विश्लेषण", "clip": "मूल वीडियो", "breaking": "ब्रेकिंग न्यूज़",
        "facts": "पक्के तथ्य", "quote": "बयान", "timeline": "टाइमलाइन", "numbers": "आँकड़ों में",
        "ledger": "क्या पक्का, क्या नहीं",
        "ticker": "सुर्खियाँ", "source": "स्रोत", "thumb_tag": "ताज़ा खबर",
        "intro_tag": "भारतीय राजनीति, सीधी और साफ़ बात",
        "outro_title": "चैनल को सब्सक्राइब करें", "outro_sub": "रोज़ ताज़ा राजनीतिक खबरें",
        "endcard_title": "अगला वीडियो ज़रूर देखिए", "endcard_sub": "सब्सक्राइब करें",
        "d_sources": "स्रोत / Sources:", "d_clips": "वीडियो अंश / Video excerpts:",
        "d_credits": "तस्वीरें / Image credits:",
        "d_ai": "ℹ️ इस वीडियो की स्क्रिप्ट और आवाज़ AI की मदद से तैयार की गई है और प्रकाशन से पहले संपादकीय जाँच से गुज़री है। "
                "यह सामग्री सूचना और विश्लेषण के उद्देश्य से है; किसी पार्टी या व्यक्ति का समर्थन/विरोध नहीं।",
        "d_watch": "Watch next / और देखिए:", "d_playlist": "Full playlist:", "d_next": "Next / अगला:",
        "comment_q": "आपकी क्या राय है? नीचे कमेंट में बताइए 👇", "comment_next": "▶ अगला वीडियो:",
        "comment_sub": "ऐसी खबरों के लिए चैनल को सब्सक्राइब करना न भूलें।",
        "chapters": "Chapters:", "asof": "As of", "sources_tier": "Source quality",
        "tier1": "Official / primary", "tier2": "Major outlet / agency", "tier3": "Other",
    },
}

# What to tell the script writer about the output language.
LANG_RULES: dict[str, str] = {
    "hinglish": (
        "OUTPUT LANGUAGE: Hinglish - natural spoken Hindi grammar mixed with common English words, written in ROMAN script "
        "(example: 'Dosto, kya Maharashtra sarkar ne ek hi din mein apna stand badal diya? Chaliye, facts dekhte hain.'). "
        "Write exactly how a confident Indian news analyst speaks on YouTube: short punchy sentences, direct address "
        "('dosto', 'aap'), English for terms people say in English (government, police, protest, budget, court, election). "
        "Spell Hindi words the common way people type them (kya, hai, nahi, sarkar, chunav). Write numbers as digits with the unit "
        "('15 percent', '2 lakh', '500 crore'). Headlines, titles, labels and thumbnail text are Hinglish too (Roman script)."),
    "hindi": (
        "OUTPUT LANGUAGE: simple spoken Hindi in DEVANAGARI script, news-anchor tone, short sentences. Write numbers in words "
        "so the voice pronounces them correctly. Headlines, titles, labels and thumbnail text are Hindi (Devanagari) too."),
}

# Clean-up before text-to-speech (helps pronunciation).
TTS_REPLACE: dict[str, list[tuple[str, str]]] = {
    "hinglish": [("%", " percent"), ("₹", " rupees "), ("&", " and ")],
    "hindi": [("%", " प्रतिशत"), ("₹", " रुपये "), ("&", " और "), ("PM", "पीएम"), ("CM", "सीएम"),
              ("BJP", "बीजेपी"), ("NDA", "एनडीए"), ("ECI", "चुनाव आयोग")],
}


def norm(lang: str) -> str:
    return lang if lang in LANGS else "hinglish"


def t(lang: str, key: str) -> str:
    return LABELS[norm(lang)][key]
