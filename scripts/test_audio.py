from __future__ import annotations

import time
import logging

import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice.audio import AudioCapture, FRAME_SAMPLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("test_audio")


def main():
    a = AudioCapture(device=None, maxqueue=10)
    # enable debug logging
    a._debug = True
    a._enqueue_log_every = 1
    a.start()
    log.info("Started audio capture; reading 10 frames (timeout 10s)")
    got = 0
    start = time.time()
    try:
        while got < 10 and (time.time() - start) < 10:
            try:
                frame = a.queue.get(timeout=1.0)
            except Exception:
                log.info("No frame available yet")
                continue
            log.info("Got frame %d length=%d", got + 1, len(frame))
            got += 1
    finally:
        a.stop()
        log.info("Stopped")


if __name__ == "__main__":
    main()
