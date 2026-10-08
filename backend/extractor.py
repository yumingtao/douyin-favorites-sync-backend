"""Self-contained Douyin video extractor — no external project dependency.

Provides:
  - extract_light(url): resolve share URL -> metadata (title, author, cover, download URL)
  - extract_heavy(url, output_dir): download video + transcribe audio via Whisper
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .douyin_client import (
    COLLECTION_BASE_PARAMS,
    SIGN_SESSION_UA,
    DouyinClient,
    Settings,
    cookie_value,
    random_mstoken,
    read_cookie,
    resolve_sign_asset,
    sign_url_with_recovery,
    signed_query,
)
from .transcriber import transcribe_media


class ExtractNotAvailableError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────
#  Douyin share-link resolver (self-implemented)
# ──────────────────────────────────────────────────────


@dataclass
class DouyinMeta:
    aweme_id: str = ""
    title: str = ""
    author: str = ""
    cover_url: str | None = None
    download_url: str | None = None
    image_urls: list[str] | None = None
    content_type: str = "video"
    source_url: str = ""


def _resolve_short_url(client: httpx.Client, url: str) -> str:
    """Follow 302 redirects to get the final douyin.com URL."""
    if "v.douyin.com" not in url and "iesdouyin.com" not in url:
        return url
    resp = client.get(url, follow_redirects=True, headers={
        "User-Agent": SIGN_SESSION_UA,
    })
    return str(resp.url)


def _extract_aweme_id(final_url: str) -> str:
    m = re.search(r"/video/(\d+)", final_url)
    if m:
        return m.group(1)
    m = re.search(r"/note/(\d+)", final_url)
    if m:
        return m.group(1)
    m = re.search(r"modal_id=(\d+)", final_url)
    if m:
        return m.group(1)
    return ""


async def _resolve_detail(aweme_id: str, settings: Settings) -> dict[str, Any]:
    """Call Douyin web detail API to get video metadata.

    Uses the same session-aware request flow as the collection API
    to ensure consistent compatibility.
    """
    client_obj = DouyinClient(settings)
    cookie = read_cookie(settings)
    uifid = cookie_value(cookie, "UIFID")

    # 使用出签会话一致的参数集（HeadlessChrome 151），而非 BASE_PARAMS（Windows Chrome 139）
    params = dict(COLLECTION_BASE_PARAMS)
    params["aweme_id"] = aweme_id
    params["msToken"] = cookie_value(cookie, "msToken") or random_mstoken()

    # 解析 webid（与收藏接口一致）
    webid = settings.webid or await resolve_sign_asset("webid", "DOUYIN_WEBID")
    if not webid:
        from .douyin_client import SignSessionError
        raise SignSessionError(
            "webid 无法解析：请设置 DOUYIN_WEBID，或确保出签浏览器会话可用于自动提取"
        )
    params["webid"] = webid
    if uifid:
        params["uifid"] = uifid

    # 本地 a_bogus 签名（用 collection_abogus，UA 与出签会话一致）
    query = urlencode(params, quote_via=quote)
    a_bogus = client_obj.collection_abogus.get_value(query, "GET")
    base_url = (
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?"
        f"{query}&a_bogus={quote(a_bogus, safe='')}"
    )

    # 浏览器会话内出签（带会话自愈），与收藏接口一致
    signed_url = await sign_url_with_recovery(base_url)

    # 请求头必须与出签会话 UA 保持一致
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://www.douyin.com",
        "Referer": f"https://www.douyin.com/video/{aweme_id}",
        "User-Agent": SIGN_SESSION_UA,
    }
    if uifid:
        headers["uifid"] = uifid
    headers["x-secsdk-csrf-token"] = "DOWNGRADE"

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        async with httpx.AsyncClient(timeout=20) as client:
            # 与收藏接口一样使用 POST 方法
            resp = await client.post(
                signed_url,
                content=f"aweme_id={aweme_id}",
                headers=headers,
            )
            if resp.status_code == 403:
                # 403 临时拦截：等待后重试（重新出签）
                if attempt < max_attempts:
                    await asyncio.sleep(8)
                    signed_url = await sign_url_with_recovery(base_url)
                    continue
                raise ExtractNotAvailableError(
                    f"detail API returned 403 Forbidden after {max_attempts} attempts for aweme_id={aweme_id}"
                )
            if resp.status_code == 401:
                raise ExtractNotAvailableError(
                    f"detail API returned 401 (auth_expired) for aweme_id={aweme_id}"
                )
            resp.raise_for_status()
            # 200 + 纯文本 "blocked"：软拦截
            if resp.text.strip() == "blocked":
                if attempt < max_attempts:
                    await asyncio.sleep(8)
                    signed_url = await sign_url_with_recovery(base_url)
                    continue
                raise ExtractNotAvailableError(
                    f"detail API returned 'blocked' after {max_attempts} attempts for aweme_id={aweme_id}"
                )
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                preview = resp.text[:300].replace("\n", "\\n")
                if attempt < max_attempts and (not resp.text.strip() or "<" in resp.text[:50]):
                    await asyncio.sleep(10)
                    signed_url = await sign_url_with_recovery(base_url)
                    continue
                raise ExtractNotAvailableError(
                    f"detail API returned non-JSON (HTTP {resp.status_code}): {preview}"
                ) from exc

    raise ExtractNotAvailableError(
        f"detail API returned non-JSON after {max_attempts} attempts for aweme_id={aweme_id}"
    )


def _parse_detail(raw: dict[str, Any], aweme_id: str, source_url: str) -> DouyinMeta:
    detail = raw.get("aweme_detail") or raw.get("detail") or {}
    if not detail:
        raise ExtractNotAvailableError(
            f"detail API returned empty for aweme_id={aweme_id}: "
            f"{json.dumps(raw, ensure_ascii=False)[:200]}"
        )

    desc = detail.get("desc") or ""
    author_obj = detail.get("author") or {}
    author = author_obj.get("nickname") or ""
    video = detail.get("video") or {}
    cover = video.get("cover") or {}
    cover_url = (cover.get("url_list") or [None])[0]

    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    download_url = url_list[0] if url_list else None
    if download_url and download_url.startswith("http://"):
        download_url = "https://" + download_url[len("http://"):]

    images_field = detail.get("images") or []
    image_urls: list[str] = []
    if images_field:
        for img in images_field:
            url_list_img = img.get("url_list") or []
            if url_list_img:
                image_urls.append(url_list_img[0])

    content_type = "image" if image_urls else "video"

    return DouyinMeta(
        aweme_id=str(detail.get("aweme_id") or aweme_id),
        title=desc,
        author=author,
        cover_url=cover_url,
        download_url=download_url,
        image_urls=image_urls,
        content_type=content_type,
        source_url=source_url,
    )


async def resolve_douyin_share(url: str, settings: Settings) -> DouyinMeta:
    """Resolve a Douyin share URL to metadata."""
    with httpx.Client(timeout=20) as client:
        final_url = _resolve_short_url(client, url)

    aweme_id = _extract_aweme_id(final_url)
    if not aweme_id:
        raise ExtractNotAvailableError(
            f"Could not extract aweme_id from URL: {final_url}"
        )

    raw = await _resolve_detail(aweme_id, settings)
    return _parse_detail(raw, aweme_id, url)


# ──────────────────────────────────────────────────────
#  Video downloader (self-implemented)
# ──────────────────────────────────────────────────────


def download_video(url: str, dest: Path, settings: Settings) -> bool:
    """Download a video from *url* to *dest*. Returns True on success."""
    cookie = read_cookie(settings)
    headers = {
        "User-Agent": SIGN_SESSION_UA,
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                return False
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
    return dest.exists() and dest.stat().st_size > 0


# ──────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────


def _tags_from_desc(desc: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in re.findall(r"#\s*([\w\u4e00-\u9fff-]+)", desc):
        t = tag.strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return tags


def _get_settings() -> Settings:
    root = Path(os.environ.get("DOUYIN_SYNC_ROOT", ".")).resolve()
    return Settings.from_root(root)


async def extract_light(url: str) -> dict[str, Any]:
    """Light extraction: resolve URL to metadata without downloading video."""
    settings = _get_settings()
    meta = await resolve_douyin_share(url, settings)
    return {
        "douyin_id": meta.aweme_id,
        "video_url": meta.download_url or None,
        "images": meta.image_urls or [],
        "desc": meta.title,
        "tags": _tags_from_desc(meta.title),
        "transcript": None,
        "author": meta.author,
        "cover": meta.cover_url,
        "content_type": meta.content_type,
        "source": meta.source_url or url,
    }


async def extract_heavy(url: str, output_dir: Path, *, whisper_model: str = "small") -> dict[str, Any]:
    """Heavy extraction: resolve + download video + transcribe audio."""
    try:
        import faster_whisper  # noqa: F401
        import zhconv  # noqa: F401
    except ImportError as exc:
        raise ExtractNotAvailableError(
            "heavy 模式缺少依赖，请运行 `pip install faster-whisper zhconv`"
        ) from exc

    settings = _get_settings()
    meta = await resolve_douyin_share(url, settings)
    aweme_id = meta.aweme_id or "unknown"
    out_dir = output_dir / f"{aweme_id}_{meta.author or 'unknown'}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write meta.json
    meta_data = {
        "aweme_id": aweme_id,
        "title": meta.title,
        "author": meta.author,
        "cover_url": meta.cover_url,
        "download_url": meta.download_url,
        "content_type": meta.content_type,
        "source_url": meta.source_url,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Download video
    video_path = out_dir / "video.mp4"
    download_ok = False
    if meta.download_url:
        download_ok = download_video(meta.download_url, video_path, settings)

    # Download images for image posts
    images: list[str] = []
    if meta.image_urls:
        images_subdir = out_dir / "images"
        images_subdir.mkdir(exist_ok=True)
        for idx, img_url in enumerate(meta.image_urls):
            img_dest = images_subdir / f"{idx:03d}.jpg"
            try:
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    resp = client.get(img_url, headers={
                        "User-Agent": SIGN_SESSION_UA,
                        "Referer": "https://www.douyin.com/",
                    })
                    if resp.status_code == 200:
                        img_dest.write_bytes(resp.content)
                        images.append(str(img_dest.resolve()))
            except Exception:
                pass

    # Transcribe
    transcript = ""
    if download_ok and video_path.exists():
        transcript = transcribe_media(video_path, model_name=whisper_model)

    # Write transcript file
    (out_dir / "transcript.txt").write_text(
        f"--- 文案 ---\n{meta.title}\n\n--- 转写 ---\n{transcript}",
        encoding="utf-8",
    )

    return {
        "douyin_id": aweme_id,
        "video_url": str(video_path.resolve()) if video_path.exists() else meta.download_url,
        "images": images,
        "desc": meta.title,
        "tags": _tags_from_desc(meta.title),
        "transcript": transcript or None,
        "author": meta.author,
        "cover": meta.cover_url,
        "content_type": meta.content_type,
        "source": meta.source_url or url,
        "out_dir": str(out_dir.resolve()),
    }
