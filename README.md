# PPG Signal Filter & Analysis

Streamlit app for filtering and analyzing PPG (Photoplethysmography) signals using [NeuroKit2](https://github.com/neuropsychology/NeuroKit). Built for R&D use with ppg_afe and similar ADC-based PPG hardware.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Requires **Python 3.10+** (neurokit2 ≥0.2.10 uses `float | None` union syntax from PEP 604).

## Data Format

Upload a CSV or XLSX file, or use one of the bundled demo files in `data/`.

**Expected columns:**
- `timestamp` — integer milliseconds (monotonically increasing)
- One or more numeric signal columns (e.g. `slotA`, `slotB`, `slotA-Channel1`)

Files with multiple signal columns (dual-channel) are supported. Columns with >80% NaN are automatically excluded from the channel selector. Duplicate timestamps are deduplicated (first value kept).

**Supported formats:** `.csv`, `.xlsx`, `.xls`

## Sidebar Controls

| Control | Description |
|---|---|
| **Source** | Choose a bundled demo file or upload your own |
| **Channel** | Select which signal column to analyze |
| **Invert Signal** | Flip signal using `2^x − raw` — for hardware that outputs an inverted PPG |
| **ADC bits (x)** | Bit depth for inversion formula (default 24, shown only when Invert is on) |
| **Detected SR** | Sample rate auto-calculated from timestamps |
| **Override SR** | Manually set sample rate if auto-detection is wrong |
| **Reset Timeline** | Restore full signal range after zooming |
| **Cleaning Method** | NeuroKit2 `ppg_clean` method: `elgendi`, `nabian2018`, `pantompkins`, `hamilton`, `elgendi_old` |
| **Peak Detection Method** | NeuroKit2 `ppg_peaks` method, or `none` to skip |
| **NeuroKit2 native plot** | Show matplotlib `ppg_plot` output in an expander |
| **Export All CSV** | Download a single CSV with all processed columns for the current window |

## Main Sections

### Time Window
Drag-select on any chart to zoom in — all charts sync to the same window. The NeuroKit2 pipeline re-runs on the selected window only. Use **Reset Timeline** in the sidebar to return to full range.

### Raw Signal
Raw signal chart + descriptive statistics. Export: `raw_signal.csv` (timestamp + raw values).

### Processed Signal
Cleaned signal overlaid on the raw (raw overlay toggleable, off by default). Export: `processed_signal.csv` (timestamp + raw + cleaned + peak flag + HR).

### Peak Detection
Cleaned signal with detected peaks marked. HR metrics: mean HR, min/max HR, peak count, recording duration. Optional NeuroKit2 matplotlib figure. Export: `peak_data.csv`.

### Individual Beats
Overlaid beat waveforms aligned to each detected peak, with the mean beat shape highlighted. Pre/post window sizes are auto-set from the median RR interval (35% / 55% of RR, clamped to 0.10–0.40 s and 0.20–0.80 s).

### HRV / Analysis
`ppg_analyze()` results table (interval-related HRV metrics). Falls back to basic RR-derived stats (mean HR, SDNN, RMSSD, etc.) for short recordings. Export: `hrv_analysis.csv`.

### Signal Quality
`ppg_quality()` index (0–1) plotted over time, with reference lines at 0.5 (orange) and 0.8 (green). Export: `signal_quality.csv` (timestamp + raw + cleaned + quality index).

## Signal Inversion

Some hardware configurations (e.g. ppg_afe in certain optical setups) output a signal where peaks appear as troughs. Enable **Invert Signal** and set the ADC bit depth to correct this before any processing:

```
corrected = 2^x − raw
```

The default `x = 24` matches a 24-bit ADC (range 0 – 16,777,216).

## Demo Files

| File | Rows | Channels | Notes |
|---|---|---|---|
| `trial_data_1.csv` | ~1,654 | slotA | Single channel |
| `trial_data_2.csv` | ~167k | slotA, slotB | Long dual-channel recording |
| `trial_data_3.csv` | ~4,646 | slotA | 3 duplicate timestamps; slotB all NaN (auto-excluded) |
| `trial_data_4.csv` | ~1,000 | slotA-Channel1 | Hyphenated column name |
| `trial_data_5.xlsx` | — | — | XLSX format |
