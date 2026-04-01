"""ui/sidebar.py — Sidebar controls for source selection and analysis options.

The sidebar is intentionally analysis-oriented. Device pairing and live stream
start/stop live in the Connect Device tab; the sidebar only reports live status
and lets the user choose analysis-specific options.
"""

from __future__ import annotations

import os

import numpy as np
import streamlit as st

from ppg_processing import CLEAN_METHODS, PEAK_METHODS, QUALITY_METHODS, TIMESTAMP_COL
from ui.cache import cached_prepare_signal
from ui.data_loader import DATA_DIR, DEMO_FILES, find_timestamp_col, get_signal_columns, load_data
from ui.live_session import (
    LIVE_ANALYSIS_WINDOW_KEY,
    LIVE_CHANNEL_KEY,
    LIVE_COMPUTED_SR_KEY,
    LIVE_MANUAL_SR_KEY,
    LIVE_OVERRIDE_SR_KEY,
    LIVE_SLOT_KEY,
    ensure_live_session_state,
    get_live_capture_status,
    get_live_channel_options,
    get_live_shared_state,
)
from usb_serial import SERIAL_AVAILABLE, find_adpd7000_port_pair

_LIVE_DISPLAY_S = 15.0


def render_sidebar() -> dict:
    """Render sidebar widgets and return app configuration."""
    ensure_live_session_state()

    with st.sidebar:
        st.markdown("## Signal Source")
        st.caption("Choose what feeds the analysis dashboard.")

        data_source_mode = st.radio(
            "source",
            ["Demo recording", "Upload recording", "Live device"],
            label_visibility="collapsed",
            horizontal=True,
        )
        live_stream_mode = data_source_mode == "Live device"
        df_raw = None
        chosen_file = ""

        if data_source_mode == "Demo recording":
            chosen_file = st.selectbox("demo", DEMO_FILES, label_visibility="collapsed")
            file_path = os.path.join(DATA_DIR, chosen_file)
            df_raw = load_data(file_path, os.path.splitext(chosen_file)[1].lower())
        elif data_source_mode == "Upload recording":
            uploaded = st.file_uploader(
                "upload",
                type=["csv", "xlsx", "xls"],
                label_visibility="collapsed",
            )
            if uploaded is None:
                st.info("Upload a CSV or XLSX recording to continue.")
                st.stop()
            chosen_file = uploaded.name
            df_raw = load_data(uploaded, os.path.splitext(uploaded.name)[1].lower())
        else:
            _render_live_status_panel()

        if not live_stream_mode:
            ts_col = find_timestamp_col(df_raw)
            signal_cols = get_signal_columns(df_raw)
            if not signal_cols:
                st.warning("No valid numeric signal columns found.")
                st.stop()

            st.caption("ANALYSIS CHANNEL")
            signal_col = st.selectbox("ch", signal_cols, label_visibility="collapsed")

            source_key = (chosen_file, signal_col)
            if st.session_state.get("_source_key") != source_key:
                st.session_state.pop("analysis_window", None)
                st.session_state.pop("_pending_window", None)
                st.session_state["_source_key"] = source_key

            timestamps_ms, signal, detected_sr = cached_prepare_signal(df_raw, signal_col, ts_col)
            t0, t1 = float(timestamps_ms[0]), float(timestamps_ms[-1])

            if "_pending_window" in st.session_state:
                st.session_state.analysis_window = st.session_state.pop("_pending_window")

            st.caption("TIME WINDOW")
            duration_s = (t1 - t0) / 1000
            win_ms = st.slider(
                "window",
                min_value=t0,
                max_value=t1,
                value=(t0, t1),
                key="analysis_window",
                label_visibility="collapsed",
            )
            selected_s = (win_ms[1] - win_ms[0]) / 1000
            rc1, rc2 = st.columns([2, 1])
            rc1.caption(f"{selected_s:.1f} s / {duration_s:.1f} s")
            with rc2:
                if st.button("Reset", width="stretch", key="reset_win"):
                    st.session_state._pending_window = (t0, t1)
                    st.rerun()
        else:
            shared = get_live_shared_state()
            buf = shared.get("buf", [])
            slot = st.session_state.get(LIVE_SLOT_KEY, "slota")
            if buf:
                channel_options = list(buf[0].channel_names)
            else:
                channel_options = list(get_live_channel_options(slot))
            current_channel = st.session_state.get(
                LIVE_CHANNEL_KEY,
                channel_options[min(2, len(channel_options) - 1)],
            )
            if current_channel not in channel_options:
                current_channel = channel_options[0]
                st.session_state[LIVE_CHANNEL_KEY] = current_channel

            st.caption("ANALYSIS CHANNEL")
            st.selectbox(
                "Live channel",
                channel_options,
                index=channel_options.index(current_channel),
                key=LIVE_CHANNEL_KEY,
                help="Choose which live channel drives the analysis dashboard.",
            )
            st.caption("ANALYSIS WINDOW")
            st.slider(
                "Analysis window (s)",
                min_value=3,
                max_value=10,
                value=int(st.session_state.get(LIVE_ANALYSIS_WINDOW_KEY, 5)),
                key=LIVE_ANALYSIS_WINDOW_KEY,
                help=f"The dashboard analyses the last N seconds while showing the last {int(_LIVE_DISPLAY_S)} seconds.",
            )
            signal_col = st.session_state.get(LIVE_CHANNEL_KEY, current_channel)
            ts_col = TIMESTAMP_COL
            timestamps_ms = np.array([], dtype=np.float64)
            signal = np.array([], dtype=np.float64)
            t0, t1 = 0.0, _LIVE_DISPLAY_S * 1000
            detected_sr = 0.0

        st.divider()
        st.caption("SIGNAL PREP")
        transform_opt = st.radio(
            "xform",
            ["None", "Invert (2^x − raw)", "Flip AC (2×mean − signal)"],
            label_visibility="collapsed",
            help="None: use as-is  |  Invert: ADC hardware inversion  |  Flip AC: flip polarity, preserve DC",
        )
        adc_bits = 24
        flip_ac_sliding = True
        flip_ac_window_s = 2.0
        if transform_opt == "Invert (2^x − raw)":
            adc_bits = st.number_input("ADC bits (x)", min_value=1, max_value=32, value=24, step=1)
        if transform_opt == "Flip AC (2×mean − signal)":
            flip_ac_sliding = st.toggle("Sliding baseline", value=True)
            if flip_ac_sliding:
                flip_ac_window_s = st.number_input(
                    "Baseline window (s)",
                    min_value=0.1,
                    max_value=30.0,
                    value=2.0,
                    step=0.1,
                )
        transform_mode = (
            "invert" if transform_opt == "Invert (2^x − raw)"
            else "flip_ac" if transform_opt == "Flip AC (2×mean − signal)"
            else "none"
        )

        st.divider()
        st.caption("SAMPLING")
        if not live_stream_mode:
            st.metric("Detected SR", f"{detected_sr:.1f} Hz")
            override_sr = st.toggle("Override SR")
            if override_sr:
                sampling_rate = st.number_input(
                    "Manual SR (Hz)",
                    min_value=1.0,
                    max_value=10000.0,
                    value=float(round(detected_sr, 1)),
                    step=0.5,
                )
            else:
                sampling_rate = detected_sr
        else:
            live_sr = st.session_state.get(LIVE_COMPUTED_SR_KEY, 0.0)
            st.metric("Live SR", f"{live_sr:.1f} Hz" if live_sr > 0 else "—")
            override_sr = st.toggle("Override SR", key=LIVE_OVERRIDE_SR_KEY)
            if override_sr:
                sampling_rate = st.number_input(
                    "Manual SR (Hz)",
                    min_value=1.0,
                    max_value=10000.0,
                    value=float(round(live_sr or 100.0, 1)),
                    step=0.5,
                    key=LIVE_MANUAL_SR_KEY,
                )
            else:
                sampling_rate = live_sr if live_sr > 0 else 100.0

        st.divider()
        st.caption("PIPELINE")
        st.selectbox(
            "Cleaning method",
            CLEAN_METHODS,
            index=CLEAN_METHODS.index(st.session_state.get("clean_method", CLEAN_METHODS[0])),
            key="clean_method",
        )
        st.selectbox(
            "Peak detection",
            PEAK_METHODS,
            index=PEAK_METHODS.index(st.session_state.get("peak_method", PEAK_METHODS[0])),
            key="peak_method",
        )
        st.multiselect(
            "Quality methods",
            QUALITY_METHODS,
            default=st.session_state.get("quality_methods") or [QUALITY_METHODS[0]],
            key="quality_methods",
        )

        st.divider()
        show_nk_plot = st.checkbox("Show NeuroKit2 native plot")

    return {
        "live_stream_mode": live_stream_mode,
        "df_raw": df_raw,
        "signal_col": signal_col,
        "ts_col": ts_col,
        "transform_mode": transform_mode,
        "adc_bits": adc_bits,
        "flip_ac_sliding": flip_ac_sliding,
        "flip_ac_window_s": flip_ac_window_s,
        "sampling_rate": sampling_rate,
        "timestamps_ms": timestamps_ms,
        "signal": signal,
        "t0": t0,
        "t1": t1,
        "show_nk_plot": show_nk_plot,
    }


def _render_live_status_panel() -> None:
    if not SERIAL_AVAILABLE:
        st.error("`pyserial` not installed — `pip install pyserial`")
        st.stop()

    status = get_live_capture_status()
    shared = get_live_shared_state()
    buf = shared.get("buf", [])

    st.caption("LIVE STATUS")
    if status["phase"] == "needs_connection":
        pair = find_adpd7000_port_pair()
        st.info(status["detail"])
        st.caption(f"Suggested control port: {pair['control_port'] or 'Not detected'}")
        st.caption(f"Suggested stream port: {pair['stream_port'] or 'Not detected'}")
        return

    control_port = status["control_port"]
    stream_port = status["stream_port"]
    st.caption(f"{status['title']}  |  Control `{control_port}`  |  Stream `{stream_port}`")

    tone = status["tone"]
    if tone == "error":
        st.error(status["detail"])
    elif tone == "success":
        st.success(status["detail"])
    else:
        st.info(status["detail"])

    meta1, meta2, meta3 = st.columns(3)
    meta1.metric("Slot", status["slot"])
    meta2.metric("ODR", f"{status['odr_hz']} Hz")
    meta3.metric("Samples kept", f"{len(buf):,}")
    st.caption(status["action_hint"])
