# User Guide

This guide describes how to use the current firmware with UART control and USB binary streaming.

The firmware exposes:

- `USART2`
  Plain-text shell for commands and diagnostics
- `USB CDC`
  Framed binary telemetry output

## 1. Connect

Open `USART2` in a serial terminal:

```bash
screen /dev/tty.usbserialXXXX 115200
```

Use your USB-to-UART adapter's device path here. `USART2` is the text shell; USB CDC is reserved for binary streaming.

Typical startup banner:

```text
--- ADPD7000 Control Shell ---
Text commands: USART2
Binary samples: USB CDC stream
Type 'help' for the command tree.
>
```

## 2. Explore the Shell

The shell supports hierarchical help:

```text
help
help adpd
help adpd ppg
help eeprom
help rtos
```

If you type an invalid or incomplete command, the shell prints the nearest valid subtree instead of only showing a generic error.

## 3. Core Commands

### System

```text
reset
rtos stats
```

### Bus / Storage

```text
scan i2c
scan spi
eeprom info
eeprom read 0x20
eeprom write 0x20 0x5A
eeprom test
```

### ADPD

```text
adpd probe
adpd probe sdk
adpd read 0x10 0x1F
adpd read slota
adpd write 0x0128 0x000A
adpd reset
adpd gpio read
adpd gpio set 1 2 0x17
adpd calib clk
```

## 4. PPG Profiles

Runtime PPG configuration is exposed as three mutable profiles:

- `slota`
- `slotab`
- `slota2`

These live in RAM. Reset or power-cycle loses changes.

### Inspect Profiles

```text
adpd ppg list
adpd ppg slota show
adpd ppg slotab show
adpd ppg slota2 show
```

### Reset a Profile

```text
adpd ppg slota reset
```

### Edit Analog Settings

Supported per-slot fields:

- `led-current`
- `led-channel`
- `led-mode`
- `afe-path`
- `tia-gain`

Examples:

```text
adpd ppg slota set a led-current 20
adpd ppg slota set a led-channel a
adpd ppg slota set a led-mode high-snr
adpd ppg slota set a afe-path tia-buf-adc-1x
adpd ppg slota set a tia-gain ch1 100k
adpd ppg slotab set b tia-gain ch3 50k
```

## 5. Output Data Rate

Supported ODR values:

- `10`
- `25`
- `50`
- `100`
- `200`
- `400`

Set ODR from the UART shell:

```text
adpd ppg freq 100
```

The selected ODR is applied when PPG is started.

## 6. Start and Stop PPG

Examples:

```text
adpd ppg slota start
adpd ppg slotab start
adpd ppg slota2 start
adpd ppg stop
```

## 7. CSV Streaming

CSV streaming prints on `USART2`.

Examples:

```text
adpd ppg slota stream
adpd ppg slota stream 500
adpd ppg slotab stream 100
```

If HR is enabled, the CSV path includes `Peak` and `HR`.

HR requires explicit channel selection:

```text
adpd ppg slota stream 500 hr on sAch1
```

If HR is enabled without a selected channel, the shell rejects the command.

## 8. Binary Streaming

Binary streaming uses the USB CDC stream only:

```text
adpd ppg slota stream-bin 1000
adpd ppg slotab stream-bin 200
```

Important behavior:

- The command is entered on `USART2`
- Framed binary output appears on USB CDC
- Human-readable shell output stays on `USART2`
- No shell text is intentionally mixed into USB CDC

Open USB CDC with a binary-capable capture tool or a script, not a normal text terminal if you want to parse the payload.

See [BINARY_STREAMING.md](/Volumes/Power/projects/aulee/firmware/FreeRTOS_stm32f4/Reference_Projects/blackpill_eval_adpd7000_sdk_integration/BINARY_STREAMING.md) for the frame format.

## 9. Typical Workflow

Example: probe, tune, start, then capture binary samples

```text
adpd probe sdk
adpd ppg slota show
adpd ppg slota set a led-current 24
adpd ppg slota set a tia-gain ch1 100k
adpd ppg freq 100
adpd ppg slota stream-bin 1000
```

Then capture from USB CDC with your host-side script.

## 10. Troubleshooting

### `help` does nothing on USB

That is expected. `help` belongs on `USART2` only.

### I can type, but streaming parser fails

Make sure you are reading from USB CDC, not the UART shell.

### PPG start fails

Try:

```text
adpd probe
adpd probe sdk
adpd read slota
```

### GPIO debug

```text
adpd gpio read
adpd gpio set 1 2 0x16
```

## 11. Current Limitations

- Large ADPD report tables are still formatted with `printf` internally
- CSV stream headers remain text-oriented
- Calibration reports are still verbose diagnostic output rather than compact structured text
- Profile edits are runtime-only and not persisted
