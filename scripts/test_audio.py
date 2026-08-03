"""
Smoke test for the audio capture pipeline (voice.audio.AudioCapture).

Purpose
-------
Verify that the microphone can be opened and that audio frames are flowing
into the capture queue before relying on the full voice pipeline (wake word,
STT, command matching, etc.).

What it does
------------
1. Creates an AudioCapture on the system default input device (or an
   explicitly requested one) with a small queue (maxqueue=10).
2. Enables debug logging so every enqueued frame is logged.
3. Starts the capture stream and pulls up to 10 frames (1280 samples =
   80 ms @ 16 kHz each) from the queue, within a 10-second overall timeout.
4. Logs each frame as it arrives (frame number + length), or reports when
   no frame is available yet.
5. Stops the stream cleanly in a finally block.

Usage
-----
    python scripts/test_audio.py                 # system default device
    python scripts/test_audio.py --device hw:1,0 # specific ALSA device
    python scripts/test_audio.py --device default

Exit behaviour
--------------
Returns 0 after either 10 frames are received or the 10 s timeout elapses.
The exit code is not meaningful for pass/fail; inspect the log output to see
whether frames were captured.
"""

from __future__ import annotations

import argparse
import time
import logging

import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice.audio import AudioCapture, FRAME_SAMPLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("test_audio")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Smoke test the audio capture pipeline.",
    )
    parser.add_argument(
        "--device",
        metavar="NAME",
        default=None,
        help=(
            "ALSA input device to capture from (e.g. 'hw:1,0', 'plughw:1,0', "
            "'default'). If omitted, the system default input device is used."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    a = AudioCapture(device=args.device, maxqueue=10)
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
