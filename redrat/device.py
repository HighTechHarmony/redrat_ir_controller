"""
RedRat3 USB device driver using PyUSB.

Provides:
  RedRatDevice.enumerate()          — find all connected RedRat3 devices
  device.get_firmware_version()     — read firmware version string
  device.get_serial_number()        — read 4-byte serial number
  device.learn(timeout_s)           — arm wideband receiver and capture one signal
  device.send(ir_data)              — encode and transmit an IR signal
  device.blink_led()                — blink the indicator LED (useful for testing)
  device.reset()                    — soft-reset the device
"""

from __future__ import annotations

import logging
from typing import List, Optional

import usb.core
import usb.util

from redrat.protocol import (
    IrData,
    RR3_VENDOR_ID,
    RR3_PRODUCT_IDS,
    RR3_EP_WIDEBAND_IN,
    RR3_EP_TX,
    RR3_RESET,
    RR3_FW_VERSION,
    RR3_MODSIG_CAPTURE,
    RR3_TX_SEND_SIGNAL,
    RR3_BLINK_LED,
    RR3_READ_SER_NO,
    IRDATA_PACKET_SIZE,
    decode_irdata,
    encode_irdata,
)

log = logging.getLogger(__name__)

# usb.core.ctrl_transfer direction flags
_CTRL_IN = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
_CTRL_OUT = usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE

# Timeout for bulk transfers (milliseconds)
_BULK_TIMEOUT_MS = 2000
# Timeout for control transfers (milliseconds)
_CTRL_TIMEOUT_MS = 3000
# Firmware version response size
_FW_VERSION_LEN = 64
# Serial number response size (4 bytes → 8 hex chars as ASCII)
_SERIAL_LEN = 4


class RedRatError(Exception):
    """Raised for RedRat3 device communication errors."""


class RedRatDevice:
    """Wraps a single RedRat3 USB device."""

    def __init__(self, dev: usb.core.Device) -> None:
        self._dev = dev
        self._claimed = False
        self._claim()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _claim(self) -> None:
        """Detach kernel driver if needed and claim interface 0."""
        dev = self._dev
        # Let libusb auto-detach kernel drivers when supported.
        try:
            dev.set_auto_detach_kernel_driver(True)
        except (NotImplementedError, usb.core.USBError, AttributeError):
            pass
        if dev.is_kernel_driver_active(0):
            log.debug("Detaching kernel driver from interface 0")
            dev.detach_kernel_driver(0)
        # Only set configuration if the device is not already configured.
        # On Linux the redrat3/rc-core kernel driver may have already done this,
        # and calling set_configuration() again returns LIBUSB_ERROR_OTHER.
        try:
            if dev.get_active_configuration() is None:
                dev.set_configuration()
        except usb.core.USBError:
            # get_active_configuration() raises if nothing is set; set it now.
            dev.set_configuration()
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as exc:
            err_no = getattr(exc, "errno", None)
            backend_code = getattr(exc, "backend_error_code", None)
            if err_no == 16 or backend_code == -6:
                raise RedRatError(
                    "RedRat3 USB interface is busy. Another process is using it "
                    "(for example another running main.py instance, lircd, or ir-keytable)."
                ) from exc
            raise RedRatError(f"Failed to claim RedRat3 USB interface: {exc}") from exc
        self._claimed = True
        log.debug("Claimed RedRat3 device (VID=%04X PID=%04X)", dev.idVendor, dev.idProduct)

    def close(self) -> None:
        """Release the USB interface."""
        if self._claimed:
            try:
                usb.util.release_interface(self._dev, 0)
            except Exception:
                pass
            self._claimed = False

    def __enter__(self) -> "RedRatDevice":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def enumerate(cls, serial_number: Optional[str] = None) -> List["RedRatDevice"]:
        """
        Find all connected RedRat3 devices.

        If *serial_number* is given, return only the device with that serial.
        Returns an empty list if no device is found.
        """
        devices = []
        for pid in RR3_PRODUCT_IDS:
            found = usb.core.find(
                idVendor=RR3_VENDOR_ID,
                idProduct=pid,
                find_all=True,
            )
            if found:
                devices.extend(found)

        wrapped: List[RedRatDevice] = []
        for d in devices:
            try:
                wrapped.append(cls(d))
            except RedRatError as exc:
                log.warning(
                    "Skipping RedRat3 VID=%04X PID=%04X: %s",
                    d.idVendor,
                    d.idProduct,
                    exc,
                )

        if serial_number:
            wrapped = [d for d in wrapped if d.get_serial_number() == serial_number]

        return wrapped

    @classmethod
    def open_first(cls, serial_number: Optional[str] = None) -> "RedRatDevice":
        """
        Return the first available RedRat3 device.

        Raises RedRatError if none is found.
        """
        devices = cls.enumerate(serial_number=serial_number)
        if not devices:
            raise RedRatError(
                "No RedRat3 device found. "
                "Check USB connection and udev rules (99-redrat.rules)."
            )
        return devices[0]

    # ------------------------------------------------------------------
    # Control messages
    # ------------------------------------------------------------------

    def _ctrl_in(self, request: int, length: int, value: int = 0, index: int = 0) -> bytes:
        result = self._dev.ctrl_transfer(
            bmRequestType=_CTRL_IN,
            bRequest=request,
            wValue=value,
            wIndex=index,
            data_or_wLength=length,
            timeout=_CTRL_TIMEOUT_MS,
        )
        return bytes(result)

    def _ctrl_out(self, request: int, data: bytes = b"", value: int = 0, index: int = 0) -> None:
        self._dev.ctrl_transfer(
            bmRequestType=_CTRL_OUT,
            bRequest=request,
            wValue=value,
            wIndex=index,
            data_or_wLength=data if data else 0,
            timeout=_CTRL_TIMEOUT_MS,
        )

    def _tx_send_signal(self) -> bytes:
        """
        Trigger transmission of the signal previously written via bulk-out.

        Per the upstream Linux driver, RR3_TX_SEND_SIGNAL is a vendor IN
        control request returning a small status payload.
        """
        result = self._dev.ctrl_transfer(
            bmRequestType=_CTRL_IN,
            bRequest=RR3_TX_SEND_SIGNAL,
            wValue=0,
            wIndex=0,
            data_or_wLength=2,
            timeout=_CTRL_TIMEOUT_MS,
        )
        return bytes(result)

    def reset(self) -> None:
        """Soft-reset the device."""
        self._ctrl_out(RR3_RESET)
        log.debug("Device reset")

    def blink_led(self) -> None:
        """Blink the indicator LED (no-op if unsupported by firmware)."""
        self._ctrl_out(RR3_BLINK_LED)

    def get_firmware_version(self) -> str:
        """Return the firmware version string, or 'unknown' if the device does not respond."""
        try:
            raw = self._ctrl_in(RR3_FW_VERSION, _FW_VERSION_LEN)
            return raw.rstrip(b"\x00").decode("ascii", errors="replace")
        except usb.core.USBError as exc:
            log.warning(
                "Could not read firmware version (%s). "
                "If the redrat3 kernel module is loaded, consider blacklisting it: "
                "echo 'blacklist redrat3' | sudo tee /etc/modprobe.d/redrat3-blacklist.conf",
                exc,
            )
            return "unknown"

    def get_serial_number(self) -> str:
        """Return the device serial number as a hex string, or 'unknown' if unavailable."""
        try:
            raw = self._ctrl_in(RR3_READ_SER_NO, _SERIAL_LEN)
            return raw.hex().upper()
        except usb.core.USBError as exc:
            log.warning("Could not read serial number (%s)", exc)
            return "unknown"

    def info(self) -> dict:
        """Return a dict of basic device info."""
        return {
            "vendor_id": f"0x{self._dev.idVendor:04X}",
            "product_id": f"0x{self._dev.idProduct:04X}",
            "serial_number": self.get_serial_number(),
            "firmware_version": self.get_firmware_version(),
        }

    def diagnostics(self) -> dict:
        """
        Run lightweight communication diagnostics.

        This performs only control-channel checks (no IR transmit/learn):
          1) blink LED vendor command
          2) read serial number
          3) read firmware version
        """
        checks = []

        # 1) LED control command
        try:
            self.blink_led()
            checks.append({"name": "blink_led", "ok": True})
        except usb.core.USBError as exc:
            checks.append({"name": "blink_led", "ok": False, "error": str(exc)})

        # 2) Serial read
        try:
            serial = self._ctrl_in(RR3_READ_SER_NO, _SERIAL_LEN).hex().upper()
            checks.append({"name": "read_serial", "ok": True, "value": serial})
        except usb.core.USBError as exc:
            checks.append({"name": "read_serial", "ok": False, "error": str(exc)})

        # 3) Firmware read
        try:
            fw = self._ctrl_in(RR3_FW_VERSION, _FW_VERSION_LEN).rstrip(b"\x00").decode(
                "ascii", errors="replace"
            )
            checks.append({"name": "read_firmware", "ok": True, "value": fw})
        except usb.core.USBError as exc:
            checks.append({"name": "read_firmware", "ok": False, "error": str(exc)})

        ok = all(c.get("ok") for c in checks)
        return {
            "ok": ok,
            "vendor_id": f"0x{self._dev.idVendor:04X}",
            "product_id": f"0x{self._dev.idProduct:04X}",
            "checks": checks,
        }

    # ------------------------------------------------------------------
    # IR signal learning
    # ------------------------------------------------------------------

    def learn(self, timeout_s: float = 10.0) -> IrData:
        """
        Arm the wideband (learning) receiver and wait for an IR signal.

        The caller should point a remote control at the device and press a button
        within *timeout_s* seconds.

        Returns an IrData with the decoded signal.
        Raises RedRatError on timeout or malformed packet.
        """
        log.info("Arming wideband receiver — point remote at device (timeout=%.1fs)", timeout_s)

        # Arm the learning receiver
        self._ctrl_out(RR3_MODSIG_CAPTURE)

        timeout_ms = int(timeout_s * 1000)
        try:
            raw = self._dev.read(RR3_EP_WIDEBAND_IN, IRDATA_PACKET_SIZE, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            raise RedRatError(f"No IR signal received within {timeout_s:.1f} seconds")
        except usb.core.USBError as exc:
            raise RedRatError(f"USB read error during learning: {exc}") from exc

        raw_bytes = bytes(raw)
        log.debug("Received %d bytes from wideband endpoint", len(raw_bytes))

        try:
            ir = decode_irdata(raw_bytes)
        except ValueError as exc:
            raise RedRatError(f"Failed to decode IR packet: {exc}") from exc

        log.info(
            "Learned signal: carrier=%dHz, %d timings",
            ir.carrier_hz,
            len(ir.timings_us),
        )
        return ir

    # ------------------------------------------------------------------
    # IR signal transmission
    # ------------------------------------------------------------------

    def send(self, ir: IrData) -> None:
        """
        Encode and transmit an IR signal.

        Raises RedRatError on encoding or USB write failure.
        """
        try:
            packet = encode_irdata(ir)
        except ValueError as exc:
            raise RedRatError(f"Failed to encode IR signal: {exc}") from exc

        log.debug(
            "Sending signal: carrier=%dHz, %d timings, packet=%d bytes",
            ir.carrier_hz,
            len(ir.timings_us),
            len(packet),
        )

        try:
            written = self._dev.write(RR3_EP_TX, packet, timeout=_BULK_TIMEOUT_MS)
        except usb.core.USBError as exc:
            raise RedRatError(f"USB write error: {exc}") from exc

        if written != len(packet):
            raise RedRatError(
                f"Incomplete USB write: sent {written} of {len(packet)} bytes"
            )

        # Trigger transmission (vendor IN control request)
        try:
            self._tx_send_signal()
        except usb.core.USBError as exc:
            raise RedRatError(f"Failed to trigger TX: {exc}") from exc

        log.info("Signal transmitted (carrier=%dHz)", ir.carrier_hz)
