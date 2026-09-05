from types import SimpleNamespace

import pytest

from api.server import create_app
from voice.tts import TextToSpeech, TtsPlaybackError, TtsTimeoutError, TtsUnavailableError


class FakeRunner:
    def __init__(self, result=None, error=None):
        self.result = result or SimpleNamespace(returncode=0)
        self.error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.result


def make_client(tts):
    app = create_app(
        device=None,
        signal_store=None,
        macro_executor=None,
        voice_store=None,
        voice_status={},
        tts=tts,
    )
    app.config["TESTING"] = True
    return app.test_client()


def test_tts_uses_configured_device_and_single_text_argument():
    runner = FakeRunner()
    tts = TextToSpeech(device="hw:2,0", runner=runner)

    tts.speak("hello; $(touch unsafe)")

    command = runner.calls[0][0][0]
    assert command == ["espeak-ng", "-d", "hw:2,0", "hello; $(touch unsafe)"]
    assert runner.calls[0][1] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 30.0,
    }


def test_speak_endpoint_trims_text_and_returns_success():
    runner = FakeRunner()
    client = make_client(TextToSpeech(runner=runner))

    response = client.post("/api/voice/speak", json={"text": "  Hello there  "})

    assert response.status_code == 200
    assert response.get_json() == {"spoken": True, "text": "Hello there"}
    assert runner.calls[0][0][0][-1] == "Hello there"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (None, "Request body must be a JSON object"),
        ({}, "'text' must be a string"),
        ({"text": 42}, "'text' must be a string"),
        ({"text": "   "}, "'text' must not be empty"),
        ({"text": "x" * 501}, "'text' must be 500 characters or fewer"),
    ],
)
def test_speak_endpoint_rejects_invalid_text(body, message):
    runner = FakeRunner()
    client = make_client(TextToSpeech(runner=runner))

    response = client.post("/api/voice/speak", json=body)

    assert response.status_code == 400
    assert response.get_json() == {"error": message}
    assert runner.calls == []


def test_speak_endpoint_reports_missing_executable():
    client = make_client(TextToSpeech(runner=FakeRunner(error=FileNotFoundError())))

    response = client.post("/api/voice/speak", json={"text": "Hello"})

    assert response.status_code == 503
    assert response.get_json() == {"error": "espeak-ng is not installed"}


@pytest.mark.parametrize(
    "error",
    [TtsPlaybackError("speech playback failed"), TtsTimeoutError("speech playback timed out")],
)
def test_speak_endpoint_reports_playback_failures(error):
    client = make_client(TextToSpeech(runner=FakeRunner(error=error)))

    response = client.post("/api/voice/speak", json={"text": "Hello"})

    assert response.status_code == 502
    assert response.get_json() == {"error": str(error)}


def test_speak_endpoint_reports_nonzero_exit():
    runner = FakeRunner(result=SimpleNamespace(returncode=1))
    client = make_client(TextToSpeech(runner=runner))

    response = client.post("/api/voice/speak", json={"text": "Hello"})

    assert response.status_code == 502
    assert response.get_json() == {"error": "speech playback failed"}


def test_speak_endpoint_reports_stderr_playback_failures():
    runner = FakeRunner(result=SimpleNamespace(returncode=0, stderr="Device or resource busy"))
    client = make_client(TextToSpeech(runner=runner))

    response = client.post("/api/voice/speak", json={"text": "Hello"})

    assert response.status_code == 502
    assert response.get_json() == {"error": "speech playback failed"}


def test_speak_endpoint_is_unavailable_without_tts_service():
    client = make_client(None)

    response = client.post("/api/voice/speak", json={"text": "Hello"})

    assert response.status_code == 503
    assert response.get_json() == {"error": "Text-to-speech is unavailable"}
