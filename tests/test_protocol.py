"""
Unit tests for redrat/protocol.py — encode/decode round-trip tests.

These tests run without any USB hardware.
"""

import struct
import pytest

from redrat.protocol import (
    IrData,
    IRDATA_PACKET_SIZE,
    RR3_MAX_LENGTHS,
    RR3_MAX_SIG_SIZE,
    RR3_MOD_SIGNAL_IN,
    RR3_MOD_SIGNAL_OUT,
    RR3_END_OF_SIGNAL,
    carrier_to_mod_freq_count,
    mod_freq_count_to_carrier,
    us_to_rr3,
    rr3_to_us,
    encode_irdata,
    decode_irdata,
    _HEADER_FMT,
    _HEADER_SIZE,
    _LENS_SIZE,
)


# ---------------------------------------------------------------------------
# Helper: patch transfer_type in a raw packet to RR3_MOD_SIGNAL_IN so that
# decode_irdata will accept it (it was encoded with SIGNAL_OUT).
# ---------------------------------------------------------------------------

def _make_receivable(packet: bytes) -> bytes:
    """Flip transfer_type field from SIGNAL_OUT to SIGNAL_IN."""
    arr = bytearray(packet)
    struct.pack_into(">H", arr, 2, RR3_MOD_SIGNAL_IN)
    return bytes(arr)


# ---------------------------------------------------------------------------
# Carrier frequency helpers
# ---------------------------------------------------------------------------

class TestCarrierHelpers:
    def test_38khz_roundtrip(self):
        count = carrier_to_mod_freq_count(38_000)
        recovered = mod_freq_count_to_carrier(count, num_periods=1)
        # Allow ±500 Hz: integer truncation in the carrier formula causes ~0.5% error
        assert abs(recovered - 38_000) < 500

    def test_36khz_roundtrip(self):
        count = carrier_to_mod_freq_count(36_000)
        recovered = mod_freq_count_to_carrier(count, num_periods=1)
        assert abs(recovered - 36_000) < 200

    def test_40khz_roundtrip(self):
        count = carrier_to_mod_freq_count(40_000)
        recovered = mod_freq_count_to_carrier(count, num_periods=1)
        assert abs(recovered - 40_000) < 300


# ---------------------------------------------------------------------------
# Time unit conversion
# ---------------------------------------------------------------------------

class TestTimeConversion:
    def test_560us_roundtrip(self):
        ticks = us_to_rr3(560)
        assert abs(rr3_to_us(ticks) - 560) <= 1

    def test_8960us_roundtrip(self):
        ticks = us_to_rr3(8960)
        assert abs(rr3_to_us(ticks) - 8960) <= 1

    def test_zero(self):
        assert us_to_rr3(0) == 0
        assert rr3_to_us(0) == 0


# ---------------------------------------------------------------------------
# Packet size constant
# ---------------------------------------------------------------------------

class TestPacketSize:
    def test_size(self):
        # header(22) + lens(510) + sigdata(512) = 1044
        assert IRDATA_PACKET_SIZE == 1044


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------

# 47040 µs would overflow uint16 at 2 ticks/µs; use 27000 µs (~27 ms) as the
# final gap — realistic for NEC and within the 32767 µs uint16 limit.
NEC_TIMINGS = [
    8960, 4480,
    560, 560,   560, 1680,  560, 560,   560, 560,
    560, 1680,  560, 560,   560, 560,   560, 560,
    560, 1680,  560, 1680,  560, 1680,  560, 560,
    560, 1680,  560, 1680,  560, 1680,  560, 1680,
    560, 560,   560, 560,   560, 560,   560, 1680,
    560, 560,   560, 560,   560, 560,   560, 560,
    560, 1680,  560, 1680,  560, 1680,  560, 560,
    560, 1680,  560, 1680,  560, 1680,  560, 27000,
]


class TestEncoder:
    def test_returns_correct_size(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        packet = encode_irdata(ir)
        assert len(packet) == IRDATA_PACKET_SIZE

    def test_transfer_type_is_signal_out(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        packet = encode_irdata(ir)
        transfer_type = struct.unpack_from(">H", packet, 2)[0]
        assert transfer_type == RR3_MOD_SIGNAL_OUT

    def test_no_lengths_is_correct(self):
        # NEC_TIMINGS has exactly 3 distinct values: 560, 8960, 4480, 1680, 47040
        # Let's count unique
        unique = len(set(NEC_TIMINGS))
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        packet = encode_irdata(ir)
        no_lengths = struct.unpack_from("B", packet, _HEADER_SIZE - 7)[0]
        # Read from the actual header fields
        header_fields = struct.unpack_from(_HEADER_FMT, packet, 0)
        no_lengths = header_fields[6]   # no_lengths is the 7th field (0-indexed)
        assert no_lengths == unique

    def test_end_of_signal_marker_present(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        packet = encode_irdata(ir)
        sigdata = packet[_HEADER_SIZE + _LENS_SIZE:]
        header_fields = struct.unpack_from(_HEADER_FMT, packet, 0)
        sig_size = header_fields[8]   # sig_size
        assert sigdata[sig_size - 1] == RR3_END_OF_SIGNAL

    def test_empty_timings_raises(self):
        with pytest.raises(ValueError, match="empty"):
            encode_irdata(IrData(carrier_hz=38_000, timings_us=[]))

    def test_too_many_distinct_lengths_raises(self):
        # Create 128 distinct values (more than RR3_MAX_DISTINCT_LENGTHS=127)
        from redrat.protocol import RR3_MAX_DISTINCT_LENGTHS
        timings = list(range(100, 100 + RR3_MAX_DISTINCT_LENGTHS + 1))
        with pytest.raises(ValueError, match="distinct"):
            encode_irdata(IrData(carrier_hz=38_000, timings_us=timings))

    def test_repeat_field_preserved(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS, no_repeats=3)
        packet = encode_irdata(ir)
        header_fields = struct.unpack_from(_HEADER_FMT, packet, 0)
        assert header_fields[9] == 3   # no_repeats


# ---------------------------------------------------------------------------
# Round-trip: encode → patch → decode
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def _roundtrip(self, ir: IrData) -> IrData:
        packet = encode_irdata(ir)
        receivable = _make_receivable(packet)
        return decode_irdata(receivable)

    def test_nec_timings_roundtrip(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        decoded = self._roundtrip(ir)
        assert len(decoded.timings_us) == len(ir.timings_us)
        for orig, rec in zip(ir.timings_us, decoded.timings_us):
            # Allow ±1 µs due to integer conversion
            assert abs(rec - orig) <= 1, f"timing mismatch: {orig} → {rec}"

    def test_carrier_roundtrip(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        decoded = self._roundtrip(ir)
        # Allow ±500 Hz: integer truncation in the carrier formula causes ~0.5% error
        # at 38 kHz when num_periods=1.  Consumer IR receivers tolerate this easily.
        assert abs(decoded.carrier_hz - 38_000) < 500

    def test_single_timing_roundtrip(self):
        ir = IrData(carrier_hz=38_000, timings_us=[500, 1000])
        decoded = self._roundtrip(ir)
        assert len(decoded.timings_us) == 2
        for orig, rec in zip(ir.timings_us, decoded.timings_us):
            assert abs(rec - orig) <= 1

    def test_max_lengths_signal(self):
        # 127 distinct values: the maximum safe count given 0x7F is the end-of-signal marker
        from redrat.protocol import RR3_MAX_DISTINCT_LENGTHS
        timings = list(range(200, 200 + RR3_MAX_DISTINCT_LENGTHS))
        ir = IrData(carrier_hz=38_000, timings_us=timings)
        decoded = self._roundtrip(ir)
        assert len(decoded.timings_us) == len(timings)


# ---------------------------------------------------------------------------
# Decoder error cases
# ---------------------------------------------------------------------------

class TestDecoder:
    def test_short_packet_raises(self):
        with pytest.raises(ValueError, match="too short"):
            decode_irdata(b"\x00" * 100)

    def test_wrong_transfer_type_raises(self):
        ir = IrData(carrier_hz=38_000, timings_us=NEC_TIMINGS)
        packet = encode_irdata(ir)
        # transfer_type is SIGNAL_OUT (0x0021) — decoder expects SIGNAL_IN (0x0020)
        with pytest.raises(ValueError, match="transfer_type"):
            decode_irdata(packet)
