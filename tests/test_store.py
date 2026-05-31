import yaml

from redrat.store import SignalStore
from redrat.protocol import IrData


def test_save_sanitizes_leading_silence_and_trailing_space(tmp_path):
    path = tmp_path / "ir_codes.yaml"
    store = SignalStore(path)

    # Case 1: leading silence artifact (e.g. 16777215) should be removed
    raw1 = [16777215, 8967, 4505, 562, 1702, 562, 1702, 562]
    store.save_signal("hdmi1", IrData(carrier_hz=37647, timings_us=raw1, no_repeats=0))
    saved1 = store.get("hdmi1").timings_us
    assert saved1 == [8967, 4505, 562, 1702, 562, 1702, 562]

    # Case 2: trailing space (even count) should be stripped to end with a pulse
    raw2 = [8967, 4505, 562, 1702, 562, 1702, 562, 400]
    store.save_signal("tv_power", IrData(carrier_hz=38000, timings_us=raw2, no_repeats=0))
    saved2 = store.get("tv_power").timings_us
    assert saved2 == [8967, 4505, 562, 1702, 562, 1702, 562]
