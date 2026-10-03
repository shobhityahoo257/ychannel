"""Language support: 'hinglish' (Hindi + English words, Roman script - the default) and 'hindi' (Devanagari).

Everything the viewer sees or hears that is not the story itself (labels, tags, end-card text, description
boilerplate, TTS clean-up) comes from here, and so do the language rules given to the script writer."""
from __future__ import annotations

LANGS = ("hinglish", "hindi")

LABELS: dict[str, dict[str, str]] = {
    "hinglish": {
        "news": "Taaza Khabar", "analysis": "Vishleshan", "clip": "Original Video", "breaking": "BREAKING NEWS",
        "facts": "Confirmed Facts", "quote": "Statement", "timeline": "Timeline", "numbers": "By The Numbers",
        "ledger": "Kya pakka, kya nahi", "sources_title": "What the reports say",
        "ticker": "Headlines", "source": "Source", "thumb_tag": "TAAZA KHABAR",
        "intro_tag": "Bharatiya rajneeti, seedhi aur saaf baat",
        "outro_title": "Channel ko subscribe karein", "outro_sub": "Roz taaza rajneetik khabrein",
        "endcard_title": "Agla video zaroor dekhein", "endcard_sub": "Subscribe karein",
        "d_sources": "Sources:", "d_clips": "Video excerpts:", "d_credits": "Image credits:",
        "d_ai": "Note: Is video ka script aur awaaz AI ki madad se bani hai aur publish se pehle editorial check se guzri hai. "
                "Yeh content jaankari aur vishleshan ke liye hai; kisi party ya vyakti ka samarthan ya virodh nahi.",
        "d_stock_note": "Note: Pexels/Pixabay photos are generic representative images, not photos of the actual event.",
        "d_watch": "Watch next:", "d_playlist": "Full playlist:", "d_next": "Next:",
        "comment_q": "Aapki kya raay hai? Neeche comment mein bataiye.", "comment_next": "Agla video:",
        "comment_sub": "Aisi khabron ke liye channel ko subscribe karna na bhoolein.",
        "chapters": "Chapters:", "asof": "As of", "sources_tier": "Source quality",
        "tier1": "Official / primary", "tier2": "Major outlet / agency", "tier3": "Other",
        "ch_hook": "Intro", "ch_recap": "Kya hua", "ch_context": "Background", "ch_positions": "Kisne kya kaha",
        "ch_analysis": "Vishleshan", "ch_counterpoint": "Doosra paksh", "ch_scenarios": "Aage kya ho sakta hai",
        "ch_close": "Nishkarsh",
        "d_analysis": "Is video ke facts neeche diye sources par based hain. 'Vishleshan' wale hisse hamari vyakhya hain, "
                      "jo in facts se nikali gayi hai; woh tathya nahi, raay hai.",
        "d_unconfirmed": "Jo baatein sirf ek source ne report ki ya kisi ne aarop ke roop mein kahin, unhe video mein naam lekar bataya gaya hai.",
    },
    "hindi": {
        "news": "ताज़ा खबर", "analysis": "विश्लेषण", "clip": "मूल वीडियो", "breaking": "ब्रेकिंग न्यूज़",
        "facts": "पक्के तथ्य", "quote": "बयान", "timeline": "टाइमलाइन", "numbers": "आँकड़ों में",
        "ledger": "क्या पक्का, क्या नहीं", "sources_title": "रिपोर्ट्स क्या कहती हैं",
        "ticker": "सुर्खियाँ", "source": "स्रोत", "thumb_tag": "ताज़ा खबर",
        "intro_tag": "भारतीय राजनीति, सीधी और साफ़ बात",
        "outro_title": "चैनल को सब्सक्राइब करें", "outro_sub": "रोज़ ताज़ा राजनीतिक खबरें",
        "endcard_title": "अगला वीडियो ज़रूर देखिए", "endcard_sub": "सब्सक्राइब करें",
        "d_sources": "स्रोत / Sources:", "d_clips": "वीडियो अंश / Video excerpts:",
        "d_credits": "तस्वीरें / Image credits:",
        "d_ai": "ℹ️ इस वीडियो की स्क्रिप्ट और आवाज़ AI की मदद से तैयार की गई है और प्रकाशन से पहले संपादकीय जाँच से गुज़री है। "
                "यह सामग्री सूचना और विश्लेषण के उद्देश्य से है; किसी पार्टी या व्यक्ति का समर्थन/विरोध नहीं।",
        "d_stock_note": "नोट: Pexels/Pixabay की तस्वीरें सामान्य प्रतीकात्मक तस्वीरें हैं, असली घटना की नहीं।",
        "d_watch": "Watch next / और देखिए:", "d_playlist": "Full playlist:", "d_next": "Next / अगला:",
        "comment_q": "आपकी क्या राय है? नीचे कमेंट में बताइए 👇", "comment_next": "▶ अगला वीडियो:",
        "comment_sub": "ऐसी खबरों के लिए चैनल को सब्सक्राइब करना न भूलें।",
        "chapters": "Chapters:", "asof": "As of", "sources_tier": "Source quality",
        "tier1": "Official / primary", "tier2": "Major outlet / agency", "tier3": "Other",
        "ch_hook": "परिचय", "ch_recap": "क्या हुआ", "ch_context": "पृष्ठभूमि", "ch_positions": "किसने क्या कहा",
        "ch_analysis": "विश्लेषण", "ch_counterpoint": "दूसरा पक्ष", "ch_scenarios": "आगे क्या हो सकता है",
        "ch_close": "निष्कर्ष",
        "d_analysis": "इस वीडियो के तथ्य नीचे दिए स्रोतों पर आधारित हैं। 'विश्लेषण' वाले हिस्से हमारी व्याख्या हैं, "
                      "जो इन तथ्यों से निकाली गई है; वह तथ्य नहीं, राय है।",
        "d_unconfirmed": "जो बातें सिर्फ़ एक स्रोत ने रिपोर्ट कीं या किसी ने आरोप के रूप में कहीं, उन्हें वीडियो में नाम लेकर बताया गया है।",
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
