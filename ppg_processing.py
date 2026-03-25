"""ppg_processing.py — Pure PPG signal processing (no UI, no plotting).

This module is the C-portable layer. Every function here operates only on
numpy arrays and plain Python scalars — no Streamlit, no Plotly.

C porting: see ALGORITHM.md for pseudocode and formula references.
Requires Python 3.10+  (neurokit2 ≥0.2.10 uses float | None, PEP 604).
"""

import numpy as np
import pandas as pd
import neurokit2 as nk

# ─────────────────────────────────────────────────────────────────────────────
# Method registries
# ─────────────────────────────────────────────────────────────────────────────

CLEAN_METHODS: list[str] = [
    "elgendi", "nabian2018", "pantompkins", "hamilton", "elgendi_old",
    "langevin2021", "goda2024", "none",
]

PEAK_METHODS: list[str] = [
    "elgendi", "bishop", "ssf", "climbing", "derivative",
    "kalidas2017", "nabian2018", "gamboa",
    "charlton", "charlton2024", "none",
]

QUALITY_METHODS: list[str] = [
    "templatematch", "dissimilarity", "skewness",
    "kurtosis", "entropy", "perfusion", "relative_power", "ho2025",
]

TIMESTAMP_COL: str = "timestamp"

# Per-method reference thresholds: list of (value, plot_color, label)
# Sources: Elgendi 2016 (skewness=0), clinical PI (0.3/1.0%), correlation convention (0.5/0.8)
# Used by both the chart builders and the metrics row in the UI.
QUALITY_REFS: dict[str, list[tuple]] = {
    "templatematch":  [(0.5, "orange", "0.5 acceptable"), (0.8, "limegreen", "0.8 good")],
    "relative_power": [(0.5, "orange", "0.5 acceptable"), (0.8, "limegreen", "0.8 good")],
    "ho2025":         [(0.5, "orange", "0/1 boundary")],
    "skewness":       [(0.0, "orange", "0 threshold")],
    "dissimilarity":  [],
    "kurtosis":       [],
    "entropy":        [],
    "perfusion":      [(0.3, "orange", "0.3% acceptable"), (1.0, "limegreen", "1.0% good")],
}

# ─────────────────────────────────────────────────────────────────────────────
# Sample Rate
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sample_rate(df: pd.DataFrame, timestamp_col: str = TIMESTAMP_COL) -> float:
    """Calculate sample rate (Hz) from millisecond timestamps.

    Primary:  SR = n_unique / (duration_ms / 1000)
    Fallback: SR = 1000 / median(diff(timestamps))  — used if duration_ms == 0

    C equivalent: ALGORITHM.md §1.3
    """
    ts = df[timestamp_col].dropna().values
    n_unique = len(np.unique(ts))
    duration_ms = ts.max() - ts.min()
    if duration_ms > 0:
        return n_unique / (duration_ms / 1000.0)
    diffs = np.diff(np.unique(ts))
    if len(diffs) > 0 and np.median(diffs) > 0:
        return 1000.0 / np.median(diffs)
    return 100.0  # last-resort default


# ─────────────────────────────────────────────────────────────────────────────
# Signal Preparation
# ─────────────────────────────────────────────────────────────────────────────

def prepare_signal(
    df: pd.DataFrame,
    signal_col: str,
    timestamp_col: str = TIMESTAMP_COL,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Return (timestamps_ms, signal_array, sample_rate) after deduplication.

    Steps:
      1. Sort by timestamp (ascending)
      2. Drop rows where signal_col is NaN
      3. Drop duplicate timestamps — keep first occurrence (handles hw clock jitter)
      4. Compute sample rate from deduplicated timestamps

    C equivalent: ALGORITHM.md §1.2
    """
    df = df.sort_values(timestamp_col)
    df = df.dropna(subset=[signal_col])
    df = df.drop_duplicates(subset=[timestamp_col], keep="first")
    sr = calculate_sample_rate(df, timestamp_col)
    timestamps = df[timestamp_col].values
    signal = df[signal_col].values.astype(float)
    return timestamps, signal, sr


# ─────────────────────────────────────────────────────────────────────────────
# Signal Transform
# ─────────────────────────────────────────────────────────────────────────────

def apply_signal_transform(
    signal: np.ndarray,
    mode: str,
    adc_bits: int = 24,
    flip_sliding: bool = True,
    flip_window_s: float = 2.0,
    sampling_rate: float = 100.0,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Apply pre-processing transform to raw signal before filtering.

    Args:
        signal:         float64 array, raw ADC values.
        mode:           "none" | "invert" | "flip_ac"
        adc_bits:       ADC bit depth for inversion (default 24).
        flip_sliding:   True = rolling mean baseline; False = global mean.
        flip_window_s:  Rolling window width in seconds (used when flip_sliding=True).
        sampling_rate:  Hz, needed to convert flip_window_s to samples.

    Returns:
        (transformed_signal, baseline)
        baseline is None unless mode=="flip_ac" (used for chart overlay).

    Formulas — C equivalent: ALGORITHM.md §2
      invert:   out[i] = 2^adc_bits − signal[i]
      flip_ac:  out[i] = 2 × baseline[i] − signal[i]
        sliding baseline: centered rolling mean, window = flip_window_s × SR samples
        global  baseline: mean(signal) repeated for all i
    """
    if mode == "invert":
        return float(2 ** adc_bits) - signal, None

    if mode == "flip_ac":
        if flip_sliding:
            win = max(1, int(round(flip_window_s * sampling_rate)))
            baseline = (
                pd.Series(signal)
                .rolling(window=win, center=True, min_periods=1)
                .mean()
                .to_numpy()
            )
        else:
            baseline = np.full(len(signal), np.mean(signal))
        return 2.0 * baseline - signal, baseline

    # "none"
    return signal.copy(), None


# ─────────────────────────────────────────────────────────────────────────────
# NeuroKit2 Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    signal: np.ndarray,
    sampling_rate: float,
    clean_method: str = "elgendi",
    peak_method: str = "elgendi",
    quality_method: str = "templatematch",
) -> dict:
    """Run the full NeuroKit2 PPG processing pipeline.

    This is the primary C-portable processing entry point.
    All arguments are plain scalars or numpy arrays — no Streamlit dependencies.

    Pipeline steps (C equivalent in ALGORITHM.md):
      1. ppg_clean()    — Butterworth bandpass 0.5–8 Hz, zero-phase (filtfilt). §3
      2. ppg_peaks()    — Elgendi dual-threshold peak detection on squared signal. §4
      3. ppg_rate()     — Instantaneous HR from RR intervals, interpolated. §5
      4. ppg_quality()  — SQI; windowed methods auto-shrink window to fit signal. §8
      5. ppg_analyze()  — HRV metrics (MeanNN, SDNN, RMSSD, …). §7
                          Falls back to manual RR stats for short recordings.

    Args:
        signal:          float64 array, pre-processed (transformed) signal.
        sampling_rate:   Hz, float64.
        clean_method:    One of CLEAN_METHODS; "none" skips filtering.
        peak_method:     One of PEAK_METHODS; "none" skips peak detection.
        quality_method:  One of QUALITY_METHODS.

    Returns dict:
        cleaned       np.ndarray[float64]  — bandpass-filtered signal
        signals_df    pd.DataFrame         — PPG_Clean, PPG_Peaks, PPG_Rate columns
        info          dict                 — {"PPG_Peaks": np.ndarray[int]}
        quality       np.ndarray | None    — SQI values, same length as cleaned
        quality_error str | None           — error message if SQI failed
        analysis      pd.DataFrame | None  — HRV metrics table
    """
    # ── Step 1: Clean ────────────────────────────────────────────────────────
    cleaned = nk.ppg_clean(signal, sampling_rate=sampling_rate, method=clean_method)

    # ── Step 2: Peaks ────────────────────────────────────────────────────────
    if peak_method == "none":
        signals_df = pd.DataFrame({"PPG_Clean": cleaned, "PPG_Peaks": 0})
        info = {"PPG_Peaks": np.array([], dtype=int)}
    else:
        signals_df, info = nk.ppg_peaks(cleaned, sampling_rate=sampling_rate, method=peak_method)
        # ppg_analyze requires PPG_Rate; ppg_peaks doesn't add it automatically
        signals_df["PPG_Rate"] = nk.ppg_rate(
            signals_df, sampling_rate=sampling_rate, desired_length=len(signals_df)
        )

    # ── Step 3: Quality ──────────────────────────────────────────────────────
    quality = None
    quality_error = None
    _peaks = info.get("PPG_Peaks", np.array([], dtype=int))
    _needs_raw = quality_method in ("perfusion", "relative_power")
    _signal_duration_s = len(cleaned) / sampling_rate

    # Windowed methods need window_sec ≤ signal duration; auto-shrink to fit
    _default_win  = {"skewness": 3, "kurtosis": 3, "entropy": 3, "perfusion": 3, "relative_power": 60}
    _default_ovlp = {"skewness": 2, "kurtosis": 2, "entropy": 2, "perfusion": 2, "relative_power": 30}
    if quality_method in _default_win:
        _win  = min(_default_win[quality_method],  max(1, _signal_duration_s * 0.9))
        _ovlp = min(_default_ovlp[quality_method], _win * 0.5)
    else:
        _win = _ovlp = None

    try:
        quality = nk.ppg_quality(
            cleaned,
            peaks=_peaks if len(_peaks) > 0 else None,
            sampling_rate=sampling_rate,
            method=quality_method,
            window_sec=_win,
            overlap_sec=_ovlp,
            ppg_raw=signal if _needs_raw else None,
        )
    except TypeError:
        # Older NeuroKit2 (<0.2.10) — try without kwargs
        try:
            quality = nk.ppg_quality(cleaned, sampling_rate=sampling_rate)
        except Exception as e:
            quality_error = str(e)
    except Exception as e:
        quality_error = str(e)

    # ── Step 4: HRV Analysis ─────────────────────────────────────────────────
    analysis = None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="DFA_alpha2", category=RuntimeWarning)
            warnings.filterwarnings("ignore", message="DFA_alpha2")
            analysis = nk.ppg_analyze(signals_df, sampling_rate=sampling_rate,
                                       method="interval-related")
    except Exception:
        # Full HRV analysis needs long recordings; compute basic RR stats instead
        peak_idx = np.where(signals_df["PPG_Peaks"].values == 1)[0]
        if len(peak_idx) >= 2:
            rr_ms = np.diff(peak_idx) / sampling_rate * 1000
            hr = 60000 / rr_ms
            analysis = pd.DataFrame({
                "PPG_Rate_Mean": [float(np.mean(hr))],
                "PPG_Rate_SD":   [float(np.std(hr))],
                "HRV_MeanNN":    [float(np.mean(rr_ms))],
                "HRV_SDNN":      [float(np.std(rr_ms))],
                "HRV_RMSSD":     [float(np.sqrt(np.mean(np.diff(rr_ms) ** 2)))],
                "N_Peaks":       [int(len(peak_idx))],
            })

    return {
        "cleaned":       cleaned,
        "signals_df":    signals_df,
        "info":          info,
        "quality":       quality,
        "quality_error": quality_error,
        "analysis":      analysis,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Beat Segmentation
# ─────────────────────────────────────────────────────────────────────────────

def extract_epochs(
    signal: np.ndarray,
    peak_indices: np.ndarray,
    sampling_rate: float,
    epochs_start: float,
    epochs_end: float,
) -> dict:
    """Extract fixed-length windows around each peak.

    For each peak index p_k the extracted window spans:
        t ∈ [epochs_start, epochs_end]  seconds relative to peak (t=0)
        samples: p_k + int(epochs_start * SR) .. p_k + int(epochs_end * SR)

    epochs_start is negative (pre-peak), epochs_end is positive (post-peak).
    Peaks too close to signal boundaries are dropped automatically.

    C equivalent: ALGORITHM.md §6
    """
    return nk.epochs_create(
        signal, events=peak_indices, sampling_rate=sampling_rate,
        epochs_start=epochs_start, epochs_end=epochs_end,
    )


def auto_beat_windows(peak_indices: np.ndarray, sampling_rate: float) -> tuple[float, float]:
    """Derive pre/post beat windows from median RR interval.

    pre  = 35% of median RR, clamped to [0.10, 0.40] s, rounded to 0.05 s
    post = 55% of median RR, clamped to [0.20, 0.80] s, rounded to 0.05 s

    C equivalent: ALGORITHM.md §6
    """
    if len(peak_indices) < 2:
        return 0.2, 0.5
    rr_s = float(np.median(np.diff(peak_indices)) / sampling_rate)
    pre  = round(round(min(max(rr_s * 0.35, 0.10), 0.40) / 0.05) * 0.05, 2)
    post = round(round(min(max(rr_s * 0.55, 0.20), 0.80) / 0.05) * 0.05, 2)
    return pre, post


# ─────────────────────────────────────────────────────────────────────────────
# HR Metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_hr_metrics(
    peak_indices: np.ndarray,
    sampling_rate: float,
) -> tuple[float | None, float | None, float | None]:
    """Return (mean_HR, min_HR, max_HR) in bpm from peak sample indices.

    HR[i] = 60000 / RR_ms[i]    where RR_ms[i] = diff(peak_indices)[i] / SR * 1000

    C equivalent: ALGORITHM.md §5
    """
    if len(peak_indices) < 2:
        return None, None, None
    rr_ms = np.diff(peak_indices) / sampling_rate * 1000
    hr_inst = 60000 / rr_ms
    return float(np.mean(hr_inst)), float(np.min(hr_inst)), float(np.max(hr_inst))
