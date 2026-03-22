# PPG Signal Filter & Analysis

Streamlit app for filtering and analyzing PPG (Photoplethysmography) signals using [NeuroKit2](https://github.com/neuropsychology/NeuroKit). Built for R&D use with ppg_afe and similar ADC-based PPG hardware.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Requires **Python 3.10+** (neurokit2 ≥0.2.10 uses `float | None` union syntax from PEP 604).

---

## Data Format

Upload a CSV or XLSX file, or use one of the bundled demo files in `data/`.

**Expected columns:**
- `timestamp` — integer milliseconds (monotonically increasing)
- One or more numeric signal columns (e.g. `slotA`, `slotB`, `slotA-Channel1`)

Files with multiple signal columns (dual-channel) are supported. Columns with >80% NaN are automatically excluded from the channel selector. Duplicate timestamps are deduplicated (first value kept).

**Supported formats:** `.csv`, `.xlsx`, `.xls`

### Demo Files

| File | Rows | Channels | Notes |
|---|---|---|---|
| `trial_data_1.csv` | ~1,654 | slotA | Single channel |
| `trial_data_2.csv` | ~167k | slotA, slotB | Long dual-channel recording |
| `trial_data_3.csv` | ~4,646 | slotA | 3 duplicate timestamps; slotB all NaN (auto-excluded) |
| `trial_data_4.csv` | ~1,000 | slotA-Channel1 | Hyphenated column name |
| `trial_data_5.xlsx` | — | — | XLSX format |

---

## Sidebar Controls

| Control | Description |
|---|---|
| **Data Source** | Choose a bundled demo file or upload your own |
| **Channel** | Select which signal column to analyze |
| **Signal Transform** | 3-way radio: None / Invert / Flip AC (see below) |
| **Detected SR** | Sample rate auto-calculated from timestamps |
| **Override SR** | Toggle + number input to manually set sample rate |
| **Reset Timeline** | Restore full signal range after zooming |
| **NeuroKit2 native plot** | Show matplotlib `ppg_plot` output in an expander |
| **Export All CSV** | Download a single CSV with all processed columns for the current window |

---

## Signal Transforms

Applied before any NeuroKit2 processing. Three mutually exclusive options:

### None
Raw signal passed through unchanged.

### Invert — `2^x − raw`
Corrects hardware that outputs an inverted PPG waveform (peaks appear as troughs).

```
corrected[i] = 2^x − raw[i]
```

- **x** = ADC bit depth (default 24, matches a 24-bit ADC with range 0–16,777,216)
- Set x to match your ADC: 16-bit → x=16, 24-bit → x=24

### Flip AC — `2 × baseline(t) − raw`
Flips the AC (pulsatile) component of the waveform while preserving the DC baseline level. Useful when the waveform polarity is inverted but the DC offset should remain unchanged.

```
corrected[i] = 2 × baseline[i] − raw[i]
```

Two baseline estimation modes (toggle):

| Mode | Formula | Use when |
|---|---|---|
| **Sliding window** (default) | Centered rolling mean over N seconds | DC drifts slowly over time |
| **Global mean** | Single mean across the whole analysis window | DC is stable |

- **Sliding window size** (default 2 s): controls rolling window width. Smaller → baseline tracks drift closely; larger → flatter baseline.
- The estimated baseline is drawn as a dotted orange line on the Raw Signal chart.
- Both modes show the original (pre-flip) signal in grey behind the flipped signal.

---

## Processing Pipeline

### Sample Rate Detection

```
SR = n_unique_timestamps / (duration_ms / 1000)
```

Fallback (zero-duration edge case):
```
SR = 1000 / median(diff(timestamps))
```

### Signal Cleaning (`ppg_clean`)

Available methods:

| Method | Description |
|---|---|
| `elgendi` | Bandpass 0.5–8 Hz (Butterworth 3rd order) — default |
| `nabian2018` | Bandpass 0.5–8 Hz |
| `pantompkins` | Pan-Tompkins adapted for PPG |
| `hamilton` | Hamilton bandpass filter |
| `elgendi_old` | Legacy Elgendi filter |
| `langevin2021` | Langevin 2021 method |
| `goda2024` | Goda 2024 method |
| `none` | No filtering — raw signal passed through |

### Peak Detection (`ppg_peaks`)

Available methods:

| Method | Description |
|---|---|
| `elgendi` | Elgendi dual-threshold (default) |
| `bishop` | Bishop adaptive threshold |
| `ssf` | Slope Sum Function |
| `climbing` | Climbing algorithm |
| `derivative` | First derivative zero-crossing |
| `kalidas2017` | Kalidas 2017 |
| `nabian2018` | Nabian 2018 |
| `gamboa` | Gamboa method |
| `charlton` | Charlton method |
| `charlton2024` | Charlton 2024 |
| `none` | Skip peak detection |

### Heart Rate

Instantaneous HR computed from RR intervals:
```
HR[i] = 60000 / RR_interval_ms[i]        (bpm)
```

Interpolated to signal length using `nk.ppg_rate()`.

---

## Main Sections

### Time Window
Drag-select (box-select) on any chart to zoom — all charts and the NeuroKit2 pipeline re-run on the selected window only. Use **Reset Timeline** in the sidebar to return to full range.

### Raw Data
Raw (or transformed) signal chart + descriptive statistics table. If a transform is active, the original signal is shown in grey underneath. If Flip AC is active, the sliding baseline is shown as a dotted orange line.

Export: `raw_signal.csv` (timestamp + raw values).

### Processed Signal
Cleaned signal overlaid on raw (raw overlay toggleable, off by default). Cleaning method selector appears here.

Export: `processed_signal.csv` (timestamp + raw + cleaned + peak flag + HR).

### Peak Detection
Cleaned signal with detected peaks marked as red triangles. HR metrics: mean HR, min/max HR, peak count, recording duration. Optional NeuroKit2 matplotlib figure (`ppg_plot`). Peak detection method selector appears here.

Export: `peak_data.csv`.

### Individual Beats
Beat waveforms extracted around each detected peak, aligned to t=0, overlaid on a single chart. The red line is the average beat shape.

**Beat segmentation sliders** (auto-initialized from median RR interval):
- Pre-peak window: 35% of median RR, clamped 0.10–0.40 s
- Post-peak window: 55% of median RR, clamped 0.20–0.80 s

**Truncation handling:** beats near the end of a recording may have a shorter post-peak window. Values outside a beat's actual range are set to NaN; the average is only drawn where ≥50% of beats have real data, preventing the average from being pulled down by truncated tails.

### HRV / Analysis
`ppg_analyze()` results table (interval-related HRV metrics: MeanNN, SDNN, RMSSD, etc.). Falls back to basic RR-derived stats for short recordings.

Export: `hrv_analysis.csv`.

### Signal Quality
`ppg_quality()` index plotted over time. Select one or multiple methods using the segmented control — all selected methods are overlaid on a single chart in different colours.

**Quality method selector:** `st.segmented_control` with `selection_mode="multi"` — click to toggle methods on/off.

Quality methods and their reference lines:

| Method | Output Range | Orange line | Green line | Source |
|---|---|---|---|---|
| `templatematch` | 0–1 | 0.5 (acceptable) | 0.8 (good) | Correlation convention |
| `dissimilarity` | unbounded (0=best) | — | — | No standard threshold |
| `ho2025` | 0 or 1 (binary) | 0.5 (boundary) | — | Binary classifier |
| `skewness` | unbounded | 0 (threshold) | — | Elgendi 2016 |
| `kurtosis` | unbounded | — | — | No standard threshold |
| `entropy` | unbounded | — | — | No standard threshold |
| `perfusion` | 0–100% | 0.3% (acceptable) | 1.0% (good) | Clinical PI thresholds |
| `relative_power` | 0–1 | 0.5 (acceptable) | 0.8 (good) | Power ratio convention |

Metrics row shows mean + "% above good threshold" (or std dev for unbounded methods) per selected method.

Export: `signal_quality.csv` — includes a `quality_{method}` column for every selected method.

---

## C Porting Reference

See [`ALGORITHM.md`](./ALGORITHM.md) for a complete description of all signal processing algorithms with explicit formulas, data types, and step-by-step pseudocode suitable for a C implementation.
