"""`caption` command: animated captions for an already-dubbed video, no paid APIs.

Work files go in <out>/<video>/ so a re-run is cheap:
  words.whisper.json  word timings from speech recognition (computed once)
  transcript.txt      editable transcript, one sentence per line. Edit it (or
                      pass --script) and re-run: the corrected wording is
                      aligned onto the recognised timings.
  captions.srt        phrase-level sidecar captions
  captions.ass        styled word-by-word captions that get burned in
The finished video is <out>/<video>.captioned.mp4.
"""

from __future__ import annotations

from pathlib import Path

from .captions import CaptionStyle, to_srt
from .pipeline import log, slugify
from .render import burn_captions, video_duration, video_size
from .wordcaps import (
    DEFAULT_MODEL, _norm, _tokens, align, load_words, save_words, to_page_cues, to_word_ass,
    transcribe, words_to_text,
)


def caption_video(
    video: Path,
    out_dir: Path,
    style: CaptionStyle,
    *,
    language: str | None = None,
    script: Path | None = None,
    model: str = DEFAULT_MODEL,
    retranscribe: bool = False,
    crf: int = 18,
    preset: str = "medium",
) -> Path:
    work = out_dir / slugify(video.stem)
    work.mkdir(parents=True, exist_ok=True)
    whisper_json, transcript = work / "words.whisper.json", work / "transcript.txt"

    if retranscribe or not whisper_json.exists():
        log(f"{video.name}: transcribing ({model}) ...")
        prompt = script.read_text(encoding="utf-8")[:800] if script else None
        words, lang = transcribe(video, language, model, prompt)
        if not words:
            raise RuntimeError(f"No speech found in {video}")
        save_words(words, whisper_json)
        transcript.write_text(words_to_text(words), encoding="utf-8")
        log(f"{video.name}: {len(words)} words, language '{lang}'")
    recognised = load_words(whisper_json)

    if script:
        transcript.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    reference = transcript.read_text(encoding="utf-8")
    if [_norm(t) for t in _tokens(reference)] == [_norm(w.text) for w in recognised] and \
            reference.split() == [w.text for w in recognised]:
        words = recognised
    else:
        log(f"{video.name}: applying corrected transcript")
        words = align(reference, recognised)

    (work / "captions.srt").write_text(to_srt(to_page_cues(words, style)), encoding="utf-8")
    w, h = video_size(video)
    ass = work / "captions.ass"
    ass.write_text(
        to_word_ass(words, style, w, h, language or "", video_duration(video)), encoding="utf-8"
    )
    final = out_dir / f"{slugify(video.stem)}.captioned.mp4"
    log(f"{video.name}: burning captions -> {final}")
    burn_captions(video, ass, final, crf=crf, preset=preset)
    return final
