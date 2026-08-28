from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


DuplicatePolicy = Literal["skip", "overwrite", "copy"]


@dataclass
class VaultConfig:
    vault_path: Path
    note_folder: str
    attachment_folder: str


def sanitize_filename_segment(text: str, max_len: int = 48) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t#]', "", text)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-").strip()
    if not cleaned:
        return "untitled"
    return cleaned[:max_len].rstrip("-") if len(cleaned) > max_len else cleaned


def escape_yaml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def normalize_vault_path(path: str) -> str:
    return "/".join(part for part in path.replace("\\", "/").split("/") if part)


def title_from_desc(desc: str, fallback: str) -> str:
    first_line = (desc.splitlines() or [""])[0].strip()
    without_tags = re.sub(r"#[^\s#]+", "", first_line or fallback)
    without_tags = re.sub(r"\s+", " ", without_tags).strip()
    return without_tags or fallback


def find_existing_note(vault_path: Path, douyin_id: str) -> str | None:
    if not douyin_id or not vault_path.exists():
        return None
    pattern = re.compile(rf'(?:^|\n)douyin_id:\s*["\']?{re.escape(douyin_id)}["\']?(?:\n|$)')
    for path in vault_path.rglob("*.md"):
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                return str(path.relative_to(vault_path))
        except OSError:
            continue
    return None


def unique_note_path(vault_path: Path, base_path: str) -> Path:
    candidate = vault_path / f"{base_path}.md"
    if not candidate.exists():
        return candidate
    for index in range(2, 100):
        candidate = vault_path / f"{base_path}-{index}.md"
        if not candidate.exists():
            return candidate
    return vault_path / f"{base_path}-{int(candidate.stat().st_mtime)}.md"


def copy_attachment(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def write_obsidian_note(
    config: VaultConfig,
    result: dict[str, Any],
    *,
    duplicate_policy: DuplicatePolicy = "skip",
) -> dict[str, Any]:
    vault_path = config.vault_path.expanduser().resolve()
    if not vault_path.exists() or not vault_path.is_dir():
        raise ValueError(f"Vault path does not exist: {vault_path}")

    douyin_id = str(result.get("douyin_id") or "").strip()
    if not douyin_id:
        raise ValueError("extract result has no douyin_id")

    existing_note = find_existing_note(vault_path, douyin_id)
    if existing_note and duplicate_policy == "skip":
        return {
            "saved": False,
            "duplicate": True,
            "note_path": existing_note,
            "message": "already exists",
        }

    author = str(result.get("author") or "未知")
    desc = str(result.get("desc") or "")
    title = title_from_desc(desc, douyin_id)
    source = str(result.get("source") or "")
    tags = [str(tag) for tag in (result.get("tags") or [])]
    content_type = str(result.get("content_type") or ("image" if result.get("images") else "video"))
    note_folder = normalize_vault_path(config.note_folder)
    attachment_folder = normalize_vault_path(config.attachment_folder)
    attach_base = normalize_vault_path(f"{attachment_folder}/{douyin_id}")

    base_name = "-".join(
        [
            sanitize_filename_segment(author, 24),
            sanitize_filename_segment(title, 56),
        ]
    )
    base_path = normalize_vault_path(f"{note_folder}/{base_name}")
    if existing_note and duplicate_policy == "overwrite":
        note_path = vault_path / existing_note
    else:
        note_path = unique_note_path(vault_path, base_path)
    note_path.parent.mkdir(parents=True, exist_ok=True)

    fm = [
        "---",
        "type: douyin",
        f"content_type: {content_type}",
        f'douyin_id: "{escape_yaml(douyin_id)}"',
        f'author: "{escape_yaml(author)}"',
        f'source: "{escape_yaml(source)}"',
        f'create_time: "{escape_yaml(str(result.get("create_time") or ""))}"',
        "tags:",
        "  - douyin",
        *[f"  - {escape_yaml(tag)}" for tag in tags],
        "---",
        "",
    ]

    body = [f"# {title}", ""]
    out_dir = Path(str(result.get("out_dir") or ""))
    if content_type == "video":
        video_src = out_dir / "video.mp4"
        video_vault = normalize_vault_path(f"{attach_base}/video.mp4")
        if copy_attachment(video_src, vault_path / video_vault):
            body.extend([f"![[{video_vault}]]", ""])
        elif result.get("video_url"):
            body.extend([f"[无水印视频链接]({result['video_url']})", ""])
    elif result.get("video_url"):
        body.extend([f"[原始链接]({result['video_url']})", ""])

    local_images = [Path(str(path)) for path in (result.get("images") or []) if str(path).startswith("/")]
    if local_images:
        body.extend(["## 配图", ""])
        for image_path in local_images:
            image_vault = normalize_vault_path(f"{attach_base}/{image_path.name}")
            if copy_attachment(image_path, vault_path / image_vault):
                body.append(f"![[{image_vault}]]")
        body.append("")
    elif result.get("images"):
        body.extend(["## 配图", "", *[f"![]({url})" for url in result.get("images", [])], ""])

    body.extend(["## 文案", "", desc or "(无文案)", ""])
    if result.get("transcript"):
        body.extend(["## 转写", "", str(result["transcript"]), ""])

    note_path.write_text("\n".join(fm + body), encoding="utf-8")
    return {
        "saved": True,
        "duplicate": bool(existing_note),
        "note_path": str(note_path.relative_to(vault_path)),
        "absolute_note_path": str(note_path),
    }
