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
    test_connection, send_command, receive_binary_stream,
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
                    _conn_log(f"Connected — {_port} @ {_baud} baud", "ok")
                else:
                    _conn_log(f"Connect failed: {_chk.error}", "error")
                st.rerun()
        else:
            if st.button("Disconnect", type="secondary", width="stretch", key="serial_disconnect_btn"):
                st.session_state["serial_connected"] = False
                _conn_log(f"Disconnected from {_active_port}", "info")
                st.rerun()

    with _sc4:
        if st.button("Refresh", width="stretch", key="serial_refresh_btn",
                     disabled=_is_connected):
            st.rerun()

    # ── Status badge ──────────────────────────────────────────────────────────

    if _is_connected:
        _port_descs = {p["device"]: p["description"] for p in describe_ports()}
        _desc = _port_descs.get(_active_port, _active_port)
        st.success(f"Connected — **{_active_port}** @ {_active_baud} baud  |  {_desc}")
    else:
        if _ports:
            _port_descs = {p["device"]: p["description"] for p in describe_ports()}
            if _port in _port_descs:
                st.caption(f"Device: {_port_descs[_port]}")
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
    st.caption("Send any text command to the device and see the response.")

    _cmd_col1, _cmd_col2 = st.columns([5, 1])
    with _cmd_col1:
        _cmd_input = st.text_input(
            "Command", key="serial_cmd_input",
            label_visibility="collapsed", placeholder="Enter command…",
        )
    with _cmd_col2:
        _send_clicked = st.button("Send", width="stretch", key="serial_send_btn")

    _resp_timeout = st.number_input("Response timeout (s)", min_value=0.5, max_value=30.0,
                                    value=3.0, step=0.5, key="serial_resp_timeout")

    if _send_clicked and _cmd_input.strip():
        with st.spinner(f"Sending: `{_cmd_input.strip()}`…"):
            _result = send_command(_active_port, _active_baud, _cmd_input.strip(),
                                   response_timeout_s=_resp_timeout)
        if _result.ok:
            _conn_log(f">> {_cmd_input.strip()}", "info")
            if _result.response:
                _conn_log(f"<< {_result.response[:120]}", "info")
            st.success("Command sent")
            if _result.response:
                st.code(_result.response, language="text")
            else:
                st.info("No response received within timeout.")
        else:
            _conn_log(f"Command error: {_result.error}", "error")
            st.error(f"Error: {_result.error}")

    # Command quick-pick
    with st.expander("Common Commands"):
        _common_cmds = [
            "adpd stream-bin 100",
            "adpd stream-bin 500",
            "adpd stream-bin 1000",
            "adpd start",
            "adpd stop",
            "adpd status",
            "help",
        ]
        for _cc in _common_cmds:
            if st.button(_cc, key=f"cmd_preset_{_cc}"):
                st.session_state["serial_cmd_input"] = _cc
                st.rerun()

    st.divider()

    # ── Binary stream capture ─────────────────────────────────────────────────

    st.subheader("Binary Stream Capture")
    st.caption("Sends `adpd stream-bin N` and parses the 4-byte little-endian uint32 payload.")

    _bs1, _bs2 = st.columns(2)
    with _bs1:
        _n_samples = st.number_input("Samples to capture", min_value=10, max_value=100000,
                                     value=500, step=50, key="serial_n_samples")
    with _bs2:
        _stream_timeout = st.number_input("Stream timeout (s)", min_value=5.0, max_value=120.0,
                                          value=30.0, step=5.0, key="serial_stream_timeout")

    _capture_btn = st.button("Capture Stream", type="primary", width="stretch",
                             key="serial_capture_btn")

    if _capture_btn:
        _progress_bar = st.progress(0, text="Waiting for start marker…")

        def _update_progress(received: int, total: int):
            pct = min(int(received / total * 100), 100)
            _progress_bar.progress(pct, text=f"Receiving… {received}/{total} samples")

        _conn_log(f"Stream start: {int(_n_samples)} samples from {_active_port}", "info")
        with st.spinner("Streaming…"):
            _stream = receive_binary_stream(
                _active_port, _active_baud, int(_n_samples),
                stream_timeout_s=_stream_timeout,
                progress_cb=_update_progress,
            )

        _progress_bar.empty()

        if _stream.ok and _stream.count > 0:
            _conn_log(f"Stream complete: {_stream.count} samples captured", "ok")
            st.session_state["serial_last_samples"] = _stream.samples
            st.session_state["serial_last_log"]     = _stream.log
        elif not _stream.ok:
            _conn_log(f"Stream error: {_stream.error}", "error")
            st.error(f"Stream error: {_stream.error}")
        else:
            _conn_log("Stream: no samples received", "warn")
            st.warning("No samples received.")
            if _stream.log:
                st.code("\n".join(_stream.log), language="text")

    # ── Display captured data ─────────────────────────────────────────────────

    _samples = st.session_state.get("serial_last_samples", [])
    _log     = st.session_state.get("serial_last_log", [])

    if _samples:
        import plotly.graph_objects as _go

        _fig_serial = _go.Figure()
        _fig_serial.add_trace(_go.Scatter(
            x=list(range(len(_samples))), y=_samples,
            mode="lines", line=dict(color="#1f77b4", width=1),
            name="raw ADC",
        ))
        _fig_serial.update_layout(
            xaxis_title="Sample index",
            yaxis_title="ADC value (uint32)",
            margin=dict(l=0, r=0, t=30, b=0),
            height=350,
        )
        st.plotly_chart(_fig_serial, width="stretch", key="chart_serial_raw")

        _s_col1, _s_col2, _s_col3, _s_col4 = st.columns(4)
        _s_col1.metric("Samples",  len(_samples))
        _s_col2.metric("Min",      f"{min(_samples):,}")
        _s_col3.metric("Max",      f"{max(_samples):,}")
        _s_col4.metric("Mean",     f"{int(sum(_samples) / len(_samples)):,}")

        _serial_df = pd.DataFrame({
            "sample_index": range(len(_samples)),
            "adc_raw": _samples,
        })
        st.download_button(
            "Export Captured CSV",
            _serial_df.to_csv(index=False).encode(),
            "serial_capture.csv", "text/csv",
            key="dl_serial",
        )

    if _log:
        with st.expander("Stream log"):
            st.code("\n".join(_log), language="text")

    if _samples and st.button("Clear Captured Data", key="serial_clear"):
        st.session_state.pop("serial_last_samples", None)
        st.session_state.pop("serial_last_log", None)
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: all-in-one export (Analysis tab only)
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.divider()
    _dl_button("Export All Data", _ex(), "all_data.csv", "dl_all")
