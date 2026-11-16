"""Command line entry point: `python -m heygen_dub ...`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import load_config
from .heygen import HeyGenClient, HeyGenError
from .pipeline import Job, log, run
from .render import require_ffmpeg

DEFAULT_CONFIG = Path("config.toml")


def _client(cfg) -> HeyGenClient:
    return HeyGenClient(os.environ.get(cfg.api_key_env, ""))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="heygen_dub",
        description="Translate videos with HeyGen (lip-sync) and burn in captions.",
    )
    p.add_argument("-c", "--config", type=Path, default=None, help="TOML config (default: ./config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="translate + caption one or more videos (resumable)")
    r.add_argument("videos", nargs="+", help="local video files and/or public video URLs")
    r.add_argument("-l", "--languages", nargs="+", help="override target languages from config")
    r.add_argument("--mode", choices=["precision", "speed"], help="override heygen.mode")
    r.add_argument("--retry-failed", action="store_true", help="resubmit languages that failed on HeyGen")
    r.add_argument("--dry-run", action="store_true", help="show what would be submitted, call nothing")

    b = sub.add_parser("burn", help="re-burn captions after editing <job>/<language>/captions.srt")
    b.add_argument("job_dir", type=Path, help="e.g. output/my-video")
    b.add_argument("-l", "--languages", nargs="+", help="only these languages")

    sub.add_parser("languages", help="list HeyGen's supported target language names")

    c = sub.add_parser("caption", help="add animated word-by-word captions to dubbed videos (local, free)")
    c.add_argument("videos", nargs="+", type=Path, help="dubbed video files")
    c.add_argument("-o", "--out", type=Path, default=Path("output/captioned"), help="output folder")
    c.add_argument("--language", help="spoken language code (en, de, cs, ...); default: auto-detect")
    c.add_argument("--script", type=Path, help="correct transcript to use for the caption wording")
    c.add_argument("--model", default="large-v3-turbo", help="faster-whisper model name")
    c.add_argument("--retranscribe", action="store_true", help="ignore cached word timings")

    args = p.parse_args(argv)
    cfg_path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    cfg = load_config(cfg_path)

    try:
        if args.cmd == "languages":
            for name in _client(cfg).list_languages():
                print(name)
            return 0

        require_ffmpeg()

        if args.cmd == "caption":
            from .captioner import caption_video

            ok = True
            for video in args.videos:
                try:
                    caption_video(
                        video, args.out, cfg.captions, language=args.language, script=args.script,
                        model=args.model, retranscribe=args.retranscribe, crf=cfg.crf, preset=cfg.preset,
                    )
                except (RuntimeError, OSError) as e:
                    log(f"ERROR captioning {video}: {e}")
                    ok = False
            return 0 if ok else 1

        if args.cmd == "burn":
            job = Job(str(args.job_dir), cfg, job_dir=args.job_dir)
            job.name = args.job_dir.name
            langs = args.languages or [
                l for l, e in job.state["translations"].items() if e.get("status") == "done"
            ]
            for lang in langs:
                job.render_language(lang)
            return 0

        if args.languages:
            cfg.languages = args.languages
        if args.mode:
            cfg.mode = args.mode
        if not cfg.languages:
            p.error("no target languages: set `languages` in config.toml or pass --languages")

        if args.dry_run:
            for v in args.videos:
                print(f"{v}\n  mode: {cfg.mode}\n  languages: {cfg.languages}\n"
                      f"  extra request options: {cfg.request_options}\n  output: {cfg.output_dir}")
            return 0

        client = _client(cfg)
        ok = True
        for video in args.videos:
            try:
                ok &= run(video, cfg, client, retry_failed=args.retry_failed)
            except (HeyGenError, FileNotFoundError) as e:
                log(f"ERROR processing {video}: {e}")
                ok = False
        return 0 if ok else 1
    except HeyGenError as e:
        print(f"HeyGen error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; re-run the same command to resume.", file=sys.stderr)
        return 130
