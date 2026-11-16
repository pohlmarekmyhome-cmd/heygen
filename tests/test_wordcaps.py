from heygen_dub.captions import CaptionStyle
from heygen_dub.wordcaps import Word, align, group_words, to_word_ass, words_to_text


def W(*items):
    return [Word(t, s, e) for t, s, e in items]


REC = W(("Hi", 0.0, 0.2), ("everyone,", 0.2, 0.8), ("get", 1.0, 1.2), ("20", 1.2, 1.5),
        ("%", 1.5, 1.7), ("off", 1.7, 2.0), ("Skin", 2.0, 2.3), ("Glo.", 2.3, 2.6))


def test_align_identical_text_keeps_timings():
    assert align(" ".join(w.text for w in REC), REC) == REC


def test_align_replacements_and_insertions_stay_in_order():
    out = align("Hi everyone, get twenty percent off SkinGlow today.", REC)
    assert [w.text for w in out] == ["Hi", "everyone,", "get", "twenty", "percent", "off", "SkinGlow", "today."]
    assert out[0].start == 0.0 and out[2].start == 1.0 and out[5].start == 1.7
    # replaced "20 %" spans exactly the recognised 1.2 - 1.7
    assert out[3].start == 1.2 and abs(out[4].end - 1.7) < 1e-9
    assert out[6].start == 2.0
    for a, b in zip(out, out[1:]):
        assert a.start <= b.start and a.end <= b.end + 1e-9


def test_grouping_breaks_on_sentence_end_pause_and_size():
    words = W(("One", 0, .2), ("two.", .2, .4), ("Three", .5, .7), ("four", .7, .9),
              ("five", .9, 1.1), ("six", 1.1, 1.3), ("seven", 1.3, 1.5), ("after", 3.0, 3.2), ("pause", 3.2, 3.4))
    pages = group_words(words, max_words=4, max_chars=40)
    assert [[w.text for w in p] for p in pages] == [
        ["One", "two."], ["Three", "four", "five", "six"], ["seven"], ["after", "pause"]]


def test_word_ass_highlights_each_word_once_and_strips_punctuation():
    style = CaptionStyle(max_words=3, highlight_color="#00FF00", uppercase=True)
    ass = to_word_ass(REC, style, 1080, 1920, video_duration=2.6)
    events = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(events) == len(REC)
    assert all(e.count("&H0000FF00") == 1 for e in events)  # exactly one highlighted word
    assert "EVERYONE," not in ass and "EVERYONE" in ass
    assert "0:00:02.60" in events[-1]  # clamped to video end


def test_words_to_text_one_sentence_per_line():
    assert words_to_text(W(("A", 0, 1), ("b.", 1, 2), ("C", 2, 3))) == "A b.\nC\n"
