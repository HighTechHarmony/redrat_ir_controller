from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("test_playback")


def list_devices():
    devs = sd.query_devices()
    default_out = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    for i, d in enumerate(devs):
        kind = "output" if d.get("max_output_channels", 0) > 0 else "input"
        marker = "(default)" if i == default_out else ""
        print(f"{i:3d}: {d.get('name')!r} [{kind}] {marker}")


def play_beep(device, freq, dur, amp=0.3):
    try:
        info = sd.query_devices(device, kind="output")
        rate = int(info.get("default_samplerate", 16000))
    except Exception:
        rate = 16000
    samples = int(round(dur * rate))
    t = np.arange(samples, dtype=np.float32) / float(rate)
    wave = (amp * np.sin(2.0 * np.pi * float(freq) * t)).astype(np.float32)
    sd.play(wave, samplerate=rate, device=device, blocking=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="List available sounddevice devices")
    p.add_argument("--device", help="Device name or index to play on (default: system default)")
    p.add_argument("--freq", type=int, default=800, help="Beep frequency (Hz)")
    p.add_argument("--dur", type=float, default=0.5, help="Beep duration (s)")
    args = p.parse_args()

    if args.list:
        list_devices()
        return

    device = None
    if args.device is not None:
        try:
            device = int(args.device)
        except Exception:
            device = args.device

    log.info("Playing beep on device=%r freq=%d dur=%.3f", device, args.freq, args.dur)
    try:
        play_beep(device, args.freq, args.dur)
        log.info("Beep played successfully")
    except Exception as exc:
        log.exception("Beep playback failed: %s", exc)


if __name__ == "__main__":
    main()
