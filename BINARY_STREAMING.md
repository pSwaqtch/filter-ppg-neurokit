# Binary Streaming Protocol

## Overview

The `adpd ppg stream-bin <count>` command streams raw PPG sensor data in binary format over USB/UART. This provides efficient data transmission for high-speed logging without text encoding overhead.

The command automatically starts PPG at the configured output data rate (ODR), streams the requested number of samples, then stops PPG. For text-based streaming with timestamps, use `adpd ppg stream <count>` instead.

**Key features:**
- **Efficient**: ~20-44 bytes per sample vs. ~50-150 bytes in text mode
- **Dynamic**: Support for Slot A (4 ch) or Slot AB (8 ch) via command mask
- **Integrated DSP**: Stream calculated Heart Rate and Peak flag alongside raw data
- **Real-time**: No buffering delays, direct FIFO to serial
- **Flexible ODR**: Configure sampling rate (10–400 Hz) before streaming
- **Timestamped**: Millisecond precision timestamps from MCU clock

---

## Quick Start

### Setup (one-time)
```bash
# Terminal: Connect to device at 115200 baud
screen /dev/ttyUSB0 115200
# or: miniterm.py /dev/ttyUSB0 115200

# Probe chip to verify SPI communication
> adpd probe
[SUCCESS] ID: 0x01C6

# Check chip configuration
> adpd read slota
--- GLOBAL & SLOT A CONFIGURATION (100 Hz PPG) ---
...
```

### Streaming at Default Frequency (100 Hz)
```bash
> adpd ppg slota stream-bin 500
[BIN] Starting binary stream: 500 samples (timestamp + 4 channels)
[BIN] Stream complete: 500 samples (10000 bytes) sent
```

### Custom Frequency (e.g., 50 Hz)
```bash
# Set output data rate to 50 Hz
> adpd ppg freq 50
ODR set to 50 Hz (will take effect on next PPG start)

# Stream 1000 samples at 50 Hz (~20 seconds)
> adpd ppg stream-bin 1000
[BIN] Starting binary stream: 1000 samples
[BIN] Stream complete: 1000 samples (20000 bytes) sent

# Verify the ODR change
> adpd diag
Current ODR Setting: 50 Hz
...
```

---

## Stream Format

**Per sample:** 20 bytes (timestamp + 4 channels)
- **Timestamp:** 4 bytes (uint32_t, milliseconds from stream start)
- **Channels:** 4 channels × 4 bytes each

**Byte order:** Little-endian (LSB first) per uint32_t value
**Data rate:** ~100 Hz with 20-byte samples = ~2 KB/sec

### Binary Layout (Per Sample)

**Structure:** `[Timestamp: 4][Channels: 4*N][Optional: HR: 4][Optional: Peak: 4]`

```
[Timestamp: 4 bytes] [Ch1: 4 bytes] ... [ChN: 4 bytes] [HR: 4 bytes] [Peak: 4 bytes]

Each field (timestamp, channels, HR, peak):
[Byte 0] [Byte 1] [Byte 2] [Byte 3]
   LSB     ...      ...      MSB
= uint32_t / float32 value (little-endian)
```

**Timestamp:** Starts at 0 when stream begins, increments in milliseconds
**Channels:** 4 channels (Slot A) or 8 channels (Slot AB)
**HR:** BPM value (float32) — only sent if `hr on` is in the command
**Peak:** Flag (0 or 1) — only sent if `hr on` is in the command

### Example

Sample with timestamp 1234 ms and channel values:
- Timestamp: `0x000004D2` (1234 in decimal)
- Ch1: `0xAABBCCDD`
- Ch2: `0x11223344`
- Ch3: `0x55667788`
- Ch4: `0x99AABBCC`

Transmitted bytes (20 total):
```
Timestamp:  D2 04 00 00
Channel 1:  DD CC BB AA
Channel 2:  44 33 22 11
Channel 3:  88 77 66 55
Channel 4:  CC BB AA 99
```

Reconstruction (little-endian):
```python
# For each 4-byte chunk:
value = byte[0] | (byte[1] << 8) | (byte[2] << 16) | (byte[3] << 24)
```

## Protocol Markers

**Start (after `adpd ppg stream-bin <count>`):**
```
[BIN] Starting binary stream: N samples (timestamp + 4 channels)\r\n
```
(Text message, not binary)

**End:**
```
\r\n[BIN] Stream complete: N samples (80 bytes) sent\r\n
```
(Text message, not binary — 80 bytes = 4 samples × 20 bytes per sample, unless N is different)

## Transmission Notes

- No packet framing: raw 20-byte chunks stream continuously
- No checksums or sequence numbers
- 1 timestamp + 4 channel uint32_t values = 20 bytes per complete sample
- Timestamp starts at 0 ms, incremented per sample based on system time
- Timing controlled by FIFO polling (10 ms delay between reads → ~100 Hz)
- Data integrity depends on USB/UART link stability
- Channels are IN3 paired for PPG (Ch3/Ch4) with Ch1/Ch2 showing ambient

## Python Parser Example

```python
import struct
import sys

def parse_binary_stream(data, num_samples):
    """Parse binary stream into (timestamp_ms, ch1, ch2, ch3, ch4) tuples"""
    samples = []
    for i in range(num_samples):
        offset = i * 20  # 20 bytes per sample (timestamp + 4 channels × 4 bytes)
        if offset + 20 <= len(data):
            # Unpack timestamp + 4 little-endian uint32 values
            ts, ch1, ch2, ch3, ch4 = struct.unpack('<IIIII', data[offset:offset+20])
            samples.append((ts, ch1, ch2, ch3, ch4))
    return samples

# Usage:
# with open('stream.bin', 'rb') as f:
#     data = f.read()
# samples = parse_binary_stream(data, count)
# for ts, ch1, ch2, ch3, ch4 in samples:
#     print(f"T={ts}ms: Ch1={ch1}, Ch2={ch2}, Ch3={ch3}, Ch4={ch4}")
```

## Supported Output Data Rates (ODR)

Available sampling frequencies (all are 4-channel, 16-bit signals):

| ODR (Hz) | Period (ms) | Use Case |
|----------|-------------|----------|
| 10 | 100 | Low-power, stationary monitoring |
| 25 | 40 | Battery-constrained devices |
| 50 | 20 | Standard wrist-worn PPG |
| **100** | **10** | **Default; most common PPG use** |
| 200 | 5 | High-fidelity analysis, research |
| 400 | 2.5 | High-speed data capture, artifact detection |

**Set ODR before starting stream:**
```bash
> adpd ppg freq 200
ODR set to 200 Hz (will take effect on next PPG start)
> adpd ppg stream-bin 5000
```

## USB vs UART Performance

| Interface | Bandwidth | Throughput (20 bytes/sample) |
|-----------|-----------|------------------------------|
| USB FS (12 Mbps) | 12 Mbps | ~60,000 samples/sec |
| UART 115200 | 115.2 kbps | ~1,440 samples/sec |

**Current implementation:** Streams at selected ODR (10–400 Hz = 200–8,000 bytes/sec), well below both limits.
- **USB**: Ideal for research/development (fast file capture)
- **UART**: Suitable for all ODRs up to 200 Hz without delays

## Capturing Stream to File

**Shell workflow:**
```bash
# Terminal 1: Run device shell
# Issue command: adpd ppg stream-bin 1000

# Terminal 2: Capture to file
stty -f /dev/ttyUSB0 115200 raw
(sleep 0.5; cat < /dev/ttyUSB0) | head -c $((1000 * 20)) > stream.bin
```

**Python with pyserial (automated):**
```python
import serial
import time

port = serial.Serial('/dev/ttyUSB0', 115200, timeout=10)
num_samples = 1000

# Send command
port.write(b'adpd ppg stream-bin 1000\r\n')
time.sleep(0.1)

# Read start marker (skip text)
port.readline()

# Read N samples × 20 bytes (timestamp + 4 channels)
data = port.read(num_samples * 20)

with open('stream.bin', 'wb') as f:
    f.write(data)

# Read end marker
port.readline()
port.close()
```

## Notes

- Data immediately follows the start marker message
- No gaps between samples (continuous stream)
- Payload length depends on configuration:
  - **Slot A (4 ch)**: 20 bytes/sample
  - **Slot AB (8 ch)**: 36 bytes/sample
  - **HR Enabled**: Adds 8 bytes (Peak + BPM) to any slot mode
- Timestamp is relative to stream start (first sample = 0 ms)
- Handle text markers (start/end messages) before parsing binary data
- Recommended: Discard first 1-2 samples if timing is critical (FIFO warm-up)
- For sustained logging, pipe directly to file or Python script (avoid terminal buffering)
- Timestamp allows time-series analysis and synchronization with external events
