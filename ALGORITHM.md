# PPG Processing — Algorithm Reference for C Porting

This document describes every signal processing step with explicit formulas, data types,
and pseudocode. It is intended as a porting guide for a C implementation.

## Module Map

| Python module | C mapping | Contents |
|---|---|---|
| `ppg_processing.py` | **Port this** | All signal algorithms — pure numpy, no UI |
| `ppg_charts.py` | Leave in Python | Plotly chart builders, visualization only |
| `app.py` | Leave in Python | Streamlit UI, session state, cache wrappers |

The C port only needs to replicate `ppg_processing.py`. Key entry points:

```c
// Corresponds to ppg_processing.apply_signal_transform()
void ppg_transform(double *signal, int N, transform_mode_t mode, ...);

// Corresponds to ppg_processing.run_pipeline()
ppg_result_t ppg_run_pipeline(double *signal, int N, double SR,
                               clean_method_t clean, peak_method_t peaks,
                               quality_method_t quality);

// Corresponds to ppg_processing.extract_epochs()
void ppg_extract_epochs(double *signal, int N, int *peak_idx, int P,
                        double SR, double pre_s, double post_s, ...);
```

All floating-point values are **double (float64)** unless noted. Array indices are 0-based.

---

## 1. Data Ingestion & Timestamp Handling

### 1.1 Input
- `timestamp[]`  — uint64, milliseconds, monotonically non-decreasing
- `raw[]`        — float64, raw ADC counts (or voltage)
- `N`            — number of samples after deduplication

### 1.2 Deduplication
Keep the first sample for each unique timestamp:
```
filtered_N = 0
last_ts    = UINT64_MAX
for i in 0..N-1:
    if timestamp[i] != last_ts:
        ts_out[filtered_N]  = timestamp[i]
        raw_out[filtered_N] = raw[i]
        filtered_N++
        last_ts = timestamp[i]
N = filtered_N
```

### 1.3 Sample Rate Detection
```c
// Primary method
double duration_ms = (double)(ts_out[N-1] - ts_out[0]);
double SR = (double)N / (duration_ms / 1000.0);   // Hz

// Fallback if duration_ms == 0
// Compute median of consecutive diffs:
double diffs[N-1];
for i in 0..N-2: diffs[i] = ts_out[i+1] - ts_out[i];
sort(diffs, N-1);
double median_diff = diffs[(N-1)/2];
SR = 1000.0 / median_diff;
```

---

## 2. Signal Transform (pre-processing, before any filter)

Applied to `raw[]` → `signal[]`. Only one mode is active at a time.

### 2.1 None
```c
signal[i] = raw[i];   // identity
```

### 2.2 Hardware Inversion
Corrects ADC-inverted PPG waveforms (peaks appear as troughs).
```c
double max_val = pow(2.0, adc_bits);   // e.g. 16777216.0 for 24-bit
for i in 0..N-1:
    signal[i] = max_val - raw[i];
```
- `adc_bits`: integer, configurable (default 24)

### 2.3 AC Flip — Global Mean
Flips waveform polarity around the DC mean; DC offset is preserved.
```c
double mean = 0.0;
for i in 0..N-1: mean += raw[i];
mean /= N;

for i in 0..N-1:
    signal[i] = 2.0 * mean - raw[i];
```

### 2.4 AC Flip — Sliding Window Mean
Flips waveform polarity around a local (time-varying) baseline.
Baseline is a centered rolling mean of width `W` samples.
```c
int W = (int)round(window_sec * SR);   // e.g. 2.0 s * SR
if (W < 1) W = 1;

double baseline[N];
for i in 0..N-1:
    int half = W / 2;
    int lo   = max(0,   i - half);
    int hi   = min(N-1, i + half);
    double sum = 0.0;
    for j in lo..hi: sum += raw[j];
    baseline[i] = sum / (double)(hi - lo + 1);

for i in 0..N-1:
    signal[i] = 2.0 * baseline[i] - raw[i];
```
- `window_sec`: float64, configurable (default 2.0 s)
- Edge handling: window shrinks at signal boundaries (min_periods=1)

---

## 3. Signal Cleaning (Band-pass Filter)

Implemented by `nk.ppg_clean()`. The default and most common method is **Elgendi**.

### 3.1 Elgendi Method (default)
Zero-phase Butterworth bandpass filter, 3rd order, 0.5–8.0 Hz.

```
Passband:  0.5 Hz  to  8.0 Hz
Order:     3  (applied twice via filtfilt → effective order 6, zero phase)
Type:      Butterworth
```

**Implementation in C:**

1. Design a 3rd-order Butterworth bandpass between `f_low=0.5` and `f_high=8.0` at sample rate `SR`.
2. Apply using `filtfilt` (forward + reverse pass) to achieve zero phase shift.

```c
// Normalised cutoffs
double Wn_low  = 2.0 * f_low  / SR;
double Wn_high = 2.0 * f_high / SR;

// Design 3rd-order Butterworth bandpass → gives 6 biquad coefficients (b, a)
// Use scipy.signal.butter(3, [Wn_low, Wn_high], btype='bandpass') equivalent
// Then apply: cleaned[] = filtfilt(b, a, signal[])
```

A bandpass of order N can be factored into biquad sections (SOS form) for numerical stability.
Use a standard IIR biquad cascade with forward + reverse pass for zero-phase output.

### 3.2 Other Methods
All other cleaning methods (`nabian2018`, `pantompkins`, `hamilton`, etc.) are also
bandpass filters with slightly different cutoffs or orders. For C porting, `elgendi` is
the primary reference implementation. If other methods are needed, refer to the
[NeuroKit2 source](https://github.com/neuropsychology/NeuroKit/blob/master/neurokit2/ppg/ppg_clean.py).

---

## 4. Peak Detection

Implemented by `nk.ppg_peaks()`. The default method is **Elgendi**.

### 4.1 Elgendi Peak Detection
Reference: Elgendi et al. (2013), "Systolic Peak Detection in Acceleration Photoplethysmograms"

**Inputs:** `cleaned[]`, `SR`

**Step 1 — Square the signal**
```c
for i in 0..N-1: y[i] = cleaned[i] * cleaned[i];
```

**Step 2 — Two moving-average filters**
```c
// W1: beat window (~111 ms)
int W1 = (int)ceil(0.111 * SR);
// W2: beat window (~667 ms)
int W2 = (int)ceil(0.667 * SR);

double ma_beat[N], ma_beat2[N];
moving_average(y, N, W1, ma_beat);   // causal MA, window W1
moving_average(y, N, W2, ma_beat2);  // causal MA, window W2
```

**Step 3 — Thresholding**
```c
double alpha = 0.02 * mean(y);   // offset threshold

double thr1[N];
for i in 0..N-1:
    thr1[i] = ma_beat2[i] + alpha;
```

**Step 4 — Region of Interest (ROI) detection**
Segments where `ma_beat[i] > thr1[i]` form ROIs (systolic wave candidates).

**Step 5 — Peak within ROI**
For each contiguous ROI segment, find the sample index with the maximum value of `cleaned[]`.
That index is a detected peak.

**Output:** `peak_indices[]` — array of integer sample indices.

---

## 5. Heart Rate

```c
// RR intervals in milliseconds
double rr_ms[P-1];   // P = number of peaks
for i in 0..P-2:
    rr_ms[i] = (double)(peak_indices[i+1] - peak_indices[i]) / SR * 1000.0;

// Instantaneous HR at each RR interval
double hr[P-1];
for i in 0..P-2:
    hr[i] = 60000.0 / rr_ms[i];   // bpm
```

The instantaneous HR is then interpolated to signal length (linear or cubic spline).

---

## 6. Beat Segmentation (Individual Beat Extraction)

For each peak at index `p_k`, extract a window:
```c
int pre_samples  = (int)round(pre_sec  * SR);   // default ~35% of median RR
int post_samples = (int)round(post_sec * SR);   // default ~55% of median RR

for each peak p_k:
    int lo = p_k - pre_samples;
    int hi = p_k + post_samples;
    if (lo < 0 || hi >= N) skip;   // truncated beat — excluded from average
    beat_k[j] = cleaned[lo + j],  j = 0..(pre_samples + post_samples)
    // t-axis: (j - pre_samples) / SR   seconds, 0 at peak
```

**Average beat (NaN-safe):**
Only average over a given time point if ≥50% of beats have real data at that point.
Truncated beats (near start/end of recording) are excluded rather than padded.

---

## 7. HRV Metrics (Interval-Related)

Computed from RR intervals `rr_ms[]` of length `P-1`:

```c
// Mean RR
double MeanNN = mean(rr_ms);

// SDNN — standard deviation of RR
double SDNN = std(rr_ms);

// RMSSD — root mean square of successive differences
double sum_sq = 0.0;
for i in 0..P-3: sum_sq += pow(rr_ms[i+1] - rr_ms[i], 2);
double RMSSD = sqrt(sum_sq / (P - 2));

// Mean HR
double mean_HR = mean(hr);   // bpm, from step 5
```

---

## 8. Signal Quality Indices

All methods operate on `cleaned[]` at sample rate `SR`.

### 8.1 Template Match (default)
Pearson correlation of each beat waveform against the average beat waveform.

```
quality[k] = pearson_r(beat_k[], avg_beat[])    ∈ [0, 1]
```
1 = perfect match, 0 = no correlation.
Result is interpolated back to signal length.

**Threshold:** 0.5 = acceptable, 0.8 = good.

### 8.2 Dissimilarity
Mean absolute deviation between each beat and the average beat, normalised.
```
quality[k] = MAD(beat_k[], avg_beat[])    ∈ [0, ∞), 0 = best
```
No standard threshold.

### 8.3 ho2025 (ICI method)
Binary per-beat quality using two independent peak detectors (Charlton + Elgendi).
If both detectors agree → 1 (high quality), else → 0.
```
quality[k] ∈ {0, 1}
```
Threshold: 0.5 (boundary between 0 and 1).

### 8.4 Skewness (windowed)
Skewness of signal amplitude within sliding windows of `W_sec` seconds.
```c
// Window: W_sec=3s, overlap: 2s (auto-shrunk if signal < 3s)
double skewness = E[(x - mean)^3] / std^3
```
Higher (more positive) = better quality PPG.
**Threshold:** 0 (Elgendi 2016 — positive skewness indicates clean systolic waveform).

### 8.5 Kurtosis (windowed)
```c
double kurtosis = E[(x - mean)^4] / std^4
```
Higher = better quality (heavier tails = sharper peaks). No universal threshold.

### 8.6 Entropy (windowed, histogram-based)
Shannon entropy of signal amplitude histogram with 16 bins:
```c
// Histogram: p[j] = fraction of samples in bin j (16 bins)
double entropy = -sum(p[j] * log2(p[j]))   for j where p[j] > 0
```
Lower = more regular signal = better quality. No universal threshold.

### 8.7 Perfusion Index (windowed)
Ratio of AC amplitude to DC baseline:
```c
// Per window:
double AC = max(cleaned_window) - min(cleaned_window);
double DC = abs(mean(raw_window));
double perfusion = (AC / DC) * 100.0;   // percent
```
Requires both `cleaned[]` and `raw[]`.
**Threshold:** 0.3% = acceptable, 1.0% = good (clinical PI thresholds).

### 8.8 Relative Power (windowed)
Ratio of spectral power in cardiac band (1.0–2.25 Hz) to total power (0–8 Hz):
```c
// Per window (default 60 s, overlap 30 s):
double P_cardiac = integral(PSD, 1.0, 2.25);   // Hz
double P_total   = integral(PSD, 0.0, 8.0);
double rel_power = P_cardiac / P_total;    ∈ [0, 1]
```
**Threshold:** 0.5 = acceptable, 0.8 = good.

---

## 9. Data Flow Summary

```
raw[] + timestamp[]
    │
    ├─ 1. Deduplication + SR detection
    │
    ├─ 2. Signal Transform (None / Invert / Flip AC)
    │         signal[] ← transformed raw[]
    │
    ├─ 3. ppg_clean()  →  cleaned[]
    │         Butterworth bandpass 0.5–8 Hz, zero-phase
    │
    ├─ 4. ppg_peaks()  →  peak_indices[]
    │         Elgendi dual-threshold on squared signal
    │
    ├─ 5. HR           →  hr[]
    │         60000 / rr_ms[], interpolated to N
    │
    ├─ 6. Beat segmentation  →  beats[K][W]
    │         K beats × W samples window around each peak
    │
    ├─ 7. HRV          →  MeanNN, SDNN, RMSSD
    │
    └─ 8. ppg_quality()  →  quality[]
              per-sample or per-beat index ∈ method-specific range
```

---

## 10. Key Numerical Parameters

| Parameter | Default | Description |
|---|---|---|
| `SR` | auto-detected | Sample rate (Hz) |
| `adc_bits` | 24 | ADC bit depth for hardware inversion |
| `flip_window_sec` | 2.0 s | Rolling window for AC flip baseline |
| `bandpass_low` | 0.5 Hz | Elgendi cleaning lower cutoff |
| `bandpass_high` | 8.0 Hz | Elgendi cleaning upper cutoff |
| `filter_order` | 3 | Butterworth order (filtfilt → effective order 6) |
| `elgendi_W1` | ceil(0.111 × SR) | Peak detection beat window |
| `elgendi_W2` | ceil(0.667 × SR) | Peak detection beat window 2 |
| `elgendi_alpha` | 0.02 × mean(y²) | Peak detection threshold offset |
| `beat_pre_sec` | ~35% of median RR | Pre-peak window for beat extraction |
| `beat_post_sec` | ~55% of median RR | Post-peak window for beat extraction |
| `quality_window_sec` | 3 s (60 s for rel_power) | Windowed quality method window |
| `quality_overlap_sec` | 2 s (30 s for rel_power) | Windowed quality method overlap |
| `entropy_bins` | 16 | Histogram bins for entropy SQI |
