"""usb_serial.py — USB/UART serial I/O helpers for the PPG AFE device.

Protocol reference: BINARY_STREAMING.md

Binary stream format
--------------------
- Start marker (text): ``[BIN] Starting binary stream: N samples\\r\\n``
- Payload: N × 4 bytes, each sample is one little-endian uint32_t
- End marker (text):   ``\\r\\n[BIN] Stream complete: N samples sent\\r\\n``

This module is Streamlit-free so it can be reused in CLI scripts or tests.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Port discovery
# ─────────────────────────────────────────────────────────────────────────────

def list_serial_ports() -> list[str]:
    """Return a list of available serial port device paths, sorted."""
    if not SERIAL_AVAILABLE:
        return []
    ports = serial.tools.list_ports.comports()
    return sorted(p.device for p in ports)


def describe_ports() -> list[dict]:
    """Return rich descriptions (device, description, hwid) for each port."""
    if not SERIAL_AVAILABLE:
        return []
    return [
        {"device": p.device, "description": p.description, "hwid": p.hwid}
        for p in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """Outcome of a single command send + response cycle."""
    command: str
    response: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class StreamResult:
    """Outcome of a binary stream capture.

    ``samples`` is a list of (ch1, ch2, ch3, ch4) tuples — one per sample.
    Each channel value is a little-endian uint32.
    Ch3/Ch4 carry the PPG signal (IN3 pair); Ch1/Ch2 are ambient.
    """
    samples: list[tuple[int, int, int, int]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def count(self) -> int:
        return len(self.samples)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _open(port: str, baud: int, timeout: float = 2.0) -> "serial.Serial":
    return serial.Serial(port, baud, timeout=timeout)


def _read_line(ser: "serial.Serial", timeout_s: float = 2.0) -> str:
    """Read until \\n or timeout; return decoded string (strip \\r\\n)."""
    deadline = time.monotonic() + timeout_s
    buf = b""
    while time.monotonic() < deadline:
        ch = ser.read(1)
        if ch:
            buf += ch
            if ch == b"\n":
                break
    return buf.decode("utf-8", errors="replace").rstrip("\r\n")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def test_connection(port: str, baud: int, timeout: float = 2.0) -> CommandResult:
    """Open the port, confirm it is not busy, then close it immediately.

    Returns a :class:`CommandResult` with ``error=None`` on success, or an
    error message describing why the connection failed (port busy, not found,
    permission denied, etc.).
    """
    if not SERIAL_AVAILABLE:
        return CommandResult(command="connect", error="pyserial not installed")
    try:
        ser = _open(port, baud, timeout=timeout)
        ser.close()
        return CommandResult(command="connect", response=f"OK — {port} @ {baud} baud")
    except serial.SerialException as exc:
        msg = str(exc)
        # Classify common failure modes for clearer UI feedback
        if "busy" in msg.lower() or "resource" in msg.lower():
            return CommandResult(command="connect", error=f"Port busy — another process may have {port} open")
        if "no such file" in msg.lower() or "could not open" in msg.lower():
            return CommandResult(command="connect", error=f"Port not found: {port}")
        if "permission" in msg.lower():
            return CommandResult(command="connect", error=f"Permission denied on {port} — check user/group access")
        return CommandResult(command="connect", error=msg)
    except Exception as exc:
        return CommandResult(command="connect", error=f"Unexpected error: {exc}")


def send_command(
    port: str,
    baud: int,
    command: str,
    response_timeout_s: float = 3.0,
    response_lines: int = 8,
) -> CommandResult:
    """Send a text command and collect up to *response_lines* lines of response.

    Opens a fresh connection, sends ``command\\r\\n``, reads until timeout or
    *response_lines* lines received, then closes.

    Parameters
    ----------
    port:
        Serial device path (e.g. ``/dev/tty.usbmodem101``).
    baud:
        Baud rate (typically 115200).
    command:
        Text command without line terminator.
    response_timeout_s:
        Total seconds to wait for responses after sending.
    response_lines:
        Stop after collecting this many non-empty lines.

    Returns
    -------
    CommandResult
    """
    if not SERIAL_AVAILABLE:
        return CommandResult(command=command, error="pyserial not installed")

    try:
        ser = _open(port, baud, timeout=response_timeout_s)
        ser.reset_input_buffer()
        ser.write((command + "\r\n").encode())
        ser.flush()

        lines: list[str] = []
        deadline = time.monotonic() + response_timeout_s
        while time.monotonic() < deadline and len(lines) < response_lines:
            line = _read_line(ser, timeout_s=0.5)
            if line:
                lines.append(line)

        ser.close()
        return CommandResult(command=command, response="\n".join(lines))

    except serial.SerialException as exc:
        return CommandResult(command=command, error=str(exc))
    except Exception as exc:
        return CommandResult(command=command, error=f"Unexpected error: {exc}")


BYTES_PER_SAMPLE = 16   # 4 channels × 4 bytes each
CHANNELS = ("ch1", "ch2", "ch3", "ch4")


def receive_binary_stream(
    port: str,
    baud: int,
    num_samples: int,
    stream_timeout_s: float = 30.0,
    progress_cb=None,
) -> StreamResult:
    """Send ``adpd ppg stream-bin <num_samples>`` and parse the binary response.

    Protocol (BINARY_STREAMING.md):
    1. Text start marker:  ``[BIN] Starting binary stream: N samples (4 channels per sample)``
    2. Payload:            ``num_samples × 16`` bytes — 4 × little-endian uint32 per sample
       - Ch1, Ch2: ambient
       - Ch3, Ch4: PPG signal (IN3 paired)
    3. Text end marker:    ``[BIN] Stream complete: N samples … sent``

    Parameters
    ----------
    port:
        Serial device path.
    baud:
        Baud rate.
    num_samples:
        Number of samples to request and expect.
    stream_timeout_s:
        Total seconds to allow for the entire stream.
    progress_cb:
        Optional callable(received: int, total: int) called periodically.

    Returns
    -------
    StreamResult with ``.samples`` as a list of (ch1, ch2, ch3, ch4) tuples.
    """
    if not SERIAL_AVAILABLE:
        return StreamResult(error="pyserial not installed")

    result = StreamResult()

    try:
        ser = _open(port, baud, timeout=stream_timeout_s)
        ser.reset_input_buffer()

        # Send the streaming command
        cmd = f"adpd ppg stream-bin {num_samples}\r\n"
        ser.write(cmd.encode())
        ser.flush()
        result.log.append(f">> {cmd.strip()}")

        # Wait for the text start marker
        deadline = time.monotonic() + stream_timeout_s
        start_seen = False
        while time.monotonic() < deadline:
            line = _read_line(ser, timeout_s=1.0)
            if line:
                result.log.append(f"<< {line}")
            if "[BIN] Starting binary stream" in line:
                start_seen = True
                break

        if not start_seen:
            ser.close()
            result.error = "Start marker not received — is the device connected and running?"
            return result

        # Read the binary payload: num_samples × 16 bytes
        total_bytes = num_samples * BYTES_PER_SAMPLE
        buf = bytearray()
        while len(buf) < total_bytes and time.monotonic() < deadline:
            remaining = total_bytes - len(buf)
            chunk = ser.read(min(remaining, 512))
            if chunk:
                buf.extend(chunk)
                if progress_cb:
                    progress_cb(len(buf) // BYTES_PER_SAMPLE, num_samples)

        if len(buf) < total_bytes:
            result.log.append(f"Timeout: got {len(buf)}/{total_bytes} bytes")

        # Parse: each 16-byte group → (ch1, ch2, ch3, ch4)
        parsed = len(buf) // BYTES_PER_SAMPLE
        raw = struct.unpack(f"<{parsed * 4}I", bytes(buf[:parsed * BYTES_PER_SAMPLE]))
        result.samples = [
            (raw[i * 4], raw[i * 4 + 1], raw[i * 4 + 2], raw[i * 4 + 3])
            for i in range(parsed)
        ]
        result.log.append(f"Parsed {parsed} samples ({parsed * BYTES_PER_SAMPLE} bytes)")

        # Read trailing end marker (best-effort)
        end_deadline = time.monotonic() + 2.0
        while time.monotonic() < end_deadline:
            line = _read_line(ser, timeout_s=0.5)
            if line:
                result.log.append(f"<< {line}")
            if "[BIN] Stream complete" in line:
                break

        ser.close()

    except serial.SerialException as exc:
        result.error = str(exc)
    except Exception as exc:
        result.error = f"Unexpected error: {exc}"

    return result
