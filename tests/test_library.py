import json
from pathlib import Path

from conftest import SCRIPT, FakeClient, make_photo

from newschannel.library import Library, ahash, hamming
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.tts import MockTTS


def photo(tmp, name, w=1200, h=800, seed=1):
    p = tmp / name
    make_photo(p, w, h, seed)
    return p


def test_add_dedupe_and_fill_gaps(tmp_path):
    lib = Library(tmp_path / "lib")
    a = photo(tmp_path, "a.jpg", seed=1)
    e, new = lib.add_file(a)
    assert new and e.caption == "" and lib.path(e).exists() and lib.thumb_path(e).exists()
    # the same picture resized + re-saved is recognised as a duplicate; the caption we now know is kept
    from PIL import Image
    Image.open(a).resize((600, 400)).save(tmp_path / "copy.jpg")
    e2, new2 = lib.add_file(tmp_path / "copy.jpg", caption="Parliament building", credit="PIB")
    assert not new2 and e2.id == e.id and len(lib.all()) == 1
    assert lib.get(e.id).caption == "Parliament building" and lib.get(e.id).credit == "PIB"
    e3, new3 = lib.add_file(photo(tmp_path, "b.jpg", seed=7))
    assert new3 and len(lib.all()) == 2
    assert lib.add_file(tmp_path / "notes.txt")[0] is None


def test_search_edit_delete_and_usage(tmp_path):
    lib = Library(tmp_path / "lib")
    e1, _ = lib.add_file(photo(tmp_path, "a.jpg", seed=1), caption="Parliament building at dusk")
    e2, _ = lib.add_file(photo(tmp_path, "b.jpg", seed=9), caption="Farmers protest crowd", source="stock")
    assert [e.id for e in lib.search("parliament")] == [e1.id]
    assert [e.id for e in lib.search("", source="stock")] == [e2.id]
    lib.update(e2.id, tags=["farmers", "protest", ""], credit="Pexels")
    assert lib.get(e2.id).tags == ["farmers", "protest"] and [e.id for e in lib.search("protest")] == [e2.id]
    lib.mark_used([e1.id, e1.id])
    assert lib.get(e1.id).used == 2
    assert lib.delete(e1.id) and not lib.path(e1).exists() and lib.get(e1.id) is None


def test_suggest_prefers_relevant_unused_and_uses_ai_when_available(tmp_path):
    lib = Library(tmp_path / "lib")
    a, _ = lib.add_file(photo(tmp_path, "a.jpg", seed=1), caption="Indian parliament building")
    b, _ = lib.add_file(photo(tmp_path, "b.jpg", seed=9), caption="Cricket stadium night match")
    assert [e.id for e in lib.suggest(None, "m", "debate in the parliament today", 2)] == [a.id]
    client = FakeClient({"pick_photos": lambda kw: {"ids": [b.id, "bogus"]}})
    assert [e.id for e in lib.suggest(client, "m", "debate in the parliament", 2)] == [b.id]    # AI choice, junk ids dropped
    assert lib.suggest(None, "m", "anything", 0) == []


def test_autotag_only_describes_visible_content_and_skips_captioned(tmp_path):
    lib = Library(tmp_path / "lib")
    a, _ = lib.add_file(photo(tmp_path, "a.jpg", seed=1))
    b, _ = lib.add_file(photo(tmp_path, "b.jpg", seed=9), caption="Mine")
    seen = {}

    def answer(kw):
        seen["system"] = kw["system"]
        return {"photos": [{"id": a.id, "caption": "A crowd outside a white building", "tags": ["Crowd", "building"]}]}

    assert lib.autotag(FakeClient({"describe_photos": answer}), "m") == 1
    assert "NEVER identify" in seen["system"]
    got = lib.get(a.id)
    assert got.caption.startswith("A crowd") and got.tags == ["crowd", "building"] and got.ai_tagged
    assert lib.get(b.id).caption == "Mine"


def run_video(cfg, client, **kw):
    store = Store(Path(cfg["youtube"]["output_dir"]))
    return produce(cfg, manual_topic("संसद में बहस", "संसद में आज बहस हुई।"), "short", client, MockTTS(), store,
                   preset="ultrafast", log=lambda *_: None, **kw), store


def arrange(kw):
    ids = [ln.split("asset_id=")[1].split()[0] for b in kw["messages"][0]["content"] if b["type"] == "text"
           for ln in b["text"].split("\n") if ln.startswith("asset_id=")]
    return {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)]}]} for i in range(3)]}


def test_uploads_flow_into_library_and_can_be_reused_later(cfg, tmp_path):
    def describe(kw):
        ids = [b["text"].split("id=")[1] for b in kw["messages"][0]["content"] if b["type"] == "text" and "photo id=" in b["text"]]
        return {"photos": [{"id": i, "caption": "parliament crowd", "tags": ["politics"]} for i in ids]}

    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange, "describe_photos": describe})
    lib = Library(Path(cfg["images"]["library_dir"]))
    assert lib.all() == []
    meta, _ = run_video(cfg, client)                       # inbox photos (3 valid) are saved to the library
    assert len(lib.all()) == 3 and all(e.used == 1 for e in lib.all())
    # a second video, with NO photos supplied this time, still gets pictures from the inventory
    for f in Path(cfg["images"]["inbox_dir"]).glob("*"):
        f.unlink()
    meta2, _ = run_video(cfg, FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange,
                                          "pick_photos": lambda kw: {"ids": [e.id for e in lib.all()]}}))
    run2 = Path(meta2["video"]).parent
    assert list((run2 / "images").glob("lib_*.jpg"))
    assert all(e.used == 2 for e in lib.all())


def test_explicitly_selected_library_photos_are_used_and_credited(cfg):
    lib = Library(Path(cfg["images"]["library_dir"]))
    for f in Path(cfg["images"]["inbox_dir"]).glob("*"):
        f.unlink()
    e, _ = lib.add_file(Path(__file__).parent / "_pic.jpg") if False else (None, None)
    pic = Path(cfg["images"]["library_dir"]).parent / "pic.jpg"
    make_photo(pic, 1400, 900, 5)
    e, _ = lib.add_file(pic, caption="Sansad Bhavan", credit="Photo: PIB")
    cfg.data["images"]["use_library"] = False
    meta, _ = run_video(cfg, FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange}), library_ids=[e.id])
    assert "Photo: PIB" in meta["description"]
    plan = json.loads((Path(meta["video"]).parent / "plan.json").read_text())
    assert plan and (Path(meta["video"]).parent / "images" / f"lib_{e.id}.jpg").exists()
