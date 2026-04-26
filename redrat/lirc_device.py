"""
LIRC chardev backend for the RedRat3.

Uses the kernel `redrat3` driver via /dev/lircX rather than speaking the
USB vendor protocol directly.  Exposes the same interface as RedRatDevice
so main.py and api/server.py need only minimal changes.

Kernel driver: drivers/media/rc/redrat3.c (rc-core)
LIRC userspace API: Documentation/userspace-api/media/rc/lirc-dev-intro.rst
"""

from __future__ import annotations

import array
import fcntl
import glob
import logging
import os
import select
import struct
import time
from typing import List, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redrat.protocol import IrData

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LIRC ioctl numbers (Linux, 64-bit)
# All derived from include/uapi/linux/lirc.h
# ---------------------------------------------------------------------------
_LIRC_GET_FEATURES          = 0x80046900   # _IOR('i', 0x00, __u32)
_LIRC_SET_SEND_MODE         = 0x40046911   # _IOW('i', 0x11, __u32)
_LIRC_SET_REC_MODE          = 0x40046912   # _IOW('i', 0x12, __u32)
_LIRC_SET_SEND_CARRIER      = 0x40046913   # _IOW('i', 0x13, __u32)
_LIRC_SET_MEASURE_CARRIER_MODE = 0x4004691d  # _IOW('i', 0x1d, __u32)
_LIRC_SET_REC_TIMEOUT       = 0x40046918   # _IOW('i', 0x18, __u32)
_LIRC_GET_MIN_TIMEOUT       = 0x80046915   # _IOR('i', 0x15, __u32)
_LIRC_GET_MAX_TIMEOUT       = 0x80046916   # _IOR('i', 0x16, __u32)

# Feature flags (LIRC_GET_FEATURES bitmask)
# LIRC_CAN_SEND_PULSE  = LIRC_MODE2SEND(LIRC_MODE_PULSE)  = 0x2
# LIRC_CAN_REC_MODE2   = LIRC_MODE2REC(LIRC_MODE_MODE2)   = (0x4 << 16) = 0x00040000
# LIRC_CAN_MEASURE_CARRIER = 0x02000000
_LIRC_CAN_SEND_PULSE        = 0x00000002
_LIRC_CAN_REC_MODE2         = 0x00040000
_LIRC_CAN_MEASURE_CARRIER   = 0x02000000

# LIRC MODE2 packet types (upper 8 bits of uint32)
_LIRC_MODE2_PULSE    = 0x00
_LIRC_MODE2_SPACE    = 0x01
_LIRC_MODE2_FREQUENCY = 0x02
_LIRC_MODE2_TIMEOUT  = 0x03
_LIRC_MODE2_OVERFLOW = 0x04

_LIRC_MODE2_MASK     = 0xFF000000
_LIRC_VALUE_MASK     = 0x00FFFFFF

# LIRC mode constants
_LIRC_MODE_PULSE     = 0x00000002
_LIRC_MODE_MODE2     = 0x00000040

# Default inter-repeat gap (µs); must be > minimum gap the device expects
_REPEAT_GAP_US = 108_000   # ~108 ms — standard NEC / common remote gap

# Maximum µs value representable in a LIRC MODE2 value field
_LIRC_MAX_VALUE_US = _LIRC_VALUE_MASK   # 16,777,215 µs

# Receive read chunk size (bytes); 4 bytes per MODE2 sample
_RX_CHUNK = 4096


class LircError(Exception):
    """Raised for LIRC device or protocol errors."""


class LircDevice:
    """
    IR transmit/receive via the Linux LIRC chardev interface.

    Provides the same public interface as RedRatDevice:
        send(ir), learn(timeout_s), info(), diagnostics(),
        close(), __enter__, __exit__, enumerate(), open_first()
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: Optional[int] = None
        self._features: int = 0
        self._open()

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def enumerate(cls) -> List["LircDevice"]:
        """Return LircDevice wrappers for all /dev/lirc* nodes."""
        devices: List[LircDevice] = []
        for p in sorted(glob.glob("/dev/lirc*")):
            try:
                devices.append(cls(p))
            except LircError as exc:
                log.warning("Skipping %s: %s", p, exc)
        return devices

    @classmethod
    def open_first(cls, path: str = "/dev/lirc0") -> "LircDevice":
        """
        Open the LIRC device at *path*.

        Raises LircError if the device cannot be opened or does not support
        LIRC_MODE_PULSE transmit.
        """
        dev = cls(path)
        if not (dev._features & _LIRC_CAN_SEND_PULSE):
            dev.close()
            raise LircError(
                f"{path}: device does not advertise LIRC_CAN_SEND_PULSE "
                f"(features=0x{dev._features:08X})"
            )
        log.info("Opened LIRC device %s (features=0x%08X)", path, dev._features)
        return dev

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> None:
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise LircError(f"Cannot open {self._path}: {exc}") from exc

        self._fd = fd

        # Read feature flags
        buf = array.array("I", [0])
        try:
            fcntl.ioctl(fd, _LIRC_GET_FEATURES, buf)
        except OSError as exc:
            os.close(fd)
            self._fd = None
            raise LircError(f"LIRC_GET_FEATURES failed on {self._path}: {exc}") from exc

        self._features = buf[0]

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "LircDevice":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Info / diagnostics
    # ------------------------------------------------------------------

    def get_serial_number(self) -> str:
        return self._path

    def get_firmware_version(self) -> str:
        return "kernel:redrat3"

    def info(self) -> dict:
        return {
            "path": self._path,
            "firmware_version": self.get_firmware_version(),
            "features": f"0x{self._features:08X}",
            "can_send": bool(self._features & _LIRC_CAN_SEND_PULSE),
            "can_receive": bool(self._features & _LIRC_CAN_REC_MODE2),
            "can_measure_carrier": bool(self._features & _LIRC_CAN_MEASURE_CARRIER),
        }

    def diagnostics(self) -> dict:
        checks = []

        # Feature check
        can_send = bool(self._features & _LIRC_CAN_SEND_PULSE)
        can_recv = bool(self._features & _LIRC_CAN_REC_MODE2)
        checks.append({
            "name": "features",
            "ok": can_send and can_recv,
            "can_send_pulse": can_send,
            "can_rec_mode2": can_recv,
            "raw": f"0x{self._features:08X}",
        })

        # Device node is accessible
        checks.append({
            "name": "device_node",
            "ok": self._fd is not None,
            "path": self._path,
        })

        ok = all(c.get("ok") for c in checks)
        return {
            "ok": ok,
            "path": self._path,
            "checks": checks,
        }

    # ------------------------------------------------------------------
    # IR transmit
    # ------------------------------------------------------------------

    def send(self, ir) -> None:
        """
        Transmit an IR signal via LIRC PULSE mode.

        Sets the carrier frequency, then writes the pulse/space array.
        Repeats `ir.no_repeats` additional times (total = no_repeats + 1).
        """
        if self._fd is None:
            raise LircError("Device is not open")

        # Import IrData at runtime only for isinstance checks if needed
        timings = list(ir.timings_us)

        # LIRC requires an odd number of values (must start AND end with pulse).
        # Drop a trailing space if the stored signal ends with one.
        if len(timings) % 2 == 0:
            timings = timings[:-1]

        if not timings:
            raise LircError("timings_us is empty after normalisation")

        # Set carrier frequency
        self._set_send_carrier(ir.carrier_hz)

        # Pack as array of uint32
        tx_array = array.array("I", timings)

        total_sends = ir.no_repeats + 1
        for i in range(total_sends):
            if i > 0:
                # Wait the inter-repeat gap before sending again.
                # The gap is measured from the END of the previous transmission,
                # so a fixed sleep is a reasonable approximation.
                time.sleep(_REPEAT_GAP_US / 1_000_000)
            try:
                written = os.write(self._fd, tx_array.tobytes())
            except OSError as exc:
                raise LircError(f"LIRC write failed: {exc}") from exc

            expected = len(tx_array) * tx_array.itemsize
            if written != expected:
                raise LircError(
                    f"Incomplete LIRC write: {written}/{expected} bytes"
                )

        log.info(
            "Sent %r: carrier=%dHz, %d timings, %d time(s)",
            getattr(ir, "name", "signal"),
            ir.carrier_hz,
            len(timings),
            total_sends,
        )

    def _set_send_carrier(self, carrier_hz: int) -> None:
        buf = array.array("I", [carrier_hz])
        try:
            fcntl.ioctl(self._fd, _LIRC_SET_SEND_CARRIER, buf)
        except OSError as exc:
            # Some kernels return ENOIOCTLCMD if the driver doesn't support
            # per-send carrier setting; log a warning and continue.
            log.warning("LIRC_SET_SEND_CARRIER failed (%s) — using hardware default", exc)

    # ------------------------------------------------------------------
    # IR receive / learning
    # ------------------------------------------------------------------

    def learn(self, timeout_s: float = 10.0):
        """
        Receive a single IR burst and return it as IrData.

        Enables carrier frequency measurement so the frequency is captured
        alongside the pulse/space timings.

        Raises LircError on timeout or if the device cannot receive.
        """
        if self._fd is None:
            raise LircError("Device is not open")
        if not (self._features & _LIRC_CAN_REC_MODE2):
            raise LircError(f"{self._path}: device does not support MODE2 receive")

        # Enable carrier frequency reporting.  This implicitly enables the
        # wideband receiver on devices that have one; on the RedRat3 the
        # kernel driver handles the wideband endpoint toggle itself.
        if self._features & _LIRC_CAN_MEASURE_CARRIER:
            self._ioctl_set(_LIRC_SET_MEASURE_CARRIER_MODE, 1)

        # Clear the O_NONBLOCK flag during receive so read() blocks properly.
        old_flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, old_flags & ~os.O_NONBLOCK)

        try:
            return self._read_burst(timeout_s)
        finally:
            # Restore non-blocking flag and disable carrier measurement
            fcntl.fcntl(self._fd, fcntl.F_SETFL, old_flags)
            if self._features & _LIRC_CAN_MEASURE_CARRIER:
                try:
                    self._ioctl_set(_LIRC_SET_MEASURE_CARRIER_MODE, 0)
                except OSError:
                    pass

    def _read_burst(self, timeout_s: float) -> IrData:
        """Read MODE2 uint32 packets until a TIMEOUT packet arrives."""
        deadline = time.monotonic() + timeout_s
        timings_us: list[int] = []
        carrier_hz = 0
        got_any = False

        log.info("Waiting for IR signal (timeout=%.1fs) ...", timeout_s)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                break

            try:
                chunk = os.read(self._fd, _RX_CHUNK)
            except OSError as exc:
                raise LircError(f"LIRC read error: {exc}") from exc

            if not chunk:
                continue

            # Parse as sequence of uint32 little-endian values
            for offset in range(0, len(chunk) - 3, 4):
                word, = struct.unpack_from("<I", chunk, offset)
                ptype = (word & _LIRC_MODE2_MASK) >> 24
                value = word & _LIRC_VALUE_MASK

                if ptype == _LIRC_MODE2_PULSE:
                    timings_us.append(value)
                    got_any = True
                elif ptype == _LIRC_MODE2_SPACE:
                    timings_us.append(value)
                    got_any = True
                elif ptype == _LIRC_MODE2_FREQUENCY:
                    carrier_hz = value
                    log.debug("Measured carrier: %d Hz", carrier_hz)
                elif ptype == _LIRC_MODE2_TIMEOUT:
                    if got_any:
                        # Timeout packet marks the end of a burst
                        return self._build_irdata(timings_us, carrier_hz)
                    # else: spurious timeout before any data; keep waiting
                elif ptype == _LIRC_MODE2_OVERFLOW:
                    log.warning("LIRC overflow packet — signal may be incomplete")

        if not timings_us:
            raise LircError(f"No IR signal received within {timeout_s:.1f} seconds")

        # Fell through the deadline without a TIMEOUT packet — use what we got
        return self._build_irdata(timings_us, carrier_hz)

    @staticmethod
    def _build_irdata(timings_us: list[int], carrier_hz: int):
        # Strip trailing space (if any) — LIRC bursts often end with a space
        # before the TIMEOUT packet; we only want the active signal content.
        while timings_us and len(timings_us) % 2 == 0:
            timings_us = timings_us[:-1]

        # Import here to avoid a hard dependency at module import time.
        try:
            from redrat.protocol import IrData
        except Exception:
            # Fallback minimal structure if IrData is not available
            class IrData:
                def __init__(self, carrier_hz, timings_us, no_repeats=0):
                    self.carrier_hz = carrier_hz
                    self.timings_us = timings_us
                    self.no_repeats = no_repeats

        return IrData(
            carrier_hz=carrier_hz if carrier_hz > 0 else 38_000,
            timings_us=timings_us,
            no_repeats=0,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ioctl_set(self, request: int, value: int) -> None:
        buf = array.array("I", [value])
        fcntl.ioctl(self._fd, request, buf)
