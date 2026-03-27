"""ui/sidebar.py — Compact sidebar: all controls, no chart output.

render_sidebar() is the single source of truth for:
  - data source & file loading
  - channel / timestamp column selection
  - time-window slider (file modes)
  - signal transform
  - sample-rate override
  - analysis method selection  ← moved here from the analysis tab
  - USB analysis-stream start/stop  ← uses shared conn_* state

USB connection is managed by the USB Serial tab (writes conn_connected /
conn_port / conn_baud). The sidebar reads those keys and shows a status
badge; if not yet connected it shows a compact quick-connect panel so the
user doesn't have to leave the Analysis tab.
"""

import os
import threading

import numpy as np
import streamlit as st

from ppg_processing import CLEAN_METHODS, PEAK_METHODS, QUALITY_METHODS, TIMESTAMP_COL
from ui.data_loader import DATA_DIR, DEMO_FILES, load_data, get_signal_columns, find_timestamp_col
from ui.cache import cached_prepare_signal
from usb_serial import (
    SERIAL_AVAILABLE, list_serial_ports, describe_ports,
    test_connection, send_command, stream_binary_live,
)

_LIVE_DISPLAY_S = 15.0  # rolling display window (seconds) for live streaming


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> dict:
    """Render all sidebar widgets and return a config dict for app.py.

    Returns
    -------
    dict with keys:
        live_stream_mode  bool
        df_raw            pd.DataFrame | None
        signal_col        str
        ts_col            str
        transform_mode    str   "none" | "invert" | "flip_ac"
        adc_bits          int
        flip_ac_sliding   bool
        flip_ac_window_s  float
        sampling_rate     float
        timestamps_ms     np.ndarray  (empty array in live mode)
        signal            np.ndarray  (empty array in live mode)
        t0                float       ms
        t1                float       ms
        show_nk_plot      bool
    """
    with st.sidebar:
        # ── Header ───────────────────────────────────────────────────────────
        st.markdown("## PPG Analysis")

        # ── Data source ───────────────────────────────────────────────────────
        data_source_mode = st.radio(
            "source",
            ["Demo files", "Upload file", "USB Serial Stream"],
            label_visibility="collapsed",
            horizontal=True,
        )
        live_stream_mode = data_source_mode == "USB Serial Stream"
        df_raw = None
        file_ext = ".csv"
        chosen_file = ""

        if data_source_mode == "Demo files":
            chosen_file = st.selectbox("demo", DEMO_FILES, label_visibility="collapsed")
            file_path = os.path.join(DATA_DIR, chosen_file)
            file_ext = os.path.splitext(chosen_file)[1].lower()
            df_raw = load_data(file_path, file_ext)

        elif data_source_mode == "Upload file":
            uploaded = st.file_uploader(
                "upload", type=["csv", "xlsx", "xls"], label_visibility="collapsed"
            )
            if uploaded is not None:
                file_ext = os.path.splitext(uploaded.name)[1].lower()
                df_raw = load_data(uploaded, file_ext)
                chosen_file = uploaded.name
            else:
                st.info("Upload a CSV or XLSX file to continue.")
                st.stop()

        else:  # USB Serial Stream
            _render_usb_status_panel()

        # ── Channel + time window (file modes) ───────────────────────────────

        if not live_stream_mode:
            ts_col = find_timestamp_col(df_raw)
            signal_cols = get_signal_columns(df_raw)
            if not signal_cols:
                st.warning("No valid numeric signal columns found.")
                st.stop()

            st.caption("CHANNEL")
            signal_col = st.selectbox("ch", signal_cols, label_visibility="collapsed")

            # Reset when file or channel changes
            _source_key = (chosen_file, signal_col)
            if st.session_state.get("_source_key") != _source_key:
                st.session_state.pop("analysis_window", None)
                st.session_state.pop("_pending_window", None)
                st.session_state["_source_key"] = _source_key

            # Prepare signal → t0, t1
            timestamps_ms, signal, detected_sr = cached_prepare_signal(
                df_raw, signal_col, ts_col
            )
            t0, t1 = float(timestamps_ms[0]), float(timestamps_ms[-1])

            # Apply pending window from chart box-select
            if "_pending_window" in st.session_state:
                st.session_state.analysis_window = st.session_state.pop("_pending_window")

            # Time-window slider
            st.caption("TIME WINDOW")
            duration_s = (t1 - t0) / 1000
            win_ms = st.slider(
                "window",
                min_value=t0, max_value=t1, value=(t0, t1),
                key="analysis_window", label_visibility="collapsed",
            )
            selected_s = (win_ms[1] - win_ms[0]) / 1000
            rc1, rc2 = st.columns([2, 1])
            rc1.caption(f"{selected_s:.1f} s / {duration_s:.1f} s")
            with rc2:
                if st.button("Reset", width="stretch", key="reset_win"):
                    st.session_state._pending_window = (t0, t1)
                    st.rerun()

        else:
            # USB live mode — dummy values; real ones computed inside fragment
            signal_col = st.session_state.get("live_channel", "ch3")
            ts_col = TIMESTAMP_COL
            timestamps_ms = np.array([], dtype=np.float64)
            signal = np.array([], dtype=np.float64)
            t0, t1 = 0.0, _LIVE_DISPLAY_S * 1000
            detected_sr = 0.0

        st.divider()

        # ── Signal transform ──────────────────────────────────────────────────

        st.caption("SIGNAL TRANSFORM")
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
            adc_bits = st.number_input(
                "ADC bits (x)", min_value=1, max_value=32, value=24, step=1,
                help="Formula: 2^x − signal",
            )
        if transform_opt == "Flip AC (2×mean − signal)":
            flip_ac_sliding = st.toggle(
                "Sliding baseline", value=True,
                help="ON: rolling mean tracks DC drift  |  OFF: single global mean",
            )
            if flip_ac_sliding:
                flip_ac_window_s = st.number_input(
                    "Baseline window (s)", min_value=0.1, max_value=30.0,
                    value=2.0, step=0.1,
                )

        transform_mode = (
            "invert" if transform_opt == "Invert (2^x − raw)"
            else "flip_ac" if transform_opt == "Flip AC (2×mean − signal)"
            else "none"
        )

        st.divider()

        # ── Sample rate ───────────────────────────────────────────────────────

        st.caption("SAMPLE RATE")
        if not live_stream_mode:
            st.metric("Detected SR", f"{detected_sr:.1f} Hz")
            override_sr = st.toggle("Override SR")
            if override_sr:
                sampling_rate = st.number_input(
                    "Manual SR (Hz)", min_value=1.0, max_value=10000.0,
                    value=float(round(detected_sr, 1)), step=0.5,
                )
            else:
                sampling_rate = detected_sr
        else:
            live_sr = st.session_state.get("_live_computed_sr", 0.0)
            st.metric("Live SR", f"{live_sr:.1f} Hz" if live_sr > 0 else "—")
            override_sr = st.toggle("Override SR", key="live_override_sr")
            if override_sr:
                sampling_rate = st.number_input(
                    "Manual SR (Hz)", min_value=1.0, max_value=10000.0,
                    value=float(round(live_sr or 100.0, 1)), step=0.5,
                    key="live_manual_sr",
                )
            else:
                sampling_rate = live_sr if live_sr > 0 else 100.0

        st.divider()

        # ── Analysis methods ──────────────────────────────────────────────────

        st.caption("ANALYSIS METHODS")
        st.selectbox(
            "Cleaning method", CLEAN_METHODS,
            index=CLEAN_METHODS.index(st.session_state.get("clean_method", CLEAN_METHODS[0])),
            key="clean_method",
        )
        st.selectbox(
            "Peak detection", PEAK_METHODS,
            index=PEAK_METHODS.index(st.session_state.get("peak_method", PEAK_METHODS[0])),
            key="peak_method",
        )
        st.multiselect(
            "Quality methods", QUALITY_METHODS,
            default=st.session_state.get("quality_methods") or [QUALITY_METHODS[0]],
            key="quality_methods",
        )

        st.divider()

        # ── Display options ───────────────────────────────────────────────────

        show_nk_plot = st.checkbox("Show NeuroKit2 native plot")

    return {
        "live_stream_mode": live_stream_mode,
        "df_raw":           df_raw,
        "signal_col":       signal_col,
        "ts_col":           ts_col,
        "transform_mode":   transform_mode,
        "adc_bits":         adc_bits,
        "flip_ac_sliding":  flip_ac_sliding,
        "flip_ac_window_s": flip_ac_window_s,
        "sampling_rate":    sampling_rate,
        "timestamps_ms":    timestamps_ms,
        "signal":           signal,
        "t0":               t0,
        "t1":               t1,
        "show_nk_plot":     show_nk_plot,
    }


# ─────────────────────────────────────────────────────────────────────────────
# USB status panel (shown when data_source_mode == "USB Serial Stream")
# ─────────────────────────────────────────────────────────────────────────────

def _render_usb_status_panel():
    """Show connection status + quick connect + analysis stream controls."""
    if not SERIAL_AVAILABLE:
        st.error("`pyserial` not installed — `pip install pyserial`")
        st.stop()

    conn_connected    = st.session_state.get("conn_connected", False)
    conn_port         = st.session_state.get("conn_port", "")
    conn_baud         = st.session_state.get("conn_baud", 115200)
    live_is_streaming = st.session_state.get("live_streaming", False)

    st.caption("CONNECTION")

    if conn_connected:
        # Status badge
        desc_map = {p["device"]: p["description"] for p in describe_ports()}
        desc = desc_map.get(conn_port, conn_port)
        st.success(f"● **{conn_port}** @ {conn_baud}  \n{desc}")
        if st.button("Disconnect", type="secondary", width="stretch", key="usb_quick_disconn"):
            if live_is_streaming:
                ev = st.session_state.get("live_stop_event")
                if ev:
                    ev.set()
            st.session_state["conn_connected"] = False
            st.session_state["live_streaming"] = False
            st.rerun()
    else:
        # Quick-connect panel (so user doesn't have to switch tabs)
        ports = list_serial_ports()
        if ports:
            qport = st.selectbox("Port", ports, key="quick_conn_port",
                                 label_visibility="collapsed")
        else:
            qport = st.text_input("Port", value=conn_port or "/dev/tty.usbmodem101",
                                  key="quick_conn_port_txt", label_visibility="collapsed")

        baud_opts = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        qbaud = st.selectbox("Baud", baud_opts, index=4, key="quick_conn_baud",
                             label_visibility="collapsed")

        qc1, qc2 = st.columns(2)
        with qc1:
            if st.button("Connect", type="primary", width="stretch", key="quick_conn_btn"):
                chk = test_connection(qport, qbaud)
                if chk.ok:
                    st.session_state["conn_connected"] = True
                    st.session_state["conn_port"]      = qport
                    st.session_state["conn_baud"]      = qbaud
                    st.session_state.pop("_quick_conn_err", None)
                else:
                    st.session_state["_quick_conn_err"] = chk.error or "Connection failed"
                st.rerun()
        with qc2:
            if st.button("Refresh", width="stretch", key="quick_refresh_btn"):
                st.rerun()

        if err := st.session_state.get("_quick_conn_err"):
            st.error(err)

        st.caption("Or connect from the **USB Serial** tab.")
        return  # no streaming controls until connected

    st.divider()

    # ── Analysis stream controls (only when connected) ────────────────────────

    st.caption("LIVE ANALYSIS STREAM")
    ODR_OPTIONS = [10, 25, 50, 100, 200, 400]
    st.select_slider(
        "ODR (Hz)", options=ODR_OPTIONS, value=100, key="live_odr",
        disabled=live_is_streaming,
        help="Sends `adpd ppg freq <hz>` before stream start",
    )
    st.selectbox(
        "Channel", ["ch1", "ch2", "ch3", "ch4"], index=2, key="live_channel",
        disabled=live_is_streaming,
        help="Ch3/Ch4 = PPG (IN3 paired)  |  Ch1/Ch2 = ambient",
    )
    st.number_input(
        "Samples", min_value=100, max_value=100_000, value=10_000, step=500,
        key="live_n_samples", disabled=live_is_streaming,
        help="Large value = long stream; press Stop to end early",
    )
    st.slider(
        "Analysis window (s)", min_value=3, max_value=10, value=5,
        key="live_analysis_window_s",
        help=f"Last N seconds analysed; display shows last {int(_LIVE_DISPLAY_S)} s",
    )

    lc1, lc2 = st.columns(2)
    with lc1:
        if st.button("▶ Start", disabled=live_is_streaming, type="primary",
                     width="stretch", key="live_start_btn"):
            odr = st.session_state.get("live_odr", 100)
            send_command(conn_port, conn_baud,
                         f"adpd ppg freq {odr}", response_timeout_s=2.0)
            _start_live_stream(
                conn_port, conn_baud,
                st.session_state.get("live_n_samples", 10_000),
            )
            st.rerun()
    with lc2:
        if st.button("■ Stop", disabled=not live_is_streaming,
                     width="stretch", key="live_stop_btn"):
            ev = st.session_state.get("live_stop_event")
            if ev:
                ev.set()

    # Stream status
    shared = st.session_state.get("_sshared_live", {})
    buf    = shared.get("buf", [])
    err    = shared.get("error")
    odr_d  = st.session_state.get("live_odr", 100)

    if live_is_streaming:
        st.info(f"{len(buf):,} samples @ {odr_d} Hz")
    elif err:
        st.error(err)
    elif buf:
        st.success(f"Done — {len(buf):,} samples")
    else:
        st.caption("Press ▶ Start to begin streaming.")


# ─────────────────────────────────────────────────────────────────────────────
# Live stream worker
# ─────────────────────────────────────────────────────────────────────────────

def _start_live_stream(port: str, baud: int, n_samples: int):
    """Initialise shared state and launch the background stream worker thread."""
    stop_ev = threading.Event()
    shared: dict = {"buf": [], "raw": bytearray(), "log": [], "error": None, "done": False}

    st.session_state["live_streaming"]   = True
    st.session_state["live_stop_event"]  = stop_ev
    st.session_state["_sshared_live"]    = shared
    st.session_state["_live_finalised"]  = False
    st.session_state.pop("_live_computed_sr", None)

    def _worker():
        try:
            for new_s, new_raw, new_log, is_final in stream_binary_live(port, baud, n_samples):
                if stop_ev.is_set():
                    break
                shared["buf"].extend(new_s)
                shared["raw"].extend(new_raw)
                shared["log"].extend(new_log)
                for ll in new_log:
                    if ll.startswith("ERROR:"):
                        shared["error"] = ll[6:].strip()
                        stop_ev.set()
                        break
                if is_final or stop_ev.is_set():
                    break
        except Exception as exc:
            shared["error"] = str(exc)
        finally:
            shared["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
