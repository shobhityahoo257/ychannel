from __future__ import annotations

from typing import Any

from .config import Format
from .i18n import LANG_RULES, norm, t
from .llm import call_tool
from .models import Scene, Script, Topic

WORDS_PER_SEC = 2.4  # natural Hindi/Hinglish TTS pace


def system_prompt(lang: str) -> str:
    return f"""You are an experienced Indian news anchor and script-writer for a YouTube channel on Indian politics.

{LANG_RULES[norm(lang)]}

Rules (follow strictly):
1. Use ONLY facts present in the given sources. Never add a number, quote, date or name of your own.
2. Be fair: no personal opinions, abuse or insulting language about any party or leader. If something is an allegation, say "X alleged ..." and
   give the other side where the sources do.
3. Attribute every important claim to its source ("according to BBC Hindi ...").
4. Language: short sentences, spoken style, natural for a voice-over.
5. Scene 1 is a strong hook: within 3 seconds say what happened and why it matters.
6. Include one scene with kind=analysis: context, background, and "what could this mean" - grounded in the facts, not predictions.
7. Last scene: a one-line summary plus a question for viewers / invitation to subscribe.
8. visual_query: 2-4 English words for a generic stock photo search (e.g. "Indian parliament building", "voters queue India"); never a person's name.
9. headline: a short on-screen headline (max ~50 characters).
10. If video clips (speeches/statements) are provided: make exactly one scene with kind="clip" for each clip (with its clip_id, empty narration).
    The scene before a clip introduces it (who, where, context); the scene after gives the summary / context / analysis. Describe what is said in a clip
    ONLY from the given transcript; do not put words in anyone's mouth or cut context to change the meaning. A clip scene is never the first scene.
11. title: YouTube title, max 70 characters, curiosity-driven but honest - no sensational lies."""


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string", "description": "2-3 line summary for the YouTube description"},
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
    parts = ["\n\nOriginal video clips:"]
    for i, c in enumerate(clips):
        parts.append(f"clip_id={i} | source: {c.credit or 'unknown'} | length: {c.duration:.0f}s | context: {c.note}\n"
                     f"What is said (transcript): {c.transcript[:1500] or '(not available)'}")
    return "\n".join(parts)


def write_script(client: Any, model: str, topic: Topic, fmt: Format, channel: str,
                 clips: list | None = None, insights: str = "", urgent: bool = False,
                 lang: str = "hinglish") -> Script:
    clips = clips or []
    lo, hi = budget(fmt, sum(c.duration for c in clips))
    kind = f"YouTube Short (vertical, fast, ~{fmt.target_seconds}s)" if fmt.portrait \
        else f"long-form news video (~{round(fmt.target_seconds / 60)} minutes)"
    base = (f"Channel: {channel}\nFormat: {kind}\nTotal spoken length: {lo}-{hi} words, at most {fmt.max_scenes} scenes.\n"
            f"Topic: {topic.title}\n\nSources:\n{_source_pack(topic)}{_clip_pack(clips)}"
            + ("\n\nThis is BREAKING news: say the latest event in the very first line, only firm facts, with caution "
               "like 'details are still coming in', and no speculation." if urgent else "")
            + (f"\n\nLessons from this channel's analytics (for style/hook only, never for facts):\n{insights}" if insights else ""))
    feedback = ""
    script = None
    for _ in range(2):
        res = call_tool(client, model, system_prompt(lang), base + feedback, "submit_script", SCHEMA, 6000)
        script = Script(
            title=res["title"].strip(), description=res["description"].strip(),
            tags=[x.strip() for x in res.get("tags", [])][:12],
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
        feedback = "\n\nThe previous script had these problems, fix them:\n- " + "\n- ".join(problems)
    assert script is not None
    for s in script.scenes:
        s.label = s.label or {"analysis": t(lang, "analysis"), "clip": t(lang, "clip")}.get(s.kind, t(lang, "news"))
    return script


def build_description(script: Script, topic: Topic, cfg_channel: dict, short: bool,
                      credits: list[str], ai_note: bool = True, clips: list | None = None,
                      lang: str = "hinglish") -> str:
    lines = [script.description, ""]
    lines.append(t(lang, "d_sources"))
    seen = set()
    for st in topic.stories[:6]:
        if st.link not in seen:
            seen.add(st.link)
            lines.append(f"• {st.source}: {st.link}")
    if clips:
        lines += ["", t(lang, "d_clips")] + [f"• {c.credit}: {c.note}" for c in clips]
    if credits:
        lines += ["", t(lang, "d_credits")] + [f"• {c}" for c in credits]
    if ai_note:
        lines += ["", t(lang, "d_ai")]
    lines += ["", f"{cfg_channel['name']} {cfg_channel.get('handle', '')}".strip()]
    tags = ["#" + x.replace(" ", "") for x in script.tags[:4]]
    if short:
        tags.insert(0, "#Shorts")
    lines.append(" ".join(tags))
    return "\n".join(lines)[:4900]


def fix_clip_scenes(script: Script, clips: list, lang: str = "hinglish") -> None:
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
            script.scenes.insert(pos, Scene("", (c.note or t(lang, "clip"))[:50], "", t(lang, "clip"), "clip", i))
            seen.add(i)
    if script.scenes and script.scenes[0].kind == "clip":
        script.scenes.insert(1 if len(script.scenes) > 1 else 0, script.scenes.pop(0))
