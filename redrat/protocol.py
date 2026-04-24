"""
RedRat3 USB protocol constants and redrat3_irdata struct encoder/decoder.

Source: Linux kernel drivers/media/rc/redrat3.c
  USB VID 0x112A  PID 0x0001 (RedRat3) / 0x0005 (RedRat3-II)

Struct layout (all big-endian):
  uint16  length           — byte length of the body that follows
  uint16  transfer_type    — 0x0020 = signal in, 0x0021 = signal out
  uint32  pause            — pre-signal pause (RR3 time units)
  uint16  mod_freq_count   — carrier frequency counter
  uint16  num_periods      — carrier periods measured
  uint8   max_lengths      — allocated slots in lens[]
  uint8   no_lengths       — number of distinct lengths used
  uint16  max_sig_size     — allocated bytes in sigdata[]
  uint16  sig_size         — actual bytes used in sigdata[]
  uint8   no_repeats       — repeat count
  uint16[255] lens         — distinct pulse/space durations (RR3 time units)
  uint8[512]  sigdata      — indices into lens[], 0x7f = end-of-signal
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# USB identifiers
# ---------------------------------------------------------------------------
RR3_VENDOR_ID = 0x112A
RR3_PRODUCT_ID_V1 = 0x0001   # RedRat3 original
RR3_PRODUCT_ID_V2 = 0x0005   # RedRat3-II
RR3_PRODUCT_IDS = (RR3_PRODUCT_ID_V1, RR3_PRODUCT_ID_V2)

# ---------------------------------------------------------------------------
# Endpoint addresses
# ---------------------------------------------------------------------------
RR3_EP_WIDEBAND_IN = 0x81    # wideband / learning receiver  (bulk-in)
RR3_EP_NARROWBAND_IN = 0x82  # narrowband / RC receiver       (bulk-in)
RR3_EP_TX = 0x01             # IR transmitter                 (bulk-out)

# ---------------------------------------------------------------------------
# Control message request codes (bRequest, USB_TYPE_VENDOR | USB_RECIP_DEVICE)
# ---------------------------------------------------------------------------
RR3_RESET = 0xA0
RR3_FW_VERSION = 0xB1
RR3_MODSIG_CAPTURE = 0xB2    # arm wideband (learning) receiver
RR3_TX_SEND_SIGNAL = 0xB3    # trigger transmission of pre-loaded signal
RR3_SET_IR_PARAM = 0xB7
RR3_GET_IR_PARAM = 0xB8
RR3_BLINK_LED = 0xB9
RR3_READ_SER_NO = 0xBA
RR3_RC_DET_ENABLE = 0xBB
RR3_RC_DET_DISABLE = 0xBC
RR3_RC_DETStatus = 0xBD

# ---------------------------------------------------------------------------
# IR parameter indices (used with RR3_SET_IR_PARAM / RR3_GET_IR_PARAM)
# ---------------------------------------------------------------------------
RR3_IR_IO_MAX_LENGTHS = 0x01
RR3_IR_IO_PERIODS_MF = 0x02
RR3_IR_IO_SIG_MEM_SIZE = 0x03
RR3_IR_IO_LENGTH_FUZZ = 0x04   # µs tolerance for merging similar lengths
RR3_IR_IO_SIG_TIMEOUT = 0x05
RR3_IR_IO_MIN_PAUSE = 0x06     # minimum pre-signal pause (ms), default 18

# ---------------------------------------------------------------------------
# Transfer type field values
# ---------------------------------------------------------------------------
RR3_MOD_SIGNAL_IN = 0x0020    # device → host (learned signal)
RR3_MOD_SIGNAL_OUT = 0x0021   # host → device (signal to transmit)

# ---------------------------------------------------------------------------
# Struct constants
# ---------------------------------------------------------------------------
RR3_MAX_LENGTHS = 0xFF         # lens[] table size: 255 slots
# sigdata uses byte indices into lens[].  0x7F is the end-of-signal
# marker, so only indices 0x00–0x7E (0–126) are valid data indices.
RR3_MAX_DISTINCT_LENGTHS = 0x7F   # maximum safe distinct pulse/space lengths
RR3_MAX_SIG_SIZE = 512             # maximum sigdata bytes
RR3_END_OF_SIGNAL = 0x7F
RR3_DEFAULT_CARRIER_HZ = 38_000

# Clock: 24 MHz divided by 12 = 2 MHz → each tick ≈ 0.5 µs
RR3_CLOCK_HZ = 2_000_000
RR3_CARRIER_FREQ_BASE = 6_000_000   # used in mod_freq_count formula

# ---------------------------------------------------------------------------
# Struct format for the fixed header portion (before lens[] and sigdata[])
# Big-endian: >  H H I H H B B H H B
# Fields:     length transfer_type pause mod_freq_count num_periods
#             max_lengths no_lengths max_sig_size sig_size no_repeats
# ---------------------------------------------------------------------------
_HEADER_FMT = ">HHIHHBBHHBxxx"   # 3 pad bytes to align to 4-byte boundary
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)   # 22 bytes

# The device sends compact (variable-length) packets without the 3 padding
# bytes and with only max_lengths lens slots and sig_size sigdata bytes.
# Use this format when decoding received (learning) packets.
_HEADER_FMT_RX = ">HHIHHBBHHB"   # no padding, 19 bytes
_HEADER_SIZE_RX = struct.calcsize(_HEADER_FMT_RX)   # 19 bytes

_LENS_FMT = ">255H"   # 510 bytes
_LENS_SIZE = struct.calcsize(_LENS_FMT)

_SIGDATA_FMT = "512s"  # 512 bytes (raw bytes, no endian needed for uint8)
_SIGDATA_SIZE = 512

IRDATA_PACKET_SIZE = _HEADER_SIZE + _LENS_SIZE + _SIGDATA_SIZE  # 1044 bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def carrier_to_mod_freq_count(carrier_hz: int) -> int:
    return 65536 - (RR3_CARRIER_FREQ_BASE // carrier_hz)


def mod_freq_count_to_carrier(mod_freq_count: int, num_periods: int = 1) -> int:
    if num_periods == 0:
        return RR3_DEFAULT_CARRIER_HZ
    divisor = 65536 - mod_freq_count
    if divisor <= 0:
        return RR3_DEFAULT_CARRIER_HZ
    return (RR3_CARRIER_FREQ_BASE * num_periods) // divisor


def us_to_rr3(microseconds: int) -> int:
    return (microseconds * RR3_CLOCK_HZ) // 1_000_000


def rr3_to_us(ticks: int) -> int:
    return (ticks * 1_000_000) // RR3_CLOCK_HZ


@dataclass
class IrData:
    carrier_hz: int
    timings_us: List[int]
    pause_us: int = 0
    no_repeats: int = 0


def encode_irdata(ir: IrData) -> bytes:
    timings = ir.timings_us
    if not timings:
        raise ValueError("timings_us must not be empty")

    tick_values = [min(us_to_rr3(t), 65535) for t in timings]

    unique_ticks: list[int] = []
    seen: dict[int, int] = {}
    for v in tick_values:
        if v not in seen:
            if len(unique_ticks) >= RR3_MAX_DISTINCT_LENGTHS:
                raise ValueError("too many distinct lengths")
            seen[v] = len(unique_ticks)
            unique_ticks.append(v)

    sigdata_list: list[int] = [seen[v] for v in tick_values]
    sigdata_list.append(RR3_END_OF_SIGNAL)

    sig_size = len(sigdata_list)
    if sig_size > RR3_MAX_SIG_SIZE:
        raise ValueError("signal too large")

    no_lengths = len(unique_ticks)
    mod_freq_count = carrier_to_mod_freq_count(ir.carrier_hz)
    pause_ticks = us_to_rr3(ir.pause_us)

    body_length = IRDATA_PACKET_SIZE - 2

    header = struct.pack(
        _HEADER_FMT,
        body_length,
        RR3_MOD_SIGNAL_OUT,
        pause_ticks,
        mod_freq_count,
        1,
        RR3_MAX_LENGTHS,
        no_lengths,
        RR3_MAX_SIG_SIZE,
        sig_size,
        ir.no_repeats,
    )

    lens_padded = unique_ticks + [0] * (RR3_MAX_LENGTHS - no_lengths)
    lens_bytes = struct.pack(_LENS_FMT, *lens_padded)

    sigdata_padded = sigdata_list + [RR3_END_OF_SIGNAL] * (RR3_MAX_SIG_SIZE - sig_size)
    sigdata_bytes = bytes(sigdata_padded)

    return header + lens_bytes + sigdata_bytes


def decode_irdata(data: bytes) -> IrData:
    if len(data) < _HEADER_SIZE_RX:
        raise ValueError("Packet too short for header")

    (
        _length,
        transfer_type,
        pause_ticks,
        mod_freq_count,
        num_periods,
        max_lengths,
        no_lengths,
        _max_sig_size,
        sig_size,
        no_repeats,
    ) = struct.unpack_from(_HEADER_FMT_RX, data, 0)

    if transfer_type != RR3_MOD_SIGNAL_IN:
        raise ValueError("Unexpected transfer_type")

    if len(data) >= IRDATA_PACKET_SIZE:
        lens_slot_count = RR3_MAX_LENGTHS
        lens_fmt = _LENS_FMT
        lens_offset = _HEADER_SIZE
    else:
        lens_slot_count = max_lengths if max_lengths else RR3_MAX_LENGTHS
        lens_fmt = f">{lens_slot_count}H"
        lens_offset = _HEADER_SIZE_RX

    sigdata_offset = lens_offset + 2 * lens_slot_count
    min_size = sigdata_offset + sig_size
    if len(data) < min_size:
        raise ValueError("Packet too short for payload")

    lens_raw = struct.unpack_from(lens_fmt, data, lens_offset)
    sigdata_raw = data[sigdata_offset: sigdata_offset + sig_size]

    lens_table = lens_raw[:no_lengths]

    timings_ticks: list[int] = []
    for i in range(sig_size):
        idx = sigdata_raw[i]
        if idx == RR3_END_OF_SIGNAL:
            break
        if idx >= no_lengths:
            raise ValueError("sigdata index out of range")
        timings_ticks.append(lens_table[idx])

    timings_us = [rr3_to_us(t) for t in timings_ticks]

    if len(data) >= IRDATA_PACKET_SIZE:
        carrier_hz = mod_freq_count_to_carrier(mod_freq_count, num_periods)
    elif mod_freq_count > 0 and num_periods > 0:
        carrier_hz = (num_periods * RR3_CLOCK_HZ) // mod_freq_count
    else:
        carrier_hz = RR3_DEFAULT_CARRIER_HZ

    pause_us = rr3_to_us(pause_ticks)

    return IrData(
        carrier_hz=carrier_hz,
        timings_us=timings_us,
        pause_us=pause_us,
        no_repeats=no_repeats,
    )
