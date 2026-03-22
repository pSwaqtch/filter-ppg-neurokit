import matplotlib
matplotlib.use("Agg")

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import matplotlib.pyplot as plt
import neurokit2 as nk
import io
import os

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DEMO_FILES = [
    "trial_data_1.csv",
    "trial_data_2.csv",
    "trial_data_3.csv",
    "trial_data_4.csv",
    "trial_data_5.xlsx",
]

CLEAN_METHODS = [
    "elgendi", "nabian2018", "pantompkins", "hamilton", "elgendi_old",
    "langevin2021", "goda2024", "none",
]
PEAK_METHODS = [
    "elgendi", "bishop", "ssf", "climbing", "derivative",
    "kalidas2017", "nabian2018", "gamboa",
    "charlton", "charlton2024", "none",
]
QUALITY_METHODS = [
    "templatematch", "dissimilarity", "skewness",
    "kurtosis", "entropy", "perfusion", "relative_power", "ho2025",
]

TIMESTAMP_COL = "timestamp"

# ─────────────────────────────────────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Loading file…")
def load_data(source, file_ext: str) -> pd.DataFrame:
    """Load CSV or XLSX from file path or file-like object."""
    try:
        if file_ext in (".xlsx", ".xls"):
            df = pd.read_excel(source, sheet_name=0, engine="openpyxl")
        else:
            df = pd.read_csv(source)
    except Exception as e:
        st.error(f"Failed to load file: {e}")
        st.stop()
    return df


def get_signal_columns(df: pd.DataFrame) -> list:
    """Return numeric non-timestamp columns with <80% NaN."""
    cols = []
    for col in df.columns:
        if col.lower() == TIMESTAMP_COL:
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        nan_frac = df[col].isna().mean()
        if nan_frac < 0.8:
            cols.append(col)
    return cols


def find_timestamp_col(df: pd.DataFrame) -> str:
    """Find timestamp column: prefer 'timestamp', else first monotone int col."""
    if TIMESTAMP_COL in df.columns:
        return TIMESTAMP_COL
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]):
            vals = df[col].dropna().values
            if len(vals) > 1 and np.all(np.diff(vals) >= 0):
                return col
    return df.columns[0]


# ─────────────────────────────────────────────────────────────────────────────
# Sample Rate
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sample_rate(df: pd.DataFrame, timestamp_col: str = TIMESTAMP_COL) -> float:
    """Calculate sample rate from deduplicated timestamps (ms)."""
    ts = df[timestamp_col].dropna().values
    n_unique = len(np.unique(ts))
    duration_ms = ts.max() - ts.min()
    if duration_ms > 0:
        return n_unique / (duration_ms / 1000.0)
    # Fallback: use median inter-sample interval
    diffs = np.diff(np.unique(ts))
    if len(diffs) > 0 and np.median(diffs) > 0:
        return 1000.0 / np.median(diffs)
    return 100.0  # last-resort default


# ─────────────────────────────────────────────────────────────────────────────
# Signal Preparation
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def prepare_signal(df: pd.DataFrame, signal_col: str, timestamp_col: str = TIMESTAMP_COL):
    """Return (timestamps_ms, signal_array, sample_rate) after cleaning."""
    df = df.sort_values(timestamp_col)
    df = df.dropna(subset=[signal_col])
    df = df.drop_duplicates(subset=[timestamp_col], keep="first")
    sr = calculate_sample_rate(df, timestamp_col)
    timestamps = df[timestamp_col].values
    signal = df[signal_col].values.astype(float)
    return timestamps, signal, sr


# ─────────────────────────────────────────────────────────────────────────────
# NeuroKit2 Pipeline (cached)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Running NeuroKit2 pipeline…")
def cached_pipeline(signal_bytes: bytes, sampling_rate: float, clean_method: str, peak_method: str, quality_method: str = "templatematch") -> dict:
    signal = np.frombuffer(signal_bytes, dtype=np.float64)

    cleaned = nk.ppg_clean(signal, sampling_rate=sampling_rate, method=clean_method)

    if peak_method == "none":
        signals_df = pd.DataFrame({"PPG_Clean": cleaned, "PPG_Peaks": 0})
        info = {"PPG_Peaks": np.array([], dtype=int)}
    else:
        signals_df, info = nk.ppg_peaks(cleaned, sampling_rate=sampling_rate, method=peak_method)
        # ppg_analyze requires PPG_Rate; ppg_peaks doesn't add it automatically
        signals_df["PPG_Rate"] = nk.ppg_rate(
            signals_df, sampling_rate=sampling_rate, desired_length=len(signals_df)
        )

    quality = None
    quality_error = None
    _peaks = info.get("PPG_Peaks", np.array([], dtype=int))
    _needs_raw = quality_method in ("perfusion", "relative_power")
    _signal_duration_s = len(cleaned) / sampling_rate

    # Windowed methods need window_sec <= signal duration; auto-shrink to fit
    _windowed = quality_method in ("skewness", "kurtosis", "entropy", "perfusion")
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

    analysis = None
    try:
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
        "cleaned": cleaned,
        "signals_df": signals_df,
        "info": info,
        "quality": quality,
        "quality_error": quality_error,
        "analysis": analysis,
    }


@st.cache_data(show_spinner=False)
def cached_epochs(signal_bytes: bytes, peak_indices_bytes: bytes, sampling_rate: float,
                  epochs_start: float, epochs_end: float) -> dict:
    signal = np.frombuffer(signal_bytes, dtype=np.float64)
    peak_indices = np.frombuffer(peak_indices_bytes, dtype=np.int64)
    return nk.epochs_create(signal, events=peak_indices, sampling_rate=sampling_rate,
                             epochs_start=epochs_start, epochs_end=epochs_end)


def downsample(timestamps, signal, max_pts: int = 5_000):
    """Stride-based downsample for display only — preserves shape."""
    n = len(timestamps)
    if n <= max_pts:
        return timestamps, signal
    step = max(1, n // max_pts)
    return timestamps[::step], signal[::step]


# ─────────────────────────────────────────────────────────────────────────────
# Plotly Chart Builders
# ─────────────────────────────────────────────────────────────────────────────

DARK = "plotly_dark"


def plot_signal_overview(timestamps_ms, signal, cleaned, peak_indices, quality, signal_col) -> go.Figure:
    """Combined subplot: raw / cleaned overlay / peaks / quality — shared X axis."""
    has_q = quality is not None
    n_rows = 4 if has_q else 3
    row_heights = [0.28, 0.28, 0.28, 0.16] if has_q else [0.34, 0.33, 0.33]
    subplot_titles = ["Raw Signal", "Processed Signal", "Peak Detection"]
    if has_q:
        subplot_titles.append("Signal Quality")

    fig = make_subplots(
        rows=n_rows, cols=1,
        shared_xaxes=True,
        subplot_titles=subplot_titles,
        row_heights=row_heights,
        vertical_spacing=0.05,
    )

    ts, sig_d = downsample(timestamps_ms, signal)
    _, cln_d  = downsample(timestamps_ms, cleaned)

    # Row 1 — Raw
    fig.add_trace(go.Scatter(x=ts, y=sig_d, mode="lines",
        line=dict(color="#636EFA", width=1), name=signal_col), row=1, col=1)

    # Row 2 — Cleaned overlay
    fig.add_trace(go.Scatter(x=ts, y=sig_d, mode="lines",
        line=dict(color="rgba(160,160,160,0.35)", width=1),
        name="Raw", legendgroup="overlay"), row=2, col=1)
    fig.add_trace(go.Scatter(x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned", legendgroup="overlay"), row=2, col=1)

    # Row 3 — Peaks
    fig.add_trace(go.Scatter(x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Signal", showlegend=False), row=3, col=1)
    if len(peak_indices) > 0:
        fig.add_trace(go.Scatter(
            x=timestamps_ms[peak_indices], y=cleaned[peak_indices], mode="markers",
            marker=dict(color="#EF553B", size=7, symbol="triangle-up"),
            name="Peaks"), row=3, col=1)

    # Row 4 — Quality
    if has_q:
        q_arr = np.array(quality)
        min_len = min(len(timestamps_ms), len(q_arr))
        ts_q, q_d = downsample(timestamps_ms[:min_len], q_arr[:min_len])
        fig.add_trace(go.Scatter(x=ts_q, y=q_d, mode="lines",
            line=dict(color="#AB63FA", width=1.5), name="Quality",
            fill="tozeroy", fillcolor="rgba(171,99,250,0.15)"), row=4, col=1)
        fig.add_hline(y=0.5, line_dash="dash", line_color="orange", row=4, col=1)
        fig.add_hline(y=0.8, line_dash="dash", line_color="limegreen", row=4, col=1)
        fig.update_yaxes(range=[0, 1], fixedrange=True, row=4, col=1)

    # Y autorange on all signal rows so it always fits visible data
    for r in range(1, (3 if not has_q else 3) + 1):
        fig.update_yaxes(autorange=True, row=r, col=1)

    fig.update_xaxes(title_text="Timestamp (ms)", row=n_rows, col=1)
    fig.update_layout(
        template=DARK,
        height=820 if has_q else 680,
        legend=dict(orientation="h", y=1.02, x=0),
        margin=dict(l=50, r=20, t=60, b=40),
    )
    return fig


def plot_raw_signal(timestamps_ms, signal, signal_col: str, original=None, baseline=None) -> go.Figure:
    ts, sig = downsample(timestamps_ms, signal)
    fig = go.Figure()
    if original is not None:
        _, orig_d = downsample(timestamps_ms, original)
        fig.add_trace(go.Scatter(
            x=ts, y=orig_d, mode="lines",
            line=dict(color="rgba(160,160,160,0.35)", width=1),
            name=f"{signal_col} (original)",
        ))
    fig.add_trace(go.Scatter(
        x=ts, y=sig, mode="lines",
        line=dict(color="#636EFA", width=1.5),
        name=f"{signal_col} (transformed)" if original is not None else signal_col,
    ))
    if baseline is not None:
        _, bl_d = downsample(timestamps_ms, baseline)
        fig.add_trace(go.Scatter(
            x=ts, y=bl_d, mode="lines",
            line=dict(color="#FFA15A", width=1.2, dash="dot"),
            name="sliding baseline",
        ))
    fig.update_layout(
        template=DARK,
        title=f"Raw Signal — {signal_col}",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    return fig


def plot_cleaned_overlay(timestamps_ms, raw, cleaned, signal_col: str) -> go.Figure:
    ts, cln_d = downsample(timestamps_ms, cleaned)
    fig = go.Figure()
    if raw is not None:
        _, raw_d = downsample(timestamps_ms, raw)
        fig.add_trace(go.Scatter(
            x=ts, y=raw_d, mode="lines",
            line=dict(color="rgba(160,160,160,0.4)", width=1),
            name="Raw",
        ))
    fig.add_trace(go.Scatter(
        x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned",
    ))
    fig.update_layout(
        template=DARK,
        title=f"Processed Signal — {signal_col}",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        legend=dict(orientation="h", y=1.02),
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def plot_peaks(timestamps_ms, cleaned, peak_indices) -> go.Figure:
    ts, cln_d = downsample(timestamps_ms, cleaned)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned",
    ))
    if len(peak_indices) > 0:
        peak_ts = timestamps_ms[peak_indices]
        peak_vals = cleaned[peak_indices]
        fig.add_trace(go.Scatter(
            x=peak_ts, y=peak_vals, mode="markers",
            marker=dict(color="#EF553B", size=8, symbol="triangle-up"),
            name="Peaks",
        ))
    fig.update_layout(
        template=DARK,
        title="Peak Detection",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        legend=dict(orientation="h", y=1.02),
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def plot_individual_beats(epochs: dict, hr_mean: float) -> go.Figure:
    """Overlay individual beat waveforms + thick average, x-axis in seconds."""
    # Collect all signals on a common time grid via interpolation
    time_min, time_max = np.inf, -np.inf
    for ep in epochs.values():
        t = ep.index.astype(float)
        time_min = min(time_min, t.min())
        time_max = max(time_max, t.max())

    common_t = np.linspace(time_min, time_max, 300)
    beat_matrix = []

    for ep in epochs.values():
        t = ep.index.astype(float)
        s = ep["Signal"].values
        if len(t) < 2:
            continue
        interp = np.interp(common_t, t, s)
        beat_matrix.append(interp)

    if not beat_matrix:
        return go.Figure()

    beat_matrix = np.array(beat_matrix)
    avg = np.mean(beat_matrix, axis=0)

    fig = go.Figure()

    # Individual beats — thin grey, all sharing one legend entry
    first = True
    for beat in beat_matrix:
        fig.add_trace(go.Scatter(
            x=common_t, y=beat, mode="lines",
            line=dict(color="rgba(180,180,180,0.25)", width=1),
            name="Individual beats",
            legendgroup="beats",
            showlegend=first,
        ))
        first = False

    # Average beat — thick red
    fig.add_trace(go.Scatter(
        x=common_t, y=avg, mode="lines",
        line=dict(color="#C0392B", width=4),
        name="Average beat shape",
    ))

    # Dashed vertical line at peak (t=0)
    fig.add_vline(x=0, line_dash="dash", line_color="grey", line_width=1.5)

    title = f"Individual beats ({len(beat_matrix)} beats"
    if hr_mean is not None:
        title += f", average heart rate: {hr_mean:.1f} bpm"
    title += ")"

    fig.update_layout(
        template=DARK,
        title=title,
        xaxis_title="Time (seconds)",
        yaxis_title="PPG",
        height=420,
        legend=dict(orientation="h", y=1.05),
        margin=dict(l=50, r=20, t=60, b=50),
    )
    return fig


# Per-method reference lines: (value, color, label)
# Sources: Elgendi 2016 (skewness=0), clinical PI (0.3/1.0%), correlation convention (0.5/0.8)
_QUALITY_REFS = {
    "templatematch":  [(0.5, "orange", "0.5 acceptable"), (0.8, "limegreen", "0.8 good")],
    "relative_power": [(0.5, "orange", "0.5 acceptable"), (0.8, "limegreen", "0.8 good")],
    "ho2025":         [(0.5, "orange", "0/1 boundary")],
    "skewness":       [(0.0, "orange", "0 threshold")],
    "dissimilarity":  [],
    "kurtosis":       [],
    "entropy":        [],
    "perfusion":      [(0.3, "orange", "0.3% acceptable"), (1.0, "limegreen", "1.0% good")],
}


def plot_quality(timestamps_ms, quality, method="templatematch") -> go.Figure:
    ts, q_d = downsample(timestamps_ms, quality)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=q_d, mode="lines",
        line=dict(color="#AB63FA", width=1.5),
        name="Quality",
        fill="tozeroy",
        fillcolor="rgba(171,99,250,0.15)",
    ))
    for val, color, label in _QUALITY_REFS.get(method, []):
        fig.add_hline(y=val, line_dash="dash", line_color=color, annotation_text=label)
    fig.update_layout(
        template=DARK,
        title=f"Signal Quality — {method}",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Quality", autorange=True),
        dragmode="select",
        height=350,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit App
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="PPG Filter & Analysis", layout="wide")
st.title("PPG Signal Filter & Analysis")

# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Data Source")
    data_source_mode = st.radio("Source", ["Demo files", "Upload file"], label_visibility="collapsed")

    df_raw = None
    file_ext = ".csv"

    if data_source_mode == "Demo files":
        chosen_file = st.selectbox("Choose demo file", DEMO_FILES)
        file_path = os.path.join(DATA_DIR, chosen_file)
        file_ext = os.path.splitext(chosen_file)[1].lower()
        df_raw = load_data(file_path, file_ext)
    else:
        uploaded = st.file_uploader("Upload CSV or XLSX", type=["csv", "xlsx", "xls"])
        if uploaded is not None:
            file_ext = os.path.splitext(uploaded.name)[1].lower()
            df_raw = load_data(uploaded, file_ext)
        else:
            st.info("Upload a file to continue.")
            st.stop()

    # Timestamp column detection
    ts_col = find_timestamp_col(df_raw)

    # Channel selection
    signal_cols = get_signal_columns(df_raw)
    if not signal_cols:
        st.warning("No valid signal columns found (all numeric non-timestamp columns are >80% NaN).")
        st.stop()

    signal_col = st.selectbox("Channel", signal_cols)

    # Reset timeline whenever file or channel changes
    _source_key = (
        chosen_file if data_source_mode == "Demo files" else getattr(uploaded, "name", ""),
        signal_col,
    )
    if st.session_state.get("_source_key") != _source_key:
        st.session_state.pop("analysis_window", None)
        st.session_state.pop("_pending_window", None)
        st.session_state["_source_key"] = _source_key

    signal_transform = st.radio(
        "Signal transform",
        ["None", "Invert (2^x − raw)", "Flip AC (2×mean − signal)"],
        help="None: use as-is  |  Invert: hardware ADC inversion  |  Flip AC: flip waveform polarity, preserve DC",
    )
    invert_signal = signal_transform == "Invert (2^x − raw)"
    flip_ac       = signal_transform == "Flip AC (2×mean − signal)"
    if invert_signal:
        adc_bits = st.number_input("ADC bits (x)", min_value=1, max_value=32,
                                   value=24, step=1,
                                   help="Inversion formula: 2^x − signal")
    if flip_ac:
        flip_ac_sliding = st.toggle("Sliding window mean", value=True,
                                    help="ON: rolling mean baseline tracks DC drift.  "
                                         "OFF: single global mean for the whole window.")
        if flip_ac_sliding:
            flip_ac_window_s = st.number_input(
                "Sliding window (s)",
                min_value=0.1, max_value=30.0, value=2.0, step=0.1,
                help="Width of the rolling mean used to estimate DC baseline. "
                     "Smaller → follows DC drift closely; larger → flatter baseline.",
            )

    st.divider()
    st.subheader("Sample Rate")

    # Prepare signal to get accurate SR from deduplicated data
    timestamps_ms, signal, detected_sr = prepare_signal(df_raw, signal_col, ts_col)

    st.metric("Detected", f"{detected_sr:.1f} Hz")
    override_sr = st.toggle("Override SR")
    if override_sr:
        sampling_rate = st.number_input("Manual SR (Hz)", min_value=1.0, max_value=10000.0,
                                        value=float(round(detected_sr, 1)), step=0.5)
    else:
        sampling_rate = detected_sr

    _t0 = float(timestamps_ms[0])
    _t1 = float(timestamps_ms[-1])
    _duration_s = (_t1 - _t0) / 1000

    if st.button("Reset Timeline", width="stretch"):
        st.session_state._pending_window = (_t0, _t1)
        st.rerun()

    st.divider()
    show_nk_plot = st.checkbox("Show NeuroKit2 native plot (matplotlib)")

    # Apply any window queued by a chart box-selection before the slider renders
    if "_pending_window" in st.session_state:
        st.session_state.analysis_window = st.session_state.pop("_pending_window")


# ── Helper: extract x-range from a Plotly box-selection event ─────────────────

def _extract_box_x(event):
    try:
        if event and event.selection and event.selection.box:
            box = event.selection.box[0]
            if box.get("x"):
                return float(box["x"][0]), float(box["x"][1])
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return None


# ── Resolve processing methods from session state (widgets rendered in sections) ─
clean_method   = st.session_state.get("clean_method",   CLEAN_METHODS[0])
peak_method    = st.session_state.get("peak_method",    PEAK_METHODS[0])
quality_method = st.session_state.get("quality_method", QUALITY_METHODS[0])

# ── Resolve time window from session state (slider rendered later in col_main) ─
# On first render session_state has no key → full range. Slider picks it up too.
win_ms = st.session_state.get("analysis_window", (_t0, _t1))

mask = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
timestamps_w  = timestamps_ms[mask]
signal_w_orig = signal[mask]
signal_w      = signal_w_orig.copy()
_flip_baseline = None
if invert_signal:
    signal_w = float(2 ** adc_bits) - signal_w_orig
elif flip_ac:
    if flip_ac_sliding:
        _win_samples = max(1, int(round(flip_ac_window_s * sampling_rate)))
        _flip_baseline = (
            pd.Series(signal_w_orig)
            .rolling(window=_win_samples, center=True, min_periods=1)
            .mean()
            .to_numpy()
        )
    else:
        _flip_baseline = np.full(len(signal_w_orig), np.mean(signal_w_orig))
    signal_w = 2.0 * _flip_baseline - signal_w_orig
_signal_transformed = invert_signal or flip_ac

# ── Run Pipeline ─────────────────────────────────────────────────────────────

if len(signal_w) < 10:
    st.error("Selected window is too short. Widen the time window in the sidebar.")
    st.stop()

signal_bytes = signal_w.tobytes()

try:
    results = cached_pipeline(signal_bytes, sampling_rate, clean_method, peak_method, quality_method)
except ValueError as e:
    st.error(f"Processing error: {e}\n\nTry a different cleaning/peak method or check the signal length.")
    st.stop()
except Exception as e:
    st.error(f"Unexpected error during pipeline: {e}")
    st.stop()

cleaned = results["cleaned"]
signals_df = results["signals_df"]
info = results["info"]
quality       = results["quality"]
quality_error = results["quality_error"]
analysis = results["analysis"]

peak_indices = info.get("PPG_Peaks", np.array([], dtype=int))

# ── Master export DataFrame ───────────────────────────────────────────────────

def make_export_df() -> pd.DataFrame:
    n = len(timestamps_w)
    col = signal_col.replace("-", "_")
    df = pd.DataFrame({
        "timestamp_ms":       timestamps_w,
        f"{col}_raw":         signal_w,
        f"{col}_cleaned":     cleaned,
        "PPG_Peak":           signals_df["PPG_Peaks"].values.astype(int),
        "PPG_Rate_bpm":       signals_df["PPG_Rate"].values
                              if "PPG_Rate" in signals_df.columns
                              else np.full(n, np.nan),
    })
    if quality is not None:
        q = np.array(quality)
        mn = min(n, len(q))
        df["signal_quality"] = np.concatenate([q[:mn], np.full(n - mn, np.nan)])
    else:
        df["signal_quality"] = np.nan
    return df

def _dl_button(label: str, df: pd.DataFrame, filename: str, key: str):
    st.download_button(label, df.to_csv(index=False).encode(),
                       filename, "text/csv", key=key, width="stretch")

# ── Auto beat windows ─────────────────────────────────────────────────────────

def auto_beat_windows(peak_indices, sampling_rate):
    """Derive pre/post windows from median RR interval (35% / 55% of RR)."""
    if len(peak_indices) < 2:
        return 0.2, 0.5
    rr_s = float(np.median(np.diff(peak_indices)) / sampling_rate)
    pre  = round(round(min(max(rr_s * 0.35, 0.10), 0.40) / 0.05) * 0.05, 2)
    post = round(round(min(max(rr_s * 0.55, 0.20), 0.80) / 0.05) * 0.05, 2)
    return pre, post

# Fingerprint peaks — auto-update slider defaults whenever peaks change
_peaks_fp = (len(peak_indices), int(np.sum(peak_indices)) if len(peak_indices) else 0)
if st.session_state.get("_peaks_fp") != _peaks_fp:
    _auto_pre, _auto_post = auto_beat_windows(peak_indices, sampling_rate)
    st.session_state.beat_pre   = _auto_pre
    st.session_state.beat_post  = _auto_post
    st.session_state._peaks_fp  = _peaks_fp
else:
    # Ensure keys always exist before sliders render (first load with no peaks)
    st.session_state.setdefault("beat_pre",  0.2)
    st.session_state.setdefault("beat_post", 0.5)

# ── HR Metrics ────────────────────────────────────────────────────────────────

def compute_hr_metrics(peak_indices, sampling_rate):
    if len(peak_indices) < 2:
        return None, None, None
    rr_ms = np.diff(peak_indices) / sampling_rate * 1000
    hr_inst = 60000 / rr_ms
    return float(np.mean(hr_inst)), float(np.min(hr_inst)), float(np.max(hr_inst))


# ── Layout: single-page with sticky right nav ─────────────────────────────────

st.markdown("""
<style>
.toc-nav {
    position: fixed;
    top: 5rem;
    right: 1.25rem;
    z-index: 999;
    background: rgba(14, 17, 23, 0.88);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    border-left: 2px solid rgba(255,255,255,0.1);
    border-radius: 0 6px 6px 0;
    padding: 0.65rem 0.9rem 0.65rem 0.75rem;
    min-width: 130px;
}
.toc-nav a {
    display: block;
    padding: 0.3rem 0;
    font-size: 0.75rem;
    color: rgba(255,255,255,0.45);
    text-decoration: none;
    line-height: 1.3;
    transition: color 0.15s;
    white-space: nowrap;
}
.toc-nav a:hover { color: rgba(255,255,255,0.9); }
.toc-title {
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.25);
    margin-bottom: 0.4rem;
}
.section-anchor { scroll-margin-top: 5rem; }
</style>
""", unsafe_allow_html=True)

# Fixed nav — rendered outside any column so it doesn't consume page width
st.markdown("""
<div class="toc-nav">
  <div class="toc-title">On this page</div>
  <a href="#raw-data">Raw Data</a>
  <a href="#processed-signal">Processed Signal</a>
  <a href="#peak-detection">Peak Detection</a>
  <a href="#individual-beats">Individual Beats</a>
  <a href="#hrv-analysis">HRV / Analysis</a>
  <a href="#signal-quality">Signal Quality</a>
</div>
""", unsafe_allow_html=True)

hr_mean, hr_min, hr_max = compute_hr_metrics(peak_indices, sampling_rate)


# ── Time Window (top of main) ─────────────────────────────────────────────

st.subheader("Time Window")
wc1, wc2 = st.columns([11, 1])
with wc1:
    win_ms = st.slider(
        "Analysis window",
        min_value=_t0, max_value=_t1,
        value=(_t0, _t1),
        key="analysis_window",
        label_visibility="collapsed",
    )
with wc2:
    if st.button("Reset", width="stretch", key="reset_main"):
        st.session_state._pending_window = (_t0, _t1)
        st.rerun()

win_dur = (win_ms[1] - win_ms[0]) / 1000
st.caption(f"{win_dur:.1f} s selected of {_duration_s:.1f} s total — box-select any chart below to zoom")

# Apply window (re-apply mask for slider; transforms already applied above)
mask = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
timestamps_w = timestamps_ms[mask]

st.divider()

# ── Section 1: Raw Data ───────────────────────────────────────────────────

st.markdown('<div class="section-anchor" id="raw-data"></div>', unsafe_allow_html=True)
st.header("Raw Data")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Rows", len(df_raw))
col2.metric("Columns", len(df_raw.columns))
col3.metric("Duration (window)", f"{(timestamps_w[-1] - timestamps_w[0]) / 1000:.1f} s")
col4.metric("SR", f"{sampling_rate:.1f} Hz")

st.caption("Box-select a region to zoom all charts")
ev_raw = st.plotly_chart(plot_raw_signal(timestamps_w, signal_w, signal_col,
                                         original=signal_w_orig if _signal_transformed else None,
                                         baseline=_flip_baseline),
                         width="stretch", key="chart_raw",
                         on_select="rerun", selection_mode="box")
_b = _extract_box_x(ev_raw)
if _b:
    st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
    st.rerun()

st.subheader("Signal Statistics")
stats = pd.Series(signal_w, name=signal_col).describe()
st.dataframe(stats.to_frame().T, width="stretch")

with st.expander("Data Preview (full file, head 200)"):
    st.dataframe(df_raw.head(200), width="stretch")

_ex = make_export_df()
_dl_button("Export Raw CSV", _ex[["timestamp_ms", f"{signal_col.replace('-','_')}_raw"]],
           "raw_signal.csv", "dl_raw")

st.divider()

# ── Section 2: Processed Signal ──────────────────────────────────────────

st.markdown('<div class="section-anchor" id="processed-signal"></div>', unsafe_allow_html=True)
st.header("Processed Signal")

_pcol1, _pcol2 = st.columns(2)
_pcol1.selectbox("Cleaning Method", CLEAN_METHODS,
                 index=CLEAN_METHODS.index(clean_method), key="clean_method")
_pcol2.selectbox("Peak Detection Method", PEAK_METHODS,
                 index=PEAK_METHODS.index(peak_method), key="peak_method")

show_raw_overlay = st.checkbox("Show raw signal", value=False, key="show_raw_overlay")
st.caption("Box-select a region to zoom all charts")
ev_proc = st.plotly_chart(
    plot_cleaned_overlay(timestamps_w, signal_w if show_raw_overlay else None, cleaned, signal_col),
    width="stretch", key="chart_proc",
    on_select="rerun", selection_mode="box")
_b = _extract_box_x(ev_proc)
if _b:
    st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
    st.rerun()
st.caption(f"Cleaning method: **{clean_method}** | SR: **{sampling_rate:.1f} Hz**")

_ex = make_export_df(); _c = signal_col.replace("-", "_")
_dl_button("Export Processed CSV",
           _ex[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned"]],
           "processed_signal.csv", "dl_proc")

st.divider()

# ── Section 3: Peak Detection ─────────────────────────────────────────────

st.markdown('<div class="section-anchor" id="peak-detection"></div>', unsafe_allow_html=True)
st.header("Peak Detection")

st.caption("Box-select a region to zoom all charts")
ev_peaks = st.plotly_chart(plot_peaks(timestamps_w, cleaned, peak_indices),
                           width="stretch", key="chart_peaks",
                           on_select="rerun", selection_mode="box")
_b = _extract_box_x(ev_peaks)
if _b:
    st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
    st.rerun()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Peaks Detected", len(peak_indices))
if hr_mean is not None:
    m2.metric("Mean HR", f"{hr_mean:.1f} bpm")
    m3.metric("Min HR",  f"{hr_min:.1f} bpm")
    m4.metric("Max HR",  f"{hr_max:.1f} bpm")

st.caption(f"Peak method: **{peak_method}** | Cleaning: **{clean_method}**")

_ex = make_export_df(); _c = signal_col.replace("-", "_")
_dl_button("Export Peaks CSV",
           _ex[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned", "PPG_Peak", "PPG_Rate_bpm"]],
           "peak_detection.csv", "dl_peaks")

if show_nk_plot:
    with st.expander("NeuroKit2 Native Plot", expanded=True):
        try:
            fig_nk = nk.ppg_plot(signals_df, info)
            st.pyplot(fig_nk)
            plt.close(fig_nk)
        except Exception as e:
            st.warning(f"Could not render NeuroKit2 native plot: {e}")

st.divider()

# ── Section 4: Individual Beats ──────────────────────────────────────────

st.markdown('<div class="section-anchor" id="individual-beats"></div>', unsafe_allow_html=True)
st.header("Individual Beats")

beat_pre  = st.session_state.get("beat_pre",  0.2)
beat_post = st.session_state.get("beat_post", 0.5)

if len(peak_indices) < 2:
    st.warning("Not enough peaks detected to segment individual beats.")
else:
    try:
        epochs = cached_epochs(
            cleaned.tobytes(),
            peak_indices.astype(np.int64).tobytes(),
            sampling_rate, -beat_pre, beat_post,
        )
        hr_mean_beats, _, _ = compute_hr_metrics(peak_indices, sampling_rate)
        st.plotly_chart(
            plot_individual_beats(epochs, hr_mean_beats),
            width="stretch",
            key=f"chart_beats_{beat_pre}_{beat_post}",
        )
        st.caption(
            f"{len(epochs)} beats | window: −{beat_pre}s to +{beat_post}s around each peak"
        )
    except Exception as e:
        st.error(f"Beat segmentation failed: {e}")

st.subheader("Beat Segmentation")
bc1, bc2 = st.columns(2)
with bc1:
    st.slider("Pre-peak window (s)",  0.1, 0.5, step=0.05, key="beat_pre")
with bc2:
    st.slider("Post-peak window (s)", 0.2, 1.0, step=0.05, key="beat_post")

st.divider()

# ── Section 5: HRV / Analysis ─────────────────────────────────────────────

st.markdown('<div class="section-anchor" id="hrv-analysis"></div>', unsafe_allow_html=True)
st.header("HRV / Analysis")

if analysis is not None:
    st.dataframe(analysis, width="stretch")
    csv_buf = io.BytesIO()
    analysis.to_csv(csv_buf, index=False)
    st.download_button(
        label="Download Analysis CSV",
        data=csv_buf.getvalue(),
        file_name="ppg_analysis.csv",
        mime="text/csv",
    )
else:
    st.info("Window too short for full HRV analysis — widen the time window for entropy and frequency-domain metrics.")
    st.dataframe(signals_df.head(500), width="stretch")

st.divider()

# ── Section 6: Signal Quality ─────────────────────────────────────────────

st.markdown('<div class="section-anchor" id="signal-quality"></div>', unsafe_allow_html=True)
st.header("Signal Quality")

st.selectbox("Quality Method", QUALITY_METHODS,
             index=QUALITY_METHODS.index(quality_method), key="quality_method")

if quality is not None:
    q_arr = np.array(quality)
    min_len = min(len(timestamps_w), len(q_arr))
    st.caption("Box-select a region to zoom all charts")
    ev_qual = st.plotly_chart(plot_quality(timestamps_w[:min_len], q_arr[:min_len], quality_method),
                              width="stretch", key="chart_qual",
                              on_select="rerun", selection_mode="box")
    _b = _extract_box_x(ev_qual)
    if _b:
        st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
        st.rerun()

    mean_q = float(np.nanmean(q_arr))
    c1, c2 = st.columns(2)
    c1.metric("Mean Quality", f"{mean_q:.3f}")
    _good_refs = _QUALITY_REFS.get(quality_method, [])
    _good_thresh = next((v for v, _, lbl in _good_refs if "good" in lbl or "boundary" in lbl), None)
    if _good_thresh is not None:
        pct_good = float(np.mean(q_arr >= _good_thresh) * 100)
        c2.metric(f"% Above {_good_thresh}", f"{pct_good:.1f}%")
    elif quality_method == "skewness":
        pct_good = float(np.mean(q_arr >= 0.0) * 100)
        c2.metric("% Above 0 (good)", f"{pct_good:.1f}%")
    else:
        c2.metric("Std Dev", f"{float(np.nanstd(q_arr)):.3f}")
else:
    if quality_error:
        st.warning(f"Signal quality unavailable: `{quality_error}`")
    else:
        st.warning("Signal quality index not available for this signal/method combination.")

_ex = make_export_df(); _c = signal_col.replace("-", "_")
_dl_button("Export Signal Quality CSV",
           _ex[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned", "signal_quality"]],
           "signal_quality.csv", "dl_qual")

# ── Sidebar: all-in-one export (appended after pipeline runs) ─────────────────

with st.sidebar:
    st.divider()
    _dl_button("Export All Data", make_export_df(), "all_data.csv", "dl_all")
