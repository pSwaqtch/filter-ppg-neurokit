# User Guide

This guide describes how to use the current firmware as it exists on the dual-CDC branch.

The firmware exposes two USB serial ports:

- `ADPD7000 Control Port`
  Plain-text shell for commands and diagnostics
- `ADPD7000 Stream Port`
  Framed binary telemetry output

Do not expect text commands like `help` to work on the stream port.

## 1. Connect

Open the control port in a serial terminal:

```bash
screen /dev/tty.usbmodemXXXX 115200
```

Typical startup banner:

```text
--- ADPD7000 Control Shell ---
Text commands: this port
Binary samples: ADPD7000 Stream port
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

Set ODR from the control shell:

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

CSV streaming prints on the control port.

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

Binary streaming uses the stream port only:

```text
adpd ppg slota stream-bin 1000
adpd ppg slotab stream-bin 200
```

Important behavior:

- The command is entered on the control port
- Framed binary output appears on the stream port
- Human-readable shell output stays on the control port
- No shell text is intentionally mixed into the stream port

Open the stream port with a binary-capable capture tool or a script, not a normal text terminal if you want to parse the payload.

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

Then capture from the stream port with your host-side script.

## 10. Troubleshooting

### I see two USB ports. Which one is which?

Use the interface strings:

- control: `ADPD7000 Control Port`
- stream: `ADPD7000 Stream Port`

### `help` does nothing on one port

That is expected on the stream port. `help` belongs on the control port only.

### I can type, but streaming parser fails

Make sure you are reading from the stream port, not the control port.

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
