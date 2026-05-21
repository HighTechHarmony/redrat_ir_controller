#!/usr/bin/env python3
"""Play a 1s beep on every ALSA interface reported by `aplay -L`.

Useful to quickly test which physical output the system will actually play on.
Try sounddevice playback first (by alias), then fall back to `speaker-test`.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import List


def list_aplay_aliases() -> List[str]:
    try:
        out = subprocess.check_output(["aplay", "-L"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    aliases: List[str] = []
    for line in out.splitlines():
        if not line:
            continue
        if not line[0].isspace():
            aliases.append(line.strip())
    # Remove duplicates while preserving order
    seen = set()
    uniq = []
    for a in aliases:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def try_sounddevice_play(alias: str) -> bool:
    try:
        import numpy as np
        import sounddevice as sd

        try:
            info = sd.query_devices(alias)
            rate = int(info.get("default_samplerate") or 48000)
        except Exception:
            rate = 48000

        samples = int(round(rate * 1.0))
        t = (np.arange(samples, dtype=np.float32) / float(rate))
        wave = (0.3 * np.sin(2.0 * np.pi * 880.0 * t)).astype(np.float32)
        sd.play(wave, samplerate=rate, device=alias, blocking=True)
        return True
    except Exception as exc:  # noqa: BLE001 - we want to catch any playback error
        print(f"    sounddevice failed for {alias!r}: {exc}")
        return False


def try_speaker_test(alias: str) -> bool:
    # speaker-test uses ALSA device names; pass alias directly
    cmd = ["speaker-test", "-D", alias, "-t", "sine", "-f", "1000", "-c", "1", "-l", "1"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return proc.returncode == 0
    except Exception as exc:
        print(f"    speaker-test failed for {alias!r}: {exc}")
        return False


def main() -> int:
    aliases = list_aplay_aliases()
    if not aliases:
        print("No ALSA devices found via `aplay -L`.")
        return 1

    print(f"Found {len(aliases)} ALSA device aliases. Testing each for a 1s beep:\n")
    for a in aliases:
        print(f"- Testing: {a}")
        # Try sounddevice by alias (non-blocking -> blocking=True used inside)
        ok = try_sounddevice_play(a)
        if ok:
            print(f"    OK (sounddevice) -> {a}\n")
            # small pause between tests so hardware has time to settle
            time.sleep(0.15)
            continue

        # Fallback to speaker-test (external utility)
        ok2 = try_speaker_test(a)
        if ok2:
            print(f"    OK (speaker-test) -> {a}\n")
        else:
            print(f"    FAIL -> {a}\n")
        time.sleep(0.15)

    print("Done testing devices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
