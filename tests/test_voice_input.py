"""
Tests for the push-to-talk voice-input endpoint (POST /app1/voice).

These run under plain unittest with NO live OpenAI call and NO real app boot:
we register the actual `app1.voice` view function on a tiny throwaway Flask app
(mirroring tests/test_auth_decorators.py) and monkeypatch the shared OpenAI
client (`ai_runtime._openai_client`) with a fake whose audio.transcriptions
returns a canned transcript. We assert the happy path returns {text, ok:true}
and that the guarded failure modes (missing key, empty upload, oversize,
feature-disabled) return the documented Danish error JSON.
"""

import io
import os
import sys
import unittest

from flask import Flask

# Make the project root importable when run from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app1  # noqa: E402
import ai_runtime  # noqa: E402


class _FakeResult:
    def __init__(self, text):
        self.text = text


class _FakeTranscriptions:
    """Stand-in for client.audio.transcriptions; records the kwargs it got."""

    def __init__(self, text="Hej, jeg leder efter et kursus i projektledelse."):
        self._text = text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResult(self._text)


class _FakeAudio:
    def __init__(self, transcriptions):
        self.transcriptions = transcriptions


class _FakeClient:
    def __init__(self, transcriptions):
        self.audio = _FakeAudio(transcriptions)


def _build_app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    # Register the REAL view function under the same endpoint name it has in the
    # app1 blueprint, so we exercise the production code path, not a copy.
    app.add_url_rule("/app1/voice", endpoint="app1.voice",
                     view_func=app1.voice, methods=["POST"])
    return app


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.client = _build_app().test_client()
        # Snapshot env / monkeypatched globals so each test is isolated.
        self._orig_client = ai_runtime._openai_client
        self._orig_key = os.environ.get("OPENAI_API_KEY")
        self._orig_flag = os.environ.get("VOICE_INPUT_ENABLED")
        self.fake_transcriptions = _FakeTranscriptions()
        ai_runtime._openai_client = lambda: _FakeClient(self.fake_transcriptions)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ.pop("VOICE_INPUT_ENABLED", None)

    def tearDown(self):
        ai_runtime._openai_client = self._orig_client
        if self._orig_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._orig_key
        if self._orig_flag is None:
            os.environ.pop("VOICE_INPUT_ENABLED", None)
        else:
            os.environ["VOICE_INPUT_ENABLED"] = self._orig_flag

    def _post_audio(self, payload=b"fakeaudiobytes", filename="rec.webm"):
        return self.client.post(
            "/app1/voice",
            data={"audio": (io.BytesIO(payload), filename)},
            content_type="multipart/form-data",
        )

    def test_happy_path_returns_text_and_ok(self):
        resp = self._post_audio()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        # Whitespace is stripped from the transcript.
        self.assertEqual(body["text"], "Hej, jeg leder efter et kursus i projektledelse.")
        # Danish language hint and whisper-1 model are passed through.
        kw = self.fake_transcriptions.last_kwargs
        self.assertEqual(kw["language"], "da")
        self.assertEqual(kw["model"], "whisper-1")
        # The uploaded file is forwarded with a name so OpenAI can infer format.
        self.assertTrue(hasattr(kw["file"], "name"))

    def test_raw_body_audio_is_accepted(self):
        resp = self.client.post(
            "/app1/voice", data=b"rawaudio", content_type="audio/webm"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_empty_upload_returns_guarded_error(self):
        resp = self.client.post("/app1/voice", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["text"], "")
        self.assertTrue(body["error"])  # Danish, user-safe message present.

    def test_missing_api_key_degrades_gracefully(self):
        os.environ.pop("OPENAI_API_KEY", None)
        resp = self._post_audio()
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["text"], "")
        self.assertIn("Skriv", body["error"])  # nudges the user to type instead.

    def test_oversize_upload_is_rejected(self):
        from app1 import VOICE_MAX_BYTES
        big = b"x" * (VOICE_MAX_BYTES + 1)
        resp = self._post_audio(payload=big)
        self.assertEqual(resp.status_code, 413)
        body = resp.get_json()
        self.assertFalse(body["ok"])

    def test_feature_disabled_returns_clean_response(self):
        os.environ["VOICE_INPUT_ENABLED"] = "0"
        resp = self._post_audio()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["text"], "")

    def test_transcription_error_is_caught(self):
        class _Boom(_FakeTranscriptions):
            def create(self, **kwargs):
                raise RuntimeError("whisper exploded")

        self.fake_transcriptions = _Boom()
        ai_runtime._openai_client = lambda: _FakeClient(self.fake_transcriptions)
        resp = self._post_audio()
        self.assertEqual(resp.status_code, 502)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["text"], "")

    def test_blank_transcript_returns_no_speech_message(self):
        self.fake_transcriptions = _FakeTranscriptions(text="   ")
        ai_runtime._openai_client = lambda: _FakeClient(self.fake_transcriptions)
        resp = self._post_audio()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["text"], "")


if __name__ == "__main__":
    unittest.main()
