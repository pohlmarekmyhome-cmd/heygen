"""End-to-end job: upload -> translate (lip-sync) -> download -> caption -> burn.

Every step records its result in <output>/<job>/state.json, so re-running the
same command resumes where it stopped and never pays for a translation twice.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

from .captions import parse_srt, rechunk, to_ass, to_srt
from .config import Config
from .heygen import HeyGenClient, HeyGenError, download
from .render import burn_captions, video_size


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "video"


def is_url(s: str) -> bool:
    return urllib.parse.urlparse(s).scheme in ("http", "https")


def job_name(source: str) -> str:
    name = Path(urllib.parse.urlparse(source).path).stem if is_url(source) else Path(source).stem
    return slugify(name)


class Job:
    def __init__(self, source: str, cfg: Config, job_dir: Path | None = None):
        self.source = source
        self.cfg = cfg
        self.name = job_name(source)
        self.dir = job_dir or cfg.output_dir / self.name
        self.state_path = self.dir / "state.json"
        self.state = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {"source": source, "asset": None, "translations": {}}
        )

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))
        tmp.replace(self.state_path)

    def lang_dir(self, lang: str) -> Path:
        return self.dir / slugify(lang)

    def final_path(self, lang: str) -> Path:
        return self.dir / f"{self.name}.{slugify(lang)}.mp4"

    # ------------------------------------------------------------- source
    def resolve_source(self, client: HeyGenClient) -> dict:
        if is_url(self.source):
            return {"url": self.source}
        if self.state.get("asset"):
            return {"asset_id": self.state["asset"]["id"]}
        path = Path(self.source)
        if not path.exists():
            raise FileNotFoundError(path)
        log(f"Uploading {path.name} to HeyGen ...")
        asset = client.upload_asset(path)
        self.state["asset"] = asset
        self.save()
        log(f"Uploaded as asset {asset['id']}")
        return {"asset_id": asset["id"]}

    # ------------------------------------------------------------- submit
    def submit(self, client: HeyGenClient, languages: list[str], retry_failed: bool) -> None:
        tr = self.state["translations"]
        pending = [
            l for l in languages
            if l not in tr or (retry_failed and tr[l].get("status") == "failed")
        ]
        if not pending:
            return
        source = self.resolve_source(client)
        log(f"Requesting {self.cfg.mode} translation into: {', '.join(pending)}")
        ids = client.create_translations(
            source,
            pending,
            mode=self.cfg.mode,
            title=self.name,
            options=self.cfg.request_options,
        )
        for lang, tid in ids.items():
            tr[lang] = {"id": tid, "status": "pending", "submitted_at": time.time()}
            log(f"  {lang}: translation id {tid}")
        self.save()

    # -------------------------------------------------------------- poll
    def wait_and_process(self, client: HeyGenClient, languages: list[str]) -> None:
        tr = self.state["translations"]
        deadline = time.time() + self.cfg.timeout_minutes * 60
        while True:
            open_langs = [l for l in languages if l in tr and tr[l]["status"] not in ("done", "failed")]
            if not open_langs:
                return
            for lang in open_langs:
                entry = tr[lang]
                if entry["status"] == "translated":
                    # Finished on HeyGen earlier; a local step failed. Retry it.
                    self.finish_language(client, lang)
                    continue
                t = client.get_translation(entry["id"])
                if t.language and slugify(t.language) != slugify(lang):
                    log(f"  WARNING: {entry['id']} reports language {t.language!r}, expected {lang!r}")
                if t.failed:
                    entry.update(status="failed", error=t.error)
                    log(f"  {lang}: FAILED - {t.error}")
                elif t.done and t.video_url:
                    entry["status"] = "translated"
                    log(f"  {lang}: translation finished")
                    self.save()
                    self.finish_language(client, lang)
                elif entry.get("remote_status") != t.status:
                    log(f"  {lang}: {t.status}")
                entry["remote_status"] = t.status
                self.save()
            if any(tr[l]["status"] not in ("done", "failed") for l in languages if l in tr):
                if time.time() > deadline:
                    log("Timed out waiting for HeyGen; re-run the same command later to resume.")
                    return
                time.sleep(self.cfg.poll_interval_seconds)

    def finish_language(self, client: HeyGenClient, lang: str) -> None:
        entry = self.state["translations"][lang]
        d = self.lang_dir(lang)
        d.mkdir(parents=True, exist_ok=True)
        dubbed, raw_srt = d / "dubbed.mp4", d / "captions.heygen.srt"
        try:
            t = client.get_translation(entry["id"])  # fresh presigned URLs
            if not dubbed.exists():
                log(f"  {lang}: downloading dubbed video")
                download(t.video_url, dubbed)
            if not raw_srt.exists():
                log(f"  {lang}: fetching captions")
                raw_srt.write_text(client.get_caption_srt(t), encoding="utf-8")
            self.render_language(lang)
            entry["status"] = "done"
        except (HeyGenError, OSError, RuntimeError) as e:
            entry["error"] = str(e)
            log(f"  {lang}: local processing failed ({e}); will retry on next run")
        self.save()

    # ------------------------------------------------------------ render
    def render_language(self, lang: str) -> Path:
        """Build captions.srt (if not hand-edited) and burn it into the final video."""
        d = self.lang_dir(lang)
        style = self.cfg.captions
        dubbed, raw_srt, srt = d / "dubbed.mp4", d / "captions.heygen.srt", d / "captions.srt"
        if not srt.exists():
            cues = rechunk(parse_srt(raw_srt.read_text(encoding="utf-8")), style.max_chars_per_line, style.max_lines)
            if not cues:
                raise RuntimeError(f"HeyGen returned no caption cues for {lang}")
            srt.write_text(to_srt(cues), encoding="utf-8")
        final = self.final_path(lang)
        if not self.cfg.burn_captions:
            return dubbed
        w, h = video_size(dubbed)
        ass = d / "captions.ass"
        ass.write_text(to_ass(parse_srt(srt.read_text(encoding="utf-8")), style, w, h, lang), encoding="utf-8")
        log(f"  {lang}: burning captions -> {final}")
        burn_captions(dubbed, ass, final, crf=self.cfg.crf, preset=self.cfg.preset)
        return final

    def summary(self, languages: list[str]) -> bool:
        ok = True
        log(f"Summary for {self.name}:")
        for lang in languages:
            e = self.state["translations"].get(lang, {})
            status = e.get("status", "not submitted")
            where = self.final_path(lang) if status == "done" and self.cfg.burn_captions else ""
            print(f"    {lang:32} {status:12} {where}")
            ok &= status == "done"
        return ok


def run(source: str, cfg: Config, client: HeyGenClient, *, retry_failed: bool = False) -> bool:
    job = Job(source, cfg)
    log(f"Job '{job.name}' -> {job.dir}")
    job.save()
    job.submit(client, cfg.languages, retry_failed)
    job.wait_and_process(client, cfg.languages)
    return job.summary(cfg.languages)
