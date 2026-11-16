"""Word-by-word animated captions (the "Captions app" look), fully local.

1. Transcribe the (dubbed) video with faster-whisper to get per-word timings.
2. Optionally align a corrected transcript onto those timings, so wording is
   exact even where speech recognition misheard.
3. Group words into short phrases and render an ASS file in which the word
   being spoken is highlighted.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .captions import CaptionStyle, Cue, _ass_color, _ass_escape, ass_header, dialogue

DEFAULT_MODEL = "large-v3-turbo"
SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
CLAUSE_END = (",", ";", ":", "，", "、")


@dataclass
class Word:
    text: str
    start: float
    end: float


# ------------------------------------------------------------- transcription
def transcribe(video: Path, language: str | None = None, model_name: str = DEFAULT_MODEL,
               prompt: str | None = None) -> tuple[list[Word], str]:
    """Return (words, detected_language)."""
    from faster_whisper import WhisperModel  # heavy import, only when needed

    import numpy as np

    # Decode with ffmpeg to 16 kHz mono float32 (avoids PyAV version issues).
    pcm = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    if True:
        model = WhisperModel(model_name, device="auto", compute_type="int8")
        segments, info = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        words = [
            Word(w.word.strip(), round(w.start, 3), round(w.end, 3))
            for seg in segments
            for w in (seg.words or [])
            if w.word.strip()
        ]
    return words, info.language


def save_words(words: list[Word], path: Path) -> None:
    path.write_text(json.dumps([asdict(w) for w in words], ensure_ascii=False, indent=1), encoding="utf-8")


def load_words(path: Path) -> list[Word]:
    return [Word(**w) for w in json.loads(path.read_text(encoding="utf-8"))]


def words_to_text(words: list[Word]) -> str:
    """Plain transcript, one sentence per line (easy to proofread/edit)."""
    lines, cur = [], []
    for w in words:
        cur.append(w.text)
        if w.text.endswith(SENTENCE_END):
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- alignment
def _norm(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lower())


def _tokens(text: str) -> list[str]:
    toks = text.split()
    if len(toks) <= 1 and len(text.strip()) > 1 and not text.isascii():
        # Chinese/Japanese/Thai: no spaces, so align character by character.
        toks = [c for c in text if not c.isspace()]
    return toks


def align(reference: str, words: list[Word]) -> list[Word]:
    """Put the reference wording onto the recognised words' timings.

    Matching words take their timing directly. Replaced words share the time
    span of the recognised words they replace. Inserted words are fitted into
    the gap between their neighbours.
    """
    ref = _tokens(reference)
    if not words:
        return []
    sm = SequenceMatcher(a=[_norm(w.text) for w in words], b=[_norm(t) for t in ref], autojunk=False)
    times: list[tuple[float, float] | None] = [None] * len(ref)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[j1 + k] = (words[i1 + k].start, words[i1 + k].end)
        elif tag == "replace":
            _spread(times, ref, j1, j2, words[i1].start, words[i2 - 1].end)
    # Fill inserted words between known neighbours.
    j = 0
    while j < len(ref):
        if times[j] is not None:
            j += 1
            continue
        k = j
        while k < len(ref) and times[k] is None:
            k += 1
        lo = times[j - 1][1] if j > 0 else words[0].start
        hi = times[k][0] if k < len(ref) else words[-1].end
        if hi <= lo:
            hi = lo + 0.15 * (k - j)
        _spread(times, ref, j, k, lo, hi)
        j = k
    return [Word(t, s, e) for t, (s, e) in zip(ref, times)]


def _spread(times, ref, j1, j2, start, end):
    total = sum(max(len(t), 1) for t in ref[j1:j2]) or 1
    t = start
    for j in range(j1, j2):
        d = (end - start) * max(len(ref[j]), 1) / total
        times[j] = (t, t + d)
        t += d


# ------------------------------------------------------------------ grouping
def group_words(words: list[Word], max_words: int, max_chars: int, max_gap: float = 0.6) -> list[list[Word]]:
    """Split words into short on-screen phrases at punctuation, pauses and size limits."""
    pages: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur:
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            prev = cur[-1]
            if (
                len(cur) >= max_words
                or chars > max_chars
                or w.start - prev.end > max_gap
                or prev.text.endswith(SENTENCE_END)
                or (prev.text.endswith(CLAUSE_END) and len(cur) >= 2)
            ):
                pages.append(cur)
                cur = []
        cur.append(w)
    if cur:
        pages.append(cur)
    return pages


def _display(text: str, style: CaptionStyle) -> str:
    if style.strip_punctuation:
        text = re.sub(r"[.,;:…。，、]+$", "", text)
        text = text.strip("\"'“”„«»")
    return text.upper() if style.uppercase else text


def to_word_ass(words: list[Word], style: CaptionStyle, width: int, height: int, language: str = "",
                video_duration: float | None = None) -> str:
    """ASS with one event per spoken word; the current word is highlighted."""
    pages = group_words(words, style.max_words, style.max_chars_per_line * style.max_lines)
    hl = _ass_color(style.highlight_color)
    base = _ass_color(style.text_color)
    events = []
    for p, page in enumerate(pages):
        page_start = page[0].start
        nxt = pages[p + 1][0].start if p + 1 < len(pages) else None
        # Hold the phrase until the next one if the gap is short (avoids flicker).
        page_end = page[-1].end + 0.25
        if nxt is not None and nxt - page[-1].end < 0.5:
            page_end = nxt
        elif nxt is not None:
            page_end = min(page_end, nxt)
        if video_duration:
            page_end = min(page_end, video_duration)
        texts = [_ass_escape(_display(w.text, style)) for w in page]
        sep = "" if len(page) > 1 and all(len(w.text) == 1 and not w.text.isascii() for w in page) else " "
        for i, w in enumerate(page):
            start = page_start if i == 0 else w.start
            end = page[i + 1].start if i + 1 < len(page) else page_end
            if end <= start:
                continue
            parts = [f"{{\\c{hl}}}{t}{{\\c{base}}}" if k == i else t for k, t in enumerate(texts)]
            prefix = ""
            if style.pop and i == 0:
                prefix = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}"
            events.append(dialogue(start, end, prefix + sep.join(parts)))
    return ass_header(style, width, height, language) + "\n".join(events) + "\n"


def to_page_cues(words: list[Word], style: CaptionStyle) -> list[Cue]:
    """Phrase-level cues (for an .srt sidecar some platforms accept)."""
    pages = group_words(words, style.max_words, style.max_chars_per_line * style.max_lines)
    return [Cue(p[0].start, p[-1].end, " ".join(w.text for w in p)) for p in pages]
