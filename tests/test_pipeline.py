"""End-to-end test against a fake HeyGen server (no network, no credits)."""

import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from heygen_dub.config import load_config
from heygen_dub.heygen import HeyGenClient
from heygen_dub.pipeline import run
from heygen_dub.render import video_size

SRT = """1
00:00:00,200 --> 00:00:03,800
This is a translated sentence that is long enough to be split into chunks
"""


def make_video(path: Path, color: str = "blue") -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color={color}:s=360x640:d=4:r=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


class FakeHeyGen:
    def __init__(self, files: Path):
        self.files = files
        self.requests: list[tuple[str, str, object]] = []
        self.polls: dict[str, int] = {}
        self.fail_language = None
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("X-Api-Key") != "test-key":
                    return self._send(401, {"error": {"message": "bad key"}})
                if self.path == "/v1/asset":
                    fake.requests.append(("POST", self.path, len(body)))
                    return self._send(200, {"code": 100, "data": {"id": "asset-1", "url": "x"}})
                if self.path == "/v3/video-translations":
                    req = json.loads(body)
                    fake.requests.append(("POST", self.path, req))
                    ids = [f"tr-{i}-{len(fake.requests)}" for i, _ in enumerate(req["output_languages"])]
                    fake.langs = dict(zip(ids, req["output_languages"]))
                    fake.all_langs = {**getattr(fake, "all_langs", {}), **fake.langs}
                    return self._send(200, {"error": None, "data": {"video_translation_ids": ids}})
                self._send(404, {"error": "nope"})

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                base = f"http://127.0.0.1:{self.server.server_port}"
                if self.path.startswith("/files/"):
                    return self._send(200, (fake.files / self.path[7:]).read_bytes(), "application/octet-stream")
                m = re.fullmatch(r"/v3/video-translations/([^/?]+)(/caption\?format=srt)?", self.path)
                if m:
                    tid = m.group(1)
                    lang = fake.all_langs[tid]
                    if m.group(2):
                        return self._send(200, {"data": {"caption_url": f"{base}/files/captions.srt"}})
                    n = fake.polls[tid] = fake.polls.get(tid, 0) + 1
                    if lang == fake.fail_language:
                        return self._send(200, {"data": {"status": "failed", "failure_message": "boom"}})
                    if n < 2:
                        return self._send(200, {"data": {"status": "running"}})
                    return self._send(200, {"data": {"status": "completed", "output_language": lang,
                                                     "video_url": f"{base}/files/dubbed.mp4"}})
                self._send(404, {"error": "nope"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def env(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    make_video(files / "dubbed.mp4", "blue")
    (files / "captions.srt").write_text(SRT)
    src = tmp_path / "Moje Video.mp4"
    make_video(src, "red")
    fake = FakeHeyGen(files)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f"""
languages = ["English (United States)", "German (Germany)"]
[heygen]
poll_interval_seconds = 0
[heygen.request_options]
speaker_num = 1
[output]
dir = "{tmp_path / 'out'}"
preset = "ultrafast"
[captions]
max_chars_per_line = 20
""")
    cfg = load_config(cfg_path)
    client = HeyGenClient("test-key", base_url=fake.url, upload_url=fake.url, max_retries=0)
    yield cfg, client, fake, src
    fake.server.shutdown()


def frame_at(video: Path, t: float) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True,
    ).stdout


def test_full_run_and_resume(env):
    cfg, client, fake, src = env
    assert run(str(src), cfg, client) is True

    job = cfg.output_dir / "moje-video"
    create = [r for r in fake.requests if r[1] == "/v3/video-translations"]
    assert len(create) == 1
    body = create[0][2]
    assert body["video"] == {"type": "asset_id", "asset_id": "asset-1"}
    assert body["output_languages"] == ["English (United States)", "German (Germany)"]
    assert body["mode"] == "precision" and body["speaker_num"] == 1

    for slug in ("english-united-states", "german-germany"):
        final = job / f"moje-video.{slug}.mp4"
        assert final.exists()
        assert video_size(final) == (360, 640)
        srt = (job / slug / "captions.srt").read_text()
        assert all(len(l) <= 20 for l in srt.splitlines() if l and not l[0].isdigit())
        # Captions visible at t=1s, frame otherwise identical to the dubbed input.
        assert frame_at(final, 1.0) != frame_at(job / slug / "dubbed.mp4", 1.0)

    state = json.loads((job / "state.json").read_text())
    assert {e["status"] for e in state["translations"].values()} == {"done"}

    # Re-running must not upload or submit again.
    n = len(fake.requests)
    assert run(str(src), cfg, client) is True
    assert not [r for r in fake.requests[n:] if r[0] == "POST"]


def test_failed_language_reported_and_retryable(env):
    cfg, client, fake, src = env
    fake.fail_language = "German (Germany)"
    assert run(str(src), cfg, client) is False
    state = json.loads((cfg.output_dir / "moje-video" / "state.json").read_text())
    assert state["translations"]["German (Germany)"]["status"] == "failed"
    assert state["translations"]["English (United States)"]["status"] == "done"

    fake.fail_language = None
    assert run(str(src), cfg, client, retry_failed=True) is True
    creates = [r[2] for r in fake.requests if r[1] == "/v3/video-translations"]
    assert creates[-1]["output_languages"] == ["German (Germany)"]
