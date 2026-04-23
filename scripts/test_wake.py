from __future__ import annotations

import time
import logging
import sys
from pathlib import Path

import yaml

# Ensure repo root is on sys.path when running as a script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice.audio import AudioCapture
from voice.wake_word import WakeWordDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("test_wake")


def _load_voice_cfg() -> dict:
    cfg_path = ROOT / "config" / "config.yaml"
    try:
        with cfg_path.open() as f:
            return (yaml.safe_load(f) or {}).get("voice", {})
    except Exception:
        return {}


def main():
    vcfg = _load_voice_cfg()
    device = vcfg.get("alsa_device") or None
    model_name = vcfg.get("wake_word_model", "hey_jarvis_v0.1")
    threshold = float(vcfg.get("wake_word_threshold", 0.3))

    log.info("Using device=%r model=%r threshold=%.2f", device, model_name, threshold)

    audio = AudioCapture(device=device, maxqueue=100)
    audio._debug = True
    audio._enqueue_log_every = 10
    audio.start()

    detector = WakeWordDetector(
        model_name=model_name,
        audio_queue=audio.queue,
        threshold=threshold,
        cooldown_s=1.0,
        log_scores=True,
        log_every=5,
    )
    detector.start()

    log_end = time.time() + 20
    try:
        log.info("Running wake detector for 20s; say the wake word now")
        while time.time() < log_end:
            if detector.wake_event.is_set():
                log.info("Wake event received!")
                detector.wake_event.clear()
            time.sleep(0.1)
    finally:
        detector.stop()
        audio.stop()


if __name__ == "__main__":
    main()
