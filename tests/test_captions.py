from heygen_dub.captions import CaptionStyle, Cue, parse_srt, rechunk, to_ass, to_srt

SRT = """﻿1
00:00:00,000 --> 00:00:04,000
Hello everyone, today I want to show you <i>three</i> things about our new product

2
00:00:04,500 --> 00:00:06,000
Thanks!
"""


def test_parse_srt_strips_bom_and_tags():
    cues = parse_srt(SRT)
    assert len(cues) == 2
    assert cues[0].start == 0 and cues[0].end == 4
    assert "<i>" not in cues[0].text and "three" in cues[0].text
    assert cues[1].start == 4.5


def test_roundtrip():
    cues = parse_srt(SRT)
    assert parse_srt(to_srt(cues)) == cues


def test_rechunk_respects_limits_and_keeps_timing():
    out = rechunk(parse_srt(SRT), max_chars=20, max_lines=1)
    assert all(len(line) <= 20 for c in out for line in c.text.split("\n"))
    assert out[0].start == 0
    # long cue split into contiguous pieces ending at the original end
    first = [c for c in out if c.end <= 4.0]
    assert first[-1].end == 4.0
    for a, b in zip(first, first[1:]):
        assert a.end == b.start
    # words are not broken
    assert " ".join(c.text for c in first).split() == parse_srt(SRT)[0].text.split()


def test_rechunk_cjk_splits_by_character():
    out = rechunk([Cue(0, 3, "今日は新しい製品について三つのことを紹介します")], max_chars=8, max_lines=1)
    assert len(out) > 1
    assert all(len(c.text) <= 8 for c in out)
    assert "".join(c.text for c in out) == "今日は新しい製品について三つのことを紹介します"


def test_rechunk_korean_wraps_on_spaces():
    out = rechunk([Cue(0, 3, "안녕하세요 여러분 오늘은 새로운 제품을 소개합니다")], max_chars=12, max_lines=1)
    assert all(" " not in c.text.strip() or len(c.text) <= 12 for c in out)
    assert " ".join(c.text for c in out) == "안녕하세요 여러분 오늘은 새로운 제품을 소개합니다"


def test_ass_scales_with_video_and_uses_language_font():
    style = CaptionStyle(font_size=0.05, margin_bottom=0.2, fonts={"Japanese (Japan)": "Noto Sans CJK JP"})
    ass = to_ass([Cue(0, 1.5, "line one\nline {two}")], style, 1080, 1920, "Japanese (Japan)")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Default,Noto Sans CJK JP,96," in ass  # 0.05 * 1920
    assert ",384,1" in ass  # MarginV = 0.2 * 1920
    assert "line one\\Nline (two)" in ass
    assert "0:00:00.00,0:00:01.50" in ass
