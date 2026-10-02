from __future__ import annotations

import re
from typing import Any

from .llm import call_tool
from .media import thumb_b64
from .models import Asset, Scene, Shot

MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]
MIN_SHOT, MAX_SHOT = 2.6, 7.0

SYSTEM = """You are the picture editor of a TV news channel. You receive the scenes of a Hindi news
script and a set of candidate photos (some supplied by the channel owner, some stock).
For every scene choose 1-3 photos that best illustrate what is being said, and arrange them in the order they should appear.
Rules:
- Prefer the owner's photos (kind=user) whenever they are relevant.
- Scene 1 is the hook: give it the most striking, relevant photo.
- Avoid repeating a photo in consecutive scenes unless photos are scarce.
- NEVER pair a photo with a claim it does not clearly support (e.g. do not use a photo of person A for a statement about person B). When unsure, choose a neutral photo (building, crowd, flag, document).
- For each photo give focus_x / focus_y (0-1): the point of interest (faces, subject) that must stay in frame when cropped. Use motion: zoom_in, zoom_out, pan_left, pan_right.
- Skip photos that are blurry, contain watermarks/text overlays from other channels, or look unrelated."""

SCHEMA = {
    "type": "object",
    "properties": {"scenes": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "scene": {"type": "integer"},
            "photos": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string"},
                    "focus_x": {"type": "number"}, "focus_y": {"type": "number"},
                    "motion": {"type": "string", "enum": MOTIONS + ["still"]}},
                "required": ["asset_id"]}}},
        "required": ["scene", "photos"]}}},
    "required": ["scenes"],
}


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"\w+", t.lower()) if len(w) > 2}


def heuristic_plan(scenes: list[Scene], assets: list[Asset]) -> list[list[Shot]]:
    """No-LLM fallback: keyword overlap with captions, then round-robin; user photos first."""
    use_count = {a.id: 0 for a in assets}
    plan: list[list[Shot]] = []
    for i, sc in enumerate(scenes):
        if sc.kind == "clip":
            plan.append([])
            continue
        want = _words(f"{sc.visual_query} {sc.headline}")
        ranked = sorted(assets, key=lambda a: (-len(want & _words(a.caption)),
                                               use_count[a.id], a.kind != "user"))
        pick = ranked[:2 if sc.kind != "outro" else 1]
        shots = []
        for j, a in enumerate(pick):
            use_count[a.id] += 1
            shots.append(Shot(a.id, i, motion=MOTIONS[(i + j) % 4]))
        plan.append(shots)
    return plan


def llm_plan(client: Any, model: str, scenes: list[Scene], assets: list[Asset],
             max_per_scene: int) -> list[list[Shot]]:
    content: list[dict] = [{"type": "text", "text": "SCENES:\n" + "\n".join(
        f"{i}. [{s.kind}] {s.headline} — {s.narration[:220]}" if s.kind != "clip" else
        f"{i}. [clip] original video plays here; do NOT assign photos" for i, s in enumerate(scenes))}]
    content.append({"type": "text", "text": "\nCANDIDATE PHOTOS:"})
    for a in assets:
        content.append({"type": "text", "text": f"\nasset_id={a.id} kind={a.kind} size={a.width}x{a.height} "
                                                  f"caption={a.caption!r}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": thumb_b64(a.path)}})
    res = call_tool(client, model, SYSTEM, content, "arrange_photos", SCHEMA, 4000)
    by_id = {a.id: a for a in assets}
    plan: list[list[Shot]] = [[] for _ in scenes]
    for entry in res["scenes"]:
        i = entry["scene"]
        if not (0 <= i < len(scenes)):
            continue
        for k, p in enumerate(entry["photos"][:max_per_scene]):
            if p["asset_id"] in by_id:
                plan[i].append(Shot(
                    p["asset_id"], i, motion=p.get("motion") or MOTIONS[(i + k) % 4],
                    focus_x=min(1, max(0, p.get("focus_x", 0.5))),
                    focus_y=min(1, max(0, p.get("focus_y", 0.5)))))
    return plan


def repair(plan: list[list[Shot]], scenes: list[Scene], assets: list[Asset]) -> list[list[Shot]]:
    """Guarantee every scene has at least one shot, even if the model skipped it."""
    if not assets:
        return plan
    used: dict[str, int] = {a.id: 0 for a in assets}
    for shots in plan:
        for s in shots:
            used[s.asset_id] += 1
    for i, shots in enumerate(plan):
        if scenes[i].kind == "clip":
            shots.clear()
            continue
        if not shots:
            a = min(assets, key=lambda a: (used[a.id], a.kind != "user"))
            used[a.id] += 1
            shots.append(Shot(a.id, i, motion=MOTIONS[i % 4]))
    return plan


def make_plan(client: Any, model: str, scenes: list[Scene], assets: list[Asset],
              max_per_scene: int = 3) -> list[list[Shot]]:
    if client is None:
        plan = heuristic_plan(scenes, assets)
    else:
        try:
            plan = llm_plan(client, model, scenes, assets, max_per_scene)
        except Exception as exc:
            print(f"[planner] vision planning failed, using heuristic: {exc}")
            plan = heuristic_plan(scenes, assets)
    return repair(plan, scenes, assets)


def time_shots(plan: list[list[Shot]], starts: list[float], durations: list[float]) -> list[Shot]:
    """Give every shot an absolute start/end; split each scene evenly, dropping surplus photos."""
    flat: list[Shot] = []
    for i, shots in enumerate(plan):
        n = len(shots)
        if n == 0:
            continue
        while n > 1 and durations[i] / n < MIN_SHOT:
            n -= 1
        shots = shots[:n]
        step = durations[i] / n
        for k, s in enumerate(shots):
            s.start = starts[i] + k * step
            s.end = starts[i] + (k + 1) * step
            flat.append(s)
    return flat


def needed_photos(total_seconds: float) -> int:
    return max(3, round(total_seconds / 4.5))
