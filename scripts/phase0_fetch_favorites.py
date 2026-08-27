#!/usr/bin/env python3
"""
Phase 0 validator for Douyin favorites pagination.

This script intentionally stays small: it imports DouK/TikTokDownloader's
ABogus signer, assembles the listcollection request, reads a manually supplied
Cookie, paginates, and prints aweme ids plus share URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

BASE_PARAMS: dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "support_h265": "1",
    "support_dash": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
    "uifid": "",
    "msToken": "",
    "publish_video_strategy_type": "2",
}


def load_abogus(douk_src: str):
    src_path = Path(douk_src).expanduser().resolve()
    if src_path.name == "src":
        root = src_path.parent
    else:
        root = src_path
    if not (root / "src" / "encrypt" / "aBogus.py").exists():
        raise SystemExit(
            "Cannot find DouK aBogus.py. Clone it first, e.g.\n"
            "  git clone --depth 1 https://github.com/JoeanAmier/TikTokDownloader.git /tmp/TikTokDownloader\n"
            "then pass --douk-src /tmp/TikTokDownloader"
        )
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "phase0_abogus", root / "src" / "encrypt" / "aBogus.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("Failed to load DouK aBogus.py.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ABogus(user_agent=USER_AGENT)


def read_cookie(cookie_file: str | None) -> str:
    env_cookie = os.environ.get("DOUYIN_COOKIE", "").strip()
    if env_cookie:
        return env_cookie

    path = Path(cookie_file or "cookie.txt")
    if not path.exists():
        raise SystemExit(
            f"Missing Cookie. Put the raw Cookie header in {path} or set DOUYIN_COOKIE."
        )

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"{path} is empty.")

    if path.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "cookie" in data:
            return str(data["cookie"]).strip()
        if isinstance(data, dict):
            return "; ".join(f"{k}={v}" for k, v in data.items())
    return text.removeprefix("Cookie:").strip()


def cookie_value(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def signed_query(abogus: Any, params: dict[str, str], method: str) -> str:
    query = urlencode(params, safe="=", quote_via=quote)
    return f"{query}&a_bogus={abogus.get_value(query, method)}"


def item_summary(item: dict[str, Any]) -> dict[str, Any]:
    aweme_id = str(item.get("aweme_id") or item.get("id") or "")
    share_info = item.get("share_info") or {}
    video = item.get("video") or {}
    cover = video.get("cover") or {}
    return {
        "aweme_id": aweme_id,
        "share_url": share_info.get("share_url")
        or f"https://www.douyin.com/video/{aweme_id}",
        "desc": item.get("desc") or "",
        "cover": (cover.get("url_list") or [None])[0],
        "create_time": item.get("create_time"),
    }


async def fetch_page(
    client: httpx.AsyncClient,
    abogus: Any,
    cookie: str,
    cursor: int,
    count: int,
) -> dict[str, Any]:
    params = BASE_PARAMS.copy()
    params["msToken"] = cookie_value(cookie, "msToken")
    params["count"] = str(count)
    params["cursor"] = str(cursor)
    query = signed_query(abogus, params, "GET")
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "*/*",
        "Content-Type": "text/plain;charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://www.douyin.com",
        "Referer": "https://www.douyin.com/user/self?showTab=favorite_collection",
        "User-Agent": USER_AGENT,
    }
    response = await client.post(
        f"https://www.douyin.com/aweme/v1/web/aweme/listcollection/?{query}",
        data={},
        headers=headers,
    )
    print(f"HTTP {response.status_code} cursor={cursor}", file=sys.stderr)
    response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        preview = response.text[:500].replace("\n", "\\n")
        raise RuntimeError(f"Douyin returned non-JSON content: {preview}") from exc


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie-file", default="cookie.txt")
    parser.add_argument("--douk-src", default=os.environ.get("DOUK_SRC", "/tmp/TikTokDownloader"))
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    abogus = load_abogus(args.douk_src)
    cookie = read_cookie(args.cookie_file)
    cursor = 0
    all_items: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for page in range(1, args.max_pages + 1):
            try:
                data = await fetch_page(client, abogus, cookie, cursor, args.count)
            except Exception as exc:
                print(f"request_failed: {exc}", file=sys.stderr)
                return 4
            if data.get("status_code") not in (None, 0):
                print(json.dumps(data, ensure_ascii=False, indent=2), file=sys.stderr)
                return 2

            items = data.get("aweme_list") or []
            print(f"page={page} items={len(items)} has_more={data.get('has_more')} next_cursor={data.get('cursor')}")
            for raw in items:
                summary = item_summary(raw)
                all_items.append(summary)
                print(f"{summary['aweme_id']}\t{summary['share_url']}")

            cursor = int(data.get("cursor") or 0)
            if not data.get("has_more") or not items:
                break

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(all_items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if all_items else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
