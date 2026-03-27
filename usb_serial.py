"""usb_serial.py — USB/UART serial I/O helpers for the PPG AFE device.

Protocol reference: BINARY_STREAMING.md

Binary frame format (STREAM_MODE_BIN)
--------------------------------------
Every sample is wrapped in a 4-byte frame header:

    [0xAD][0x7E][type:1][len:1][payload: len bytes]

- Magic 0xAD 0x7E  — sync word; receiver scans for this to re-sync after
                     any stray text bytes (shell prompt, log lines, etc.)
- type             — stream type: 0x01 = PPG
- len              — payload byte count (20 for PPG)

PPG payload (20 bytes, all little-endian uint32_t):
    [timestamp_ms][ch1][ch2][ch3][ch4]
    - timestamp_ms : ms from stream start (relative, first sample ≈ 0)
    - ch1/ch2      : ambient channels
    - ch3/ch4      : PPG signal (IN3 paired, LED1A/LED1B)

Session markers (text lines, for logging only — parser does not depend on them):
    Start: ``[BIN] Starting binary stream: N samples (ts_ms + 4 ch, framed)``
    End:   ``[BIN] Stream complete: N samples (N*24 bytes) sent``

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
# Binary frame protocol constants  (must match stream_task.h)
# ─────────────────────────────────────────────────────────────────────────────

FRAME_MAGIC    = bytes([0xAD, 0x7E])   # sync word
FRAME_OVERHEAD = 4                      # magic[2] + type[1] + len[1]

STREAM_TYPE_PPG = 0x01

# Valid PPG payload sizes — determined by slot config and HR flag:
#   Slot A, no HR  : ts(4) + 4×ch(16)               = 20 bytes
#   Slot A + HR    : ts(4) + 4×ch(16) + hr(4)+peak(4) = 28 bytes
#   Slot AB, no HR : ts(4) + 8×ch(32)               = 36 bytes
#   Slot AB + HR   : ts(4) + 8×ch(32) + hr(4)+peak(4) = 44 bytes
PPG_VALID_SIZES = frozenset({20, 28, 36, 44})

# struct format strings for each payload size.
# HR field is float32; all other fields (ts, channels, peak) are uint32.
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

def _scan_to_magic(buf: bytearray) -> int:
    """Return index of first 0xAD 0x7E in buf, or -1 if not found."""
    for i in range(len(buf) - 1):
        if buf[i] == 0xAD and buf[i + 1] == 0x7E:
            return i
    return -1


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
        # Need at least FRAME_OVERHEAD bytes to read a header
        if len(buf) < FRAME_OVERHEAD:
            break

        idx = _scan_to_magic(buf)
        if idx == -1:
            # No magic found — keep last byte in case it's the start of a magic word
            if buf:
                log.append(f"[sync] discarded {len(buf) - 1} non-frame bytes")
            buf = buf[-1:]
            break

        if idx > 0:
            log.append(f"[sync] skipped {idx} bytes before frame magic")
            del buf[:idx]

        # buf[0:2] = magic, buf[2] = type, buf[3] = len
        frame_type = buf[2]
        payload_len = buf[3]
        total_frame = FRAME_OVERHEAD + payload_len

        if len(buf) < total_frame:
            break  # wait for more bytes

        payload = bytes(buf[FRAME_OVERHEAD:total_frame])
        del buf[:total_frame]

        if frame_type == STREAM_TYPE_PPG:
            if payload_len not in PPG_VALID_SIZES:
                log.append(
                    f"[warn] PPG frame unexpected payload len {payload_len} "
                    f"(valid: {sorted(PPG_VALID_SIZES)}) — skipped"
                )
                continue
            # Unpack using the format for this payload size.
            # HR field is float32; all others are uint32.
            vals = struct.unpack(_PPG_STRUCT_FMT[payload_len], payload)
            samples.append(vals)
            raw_payloads.extend(payload)
        else:
            log.append(f"[warn] unknown stream type 0x{frame_type:02X}, len={payload_len} — skipped")

    return samples, buf, bytes(raw_payloads)


def _wait_for_start_marker(ser, deadline: float, log: list[str]) -> bool:
    """Read text lines until the binary stream start banner or deadline.

    Returns True if the start marker was seen.
    Stray text lines are appended to log.
    """
    while time.monotonic() < deadline:
        line = _read_line(ser, timeout_s=1.0)
        if line:
            log.append(f"<< {line}")
        if "[BIN] Starting binary stream" in line:
            return True
    return False


def _read_end_marker(ser, log: list[str]) -> None:
    """Best-effort: read text lines for up to 2 s looking for the end banner."""
    end_deadline = time.monotonic() + 2.0
    while time.monotonic() < end_deadline:
        line = _read_line(ser, timeout_s=0.5)
        if line:
            log.append(f"<< {line}")
        if "[BIN] Stream complete" in line:
            break


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

    Uses framed binary protocol (magic + type + len + payload) so any stray
    shell text before or between frames is automatically skipped.

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
        ``"slota"`` (4 ch, 20-byte frames) or ``"slotab"`` (8 ch, 36-byte frames).
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
    cmd_str   = f"adpd ppg {slot} stream-bin {num_samples}{hr_suffix}"

    try:
        ser = _open(port, baud, timeout=stream_timeout_s)
        ser.reset_input_buffer()

        cmd = cmd_str + "\r\n"
        ser.write(cmd.encode())
        ser.flush()
        result.log.append(f">> {cmd.strip()}")

        deadline = time.monotonic() + stream_timeout_s

        if not _wait_for_start_marker(ser, deadline, result.log):
            ser.close()
            result.error = "Start marker not received — is the device connected and running?"
            return result

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

        _read_end_marker(ser, result.log)
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
        ``"slota"`` (4-channel, 20-byte frames) or ``"slotab"`` (8-channel, 36-byte frames).
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
    cmd_str   = f"adpd ppg {slot} stream-bin {num_samples}{hr_suffix}"

    try:
        ser = _open(port, baud, timeout=stream_timeout_s)
        ser.reset_input_buffer()

        cmd = cmd_str + "\r\n"
        ser.write(cmd.encode())
        ser.flush()
        log.append(f">> {cmd.strip()}")

        deadline = time.monotonic() + stream_timeout_s

        if not _wait_for_start_marker(ser, deadline, log):
            ser.close()
            yield [], b"", log + ["ERROR: Start marker not received — is the device connected?"], True
            return

        # Yield start-marker log before any data
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

        _read_end_marker(ser, log)
        ser.close()

        if log:
            yield [], b"", log, True

    except serial.SerialException as exc:
        yield [], b"", [f"ERROR: {exc}"], True
    except Exception as exc:
        yield [], b"", [f"ERROR: Unexpected: {exc}"], True
