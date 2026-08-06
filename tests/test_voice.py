"""Tests for live voice playback (winsound). No audio hardware required — mocked.

Runnable directly:   python tests/test_voice.py
Also collected by:   python -m pytest tests/test_voice.py -v
"""

import asyncio
import winsound

import agent.services.voice as voice


class MP:
    """Minimal monkeypatch shim (mirrors pytest's monkeypatch API)."""

    def __init__(self):
        self._patches = []

    def setattr(self, obj, name, value):
        had = hasattr(obj, name)
        old = getattr(obj, name) if had else None
        setattr(obj, name, value)
        self._patches.append((obj, name, old, had))

    def undo(self):
        for obj, name, old, had in self._patches:
            if had:
                setattr(obj, name, old)
            else:
                delattr(obj, name)


def test_speak_line_plays_wav(monkeypatch):
    played = []

    def fake_synthesize(text, voice="alloy", response_format="mp3", model=None):
        assert response_format == "wav"  # must request wav for in-memory playback
        return b"RIFF....WAVEfakeaudio"

    def fake_playsound(data, flags):
        played.append((data, flags))

    monkeypatch.setattr(voice, "synthesize_speech", fake_synthesize)
    monkeypatch.setattr(winsound, "PlaySound", fake_playsound)

    voice.speak_line("Hello there")
    assert played and played[0][0] == b"RIFF....WAVEfakeaudio"
    assert played[0][1] & winsound.SND_MEMORY


def test_play_audio_async_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(winsound, "PlaySound", lambda data, flags: seen.append(flags))
    voice.play_audio(b"RIFF....WAVEx", audio_format="wav", async_play=True)
    assert seen[0] & winsound.SND_ASYNC


async def _fake_run_agent(user_input, session_id="default"):
    yield {"type": "agent", "name": "orchestrator", "message": "Let me check this first."}
    yield {"type": "status", "step": 1, "text": "Searching the web now.", "agent": "orchestrator"}
    yield {"type": "complete", "final": "Done", "artifacts": []}


def test_driver_speaks_narration(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "run_agent", _fake_run_agent)
    monkeypatch.setattr(voice, "speak_line", lambda text, voice="alloy": spoken.append(text))

    collected = []

    async def run():
        async for ev in voice.speak_agent_run("hi"):
            collected.append(ev)

    asyncio.run(run())
    assert "Let me check this first." in spoken
    assert "Searching the web now." in spoken
    assert any(ev["type"] == "complete" for ev in collected)
    # Events pass through unchanged for other consumers (UI / relay).
    assert collected and collected[0]["type"] == "agent"


def main() -> None:
    mp = MP()
    try:
        test_speak_line_plays_wav(mp)
        test_play_audio_async_flag(mp)
        test_driver_speaks_narration(mp)
    finally:
        mp.undo()
    print("[OK] voice playback tests passed (mocked, no audio device needed)")


if __name__ == "__main__":
    main()
