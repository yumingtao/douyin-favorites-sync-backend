from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .douyin_client import (
    AuthExpiredError,
    DouyinClient,
    DouyinRequestError,
    Settings,
    read_cookie,
    save_cookie,
)
from .extractor import ExtractNotAvailableError, extract_heavy, extract_light
from .state import BackendConfigStore, ContentIndexStore, HistoryStore, local_now
from .vault_writer import DuplicatePolicy, VaultConfig, write_obsidian_note


ROOT = Path(os.environ.get("DOUYIN_SYNC_ROOT", ".")).resolve()
SETTINGS = Settings.from_root(ROOT)
CLIENT = DouyinClient(SETTINGS)
HISTORY = HistoryStore(ROOT)
CONTENT = ContentIndexStore(ROOT)
CONFIG = BackendConfigStore(ROOT)
OUTPUT_DIR = ROOT / "output"
JOBS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Douyin Obsidian Sync Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["app://obsidian.md", "http://localhost", "http://127.0.0.1"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


class SyncFavoritesRequest(BaseModel):
    known_ids: list[str] = Field(default_factory=list)
    max: int = Field(default=50, ge=1, le=200)
    page_size: int = Field(default=10, ge=1, le=30)
    max_pages: int = Field(default=20, ge=1, le=100)


class CookieRequest(BaseModel):
    cookie: str = Field(min_length=1)


class ExtractRequest(BaseModel):
    url: str = Field(min_length=1)
    mode: Literal["light", "heavy"] = "light"
    model: Literal["tiny", "base", "small", "medium", "large-v2", "large-v3"] = "small"
    save_to_obsidian: bool = False
    duplicate_policy: DuplicatePolicy = "skip"


class VaultConfigRequest(BaseModel):
    vault_path: str = Field(min_length=1)
    note_folder: str = "Douyin"
    attachment_folder: str = "attachments/douyin"
    known_notes: list[dict[str, str]] = Field(default_factory=list)


class SaveHistoryRequest(BaseModel):
    duplicate_policy: DuplicatePolicy = "skip"


class RetryMarkRequest(BaseModel):
    retry_status: str
    retry_history_id: str = ""


class ManualImportRequest(BaseModel):
    share_text: str = Field(min_length=1)
    save_to_obsidian: bool = False
    duplicate_policy: DuplicatePolicy = "skip"


def _parse_share_text(text: str) -> dict[str, Any]:
    """Parse a pasted Douyin share text into a result dict.

    Share text format:
      <code> <description> # tag1 # tag2 https://v.douyin.com/xxx/ 复制此链接…
    The share code at the start is typically something like:
      "1.02 :6pm DhB:/ q@e.oQ 03/15 "
    Tags may have spaces after #, e.g. "# 大模型"
    """
    url = _extract_douyin_url(text)
    # Extract tags: "#tag" or "# tag" (space after #)
    raw_tags = re.findall(r"#\s*([\w\u4e00-\u9fff-]+)", text)
    tags = list(dict.fromkeys(raw_tags))
    # Strip tags, URL, and trailing boilerplate from description
    desc = text
    # Remove the URL
    desc = re.sub(r"https?://\S+", "", desc)
    # Remove the copy prompt
    desc = re.sub(r"复制此链接.*$", "", desc, flags=re.DOTALL)
    # Remove tags from description body (both "#tag" and "# tag")
    desc = re.sub(r"#\s*[\w\u4e00-\u9fff-]+", "", desc)
    # Remove the leading share code. The code is at the start and ends before
    # the first Chinese/CJK character or long English word.
    # Typical patterns: "1.02 :6pm DhB:/ q@e.oQ 03/15 "
    # Strip leading non-CJK gibberish: digits, colons, slashes, spaces, ascii tokens
    # until we hit CJK or a word >= 4 ascii chars (likely the title).
    m = re.match(
        r"^[\s\d.:/@a-zA-Z!%^&*+=_~\-]{3,40}?(?=[\u4e00-\u9fff]|[a-zA-Z]{4,})",
        desc,
    )
    if m:
        desc = desc[m.end():]
    # Clean up whitespace
    desc = re.sub(r"\s+", " ", desc).strip()
    if not desc:
        desc = "(无文案)"
    return {
        "douyin_id": "",
        "video_url": None,
        "images": [],
        "desc": desc,
        "tags": tags,
        "transcript": None,
        "author": "",
        "cover": None,
        "content_type": "video",
        "source": url,
    }


def capabilities() -> dict[str, bool]:
    heavy = (
        importlib.util.find_spec("faster_whisper") is not None
        and importlib.util.find_spec("zhconv") is not None
    )
    return {
        "light": True,
        "heavy": heavy,
        "web_ui": True,
        "obsidian_save": bool(CONFIG.read().get("vault_path")),
    }


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _extract_details(result: dict[str, Any], url: str, mode: str) -> dict[str, Any]:
    out_dir = result.get("out_dir")
    transcript = result.get("transcript") or ""
    details: dict[str, Any] = {
        "mode": mode,
        "source_input": url,
        "douyin_id": result.get("douyin_id"),
        "content_type": result.get("content_type"),
        "source": result.get("source"),
        "author": result.get("author"),
        "desc": result.get("desc"),
        "tags": result.get("tags") or [],
        "video_url": result.get("video_url"),
        "images": result.get("images") or [],
        "out_dir": out_dir,
        "transcript_length": len(transcript),
        "transcript_preview": transcript[:500],
    }
    if out_dir:
        details["meta_path"] = str(Path(out_dir) / "meta.json")
        details["transcript_path"] = str(Path(out_dir) / "transcript.txt")
        details["video_path"] = str(Path(out_dir) / "video.mp4")
    return details


def _extract_summary(result: dict[str, Any], details: dict[str, Any]) -> str:
    douyin_id = result.get("douyin_id") or "unknown id"
    content_type = result.get("content_type") or "unknown"
    pieces = [f"{douyin_id} {content_type}"]
    if details.get("transcript_length"):
        pieces.append(f"transcript {details['transcript_length']} chars")
    if details.get("out_dir"):
        pieces.append(f"saved to {Path(str(details['out_dir'])).name}")
    return " · ".join(pieces)


def _sync_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "returned 0 items"
    newest = items[0].get("aweme_id") or items[0].get("id") or ""
    return f"returned {len(items)} items · newest {newest}"


def _find_output_dirs(douyin_id: str) -> list[str]:
    if not douyin_id or not OUTPUT_DIR.exists():
        return []
    return [str(path.resolve()) for path in sorted(OUTPUT_DIR.glob(f"{douyin_id}_*")) if path.is_dir()]


def _vault_config() -> VaultConfig | None:
    data = CONFIG.read()
    vault_path = data.get("vault_path")
    if not vault_path:
        return None
    return VaultConfig(
        vault_path=Path(str(vault_path)),
        note_folder=str(data.get("note_folder") or "Douyin"),
        attachment_folder=str(data.get("attachment_folder") or "attachments/douyin"),
    )


def _extract_result_from_details(details: dict[str, Any]) -> dict[str, Any]:
    result = {
        "douyin_id": details.get("douyin_id"),
        "video_url": details.get("video_url"),
        "images": details.get("images") or [],
        "desc": details.get("desc") or "",
        "tags": details.get("tags") or [],
        "transcript": None,
        "author": details.get("author"),
        "cover": details.get("cover"),
        "content_type": details.get("content_type"),
        "source": details.get("source"),
        "out_dir": details.get("out_dir"),
    }
    out_dir = Path(str(details.get("out_dir") or ""))
    transcript_path = out_dir / "transcript.txt"
    if transcript_path.exists():
        text = transcript_path.read_text(encoding="utf-8")
        marker = "--- 文案 ---"
        result["transcript"] = text.split(marker, 1)[1].strip() if marker in text else text.strip()
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            result.update(
                {
                    "douyin_id": result.get("douyin_id") or meta.get("aweme_id"),
                    "desc": result.get("desc") or meta.get("title") or "",
                    "author": result.get("author") or meta.get("author"),
                    "cover": result.get("cover") or meta.get("cover_url"),
                    "content_type": result.get("content_type") or meta.get("content_type"),
                    "source": result.get("source") or meta.get("source_url"),
                }
            )
        except Exception:
            pass
    return result


def _save_result_to_obsidian(
    result: dict[str, Any],
    *,
    duplicate_policy: DuplicatePolicy,
) -> dict[str, Any]:
    config = _vault_config()
    if config is None:
        return {
            "saved": False,
            "duplicate": False,
            "error": "vault_not_configured",
            "message": "Obsidian Vault is not registered. Open the plugin once to register it.",
        }
    save_result = write_obsidian_note(config, result, duplicate_policy=duplicate_policy)
    douyin_id = str(result.get("douyin_id") or "")
    if douyin_id:
        CONTENT.upsert(
            douyin_id,
            {
                "status": "saved" if save_result.get("saved") else "duplicate",
                "note_path": save_result.get("note_path"),
                "absolute_note_path": save_result.get("absolute_note_path"),
                "out_dir": result.get("out_dir"),
                "source": result.get("source"),
                "content_type": result.get("content_type"),
                "last_duplicate_policy": duplicate_policy,
            },
        )
    return save_result


_DOUYIN_URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+/?|www\.douyin\.com/video/\d+)"
)


def _extract_douyin_url(raw: str) -> str:
    """Extract the first Douyin share URL from pasted share text.

    Accepts either a bare URL or the full share text copied from Douyin app,
    e.g.  '1.02 :6pm DhB:/ … https://v.douyin.com/xxxxx/ 复制此链接…'
    """
    raw = raw.strip()
    m = _DOUYIN_URL_RE.search(raw)
    return m.group(0) if m else raw


def _run_extract(payload: ExtractRequest) -> tuple[dict[str, Any], dict[str, Any], str]:
    url = _extract_douyin_url(payload.url)
    result = (
        extract_heavy(url, OUTPUT_DIR, whisper_model=payload.model)
        if payload.mode == "heavy"
        else extract_light(url)
    )
    details = _extract_details(result, url, payload.mode)
    summary = _extract_summary(result, details)
    return result, details, summary


async def _run_extract_job(job_id: str, payload: ExtractRequest) -> None:
    job = JOBS[job_id]
    job.update({"status": "running", "progress": 10, "stage": "解析输入和准备任务", "updated_at": local_now()})
    try:
        job.update({"progress": 35, "stage": "执行提取；heavy 模式会下载视频并转写音频", "updated_at": local_now()})
        result, details, summary = await asyncio.to_thread(_run_extract, payload)
        save_result = None
        if result.get("douyin_id"):
            existing = CONTENT.get(str(result["douyin_id"]))
            if existing:
                details["existing"] = existing
        if payload.save_to_obsidian:
            job.update({"progress": 85, "stage": "写入 Obsidian", "updated_at": local_now()})
            save_result = await asyncio.to_thread(
                _save_result_to_obsidian,
                result,
                duplicate_policy=payload.duplicate_policy,
            )
            details["obsidian"] = save_result
            if save_result.get("saved"):
                summary = f"{summary} · saved to Obsidian {save_result.get('note_path')}"
            elif save_result.get("duplicate"):
                summary = f"{summary} · duplicate skipped {save_result.get('note_path')}"
            elif save_result.get("error"):
                summary = f"{summary} · Obsidian save failed {save_result.get('error')}"
        record = HISTORY.append(
            {
                "type": f"web_extract_{payload.mode}",
                "status": "ok",
                "summary": summary,
                "url": payload.url,
                "details": details,
            }
        )
        job.update(
            {
                "status": "ok",
                "progress": 100,
                "stage": "完成",
                "updated_at": local_now(),
                "result": result,
                "history_id": record["id"],
                "summary": summary,
                "obsidian": save_result,
            }
        )
    except Exception as exc:
        record = HISTORY.append(
            {
                "type": f"web_extract_{payload.mode}",
                "status": "error",
                "summary": str(exc),
                "url": payload.url,
                "details": {"mode": payload.mode, "source_input": payload.url, "error": str(exc)},
            }
        )
        job.update(
            {
                "status": "error",
                "progress": 100,
                "stage": "失败",
                "updated_at": local_now(),
                "error": str(exc),
                "history_id": record["id"],
            }
        )


def render_page(request: Request, message: str = "") -> str:
    try:
        read_cookie(SETTINGS)
        auth = "cookie_present"
    except AuthExpiredError:
        auth = "missing_cookie"

    page = int(request.query_params.get("page", "1") or "1")
    page_size = int(request.query_params.get("page_size", "20") or "20")
    type_filter = request.query_params.get("type", "")
    status_filter = request.query_params.get("status", "")
    q = request.query_params.get("q", "")
    history = HISTORY.query(
        page=page,
        page_size=page_size,
        type_filter=type_filter,
        status_filter=status_filter,
        q=q,
    )

    type_options = ["<option value=''>All types</option>"] + [
        f"<option value='{esc(t)}' {'selected' if t == type_filter else ''}>{esc(t)}</option>"
        for t in history["types"]
    ]
    status_labels = {"ok": "✅ 成功", "error": "❌ 失败"}
    extra_status_options = [
        ("retried_ok", "🔄 重试成功"),
        ("retried_error", "⚠️ 重试仍失败"),
    ]
    status_options = ["<option value=''>全部状态</option>"] + [
        f"<option value='{esc(s)}' {'selected' if s == status_filter else ''}>{esc(status_labels.get(s, s))}</option>"
        for s in history["statuses"]
    ] + [
        f"<option value='{esc(v)}' {'selected' if v == status_filter else ''}>{esc(label)}</option>"
        for v, label in extra_status_options
    ]

    rows = []
    for event in history["items"]:
        status = event.get("status", "")
        css = "ok" if status == "ok" else "error" if status == "error" else ""
        event_id = event.get("id", "")
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        douyin_id = details.get("douyin_id") or str(event.get("summary", "")).split(" ")[0]
        source = douyin_id if douyin_id and douyin_id != "returned" else event.get("url", "")
        actions = [f"<a class='action' href='/history/{esc(event_id)}'>详情</a>"]
        if str(event.get("type", "")).startswith(("web_extract_", "extract_")) and status == "ok":
            actions.append(
                f"<button class='link-button' type='button' data-save-history='{esc(event_id)}'>保存</button>"
            )
        if event.get("type") == "sync_favorites" and status == "error":
            actions.append(
                f"<button class='link-button' type='button' data-sync-retry-id='{esc(event_id)}'>重试</button>"
            )
        if event.get("url"):
            actions.append(
                f"<button class='link-button' type='button' data-retry-url='{esc(event.get('url'))}' data-retry-mode='heavy' data-retry-id='{esc(event_id)}'>重试</button>"
            )
        actions.append(
            f"<button class='link-button link-button--danger' type='button' data-delete-id='{esc(event_id)}'>删除</button>"
        )
        # Build status cell with retry badge
        retry_status = event.get("retry_status", "")
        if retry_status == "ok":
            status_badge = f"<span class='ok'>ok</span> <span class='retry-badge retry-badge--ok' title='已重试成功'>✅ 已重试</span>"
        elif retry_status == "duplicate":
            status_badge = f"<span class='ok'>ok</span> <span class='retry-badge retry-badge--dup' title='重试发现内容重复'>📎 内容重复</span>"
        elif retry_status == "error":
            status_badge = f"<span class='{css}'>{esc(status)}</span> <span class='retry-badge retry-badge--error' title='重试仍失败'>⚠️ 重试失败</span>"
        else:
            status_badge = f"<span class='{css}'>{esc(status)}</span>"
        rows.append(
            f"<tr data-href='/history/{esc(event_id)}'>"
            f"<td>{esc(event.get('time', ''))}</td>"
            f"<td>{esc(event.get('type', ''))}</td>"
            f"<td>{status_badge}</td>"
            f"<td>{esc(source)}</td>"
            f"<td>{esc(event.get('summary', ''))}</td>"
            f"<td class='actions'>{' '.join(actions)}</td>"
            "</tr>"
        )
    history_rows = "\n".join(rows) or "<tr><td colspan='6'>No matching history.</td></tr>"

    prev_page = max(1, history["page"] - 1)
    next_page = min(history["pages"], history["page"] + 1)
    query_base = urlencode(
        {
            "type": type_filter,
            "status": status_filter,
            "q": q,
            "page_size": history["page_size"],
        }
    )
    caps = capabilities()
    cookie_status_class = "status-ok" if auth == "cookie_present" else "status-error"
    cookie_label = "✅ Cookie 有效" if auth == "cookie_present" else "❌ Cookie 缺失"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>抖音收藏同步 — Douyin Obsidian Sync</title>
  <style>
    :root {{
      --bg: #f0f4f8; --surface: #ffffff; --border: #e2e8f0; --border-light: #f1f5f9;
      --text: #1e293b; --text-secondary: #64748b; --text-muted: #94a3b8;
      --primary: #6366f1; --primary-hover: #4f46e5; --primary-light: #eef2ff;
      --success: #10b981; --success-light: #ecfdf5; --success-border: #a7f3d0;
      --error: #ef4444; --error-light: #fef2f2; --error-border: #fecaca;
      --warning: #f59e0b; --warning-light: #fffbeb;
      --radius: 12px; --radius-sm: 8px; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
      --shadow-md: 0 4px 6px -1px rgba(0,0,0,.07), 0 2px 4px -2px rgba(0,0,0,.05);
      --shadow-lg: 0 10px 15px -3px rgba(0,0,0,.08), 0 4px 6px -4px rgba(0,0,0,.04);
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; color: var(--text); background: var(--bg); line-height: 1.6; }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px; }}

    /* ── Header ── */
    .header {{ margin-bottom: 32px; }}
    .header h1 {{ font-size: 1.75rem; font-weight: 800; letter-spacing: -0.03em; margin: 0 0 4px; background: linear-gradient(135deg, var(--primary), #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}
    .header__sub {{ color: var(--text-secondary); font-size: 0.9rem; margin: 0; }}

    /* ── Message Banner ── */
    .msg {{ background: var(--primary-light); border: 1px solid #c7d2fe; color: #4338ca; padding: 12px 16px; border-radius: var(--radius-sm); margin-bottom: 20px; font-size: 0.9rem; animation: slideIn .3s ease; }}
    @keyframes slideIn {{ from {{ opacity: 0; transform: translateY(-8px); }} to {{ opacity: 1; transform: translateY(0); }} }}

    /* ── Status Grid ── */
    .status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 28px; }}
    .status-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; transition: box-shadow .2s ease, transform .15s ease; }}
    .status-card:hover {{ box-shadow: var(--shadow-md); transform: translateY(-1px); }}
    .status-card__icon {{ font-size: 1.5rem; margin-bottom: 8px; }}
    .status-card__label {{ color: var(--text-secondary); font-size: 0.8rem; font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; }}
    .status-card__value {{ font-weight: 700; font-size: 0.95rem; margin-top: 4px; word-break: break-all; }}
    .status-ok {{ color: var(--success); }}
    .status-error {{ color: var(--error); }}

    /* ── Section Card ── */
    .section {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 24px; box-shadow: var(--shadow); }}
    .section__header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }}
    .section__icon {{ font-size: 1.2rem; }}
    .section__title {{ font-size: 1.1rem; font-weight: 700; margin: 0; color: var(--text); }}
    .section__desc {{ color: var(--text-secondary); font-size: 0.85rem; line-height: 1.5; margin: 0 0 16px; }}

    /* ── Forms ── */
    .form-row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    input[type="text"], input[name="url"], input[type="number"] {{ font: inherit; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); min-width: 0; flex: 1; transition: border-color .2s, box-shadow .2s; font-size: 0.9rem; }}
    input:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,.15); }}
    input[name="url"], input[type="text"] {{ min-width: min(400px, 100%); }}
    select {{ font: inherit; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: 0.9rem; }}
    select:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,.15); }}

    /* ── Buttons ── */
    .btn {{ font: inherit; padding: 10px 20px; border-radius: var(--radius-sm); border: none; cursor: pointer; font-weight: 600; font-size: 0.9rem; transition: all .2s ease; display: inline-flex; align-items: center; gap: 6px; }}
    .btn--primary {{ background: var(--primary); color: white; box-shadow: 0 1px 2px rgba(99,102,241,.3); }}
    .btn--primary:hover {{ background: var(--primary-hover); box-shadow: 0 4px 6px rgba(99,102,241,.3); transform: translateY(-1px); }}
    .btn--primary:active {{ transform: translateY(0); }}
    .btn--primary:disabled {{ opacity: .6; cursor: wait; transform: none; }}
    .btn--ghost {{ background: transparent; color: var(--primary); padding: 6px 12px; }}
    .btn--ghost:hover {{ background: var(--primary-light); }}
    .btn--success {{ background: var(--success); color: white; }}
    .btn--success:hover {{ background: #059669; }}

    /* ── Inline controls ── */
    .inline-control {{ display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 0.9rem; cursor: pointer; }}
    .inline-control input[type="checkbox"] {{ width: 16px; height: 16px; accent-color: var(--primary); cursor: pointer; }}

    /* ── Mode info ── */
    .mode-info {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
    .mode-info__item {{ flex: 1; min-width: 260px; padding: 10px 14px; border-radius: var(--radius-sm); background: var(--border-light); font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; }}
    .mode-info__item strong {{ color: var(--text); }}

    /* ── Progress ── */
    .progress {{ display: none; margin-top: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; background: var(--surface); }}
    .progress__bar {{ height: 6px; background: var(--border-light); }}
    .progress__fill {{ display: block; height: 100%; width: 0%; background: linear-gradient(90deg, var(--primary), #8b5cf6); border-radius: 3px; transition: width .3s ease; }}
    .progress__body {{ padding: 14px 16px; }}
    .progress__stage {{ font-weight: 600; font-size: 0.9rem; color: var(--text); }}
    .progress__result {{ color: var(--text-secondary); font-size: 0.85rem; margin-top: 4px; }}

    /* ── Table ── */
    .table-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow-x: auto; box-shadow: var(--shadow); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 900px; }}
    th {{ background: var(--border-light); padding: 12px 16px; text-align: left; font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-secondary); border-bottom: 2px solid var(--border); }}
    td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 0.9rem; vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    tr[data-href] {{ cursor: pointer; transition: background .15s; }}
    tr[data-href]:hover {{ background: var(--primary-light); }}
    .ok {{ color: var(--success); font-weight: 600; }}
    .error {{ color: var(--error); font-weight: 600; }}
    .actions {{ white-space: nowrap; min-width: 160px; }}
    .action, .link-button {{ display: inline-block; margin-right: 6px; color: var(--primary); background: transparent; border: 0; padding: 4px 8px; cursor: pointer; font: inherit; font-size: 0.85rem; border-radius: 4px; transition: background .15s; }}
    .action:hover, .link-button:hover {{ background: var(--primary-light); text-decoration: none; }}
    .link-button--danger {{ color: var(--error); }}
    .link-button--danger:hover {{ background: var(--error-light); }}

    /* ── Toast ── */
    .toast-container {{ position: fixed; top: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }}
    .toast {{ padding: 12px 20px; border-radius: var(--radius-sm); font-size: 0.9rem; font-weight: 500; box-shadow: var(--shadow-lg); animation: toast-in .3s ease; max-width: 360px; }}
    .toast--ok {{ background: var(--success-light); color: var(--success); border: 1px solid var(--success-border); }}
    .toast--error {{ background: var(--error-light); color: var(--error); border: 1px solid var(--error-border); }}
    .toast--info {{ background: var(--primary-light); color: var(--primary); border: 1px solid var(--border); }}
    @keyframes toast-in {{ from {{ opacity: 0; transform: translateX(20px); }} to {{ opacity: 1; transform: translateX(0); }} }}

    /* ── Confirm Modal ── */
    .modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 10000; display: flex; align-items: center; justify-content: center; animation: fadeIn .2s ease; }}
    .modal-box {{ background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow-lg); padding: 28px 24px 20px; max-width: 380px; width: 90%; text-align: center; animation: modalIn .25s ease; }}
    .modal-box__icon {{ font-size: 2rem; margin-bottom: 8px; }}
    .modal-box__title {{ font-size: 1.1rem; font-weight: 700; margin: 0 0 6px; }}
    .modal-box__desc {{ color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 20px; line-height: 1.5; }}
    .modal-box__actions {{ display: flex; gap: 10px; justify-content: center; }}
    .modal-box__btn {{ padding: 8px 22px; border-radius: var(--radius-sm); font-size: 0.9rem; font-weight: 600; border: none; cursor: pointer; transition: background .15s ease; }}
    .modal-box__btn--cancel {{ background: var(--border-light); color: var(--text-secondary); }}
    .modal-box__btn--cancel:hover {{ background: var(--border); }}
    .modal-box__btn--danger {{ background: var(--error); color: #fff; }}
    .modal-box__btn--danger:hover {{ background: #dc2626; }}
    @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    @keyframes modalIn {{ from {{ opacity: 0; transform: scale(.95) translateY(8px); }} to {{ opacity: 1; transform: scale(1) translateY(0); }} }}

    /* ── Retry badge ── */
    .retry-badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 500; white-space: nowrap; }}
    .retry-badge--ok {{ background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }}
    .retry-badge--dup {{ background: #eff6ff; color: #1e40af; border: 1px solid #93c5fd; }}
    .retry-badge--error {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}

    /* ── Inline retry progress ── */
    .retry-progress-row td {{ background: var(--primary-light); padding: 8px 16px; }}
    .retry-progress-bar {{ height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-bottom: 4px; }}
    .retry-progress-fill {{ height: 100%; background: var(--primary); border-radius: 3px; transition: width .3s; }}
    .retry-progress-text {{ font-size: 0.82rem; color: var(--text-secondary); }}

    /* ── Pager ── */
    .pager {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 16px; padding: 0 4px; }}
    .pager a {{ border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 16px; background: var(--surface); color: var(--primary); font-weight: 500; font-size: 0.9rem; transition: all .2s; text-decoration: none; }}
    .pager a:hover {{ border-color: var(--primary); background: var(--primary-light); }}
    .pager__info {{ color: var(--text-secondary); font-size: 0.85rem; }}

    /* ── Links ── */
    a {{ color: var(--primary); text-decoration: none; transition: color .15s; }}
    a:hover {{ color: var(--primary-hover); text-decoration: underline; }}

    /* ── Filter bar ── */
    .filter-bar {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }}

    /* ── Auto refresh ── */
    .auto-refresh {{ display: flex; align-items: center; gap: 8px; margin-left: auto; font-size: 0.85rem; }}
    .auto-refresh select {{ padding: 4px 8px; font-size: 0.82rem; }}
    .refresh-countdown {{ color: var(--text-muted); font-variant-numeric: tabular-nums; min-width: 28px; }}

    /* ── Manual import ── */
    .manual-import-form {{ display: flex; flex-direction: column; gap: 12px; }}
    .manual-import-form textarea {{ font: inherit; padding: 12px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); resize: vertical; min-height: 80px; font-size: 0.9rem; line-height: 1.5; transition: border-color .2s, box-shadow .2s; }}
    .manual-import-form textarea:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,.15); }}
    .manual-import-actions {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .manual-import-result {{ margin-top: 12px; padding: 12px 16px; border-radius: var(--radius-sm); font-size: 0.9rem; }}
    .manual-import-result--ok {{ background: var(--success-light); border: 1px solid var(--success-border); color: #065f46; }}
    .manual-import-result--error {{ background: var(--error-light); border: 1px solid var(--error-border); color: #991b1b; }}

    /* ── Cookie management ── */
    .cookie-guide {{ background: var(--border-light); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 16px; margin-bottom: 16px; }}
    .cookie-guide > summary {{ cursor: pointer; font-weight: 600; font-size: 0.9rem; color: var(--primary); user-select: none; }}
    .cookie-guide > summary:hover {{ text-decoration: underline; }}
    .cookie-steps {{ padding-left: 20px; margin: 12px 0 8px; font-size: 0.85rem; line-height: 1.8; color: var(--text); }}
    .cookie-steps li {{ margin-bottom: 4px; }}
    .cookie-steps kbd {{ background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; font-size: 0.8rem; font-family: monospace; }}
    .cookie-steps code {{ background: #0f172a; color: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 0.82rem; }}
    .cookie-tips {{ margin-top: 8px; padding: 10px 14px; background: var(--warning-light); border: 1px solid #fde68a; border-radius: var(--radius-sm); font-size: 0.82rem; color: #92400e; }}
    .cookie-tips ul {{ margin: 6px 0 0; padding-left: 18px; }}
    .cookie-form {{ display: flex; flex-direction: column; gap: 12px; }}
    .cookie-form textarea {{ font: inherit; padding: 12px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); resize: vertical; min-height: 80px; font-size: 0.85rem; line-height: 1.5; font-family: monospace; transition: border-color .2s, box-shadow .2s; }}
    .cookie-form textarea:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,.15); }}
    .cookie-actions {{ display: flex; align-items: center; gap: 12px; }}
    .cookie-result {{ font-size: 0.85rem; }}
    .cookie-result--ok {{ color: var(--success); }}
    .cookie-result--error {{ color: var(--error); }}

    /* ── Responsive ── */
    @media (max-width: 768px) {{
      main {{ padding: 20px 16px; }}
      .status-grid {{ grid-template-columns: 1fr 1fr; }}
      .form-row {{ flex-direction: column; }}
      .form-row input, .form-row select {{ width: 100%; }}
      .filter-bar {{ flex-direction: column; }}
      .filter-bar select, .filter-bar input {{ width: 100%; }}
    }}
  </style>
</head>
<body>
<main>
  <div class="header">
    <h1>抖音收藏同步</h1>
    <p class="header__sub">本地后端状态与手动操作面板 · Douyin Obsidian Sync</p>
  </div>

  {f"<p class='msg'>{esc(message)}</p>" if message else ""}

  <section class="status-grid">
    <div class="status-card">
      <div class="status-card__icon">🕐</div>
      <div class="status-card__label">本地时间</div>
      <div class="status-card__value">{esc(local_now())}</div>
    </div>
    <div class="status-card">
      <div class="status-card__icon">🖥️</div>
      <div class="status-card__label">后端地址</div>
      <div class="status-card__value">http://127.0.0.1:8765</div>
    </div>
    <div class="status-card">
      <div class="status-card__icon">🔐</div>
      <div class="status-card__label">登录状态</div>
      <div class="status-card__value {cookie_status_class}">{cookie_label}</div>
    </div>
    <div class="status-card">
      <div class="status-card__icon">⚡</div>
      <div class="status-card__label">能力检测</div>
      <div class="status-card__value">Light: {'✅' if caps['light'] else '❌'} · Heavy: {'✅' if caps['heavy'] else '❌'}</div>
    </div>
  </section>

  <section class="section" id="cookieSection">
    <div class="section__header">
      <span class="section__icon">🔐</span>
      <h2 class="section__title">Cookie 管理</h2>
    </div>
    <details class="cookie-guide">
      <summary>📖 如何获取抖音 Cookie？（点击展开教程）</summary>
      <ol class="cookie-steps">
        <li>用电脑浏览器打开 <a href="https://www.douyin.com" target="_blank">https://www.douyin.com</a> 并登录你的账号</li>
        <li>按 <kbd>F12</kbd> 打开开发者工具，切换到 <strong>Network</strong>（网络）标签页</li>
        <li>刷新页面，在请求列表中点击任意一个发往 <code>douyin.com</code> 的请求</li>
        <li>在右侧 <strong>Headers</strong> 面板找到 <code>Request Headers</code> 下的 <code>Cookie</code> 字段</li>
        <li>复制 Cookie 后面那一整段值，粘贴到下方输入框中，点击保存</li>
      </ol>
      <div class="cookie-tips">
        💡 <strong>提示：</strong>
        <ul>
          <li>Cookie 有效期通常为 1-2 周，过期后需要重新获取</li>
          <li>关键字段为 <code>sessionid</code>、<code>ttwid</code>、<code>msToken</code>，确保这些包含在内</li>
          <li>不要泄露你的 Cookie，它等同于你的登录凭证</li>
        </ul>
      </div>
    </details>
    <form id="cookieForm" class="cookie-form">
      <textarea name="cookie" rows="4" placeholder="在此粘贴从浏览器复制的 Cookie 值…"></textarea>
      <div class="cookie-actions">
        <button id="cookieSaveBtn" type="submit" class="btn btn--primary">💾 保存 Cookie</button>
        <span id="cookieResult" class="cookie-result"></span>
      </div>
    </form>
  </section>

  <section class="section">
    <div class="section__header">
      <span class="section__icon">📋</span>
      <h2 class="section__title">手动同步预览</h2>
    </div>
    <p class="section__desc">只从收藏列表取回候选项，方便检查 Cookie 和接口是否正常；写入 Obsidian 仍由 Obsidian 插件执行。</p>
    <form method="post" action="/web/sync" class="form-row">
      <input name="known_ids" type="text" placeholder="已知 ID，逗号分隔。留空获取最新收藏" />
      <input name="max_items" type="number" min="1" max="50" value="5" style="max-width:100px" />
      <button type="submit" class="btn btn--primary">📥 拉取收藏</button>
    </form>
  </section>

  <section class="section">
    <div class="section__header">
      <span class="section__icon">🔧</span>
      <h2 class="section__title">手动提取 / 重试</h2>
    </div>
    <p class="section__desc">处理非收藏夹分享链接。默认先提取到后端 output；勾选保存后会写入已注册的 Obsidian Vault，重复内容默认跳过。</p>
    <div class="mode-info">
      <div class="mode-info__item"><strong>⚡ Light</strong> — 仅提取链接、封面和文案，速度快，适合日常浏览归档</div>
      <div class="mode-info__item"><strong>🎬 Heavy</strong> — 下载无水印视频 + Whisper 语音转写，耗时较长，适合深度阅读和检索</div>
    </div>
    <form id="extractForm" class="form-row">
      <input name="url" type="text" placeholder="粘贴抖音分享链接" />
      <select name="mode"><option value="heavy">🎬 Heavy（默认）</option><option value="light">⚡ Light</option></select>
      <label class="inline-control"><input name="save_to_obsidian" type="checkbox" checked /> 保存到 Obsidian</label>
      <select name="whisper_model" title="Whisper 转写模型（仅 Heavy）"></select>
      <select name="duplicate_policy">
        <option value="skip">跳过重复</option>
        <option value="overwrite">覆盖已有</option>
        <option value="copy">创建副本</option>
      </select>
      <button id="extractButton" type="submit" class="btn btn--primary">🚀 开始提取</button>
    </form>
    <div id="extractProgress" class="progress">
      <div class="progress__bar"><span id="extractBar" class="progress__fill"></span></div>
      <div class="progress__body">
        <div id="extractStage" class="progress__stage">等待开始</div>
        <div id="extractResult" class="progress__result"></div>
      </div>
    </div>
  </section>

  <section class="section">
    <div class="section__header">
      <span class="section__icon">📝</span>
      <h2 class="section__title">手动导入</h2>
    </div>
    <p class="section__desc">直接粘贴抖音分享文本，无需链接可访问。系统会自动提取文案、标签和链接，生成 Obsidian 笔记。适用于链接过期或无法在线解析的情况。</p>
    <form id="manualImportForm" class="manual-import-form">
      <textarea name="share_text" rows="4" placeholder="粘贴抖音分享文本，例如：&#10;1.02 :6pm DhB:/ q@e.oQ 03/15 视频标题... #标签1 #标签2 https://v.douyin.com/xxx/ 复制此链接..."></textarea>
      <div class="manual-import-actions">
        <label class="inline-control"><input name="save_to_obsidian" type="checkbox" checked /> 保存到 Obsidian</label>
        <select name="duplicate_policy">
          <option value="skip">跳过重复</option>
          <option value="overwrite">覆盖已有</option>
          <option value="copy">创建副本</option>
        </select>
        <button id="manualImportButton" type="submit" class="btn btn--success">📝 导入笔记</button>
      </div>
    </form>
    <div id="manualImportResult" class="manual-import-result" style="display:none;"></div>
  </section>

  <div class="section">
    <div class="section__header">
      <span class="section__icon">📜</span>
      <h2 class="section__title">操作历史</h2>
      <div class="auto-refresh">
        <label class="inline-control"><input id="autoRefreshToggle" type="checkbox" checked /> 自动刷新</label>
        <select id="refreshInterval">
          <option value="15">15 秒</option>
          <option value="30" selected>30 秒</option>
          <option value="60">60 秒</option>
        </select>
        <span id="refreshCountdown" class="refresh-countdown"></span>
      </div>
    </div>
    <p class="section__desc">Summary 是结果摘要：sync 显示返回数量和最新 ID；extract 显示作品 ID、类型、转写长度和保存目录。点击行可查看完整详情。历史数据存储在 SQLite 数据库 <code>douyin-sync.db</code>。</p>
    <form id="filterForm" method="get" action="/" class="filter-bar">
      <select name="type">{"".join(type_options)}</select>
      <select name="status">{"".join(status_options)}</select>
      <input name="q" type="text" value="{esc(q)}" placeholder="搜索 ID、URL、摘要、路径…" style="flex:1;min-width:200px" />
      <select name="page_size">
        {"".join(f"<option value='{n}' {'selected' if history['page_size'] == n else ''}>{n} 条/页</option>" for n in (10, 20, 50, 100))}
      </select>
      <button type="submit" class="btn btn--primary">🔍 筛选</button>
    </form>
    <div class="table-wrap">
      <table id="historyTable">
        <thead><tr><th>时间</th><th>类型</th><th>状态</th><th>ID / 来源</th><th>摘要</th><th>操作</th></tr></thead>
        <tbody id="historyBody">{history_rows}</tbody>
      </table>
    </div>
    <div class="pager">
      <a href="/?{query_base}&page={prev_page}">← 上一页</a>
      <span class="pager__info">第 {history['page']} / {history['pages']} 页 · 共 {history['total']} 条</span>
      <a href="/?{query_base}&page={next_page}">下一页 →</a>
    </div>
    </div>
  </div>
</main>
<div class="toast-container" id="toastContainer"></div>
<script>
function showToast(msg, type = 'info') {{
  const c = document.getElementById('toastContainer');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast toast--${{type}}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => {{ t.style.opacity = '0'; t.style.transform = 'translateX(20px)'; t.style.transition = 'all .3s'; setTimeout(() => t.remove(), 300); }}, 3000);
}}
const form = document.getElementById("extractForm");
const button = document.getElementById("extractButton");
const progress = document.getElementById("extractProgress");
const bar = document.getElementById("extractBar");
const stage = document.getElementById("extractStage");
const result = document.getElementById("extractResult");

// Track active inline retries so progress survives auto-refresh
const _activeRetries = new Map();

// ── Inline retry: trigger extraction directly, show progress below the row ──
async function inlineRetry(btn) {{
  const url = btn.dataset.retryUrl;
  const mode = btn.dataset.retryMode || "heavy";
  const originId = btn.dataset.retryId || "";
  const row = btn.closest("tr");

  // Remove stale progress row if any
  const next = row.nextElementSibling;
  if (next && next.classList.contains("retry-progress-row")) next.remove();

  // Insert progress row
  const progRow = document.createElement("tr");
  progRow.className = "retry-progress-row";
  progRow.innerHTML = `<td colspan="6">
    <div class="retry-progress-bar"><div class="retry-progress-fill" style="width:0"></div></div>
    <div class="retry-progress-text">⏳ 提交任务…</div>
  </td>`;
  row.after(progRow);
  const fill = progRow.querySelector(".retry-progress-fill");
  const txt = progRow.querySelector(".retry-progress-text");
  btn.disabled = true;
  _activeRetries.set(originId, {{ progress: 0, stage: "⏳ 提交任务…", originRow: row }});

  try {{
    const resp = await fetch("/api/jobs/extract", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ url, mode, model: getWebWhisperModel() }})
    }});
    const created = await resp.json();
    if (!resp.ok) throw new Error(created.detail?.error || JSON.stringify(created));

    const timer = setInterval(async () => {{
      const poll = await fetch(`/api/jobs/${{created.job_id}}`);
      const job = await poll.json();
      const pct = job.progress || 0;
      const label = `${{job.status === "ok" ? "✅" : job.status === "error" ? "❌" : "⏳"}} ${{job.stage || job.status || ""}}`;
      fill.style.width = `${{pct}}%`;
      txt.textContent = label;
      const entry = _activeRetries.get(originId);
      if (entry) {{ entry.progress = pct; entry.stage = label; }}
      if (job.status === "ok" || job.status === "error") {{
        clearInterval(timer);
        btn.disabled = false;
        // Determine effective retry status
        let rs = job.status;
        if (rs === "ok" && (job.obsidian?.duplicate || (job.summary || "").includes("duplicate"))) {{
          rs = "duplicate";
        }}
        // Mark original record
        if (originId) {{
          await fetch(`/api/history/${{originId}}/retry-mark`, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ retry_status: rs, retry_history_id: job.history_id || "" }})
          }}).catch(() => {{}});
        }}
        // Show result then update the row in-place (no full list refresh)
        txt.textContent += job.summary ? ` · ${{job.summary}}` : "";
        setTimeout(() => {{
          progRow.remove();
          _activeRetries.delete(originId);
          // Update status cell directly so the record stays visible regardless of filter
          const statusCell = row.cells[2];
          if (statusCell) {{
            const eff = (rs === "ok" || rs === "duplicate") ? "ok" : "error";
            let badge = "";
            if (rs === "ok") badge = " <span class='retry-badge retry-badge--ok' title='已重试成功'>✅ 已重试</span>";
            else if (rs === "duplicate") badge = " <span class='retry-badge retry-badge--dup' title='重试发现内容重复'>📎 内容重复</span>";
            else if (rs === "error") badge = " <span class='retry-badge retry-badge--error' title='重试仍失败'>⚠️ 重试失败</span>";
            statusCell.innerHTML = `<span class='${{eff}}'>${{eff}}</span>${{badge}}`;
          }}
        }}, 1500);
      }}
    }}, 1000);
  }} catch (err) {{
    btn.disabled = false;
    txt.textContent = "❌ " + String(err);
    _activeRetries.delete(originId);
  }}
}}
function setProgress(job) {{
  progress.style.display = "block";
  bar.style.width = `${{job.progress || 0}}%`;
  stage.textContent = `${{job.status === 'ok' ? '✅' : job.status === 'error' ? '❌' : '⏳'}} ${{job.stage || job.status}}`;
  const text = job.error || job.summary || "";
  result.innerHTML = text;
  if (job.obsidian?.note_path) result.innerHTML += ` · 📝 <a href="/history/${{job.history_id}}">${{job.obsidian.note_path}}</a>`;
  if (job.history_id) result.innerHTML += ` · <a href="/history/${{job.history_id}}">查看详情</a>`;
}}
// ── Whisper model helper (localStorage persistence) ──
const WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v2", "large-v3"];
function getWebWhisperModel() {{
  let m = localStorage.getItem("douyin_whisper_model");
  if (!WHISPER_MODELS.includes(m)) m = "small";
  return m;
}}
function setWebWhisperModel(m) {{
  if (WHISPER_MODELS.includes(m)) localStorage.setItem("douyin_whisper_model", m);
}}
function populateWhisperSelects() {{
  const saved = getWebWhisperModel();
  document.querySelectorAll('select[name="whisper_model"], select#retryWhisperModel').forEach((sel) => {{
    sel.innerHTML = WHISPER_MODELS.map((m) => `<option value="${{m}}" ${{m === saved ? "selected" : ""}}>${{m === "small" ? `${{m}}（推荐）` : m}}</option>`).join("");
    sel.addEventListener("change", () => setWebWhisperModel(sel.value));
  }});
}}
populateWhisperSelects();
async function startExtract(url, mode, model, saveToObsidian, duplicatePolicy) {{
  const response = await fetch("/api/jobs/extract", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      url, mode, model,
      save_to_obsidian: saveToObsidian,
      duplicate_policy: duplicatePolicy
    }})
  }});
  const created = await response.json();
  if (!response.ok) throw new Error(created.detail?.error || JSON.stringify(created));
  const timer = setInterval(async () => {{
    const poll = await fetch(`/api/jobs/${{created.job_id}}`);
    const job = await poll.json();
    setProgress(job);
    if (job.status === "ok" || job.status === "error") {{
      clearInterval(timer);
      button.disabled = false;
      // Refresh history list immediately and reset countdown
      if (window._refreshHistoryNow) window._refreshHistoryNow();
    }}
  }}, 1000);
}}
form.addEventListener("submit", async (event) => {{
  event.preventDefault();
  button.disabled = true;
  result.textContent = "";
  const data = new FormData(form);
  try {{
    await startExtract(
      data.get("url"), data.get("mode"),
      data.get("whisper_model") || getWebWhisperModel(),
      data.get("save_to_obsidian") === "on",
      data.get("duplicate_policy")
    );
  }} catch (err) {{
    button.disabled = false;
    progress.style.display = "block";
    stage.textContent = "❌ 提取失败";
    result.textContent = String(err);
  }}
}});
document.querySelectorAll("tr[data-href]").forEach((row) => {{
  row.addEventListener("click", (event) => {{
    if (event.target.closest("a,button")) return;
    window.location.href = row.dataset.href;
  }});
}});
document.querySelectorAll("[data-retry-url]").forEach((el) => {{
  el.addEventListener("click", () => inlineRetry(el));
}});

// ── Inline sync retry (sync_favorites) ──
async function syncRetry(btn) {{
  const originId = btn.dataset.syncRetryId || "";
  const row = btn.closest("tr");
  const next = row.nextElementSibling;
  if (next && next.classList.contains("retry-progress-row")) next.remove();
  const progRow = document.createElement("tr");
  progRow.className = "retry-progress-row";
  progRow.innerHTML = `<td colspan="6">
    <div class="retry-progress-bar"><div class="retry-progress-fill" style="width:5%"></div></div>
    <div class="retry-progress-text">⏳ 探测中…（单次请求，数秒返回；被拦说明仍在惩罚期，请勿频繁点击）</div>
  </td>`;
  row.after(progRow);
  const fill = progRow.querySelector(".retry-progress-fill");
  const txt = progRow.querySelector(".retry-progress-text");
  btn.disabled = true;
  let pct = 5;
  const timer = setInterval(() => {{ pct = Math.min(95, pct + 2); fill.style.width = pct + "%"; }}, 1500);
  try {{
    const resp = await fetch(`/api/history/${{originId}}/retry-sync`, {{ method: "POST" }});
    const data = await resp.json().catch(() => ({{}}));
    if (!resp.ok) throw new Error((data.detail && data.detail.error) || ("HTTP " + resp.status));
    clearInterval(timer);
    fill.style.width = "100%";
    txt.textContent = "✅ " + (data.summary || "ok");
    const statusCell = row.cells[2];
    if (statusCell) statusCell.innerHTML = `<span class='ok'>ok</span> <span class='retry-badge retry-badge--ok' title='已重试成功'>✅ 已重试</span>`;
    setTimeout(() => {{ progRow.remove(); }}, 2500);
  }} catch (err) {{
    clearInterval(timer);
    fill.style.width = "100%";
    txt.textContent = "❌ " + String(err.message || err);
    const statusCell = row.cells[2];
    if (statusCell) statusCell.innerHTML = `<span class='error'>error</span> <span class='retry-badge retry-badge--error' title='重试仍失败'>⚠️ 重试失败</span>`;
    setTimeout(() => {{ progRow.remove(); btn.disabled = false; }}, 3500);
  }}
}}
document.querySelectorAll("[data-sync-retry-id]").forEach((el) => {{
  el.addEventListener("click", () => syncRetry(el));
}});

// ── Confirm dialog ──
function confirmDialog(title, desc) {{
  return new Promise((resolve) => {{
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-box">
        <div class="modal-box__icon">⚠️</div>
        <div class="modal-box__title">${{title}}</div>
        <div class="modal-box__desc">${{desc}}</div>
        <div class="modal-box__actions">
          <button class="modal-box__btn modal-box__btn--cancel" type="button">取消</button>
          <button class="modal-box__btn modal-box__btn--danger" type="button">确认删除</button>
        </div>
      </div>`;
    overlay.querySelector('.modal-box__btn--cancel').addEventListener('click', () => {{ overlay.remove(); resolve(false); }});
    overlay.querySelector('.modal-box__btn--danger').addEventListener('click', () => {{ overlay.remove(); resolve(true); }});
    overlay.addEventListener('click', (e) => {{ if (e.target === overlay) {{ overlay.remove(); resolve(false); }} }});
    document.body.appendChild(overlay);
  }});
}}

// ── Inline delete ──
async function inlineDelete(btn) {{
  const id = btn.dataset.deleteId;
  if (!id) return;
  const confirmed = await confirmDialog("确认删除", "删除后不可恢复，确定要删除这条记录吗？");
  if (!confirmed) return;
  const row = btn.closest("tr");
  btn.disabled = true;
  try {{
    const resp = await fetch(`/api/history/${{id}}`, {{ method: "DELETE" }});
    if (!resp.ok) throw new Error("删除失败");
    if (row) {{
      const next = row.nextElementSibling;
      if (next && next.classList.contains("retry-progress-row")) next.remove();
      row.remove();
    }}
    showToast("✅ 已删除", "ok");
    const tbody = document.getElementById("historyBody");
    if (tbody && !tbody.querySelector("tr")) {{
      tbody.innerHTML = "<tr><td colspan='6'>暂无匹配记录</td></tr>";
    }}
  }} catch (err) {{
    btn.disabled = false;
    showToast("❌ " + String(err), "error");
  }}
}}
document.querySelectorAll("[data-delete-id]").forEach((el) => {{
  el.addEventListener("click", () => inlineDelete(el));
}});
document.querySelectorAll("[data-save-history]").forEach((el) => {{
  el.addEventListener("click", async () => {{
    progress.style.display = "block";
    stage.textContent = "⏳ 写入 Obsidian…";
    result.textContent = "";
    const response = await fetch(`/api/history/${{el.dataset.saveHistory}}/save`, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ duplicate_policy: "skip" }})
    }});
    const data = await response.json();
    stage.textContent = response.ok ? "✅ 保存完成" : "❌ 保存失败";
    result.textContent = data.message || data.error || data.note_path || JSON.stringify(data);
  }});
}});

// ── Auto refresh history ──
(function() {{
  const toggle = document.getElementById("autoRefreshToggle");
  const intervalSelect = document.getElementById("refreshInterval");
  const countdownEl = document.getElementById("refreshCountdown");
  const tbody = document.getElementById("historyBody");
  let countdown = 0;
  let timer = null;

  function esc(s) {{
    const div = document.createElement("div");
    div.textContent = String(s ?? "");
    return div.innerHTML;
  }}

  function getParams() {{
    const url = new URL(window.location);
    return {{
      page: url.searchParams.get("page") || "1",
      page_size: url.searchParams.get("page_size") || "20",
      type: url.searchParams.get("type") || "",
      status: url.searchParams.get("status") || "",
      q: url.searchParams.get("q") || ""
    }};
  }}

  function buildRow(event) {{
    const status = event.status || "";
    const css = status === "ok" ? "ok" : status === "error" ? "error" : "";
    const eid = event.id || "";
    const details = (typeof event.details === "object") ? event.details : {{}};
    const douyinId = details.douyin_id || String(event.summary || "").split(" ")[0];
    const source = (douyinId && douyinId !== "returned") ? douyinId : (event.url || "");
    let actions = `<a class='action' href='/history/${{esc(eid)}}'>详情</a>`;
    if (String(event.type || "").startsWith("web_extract_") && status === "ok") {{
      actions += `<button class='link-button' type='button' data-save-history='${{esc(eid)}}'>保存</button>`;
    }}
    if (event.type === "sync_favorites" && status === "error") {{
      actions += `<button class='link-button' type='button' data-sync-retry-id='${{esc(eid)}}'>重试</button>`;
    }}
    if (event.url) {{
      actions += `<button class='link-button' type='button' data-retry-url='${{esc(event.url)}}' data-retry-mode='heavy' data-retry-id='${{esc(eid)}}'>重试</button>`;
    }}
    actions += `<button class='link-button link-button--danger' type='button' data-delete-id='${{esc(eid)}}'>删除</button>`;
    const rs = event.retry_status || "";
    const effStatus = (rs === "ok" || rs === "duplicate") ? "ok" : status;
    const effCss = effStatus === "ok" ? "ok" : effStatus === "error" ? "error" : "";
    let statusCell = `<span class='${{effCss}}'>${{esc(effStatus)}}</span>`;
    if (rs === "ok") {{
      statusCell += ` <span class='retry-badge retry-badge--ok' title='已重试成功'>✅ 已重试</span>`;
    }} else if (rs === "duplicate") {{
      statusCell += ` <span class='retry-badge retry-badge--dup' title='重试发现内容重复'>📎 内容重复</span>`;
    }} else if (rs === "error") {{
      statusCell += ` <span class='retry-badge retry-badge--error' title='重试仍失败'>⚠️ 重试失败</span>`;
    }}
    return `<tr data-href='/history/${{esc(eid)}}'>`
      + `<td>${{esc(event.time || '')}}</td>`
      + `<td>${{esc(event.type || '')}}</td>`
      + `<td>${{statusCell}}</td>`
      + `<td>${{esc(source)}}</td>`
      + `<td>${{esc(event.summary || '')}}</td>`
      + `<td class='actions'>${{actions}}</td></tr>`;
  }}

  async function refreshHistory() {{
    try {{
      const p = getParams();
      const qs = new URLSearchParams(p).toString();
      const resp = await fetch(`/api/history?${{qs}}`);
      const data = await resp.json();
      if (data.items && tbody) {{
        tbody.innerHTML = data.items.map(buildRow).join("\\n") || "<tr><td colspan='6'>暂无匹配记录</td></tr>";
        // Re-bind row clicks and action buttons
        tbody.querySelectorAll("tr[data-href]").forEach(row => {{
          row.addEventListener("click", (e) => {{ if (!e.target.closest("a,button")) window.location.href = row.dataset.href; }});
        }});
        tbody.querySelectorAll("[data-retry-url]").forEach(el => {{
          el.addEventListener("click", () => inlineRetry(el));
        }});
        tbody.querySelectorAll("[data-sync-retry-id]").forEach(el => {{
          el.addEventListener("click", () => syncRetry(el));
        }});
        tbody.querySelectorAll("[data-delete-id]").forEach(el => {{
          el.addEventListener("click", () => inlineDelete(el));
        }});
        tbody.querySelectorAll("[data-save-history]").forEach(el => {{
          el.addEventListener("click", async () => {{
            progress.style.display = "block";
            stage.textContent = "⏳ 写入 Obsidian…";
            result.textContent = "";
            const r = await fetch(`/api/history/${{el.dataset.saveHistory}}/save`, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ duplicate_policy: "skip" }})
            }});
            const d = await r.json();
            stage.textContent = r.ok ? "✅ 保存完成" : "❌ 保存失败";
            result.textContent = d.message || d.error || d.note_path || JSON.stringify(d);
          }});
        }});
      }}

      // Re-inject progress rows for active retries
      _activeRetries.forEach((entry, originId) => {{
        const btn = tbody.querySelector(`[data-retry-id="${{originId}}"]`);
        if (btn) {{
          btn.disabled = true;
          const r = btn.closest("tr");
          if (r) {{
            const pr = document.createElement("tr");
            pr.className = "retry-progress-row";
            pr.innerHTML = `<td colspan="6">
              <div class="retry-progress-bar"><div class="retry-progress-fill" style="width:${{entry.progress || 0}}%"></div></div>
              <div class="retry-progress-text">${{entry.stage || "⏳ 处理中…"}}</div>
            </td>`;
            r.after(pr);
          }}
        }}
      }});

      // Update pager info and links
      const pagerInfo = document.querySelector(".pager__info");
      const pagerLinks = document.querySelectorAll(".pager a");
      if (pagerInfo) pagerInfo.textContent = `第 ${{data.page}} / ${{data.pages}} 页 · 共 ${{data.total}} 条`;
      const params = new URLSearchParams(getParams());
      const curPage = data.page;
      if (pagerLinks.length >= 2) {{
        const prevP = Math.max(1, curPage - 1);
        const nextP = Math.min(data.pages, curPage + 1);
        params.set("page", String(prevP));
        pagerLinks[0].setAttribute("href", `/?${{params.toString()}}`);
        params.set("page", String(nextP));
        pagerLinks[1].setAttribute("href", `/?${{params.toString()}}`);
        // Re-bind pager clicks
        pagerLinks.forEach(a => {{
          a.onclick = (e) => {{
            e.preventDefault();
            window.history.pushState({{}}, "", a.getAttribute("href"));
            if (window._refreshHistoryNow) window._refreshHistoryNow();
          }};
        }});
      }}
    }} catch (err) {{
      console.warn("Auto-refresh failed:", err);
    }}
  }}

  function startTimer() {{
    stopTimer();
    const interval = parseInt(intervalSelect.value) || 30;
    countdown = interval;
    timer = setInterval(() => {{
      countdown--;
      countdownEl.textContent = `${{countdown}}s`;
      if (countdown <= 0) {{
        countdown = interval;
        void refreshHistory();
      }}
    }}, 1000);
    countdownEl.textContent = `${{countdown}}s`;
  }}

  function stopTimer() {{
    if (timer) {{ clearInterval(timer); timer = null; }}
    countdownEl.textContent = "";
  }}

  toggle.addEventListener("change", () => {{
    if (toggle.checked) startTimer(); else stopTimer();
  }});
  intervalSelect.addEventListener("change", () => {{
    if (toggle.checked) startTimer();
  }});

  if (toggle.checked) startTimer();

  // Expose for external callers (e.g. extract completion)
  window._refreshHistoryNow = function() {{
    void refreshHistory();
    if (toggle.checked) startTimer();
  }};
}})();

// ── AJAX filter & pager (no full page reload, preserve scroll) ──
(function() {{
  const filterForm = document.getElementById("filterForm");
  if (!filterForm) return;

  filterForm.addEventListener("submit", (e) => {{
    e.preventDefault();
    const fd = new FormData(filterForm);
    const params = new URLSearchParams();
    for (const [k, v] of fd.entries()) {{
      if (v) params.set(k, v);
    }}
    const newUrl = `${{window.location.pathname}}?${{params.toString()}}`;
    window.history.pushState({{}}, "", newUrl);
    if (window._refreshHistoryNow) window._refreshHistoryNow();
  }});

  // Intercept pager links
  document.querySelectorAll(".pager a").forEach(a => {{
    a.addEventListener("click", (e) => {{
      e.preventDefault();
      const href = a.getAttribute("href");
      window.history.pushState({{}}, "", href);
      if (window._refreshHistoryNow) window._refreshHistoryNow();
    }});
  }});
}})();

// ── Manual import ──
(function() {{
  const form = document.getElementById("manualImportForm");
  const btn = document.getElementById("manualImportButton");
  const resultEl = document.getElementById("manualImportResult");
  if (!form) return;

  form.addEventListener("submit", async (e) => {{
    e.preventDefault();
    btn.disabled = true;
    resultEl.style.display = "none";
    const data = new FormData(form);
    try {{
      const resp = await fetch("/api/manual-import", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          share_text: data.get("share_text"),
          save_to_obsidian: data.get("save_to_obsidian") === "on",
          duplicate_policy: data.get("duplicate_policy")
        }})
      }});
      const result = await resp.json();
      resultEl.style.display = "block";
      if (resp.ok) {{
        resultEl.className = "manual-import-result manual-import-result--ok";
        const r = result.result;
        let html = `✅ 导入成功 · ${{r.desc.length}} 字 · ${{r.tags.length}} 标签`;
        if (r.source) html += ` · <a href="${{r.source}}">原始链接</a>`;
        if (result.obsidian?.note_path) html += ` · 📝 ${{result.obsidian.note_path}}`;
        if (result.history_id) html += ` · <a href="/history/${{result.history_id}}">查看详情</a>`;
        resultEl.innerHTML = html;
        form.elements.share_text.value = "";
      }} else {{
        resultEl.className = "manual-import-result manual-import-result--error";
        resultEl.textContent = "❌ " + (result.detail?.error || JSON.stringify(result));
      }}
    }} catch (err) {{
      resultEl.style.display = "block";
      resultEl.className = "manual-import-result manual-import-result--error";
      resultEl.textContent = "❌ " + String(err);
    }} finally {{
      btn.disabled = false;
    }}
  }});
}})();

// ── Cookie management ──
(function() {{
  const form = document.getElementById("cookieForm");
  if (!form) return;
  const btn = document.getElementById("cookieSaveBtn");
  const resultEl = document.getElementById("cookieResult");
  form.addEventListener("submit", async (e) => {{
    e.preventDefault();
    btn.disabled = true;
    resultEl.textContent = "⏳ 保存中…";
    resultEl.className = "cookie-result";
    const data = new FormData(form);
    const cookie = (data.get("cookie") || "").trim();
    if (!cookie) {{
      resultEl.textContent = "❌ 请输入 Cookie";
      resultEl.className = "cookie-result cookie-result--error";
      btn.disabled = false;
      return;
    }}
    try {{
      const resp = await fetch("/api/auth/cookie", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ cookie }})
      }});
      const result = await resp.json();
      if (resp.ok) {{
        resultEl.textContent = "✅ Cookie 保存成功！页面将在 2 秒后刷新…";
        resultEl.className = "cookie-result cookie-result--ok";
        form.elements.cookie.value = "";
        setTimeout(() => window.location.reload(), 2000);
      }} else {{
        resultEl.textContent = "❌ " + (result.detail?.error || "保存失败");
        resultEl.className = "cookie-result cookie-result--error";
      }}
    }} catch (err) {{
      resultEl.textContent = "❌ " + String(err);
      resultEl.className = "cookie-result cookie-result--error";
    }} finally {{
      btn.disabled = false;
    }}
  }});
}})();
</script>
</body>
</html>"""


def render_detail_page(event: dict[str, Any]) -> str:
    details = dict(event.get("details") or {})
    douyin_id = str(details.get("douyin_id") or event.get("summary", "")).split(" ")[0]
    output_dirs = details.get("out_dir")
    if output_dirs:
        output_list = [str(output_dirs)]
    else:
        output_list = _find_output_dirs(douyin_id)
    if output_list and "out_dir" not in details:
        details["out_dir_candidates"] = output_list
        details["out_dir"] = output_list[0]
        details["transcript_path"] = str(Path(output_list[0]) / "transcript.txt")
        details["video_path"] = str(Path(output_list[0]) / "video.mp4")
    douyin_record = CONTENT.get(douyin_id) if douyin_id else None
    pretty = json.dumps({**event, "details": details}, ensure_ascii=False, indent=2)
    retry_status = event.get("retry_status") or ""
    retry_history_id = event.get("retry_history_id") or ""
    # Effective status: retry success upgrades the displayed status
    effective_status = "ok" if retry_status in ("ok", "duplicate") else event.get("status", "")
    save_button = (
        f"<button id='saveButton' data-event-id='{esc(event.get('id'))}'>Save to Obsidian</button>"
        if effective_status == "ok" and str(event.get("type", "")).startswith(("web_extract_", "extract_"))
        else ""
    )
    status_class = "status-ok" if effective_status == "ok" else "status-error" if effective_status == "error" else ""
    retry_url = event.get("url") or details.get("source_input") or ""
    show_retry = bool(retry_url and retry_url != "None")
    # Retry badge HTML
    retry_badge = ""
    if retry_status == "ok":
        retry_badge = ' <span class="retry-badge retry-badge--ok" title="重试成功">✅ 已重试成功</span>'
    elif retry_status == "duplicate":
        retry_badge = ' <span class="retry-badge retry-badge--dup" title="重试发现内容重复">📎 内容重复</span>'
    elif retry_status == "error":
        retry_badge = ' <span class="retry-badge retry-badge--error" title="重试仍失败">⚠️ 重试仍失败</span>'
    retry_link = f' · <a href="/history/{esc(retry_history_id)}">查看重试记录</a>' if retry_history_id else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>历史详情 — 抖音收藏同步</title>
  <style>
    :root {{
      --bg: #f0f4f8; --surface: #ffffff; --border: #e2e8f0; --border-light: #f1f5f9;
      --text: #1e293b; --text-secondary: #64748b; --text-muted: #94a3b8;
      --primary: #6366f1; --primary-hover: #4f46e5; --primary-light: #eef2ff;
      --success: #10b981; --success-light: #ecfdf5; --success-border: #a7f3d0;
      --error: #ef4444; --error-light: #fef2f2; --error-border: #fecaca;
      --radius: 12px; --radius-sm: 8px; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
      --shadow-md: 0 4px 6px -1px rgba(0,0,0,.07), 0 2px 4px -2px rgba(0,0,0,.05);
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif; margin: 0; padding: 0; color: var(--text); background: var(--bg); line-height: 1.6; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 24px; }}
    a {{ color: var(--primary); text-decoration: none; transition: color .15s; }}
    a:hover {{ color: var(--primary-hover); text-decoration: underline; }}
    .back-link {{ display: inline-flex; align-items: center; gap: 6px; font-weight: 500; margin-bottom: 20px; padding: 8px 14px; border-radius: var(--radius-sm); background: var(--surface); border: 1px solid var(--border); transition: all .2s; }}
    .back-link:hover {{ border-color: var(--primary); background: var(--primary-light); text-decoration: none; }}
    h1 {{ font-size: 1.5rem; font-weight: 800; margin: 0 0 24px; letter-spacing: -0.02em; }}
    .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 20px; box-shadow: var(--shadow); }}
    .meta-grid {{ display: grid; grid-template-columns: 140px 1fr; gap: 12px 20px; }}
    .meta-label {{ color: var(--text-secondary); font-size: 0.85rem; font-weight: 500; padding-top: 2px; }}
    .meta-value {{ font-size: 0.9rem; word-break: break-all; }}
    .status-ok {{ color: var(--success); font-weight: 600; }}
    .status-error {{ color: var(--error); font-weight: 600; }}
    .retry-badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 500; white-space: nowrap; margin-left: 6px; }}
    .retry-badge--ok {{ background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }}
    .retry-badge--dup {{ background: #eff6ff; color: #1e40af; border: 1px solid #93c5fd; }}
    .retry-badge--error {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}
    .save-section {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .btn {{ font: inherit; padding: 10px 20px; border-radius: var(--radius-sm); border: none; cursor: pointer; font-weight: 600; font-size: 0.9rem; transition: all .2s; }}
    .btn--primary {{ background: var(--primary); color: white; }}
    .btn--primary:hover {{ background: var(--primary-hover); }}
    .btn--success {{ background: var(--success); color: white; }}
    .btn--success:hover {{ background: #059669; }}
    select {{ font: inherit; padding: 10px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: 0.9rem; }}
    select:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,.15); }}
    .hint {{ color: var(--text-secondary); font-size: 0.85rem; line-height: 1.5; margin: 0 0 12px; }}
    .save-result {{ color: var(--text-secondary); font-size: 0.85rem; margin-top: 8px; }}
    .section-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }}
    .section-header__icon {{ font-size: 1.2rem; }}
    .section-header__title {{ font-size: 1.1rem; font-weight: 700; margin: 0; }}
    code, pre {{ background: #0f172a; color: #e2e8f0; border-radius: var(--radius-sm); }}
    code {{ padding: 2px 6px; font-size: 0.85em; }}
    pre {{ padding: 20px; overflow: auto; font-size: 0.85rem; line-height: 1.55; }}
    @media (max-width: 640px) {{
      .meta-grid {{ grid-template-columns: 1fr; gap: 4px 0; }}
      .save-section {{ flex-direction: column; align-items: stretch; }}
    }}
  </style>
</head>
<body>
<main>
  <a href="/" class="back-link">← 返回历史列表</a>
  <h1>📋 历史详情</h1>

  <section class="card">
    <div class="meta-grid">
      <div class="meta-label">🆔 ID</div><div class="meta-value">{esc(event.get('id'))}</div>
      <div class="meta-label">🕐 时间</div><div class="meta-value">{esc(event.get('time'))}</div>
      <div class="meta-label">📌 类型</div><div class="meta-value">{esc(event.get('type'))}</div>
      <div class="meta-label">📊 状态</div><div class="meta-value {status_class}">{esc(effective_status)}{retry_badge}{retry_link}</div>
      <div class="meta-label">📝 摘要</div><div class="meta-value">{esc(event.get('summary'))}</div>
      <div class="meta-label">🔗 URL / 输入</div><div class="meta-value">{esc(event.get('url'))}</div>
      <div class="meta-label">📁 输出目录</div><div class="meta-value">{'<br>'.join(esc(p) for p in output_list) if output_list else '<span style="color:var(--text-muted)">无后端输出目录</span>'}</div>
      <div class="meta-label">📝 Obsidian</div><div class="meta-value">{esc(douyin_record.get('note_path') if douyin_record else details.get('obsidian', {}).get('note_path') if isinstance(details.get('obsidian'), dict) else '') or '<span style="color:var(--text-muted)">未保存</span>'}</div>
    </div>
  </section>

  <section class="card">
    <div class="section-header">
      <span class="section-header__icon">💾</span>
      <h2 class="section-header__title">保存到 Obsidian</h2>
    </div>
    <p class="hint">Web UI 可以保存非收藏夹分享链接到 Obsidian。重复内容默认跳过；需要更新已有笔记时可选择「覆盖已有」。</p>
    <div class="save-section">
      {save_button.replace('Save to Obsidian', '💾 保存到 Obsidian').replace("id='saveButton'", "id='saveButton' class='btn btn--success'") if save_button else ''}
      <select id="duplicatePolicy">
        <option value="skip">跳过重复</option>
        <option value="overwrite">覆盖已有</option>
        <option value="copy">创建副本</option>
      </select>
    </div>
    <p id="saveResult" class="save-result"></p>
  </section>

  <section class="card" id="retrySection" style="{'display:none' if not show_retry else ''}">
    <div class="section-header">
      <span class="section-header__icon">🔄</span>
      <h2 class="section-header__title">重新提取</h2>
    </div>
    <p class="hint">使用不同模式重新提取此链接。Heavy 模式会下载视频并转写音频。</p>
    <div class="save-section">
      <input id="retryUrl" type="hidden" value="{esc(retry_url)}" />
      <select id="retryMode">
        <option value="heavy">🎬 Heavy（默认）</option>
        <option value="light">⚡ Light</option>
      </select>
      <select id="retryWhisperModel" title="Whisper 转写模型（仅 Heavy）"></select>
      <button id="retryButton" class="btn btn--primary" type="button">🚀 开始提取</button>
    </div>
    <div id="retryProgress" style="display:none;margin-top:16px;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;">
      <div style="height:6px;background:var(--border-light);">
        <span id="retryBar" style="display:block;height:100%;width:0%;background:linear-gradient(90deg,var(--primary),#8b5cf6);border-radius:3px;transition:width .3s ease;"></span>
      </div>
      <div style="padding:14px 16px;">
        <div id="retryStage" style="font-weight:600;font-size:0.9rem;"></div>
        <div id="retryResult" style="color:var(--text-secondary);font-size:0.85rem;margin-top:4px;"></div>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="section-header">
      <span class="section-header__icon">📄</span>
      <h2 class="section-header__title">原始记录</h2>
    </div>
    <pre>{esc(pretty)}</pre>
  </section>
</main>
<script>
const saveButton = document.getElementById("saveButton");
if (saveButton) {{
  saveButton.addEventListener("click", async () => {{
    const result = document.getElementById("saveResult");
    saveButton.disabled = true;
    result.textContent = "⏳ 写入中…";
    try {{
      const response = await fetch(`/api/history/${{saveButton.dataset.eventId}}/save`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ duplicate_policy: document.getElementById("duplicatePolicy").value }})
      }});
      const data = await response.json();
      if (response.ok) {{
        result.innerHTML = `✅ ${{data.message || data.note_path || '保存成功'}}`;
      }} else {{
        result.innerHTML = `❌ ${{data.error || data.detail?.error || '保存失败'}}`;
      }}
    }} catch (err) {{
      result.innerHTML = `❌ ${{String(err)}}`;
    }} finally {{
      saveButton.disabled = false;
    }}
  }});
}}
// ── Retry extract ──
const retryButton = document.getElementById("retryButton");
if (retryButton) {{
  retryButton.addEventListener("click", async () => {{
    const retryUrl = document.getElementById("retryUrl").value;
    const retryMode = document.getElementById("retryMode").value;
    const retryModelSel = document.getElementById("retryWhisperModel");
    const retryModel = retryModelSel ? retryModelSel.value : getWebWhisperModel();
    const progress = document.getElementById("retryProgress");
    const bar = document.getElementById("retryBar");
    const stage = document.getElementById("retryStage");
    const result = document.getElementById("retryResult");
    retryButton.disabled = true;
    progress.style.display = "block";
    stage.textContent = "⏳ 提交任务…";
    result.textContent = "";
    try {{
      const resp = await fetch("/api/jobs/extract", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ url: retryUrl, mode: retryMode, model: retryModel }})
      }});
      const created = await resp.json();
      if (!resp.ok) throw new Error(created.detail?.error || JSON.stringify(created));
      const timer = setInterval(async () => {{
        const poll = await fetch(`/api/jobs/${{created.job_id}}`);
        const job = await poll.json();
        bar.style.width = `${{job.progress || 0}}%`;
        stage.textContent = `${{job.status === 'ok' ? '✅' : job.status === 'error' ? '❌' : '⏳'}} ${{job.stage || job.status}}`;
        result.innerHTML = job.error || job.summary || "";
        if (job.status === "ok" || job.status === "error") {{
          clearInterval(timer);
          retryButton.disabled = false;
          if (job.history_id) result.innerHTML += ` · <a href="/history/${{job.history_id}}">查看详情</a>`;
          // Mark original event with retry result
          const origId = "{esc(event.get('id', ''))}";
          if (origId) {{
            let rs2 = job.status;
            if (rs2 === "ok" && (job.obsidian?.duplicate || (job.summary || "").includes("duplicate"))) {{
              rs2 = "duplicate";
            }}
            fetch(`/api/history/${{origId}}/retry-mark`, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ retry_status: rs2, retry_history_id: job.history_id || "" }})
            }}).catch(() => {{}});
          }}
        }}
      }}, 1000);
    }} catch (err) {{
      retryButton.disabled = false;
      stage.textContent = "❌ 提取失败";
      result.textContent = String(err);
    }}
  }});
}}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def web_index(request: Request):
    return render_page(request)


@app.get("/history/{event_id}", response_class=HTMLResponse)
async def web_history_detail(event_id: str):
    event = HISTORY.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail={"error": "history_not_found"})
    return render_detail_page(event)


@app.get("/api/history")
async def history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    type: str = "",
    status: str = "",
    q: str = "",
):
    return HISTORY.query(page=page, page_size=page_size, type_filter=type, status_filter=status, q=q)


@app.get("/api/history/{event_id}")
async def history_detail(event_id: str):
    event = HISTORY.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail={"error": "history_not_found"})
    return event


@app.post("/api/history/{event_id}/save")
async def save_history_to_obsidian(event_id: str, payload: SaveHistoryRequest):
    event = HISTORY.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail={"error": "history_not_found"})
    details = dict(event.get("details") or {})
    if not details.get("out_dir"):
        douyin_id = str(details.get("douyin_id") or event.get("summary", "")).split(" ")[0]
        candidates = _find_output_dirs(douyin_id)
        if candidates:
            details["out_dir"] = candidates[0]
    result = _extract_result_from_details(details)
    try:
        save_result = await asyncio.to_thread(
            _save_result_to_obsidian,
            result,
            duplicate_policy=payload.duplicate_policy,
        )
    except Exception as exc:
        HISTORY.append(
            {
                "type": "save_obsidian",
                "status": "error",
                "summary": str(exc),
                "details": {"source_history_id": event_id, "error": str(exc)},
            }
        )
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    HISTORY.append(
        {
            "type": "save_obsidian",
            "status": "ok" if not save_result.get("error") else "error",
            "summary": save_result.get("message") or save_result.get("note_path") or str(save_result),
            "details": {"source_history_id": event_id, "obsidian": save_result},
        }
    )
    return save_result


@app.post("/api/history/{event_id}/retry-mark")
async def mark_retry_result(event_id: str, payload: RetryMarkRequest):
    found = HISTORY.mark_retry(
        event_id,
        retry_status=payload.retry_status,
        retry_history_id=payload.retry_history_id,
    )
    if not found:
        raise HTTPException(status_code=404, detail={"error": "history_not_found"})
    return {"success": True, "event_id": event_id, "retry_status": payload.retry_status}


@app.post("/api/history/{event_id}/retry-sync")
async def retry_sync_favorites(event_id: str):
    """在 Web UI 上重试一条失败的收藏夹同步：只拉 1 页验证可用性，不写 vault。"""
    event = HISTORY.get(event_id)
    if not event or event.get("type") != "sync_favorites":
        raise HTTPException(status_code=404, detail={"error": "event not found or not a sync_favorites record"})
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    known_ids = [str(x) for x in (details.get("known_ids") or [])]
    try:
        # 单发探测：不做退避连发（连发会刷新惩罚计时），也不等冷却
        items = await CLIENT.list_new_favorites(
            set(known_ids), max_items=10, page_size=10, max_pages=1,
            max_retries=0, respect_cooldown=False,
        )
        record = HISTORY.append(
            {
                "type": "sync_favorites",
                "status": "ok",
                "summary": _sync_summary(items),
                "details": {
                    "retried_from": event_id,
                    "returned": len(items),
                    "ids": [i.get("aweme_id") or i.get("id") for i in items],
                },
            }
        )
        HISTORY.mark_retry(event_id, retry_status="ok", retry_history_id=record["id"])
        return {"status": "ok", "returned": len(items), "summary": record["summary"], "retry_history_id": record["id"]}
    except AuthExpiredError as exc:
        record = HISTORY.append(
            {"type": "sync_favorites", "status": "error", "summary": "auth_expired", "details": {"retried_from": event_id}}
        )
        HISTORY.mark_retry(event_id, retry_status="error", retry_history_id=record["id"])
        raise HTTPException(status_code=401, detail={"error": "auth_expired"}) from exc
    except DouyinRequestError as exc:
        record = HISTORY.append(
            {"type": "sync_favorites", "status": "error", "summary": str(exc), "details": {"retried_from": event_id}}
        )
        HISTORY.mark_retry(event_id, retry_status="error", retry_history_id=record["id"])
        raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc


@app.delete("/api/history/{event_id}")
async def delete_history(event_id: str):
    found = HISTORY.delete(event_id)
    if not found:
        raise HTTPException(status_code=404, detail={"error": "history_not_found"})
    return {"success": True, "event_id": event_id}


@app.get("/api/content/{douyin_id}")
async def content_detail(douyin_id: str):
    record = CONTENT.get(douyin_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"error": "content_not_found"})
    return record


@app.post("/api/config/vault")
async def register_vault_config(payload: VaultConfigRequest):
    data = CONFIG.update(
        {
            "vault_path": payload.vault_path,
            "note_folder": payload.note_folder,
            "attachment_folder": payload.attachment_folder,
        }
    )
    for note in payload.known_notes:
        douyin_id = str(note.get("douyin_id") or "").strip()
        if douyin_id:
            CONTENT.upsert(
                douyin_id,
                {
                    "status": "saved",
                    "note_path": note.get("note_path"),
                    "source": "obsidian_register",
                },
            )
    HISTORY.append(
        {
            "type": "vault_config",
            "status": "ok",
            "summary": f"registered vault · {len(payload.known_notes)} known notes",
            "details": {
                "vault_path": payload.vault_path,
                "note_folder": payload.note_folder,
                "attachment_folder": payload.attachment_folder,
                "known_notes": len(payload.known_notes),
            },
        }
    )
    return {"success": True, "config": data, "known_notes": len(payload.known_notes)}


@app.post("/api/jobs/extract")
async def create_extract_job(payload: ExtractRequest):
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "type": f"web_extract_{payload.mode}",
        "status": "queued",
        "progress": 0,
        "stage": "排队中",
        "created_at": local_now(),
        "updated_at": local_now(),
    }
    asyncio.create_task(_run_extract_job(job_id, payload))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return job


@app.get("/api/health")
async def health():
    try:
        read_cookie(SETTINGS)
        auth = "cookie_present"
    except AuthExpiredError:
        auth = "missing_cookie"
    return {
        "success": True,
        "engine": "local",
        "auth": auth,
        "phase": 1,
        "capabilities": capabilities(),
        "local_time": local_now(),
    }


@app.get("/api/auth/qrcode")
async def auth_qrcode():
    try:
        return await CLIENT.create_qrcode_session()
    except DouyinRequestError as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc


@app.get("/api/auth/status")
async def auth_status(session_token: str):
    try:
        return await CLIENT.check_qrcode_session(session_token)
    except DouyinRequestError as exc:
        raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc


@app.post("/api/auth/cookie")
async def auth_cookie(payload: CookieRequest):
    save_cookie(SETTINGS, payload.cookie, source="manual")
    HISTORY.append({"type": "auth_cookie", "status": "ok", "summary": "manual cookie saved"})
    return {"success": True}


@app.post("/api/sync/favorites")
async def sync_favorites(payload: SyncFavoritesRequest):
    try:
        items = await CLIENT.list_new_favorites(
            set(payload.known_ids),
            max_items=payload.max,
            page_size=payload.page_size,
            max_pages=payload.max_pages,
        )
        HISTORY.append(
            {
                "type": "sync_favorites",
                "status": "ok",
                "summary": _sync_summary(items),
                "details": {
                    "returned": len(items),
                    "known_ids": payload.known_ids,
                    "ids": [item.get("aweme_id") for item in items],
                },
            }
        )
        return {"items": items}
    except AuthExpiredError as exc:
        HISTORY.append({"type": "sync_favorites", "status": "error", "summary": "auth_expired"})
        raise HTTPException(status_code=401, detail={"error": "auth_expired"}) from exc
    except DouyinRequestError as exc:
        HISTORY.append({"type": "sync_favorites", "status": "error", "summary": str(exc)})
        raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc


@app.post("/api/manual-import")
async def manual_import(payload: ManualImportRequest):
    result = _parse_share_text(payload.share_text)
    details = _extract_details(result, result.get("source", ""), "manual")
    details["mode"] = "manual"
    summary = f"手动导入 · {len(result['desc'])} 字 · {len(result['tags'])} 标签"
    save_result = None
    if payload.save_to_obsidian:
        try:
            save_result = await asyncio.to_thread(
                _save_result_to_obsidian, result, duplicate_policy=payload.duplicate_policy
            )
            details["obsidian"] = save_result
            if save_result.get("saved"):
                summary += f" · saved to {save_result.get('note_path')}"
        except Exception as exc:
            details["obsidian"] = {"error": str(exc)}
    record = HISTORY.append(
        {
            "type": "manual_import",
            "status": "ok",
            "summary": summary,
            "url": result.get("source", ""),
            "details": details,
        }
    )
    return {"result": result, "history_id": record["id"], "obsidian": save_result}


@app.post("/api/video/extract")
async def video_extract(payload: ExtractRequest):
    try:
        result, details, summary = await asyncio.to_thread(_run_extract, payload)
        if payload.save_to_obsidian:
            save_result = await asyncio.to_thread(
                _save_result_to_obsidian,
                result,
                duplicate_policy=payload.duplicate_policy,
            )
            details["obsidian"] = save_result
            if save_result.get("saved"):
                summary = f"{summary} · saved to Obsidian {save_result.get('note_path')}"
            elif save_result.get("duplicate"):
                summary = f"{summary} · duplicate skipped {save_result.get('note_path')}"
        HISTORY.append(
            {
                "type": f"extract_{payload.mode}",
                "status": "ok",
                "summary": summary,
                "url": payload.url,
                "details": details,
            }
        )
        return result
    except ExtractNotAvailableError as exc:
        HISTORY.append(
            {
                "type": f"extract_{payload.mode}",
                "status": "error",
                "summary": str(exc),
                "url": payload.url,
                "details": {"mode": payload.mode, "source_input": payload.url, "error": str(exc)},
            }
        )
        raise HTTPException(status_code=501, detail={"error": str(exc)}) from exc
    except Exception as exc:
        HISTORY.append(
            {
                "type": f"extract_{payload.mode}",
                "status": "error",
                "summary": str(exc),
                "url": payload.url,
                "details": {"mode": payload.mode, "source_input": payload.url, "error": str(exc)},
            }
        )
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc


@app.post("/web/sync", response_class=HTMLResponse)
async def web_sync(request: Request, known_ids: str = Form(default=""), max_items: int = Form(default=5)):
    ids = {i.strip() for i in known_ids.split(",") if i.strip()}
    try:
        items = await CLIENT.list_new_favorites(ids, max_items=max_items)
        HISTORY.append(
            {
                "type": "web_sync",
                "status": "ok",
                "summary": _sync_summary(items),
                "details": {
                    "returned": len(items),
                    "known_ids": sorted(ids),
                    "ids": [item.get("aweme_id") for item in items],
                },
            }
        )
        sample = ", ".join(str(i.get("aweme_id", "")) for i in items[:5])
        return render_page(request, f"Fetched {len(items)} item(s). {sample}")
    except Exception as exc:
        HISTORY.append({"type": "web_sync", "status": "error", "summary": str(exc)})
        return render_page(request, f"Sync failed: {exc}")


@app.post("/web/extract", response_class=HTMLResponse)
async def web_extract(
    request: Request,
    url: str = Form(default=""),
    mode: Literal["light", "heavy"] = Form(default="light"),
    model: Literal["tiny", "base", "small", "medium", "large-v2", "large-v3"] = Form(default="small"),
):
    if not url.strip():
        return render_page(request, "Missing URL.")
    payload = ExtractRequest(url=url, mode=mode, model=model)
    try:
        result, details, summary = await asyncio.to_thread(_run_extract, payload)
        record = HISTORY.append(
            {
                "type": f"web_extract_{mode}",
                "status": "ok",
                "summary": summary,
                "url": url,
                "details": details,
            }
        )
        return render_page(request, f"Extracted. See /history/{record['id']}")
    except Exception as exc:
        HISTORY.append(
            {
                "type": f"web_extract_{mode}",
                "status": "error",
                "summary": str(exc),
                "url": url,
                "details": {"mode": mode, "source_input": url, "error": str(exc)},
            }
        )
        return render_page(request, f"Extract failed: {exc}")
