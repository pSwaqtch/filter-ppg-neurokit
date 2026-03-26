# User Guide: ADPD7000 PPG Streaming Firmware

This guide walks you through all available commands and workflows for capturing, analyzing, and leveraging PPG data from your BlackPill evaluation board.

---

## Table of Contents

1. [Initial Setup](#initial-setup)
2. [Command Reference](#command-reference)
3. [Streaming Workflows](#streaming-workflows)
4. [Data Analysis](#data-analysis)
5. [Troubleshooting](#troubleshooting)
6. [Advanced Usage](#advanced-usage)

---

## Initial Setup

### Hardware Connections
- **MCU**: STM32F411 BlackPill
- **Sensor**: ADPD7000 connected via SPI (PB0=CS, PA5=SCK, PA6=MISO, PA7=MOSI)
- **Serial**: UART2 @ 115200 (PA2=TX, PA3=RX) or USB CDC
- **Power**: 3.3V regulated supply

### Connect to Shell
```bash
# Linux/Mac
screen /dev/ttyUSB0 115200
# or
miniterm.py /dev/ttyUSB0 115200

# Windows (use device manager to find COM port)
putty -serial COM3 -serspeed 115200
```

### Verify Connection
```bash
> help
--- SHELL HELP ---
  help      : Show available commands
  interface : Toggle shell interfaces: interface <usb|uart> <on|off>
  scan      : Scan bus: scan i2c, scan spi
  reset     : Reset MCU via NVIC
  eeprom    : EEPROM commands: info, test, read <addr>, write <addr> <val>
  adpd      : ADPD core: probe [sdk], dump, read <reg>, write <reg> <val>, reset, ppg, diag
```

---

## Command Reference

### System Commands

#### `help`
Display all available commands.
```bash
> help
```

#### `reset`
Reset the MCU (useful if shell becomes unresponsive).
```bash
> reset
Resetting MCU via NVIC...
```

#### `interface <usb|uart> <on|off>`
Enable/disable USB or UART communication.
```bash
> interface usb on     # Enable USB CDC
> interface uart off   # Disable UART
```

---

### Diagnostics & Hardware Probing

#### `adpd probe [sdk]`
Verify ADPD7000 chip communication.
```bash
# Raw SPI read (checks if SPI is working)
> adpd probe
[SUCCESS] ID: 0x01C6

# SDK-level probe (checks SDK initialization path)
> adpd probe sdk
[SUCCESS] SDK ID: 0x01C6, Rev: 0x00
```

**Troubleshooting:**
- If you get `[WARN] Found data but ID mismatch: 0x00E3` → SPI clock is too fast. Check main.c prescaler.
- If you get `[FAIL] SPI Communication Error` → Check SPI Mode 3 (CPOL=1, CPHA=1) in main.c.

#### `adpd dump`
Display first 32 registers (good for quick health check).
```bash
> adpd dump
ADPD7000 Register Map (First 32 Registers):
     | 00   01   02   03   04   05   06   07
-----|--------------------------------------
0000 | 0000 0000 0040 0000 0000 0000 00A0 0000
...
```

#### `adpd diag`
Show full PPG configuration (AFE, LED, FIFO, signal path). Run this after `adpd ppg start` to verify setup.
```bash
> adpd ppg start
[SUCCESS] PPG running at 100 Hz...

> adpd diag
--- ADPD DIAGNOSTIC ---
Current ODR Setting: 100 Hz

Global Configuration:
  0x006 (FIFO_TH): 0x00A0
  0x010 (OPMODE): 0x0011    ← GO bit should be 1

Slot A - LED Control:
  0x128 (LED_POW12): 0x000A  ← LED current (10 = ~15.7 mA)
  0x129 (LED_MODE): 0x0000   ← LED1A/LED1B

Slot A - Data:
  0x137 (DECIMATE_A/CHANNEL_EN): 0xC010
     -> CHANNEL_EN[15:14] = 0x3 (0x3=4ch, 0x2=3ch, 0x1=2ch, 0x0=1ch)
```

#### `adpd read <reg>` / `adpd write <reg> <val>`
Read or modify individual registers (advanced use only).
```bash
> adpd read 0x010
ADPD7000 Reg 0x0010: 0x0011

> adpd write 0x128 0x000A    # Set LED current to 10
Writing 0x000A to ADPD7000 Reg 0x0128...
SUCCESS
```

---

### PPG Control & Streaming

#### `adpd ppg start`
Initialize PPG at the currently configured ODR and start acquisition.
```bash
> adpd ppg start
Initializing ADPD7000 PPG at 100 Hz on Slot A...
[SUCCESS] PPG running at 100 Hz. Use 'adpd ppg stream <n>' to read samples.
```

#### `adpd ppg stop`
Stop PPG acquisition and return to standby.
```bash
> adpd ppg stop
[OK] PPG stopped.
```

#### `adpd ppg freq <hz>`
Set output data rate before next `adpd ppg start`. Supported: 10, 25, 50, 100 (default), 200, 400 Hz.
```bash
> adpd ppg freq 50
ODR set to 50 Hz (will take effect on next PPG start)

> adpd ppg freq
Supported ODRs: 10 Hz, 25 Hz, 50 Hz, 100 Hz, 200 Hz, 400 Hz
Current ODR: 50 Hz
```

#### `adpd ppg stream <count>`
Stream `count` samples as CSV (human-readable, for quick visualization).
```bash
> adpd ppg stream 10
Starting PPG...
Streaming 10 samples @ ~100 Hz (CSV format for plotting)...
Time_ms,Ch1,Ch2,Ch3,Ch4
0,0,0,123456,234567
10,0,0,125678,236789
20,0,0,127890,238901
...
Stream done: 10/10 samples with data (100%)
```

**Output format:**
- Time_ms: Milliseconds from stream start
- Ch1, Ch2: Ambient channels (usually near-zero if LED off)
- Ch3, Ch4: Signal channels (should show pulsatile waveform when LED on)

#### `adpd ppg stream-bin <count>`
Stream `count` samples in binary format (efficient for logging and analysis).
```bash
> adpd ppg stream-bin 100
[BIN] Starting binary stream: 100 samples (timestamp + 4 channels)
[BIN] Stream complete: 100 samples (2000 bytes) sent
```

**Output format:** See [BINARY_STREAMING.md](BINARY_STREAMING.md) for details.

---

### EEPROM (External Memory)

#### `eeprom info`
Display EEPROM capacity and usage statistics.
```bash
> eeprom info
--- EEPROM Analysis: M24C32 [0x51] ---
Capacity    : 4096 Bytes (32 Kbit)
Addressing  : 2-byte (16-bit)
  Used : 512 Bytes (12.5%)
  Free : 3584 Bytes (87.5%)
```

#### `eeprom read <addr>` / `eeprom write <addr> <val>`
Read or write a single byte from external EEPROM.
```bash
> eeprom read 0x0100
M24C32 [0x51] @ 0x0100: 0x42

> eeprom write 0x0100 0xFF
Writing 0xFF to 0x0100...
SUCCESS
```

#### `eeprom test`
Run a quick write-verify test on multiple addresses.
```bash
> eeprom test
Starting Randomized Write-Check on M24C32...
Write 0xAA to 0x0010... OK
Write 0x55 to 0x0150... OK
Write 0x12 to 0x0A00... OK
Test Finished. Use 'eeprom info' for stats.
```

---

### Bus Scanning

#### `scan i2c`
Probe all I2C1 addresses (0x01–0x7F) for connected devices.
```bash
> scan i2c
Scanning I2C1 (0x01-0x7F)...
  [0x51] REPLIED   ← M24C32 EEPROM
Scan finished (1 devices found).
```

#### `scan spi`
Test basic SPI communication with ADPD7000 (checks a few key registers).
```bash
> scan spi
Scanning SPI1 (PB0 CS Active Low)...
  Reg 0x00: 0x0000
  Reg 0x01: 0x01C6   ← Chip ID
  Reg 0x08: 0x0000
  Reg 0x0F: 0x0002
```

---

## Streaming Workflows

### Workflow 1: Quick Data Capture (Text CSV)
**Goal:** Get a few samples for manual inspection.

```bash
# Terminal
> adpd ppg stream 20
Time_ms,Ch1,Ch2,Ch3,Ch4
0,0,0,248516,356742
10,0,0,251803,359284
...
```

**Copy-paste into Excel/Python for immediate visualization.**

---

### Workflow 2: High-Volume Binary Logging to File
**Goal:** Capture 10,000 samples at 100 Hz for detailed analysis (~100 seconds).

```bash
# Terminal 1: Run device shell
> adpd ppg stream-bin 10000
[BIN] Starting binary stream: 10000 samples...

# Terminal 2: Capture to file (macOS/Linux)
(sleep 0.2; cat < /dev/ttyUSB0) | head -c $((10000 * 20)) > ppg_data.bin
```

**Parse in Python:**
```python
import struct
with open('ppg_data.bin', 'rb') as f:
    data = f.read()

samples = []
for i in range(10000):
    offset = i * 20
    ts, ch1, ch2, ch3, ch4 = struct.unpack('<IIIII', data[offset:offset+20])
    samples.append((ts, ch1, ch2, ch3, ch4))

# Plot with matplotlib
import matplotlib.pyplot as plt
ts = [s[0] for s in samples]
ch3 = [s[3] for s in samples]  # Primary signal
plt.plot(ts, ch3, label='Ch3 (PPG Signal)')
plt.xlabel('Time (ms)')
plt.ylabel('Signal Value')
plt.legend()
plt.show()
```

---

### Workflow 3: Different Sampling Rates

#### 10 Hz (Low Power)
```bash
> adpd ppg freq 10
ODR set to 10 Hz...
> adpd ppg stream 100        # 10 seconds of data
```
**Use case:** Wellness monitoring, minimal power consumption.

#### 200 Hz (High Fidelity)
```bash
> adpd ppg freq 200
ODR set to 200 Hz...
> adpd ppg stream-bin 5000   # 25 seconds of data
```
**Use case:** Research, arrhythmia detection, artifact analysis.

#### 400 Hz (Maximum)
```bash
> adpd ppg freq 400
ODR set to 400 Hz...
> adpd ppg stream-bin 2000   # 5 seconds of high-speed capture
```
**Use case:** Motion artifact characterization, HRV analysis, sensor validation.

---

### Workflow 4: Continuous Monitoring (Repeated Streams)
**Goal:** Collect data in batches without stopping PPG.

```bash
> adpd ppg start
[SUCCESS] PPG running at 100 Hz...

# Batch 1
> adpd ppg stream 500
Stream done...

# Batch 2
> adpd ppg stream 500
Stream done...

# Stop when done
> adpd ppg stop
[OK] PPG stopped.
```

---

## Data Analysis

### Signal Interpretation

**4-Channel Configuration:**
- **Ch1 (Ambient)**: Constant background light → should be ~0 or very steady
- **Ch2 (Ambient)**: Same as Ch1 for redundancy → should match Ch1
- **Ch3 (Signal)**: PPG signal from IN3 input → **pulsatile, oscillating 1–100 kHz**
- **Ch4 (Signal)**: PPG signal from IN3 input → **matched pair with Ch3**

**Good signal** (Ch3/Ch4):
```
Sample | Ch3     | Ch4
-------|---------|----------
0      | 250000  | 350000
1      | 255000  | 355500   ← Rising (systole)
2      | 265000  | 365000
3      | 260000  | 360000   ← Falling (diastole)
4      | 240000  | 340000
```

**Bad signal** (all channels near zero):
- LED not powered → Check `adpd diag`, verify 0x128 (LED_POW12) = 0x000A
- No sensor connected → Check physical connections
- SPI communication issue → Run `adpd probe`, check for chip ID 0x01C6

---

### Python Analysis Template

```python
import struct
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

# Load binary stream
with open('ppg_data.bin', 'rb') as f:
    data = f.read()

# Parse 20-byte samples
samples = []
for i in range(len(data) // 20):
    ts, ch1, ch2, ch3, ch4 = struct.unpack('<IIIII', data[i*20:i*20+20])
    samples.append((ts, ch1, ch2, ch3, ch4))

ts_arr = np.array([s[0] for s in samples])
ch3_arr = np.array([s[3] for s in samples])
ch4_arr = np.array([s[4] for s in samples])

# Bandpass filter (typical PPG is 0.5–5 Hz for heart rate)
sos = signal.butter(4, [0.5, 5], 'band', fs=100, output='sos')
ch3_filtered = signal.sosfilt(sos, ch3_arr)

# Detect peaks (heartbeats)
peaks, _ = signal.find_peaks(ch3_filtered, distance=50)  # Min 50 samples apart at 100 Hz
heart_rate = len(peaks) * 60 / (ts_arr[-1] / 1000)

print(f"Detected {len(peaks)} beats")
print(f"Estimated HR: {heart_rate:.1f} bpm")

# Plot
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6))
ax1.plot(ts_arr, ch3_arr, 'b-', label='Raw Ch3')
ax1.set_ylabel('Signal (raw ADC counts)')
ax1.legend()
ax2.plot(ts_arr, ch3_filtered, 'g-', label='Filtered Ch3')
ax2.plot(ts_arr[peaks], ch3_filtered[peaks], 'ro', label='Detected Peaks')
ax2.set_xlabel('Time (ms)')
ax2.set_ylabel('Signal (filtered)')
ax2.legend()
plt.tight_layout()
plt.show()
```

---

## Troubleshooting

### No Data (All Zeros)

**Symptom:** `adpd ppg stream` returns all zeros for Ch3/Ch4.

**Check 1: LED Power**
```bash
> adpd diag
...
Slot A - LED Control:
  0x128 (LED_POW12): 0x0000   ← Should be 0x000A or higher
```
**Fix:** LED current not set. This is done in `App_ADPD_StartPPG_SlotA()`. Verify register 0x128 = 0x000A.

**Check 2: Channel Enable**
```bash
> adpd diag
...
Slot A - Data:
  0x137 (DECIMATE_A/CHANNEL_EN): 0xC010
     -> CHANNEL_EN[15:14] = 0x3 (0x3=4ch, ...)
```
**Fix:** If CHANNEL_EN bits show 0x0, channels are disabled. Run `adpd ppg start` to re-initialize.

**Check 3: FIFO Status**
```bash
> adpd read 0x000
ADPD7000 Reg 0x0000: 0x0005   ← FIFO has 5 bytes (should have 16+ for a complete sample)
```
**Fix:** FIFO not accumulating data. Check if PPG is actually running (`adpd diag` should show OPMODE bit0=1).

---

### Inconsistent Data / Corrupted Samples

**Symptom:** Occasional samples are 0xFFFFFFFF or other garbage.

**Likely Cause:** SPI timing issue or FIFO underrun.

**Fix:**
1. Check SPI clock: Should be 5.25 MHz (APB2 84 MHz / prescaler 16). See main.c `MX_SPI1_Init()`.
2. Reduce streaming frequency: `adpd ppg freq 50` instead of 100 Hz.
3. Use USB instead of UART for higher bandwidth.

---

### Shell Unresponsive

**Symptom:** Commands don't execute or shell stops responding.

**Fix 1: Soft Reset**
```bash
> reset
Resetting MCU via NVIC...
```

**Fix 2: Restart Device**
Power-cycle the BlackPill (disconnect and reconnect USB).

---

### Chip ID Wrong (0x00E3 instead of 0x01C6)

**Symptom:** `adpd probe` shows wrong chip ID.

**Likely Cause:** SPI clock too fast (over-spec).

**Fix:** In main.c `MX_SPI1_Init()`, set prescaler to `/16` (5.25 MHz):
```c
hspi1.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_16;
```

---

## Advanced Usage

### Logging Calibration Data to EEPROM

```bash
# Read current sensor configuration
> adpd read 0x124
ADPD7000 Reg 0x0124: 0x9912   ← AFE_TRIM1_A

# Save to EEPROM (example: store at offset 0x0010)
> eeprom write 0x0010 0x99
> eeprom write 0x0011 0x12
SUCCESS

# Verify
> eeprom read 0x0010
M24C32 [0x51] @ 0x0010: 0x99
```

---

### Custom ODR Testing

```bash
# Capture at 10 different rates and compare data quality
for rate in 10 25 50 100 200; do
    echo "Testing at $rate Hz..."
    adpd ppg freq $rate
    adpd ppg stream 500 > data_${rate}hz.csv
done
```

Then analyze signal-to-noise ratio, harmonic content, etc. in Python.

---

### Diagnostic Logging

Enable full shell output logging (requires terminal with file capture):
```bash
# macOS/Linux
script session.log < /dev/ttyUSB0
# Type commands, then 'exit'

# Windows (PuTTY)
Logging → All session output → Enable
```

This captures all shell I/O for debugging or documentation.

---

## Next Steps

1. **Run your first stream:**
   ```bash
   > adpd probe
   > adpd ppg start
   > adpd ppg stream 100
   ```

2. **Save data to file** using BINARY_STREAMING.md instructions.

3. **Analyze in Python** using the template above.

4. **Experiment with ODRs** to find the best trade-off for your use case.

5. **Check CLAUDE.md** for architecture details if you want to modify firmware.

---

## Support

- **Hardware issues** → Check README.md SPI Mode 3 and clock settings
- **Data quality** → See [BINARY_STREAMING.md](BINARY_STREAMING.md) for protocol details
- **Firmware changes** → See [CLAUDE.md](CLAUDE.md) for architecture and development patterns
