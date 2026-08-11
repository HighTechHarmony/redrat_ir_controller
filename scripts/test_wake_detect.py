"""
Live wake-word test — runs the actual openWakeWord detector the service uses.

Opens the same AudioCapture device as the service (hw:2,0 @ 16 kHz mono),
runs the configured wake word model, and prints the detection score for
every frame so we can see whether "hey Jarvis" is being heard.

Run with the redrat service STOPPED (it holds the mic):
    sudo systemctl stop redrat
    .venv/bin/python scripts/test_wake_detect.py
    sudo systemctl start redrat

Say "hey Jarvis" while it runs. A score spike (>~0.5) proves the wake word
pipeline hears and detects you; a flat ~0 score means it does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice.audio import AudioCapture

MODEL = "hey_jarvis_v0.1"
DEVICE = "hw:2,0"


def main() -> None:
    print(f"Opening mic {DEVICE!r} at 16 kHz ...")
    audio = AudioCapture(device=DEVICE, maxqueue=200)
    audio.start()

    from openwakeword.model import Model
    print(f"Loading wake word model {MODEL!r} ...")
    model = Model(wakeword_models=[MODEL], vad_threshold=0.5)
    key = MODEL
    print(f"Model loaded.  Say \"hey Jarvis\" for up to 15 seconds ...\n")

    import time
    import numpy as np

    started = time.time()
    hop = 1280 // 2
    buf = np.array([], dtype=np.int16)
    while time.time() - started < 15:
        try:
            frame = audio.queue.get(timeout=0.5)
        except Exception:
            continue
        buf = np.concatenate((buf, frame.astype(np.int16, copy=False)))
        while buf.shape[0] >= hop:
            window = buf[:hop]
            buf = buf[hop:]
            pred = model.predict(window)
            score = float(pred[key]) if key in pred else 0.0
            elapsed = time.time() - started
            bar = "#" * int(score * 50)
            print(f"t={elapsed:5.1f}s  score={score:.3f} {bar}")
            if score > 0.5:
                print("\n>>> WAKE WORD DETECTED <<<\n")

    audio.stop()
    print("\nTest complete.")


if __name__ == "__main__":
    main()
