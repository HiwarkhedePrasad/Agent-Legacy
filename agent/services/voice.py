"""Live voice output for the agent.

The orchestrator already narrates each step in plain English. Instead of saving
synthesized audio to disk, this module PLAYS each narration line as soon as its
audio bytes are received.

Playback uses the Windows standard-library ``winsound`` module, which plays a
WAV from an in-memory bytes buffer — no third-party audio package or install
required. OpenRouter TTS is therefore requested in ``wav`` format for playback.
"""

from __future__ import annotations

import platform
from typing import AsyncGenerator

from agent.services.runner import run_agent
from agent.services.speech import synthesize_speech

_SENTENCE_END = (". ", "! ", "? ", "\n")
_NARRATION_TYPES = ("agent", "status")


def play_audio(audio_bytes: bytes, audio_format: str = "wav", async_play: bool = False) -> None:
    """Play TTS audio bytes immediately, in memory (blocks until done by default)."""
    if not audio_bytes:
        return
    if platform.system() != "Windows":
        raise RuntimeError(
            "Instant playback uses the Windows stdlib 'winsound' module. On other "
            "platforms, save the audio and open it with a system player instead."
        )
    if audio_format.lower() != "wav":
        raise ValueError(
            "Instant playback requires WAV audio. Call synthesize_speech() with "
            "response_format='wav', or convert to WAV first."
        )
    import winsound  # Windows-only stdlib module

    flags = winsound.SND_MEMORY
    if async_play:
        flags |= winsound.SND_ASYNC
    try:
        winsound.PlaySound(audio_bytes, flags)
    except (RuntimeError, TypeError) as exc:
        raise RuntimeError(
            f"Audio playback failed ({exc}). Ensure a default audio device is "
            "available and the data is a valid WAV."
        ) from exc


def speak_line(text: str, voice: str = "alloy", async_play: bool = False) -> None:
    """Synthesize one narration line as WAV and play it immediately (not saved)."""
    audio = synthesize_speech(text, voice=voice, response_format="wav")
    play_audio(audio, audio_format="wav", async_play=async_play)


def _flush_sentences(buf: str, voice: str) -> str:
    """Speak complete sentences accumulated in ``buf``; return the leftover."""
    ends = [(sep, buf.find(sep)) for sep in _SENTENCE_END if sep in buf]
    if not ends:
        return buf
    sep, pos = min(ends, key=lambda t: t[1])
    sentence, buf = buf[: pos + len(sep)], buf[pos + len(sep):]
    if sentence.strip():
        speak_line(sentence.strip(), voice=voice)
    return buf


async def speak_agent_run(
    user_input: str,
    session_id: str = "default",
    voice: str = "alloy",
    *,
    speak_narration: bool = True,
    speak_tokens: bool = False,
) -> AsyncGenerator[dict, None]:
    """Run the agent and SPEAK each narration line aloud as it is received.

    A thin, side-effecting passthrough over ``run_agent()``: every event is
    re-yielded unchanged for other consumers (UI, relay, etc.) while narration
    lines (and optionally the streamed final answer) are synthesized and played
    immediately — nothing is written to disk.
    """
    token_buf = ""
    async for event in run_agent(user_input, session_id):
        etype = event.get("type")
        if speak_tokens and etype == "token":
            token_buf = _flush_sentences(token_buf + (event.get("content") or ""), voice=voice)
        elif speak_narration and etype in _NARRATION_TYPES:
            text = event.get("message") or event.get("text")
            if text:
                speak_line(text, voice=voice)
        yield event
    if speak_tokens and token_buf.strip():
        speak_line(token_buf.strip(), voice=voice)
