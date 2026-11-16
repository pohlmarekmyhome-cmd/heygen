"""Thin HeyGen API client for video translation (stdlib only).

Endpoint paths and field names follow HeyGen's v3 video-translation API
(`/v3/video-translations`). Every request/response mapping lives in this one
file, so if HeyGen renames a field you only have to fix it here. Responses are
parsed leniently: several known spellings of each field are accepted.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.heygen.com"
DEFAULT_UPLOAD_URL = "https://upload.heygen.com"

# Status values HeyGen uses for a finished / failed translation.
DONE_STATUSES = {"completed", "success", "succeeded", "done"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}

# HeyGen's documented limit for direct asset uploads.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class HeyGenError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Translation:
    id: str
    status: str
    language: str | None = None
    video_url: str | None = None
    caption_url: str | None = None
    error: str | None = None
    raw: dict | None = None

    @property
    def done(self) -> bool:
        return self.status in DONE_STATUSES

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


class HeyGenClient:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        upload_url: str | None = None,
        max_retries: int = 5,
    ):
        if not api_key:
            raise HeyGenError("Missing HeyGen API key (set HEYGEN_API_KEY).")
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("HEYGEN_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.upload_url = (upload_url or os.environ.get("HEYGEN_UPLOAD_URL") or DEFAULT_UPLOAD_URL).rstrip("/")
        self.max_retries = max_retries

    # ------------------------------------------------------------------ HTTP
    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        data: bytes | None = None,
        content_type: str | None = None,
        raw: bool = False,
    ) -> Any:
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read()
                    if raw:
                        return body
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                retryable = e.code == 429 or e.code >= 500
                if retryable and attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = body
                raise HeyGenError(
                    f"{method} {url} -> HTTP {e.code}: {_error_message(parsed)}", e.code, parsed
                ) from None
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise HeyGenError(f"{method} {url} failed: {e.reason}") from None
        raise AssertionError("unreachable")

    def _api(self, method: str, path: str, **kw) -> Any:
        resp = self._request(method, f"{self.base_url}{path}", **kw)
        if isinstance(resp, dict) and resp.get("error") and not resp.get("data"):
            raise HeyGenError(f"{method} {path}: {_error_message(resp)}", body=resp)
        return resp.get("data", resp) if isinstance(resp, dict) else resp

    # ------------------------------------------------------------ endpoints
    def list_languages(self) -> list[str]:
        data = self._api("GET", "/v3/video-translations/languages")
        langs = data.get("languages", data) if isinstance(data, dict) else data
        return [l if isinstance(l, str) else _first(l, "name", "language", "label") for l in langs]

    def upload_asset(self, path: Path) -> dict:
        """Upload a local video and return {'id':..., 'url':...}."""
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise HeyGenError(
                f"{path.name} is {size / 1e6:.1f} MB; HeyGen direct uploads are limited to "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Re-encode it smaller or pass a public "
                "URL (e.g. a presigned S3/GCS link valid for at least 2 hours) instead."
            )
        ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
        resp = self._request(
            "POST", f"{self.upload_url}/v1/asset", data=path.read_bytes(), content_type=ctype
        )
        data = resp.get("data", resp)
        asset_id = _first(data, "id", "asset_id")
        if not asset_id:
            raise HeyGenError(f"Upload response had no asset id: {resp}", body=resp)
        return {"id": asset_id, "url": _first(data, "url")}

    def create_translations(
        self,
        source: dict,
        languages: list[str],
        *,
        mode: str = "precision",
        title: str | None = None,
        options: dict | None = None,
    ) -> dict[str, str]:
        """Submit one translation request for several languages.

        `source` is {"url": ...} or {"asset_id": ...}.
        Returns {language: translation_id}.
        """
        if "asset_id" in source:
            video = {"type": "asset_id", "asset_id": source["asset_id"]}
        else:
            video = {"type": "url", "url": source["url"]}
        body: dict[str, Any] = {"video": video, "output_languages": languages, "mode": mode}
        if title:
            body["title"] = title
        body.update(options or {})

        data = self._api("POST", "/v3/video-translations", json_body=body)
        return _map_ids_to_languages(data, languages)

    def get_translation(self, translation_id: str) -> Translation:
        data = self._api("GET", f"/v3/video-translations/{urllib.parse.quote(translation_id)}")
        return Translation(
            id=translation_id,
            status=str(_first(data, "status") or "unknown").lower(),
            language=_first(data, "output_language", "language", "target_language"),
            video_url=_first(data, "video_url", "url", "output_video_url"),
            caption_url=_first(data, "srt_caption_url", "caption_url", "srt_url"),
            error=_first(data, "failure_message", "error", "message"),
            raw=data,
        )

    def get_caption_srt(self, translation: Translation) -> str:
        """Return the translated SRT for a finished translation."""
        url = translation.caption_url
        if not url:
            data = self._api(
                "GET",
                f"/v3/video-translations/{urllib.parse.quote(translation.id)}/caption?format=srt",
            )
            if isinstance(data, str):
                return data
            url = _first(data, "caption_url", "srt_caption_url", "url", "srt_url")
            if not url and isinstance(data, dict) and _first(data, "caption", "content", "srt"):
                return _first(data, "caption", "content", "srt")
        if not url:
            raise HeyGenError(f"No caption available for translation {translation.id}")
        # Caption URLs are presigned storage links, so no API key header.
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read().decode("utf-8-sig")


def download(url: str, dest: Path) -> None:
    """Stream a (presigned) URL to disk atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(_first(err, "message", "code") or err)
        if err:
            return str(err)
        return str(_first(body, "message") or body)
    return str(body)[:500]


def _map_ids_to_languages(data: Any, languages: list[str]) -> dict[str, str]:
    """HeyGen returns translation ids in request order; accept the known shapes."""
    if isinstance(data, dict):
        # Shape: {"video_translation_ids": ["id1", ...]} or list of objects.
        items = _first(data, "video_translation_ids", "video_translate_ids", "translations", "items")
        if items is None:
            single = _first(data, "video_translation_id", "video_translate_id", "id")
            items = [single] if single else []
    else:
        items = data or []

    result: dict[str, str] = {}
    for i, item in enumerate(items):
        if isinstance(item, dict):
            tid = _first(item, "video_translation_id", "video_translate_id", "id")
            lang = _first(item, "output_language", "language") or (
                languages[i] if i < len(languages) else None
            )
        else:
            tid, lang = item, languages[i] if i < len(languages) else None
        if tid and lang:
            result[lang] = tid
    if len(result) != len(languages):
        raise HeyGenError(
            f"Expected {len(languages)} translation ids, got {len(result)}: {data}", body=data
        )
    return result
