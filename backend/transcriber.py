"""Self-contained Whisper transcription — no external project dependency.

Uses faster_whisper + zhconv to transcribe audio/video files.
"""
from __future__ import annotations

from pathlib import Path


def transcribe_media(
    media_path: Path,
    *,
    model_name: str = "base",
    language: str = "zh",
) -> str:
    """Transcribe *media_path* (audio or video) to Simplified Chinese text.

    Returns the full transcript string.
    Raises ``ImportError`` if faster_whisper / zhconv are not installed.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster_whisper is required for heavy mode. "
            "Run: pip install faster-whisper"
        ) from exc

    try:
        import zhconv
    except ImportError as exc:
        raise ImportError(
            "zhconv is required for heavy mode. Run: pip install zhconv"
        ) from exc

    if not media_path.exists():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        str(media_path),
        language=language,
        vad_filter=True,
        beam_size=5,
    )

    parts: list[str] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            parts.append(zhconv.convert(text, "zh-cn"))

    return "\n".join(parts).strip()
