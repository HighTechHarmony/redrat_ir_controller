"""
RedRat IR Controller — entry point.

Starts the Flask web API in a daemon thread and runs the voice command
pipeline in the main thread.  Both share the same RedRatDevice,
SignalStore, MacroExecutor, and VoiceCommandStore instances.

Usage:
    python main.py [--config path/to/config.yaml]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="RedRat IR Controller")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml (default: config/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # ---------------------------------------------------------------
    # Load configuration
    # ---------------------------------------------------------------
    cfg = load_config(args.config)

    flask_cfg = cfg.get("flask", {})
    storage_cfg = cfg.get("storage", {})
    voice_cfg = cfg.get("voice", {})
    redrat_cfg = cfg.get("redrat", {})

    # ---------------------------------------------------------------
    # RedRat3 device
    # ---------------------------------------------------------------
    backend = redrat_cfg.get("backend", "usb").lower()

    if backend == "lirc":
        from redrat.lirc_device import LircDevice, LircError as _DeviceError
        lirc_path = redrat_cfg.get("lirc_path", "/dev/lirc0")
        try:
            device = LircDevice.open_first(path=lirc_path)
            log.info(
                "Connected to LIRC device: %s firmware=%s",
                device.get_serial_number(),
                device.get_firmware_version(),
            )
        except LircError as exc:
            log.error("Could not open LIRC device %s: %s", lirc_path, exc)
            sys.exit(1)
    else:
        from redrat.device import RedRatDevice, RedRatError as _DeviceError
        serial = redrat_cfg.get("serial_number") or None
        try:
            device = RedRatDevice.open_first(serial_number=serial)
            log.info(
                "Connected to RedRat3: serial=%s firmware=%s",
                device.get_serial_number(),
                device.get_firmware_version(),
            )
        except RedRatError as exc:
            log.error("Could not open RedRat3: %s", exc)
            sys.exit(1)

    # ---------------------------------------------------------------
    # Stores & executor
    # ---------------------------------------------------------------
    from redrat.store import SignalStore
    from macros.executor import MacroExecutor
    from voice.store import VoiceCommandStore

    signal_store = SignalStore(storage_cfg.get("ir_codes", "config/ir_codes.yaml"))
    voice_store = VoiceCommandStore(storage_cfg.get("voice_commands", "config/voice_commands.yaml"))
    macro_executor = MacroExecutor(
        macro_path=storage_cfg.get("macros", "config/macros.yaml"),
        signal_store=signal_store,
        device=device,
    )

    # ---------------------------------------------------------------
    # Voice pipeline
    # ---------------------------------------------------------------
    from voice.audio import AudioCapture
    from voice.wake_word import WakeWordDetector
    from voice.stt import SpeechRecognizer
    from voice.command_matcher import CommandMatcher

    voice_status: dict = {"state": "starting"}

    # Debug flags for audio/wake logging (set in config/config.yaml)
    debug_wake = bool(voice_cfg.get("debug_wake", False))
    wake_log_every = int(voice_cfg.get("wake_log_every", 50))
    debug_audio = bool(voice_cfg.get("debug_audio", False))
    audio_log_every = int(voice_cfg.get("audio_log_every", 100))

    audio = AudioCapture(
        device=voice_cfg.get("alsa_device", "default"),
    )
    # Apply debug settings after construction to avoid changing constructor
    # signature if present in older installs.
    try:
        audio._debug = debug_audio
        audio._enqueue_log_every = audio_log_every
    except Exception:
        pass

    def run_macro_safe(name: str) -> None:
        """Wrapper so the STT thread can run macros without crashing."""
        try:
            macro_executor.run(name)
        except Exception as exc:
            log.error("Macro %r failed: %s", name, exc)

    matcher = CommandMatcher(
        get_phrase_map=voice_store.phrase_to_macro,
        run_macro=run_macro_safe,
        threshold=float(voice_cfg.get("command_match_threshold", 70)),
    )

    recognizer = SpeechRecognizer(
        model_path=voice_cfg.get("vosk_model_path", "models/vosk-model-small-en-us-0.15"),
        audio_queue=audio.queue,
        wake_event=threading.Event(),  # will be replaced below after detector init
        rebuild_event=voice_store.rebuild_event,
        get_phrases=voice_store.all_phrases,
        on_transcript=matcher.handle,
        command_timeout_s=float(voice_cfg.get("command_timeout_s", 5)),
    )

    detector = WakeWordDetector(
        model_name=voice_cfg.get("wake_word_model", "hey_jarvis_v0.1"),
        audio_queue=audio.queue,
        threshold=float(voice_cfg.get("wake_word_threshold", 0.5)),
        log_scores=debug_wake,
        log_every=wake_log_every,
    )
    # Enable a short beep on wake using the configured ALSA device (if present)
    try:
        detector = WakeWordDetector(
            model_name=voice_cfg.get("wake_word_model", "hey_jarvis_v0.1"),
            audio_queue=audio.queue,
            threshold=float(voice_cfg.get("wake_word_threshold", 0.5)),
            log_scores=debug_wake,
            log_every=wake_log_every,
            beep_on_wake=bool(voice_cfg.get("beep_on_wake", True)),
            beep_device=voice_cfg.get("alsa_device", None),
            beep_freq=int(voice_cfg.get("beep_freq_hz", 800)),
            beep_duration_s=float(voice_cfg.get("beep_duration_s", 0.5)),
        )
    except TypeError:
        # Fallback for older installs: construct without beep args
        detector = WakeWordDetector(
            model_name=voice_cfg.get("wake_word_model", "hey_jarvis_v0.1"),
            audio_queue=audio.queue,
            threshold=float(voice_cfg.get("wake_word_threshold", 0.5)),
            log_scores=debug_wake,
            log_every=wake_log_every,
        )

    # Wire the wake event from the detector into the recognizer
    recognizer._wake_event = detector.wake_event
    # Wire listening_event so STT can suppress wake beep while active
    recognizer._listening_event = detector.listening_event
    # Provide recognizer with beep playback settings for timeout tone.
    recognizer._beep_device = voice_cfg.get("alsa_device", None)
    recognizer._beep_freq = int(voice_cfg.get("beep_freq_hz", 800))
    recognizer._beep_duration_s = float(voice_cfg.get("beep_duration_s", 0.5))
    # Expose recognizer status to the API
    voice_status = recognizer.status

    # ---------------------------------------------------------------
    # Flask API
    # ---------------------------------------------------------------
    from api.server import create_app

    flask_app = create_app(
        device=device,
        signal_store=signal_store,
        macro_executor=macro_executor,
        voice_store=voice_store,
        voice_status=voice_status,
    )

    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host=flask_cfg.get("host", "0.0.0.0"),
            port=int(flask_cfg.get("port", 5000)),
            debug=bool(flask_cfg.get("debug", False)),
            use_reloader=False,   # reloader is incompatible with daemon threads
        ),
        daemon=True,
        name="flask",
    )

    # ---------------------------------------------------------------
    # Graceful shutdown
    # ---------------------------------------------------------------
    shutdown_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Shutdown signal received — stopping ...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ---------------------------------------------------------------
    # Start everything
    # ---------------------------------------------------------------
    log.info("Starting Flask API on %s:%s", flask_cfg.get("host", "0.0.0.0"), flask_cfg.get("port", 5000))
    flask_thread.start()

    log.info("Starting audio capture ...")
    audio.start()

    log.info("Starting wake word detector ...")
    detector.start()

    log.info("Starting speech recognizer ...")
    recognizer.start()

    log.info("RedRat IR Controller running.  Press Ctrl-C to stop.")
    shutdown_event.wait()

    # ---------------------------------------------------------------
    # Teardown
    # ---------------------------------------------------------------
    recognizer.stop()
    detector.stop()
    audio.stop()
    device.close()
    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
