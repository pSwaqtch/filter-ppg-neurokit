"""app.py — Streamlit UI for PPG Signal Filter & Analysis.

Thin UI layer: data loading, session state, @st.cache_data wrappers, and
section rendering. All signal processing lives in ppg_processing.py;
all chart builders live in ppg_charts.py.

Requires Python 3.10+  (neurokit2 ≥0.2.10 uses float | None, PEP 604).
"""
import matplotlib
matplotlib.use("Agg")

import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import neurokit2 as nk
import io
import os
import time

from ppg_processing import (
    CLEAN_METHODS, PEAK_METHODS, QUALITY_METHODS, TIMESTAMP_COL, QUALITY_REFS,
    calculate_sample_rate, prepare_signal, apply_signal_transform,
    run_pipeline, extract_epochs, auto_beat_windows, compute_hr_metrics,
)
from ppg_charts import (
    plot_signal_overview, plot_raw_signal, plot_cleaned_overlay,
    plot_peaks, plot_individual_beats, plot_quality,
)
from usb_serial import (
    SERIAL_AVAILABLE, list_serial_ports, describe_ports,
    find_port_owner, force_release_port, test_connection,
    send_command, receive_binary_stream, stream_binary_live,
)

# ─────────────────────────────────────────────────────────────────────────────
# App-level constants
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DEMO_FILES = [
    "trial_data_1.csv",
    "trial_data_2.csv",
    "trial_data_3.csv",
    "trial_data_4.csv",
    "trial_data_5.xlsx",
]

# ─────────────────────────────────────────────────────────────────────────────
# Data loading helpers (Streamlit IO — not portable to C)
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
        if df[col].isna().mean() < 0.8:
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
# Cached wrappers (serialise numpy arrays for st.cache_data hash)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Running NeuroKit2 pipeline…")
def cached_pipeline(
    signal_bytes: bytes,
    sampling_rate: float,
    clean_method: str,
    peak_method: str,
    quality_method: str = "templatematch",
) -> dict:
    """Cache wrapper around run_pipeline(). Signal passed as bytes for hashability."""
    signal = np.frombuffer(signal_bytes, dtype=np.float64)
    return run_pipeline(signal, sampling_rate, clean_method, peak_method, quality_method)


@st.cache_data(show_spinner=False)
def cached_epochs(
    signal_bytes: bytes,
    peak_indices_bytes: bytes,
    sampling_rate: float,
    epochs_start: float,
    epochs_end: float,
) -> dict:
    """Cache wrapper around extract_epochs(). Arrays passed as bytes for hashability."""
    signal = np.frombuffer(signal_bytes, dtype=np.float64)
    peak_indices = np.frombuffer(peak_indices_bytes, dtype=np.int64)
    return extract_epochs(signal, peak_indices, sampling_rate, epochs_start, epochs_end)


@st.cache_data(show_spinner=False)
def cached_prepare_signal(df: pd.DataFrame, signal_col: str, timestamp_col: str):
    return prepare_signal(df, signal_col, timestamp_col)


# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_box_x(event):
    """Extract x-range [lo, hi] from a Plotly box-selection event, or None."""
    try:
        if event and event.selection and event.selection.box:
            box = event.selection.box[0]
            if box.get("x"):
                return float(box["x"][0]), float(box["x"][1])
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return None


def _dl_button(label: str, df: pd.DataFrame, filename: str, key: str):
    st.download_button(label, df.to_csv(index=False).encode(),
                       filename, "text/csv", key=key, width="stretch")


def make_export_df(
    timestamps_w, signal_w, cleaned, signals_df,
    signal_col, signal_bytes, sampling_rate,
    clean_method, peak_method, quality_methods,
) -> pd.DataFrame:
    """Build the master export DataFrame for the current window."""
    n   = len(timestamps_w)
    col = signal_col.replace("-", "_")
    df  = pd.DataFrame({
        "timestamp_ms":   timestamps_w,
        f"{col}_raw":     signal_w,
        f"{col}_cleaned": cleaned,
        "PPG_Peak":       signals_df["PPG_Peaks"].values.astype(int),
        "PPG_Rate_bpm":   signals_df["PPG_Rate"].values
                          if "PPG_Rate" in signals_df.columns
                          else np.full(n, np.nan),
    })
    for _qm in quality_methods:
        _qr = cached_pipeline(signal_bytes, sampling_rate, clean_method, peak_method, _qm)
        _q  = _qr["quality"]
        if _q is not None:
            _qa = np.array(_q)
            mn  = min(n, len(_qa))
            df[f"quality_{_qm}"] = np.concatenate([_qa[:mn], np.full(n - mn, np.nan)])
        else:
            df[f"quality_{_qm}"] = np.nan
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="PPG Filter & Analysis", layout="wide")
st.title("PPG Signal Filter & Analysis")

_tab_analysis, _tab_serial = st.tabs(["Analysis", "USB Serial"])

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

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

    ts_col = find_timestamp_col(df_raw)

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

    # ── Signal Transform ──────────────────────────────────────────────────────
    signal_transform = st.radio(
        "Signal transform",
        ["None", "Invert (2^x − raw)", "Flip AC (2×mean − signal)"],
        help="None: use as-is  |  Invert: hardware ADC inversion  |  Flip AC: flip waveform polarity, preserve DC",
    )
    invert_signal = signal_transform == "Invert (2^x − raw)"
    flip_ac       = signal_transform == "Flip AC (2×mean − signal)"

    adc_bits = 24
    flip_ac_sliding  = True
    flip_ac_window_s = 2.0

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
                help="Width of the rolling mean used to estimate DC baseline.",
            )

    st.divider()
    st.subheader("Sample Rate")

    timestamps_ms, signal, detected_sr = cached_prepare_signal(df_raw, signal_col, ts_col)

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

    if "_pending_window" in st.session_state:
        st.session_state.analysis_window = st.session_state.pop("_pending_window")


# ─────────────────────────────────────────────────────────────────────────────
# Resolve session state before pipeline
# ─────────────────────────────────────────────────────────────────────────────

clean_method    = st.session_state.get("clean_method",    CLEAN_METHODS[0])
peak_method     = st.session_state.get("peak_method",     PEAK_METHODS[0])
quality_methods = st.session_state.get("quality_methods", [QUALITY_METHODS[0]])
if not quality_methods:
    quality_methods = [QUALITY_METHODS[0]]
quality_method = quality_methods[0]   # used for overview subplot

win_ms = st.session_state.get("analysis_window", (_t0, _t1))

# ── Apply window mask + signal transform ──────────────────────────────────────
mask = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
timestamps_w  = timestamps_ms[mask]
signal_w_orig = signal[mask]

transform_mode = "invert" if invert_signal else ("flip_ac" if flip_ac else "none")
signal_w, _flip_baseline = apply_signal_transform(
    signal_w_orig,
    mode=transform_mode,
    adc_bits=adc_bits,
    flip_sliding=flip_ac_sliding,
    flip_window_s=flip_ac_window_s,
    sampling_rate=sampling_rate,
)
_signal_transformed = transform_mode != "none"

# ── Run Pipeline ──────────────────────────────────────────────────────────────

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

cleaned    = results["cleaned"]
signals_df = results["signals_df"]
info       = results["info"]
quality    = results["quality"]
analysis   = results["analysis"]

peak_indices = info.get("PPG_Peaks", np.array([], dtype=int))

# ── Auto beat windows (update on peak change) ────────────────────────────────
_peaks_fp = (len(peak_indices), int(np.sum(peak_indices)) if len(peak_indices) else 0)
if st.session_state.get("_peaks_fp") != _peaks_fp:
    _auto_pre, _auto_post = auto_beat_windows(peak_indices, sampling_rate)
    st.session_state.beat_pre  = _auto_pre
    st.session_state.beat_post = _auto_post
    st.session_state._peaks_fp = _peaks_fp
else:
    st.session_state.setdefault("beat_pre",  0.2)
    st.session_state.setdefault("beat_post", 0.5)

hr_mean, hr_min, hr_max = compute_hr_metrics(peak_indices, sampling_rate)

# ─────────────────────────────────────────────────────────────────────────────
# Tab 1: Analysis
# ─────────────────────────────────────────────────────────────────────────────

with _tab_analysis:
    st.markdown("""
<style>
.toc-nav {
    position: fixed; top: 5rem; right: 1.25rem; z-index: 999;
    background: rgba(14, 17, 23, 0.88); backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    border-left: 2px solid rgba(255,255,255,0.1);
    border-radius: 0 6px 6px 0;
    padding: 0.65rem 0.9rem 0.65rem 0.75rem; min-width: 130px;
}
.toc-nav a {
    display: block; padding: 0.3rem 0; font-size: 0.75rem;
    color: rgba(255,255,255,0.45); text-decoration: none;
    line-height: 1.3; transition: color 0.15s; white-space: nowrap;
}
.toc-nav a:hover { color: rgba(255,255,255,0.9); }
.toc-title {
    font-size: 0.65rem; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: rgba(255,255,255,0.25); margin-bottom: 0.4rem;
}
.section-anchor { scroll-margin-top: 5rem; }
</style>
""", unsafe_allow_html=True)

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

    # ── Time Window slider ────────────────────────────────────────────────────

    st.subheader("Time Window")
    wc1, wc2 = st.columns([11, 1])
    with wc1:
        win_ms = st.slider(
            "Analysis window", min_value=_t0, max_value=_t1, value=(_t0, _t1),
            key="analysis_window", label_visibility="collapsed",
        )
    with wc2:
        if st.button("Reset", width="stretch", key="reset_main"):
            st.session_state._pending_window = (_t0, _t1)
            st.rerun()

    win_dur = (win_ms[1] - win_ms[0]) / 1000
    st.caption(f"{win_dur:.1f} s selected of {_duration_s:.1f} s total — box-select any chart below to zoom")

    mask = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
    timestamps_w = timestamps_ms[mask]

    st.divider()

    # Convenience shortcut used repeatedly below
    def _ex():
        return make_export_df(timestamps_w, signal_w, cleaned, signals_df,
                              signal_col, signal_bytes, sampling_rate,
                              clean_method, peak_method, quality_methods)

    _c = signal_col.replace("-", "_")

    # ── Section 1: Raw Data ───────────────────────────────────────────────────

    st.markdown('<div class="section-anchor" id="raw-data"></div>', unsafe_allow_html=True)
    st.header("Raw Data")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Rows",              len(df_raw))
    col2.metric("Columns",           len(df_raw.columns))
    col3.metric("Duration (window)", f"{(timestamps_w[-1] - timestamps_w[0]) / 1000:.1f} s")
    col4.metric("SR",                f"{sampling_rate:.1f} Hz")

    st.caption("Box-select a region to zoom all charts")
    ev_raw = st.plotly_chart(
        plot_raw_signal(timestamps_w, signal_w, signal_col,
                        original=signal_w_orig if _signal_transformed else None,
                        baseline=_flip_baseline),
        width="stretch", key="chart_raw", on_select="rerun", selection_mode="box",
    )
    _b = _extract_box_x(ev_raw)
    if _b:
        st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
        st.rerun()

    st.subheader("Signal Statistics")
    st.dataframe(pd.Series(signal_w, name=signal_col).describe().to_frame().T, width="stretch")

    with st.expander("Data Preview (full file, head 200)"):
        st.dataframe(df_raw.head(200), width="stretch")

    _dl_button("Export Raw CSV", _ex()[["timestamp_ms", f"{_c}_raw"]], "raw_signal.csv", "dl_raw")

    st.divider()

    # ── Section 2: Processed Signal ───────────────────────────────────────────

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
        width="stretch", key="chart_proc", on_select="rerun", selection_mode="box",
    )
    _b = _extract_box_x(ev_proc)
    if _b:
        st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
        st.rerun()
    st.caption(f"Cleaning method: **{clean_method}** | SR: **{sampling_rate:.1f} Hz**")

    _dl_button("Export Processed CSV",
               _ex()[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned"]],
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

    _dl_button("Export Peaks CSV",
               _ex()[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned", "PPG_Peak", "PPG_Rate_bpm"]],
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

    # ── Section 4: Individual Beats ───────────────────────────────────────────

    st.markdown('<div class="section-anchor" id="individual-beats"></div>', unsafe_allow_html=True)
    st.header("Individual Beats")

    beat_pre  = st.session_state.get("beat_pre",  0.2)
    beat_post = st.session_state.get("beat_post", 0.5)

    if len(peak_indices) < 2:
        st.warning("Not enough peaks detected to segment individual beats.")
    else:
        try:
            epochs = cached_epochs(
                cleaned.tobytes(), peak_indices.astype(np.int64).tobytes(),
                sampling_rate, -beat_pre, beat_post,
            )
            hr_mean_beats, _, _ = compute_hr_metrics(peak_indices, sampling_rate)
            st.plotly_chart(
                plot_individual_beats(epochs, hr_mean_beats),
                width="stretch", key=f"chart_beats_{beat_pre}_{beat_post}",
            )
            st.caption(f"{len(epochs)} beats | window: −{beat_pre}s to +{beat_post}s around each peak")
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
        st.download_button("Download Analysis CSV", csv_buf.getvalue(),
                           "ppg_analysis.csv", "text/csv")
    else:
        st.info("Window too short for full HRV analysis — widen the time window.")
        st.dataframe(signals_df.head(500), width="stretch")

    st.divider()

    # ── Section 6: Signal Quality ─────────────────────────────────────────────

    st.markdown('<div class="section-anchor" id="signal-quality"></div>', unsafe_allow_html=True)
    st.header("Signal Quality")

    st.segmented_control(
        "Quality Methods", QUALITY_METHODS,
        selection_mode="multi", default=[QUALITY_METHODS[0]], key="quality_methods",
    )

    _quality_map    = {}
    _quality_errors = {}
    for _qm in quality_methods:
        _qres = cached_pipeline(signal_bytes, sampling_rate, clean_method, peak_method, _qm)
        if _qres["quality"] is not None:
            _qa = np.array(_qres["quality"])
            _minlen = min(len(timestamps_w), len(_qa))
            _quality_map[_qm] = _qa[:_minlen]
        elif _qres["quality_error"]:
            _quality_errors[_qm] = _qres["quality_error"]
        else:
            _quality_errors[_qm] = "not available"

    if _quality_map:
        _common_len  = min(len(v) for v in _quality_map.values())
        _aligned_map = {m: v[:_common_len] for m, v in _quality_map.items()}
        st.caption("Box-select a region to zoom all charts")
        ev_qual = st.plotly_chart(
            plot_quality(timestamps_w[:_common_len], _aligned_map),
            width="stretch", key=f"chart_qual_{'_'.join(quality_methods)}",
            on_select="rerun", selection_mode="box",
        )
        _b = _extract_box_x(ev_qual)
        if _b:
            st.session_state._pending_window = (max(_t0, min(_b)), min(_t1, max(_b)))
            st.rerun()

        _mcols = st.columns(max(1, len(_quality_map)))
        for _col, (_qm, _qa) in zip(_mcols, _quality_map.items()):
            _mean = float(np.nanmean(_qa))
            _good_refs   = QUALITY_REFS.get(_qm, [])
            _good_thresh = next((v for v, _, lbl in _good_refs if "good" in lbl or "boundary" in lbl), None)
            if _good_thresh is not None:
                _pct = float(np.mean(_qa >= _good_thresh) * 100)
                _col.metric(f"{_qm}", f"{_mean:.3f}", f"{_pct:.0f}% above {_good_thresh}")
            elif _qm == "skewness":
                _pct = float(np.mean(_qa >= 0.0) * 100)
                _col.metric(f"{_qm}", f"{_mean:.3f}", f"{_pct:.0f}% above 0")
            else:
                _col.metric(f"{_qm}", f"{_mean:.3f}", f"σ={float(np.nanstd(_qa)):.3f}")

    for _qm, _err in _quality_errors.items():
        st.warning(f"**{_qm}**: `{_err}`")

    _dl_button("Export Signal Quality CSV", _ex(), "signal_quality.csv", "dl_qual")

# ─────────────────────────────────────────────────────────────────────────────
# Tab 2: USB Serial
# ─────────────────────────────────────────────────────────────────────────────

with _tab_serial:
    st.header("USB Serial")

    if not SERIAL_AVAILABLE:
        st.error("`pyserial` is not installed. Run: `pip install pyserial`")
        st.stop()

    # ── Session-state helpers ─────────────────────────────────────────────────

    def _conn_log(msg: str, level: str = "info"):
        """Append a timestamped entry to the persistent connection log."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        st.session_state.setdefault("serial_conn_log", []).append(
            (ts, level, msg)
        )

    _is_connected   = st.session_state.get("serial_connected", False)
    _active_port    = st.session_state.get("serial_active_port", "")
    _active_baud    = st.session_state.get("serial_active_baud", 115200)

    # ── Port / baud pickers (disabled when connected) ─────────────────────────

    _ports = list_serial_ports()
    _sc1, _sc2, _sc3, _sc4 = st.columns([3, 2, 1, 1])

    with _sc1:
        if _ports:
            _default_idx = _ports.index(_active_port) if _active_port in _ports else 0
            _port = st.selectbox("Serial Port", _ports, index=_default_idx,
                                 key="serial_port", disabled=_is_connected)
        else:
            _port = st.text_input("Serial Port (manual)",
                                  value=_active_port or "/dev/tty.usbmodem101",
                                  key="serial_port_manual", disabled=_is_connected)

    with _sc2:
        _baud_opts = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        _baud_idx  = _baud_opts.index(_active_baud) if _active_baud in _baud_opts else 4
        _baud = st.selectbox("Baud Rate", _baud_opts, index=_baud_idx,
                             key="serial_baud", disabled=_is_connected)

    with _sc3:
        if not _is_connected:
            if st.button("Connect", type="primary", width="stretch", key="serial_connect_btn"):
                with st.spinner(f"Connecting to {_port}…"):
                    _chk = test_connection(_port, _baud)
                if _chk.ok:
                    st.session_state["serial_connected"]   = True
                    st.session_state["serial_active_port"] = _port
                    st.session_state["serial_active_baud"] = _baud
                    st.session_state.pop("serial_last_error", None)
                    _conn_log(f"Connected — {_port} @ {_baud} baud", "ok")
                else:
                    st.session_state["serial_last_error"] = _chk.error or ""
                    _conn_log(f"Connect failed: {_chk.error}", "error")
                st.rerun()
        else:
            if st.button("Disconnect", type="secondary", width="stretch", key="serial_disconnect_btn"):
                st.session_state["serial_connected"] = False
                st.session_state.pop("serial_last_error", None)
                _conn_log(f"Disconnected from {_active_port}", "info")
                st.rerun()

    with _sc4:
        if st.button("Refresh", width="stretch", key="serial_refresh_btn",
                     disabled=_is_connected):
            st.rerun()

    # ── Status badge ──────────────────────────────────────────────────────────

    _last_error = st.session_state.get("serial_last_error", "")

    if _is_connected:
        _port_descs = {p["device"]: p["description"] for p in describe_ports()}
        _desc = _port_descs.get(_active_port, _active_port)
        st.success(f"Connected — **{_active_port}** @ {_active_baud} baud  |  {_desc}")
    else:
        if _ports:
            _port_descs = {p["device"]: p["description"] for p in describe_ports()}
            if _port in _port_descs:
                st.caption(f"Device: {_port_descs[_port]}")

        if _last_error and "PORT_BUSY" in _last_error:
            # Show owner info + force-release option
            _owner = find_port_owner(_port)
            _owner_str = f"held by **{_owner[1]}** (PID {_owner[0]})" if _owner else "owner unknown"
            st.error(f"Port busy — {_owner_str}")
            _fc1, _fc2 = st.columns([3, 1])
            with _fc1:
                st.caption(
                    "Another process has the port open. "
                    "Force-disconnect will terminate that process and reconnect."
                )
            with _fc2:
                if st.button("Force Disconnect & Connect", type="primary",
                             width="stretch", key="serial_force_btn"):
                    with st.spinner("Releasing port…"):
                        _rel = force_release_port(_port)
                    if _rel.ok:
                        _conn_log(f"Force release: {_rel.response}", "warn")
                        # Small pause to let OS free the port, then reconnect
                        time.sleep(0.5)
                        _chk2 = test_connection(_port, _baud)
                        if _chk2.ok:
                            st.session_state["serial_connected"]   = True
                            st.session_state["serial_active_port"] = _port
                            st.session_state["serial_active_baud"] = _baud
                            st.session_state.pop("serial_last_error", None)
                            _conn_log(f"Connected after force release — {_port} @ {_baud} baud", "ok")
                        else:
                            st.session_state["serial_last_error"] = _chk2.error or ""
                            _conn_log(f"Connect after force release failed: {_chk2.error}", "error")
                    else:
                        _conn_log(f"Force release failed: {_rel.error}", "error")
                        st.session_state["serial_last_error"] = f"FORCE_FAILED: {_rel.error}"
                    st.rerun()
        elif _last_error:
            st.error(_last_error)
        else:
            st.warning("Not connected")

    # ── Connection log ────────────────────────────────────────────────────────

    _conn_entries = st.session_state.get("serial_conn_log", [])
    with st.expander(f"Connection log ({len(_conn_entries)} entries)", expanded=bool(_conn_entries)):
        if _conn_entries:
            _level_icons = {"ok": "✓", "error": "✗", "warn": "!", "info": "·"}
            _log_lines = [
                f"[{ts}]  {_level_icons.get(lvl, '·')}  {msg}"
                for ts, lvl, msg in reversed(_conn_entries)
            ]
            st.code("\n".join(_log_lines), language="text")
            if st.button("Clear log", key="serial_clear_log"):
                st.session_state["serial_conn_log"] = []
                st.rerun()
        else:
            st.caption("No events yet.")

    st.divider()

    # ── Guard: require connection for commands / stream ───────────────────────

    if not _is_connected:
        st.info("Connect to a device above to send commands or capture a stream.")
        st.stop()

    # ── Command console ───────────────────────────────────────────────────────

    st.subheader("Command Console")

    _KNOWN_CMDS = [
        "adpd ppg stream-bin 100",
        "adpd ppg stream-bin 500",
        "adpd ppg stream-bin 1000",
        "adpd ppg start",
        "adpd ppg stop",
        "adpd ppg status",
        "adpd status",
        "help",
    ]

    # on_change fills the text input BEFORE it renders — avoids SessionState conflict
    def _apply_preset():
        preset = st.session_state.get("_cmd_preset")
        if preset:
            st.session_state["serial_cmd_input"] = preset
            st.session_state["_cmd_preset"] = None

    st.selectbox(
        "Preset commands",
        options=[""] + _KNOWN_CMDS,
        index=0,
        format_func=lambda x: "— pick a preset —" if x == "" else x,
        key="_cmd_preset",
        on_change=_apply_preset,
        label_visibility="collapsed",
    )

    _cmd_input = st.text_input(
        "Command",
        key="serial_cmd_input",
        label_visibility="collapsed",
        placeholder="Type any command…",
    )

    _resp_timeout = st.number_input("Response timeout (s)", min_value=0.5, max_value=30.0,
                                    value=3.0, step=0.5, key="serial_resp_timeout")

    _send_clicked = st.button("Send", type="primary", width="stretch", key="serial_send_btn",
                              disabled=not bool(_cmd_input and _cmd_input.strip()))

    if _send_clicked and _cmd_input and _cmd_input.strip():
        _cmd_str = _cmd_input.strip()
        with st.spinner(f"Sending: `{_cmd_str}`…"):
            _result = send_command(_active_port, _active_baud, _cmd_str,
                                   response_timeout_s=_resp_timeout)
        if _result.ok:
            _conn_log(f">> {_cmd_str}", "info")
            if _result.response:
                _conn_log(f"<< {_result.response[:120]}", "info")
            if _result.response:
                st.code(_result.response, language="text")
            else:
                st.info("No response received within timeout.")
        else:
            _conn_log(f"Command error: {_result.error}", "error")
            st.error(f"Error: {_result.error}")

    st.divider()

    # ── Binary stream capture ─────────────────────────────────────────────────

    st.subheader("Binary Stream Capture")
    st.caption(
        "Sends `adpd ppg stream-bin N` — device starts PPG, streams N × 20 bytes "
        "(timestamp_ms + 4 × uint32 little-endian per sample), then stops PPG.  "
        "Ch3/Ch4 = PPG (IN3 paired), Ch1/Ch2 = ambient."
    )

    _bs1, _bs2, _bs3 = st.columns([2, 2, 1])
    with _bs1:
        _n_samples = st.number_input("Samples to capture", min_value=10, max_value=100000,
                                     value=500, step=50, key="serial_n_samples")
    with _bs2:
        _stream_timeout = st.number_input("Stream timeout (s)", min_value=5.0, max_value=120.0,
                                          value=30.0, step=5.0, key="serial_stream_timeout")
    with _bs3:
        _live_mode = st.toggle("Live graph", value=True, key="serial_live_mode",
                               help="Update chart as data arrives instead of waiting for all samples")

    # ── Start / Stop buttons ──────────────────────────────────────────────────

    import threading as _threading

    _is_streaming = st.session_state.get("serial_streaming", False)

    _btn_col1, _btn_col2 = st.columns([3, 1])
    with _btn_col1:
        _capture_btn = st.button(
            "Capture Stream", type="primary", width="stretch",
            key="serial_capture_btn", disabled=_is_streaming,
        )
    with _btn_col2:
        _stop_btn = st.button(
            "Stop", type="secondary", width="stretch",
            key="serial_stop_btn", disabled=not _is_streaming,
        )

    if _stop_btn and _is_streaming:
        st.session_state.get("serial_stop_event", _threading.Event()).set()
        _conn_log("Stream stopped by user", "warn")

    if _capture_btn and not _is_streaming:
        # Initialise shared mutable containers the thread writes into
        _stop_ev  = _threading.Event()
        _buf:      list  = []
        _raw_buf:  bytearray = bytearray()
        _log_buf:  list  = []

        st.session_state.update({
            "serial_streaming":  True,
            "serial_stop_event": _stop_ev,
            "_sbuf":             _buf,
            "_sraw":             _raw_buf,
            "_slog":             _log_buf,
            "_serror":           None,
            "_sdone":            False,
        })

        _capture_port    = _active_port
        _capture_baud    = _active_baud
        _capture_n       = int(_n_samples)
        _capture_timeout = float(_stream_timeout)

        def _stream_worker():
            try:
                for new_s, new_raw, new_log, _ in stream_binary_live(
                    _capture_port, _capture_baud, _capture_n,
                    stream_timeout_s=_capture_timeout,
                ):
                    if _stop_ev.is_set():
                        break
                    _buf.extend(new_s)
                    _raw_buf.extend(new_raw)
                    _log_buf.extend(new_log)
                    for ll in new_log:
                        if ll.startswith("ERROR:"):
                            st.session_state["_serror"] = ll[6:].strip()
                            _stop_ev.set()
                            break
            except Exception as exc:
                st.session_state["_serror"] = str(exc)
            finally:
                st.session_state["serial_streaming"] = False
                st.session_state["_sdone"] = True

        _t = _threading.Thread(target=_stream_worker, daemon=True)
        _t.start()
        _conn_log(f"Stream start: {_capture_n} samples from {_capture_port} "
                  f"({'live' if _live_mode else 'batch'})", "info")
        st.rerun()

    # ── Live / batch display fragment ─────────────────────────────────────────
    # run_every drives auto-refresh while streaming; None = render once

    _refresh_rate = 0.5 if _is_streaming and _live_mode else None

    @st.fragment(run_every=_refresh_rate)
    def _stream_display_fragment():
        import plotly.graph_objects as _go

        _streaming  = st.session_state.get("serial_streaming", False)
        _done       = st.session_state.get("_sdone", False)
        _error      = st.session_state.get("_serror")
        _buf        = st.session_state.get("_sbuf", [])
        _raw_buf    = st.session_state.get("_sraw", bytearray())
        _log_buf    = st.session_state.get("_slog", [])

        # ── Progress bar while active ─────────────────────────────────────────
        if _streaming or (_done and _buf):
            _requested = st.session_state.get("serial_n_samples", 1)
            _pct = min(int(len(_buf) / max(_requested, 1) * 100), 100)
            _label = (f"Receiving… {len(_buf)}/{_requested} samples"
                      if _streaming else
                      f"{'Stopped' if _error or st.session_state.get('serial_stop_event', _threading.Event()).is_set() else 'Complete'}"
                      f" — {len(_buf)} samples")
            st.progress(_pct, text=_label)

        # ── Error banner ──────────────────────────────────────────────────────
        if _error:
            st.error(f"Stream error: {_error}")

        # ── Chart — show live during streaming, or final result ───────────────
        _show_chart = _buf and (_streaming or _done)
        if _show_chart:
            _CH_COLORS  = {"Ch1 (ambient)": "#aaa", "Ch2 (ambient)": "#888",
                           "Ch3 (PPG)": "#1f77b4", "Ch4 (PPG)": "#ff7f0e"}
            _CH_VISIBLE = {"Ch1 (ambient)": "legendonly", "Ch2 (ambient)": "legendonly",
                           "Ch3 (PPG)": True, "Ch4 (PPG)": True}

            _ts_ms = [s[0] for s in _buf]
            _ch_arrays = {
                "Ch1 (ambient)": [s[1] for s in _buf],
                "Ch2 (ambient)": [s[2] for s in _buf],
                "Ch3 (PPG)":     [s[3] for s in _buf],
                "Ch4 (PPG)":     [s[4] for s in _buf],
            }
            _fig = _go.Figure()
            for _ch_name, _ch_vals in _ch_arrays.items():
                _fig.add_trace(_go.Scatter(
                    x=_ts_ms, y=_ch_vals, mode="lines", name=_ch_name,
                    line=dict(color=_CH_COLORS[_ch_name], width=1),
                    visible=_CH_VISIBLE[_ch_name],
                ))
            _fig.update_layout(
                xaxis_title="Time (ms from stream start)",
                yaxis_title="ADC value (uint32)",
                margin=dict(l=0, r=0, t=30, b=0),
                height=380,
                legend=dict(orientation="h", y=1.05),
                uirevision="stream",   # stable zoom/pan across redraws
            )
            st.plotly_chart(_fig, use_container_width=True)
            st.caption("Ch3/Ch4 = PPG (IN3 paired)  |  Ch1/Ch2 = ambient  |  toggle in legend")

        # ── Metrics + export — available as soon as there is ANY data ─────────
        if _buf:
            _ch3 = [s[3] for s in _buf]
            _ts_ms_b = [s[0] for s in _buf]
            _dur = (_ts_ms_b[-1] - _ts_ms_b[0]) / 1000 if len(_ts_ms_b) > 1 else 0

            _mc1, _mc2, _mc3, _mc4, _mc5 = st.columns(5)
            _mc1.metric("Samples",  len(_buf))
            _mc2.metric("Duration", f"{_dur:.2f} s")
            _mc3.metric("Ch3 mean", f"{int(sum(_ch3) / len(_ch3)):,}")
            _mc4.metric("Ch3 min",  f"{min(_ch3):,}")
            _mc5.metric("Ch3 max",  f"{max(_ch3):,}")

            _serial_df = pd.DataFrame({
                "timestamp_ms": _ts_ms_b,
                "ch1_ambient":  [s[1] for s in _buf],
                "ch2_ambient":  [s[2] for s in _buf],
                "ch3_ppg":      _ch3,
                "ch4_ppg":      [s[4] for s in _buf],
            })
            _raw_bytes_dl = bytes(_raw_buf)

            _dl1, _dl2 = st.columns(2)
            with _dl1:
                st.download_button(
                    "Export Parsed CSV",
                    _serial_df.to_csv(index=False).encode(),
                    "serial_capture.csv", "text/csv",
                    key="dl_serial_csv", width="stretch",
                )
            with _dl2:
                st.download_button(
                    "Export Raw Binary (.bin)",
                    _raw_bytes_dl,
                    "serial_capture.bin", "application/octet-stream",
                    key="dl_serial_bin", width="stretch",
                    help=f"{len(_raw_bytes_dl):,} bytes — 20 bytes/sample (timestamp_ms + ch1-4 uint32 LE)",
                )

        # ── Finalise to persistent keys once done so clear/reload works ───────
        if _done and _buf and not _streaming:
            st.session_state["serial_last_samples"]   = list(_buf)
            st.session_state["serial_last_raw_bytes"] = bytes(_raw_buf)
            st.session_state["serial_last_log"]       = list(_log_buf)
            if not _error:
                _conn_log(f"Stream complete: {len(_buf)} samples captured", "ok")
            # Trigger outer rerun to reset run_every to None (stop auto-refresh)
            st.rerun()

        # ── Stream log ────────────────────────────────────────────────────────
        if _log_buf:
            with st.expander("Stream log"):
                st.code("\n".join(_log_buf), language="text")

    _stream_display_fragment()

    # ── Previously captured data (after fragment, for persistent display) ─────

    _samples   = st.session_state.get("serial_last_samples", [])
    _raw_bytes = st.session_state.get("serial_last_raw_bytes", b"")
    _log       = st.session_state.get("serial_last_log", [])

    # Only show static display when NOT actively streaming
    if _samples and not _is_streaming and not st.session_state.get("_sdone"):
        import plotly.graph_objects as _go_s
        _ts_ms_s = [s[0] for s in _samples]
        _ch_arrays_s = {
            "Ch1 (ambient)": [s[1] for s in _samples],
            "Ch2 (ambient)": [s[2] for s in _samples],
            "Ch3 (PPG)":     [s[3] for s in _samples],
            "Ch4 (PPG)":     [s[4] for s in _samples],
        }
        _fig_s = _go_s.Figure()
        for _cn, _cv in _ch_arrays_s.items():
            _fig_s.add_trace(_go_s.Scatter(
                x=_ts_ms_s, y=_cv, mode="lines", name=_cn,
                line=dict(color={"Ch1 (ambient)":"#aaa","Ch2 (ambient)":"#888",
                                 "Ch3 (PPG)":"#1f77b4","Ch4 (PPG)":"#ff7f0e"}[_cn], width=1),
                visible={"Ch1 (ambient)":"legendonly","Ch2 (ambient)":"legendonly",
                         "Ch3 (PPG)":True,"Ch4 (PPG)":True}[_cn],
            ))
        _dur_s = (_ts_ms_s[-1] - _ts_ms_s[0]) / 1000 if len(_ts_ms_s) > 1 else 0
        _fig_s.update_layout(
            xaxis_title="Time (ms from stream start)", yaxis_title="ADC value (uint32)",
            margin=dict(l=0, r=0, t=30, b=0), height=380,
            legend=dict(orientation="h", y=1.05),
        )
        st.plotly_chart(_fig_s, width="stretch", key="chart_serial_static")
        st.caption("Ch3/Ch4 = PPG (IN3 paired)  |  Ch1/Ch2 = ambient  |  toggle in legend")

        _ch3_s = _ch_arrays_s["Ch3 (PPG)"]
        _sc1, _sc2, _sc3, _sc4, _sc5 = st.columns(5)
        _sc1.metric("Samples",  len(_samples))
        _sc2.metric("Duration", f"{_dur_s:.2f} s")
        _sc3.metric("Ch3 mean", f"{int(sum(_ch3_s)/len(_ch3_s)):,}")
        _sc4.metric("Ch3 min",  f"{min(_ch3_s):,}")
        _sc5.metric("Ch3 max",  f"{max(_ch3_s):,}")

        _sdf = pd.DataFrame({
            "timestamp_ms": _ts_ms_s,
            "ch1_ambient":  _ch_arrays_s["Ch1 (ambient)"],
            "ch2_ambient":  _ch_arrays_s["Ch2 (ambient)"],
            "ch3_ppg":      _ch3_s,
            "ch4_ppg":      _ch_arrays_s["Ch4 (PPG)"],
        })
        _dl1s, _dl2s = st.columns(2)
        with _dl1s:
            st.download_button("Export Parsed CSV", _sdf.to_csv(index=False).encode(),
                               "serial_capture.csv", "text/csv",
                               key="dl_serial_csv_s", width="stretch")
        with _dl2s:
            st.download_button("Export Raw Binary (.bin)", _raw_bytes,
                               "serial_capture.bin", "application/octet-stream",
                               key="dl_serial_bin_s", width="stretch",
                               help=f"{len(_raw_bytes):,} bytes — 20 bytes/sample")

    if _log and not _is_streaming and not st.session_state.get("_sdone"):
        with st.expander("Stream log"):
            st.code("\n".join(_log), language="text")

    if _samples and not _is_streaming and not st.session_state.get("_sdone"):
        if st.button("Clear Captured Data", key="serial_clear"):
            for _k in ("serial_last_samples", "serial_last_raw_bytes", "serial_last_log",
                       "_sbuf", "_sraw", "_slog", "_serror", "_sdone"):
                st.session_state.pop(_k, None)
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: all-in-one export (Analysis tab only)
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.divider()
    _dl_button("Export All Data", _ex(), "all_data.csv", "dl_all")
