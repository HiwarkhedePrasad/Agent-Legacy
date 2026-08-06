"""Speech I/O via OpenRouter (OpenAI-compatible endpoints).

- ``synthesize_speech`` : TTS  -> text in, raw audio bytes out
- ``transcribe``        : STT  -> audio bytes in, transcribed text out

Both reuse the same ``BASE_URL`` + ``API_KEY`` already configured for the LLMs,
so no extra provider or credentials are required. The current defaults point at
Fish Audio models (S2.1 Pro Free for TTS, Transcribe 1 for STT).
"""

from __future__ import annotations

import base64

import httpx

from agent.config import settings


def synthesize_speech(
    text: str,
    voice: str = "alloy",
    response_format: str = "mp3",
    model: str | None = None,
) -> bytes:
    """Convert ``text`` to raw audio bytes via OpenRouter's /audio/speech endpoint.

    ``response_format``: ``mp3`` for playback/files, ``pcm`` for low-latency
    streaming. If the configured (e.g. :free) model is throttled/errors, it
    falls back to the production `fish-audio/s2.1-pro` variant.
    """
    candidates = [
        model or settings.TTS_MODEL,
        "fish-audio/s2.1-pro",
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            resp = httpx.post(
                f"{settings.BASE_URL.rstrip('/')}/audio/speech",
                headers={
                    "Authorization": f"Bearer {settings.API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": candidate,
                    "input": text,
                    "voice": voice,
                    "response_format": response_format,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            # On success the response body is raw audio bytes (not JSON).
            return resp.content
        except httpx.HTTPStatusError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"TTS failed for all candidates: {last_error}")


def transcribe(
    audio_bytes: bytes,
    audio_format: str = "wav",
    model: str | None = None,
) -> str:
    """Transcribe ``audio_bytes`` to text via OpenRouter's /audio/transcriptions endpoint.

    Audio is sent base64-encoded (raw bytes, not a data URI). Keep recordings
    short -- the upstream provider enforces a 60s timeout.
    """
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    resp = httpx.post(
        f"{settings.BASE_URL.rstrip('/')}/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {settings.API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model or settings.STT_MODEL,
            "input_audio": {"data": audio_b64, "format": audio_format},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("text", "")
