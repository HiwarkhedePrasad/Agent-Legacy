"""Unit tests for the OpenRouter speech module (no live API calls)."""

import httpx
import pytest

from agent.config import settings
from agent.services.speech import synthesize_speech, transcribe


def test_synthesize_speech_returns_bytes(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)

        class Resp:
            raise_for_status = staticmethod(lambda: None)
            content = b"\x00audio\xff"

        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = synthesize_speech("Hello world")
    assert out == b"\x00audio\xff"
    assert sent["url"].endswith("/audio/speech")
    assert sent["json"]["model"] == settings.TTS_MODEL
    assert sent["json"]["input"] == "Hello world"
    assert sent["json"]["response_format"] == "mp3"
    assert sent["headers"]["Authorization"].startswith("Bearer")


def test_transcribe_returns_text(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)

        class Resp:
            raise_for_status = staticmethod(lambda: None)

            def json(self):
                return {"text": "hello there"}

        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = transcribe(b"\x00wavdata")
    assert out == "hello there"
    assert sent["url"].endswith("/audio/transcriptions")
    assert sent["json"]["model"] == settings.STT_MODEL
    # Audio must be base64-encoded raw bytes.
    assert isinstance(sent["json"]["input_audio"]["data"], str)
    assert sent["json"]["input_audio"]["format"] == "wav"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
