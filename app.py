"""app.py — PPG Signal Filter & Analysis  (Streamlit entry point).

Thin orchestrator:
  1. Page config
  2. render_sidebar() → sidebar_cfg  (all controls live there)
  3. Run NeuroKit2 pipeline for file modes (live mode runs in the fragment)
  4. render_analysis_tab() — dashboard with 2×2 chart grid
  5. render_serial_tab()   — connection, commands, binary capture

Requires Python 3.10+
"""
import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import streamlit as st

from ppg_processing import (
    CLEAN_METHODS, PEAK_METHODS, QUALITY_METHODS,
    apply_signal_transform, auto_beat_windows, compute_hr_metrics,
)
from usb_serial import SERIAL_AVAILABLE

from ui.cache import cached_pipeline
from ui.sidebar import render_sidebar
from ui.analysis_tab import render_analysis_tab
from ui.serial_tab import render_serial_tab

# ── Startup diagnostics (console + browser) ──────────────────────────────────
print(f"[STARTUP] SERIAL_AVAILABLE: {SERIAL_AVAILABLE}")
st.components.v1.html(
    f"<script>console.log('[STARTUP] SERIAL_AVAILABLE: {SERIAL_AVAILABLE}');</script>",
    height=1,
)

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="PPG Filter & Analysis", layout="wide")
st.title("PPG Signal Filter & Analysis")

_tab_analysis, _tab_serial = st.tabs(["Analysis", "USB Serial"])

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar  (all controls — data source, channel, transform, SR, methods)
# ─────────────────────────────────────────────────────────────────────────────

sidebar_cfg = render_sidebar()

# ─────────────────────────────────────────────────────────────────────────────
# Method selections  (widgets are in the sidebar with key=; read session state)
# ─────────────────────────────────────────────────────────────────────────────

clean_method    = st.session_state.get("clean_method",    CLEAN_METHODS[0])
peak_method     = st.session_state.get("peak_method",     PEAK_METHODS[0])
quality_methods = st.session_state.get("quality_methods") or [QUALITY_METHODS[0]]
quality_method  = quality_methods[0]

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline  (file modes only — live mode runs inside the analysis fragment)
# ─────────────────────────────────────────────────────────────────────────────

live_stream_mode = sidebar_cfg["live_stream_mode"]

if not live_stream_mode:
    timestamps_ms = sidebar_cfg["timestamps_ms"]
    signal        = sidebar_cfg["signal"]
    t0            = sidebar_cfg["t0"]
    t1            = sidebar_cfg["t1"]

    # Apply time-window mask
    win_ms = st.session_state.get("analysis_window", (t0, t1))
    mask   = (timestamps_ms >= win_ms[0]) & (timestamps_ms <= win_ms[1])
    timestamps_w  = timestamps_ms[mask]
    signal_w_orig = signal[mask]

    # Signal transform
    signal_w, flip_baseline = apply_signal_transform(
        signal_w_orig,
        mode=sidebar_cfg["transform_mode"],
        adc_bits=sidebar_cfg["adc_bits"],
        flip_sliding=sidebar_cfg["flip_ac_sliding"],
        flip_window_s=sidebar_cfg["flip_ac_window_s"],
        sampling_rate=sidebar_cfg["sampling_rate"],
    )

    if len(signal_w) < 10:
        st.error("Selected window is too short — widen the time window in the sidebar.")
        st.stop()

    signal_bytes = signal_w.tobytes()

    try:
        results = cached_pipeline(
            signal_bytes, sidebar_cfg["sampling_rate"],
            clean_method, peak_method, quality_method,
        )
    except ValueError as e:
        st.error(f"Processing error: {e}\n\nTry a different cleaning or peak method.")
        st.stop()
    except Exception as e:
        st.error(f"Unexpected pipeline error: {e}")
        st.stop()

    cleaned      = results["cleaned"]
    signals_df   = results["signals_df"]
    info         = results["info"]
    quality      = results["quality"]
    analysis     = results["analysis"]
    peak_indices = info.get("PPG_Peaks", np.array([], dtype=int))

    # Auto beat-window (updated when peaks change)
    peaks_fp = (len(peak_indices), int(np.sum(peak_indices)) if len(peak_indices) else 0)
    if st.session_state.get("_peaks_fp") != peaks_fp:
        auto_pre, auto_post = auto_beat_windows(peak_indices, sidebar_cfg["sampling_rate"])
        st.session_state.beat_pre  = auto_pre
        st.session_state.beat_post = auto_post
        st.session_state._peaks_fp = peaks_fp
    else:
        st.session_state.setdefault("beat_pre",  0.2)
        st.session_state.setdefault("beat_post", 0.5)

    hr_mean, hr_min, hr_max = compute_hr_metrics(peak_indices, sidebar_cfg["sampling_rate"])

    pipeline_ctx = {
        "timestamps_w":  timestamps_w,
        "signal_w":      signal_w,
        "signal_w_orig": signal_w_orig,
        "flip_baseline": flip_baseline,
        "cleaned":       cleaned,
        "signals_df":    signals_df,
        "info":          info,
        "quality":       quality,
        "analysis":      analysis,
        "peak_indices":  peak_indices,
        "hr_mean":       hr_mean,
        "hr_min":        hr_min,
        "hr_max":        hr_max,
        "signal_bytes":  signal_bytes,
    }

else:
    # Live mode: pipeline runs inside the analysis fragment
    pipeline_ctx = {
        "timestamps_w":  np.array([], dtype=np.float64),
        "signal_w":      np.array([], dtype=np.float64),
        "signal_w_orig": np.array([], dtype=np.float64),
        "flip_baseline": None,
        "cleaned":       np.array([], dtype=np.float64),
        "signals_df":    pd.DataFrame(),
        "info":          {"PPG_Peaks": np.array([], dtype=int)},
        "quality":       None,
        "analysis":      None,
        "peak_indices":  np.array([], dtype=int),
        "hr_mean":       None,
        "hr_min":        None,
        "hr_max":        None,
        "signal_bytes":  b"",
    }

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────

with _tab_analysis:
    render_analysis_tab(sidebar_cfg, pipeline_ctx)

with _tab_serial:
    render_serial_tab()
