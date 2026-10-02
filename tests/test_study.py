from newschannel import study

SAMPLE = """0:000 secondsदोस्तों क्या मंत्री वाकई घबराए हुए हैं? अंदर की खबर आपको बताने जा रहा हूं
0:077 secondsये बात सूत्रों के हवाले से कही जा रही है कि 34 हजार लोग आएंगे
1:081 minute, 8 secondsसरकार ने सरेंडर कर दिया, आप कह सकते हैं घुटनों पर आ गई
10:5510 minutes, 55 secondsतो फिर सब्सक्राइब करिए और कमेंट करिए
"""


def test_parses_youtube_copied_timestamps():
    segs = study.parse(SAMPLE)
    assert [s.t for s in segs] == [0, 7, 68, 655] and segs[0].text.startswith("दोस्तों")
    plain = study.parse("0:05 hello there\n0:12 second line\nwrapped text")
    assert [s.t for s in plain] == [5, 12] and plain[1].text.endswith("wrapped text")


def test_flags_risky_techniques_separately_from_delivery_techniques():
    a = study.analyze(SAMPLE)
    assert {"direct_address", "cta"} <= set(a["found"])
    assert {"insider_claim", "mind_reading", "hearsay", "loaded_words", "hedge_you_can_say"} <= set(a["found"])
    assert a["found"]["loaded_words"] == [68]                       # counted once per line, not once per pattern
    assert ("0:07", "34") in a["numbers"]
    text = study.report(SAMPLE)
    assert "do NOT copy" in text and "Confirmed / Reported / Alleged" in text


def test_rejects_text_without_timestamps():
    import pytest
    with pytest.raises(ValueError):
        study.analyze("just some words with no times")
