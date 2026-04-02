# Binary Streaming

This document describes the current framed telemetry protocol used on the USB CDC stream interface.

The binary stream is emitted on the dedicated USB CDC interface only.

## 1. Transport Model

The firmware uses:

- `USART2`
  Text shell, command input, help, diagnostics
- `USB CDC`
  Framed binary telemetry

Control is text CLI only. USB is binary framed telemetry only. There is no binary control protocol.

## 2. Frame Types

The USB stream carries two frame types:

- `0xA1` -> heartbeat
- `0xA0` -> PPG sample

Heartbeats continue while the stream CDC interface remains open on the host.

## 3. Start a Stream

Issue the command on `USART2`:

```text
adpd ppg slota stream-bin 1000
```

Or:

```text
adpd ppg slotab stream-bin 200
adpd ppg slota2 stream-bin 500
```

The firmware then emits framed telemetry on USB CDC. Sample frames appear after streaming is started from `USART2`.

## 4. Frame Envelope

Binary frames use the shared USB protocol envelope defined in `usb_proto`.

Layout:

```text
[magic0][magic1][version][type][flags][seq_lo][seq_hi][len_lo][len_hi][payload...][crc_lo][crc_hi]
```

Field sizes:

- `magic0`: 1 byte
- `magic1`: 1 byte
- `version`: 1 byte
- `type`: 1 byte
- `flags`: 1 byte
- `sequence`: 2 bytes, little-endian
- `payload_len`: 2 bytes, little-endian
- `payload`: variable
- `crc16`: 2 bytes, little-endian

Constants:

- `magic0 = 0xA5`
- `magic1 = 0x5A`
- `version = 0x01`
- `type = 0xA1` for `USB_PROTO_MSG_STREAM_HEARTBEAT`
- `type = 0xA0` for `USB_PROTO_MSG_STREAM_PPG`

CRC:

- CRC-16/CCITT
- computed over `[version .. payload]`
- excludes the two sync bytes

## 5. PPG Payload

Current PPG payload body:

```text
[timestamp_ms:4][channel_0:4][channel_1:4]...[channel_n:4][optional_hr:4][optional_peak:4]
```

Encoding:

- all integer fields are little-endian
- timestamp is relative to stream start
- each channel sample is `uint32_t`
- HR, when present, is raw `float32`
- Peak, when present, is `uint32_t` with `0` or `1`

Payload sizes:

- Slot A, no HR: `4 + 4*4 = 20` bytes
- Slot A, HR enabled: `28` bytes
- Slot AB, no HR: `4 + 8*4 = 36` bytes
- Slot AB, HR enabled: `44` bytes
- Slot A2 depends on configured channel count and HR mode

## 6. Example Frame

Example payload for Slot A without HR:

- timestamp = `1234 ms`
- ch1 = `0x11223344`
- ch2 = `0x55667788`
- ch3 = `0x99AABBCC`
- ch4 = `0xDDEEFF00`

Payload bytes:

```text
D2 04 00 00
44 33 22 11
88 77 66 55
CC BB AA 99
00 FF EE DD
```

The full frame on the wire is:

```text
A5 5A 01 A0 00 <seq_lo> <seq_hi> 14 00 <payload 20 bytes> <crc_lo> <crc_hi>
```

`0x0014` is the payload length for the 20-byte Slot A case.

## 7. Parsing Notes

Host parser requirements:

- search for `0xA5 0x5A`
- read fixed header
- validate version
- read payload length
- read payload and CRC
- verify CRC-16/CCITT
- decode payload according to current stream mode

Do not assume line breaks or text markers on USB CDC.

## 8. Python Skeleton

```python
import serial
import struct

MAGIC = b"\xA5\x5A"

def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def read_exact(port, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = port.read(n - len(buf))
        if not chunk:
            raise TimeoutError("short read")
        buf.extend(chunk)
    return bytes(buf)

def read_frame(port):
    while True:
        if read_exact(port, 1) == MAGIC[:1]:
            if read_exact(port, 1) == MAGIC[1:]:
                break

    header = read_exact(port, 7)
    version, msg_type, flags, seq_lo, seq_hi, len_lo, len_hi = header
    payload_len = len_lo | (len_hi << 8)
    payload = read_exact(port, payload_len)
    crc_bytes = read_exact(port, 2)
    crc_rx = crc_bytes[0] | (crc_bytes[1] << 8)
    crc_calc = crc16_ccitt(bytes([version, msg_type, flags, seq_lo, seq_hi, len_lo, len_hi]) + payload)
    if crc_rx != crc_calc:
        raise ValueError("bad CRC")
    return msg_type, (seq_lo | (seq_hi << 8)), payload

def decode_ppg_payload(payload, num_channels, hr_enabled=False):
    offset = 0
    ts_ms = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    channels = []
    for _ in range(num_channels):
        channels.append(struct.unpack_from("<I", payload, offset)[0])
        offset += 4
    hr = None
    peak = None
    if hr_enabled:
        hr = struct.unpack_from("<f", payload, offset)[0]
        offset += 4
        peak = struct.unpack_from("<I", payload, offset)[0]
    return ts_ms, channels, hr, peak
```

## 9. Recommended Capture Flow

1. Open `USART2`.
2. Open USB CDC from your capture tool.
3. Start `adpd ppg <profile> stream-bin ...` from `USART2`.
4. Confirm `0xA0` sample frames interleave with `0xA1` heartbeat frames.

Example UART setup:

```text
adpd probe sdk
adpd ppg freq 100
adpd ppg slota stream-bin 1000
```

## 10. What Is Not on USB CDC

USB CDC should not be treated like a human terminal.

Do not expect:

- `help`
- prompts
- usage text
- command acknowledgements as shell text

Those belong on `USART2`.

## 11. Current Limits

This document reflects the current branch behavior:

- stream frames are binary and CRC-protected
- control remains UART text-based
- no binary control TLV protocol is used anymore
