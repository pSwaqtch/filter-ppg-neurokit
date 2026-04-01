"""usb_serial.py — USB/UART serial I/O helpers for the PPG AFE device.

Protocol reference: BINARY_STREAMING.md

Binary frame format
-------------------
Frames use the shared USB protocol envelope:

    [0xA5][0x5A][version][type][flags][seq_lo][seq_hi][len_lo][len_hi]
    [payload...][crc_lo][crc_hi]

- magic            — sync word; receiver scans for this to re-sync after any
                      stray bytes
- version          — protocol version (currently 0x01)
- type             — stream message type (0xA0 for PPG streaming)
- flags            — currently reserved
- sequence         — little-endian frame sequence number
- payload_len      — little-endian payload byte count
- crc16            — CRC-16/CCITT over [version..payload], little-endian

PPG payload sizes supported here:
    20 bytes  — Slot A, no HR
    28 bytes  — Slot A, HR enabled
    36 bytes  — Slot AB, no HR
    44 bytes  — Slot AB, HR enabled

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
        print("[DEBUG] serial module not available")
        return []
    try:
        ports = serial.tools.list_ports.comports()
        result = sorted(p.device for p in ports)
        print(f"[DEBUG] list_serial_ports: found {len(result)} port(s): {result}")
        return result
    except Exception as e:
        print(f"[ERROR] list_serial_ports failed: {type(e).__name__}: {e}")
        return []


def describe_ports() -> list[dict]:
    """Return rich descriptions (device, description, hwid) for each port."""
    if not SERIAL_AVAILABLE:
        print("[DEBUG] serial module not available in describe_ports")
        return []
    try:
        result = [
            {"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        ]
        print(f"[DEBUG] describe_ports: found {len(result)} port(s)")
        return result
    except Exception as e:
        print(f"[ERROR] describe_ports failed: {type(e).__name__}: {e}")
        return []


def find_port_by_description(description_substring: str) -> str:
    """Return the first port whose description contains the given text."""
    needle = description_substring.lower()
    for port in describe_ports():
        description = str(port.get("description", "")).lower()
        if needle in description:
            return str(port.get("device", ""))
    return ""


def find_adpd7000_port_pair() -> dict[str, str]:
    """Best-effort auto-detection of the ADPD7000 control/stream port pair."""
    return {
        "control_port": find_port_by_description("adpd7000 control"),
        "stream_port": find_port_by_description("adpd7000 stream"),
    }


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

    ``samples`` is a list of (timestamp_ms, ch1, ch2, ch3, ch4) tuples.
    - ``timestamp_ms``: uint32, ms from stream start (first sample = 0)
    - Ch1/Ch2: ambient channels
    - Ch3/Ch4: PPG signal (IN3 paired)
    """
    samples: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    raw_bytes: bytes = b""          # verbatim payload bytes for debug export
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

def find_port_owner(port: str) -> Optional[tuple[int, str]]:
    """Return ``(pid, process_name)`` of the process holding *port* open, or None.

    Uses ``lsof`` (macOS/Linux). Returns None if nothing found or lsof unavailable.
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["lsof", "-t", port], stderr=subprocess.DEVNULL, text=True
        ).strip()
        if not out:
            return None
        pid = int(out.splitlines()[0])
        # Get process name
        name_out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "comm="], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return pid, name_out or "unknown"
    except Exception:
        return None


def force_release_port(port: str) -> CommandResult:
    """Kill the process holding *port* open so it can be reconnected.

    Finds the owning PID via ``lsof``, sends SIGTERM (then SIGKILL if needed),
    waits up to 2 s for the port to free, then returns success or error.
    """
    import subprocess
    import signal as _signal

    owner = find_port_owner(port)
    if owner is None:
        # No owner found — port may already be free; attempt connect anyway
        return CommandResult(command="force_release",
                             response="No owning process found — port may already be free")

    pid, name = owner
    try:
        import os
        os.kill(pid, _signal.SIGTERM)
        # Give it up to 1 s to die gracefully, then SIGKILL
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)   # check still alive
                time.sleep(0.05)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, _signal.SIGKILL)
            time.sleep(0.2)

        return CommandResult(
            command="force_release",
            response=f"Terminated {name} (PID {pid}) — port {port} should be free",
        )
    except PermissionError:
        return CommandResult(
            command="force_release",
            error=f"Permission denied killing PID {pid} ({name}) — try running with sudo",
        )
    except Exception as exc:
        return CommandResult(command="force_release", error=str(exc))


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
            return CommandResult(command="connect", error=f"PORT_BUSY: another process has {port} open")
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
    response_lines: int = 50,
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
        Stop after collecting this many non-empty lines.  Default 50 to handle
        verbose commands like ``adpd read slota`` (25+ register rows).

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


# ─────────────────────────────────────────────────────────────────────────────
# Binary frame protocol constants  (must match the documented USB envelope)
# ─────────────────────────────────────────────────────────────────────────────

FRAME_MAGIC = bytes([0xA5, 0x5A])      # sync word
FRAME_VERSION = 0x01
FRAME_TYPE_PPG = 0xA0
STREAM_TYPE_PPG = FRAME_TYPE_PPG       # compatibility alias
FRAME_HEADER_SIZE = 9                  # magic[2] + version/type/flags/seq/len
FRAME_TRAILER_SIZE = 2                 # crc16
FRAME_OVERHEAD = FRAME_HEADER_SIZE + FRAME_TRAILER_SIZE

PPG_VALID_SIZES = frozenset({20, 28, 36, 44})

# Struct format strings for each payload size.
# HR field is float32; all other fields (timestamp, channels, peak) are uint32.
_PPG_STRUCT_FMT: dict[int, str] = {
    20: "<5I",    # ts + 4ch
    28: "<5IfI",  # ts + 4ch + hr(float) + peak
    36: "<9I",    # ts + 8ch
    44: "<9IfI",  # ts + 8ch + hr(float) + peak
}

# Number of ADC channels per payload size (excludes timestamp, HR, Peak)
PPG_N_CHANNELS: dict[int, int] = {20: 4, 28: 4, 36: 8, 44: 8}

LIVE_CHUNK_BYTES = 240  # ~10 complete framed Slot-A samples per read


# ─────────────────────────────────────────────────────────────────────────────
# Internal: framed binary parser
# ─────────────────────────────────────────────────────────────────────────────

def _crc16_ccitt(data: bytes) -> int:
    """Return CRC-16/CCITT-FALSE for *data*."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _scan_to_magic(buf: bytearray) -> int:
    """Return index of first magic word in *buf*, or -1 if not found."""
    return buf.find(FRAME_MAGIC)


def _decode_ppg_payload(payload: bytes) -> tuple:
    """Decode a PPG payload into a tuple of integers/floats."""
    payload_len = len(payload)
    fmt = _PPG_STRUCT_FMT.get(payload_len)
    if fmt is None:
        raise ValueError(f"unexpected PPG payload length {payload_len}")
    return struct.unpack(fmt, payload)


def _parse_frames(buf: bytearray, log: list[str]) -> tuple[list[tuple], bytearray, bytes]:
    """Extract all complete frames from buf.

    Returns (samples, remaining_buf, raw_payload_bytes).
    Stray bytes before any frame header are logged and discarded.
    Unknown stream types are logged and skipped.

    Decoded sample tuples vary by payload size:
      20 bytes → (ts_ms, ch1, ch2, ch3, ch4)
      28 bytes → (ts_ms, ch1, ch2, ch3, ch4, hr_bpm, peak)   # hr is float32
      36 bytes → (ts_ms, ch1..ch8)
      44 bytes → (ts_ms, ch1..ch8, hr_bpm, peak)              # hr is float32
    """
    samples: list[tuple] = []
    raw_payloads = bytearray()

    while True:
        idx = _scan_to_magic(buf)
        if idx == -1:
            if buf:
                keep = 1 if buf[-1] == FRAME_MAGIC[0] else 0
                discarded = len(buf) - keep
                if discarded:
                    log.append(f"[sync] discarded {discarded} non-frame bytes")
                if keep:
                    buf[:] = buf[-1:]
                else:
                    buf.clear()
            break

        if idx > 0:
            log.append(f"[sync] skipped {idx} bytes before frame magic")
            del buf[:idx]

        if len(buf) < FRAME_HEADER_SIZE:
            break  # wait for more bytes

        version = buf[2]
        frame_type = buf[3]
        flags = buf[4]
        seq_lo = buf[5]
        seq_hi = buf[6]
        payload_len = buf[7] | (buf[8] << 8)
        total_frame = FRAME_HEADER_SIZE + payload_len + FRAME_TRAILER_SIZE

        if len(buf) < total_frame:
            break  # wait for more bytes

        payload = bytes(buf[FRAME_HEADER_SIZE:FRAME_HEADER_SIZE + payload_len])
        crc_rx = buf[FRAME_HEADER_SIZE + payload_len] | (buf[FRAME_HEADER_SIZE + payload_len + 1] << 8)
        crc_calc = _crc16_ccitt(bytes(buf[2:FRAME_HEADER_SIZE]) + payload)

        del buf[:total_frame]

        if version != FRAME_VERSION:
            log.append(f"[warn] unsupported frame version 0x{version:02X} — skipped")
            continue

        if frame_type != FRAME_TYPE_PPG:
            log.append(f"[warn] unknown stream type 0x{frame_type:02X}, len={payload_len} — skipped")
            continue

        if payload_len not in PPG_VALID_SIZES:
            log.append(
                f"[warn] PPG frame unexpected payload len {payload_len} "
                f"(valid: {sorted(PPG_VALID_SIZES)}) — skipped"
            )
            continue

        if crc_rx != crc_calc:
            seq = seq_lo | (seq_hi << 8)
            log.append(
                f"[warn] bad CRC for seq=0x{seq:04X} "
                f"(rx=0x{crc_rx:04X}, calc=0x{crc_calc:04X}) — skipped"
            )
            continue

        try:
            vals = _decode_ppg_payload(payload)
        except ValueError as exc:
            log.append(f"[warn] {exc} — skipped")
            continue

        samples.append(vals)
        raw_payloads.extend(payload)

    return samples, buf, bytes(raw_payloads)


# ─────────────────────────────────────────────────────────────────────────────
# Public streaming API
# ─────────────────────────────────────────────────────────────────────────────

def receive_binary_stream(
    port: str,
    baud: int,
    num_samples: int,
    stream_timeout_s: float = 30.0,
    progress_cb=None,
    slot: str = "slota",
    hr_channel: str | None = None,
) -> StreamResult:
    """Send ``adpd ppg <slot> stream-bin <num_samples>`` and parse the binary response.

    Uses the framed binary protocol so any stray bytes before or between frames
    are automatically skipped.

    Parameters
    ----------
    port:
        Serial device path (e.g. ``/dev/tty.usbmodem101``).
    baud:
        Baud rate (typically 115200).
    num_samples:
        Number of PPG samples to request.
    stream_timeout_s:
        Total seconds to allow for the entire stream.
    progress_cb:
        Optional callable(received_count: int, total: int).
    slot:
        ``"slota"`` (4 ch, 20/28-byte frames) or ``"slotab"`` (8 ch, 36/44-byte frames).
    hr_channel:
        If set (e.g. ``"sAch3"``), appends ``hr on <channel>`` to the command so
        the firmware DSP pipeline runs and adds HR + Peak fields to each frame
        (+8 bytes: float32 BPM + uint32 peak flag).

    Returns
    -------
    StreamResult with ``.samples`` as a list of variable-length tuples.
    Tuple layout depends on slot/HR:
      (ts_ms, ch1..ch4)                  — Slot A, no HR
      (ts_ms, ch1..ch4, hr_bpm, peak)    — Slot A + HR
      (ts_ms, ch1..ch8)                  — Slot AB, no HR
      (ts_ms, ch1..ch8, hr_bpm, peak)    — Slot AB + HR
    """
    if not SERIAL_AVAILABLE:
        return StreamResult(error="pyserial not installed")

    result = StreamResult()
    # Build command string — slot prefix is required; hr suffix is optional
    hr_suffix = f" hr on {hr_channel}" if hr_channel else ""
    cmd_str = f"adpd ppg {slot} stream-bin {num_samples}{hr_suffix}"

    try:
        ser = _open(port, baud, timeout=stream_timeout_s)
        ser.reset_input_buffer()

        cmd = cmd_str + "\r\n"
        ser.write(cmd.encode())
        ser.flush()
        result.log.append(f">> {cmd.strip()}")

        deadline = time.monotonic() + stream_timeout_s

        # Accumulate raw bytes and scan for frames
        buf = bytearray()
        all_raw = bytearray()

        while len(result.samples) < num_samples and time.monotonic() < deadline:
            chunk = ser.read(min(512, LIVE_CHUNK_BYTES))
            if not chunk:
                continue

            buf.extend(chunk)
            new_samples, buf, raw_chunk = _parse_frames(buf, result.log)
            result.samples.extend(new_samples)
            all_raw.extend(raw_chunk)

            if progress_cb:
                progress_cb(len(result.samples), num_samples)

        if len(result.samples) < num_samples:
            result.log.append(
                f"Timeout: got {len(result.samples)}/{num_samples} samples"
            )

        result.raw_bytes = bytes(all_raw)
        result.log.append(
            f"Parsed {len(result.samples)} samples ({len(all_raw)} payload bytes)"
        )
        ser.close()

    except serial.SerialException as exc:
        result.error = str(exc)
    except Exception as exc:
        result.error = f"Unexpected error: {exc}"

    return result


def stream_binary_live(
    port: str,
    baud: int,
    num_samples: int,
    chunk_bytes: int = LIVE_CHUNK_BYTES,
    stream_timeout_s: float = 30.0,
    slot: str = "slota",
    hr_channel: str | None = None,
):
    """Generator: yields parsed sample chunks as they arrive for live display.

    Each yield is ``(new_samples, new_raw_bytes, new_log_lines, is_final)``:
    - ``new_samples``:   list of variable-length tuples (see receive_binary_stream)
    - ``new_raw_bytes``: verbatim payload bytes for those samples
    - ``new_log_lines``: protocol/sync log lines since the last yield
    - ``is_final``:      True on the last yield (done or error)

    Log lines starting with ``ERROR:`` indicate a failure.
    Stray bytes before/between frames are logged as ``[sync]`` lines.

    Parameters
    ----------
    slot:
        ``"slota"`` (4-channel, 20/28-byte frames) or ``"slotab"`` (8-channel, 36/44-byte frames).
    hr_channel:
        Optional channel spec (e.g. ``"sAch3"``); appends ``hr on <ch>`` to the
        command, adding HR BPM (float32) and Peak flag (uint32) to each frame.
    """
    if not SERIAL_AVAILABLE:
        yield [], b"", ["ERROR: pyserial not installed"], True
        return

    log: list[str] = []
    # Build the command — slot prefix required, HR suffix optional
    hr_suffix = f" hr on {hr_channel}" if hr_channel else ""
    cmd_str = f"adpd ppg {slot} stream-bin {num_samples}{hr_suffix}"

    try:
        ser = _open(port, baud, timeout=stream_timeout_s)
        ser.reset_input_buffer()

        cmd = cmd_str + "\r\n"
        ser.write(cmd.encode())
        ser.flush()
        log.append(f">> {cmd.strip()}")

        deadline = time.monotonic() + stream_timeout_s

        yield [], b"", log, False
        log = []

        buf = bytearray()
        received = 0

        while received < num_samples and time.monotonic() < deadline:
            chunk = ser.read(chunk_bytes)
            if not chunk:
                continue

            buf.extend(chunk)
            new_samples, buf, raw_chunk = _parse_frames(buf, log)

            if not new_samples and not log:
                continue

            received += len(new_samples)
            is_done = received >= num_samples
            yield new_samples, raw_chunk, log, is_done
            log = []

        if received < num_samples:
            log.append(f"Timeout: got {received}/{num_samples} samples")
        ser.close()

        if log:
            yield [], b"", log, True

    except serial.SerialException as exc:
        yield [], b"", [f"ERROR: {exc}"], True
    except Exception as exc:
        yield [], b"", [f"ERROR: Unexpected: {exc}"], True


def stream_binary_live_dual_port(
    control_port: str,
    stream_port: str,
    baud: int,
    num_samples: int,
    chunk_bytes: int = LIVE_CHUNK_BYTES,
    stream_timeout_s: float = 30.0,
    slot: str = "slota",
    hr_channel: str | None = None,
):
    """Generator: start capture on the control port and read frames from the stream port."""
    if not SERIAL_AVAILABLE:
        yield [], b"", ["ERROR: pyserial not installed"], True
        return

    if not control_port or not stream_port:
        yield [], b"", ["ERROR: Both control and stream ports are required"], True
        return

    if control_port == stream_port:
        yield [], b"", ["ERROR: Control port and stream port must be different"], True
        return

    log: list[str] = []
    hr_suffix = f" hr on {hr_channel}" if hr_channel else ""
    cmd_str = f"adpd ppg {slot} stream-bin {num_samples}{hr_suffix}"

    try:
        ctrl = _open(control_port, baud, timeout=2.0)
        stream = _open(stream_port, baud, timeout=stream_timeout_s)
        ctrl.reset_input_buffer()
        stream.reset_input_buffer()

        ctrl.write((cmd_str + "\r\n").encode())
        ctrl.flush()
        log.append(f">> [{control_port}] {cmd_str}")
        yield [], b"", log, False
        log = []

        deadline = time.monotonic() + stream_timeout_s
        buf = bytearray()
        received = 0

        while received < num_samples and time.monotonic() < deadline:
            chunk = stream.read(chunk_bytes)
            if not chunk:
                continue

            buf.extend(chunk)
            new_samples, buf, raw_chunk = _parse_frames(buf, log)
            if not new_samples and not log:
                continue

            received += len(new_samples)
            yield new_samples, raw_chunk, log, received >= num_samples
            log = []

        if received < num_samples:
            log.append(f"Timeout: got {received}/{num_samples} samples")

        ctrl.close()
        stream.close()

        if log:
            yield [], b"", log, True

    except serial.SerialException as exc:
        yield [], b"", [f"ERROR: {exc}"], True
    except Exception as exc:
        yield [], b"", [f"ERROR: Unexpected: {exc}"], True
