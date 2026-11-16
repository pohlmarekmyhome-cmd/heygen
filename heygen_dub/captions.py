"""Caption handling: parse SRT, re-chunk for social video, and emit styled ASS."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Cue:
    start: float  # seconds
    end: float
    text: str


@dataclass
class CaptionStyle:
    font: str = "Inter"
    font_size: float = 0.04  # fraction of video height
    bold: bool = True
    uppercase: bool = False
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: float = 0.004  # fraction of video height
    shadow: float = 0.0
    box: bool = False  # opaque box behind text instead of outline
    box_color: str = "#000000"
    box_opacity: float = 0.6
    margin_bottom: float = 0.18  # fraction of height; keeps text above TikTok/Reels UI
    margin_sides: float = 0.08  # fraction of width
    max_chars_per_line: int = 22
    max_lines: int = 2
    fonts: dict[str, str] = field(default_factory=dict)  # per-language font overrides
    # Word-by-word ("Captions app" style) options, used by the `caption` command.
    highlight_color: str = "#FFE14D"  # colour of the word being spoken
    max_words: int = 4  # words shown at once
    pop: bool = True  # small zoom-in when a new group of words appears
    strip_punctuation: bool = True  # drop commas/periods, keep ? and !


# ------------------------------------------------------------------------ SRT
_TS = r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(rf"{_TS}\s*-->\s*{_TS}")


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _TIMING.search(line)
            if m:
                g = m.groups()
                body = " ".join(l.strip() for l in lines[i + 1 :] if l.strip())
                body = re.sub(r"<[^>]+>", "", body)  # drop <i>, <font> etc.
                if body:
                    cues.append(Cue(_secs(*g[:4]), _secs(*g[4:]), body))
                break
    return cues


def _fmt_srt_ts(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(cues: list[Cue]) -> str:
    return "".join(
        f"{i}\n{_fmt_srt_ts(c.start)} --> {_fmt_srt_ts(c.end)}\n{c.text}\n\n"
        for i, c in enumerate(cues, 1)
    )


# ------------------------------------------------------------- re-chunking
def _is_spaceless(text: str) -> bool:
    """True for scripts written without spaces between words (Chinese, Japanese, Thai).

    Korean uses spaces between words, so Hangul is wrapped like Latin text.
    """
    chars = text.replace(" ", "")
    spaceless = sum(
        1
        for ch in chars
        if any(k in unicodedata.name(ch, "") for k in ("CJK", "HIRAGANA", "KATAKANA", "THAI"))
    )
    return bool(chars) and spaceless > len(chars) / 2


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap into lines of at most `width` characters (words kept whole)."""
    if _is_spaceless(text):
        compact = text.replace(" ", "")
        return [compact[i : i + width] for i in range(0, len(compact), width)]
    lines: list[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}" if cur else word
        if len(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def rechunk(cues: list[Cue], max_chars: int, max_lines: int) -> list[Cue]:
    """Split long cues into short on-screen chunks, dividing time by text length."""
    out: list[Cue] = []
    for cue in cues:
        lines = _wrap(cue.text, max_chars)
        chunks = ["\n".join(lines[i : i + max_lines]) for i in range(0, len(lines), max_lines)]
        if len(chunks) == 1:
            out.append(Cue(cue.start, cue.end, chunks[0]))
            continue
        total = sum(len(c) for c in chunks)
        t = cue.start
        for i, chunk in enumerate(chunks):
            end = cue.end if i == len(chunks) - 1 else t + (cue.end - cue.start) * len(chunk) / total
            out.append(Cue(t, end, chunk))
            t = end
    return out


# ---------------------------------------------------------------------- ASS
def _ass_color(hex_color: str, opacity: float = 1.0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _fmt_ass_ts(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def ass_header(style: CaptionStyle, width: int, height: int, language: str = "") -> str:
    """ASS script header with one 'Default' style sized in real video pixels."""
    font = style.fonts.get(language, style.font)
    size = round(style.font_size * height)
    if style.box:
        border_style, outline_px, back = 3, max(2, round(size * 0.25)), _ass_color(style.box_color, style.box_opacity)
        outline_col = back
    else:
        border_style, outline_px = 1, round(style.outline * height)
        outline_col, back = _ass_color(style.outline_color), _ass_color("#000000", 0.5)
    margin_v = round(style.margin_bottom * height)
    margin_lr = round(style.margin_sides * width)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{_ass_color(style.text_color)},&H000000FF,{outline_col},{back},{-1 if style.bold else 0},0,0,0,100,100,0,0,{border_style},{outline_px},{round(style.shadow * height)},2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{_fmt_ass_ts(start)},{_fmt_ass_ts(end)},Default,,0,0,0,,{text}"


def to_ass(cues: list[Cue], style: CaptionStyle, width: int, height: int, language: str = "") -> str:
    """Render line-level cues as an ASS script sized in real video pixels."""
    events = [
        dialogue(c.start, c.end, _ass_escape(c.text.upper() if style.uppercase else c.text))
        for c in cues
    ]
    return ass_header(style, width, height, language) + "\n".join(events) + "\n"
