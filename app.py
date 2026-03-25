"""app.py — Streamlit UI for PPG Signal Filter & Analysis.

Thin UI layer: data loading, session state, @st.cache_data wrappers, and
section rendering. All signal processing lives in ppg_processing.py;
all chart builders live in ppg_charts.py.

Requires Python 3.10+  (neurokit2 ≥0.2.10 uses float | None, PEP 604).
"""
import matplotlib
matplotlib.use("Agg")

import threading
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
_LIVE_DISPLAY_S = 15.0   # rolling display window for streaming mode

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
    data_source_mode = st.radio(
        "Source",
        ["Demo files", "Upload file", "USB Serial Stream"],
        label_visibility="collapsed",
    )

    _live_stream_mode = data_source_mode == "USB Serial Stream"
    df_raw = None
    file_ext = ".csv"

    if data_source_mode == "Demo files":
        chosen_file = st.selectbox("Choose demo file", DEMO_FILES)
        file_path = os.path.join(DATA_DIR, chosen_file)
        file_ext = os.path.splitext(chosen_file)[1].lower()
        df_raw = load_data(file_path, file_ext)

    elif data_source_mode == "Upload file":
        uploaded = st.file_uploader("Upload CSV or XLSX", type=["csv", "xlsx", "xls"])
        if uploaded is not None:
            file_ext = os.path.splitext(uploaded.name)[1].lower()
            df_raw = load_data(uploaded, file_ext)
        else:
            st.info("Upload a file to continue.")
            st.stop()

    else:  # USB Serial Stream
        if not SERIAL_AVAILABLE:
            st.error("`pyserial` not installed — `pip install pyserial`")
            st.stop()

        _live_connected   = st.session_state.get("live_connected", False)
        _live_active_port = st.session_state.get("live_active_port", "")
        _live_active_baud = st.session_state.get("live_active_baud", 115200)
        _live_is_streaming = st.session_state.get("live_streaming", False)

        # ── Port / baud + Connect/Disconnect ─────────────────────────────
        _live_ports = list_serial_ports()
        _lock = _live_connected or _live_is_streaming

        if _live_ports:
            _live_port_default = _live_active_port if _live_active_port in _live_ports else _live_ports[0]
            _live_port_idx = _live_ports.index(_live_port_default)
            live_port = st.selectbox("Serial Port", _live_ports, index=_live_port_idx,
                                     key="live_port", disabled=_lock)
        else:
            live_port = st.text_input("Serial Port (manual)", _live_active_port or "/dev/tty.usbmodem101",
                                      key="live_port_manual", disabled=_lock)

        _baud_opts_live = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        _live_baud_idx = _baud_opts_live.index(_live_active_baud) if _live_active_baud in _baud_opts_live else 4
        live_baud = st.selectbox("Baud Rate", _baud_opts_live, index=_live_baud_idx,
                                  key="live_baud", disabled=_lock)

        _conn_col1, _conn_col2 = st.columns(2)
        with _conn_col1:
            if not _live_connected:
                if st.button("Connect", type="primary", width="stretch", key="live_conn_btn"):
                    _chk = test_connection(live_port, live_baud)
                    if _chk.ok:
                        st.session_state["live_connected"]   = True
                        st.session_state["live_active_port"] = live_port
                        st.session_state["live_active_baud"] = live_baud
                        st.session_state.pop("live_conn_error", None)
                    else:
                        st.session_state["live_conn_error"] = _chk.error or "Connection failed"
                    st.rerun()
            else:
                if st.button("Disconnect", type="secondary", width="stretch", key="live_disconn_btn"):
                    # Stop streaming first if active
                    if _live_is_streaming:
                        ev = st.session_state.get("live_stop_event")
                        if ev:
                            ev.set()
                    st.session_state["live_connected"]   = False
                    st.session_state["live_streaming"]   = False
                    st.session_state.pop("live_conn_error", None)
                    st.rerun()
        with _conn_col2:
            if st.button("Refresh", width="stretch", key="live_refresh_btn", disabled=_lock):
                st.rerun()

        # ── Connection status ─────────────────────────────────────────────
        _live_conn_err = st.session_state.get("live_conn_error", "")
        if _live_connected:
            _ldesc_map = {p["device"]: p["description"] for p in describe_ports()}
            _ldesc = _ldesc_map.get(_live_active_port, _live_active_port)
            st.success(f"**{_live_active_port}** @ {_live_active_baud} · {_ldesc}")
        elif _live_conn_err:
            st.error(_live_conn_err)
        else:
            st.caption("Not connected.")

        if not _live_connected:
            st.stop()

        st.divider()

        # ── Stream configuration (only when connected) ────────────────────
        _ODR_OPTIONS = [10, 25, 50, 100, 200, 400]
        live_odr = st.select_slider(
            "ODR (Hz)", options=_ODR_OPTIONS, value=100, key="live_odr",
            help="Sends `adpd ppg freq <hz>` before starting stream. Supported: 10/25/50/100/200/400 Hz",
            disabled=_live_is_streaming,
        )
        live_channel = st.selectbox(
            "PPG Channel", ["ch1", "ch2", "ch3", "ch4"], index=2, key="live_channel",
            help="Ch3/Ch4 = PPG (IN3 paired), Ch1/Ch2 = ambient",
            disabled=_live_is_streaming,
        )
        live_n_samples = int(st.number_input(
            "Samples to request", min_value=100, max_value=100_000,
            value=10_000, step=500, key="live_n_samples",
            help="Large value = continuous stream; hit Stop to end early",
            disabled=_live_is_streaming,
        ))
        live_analysis_window_s = int(st.slider(
            "Analysis window (s)", min_value=3, max_value=10, value=5,
            key="live_analysis_window_s",
            help=f"Seconds of data fed to NeuroKit2 pipeline (display shows last {int(_LIVE_DISPLAY_S)} s)",
        ))

        # ── Start / Stop ──────────────────────────────────────────────────
        _lc1, _lc2 = st.columns(2)
        with _lc1:
            if st.button("▶ Start", disabled=_live_is_streaming, type="primary",
                         width="stretch", key="live_start_btn"):
                # Send ODR command first (best-effort — ignore errors)
                send_command(_live_active_port, _live_active_baud,
                             f"adpd ppg freq {live_odr}", response_timeout_s=2.0)

                _live_stop_ev = threading.Event()
                _live_shared: dict = {
                    "buf":   [],
                    "raw":   bytearray(),
                    "log":   [],
                    "error": None,
                    "done":  False,
                }
                st.session_state["live_streaming"]   = True
                st.session_state["live_stop_event"]  = _live_stop_ev
                st.session_state["_sshared_live"]    = _live_shared
                st.session_state["_live_finalised"]  = False
                st.session_state.pop("_live_computed_sr", None)

                _lport   = _live_active_port
                _lbaud   = _live_active_baud
                _lnsampl = live_n_samples

                def _live_worker():
                    try:
                        for new_s, new_raw, new_log, is_final in stream_binary_live(
                            _lport, _lbaud, _lnsampl,
                        ):
                            if _live_stop_ev.is_set():
                                break
                            _live_shared["buf"].extend(new_s)
                            _live_shared["raw"].extend(new_raw)
                            _live_shared["log"].extend(new_log)
                            for ll in new_log:
                                if ll.startswith("ERROR:"):
                                    _live_shared["error"] = ll[6:].strip()
                                    _live_stop_ev.set()
                                    break
                            if is_final or _live_stop_ev.is_set():
                                break
                    except Exception as exc:
                        _live_shared["error"] = str(exc)
                    finally:
                        _live_shared["done"] = True

                t = threading.Thread(target=_live_worker, daemon=True)
                t.start()
                st.rerun()

        with _lc2:
            if st.button("■ Stop", disabled=not _live_is_streaming,
                         width="stretch", key="live_stop_btn"):
                ev = st.session_state.get("live_stop_event")
                if ev:
                    ev.set()

        # ── Stream status ─────────────────────────────────────────────────
        _lshared_disp = st.session_state.get("_sshared_live", {})
        _lbuf_disp    = _lshared_disp.get("buf", [])
        _lerr_disp    = _lshared_disp.get("error")
        if _live_is_streaming:
            st.info(f"Streaming — {len(_lbuf_disp):,} samples @ {live_odr} Hz")
        elif _lerr_disp:
            st.error(f"Stream error: {_lerr_disp}")
        elif _lbuf_disp:
            st.success(f"Done — {len(_lbuf_disp):,} samples")
        else:
            st.caption("Press ▶ Start to begin streaming.")

    # ── Signal column (file modes only) ───────────────────────────────────────

    if not _live_stream_mode:
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
    else:
        signal_col = st.session_state.get("live_channel", "ch3")
        ts_col     = "timestamp_ms"

    # ── Signal Transform ──────────────────────────────────────────────────────
    signal_transform = st.radio(
        "Signal transform",
        ["None", "Invert (2^x − raw)", "Flip AC (2×mean − signal)"],
        help="None: use as-is  |  Invert: hardware ADC inversion  |  Flip AC: flip waveform polarity, preserve DC",
    )
    invert_signal = signal_transform == "Invert (2^x − raw)"
    flip_ac       = signal_transform == "Flip AC (2×mean − signal)"

    adc_bits         = 24
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

    if not _live_stream_mode:
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
    else:
        # Live SR: computed inside fragment, stored in session state
        _live_sr_val = st.session_state.get("_live_computed_sr", 0.0)
        st.metric("Live SR", f"{_live_sr_val:.1f} Hz" if _live_sr_val > 0 else "— Hz")
        override_sr = st.toggle("Override SR", key="live_override_sr")
        if override_sr:
            sampling_rate = st.number_input("Manual SR (Hz)", min_value=1.0, max_value=10000.0,
                                            value=float(round(_live_sr_val or 100.0, 1)), step=0.5,
                                            key="live_manual_sr")
        else:
            sampling_rate = _live_sr_val if _live_sr_val > 0 else 100.0
        # dummy _t0/_t1 for streaming (real values computed inside fragment)
        timestamps_ms = np.array([], dtype=np.float64)
        signal        = np.array([], dtype=np.float64)
        _t0 = 0.0
        _t1 = _LIVE_DISPLAY_S * 1000

    st.divider()
    show_nk_plot = st.checkbox("Show NeuroKit2 native plot (matplotlib)")

    if not _live_stream_mode:
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
quality_method = quality_methods[0]

transform_mode = "invert" if invert_signal else ("flip_ac" if flip_ac else "none")
_signal_transformed = transform_mode != "none"

if not _live_stream_mode:
    # ── Apply window mask + signal transform ──────────────────────────────────
    win_ms = st.session_state.get("analysis_window", (_t0, _t1))

    mask = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
    timestamps_w  = timestamps_ms[mask]
    signal_w_orig = signal[mask]

    signal_w, _flip_baseline = apply_signal_transform(
        signal_w_orig,
        mode=transform_mode,
        adc_bits=adc_bits,
        flip_sliding=flip_ac_sliding,
        flip_window_s=flip_ac_window_s,
        sampling_rate=sampling_rate,
    )

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

    # ── Auto beat windows (update on peak change) ────────────────────────────
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

else:
    # Streaming mode: pipeline runs inside the analysis fragment
    timestamps_w = signal_w = signal_w_orig = np.array([], dtype=np.float64)
    _flip_baseline = None
    signal_bytes   = b""
    cleaned = np.array([], dtype=np.float64)
    signals_df = pd.DataFrame()
    info = {"PPG_Peaks": np.array([], dtype=int)}
    quality = None
    analysis = None
    peak_indices = np.array([], dtype=int)
    hr_mean = hr_min = hr_max = None

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

    # ── File mode: Time Window slider ─────────────────────────────────────────

    if not _live_stream_mode:
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

    # ── Analysis display (fragment for streaming auto-refresh) ────────────────

    _live_refresh = 0.5 if st.session_state.get("live_streaming") and _live_stream_mode else None

    @st.fragment(run_every=_live_refresh)
    def _analysis_display():
        # ── Streaming mode: compute fresh from buffer ──────────────────────
        if _live_stream_mode:
            _lshared   = st.session_state.get("_sshared_live", {})
            _lbuf      = _lshared.get("buf", [])
            _ldone     = _lshared.get("done", False)
            _lstreaming = st.session_state.get("live_streaming", False)
            _lerror    = _lshared.get("error")

            if _ldone and _lstreaming:
                st.session_state["live_streaming"] = False
                _lstreaming = False

            if _lerror:
                st.error(f"Stream error: {_lerror}")

            if not _lbuf:
                if _lstreaming:
                    st.info("Streaming… waiting for first samples.")
                else:
                    st.info("No stream data yet. Configure connection in sidebar and press ▶ Start.")
                if _ldone and not st.session_state.get("_live_finalised"):
                    st.session_state["_live_finalised"] = True
                    st.rerun()
                return

            # Build full arrays from buffer
            _lall_ts  = np.array([s[0] for s in _lbuf], dtype=np.float64)
            _lch_key  = st.session_state.get("live_channel", "ch3")
            _lch_idx  = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}[_lch_key]
            _lall_sig = np.array([s[_lch_idx] for s in _lbuf], dtype=np.float64)

            # Rolling SR from last 200 timestamps
            _lsr_win = min(len(_lall_ts), 200)
            if _lsr_win >= 2:
                _ldiffs = np.diff(_lall_ts[-_lsr_win:])
                _lpos   = _ldiffs[_ldiffs > 0]
                _lmed   = float(np.median(_lpos)) if len(_lpos) else 10.0
                _lsr    = 1000.0 / _lmed
            else:
                _lsr = 100.0

            # Apply SR override if set
            if st.session_state.get("live_override_sr"):
                _lsr = float(st.session_state.get("live_manual_sr", _lsr))

            # Store for sidebar metric
            st.session_state["_live_computed_sr"] = _lsr
            _lsampling_rate = _lsr

            # Display window: last 15 s
            _lkeep_n  = max(10, int(_LIVE_DISPLAY_S * _lsampling_rate))
            _ldisp_ts  = _lall_ts[-_lkeep_n:]
            _ldisp_sig = _lall_sig[-_lkeep_n:]

            # Analysis window: trailing N seconds
            _lanalysis_s = float(st.session_state.get("live_analysis_window_s", 5))
            _lanal_n = max(10, int(_lanalysis_s * _lsampling_rate))
            _ltimestamps_w  = _ldisp_ts[-_lanal_n:]
            _lsignal_w_orig = _ldisp_sig[-_lanal_n:]

            # Apply signal transform
            _lsignal_w, _lflip_baseline = apply_signal_transform(
                _lsignal_w_orig,
                mode=transform_mode,
                adc_bits=adc_bits,
                flip_sliding=flip_ac_sliding,
                flip_window_s=flip_ac_window_s,
                sampling_rate=_lsampling_rate,
            )

            if len(_lsignal_w) < 10:
                st.info(f"Collecting data… {len(_lbuf):,} samples so far (need ≥{_lanal_n} for {_lanalysis_s}s window)")
                return

            # Run pipeline (no cache — rolling buffer changes each refresh)
            try:
                _lresults = run_pipeline(
                    _lsignal_w, _lsampling_rate, clean_method, peak_method, quality_method,
                )
            except Exception as _le:
                st.error(f"Pipeline error: {_le}")
                return

            _lcleaned     = _lresults["cleaned"]
            _lsignals_df  = _lresults["signals_df"]
            _linfo        = _lresults["info"]
            _lquality     = _lresults["quality"]
            _lanalysis    = _lresults["analysis"]
            _lpeak_idx    = _linfo.get("PPG_Peaks", np.array([], dtype=int))
            _lhr_m, _lhr_lo, _lhr_hi = compute_hr_metrics(_lpeak_idx, _lsampling_rate)

            # Streaming status banner
            if _lstreaming:
                st.info(f"Streaming — {len(_lbuf):,} samples received · {_lsampling_rate:.1f} Hz live SR")

            # Unpack into generic names for shared rendering below
            _a_ts        = _ltimestamps_w
            _a_sig_w     = _lsignal_w
            _a_sig_orig  = _lsignal_w_orig
            _a_flip_bl   = _lflip_baseline
            _a_sr        = _lsampling_rate
            _a_cleaned   = _lcleaned
            _a_sig_df    = _lsignals_df
            _a_info      = _linfo
            _a_quality   = _lquality
            _a_analysis  = _lanalysis
            _a_peaks     = _lpeak_idx
            _a_hr_m      = _lhr_m
            _a_hr_lo     = _lhr_lo
            _a_hr_hi     = _lhr_hi
            _a_sig_col   = _lch_key
            _a_t0        = float(_ltimestamps_w[0])
            _a_t1        = float(_ltimestamps_w[-1])
            _a_nrows     = len(_lbuf)
            _a_ncols     = 5
            _a_df_raw    = None
            _a_sig_bytes = _lsignal_w.tobytes()

            # Finalise when done
            if _ldone and not _lstreaming and not st.session_state.get("_live_finalised"):
                st.session_state["_live_finalised"] = True
                st.rerun()

        else:
            # File mode: use outer-scope variables
            _a_ts        = timestamps_w
            _a_sig_w     = signal_w
            _a_sig_orig  = signal_w_orig
            _a_flip_bl   = _flip_baseline
            _a_sr        = sampling_rate
            _a_cleaned   = cleaned
            _a_sig_df    = signals_df
            _a_info      = info
            _a_quality   = quality
            _a_analysis  = analysis
            _a_peaks     = peak_indices
            _a_hr_m      = hr_mean
            _a_hr_lo     = hr_min
            _a_hr_hi     = hr_max
            _a_sig_col   = signal_col
            _a_t0        = _t0
            _a_t1        = _t1
            _a_nrows     = len(df_raw)
            _a_ncols     = len(df_raw.columns)
            _a_df_raw    = df_raw
            _a_sig_bytes = signal_bytes

        # ── Shared convenience helpers ─────────────────────────────────────
        _c = _a_sig_col.replace("-", "_")

        def _ex_shared():
            """Build export DataFrame — works for both file and streaming mode."""
            n   = len(_a_ts)
            df  = pd.DataFrame({
                "timestamp_ms":    _a_ts,
                f"{_c}_raw":       _a_sig_orig,
                f"{_c}_cleaned":   _a_cleaned,
                "PPG_Peak":        _a_sig_df["PPG_Peaks"].values.astype(int)
                                   if not _a_sig_df.empty else np.zeros(n, dtype=int),
                "PPG_Rate_bpm":    _a_sig_df["PPG_Rate"].values
                                   if "PPG_Rate" in _a_sig_df.columns
                                   else np.full(n, np.nan),
            })
            if not _live_stream_mode:
                for _qm in quality_methods:
                    _qr = cached_pipeline(_a_sig_bytes, _a_sr, clean_method, peak_method, _qm)
                    _q  = _qr["quality"]
                    if _q is not None:
                        _qa = np.array(_q)
                        mn  = min(n, len(_qa))
                        df[f"quality_{_qm}"] = np.concatenate([_qa[:mn], np.full(n - mn, np.nan)])
                    else:
                        df[f"quality_{_qm}"] = np.nan
            return df

        def _dl(label, df, filename, key):
            st.download_button(label, df.to_csv(index=False).encode(),
                               filename, "text/csv", key=key, width="stretch")

        # ── Section 1: Raw Data ───────────────────────────────────────────

        st.markdown('<div class="section-anchor" id="raw-data"></div>', unsafe_allow_html=True)
        st.header("Raw Data")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Rows",              _a_nrows)
        col2.metric("Columns",           _a_ncols)
        col3.metric("Duration (window)", f"{(_a_ts[-1] - _a_ts[0]) / 1000:.1f} s")
        col4.metric("SR",                f"{_a_sr:.1f} Hz")

        st.caption("Box-select a region to zoom all charts")
        ev_raw = st.plotly_chart(
            plot_raw_signal(_a_ts, _a_sig_w, _a_sig_col,
                            original=_a_sig_orig if _signal_transformed else None,
                            baseline=_a_flip_bl),
            width="stretch", key="chart_raw", on_select="rerun", selection_mode="box",
        )
        _b = _extract_box_x(ev_raw)
        if _b and not _live_stream_mode:
            st.session_state._pending_window = (max(_a_t0, min(_b)), min(_a_t1, max(_b)))
            st.rerun()

        st.subheader("Signal Statistics")
        st.dataframe(pd.Series(_a_sig_w, name=_a_sig_col).describe().to_frame().T, width="stretch")

        if _a_df_raw is not None:
            with st.expander("Data Preview (full file, head 200)"):
                st.dataframe(_a_df_raw.head(200), width="stretch")
        elif _live_stream_mode:
            _lshared_p = st.session_state.get("_sshared_live", {})
            _lbuf_p    = _lshared_p.get("buf", [])
            if _lbuf_p:
                with st.expander(f"Stream buffer preview (last 200 of {len(_lbuf_p):,} samples)"):
                    _preview_df = pd.DataFrame(_lbuf_p[-200:],
                                               columns=["timestamp_ms","ch1","ch2","ch3","ch4"])
                    st.dataframe(_preview_df, width="stretch")

        _dl("Export Raw CSV", _ex_shared()[["timestamp_ms", f"{_c}_raw"]], "raw_signal.csv", "dl_raw")

        st.divider()

        # ── Section 2: Processed Signal ───────────────────────────────────

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
            plot_cleaned_overlay(_a_ts, _a_sig_w if show_raw_overlay else None, _a_cleaned, _a_sig_col),
            width="stretch", key="chart_proc", on_select="rerun", selection_mode="box",
        )
        _b = _extract_box_x(ev_proc)
        if _b and not _live_stream_mode:
            st.session_state._pending_window = (max(_a_t0, min(_b)), min(_a_t1, max(_b)))
            st.rerun()
        st.caption(f"Cleaning method: **{clean_method}** | SR: **{_a_sr:.1f} Hz**")

        _dl("Export Processed CSV",
            _ex_shared()[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned"]],
            "processed_signal.csv", "dl_proc")

        st.divider()

        # ── Section 3: Peak Detection ─────────────────────────────────────

        st.markdown('<div class="section-anchor" id="peak-detection"></div>', unsafe_allow_html=True)
        st.header("Peak Detection")

        st.caption("Box-select a region to zoom all charts")
        ev_peaks = st.plotly_chart(plot_peaks(_a_ts, _a_cleaned, _a_peaks),
                                   width="stretch", key="chart_peaks",
                                   on_select="rerun", selection_mode="box")
        _b = _extract_box_x(ev_peaks)
        if _b and not _live_stream_mode:
            st.session_state._pending_window = (max(_a_t0, min(_b)), min(_a_t1, max(_b)))
            st.rerun()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Peaks Detected", len(_a_peaks))
        if _a_hr_m is not None:
            m2.metric("Mean HR", f"{_a_hr_m:.1f} bpm")
            m3.metric("Min HR",  f"{_a_hr_lo:.1f} bpm")
            m4.metric("Max HR",  f"{_a_hr_hi:.1f} bpm")

        st.caption(f"Peak method: **{peak_method}** | Cleaning: **{clean_method}**")

        _dl("Export Peaks CSV",
            _ex_shared()[["timestamp_ms", f"{_c}_raw", f"{_c}_cleaned", "PPG_Peak", "PPG_Rate_bpm"]],
            "peak_detection.csv", "dl_peaks")

        if show_nk_plot:
            with st.expander("NeuroKit2 Native Plot", expanded=True):
                try:
                    fig_nk = nk.ppg_plot(_a_sig_df, _a_info)
                    st.pyplot(fig_nk)
                    plt.close(fig_nk)
                except Exception as e:
                    st.warning(f"Could not render NeuroKit2 native plot: {e}")

        st.divider()

        # ── Section 4: Individual Beats ───────────────────────────────────

        st.markdown('<div class="section-anchor" id="individual-beats"></div>', unsafe_allow_html=True)
        st.header("Individual Beats")

        beat_pre  = st.session_state.get("beat_pre",  0.2)
        beat_post = st.session_state.get("beat_post", 0.5)

        if len(_a_peaks) < 2:
            st.warning("Not enough peaks detected to segment individual beats.")
        else:
            try:
                epochs = cached_epochs(
                    _a_cleaned.tobytes(), _a_peaks.astype(np.int64).tobytes(),
                    _a_sr, -beat_pre, beat_post,
                )
                hr_mean_beats, _, _ = compute_hr_metrics(_a_peaks, _a_sr)
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

        # ── Section 5: HRV / Analysis ─────────────────────────────────────

        st.markdown('<div class="section-anchor" id="hrv-analysis"></div>', unsafe_allow_html=True)
        st.header("HRV / Analysis")

        if _a_analysis is not None:
            st.dataframe(_a_analysis, width="stretch")
            csv_buf = io.BytesIO()
            _a_analysis.to_csv(csv_buf, index=False)
            st.download_button("Download Analysis CSV", csv_buf.getvalue(),
                               "ppg_analysis.csv", "text/csv")
        else:
            st.info("Window too short for full HRV analysis — widen the time window.")
            if not _a_sig_df.empty:
                st.dataframe(_a_sig_df.head(500), width="stretch")

        st.divider()

        # ── Section 6: Signal Quality ─────────────────────────────────────

        st.markdown('<div class="section-anchor" id="signal-quality"></div>', unsafe_allow_html=True)
        st.header("Signal Quality")

        st.segmented_control(
            "Quality Methods", QUALITY_METHODS,
            selection_mode="multi", default=[QUALITY_METHODS[0]], key="quality_methods",
        )

        _quality_map    = {}
        _quality_errors = {}
        for _qm in quality_methods:
            if _live_stream_mode:
                # Direct call — no cache for streaming
                try:
                    _qres = run_pipeline(_a_sig_w, _a_sr, clean_method, peak_method, _qm)
                    if _qres["quality"] is not None:
                        _qa = np.array(_qres["quality"])
                        _minlen = min(len(_a_ts), len(_qa))
                        _quality_map[_qm] = _qa[:_minlen]
                    elif _qres["quality_error"]:
                        _quality_errors[_qm] = _qres["quality_error"]
                    else:
                        _quality_errors[_qm] = "not available"
                except Exception as _qe:
                    _quality_errors[_qm] = str(_qe)
            else:
                _qres = cached_pipeline(_a_sig_bytes, _a_sr, clean_method, peak_method, _qm)
                if _qres["quality"] is not None:
                    _qa = np.array(_qres["quality"])
                    _minlen = min(len(_a_ts), len(_qa))
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
                plot_quality(_a_ts[:_common_len], _aligned_map),
                width="stretch", key=f"chart_qual_{'_'.join(quality_methods)}",
                on_select="rerun", selection_mode="box",
            )
            _b = _extract_box_x(ev_qual)
            if _b and not _live_stream_mode:
                st.session_state._pending_window = (max(_a_t0, min(_b)), min(_a_t1, max(_b)))
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

        _dl("Export Signal Quality CSV", _ex_shared(), "signal_quality.csv", "dl_qual")

        # ── Streaming mode: full buffer export ────────────────────────────
        if _live_stream_mode:
            _lshared_ex = st.session_state.get("_sshared_live", {})
            _lbuf_ex    = _lshared_ex.get("buf", [])
            _lraw_ex    = _lshared_ex.get("raw", b"")
            if _lbuf_ex:
                st.divider()
                st.subheader("Full Stream Export")
                _fdf = pd.DataFrame(_lbuf_ex, columns=["timestamp_ms","ch1","ch2","ch3","ch4"])
                _edl1, _edl2 = st.columns(2)
                with _edl1:
                    st.download_button(
                        "Export full stream CSV",
                        _fdf.to_csv(index=False).encode(),
                        "stream_full.csv", "text/csv",
                        key="dl_live_csv_full", width="stretch",
                    )
                with _edl2:
                    st.download_button(
                        "Export raw binary (.bin)",
                        bytes(_lraw_ex),
                        "stream_full.bin", "application/octet-stream",
                        key="dl_live_bin_full", width="stretch",
                        help=f"{len(_lraw_ex):,} bytes — 20 bytes/sample",
                    )
                if st.button("Clear stream data", key="live_clear_btn"):
                    for _k in ("_sshared_live", "_live_finalised", "_live_computed_sr"):
                        st.session_state.pop(_k, None)
                    st.rerun()

    _analysis_display()

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

    # ── Command tree ──────────────────────────────────────────────────────────
    # Each leaf is:
    #   None              → terminal (send now)
    #   {"_optional_end"} → terminal OR continue with listed keys
    #   {"_input": type}  → value widget; _next continues after value is added
    # Input types: "hex" (text), "count" (number), "odr" (select_slider)
    _CMD_FLOW = {
        "help":  None,
        "reset": None,
        "scan": {
            "i2c": None,
            "spi": None,
        },
        "interface": {
            "usb":  {"on": None, "off": None},
            "uart": {"on": None, "off": None},
        },
        "eeprom": {
            "info": None,
            "test": None,
            "read":  {"_input": "hex",   "_label": "address  e.g. 0x0100"},
            "write": {"_input": "hex",   "_label": "address  e.g. 0x0100",
                      "_next":  {"_input": "hex", "_label": "value  e.g. 0xFF"}},
        },
        "adpd": {
            "probe": {"_optional_end": True, "sdk": None},
            "dump":  None,
            "diag":  None,
            "read":  {"_input": "hex",   "_label": "register  e.g. 0x128"},
            "write": {"_input": "hex",   "_label": "register  e.g. 0x128",
                      "_next":  {"_input": "hex", "_label": "value  e.g. 0x000A"}},
            "ppg": {
                "start":      None,
                "stop":       None,
                "freq":       {"_input": "odr",   "_label": "ODR",
                               "_options": [10, 25, 50, 100, 200, 400]},
                "stream":     {"_input": "count", "_label": "sample count"},
                "stream-bin": {"_input": "count", "_label": "sample count"},
            },
        },
    }

    def _flow_navigate(tokens):
        """Return the tree node reached after applying tokens."""
        node = _CMD_FLOW
        for t in tokens:
            if node is None:
                return None
            if isinstance(node, dict):
                if "_input" in node:
                    # t is the entered value; advance to _next (may be None)
                    node = node.get("_next")
                elif t in node:
                    node = node[t]
                else:
                    return None   # unknown token
            else:
                return None
        return node

    # ── Session state ─────────────────────────────────────────────────────────
    _tokens  = st.session_state.setdefault("_cmd_tokens", [])
    _node    = _flow_navigate(_tokens)
    _cmd_str = " ".join(str(t) for t in _tokens)
    _can_send = _node is None or (isinstance(_node, dict) and _node.get("_optional_end"))

    # ── Chip strip + backspace/clear ──────────────────────────────────────────
    _chip_css = (
        "display:inline-flex; align-items:center; padding:2px 10px; margin:2px 3px; "
        "border-radius:14px; background:#1a3550; color:#7ec8e3; "
        "font-size:0.82rem; font-family:monospace; font-weight:600;"
    )
    if _tokens:
        _chips_html = "".join(f'<span style="{_chip_css}">{t}</span>' for t in _tokens)
        st.markdown(
            f'<div style="margin:4px 0 6px 0; line-height:2.2">{_chips_html}</div>',
            unsafe_allow_html=True,
        )
        _bk_col, _rst_col, _ = st.columns([1, 1, 6])
        with _bk_col:
            if st.button("⌫", key="cmd_backspace", help="Remove last token", width="stretch"):
                st.session_state["_cmd_tokens"].pop()
                st.session_state["_cmd_next_select"] = None
                st.rerun()
        with _rst_col:
            if st.button("✕", key="cmd_reset_chips", help="Clear all", width="stretch"):
                st.session_state["_cmd_tokens"] = []
                st.session_state["_cmd_next_select"] = None
                st.rerun()

    # ── Single persistent search bar (dict nodes) ─────────────────────────────
    if isinstance(_node, dict) and "_input" not in _node:
        _choices = [k for k in _node if not k.startswith("_")]
        if _choices:
            def _on_token_select():
                val = st.session_state.get("_cmd_next_select")
                if val is not None:
                    st.session_state["_cmd_tokens"].append(val)
                    st.session_state["_cmd_next_select"] = None

            st.selectbox(
                "token",
                options=_choices,
                index=None,
                placeholder="type to search…",
                key="_cmd_next_select",
                on_change=_on_token_select,
                label_visibility="collapsed",
            )

    # ── Value input (number / hex / odr) ──────────────────────────────────────
    elif isinstance(_node, dict) and "_input" in _node:
        _itype  = _node["_input"]
        _ilabel = _node.get("_label", "value")
        _iopts  = _node.get("_options")

        _iv_col, _iadd_col = st.columns([5, 1])
        with _iv_col:
            if _itype == "odr":
                _ival = st.selectbox(
                    _ilabel, options=_iopts, index=None,
                    placeholder="select ODR (Hz)…",
                    key=f"_cmd_ival_{len(_tokens)}",
                    label_visibility="collapsed",
                )
            elif _itype == "count":
                _ival = st.number_input(
                    _ilabel, min_value=1, max_value=100_000, value=None, step=50,
                    key=f"_cmd_ival_{len(_tokens)}",
                    placeholder=_ilabel, label_visibility="collapsed",
                )
            else:   # hex / free text
                _ival = st.text_input(
                    _ilabel, key=f"_cmd_ival_{len(_tokens)}",
                    placeholder=_ilabel, label_visibility="collapsed",
                )
        with _iadd_col:
            _ival_ok = _ival is not None and str(_ival).strip() != ""
            if st.button("Add →", key="cmd_add_val", type="primary",
                         width="stretch", disabled=not _ival_ok):
                st.session_state["_cmd_tokens"].append(str(_ival).strip())
                st.rerun()

    # ── Send ──────────────────────────────────────────────────────────────────
    if _tokens and _can_send:
        if st.button(f"Send ↵  `{_cmd_str}`", type="primary",
                     width="stretch", key="cmd_send_final"):
            _resp_timeout = st.session_state.get("serial_resp_timeout", 3.0)
            with st.spinner(f"Sending…"):
                _result = send_command(_active_port, _active_baud, _cmd_str,
                                       response_timeout_s=_resp_timeout)
            st.session_state["_cmd_tokens"] = []
            st.session_state["_cmd_next_select"] = None
            if _result.ok:
                _conn_log(f">> {_cmd_str}", "info")
                if _result.response:
                    _conn_log(f"<< {_result.response[:120]}", "info")
                    st.code(_result.response, language="text")
                else:
                    st.info("No response received within timeout.")
            else:
                _conn_log(f"Command error: {_result.error}", "error")
                st.error(f"Error: {_result.error}")

    with st.expander("Response timeout", expanded=False):
        st.number_input("Timeout (s)", min_value=0.5, max_value=30.0,
                        value=3.0, step=0.5, key="serial_resp_timeout")

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
        st.session_state.get("serial_stop_event", threading.Event()).set()
        _conn_log("Stream stopped by user", "warn")

    if _capture_btn and not _is_streaming:
        # All shared state lives in a plain dict — thread never touches st.session_state
        _stop_ev = threading.Event()
        _shared: dict = {
            "buf":      [],
            "raw":      bytearray(),
            "log":      [],
            "error":    None,
            "done":     False,
        }

        st.session_state["serial_streaming"]  = True
        st.session_state["serial_stop_event"] = _stop_ev
        st.session_state["_sshared"]          = _shared
        st.session_state["_sfinalised"]       = False

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
                    _shared["buf"].extend(new_s)
                    _shared["raw"].extend(new_raw)
                    _shared["log"].extend(new_log)
                    for ll in new_log:
                        if ll.startswith("ERROR:"):
                            _shared["error"] = ll[6:].strip()
                            _stop_ev.set()
                            break
            except Exception as exc:
                _shared["error"] = str(exc)
            finally:
                _shared["done"] = True   # fragment detects this and flips serial_streaming

        _t = threading.Thread(target=_stream_worker, daemon=True)
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

        _shared     = st.session_state.get("_sshared", {})
        _streaming  = st.session_state.get("serial_streaming", False)
        _buf        = _shared.get("buf", [])
        _raw_buf    = _shared.get("raw", bytearray())
        _log_buf    = _shared.get("log", [])
        _error      = _shared.get("error")
        _done       = _shared.get("done", False)

        # Fragment is the only place that flips serial_streaming off.
        # _sfinalised guards against running the finalise block twice.
        if _done and _streaming:
            st.session_state["serial_streaming"] = False
            _streaming = False

        # ── Progress bar while active ─────────────────────────────────────
        if _streaming or (_done and _buf):
            _requested = st.session_state.get("serial_n_samples", 1)
            _pct = min(int(len(_buf) / max(_requested, 1) * 100), 100)
            _stopped = _error or st.session_state.get("serial_stop_event", threading.Event()).is_set()
            _label = (f"Receiving… {len(_buf)}/{_requested} samples"
                      if _streaming else
                      f"{'Stopped' if _stopped else 'Complete'} — {len(_buf)} samples")
            st.progress(_pct, text=_label)

        # ── Error banner ──────────────────────────────────────────────────
        if _error:
            st.error(f"Stream error: {_error}")

        # ── Chart — show live during streaming, or final result ───────────
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
            st.plotly_chart(_fig, width="stretch")
            st.caption("Ch3/Ch4 = PPG (IN3 paired)  |  Ch1/Ch2 = ambient  |  toggle in legend")

        # ── Metrics + export — available as soon as there is ANY data ─────
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

        # ── Finalise to persistent keys once done so clear/reload works ───
        if _done and _buf and not _streaming and not st.session_state.get("_sfinalised"):
            st.session_state["serial_last_samples"]   = list(_buf)
            st.session_state["serial_last_raw_bytes"] = bytes(_raw_buf)
            st.session_state["serial_last_log"]       = list(_log_buf)
            _msg = f"Stream complete: {len(_buf)} samples"
            if _error:
                _conn_log(f"Stream ended with error: {_error}", "error")
            elif st.session_state.get("serial_stop_event", threading.Event()).is_set():
                _conn_log(f"Stream stopped by user: {len(_buf)} samples kept", "warn")
            else:
                _conn_log(_msg, "ok")
            st.session_state["_sfinalised"] = True
            # Outer rerun resets run_every → None (stops auto-refresh)
            st.rerun()

        # ── Stream log ────────────────────────────────────────────────────
        if _log_buf:
            with st.expander("Stream log"):
                st.code("\n".join(_log_buf), language="text")

    _stream_display_fragment()

    # ── Previously captured data (after fragment, for persistent display) ─────

    _samples   = st.session_state.get("serial_last_samples", [])
    _raw_bytes = st.session_state.get("serial_last_raw_bytes", b"")
    _log       = st.session_state.get("serial_last_log", [])

    # Only show static display when NOT actively streaming
    if _samples and not _is_streaming and not st.session_state.get("_sshared", {}).get("done"):
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

    if _log and not _is_streaming and not st.session_state.get("_sshared", {}).get("done"):
        with st.expander("Stream log"):
            st.code("\n".join(_log), language="text")

    if _samples and not _is_streaming and not st.session_state.get("_sshared", {}).get("done"):
        if st.button("Clear Captured Data", key="serial_clear"):
            for _k in ("serial_last_samples", "serial_last_raw_bytes", "serial_last_log",
                       "_sshared", "_sfinalised"):
                st.session_state.pop(_k, None)
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: all-in-one export (file modes only)
# ─────────────────────────────────────────────────────────────────────────────

if not _live_stream_mode:
    with st.sidebar:
        st.divider()
        _dl_button("Export All Data",
                   make_export_df(timestamps_w, signal_w, cleaned, signals_df,
                                  signal_col, signal_bytes, sampling_rate,
                                  clean_method, peak_method, quality_methods),
                   "all_data.csv", "dl_all")
