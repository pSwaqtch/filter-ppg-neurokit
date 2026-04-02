"""ui/serial_tab.py — USB Serial tab: connection panel, command console, binary capture.

Design philosophy (research-engineer tool):
  • All commands are visible at once — no hidden drill-down menus.
  • Zero-arg commands are single-click buttons grouped by category.
  • Commands that need a value show an inline input; pressing Enter sends immediately.
  • Response timeout is a top-level field, not buried in an expander.
  • Last command + response always visible at the top of the console.

Session state owned here:
    conn_connected      bool
    conn_port           str
    conn_baud           int
    serial_conn_log     list[(ts, level, msg)]
    _cmd_last_response  dict{cmd, text, ok}
    capture_streaming   bool
    capture_stop_event  threading.Event
    _sshared_capture    dict{buf, raw, log, error, done}
    _capture_finalised  bool
"""

import datetime
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui.live_session import (
    LIVE_ODR_KEY,
    LIVE_SAMPLE_COUNT_KEY,
    LIVE_SLOT_KEY,
    get_live_capture_status,
    launch_live_stream,
    stop_live_session,
)
from usb_serial import (
    SERIAL_AVAILABLE, list_serial_ports, describe_ports, find_adpd7000_port_pair,
    find_port_owner, force_release_port, test_connection,
    send_command, stream_binary_live_dual_port, stream_text_live_dual_port,
)

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

_SERIAL_CSS = """
<style>
/* Section dividers inside command console */
.cmd-section {
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.28);
    border-bottom: 1px solid rgba(255,255,255,0.07);
    margin: 0.9rem 0 0.35rem;
    padding-bottom: 0.2rem;
}
/* Monospace command-prefix labels inside form rows */
.cmd-prefix {
    font-family: "SFMono-Regular", "Consolas", monospace;
    font-size: 0.78rem;
    color: rgba(255,255,255,0.7);
    line-height: 2.4;   /* vertically centers text against the input widget */
    white-space: nowrap;
}
/* Console output panel label */
.console-label {
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.3);
    margin-bottom: 0.3rem;
}
</style>
"""

# Per-channel display config for binary capture chart.
# Slot A: Ch1/Ch2 = ambient (hidden by default), Ch3/Ch4 = PPG.
# Slot AB adds Ch5–Ch8 from Slot B — all shown by default.
_CH_INFO = [
    # (label,              color,       visible_default)
    ("Ch1 Slot-A ambient", "#888888",   "legendonly"),
    ("Ch2 Slot-A ambient", "#aaaaaa",   "legendonly"),
    ("Ch3 Slot-A PPG",     "#1f77b4",   True),
    ("Ch4 Slot-A PPG",     "#ff7f0e",   True),
    ("Ch5 Slot-B",         "#2ca02c",   True),
    ("Ch6 Slot-B",         "#d62728",   True),
    ("Ch7 Slot-B",         "#9467bd",   True),
    ("Ch8 Slot-B",         "#8c564b",   True),
]

# Supported ODR values for the ADPD7000 PPG freq command
_ODR_OPTIONS = [10, 25, 50, 100, 200, 400]

_DOC_COMMANDS = [
    "help",
    "help adpd",
    "help adpd ppg",
    "help eeprom",
    "help rtos",
    "reset",
    "scan i2c",
    "scan spi",
    "rtos stats",
    "eeprom info",
    "eeprom test",
    "adpd probe",
    "adpd probe sdk",
    "adpd read slota",
    "adpd read slotab",
    "adpd reset",
    "adpd gpio read",
    "adpd calib clk",
    "adpd ppg list",
    "adpd ppg slota show",
    "adpd ppg slotab show",
    "adpd ppg slota2 show",
    "adpd ppg slota reset",
    "adpd ppg slota start",
    "adpd ppg slotab start",
    "adpd ppg slota2 start",
    "adpd ppg stop",
    "adpd ppg freq",
]


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str, level: str = "info"):
    """Append a timestamped entry to the connection log stored in session state."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    st.session_state.setdefault("serial_conn_log", []).append((ts, level, msg))


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def render_serial_tab():
    st.markdown(_SERIAL_CSS, unsafe_allow_html=True)
    st.header("Connect Device")
    st.caption(
        "Pair the ADPD7000 control and stream ports here, run the live feed, then use expert shell and raw capture tools when needed."
    )

    if not SERIAL_AVAILABLE:
        st.error("`pyserial` is not installed — `pip install pyserial`")
        st.stop()

    _render_connection_panel()

    # Command console and binary capture only appear once connected
    if not st.session_state.get("conn_connected"):
        return

    st.divider()
    _render_live_analysis_feed()
    st.divider()
    _render_command_console()
    st.divider()
    _render_stream_capture()


# ─────────────────────────────────────────────────────────────────────────────
# Connection panel
# ─────────────────────────────────────────────────────────────────────────────

def _render_connection_panel():
    is_connected = st.session_state.get("conn_connected", False)
    active_control = st.session_state.get("conn_control_port", st.session_state.get("conn_port", ""))
    active_stream = st.session_state.get("conn_stream_port", "")
    active_baud  = st.session_state.get("conn_baud", 115200)

    ports = list_serial_ports()
    detected_pair = find_adpd7000_port_pair()
    # Mirror detected ports to the browser console for USB debugging
    st.components.v1.html(
        f"<script>console.log('[USB] ports:', {ports});</script>", height=1
    )

    st.subheader("1. Pair Ports")
    pc1, pc2, pc3, pc4, pc5 = st.columns([3, 3, 2, 1, 1])

    with pc1:
        if ports:
            default_control = detected_pair["control_port"] or active_control or ports[0]
            idx = ports.index(default_control) if default_control in ports else 0
            control_port = st.selectbox(
                "Control Port", ports, index=idx, key="tab_conn_control", disabled=is_connected
            )
        else:
            control_port = st.text_input(
                "Control Port (manual)",
                value=active_control or detected_pair["control_port"] or "/dev/tty.usbmodem101",
                key="tab_conn_control_txt",
                disabled=is_connected,
            )

    with pc2:
        if ports:
            fallback_stream = ports[1] if len(ports) > 1 else ports[0]
            default_stream = detected_pair["stream_port"] or active_stream or fallback_stream
            idx = ports.index(default_stream) if default_stream in ports else min(1, len(ports) - 1)
            stream_port = st.selectbox(
                "Stream Port", ports, index=idx, key="tab_conn_stream", disabled=is_connected
            )
        else:
            stream_port = st.text_input(
                "Stream Port (manual)",
                value=active_stream or detected_pair["stream_port"] or "/dev/tty.usbmodem102",
                key="tab_conn_stream_txt",
                disabled=is_connected,
            )

    with pc3:
        baud_opts = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        baud_idx  = baud_opts.index(active_baud) if active_baud in baud_opts else 4
        baud = st.selectbox("Baud", baud_opts, index=baud_idx,
                            key="tab_conn_baud", disabled=is_connected)

    with pc4:
        if not is_connected:
            if st.button("Connect", type="primary", width="stretch", key="tab_conn_btn"):
                if not control_port or not stream_port:
                    st.session_state["_tab_conn_err"] = "Select both a control port and a stream port."
                elif control_port == stream_port:
                    st.session_state["_tab_conn_err"] = "Control port and stream port must be different."
                else:
                    with st.spinner(f"Connecting to {control_port} / {stream_port}…"):
                        chk_control = test_connection(control_port, baud)
                        chk_stream = test_connection(stream_port, baud)
                    if chk_control.ok and chk_stream.ok:
                        st.session_state.update(
                            conn_connected=True,
                            conn_port=control_port,
                            conn_control_port=control_port,
                            conn_stream_port=stream_port,
                            conn_baud=baud,
                        )
                        st.session_state.pop("_tab_conn_err", None)
                        _log(f"Connected — control {control_port} / stream {stream_port} @ {baud}", "ok")
                    else:
                        err = chk_control.error if not chk_control.ok else chk_stream.error
                        st.session_state["_tab_conn_err"] = err or "Connection failed"
                        _log(f"Connect failed: {err}", "error")
                st.rerun()
        else:
            if st.button("Disconnect", type="secondary", width="stretch", key="tab_disconn_btn"):
                stop_live_session()
                st.session_state["conn_connected"] = False
                st.session_state["conn_port"] = ""
                st.session_state["conn_control_port"] = ""
                st.session_state["conn_stream_port"] = ""
                st.session_state.pop("_tab_conn_err", None)
                _log(f"Disconnected from control {active_control} / stream {active_stream}", "info")
                st.rerun()

    with pc5:
        if st.button("Refresh", width="stretch", key="tab_refresh_btn", disabled=is_connected):
            st.rerun()

    # ── Status badge ──────────────────────────────────────────────────────────
    last_err = st.session_state.get("_tab_conn_err", "")
    if is_connected:
        desc_map = {p["device"]: p["description"] for p in describe_ports()}
        st.success(
            f"Connected — control **{active_control}** ({desc_map.get(active_control, active_control)})"
            f" · stream **{active_stream}** ({desc_map.get(active_stream, active_stream)})"
            f" · {active_baud} baud"
        )
    else:
        if last_err and "PORT_BUSY" in last_err:
            busy_port = control_port
            owner = find_port_owner(busy_port)
            owner_s = f"held by **{owner[1]}** (PID {owner[0]})" if owner else "owner unknown"
            st.error(f"Port busy — {busy_port} {owner_s}")
            fc1, fc2 = st.columns([3, 1])
            fc1.caption("Another process has the port open. Force-disconnect terminates it and reconnects.")
            with fc2:
                if st.button("Force & Reconnect", type="primary", width="stretch", key="tab_force_btn"):
                    with st.spinner("Releasing port…"):
                        rel = force_release_port(busy_port)
                    if rel.ok:
                        _log(f"Force release: {rel.response}", "warn")
                        time.sleep(0.5)
                        chk2_control = test_connection(control_port, baud)
                        chk2_stream = test_connection(stream_port, baud)
                        if chk2_control.ok and chk2_stream.ok:
                            st.session_state.update(
                                conn_connected=True,
                                conn_port=control_port,
                                conn_control_port=control_port,
                                conn_stream_port=stream_port,
                                conn_baud=baud,
                            )
                            st.session_state.pop("_tab_conn_err", None)
                            _log(
                                f"Reconnected after force release — control {control_port} / stream {stream_port} @ {baud}",
                                "ok",
                            )
                        else:
                            err = chk2_control.error if not chk2_control.ok else chk2_stream.error
                            st.session_state["_tab_conn_err"] = err or ""
                            _log(f"Reconnect failed: {err}", "error")
                    else:
                        _log(f"Force release failed: {rel.error}", "error")
                        st.session_state["_tab_conn_err"] = f"FORCE_FAILED: {rel.error}"
                    st.rerun()
        elif last_err:
            st.error(last_err)
        else:
            if ports:
                desc_map = {p["device"]: p["description"] for p in describe_ports()}
                if control_port in desc_map:
                    st.caption(f"Control device: {desc_map[control_port]}")
                if stream_port in desc_map:
                    st.caption(f"Stream device: {desc_map[stream_port]}")
            st.warning("Not connected — select a port and click Connect.")

    # ── Connection log (collapsed by default) ─────────────────────────────────
    log_entries = st.session_state.get("serial_conn_log", [])
    with st.expander(f"Connection log — {len(log_entries)} entries"):
        if log_entries:
            icons = {"ok": "✓", "error": "✗", "warn": "!", "info": "·"}
            lines = [f"[{ts}] {icons.get(lvl, '·')} {msg}"
                     for ts, lvl, msg in reversed(log_entries)]
            st.code("\n".join(lines), language="text")
            if st.button("Clear log", key="tab_clear_log"):
                st.session_state["serial_conn_log"] = []
                st.rerun()
        else:
            st.caption("No events yet.")


# ─────────────────────────────────────────────────────────────────────────────
# Command console — flat palette, no hidden tree navigation
# ─────────────────────────────────────────────────────────────────────────────

def _send(cmd: str):
    """Send a command, append to the rolling terminal history, then rerun.

    History is stored first so the terminal panel is populated on the very
    next render — without the rerun Streamlit's top-to-bottom pass would
    render the terminal *before* the button handler fires, leaving it stale.
    """
    port    = st.session_state.get("conn_control_port", st.session_state.get("conn_port", ""))
    baud    = st.session_state.get("conn_baud", 115200)
    timeout = st.session_state.get("serial_resp_timeout", 3.0)
    ts      = datetime.datetime.now().strftime("%H:%M:%S")
    with st.spinner(f"`{cmd}`"):
        result = send_command(port, baud, cmd, response_timeout_s=timeout)

    if result.ok:
        _log(f">> {cmd}", "info")
        if result.response:
            _log(f"<< {result.response[:120]}", "info")  # truncate conn log only
        entry = {"ts": ts, "cmd": cmd, "text": result.response or "(no response)", "ok": True}
    else:
        _log(f"Error ({cmd}): {result.error}", "error")
        entry = {"ts": ts, "cmd": cmd, "text": result.error or "Unknown error", "ok": False}

    # Append to rolling history; cap at 100 entries to bound HTML size
    history = st.session_state.setdefault("_cmd_history", [])
    history.append(entry)
    if len(history) > 100:
        del history[:-100]

    # Rerun so the terminal panel reflects the new entry immediately
    st.rerun()


def _render_live_analysis_feed():
    status = get_live_capture_status()
    control_port = st.session_state.get("conn_control_port", st.session_state.get("conn_port", ""))
    stream_port = st.session_state.get("conn_stream_port", "")
    baud = st.session_state.get("conn_baud", 115200)
    is_streaming = st.session_state.get("live_streaming", False)

    st.subheader("2. Feed Live Analysis")
    st.caption(
        "This is the only place that starts or stops the live feed used by the analysis dashboard."
    )

    head1, head2, head3, head4 = st.columns(4)
    head1.metric("State", status["title"])
    head2.metric("Slot", status["slot"])
    head3.metric("ODR", f"{status['odr_hz']} Hz")
    head4.metric("Captured", f"{status['sample_count']:,}/{status['requested_samples']:,}")

    if status["phase"] == "streaming":
        st.progress(status["progress_ratio"], text=status["detail"])

    cfg1, cfg2, cfg3 = st.columns([2, 2, 2])
    with cfg1:
        st.radio(
            "Live slot",
            ["slota", "slotab"],
            horizontal=True,
            key=LIVE_SLOT_KEY,
            disabled=is_streaming,
        )
    with cfg2:
        st.select_slider(
            "ODR (Hz)",
            options=_ODR_OPTIONS,
            value=st.session_state.get(LIVE_ODR_KEY, 100),
            key=LIVE_ODR_KEY,
            disabled=is_streaming,
        )
    with cfg3:
        st.number_input(
            "Samples",
            min_value=100,
            max_value=100_000,
            value=int(st.session_state.get(LIVE_SAMPLE_COUNT_KEY, 10_000)),
            step=500,
            key=LIVE_SAMPLE_COUNT_KEY,
            disabled=is_streaming,
        )

    act1, act2 = st.columns([2, 1])
    with act1:
        if st.button("Start live feed", type="primary", width="stretch", disabled=is_streaming):
            odr = st.session_state.get(LIVE_ODR_KEY, 100)
            send_command(control_port, baud, f"adpd ppg freq {odr}", response_timeout_s=2.0)
            launch_live_stream(
                st.session_state,
                control_port=control_port,
                stream_port=stream_port,
                baud=baud,
                num_samples=int(st.session_state.get(LIVE_SAMPLE_COUNT_KEY, 10_000)),
                slot=st.session_state.get(LIVE_SLOT_KEY, "slota"),
            )
            _log(
                f"Live feed start: control {control_port} / stream {stream_port} @ {odr} Hz",
                "info",
            )
            st.rerun()
    with act2:
        if st.button("Stop live feed", width="stretch", disabled=not is_streaming):
            stop_live_session()
            _log("Live feed stop requested", "warn")
            st.rerun()

    if status["tone"] == "error":
        st.error(status["detail"])
    elif status["tone"] == "success":
        st.success(status["detail"])
    else:
        st.info(status["detail"])
    st.caption(status["action_hint"])


def _sec(label: str):
    """Render a small-caps section label that visually groups related commands."""
    st.markdown(f'<div class="cmd-section">{label}</div>', unsafe_allow_html=True)


def _btn_row(commands: list[tuple[str, str]], key_prefix: str, n_cols: int = 5):
    """Render zero-arg commands as a compact button row.

    commands: [(button_label, full_command_string), ...]
    Each button tooltip shows the raw command string for transparency.
    Empty slots at the end of the last row are left blank (not hidden).
    """
    cols = st.columns(n_cols)
    for i, (label, cmd) in enumerate(commands):
        if cols[i % n_cols].button(
            label, key=f"btn_{key_prefix}_{i}",
            use_container_width=True, help=cmd,
        ):
            _send(cmd)
    # Pad remaining cells in the last partial row so the grid stays aligned
    remaining = (n_cols - len(commands) % n_cols) % n_cols
    for j in range(remaining):
        cols[(len(commands) + j) % n_cols].empty()


def _form_row_1(form_key: str, prefix: str, placeholder: str,
                is_number: bool = False, num_max: int = 100_000,
                num_default: int = 500, num_step: int = 50,
                select_opts: list | None = None, select_default=None):
    """Single-value command row: [prefix label] [input] [Run ↵]

    Uses st.form(enter_to_submit=True) so pressing Enter in the input field
    immediately fires _send() — no separate "Add" then "Send" steps needed.
    Supports three input flavours: free text, number spinner, or selectbox.
    """
    with st.form(form_key, enter_to_submit=True, border=False):
        c1, c2, c3 = st.columns([3, 5, 1.5])
        c1.markdown(f'<div class="cmd-prefix">{prefix}</div>', unsafe_allow_html=True)
        if select_opts is not None:
            # Selectbox variant — e.g. ODR choices for ppg freq
            default_idx = select_opts.index(select_default) if select_default in select_opts else 0
            val = c2.selectbox("v", select_opts, index=default_idx, label_visibility="collapsed")
        elif is_number:
            # Number spinner — e.g. sample count for ppg stream
            val = c2.number_input(
                "v", min_value=1, max_value=num_max,
                value=num_default, step=num_step,
                label_visibility="collapsed",
            )
        else:
            # Plain text — e.g. register address for adpd read
            val = c2.text_input("v", placeholder=placeholder, label_visibility="collapsed")
        sent = c3.form_submit_button("Run ↵", use_container_width=True, type="primary")
        if sent:
            v = str(val).strip() if val is not None else ""
            if v:
                _send(f"{prefix} {v}")
            else:
                st.warning("Enter a value first.")


def _form_row_2(form_key: str, prefix: str,
                ph1: str, ph2: str):
    """Two-value command row: [prefix label] [input1] [input2] [Run ↵]

    Used for commands that require two arguments (e.g. register write: addr + value).
    Both fields must be non-empty before the command is sent.
    """
    with st.form(form_key, enter_to_submit=True, border=False):
        c1, c2, c3, c4 = st.columns([2.5, 3, 3, 1.5])
        c1.markdown(f'<div class="cmd-prefix">{prefix}</div>', unsafe_allow_html=True)
        v1 = c2.text_input("v1", placeholder=ph1, label_visibility="collapsed")
        v2 = c3.text_input("v2", placeholder=ph2, label_visibility="collapsed")
        sent = c4.form_submit_button("Run ↵", use_container_width=True, type="primary")
        if sent:
            if v1.strip() and v2.strip():
                _send(f"{prefix} {v1.strip()} {v2.strip()}")
            else:
                st.warning("Both fields are required.")


def _form_row_3(form_key: str, prefix: str,
                ph1: str, ph2: str, ph3: str):
    """Three-value command row: [prefix] [v1] [v2] [v3] [Run ↵]

    Used for commands requiring three arguments — e.g. adpd gpio set <idx> <mode> <out_sel>.
    All three fields must be non-empty before the command is sent.
    """
    with st.form(form_key, enter_to_submit=True, border=False):
        c1, c2, c3, c4, c5 = st.columns([2.5, 2, 2, 2, 1.5])
        c1.markdown(f'<div class="cmd-prefix">{prefix}</div>', unsafe_allow_html=True)
        v1 = c2.text_input("v1", placeholder=ph1, label_visibility="collapsed")
        v2 = c3.text_input("v2", placeholder=ph2, label_visibility="collapsed")
        v3 = c4.text_input("v3", placeholder=ph3, label_visibility="collapsed")
        sent = c5.form_submit_button("Run ↵", use_container_width=True, type="primary")
        if sent:
            if v1.strip() and v2.strip() and v3.strip():
                _send(f"{prefix} {v1.strip()} {v2.strip()} {v3.strip()}")
            else:
                st.warning("All three fields are required.")


def _form_ppg_stream(form_key: str, slot: str, binary: bool):
    """PPG stream row: [prefix] [count] [HR channel] [Run ↵]

    Slot selector determines available HR channel options:
      slota  → sAch1–sAch4
      slotab → sAch1–sAch4 + sBch1–sBch4

    If an HR channel is selected, appends `hr on <ch>` to the command so the
    firmware's DSP pipeline (Hampel → Bandpass → Peak Detection) runs inline
    and adds Peak/HR columns to the stream output.

    form_key includes slot so Streamlit re-renders the form when slot changes.
    """
    verb   = "stream-bin" if binary else "stream"
    prefix = f"adpd ppg {slot} {verb}"

    # Build HR channel options for the current slot
    hr_opts = ["— no HR", "sAch1", "sAch2", "sAch3", "sAch4"]
    if slot == "slotab":
        hr_opts += ["sBch1", "sBch2", "sBch3", "sBch4"]

    with st.form(form_key, enter_to_submit=True, border=False):
        c1, c2, c3, c4 = st.columns([3.5, 3, 3, 1.5])
        c1.markdown(f'<div class="cmd-prefix">{prefix}</div>', unsafe_allow_html=True)
        count = c2.number_input(
            "n", min_value=1, max_value=100_000,
            value=500, step=50,
            label_visibility="collapsed",
            help="Number of samples to stream",
        )
        hr_ch = c3.selectbox(
            "hr", hr_opts,
            label_visibility="collapsed",
            help="Add inline HR detection (requires a channel selection)",
        )
        sent = c4.form_submit_button("Run ↵", use_container_width=True, type="primary")
        if sent:
            cmd = f"{prefix} {int(count)}"
            if hr_ch != "— no HR":
                # Firmware DSP pipeline activates when `hr on <channel>` is present
                cmd += f" hr on {hr_ch}"
            _send(cmd)


def _render_terminal_panel():
    """Scrollable terminal-style output panel for command history.

    Rendered via st.components.v1.html so that JavaScript can auto-scroll
    the div to the bottom after each new entry — giving a real terminal feel.
    Each history entry is colour-coded:
      - sent command  : blue
      - ok response   : green
      - error response: red
    """
    history = st.session_state.get("_cmd_history", [])

    # Header row: label + clear button
    hc1, hc2 = st.columns([5, 1])
    hc1.markdown('<div class="console-label">Output</div>', unsafe_allow_html=True)
    with hc2:
        if st.button("Clear", key="clear_terminal", help="Clear terminal history",
                     use_container_width=True):
            st.session_state.pop("_cmd_history", None)
            st.rerun()

    # Build the inner HTML — each entry is sent-command + response lines
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if not history:
        body = (
            "<span style='color:#484f58;font-style:italic'>"
            "No output yet — run a command."
            "</span>"
        )
    else:
        parts = []
        for entry in history:
            ts   = _esc(entry.get("ts", ""))
            cmd  = _esc(entry.get("cmd", ""))
            text = entry.get("text", "")
            ok   = entry.get("ok", True)

            # Sent command line
            parts.append(
                f'<div>'
                f'<span class="t-ts">[{ts}]</span> '
                f'<span class="t-prompt">&gt;&gt;</span> '
                f'<span class="t-send">{cmd}</span>'
                f'</div>'
            )

            # Response: split multi-line output into individual rows
            resp_cls = "t-ok" if ok else "t-err"
            if text and text != "(no response)":
                for line in _esc(text).splitlines():
                    if line.strip():
                        parts.append(f'<div><span class="{resp_cls}">   {line}</span></div>')
            elif text == "(no response)":
                parts.append('<div><span class="t-dim">   (no response within timeout)</span></div>')

            parts.append('<div class="t-gap"></div>')

        body = "\n".join(parts)

    html = f"""
    <style>
      #console-out {{
        background   : #0d1117;
        color        : #c9d1d9;
        font-family  : 'SFMono-Regular', 'Consolas', 'Liberation Mono', monospace;
        font-size    : 0.76rem;
        line-height  : 1.55;
        padding      : 0.7rem 0.9rem;
        border-radius: 6px;
        border       : 1px solid rgba(255,255,255,0.08);
        height       : 460px;
        overflow-y   : auto;
        word-break   : break-all;
        white-space  : pre-wrap;
        box-sizing   : border-box;
      }}
      .t-send   {{ color: #79c0ff; }}
      .t-ok     {{ color: #3fb950; }}
      .t-err    {{ color: #f85149; }}
      .t-ts     {{ color: #3d444d; font-size: 0.68rem; }}
      .t-prompt {{ color: #3d444d; }}
      .t-dim    {{ color: #484f58; font-style: italic; }}
      .t-gap    {{ height: 0.4rem; }}
    </style>
    <div id="console-out">{body}</div>
    <script>
      /* Auto-scroll to bottom so newest entry is always visible */
      var el = document.getElementById('console-out');
      if (el) el.scrollTop = el.scrollHeight;
    </script>
    """
    # height matches the terminal div height + label row + small buffer
    st.components.v1.html(html, height=490, scrolling=False)


def _render_command_console():
    st.subheader("3. Run Shell Commands")
    st.caption("Use the curated command palette for common tasks, or fall back to a raw command when needed.")

    # Two-column layout: terminal output on the left, command palette on the right
    col_term, col_palette = st.columns([2, 3])

    with col_term:
        _render_terminal_panel()

    with col_palette:
        # ── Response timeout — always visible at top of palette ───────────────
        # _send() reads this on every call; no need to hunt for it.
        st.number_input(
            "Response timeout (s)",
            min_value=0.5, max_value=30.0, value=3.0, step=0.5,
            key="serial_resp_timeout",
        )

        # ─────────────────────────────────────────────────────────────────────
        # UTILITIES — board-level shell helpers and quick status commands
        # ─────────────────────────────────────────────────────────────────────
        _sec("Explore")
        _btn_row([
            ("help",       "help"),
            ("help adpd",  "help adpd"),
            ("help ppg",   "help adpd ppg"),
            ("help eeprom","help eeprom"),
            ("help rtos",  "help rtos"),
            ("reset",      "reset"),
            ("rtos stats", "rtos stats"),  # RTOS task state, priority, stack HWM
            ("scan i2c",   "scan i2c"),
            ("scan spi",   "scan spi"),
        ], "explore", n_cols=4)

        # ─────────────────────────────────────────────────────────────────────
        # PPG PROFILES — inspect and reset the mutable in-RAM profile set
        # ─────────────────────────────────────────────────────────────────────
        _sec("PPG Profiles")
        _btn_row([
            ("ppg list",     "adpd ppg list"),
            ("slota show",   "adpd ppg slota show"),
            ("slotab show",  "adpd ppg slotab show"),
            ("slota2 show",  "adpd ppg slota2 show"),
            ("slota reset",  "adpd ppg slota reset"),
        ], "ppg_profiles", n_cols=4)

        # ─────────────────────────────────────────────────────────────────────
        # PPG START & CAPTURE — probe, set ODR, start, and stream
        # ─────────────────────────────────────────────────────────────────────
        _sec("PPG Start & Capture")
        _btn_row([
            ("slota start", "adpd ppg slota start"),
            ("slotab start", "adpd ppg slotab start"),
            ("slota2 start", "adpd ppg slota2 start"),
            ("stop",         "adpd ppg stop"),
        ], "ppg_ctrl", n_cols=4)

        _form_row_1("f_ppg_freq", "adpd ppg freq",
                    placeholder="", select_opts=_ODR_OPTIONS, select_default=100)

        # ─────────────────────────────────────────────────────────────────────
        # SENSOR DIAGNOSTICS — register access, GPIO, and calibration helpers
        # ─────────────────────────────────────────────────────────────────────
        _sec("Probe & Inspect")
        # Probe + diagnostic template reads (slota and slotab now both supported)
        _btn_row([
            ("probe",        "adpd probe"),       # SPI comms check — expects chip ID 0x01C6
            ("probe sdk",    "adpd probe sdk"),   # SDK-level initialisation path check
            ("read slota",   "adpd read slota"),  # Global + Slot A config vs expected values
            ("read slotab",  "adpd read slotab"), # Global + Slot A+B config vs expected values
            ("adpd reset",   "adpd reset"),       # Software-reset the ADPD7000 sensor
            ("gpio read",    "adpd gpio read"),   # Show current GPIO0-2 configuration
        ], "adpd_probe", n_cols=4)  # 6 items → 2 rows of 4

        # Calibration — each subcommand is a self-contained procedure
        _btn_row([
            ("calib clk", "adpd calib clk"),  # Measure LF oscillator accuracy vs MCU clock
            ("calib led", "adpd calib led"),  # LED drive calibration
            ("calib tia", "adpd calib tia"),  # TIA gain calibration
        ], "adpd_calib", n_cols=4)

        # Register read: range form — e.g. "adpd read 0000 001F" (first 32 registers)
        _form_row_2("f_adpd_read_range", "adpd read",
                    ph1="start  e.g. 0000", ph2="end  e.g. 001F")
        # Register read: single register — e.g. "adpd read 0x128" (LED_POW12)
        _form_row_1("f_adpd_read",       "adpd read",
                    placeholder="register  e.g. 0x128")
        # Register write — use with caution while PPG is running
        _form_row_2("f_adpd_write",      "adpd write",
                    ph1="register  e.g. 0x128", ph2="value  e.g. 0x000A")
        # GPIO set — idx (0-2), mode (output mode), out_sel (function select hex)
        _form_row_3("f_adpd_gpio_set",   "adpd gpio set",
                    ph1="idx  0-2", ph2="mode  e.g. 2", ph3="out_sel  e.g. 0x17")

        # ─────────────────────────────────────────────────────────────────────
        # TRANSPORT — enable/disable USB and UART physical interfaces
        # ─────────────────────────────────────────────────────────────────────
        _sec("Interfaces")
        _btn_row([
            ("usb on",    "interface usb on"),
            ("usb off",   "interface usb off"),
            ("uart on",   "interface uart on"),
            ("uart off",  "interface uart off"),
        ], "iface", n_cols=4)

        # ─────────────────────────────────────────────────────────────────────
        # STORAGE — non-volatile storage read/write for calibration data
        # ─────────────────────────────────────────────────────────────────────
        _sec("Storage & Bus")
        _btn_row([
            ("info", "eeprom info"),
            ("test", "eeprom test"),
        ], "ee", n_cols=4)

        _form_row_1("f_ee_read",  "eeprom read",  placeholder="address  e.g. 0x0100")
        _form_row_2("f_ee_write", "eeprom write",
                    ph1="address  e.g. 0x0100", ph2="value  e.g. 0xFF")

        # ─────────────────────────────────────────────────────────────────────
        # CUSTOM COMMAND — free-form entry for anything not covered above
        # ─────────────────────────────────────────────────────────────────────
        _sec("Custom command")
        with st.form("f_custom", enter_to_submit=True, border=False):
            cc1, cc2 = st.columns([9, 1.5])
            custom = cc1.text_input("custom", placeholder="enter any command…",
                                    label_visibility="collapsed")
            sent = cc2.form_submit_button("Run ↵", use_container_width=True, type="primary")
            if sent and custom.strip():
                _send(custom.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Stream capture
# ─────────────────────────────────────────────────────────────────────────────

def _render_stream_capture():
    active_control = st.session_state.get("conn_control_port", st.session_state.get("conn_port", ""))
    active_stream = st.session_state.get("conn_stream_port", "")
    active_baud = st.session_state.get("conn_baud", 115200)

    st.subheader("4. Capture Raw Stream")
    st.caption(f"Control: `{active_control or '—'}`  |  Stream: `{active_stream or '—'}`")

    bs1, bs2, bs3 = st.columns([2, 2, 2])
    with bs1:
        capture_format = st.radio(
            "Format",
            ["Binary framed", "Human-readable text"],
            horizontal=True,
            key="capture_format",
        )
    with bs2:
        n_samples = st.number_input("Samples", min_value=10, max_value=100_000,
                                    value=500, step=50, key="capture_n_samples")
    with bs3:
        stream_timeout = st.number_input("Timeout (s)", min_value=5.0, max_value=120.0,
                                         value=30.0, step=5.0, key="capture_timeout")
    live_mode = False
    if capture_format == "Binary framed":
        live_mode = st.toggle(
            "Live chart",
            value=True,
            key="capture_live_mode",
            help="Update the parsed channel chart while binary frames arrive.",
        )

    if capture_format == "Binary framed":
        st.caption(
            "Reads framed binary payloads from the stream port and renders parsed channels, HR, and exports."
        )
    else:
        st.caption(
            "Reads human-readable text lines from the stream port for quick sanity checks and terminal-style inspection."
        )

    # Slot and HR selectors — must match what was set via the PPG Control section
    bs4, bs5 = st.columns([2, 3])
    with bs4:
        cap_slot = st.radio(
            "Slot", ["slota", "slotab", "slota2"], horizontal=True, key="capture_slot",
            help="slota/slota2 = 4 ch (20 B/frame) · slotab = 8 ch (36 B/frame)",
        )
    with bs5:
        hr_opts = ["— no HR", "sAch1", "sAch2", "sAch3", "sAch4"]
        if cap_slot == "slotab":
            hr_opts += ["sBch1", "sBch2", "sBch3", "sBch4"]
        cap_hr_ch = st.selectbox(
            "HR channel", hr_opts, key="capture_hr_ch",
            help="Add inline DSP HR detection; adds float32 BPM + uint32 Peak to each frame",
        )
    # Normalise — None means no HR suffix in the command
    hr_channel = None if cap_hr_ch == "— no HR" else cap_hr_ch

    is_capturing = st.session_state.get("capture_streaming", False)
    capture_action_label = "Capture Binary Stream" if capture_format == "Binary framed" else "Capture Text Stream"
    btn1, btn2 = st.columns([3, 1])
    with btn1:
        capture_btn = st.button(capture_action_label, type="primary", width="stretch",
                                key="capture_btn", disabled=is_capturing)
    with btn2:
        stop_btn = st.button("Stop", type="secondary", width="stretch",
                             key="capture_stop_btn", disabled=not is_capturing)

    if stop_btn and is_capturing:
        # Signal the worker thread to exit cleanly at the next iteration
        ev = st.session_state.get("capture_stop_event")
        if ev:
            ev.set()
        _log("Capture stopped by user", "warn")

    if capture_btn and not is_capturing:
        _start_capture(active_control, active_stream, active_baud, int(n_samples),
                       float(stream_timeout), live_mode, cap_slot, hr_channel,
                       binary=(capture_format == "Binary framed"))
        st.rerun()

    # run_every=0.5 s while streaming for live updates; None when idle (no polling)
    refresh = 0.5 if is_capturing and live_mode else None

    @st.fragment(run_every=refresh)
    def _capture_fragment():
        shared    = st.session_state.get("_sshared_capture", {})
        capturing = st.session_state.get("capture_streaming", False)
        capture_mode = shared.get("format", st.session_state.get("capture_format", "Binary framed"))
        buf       = shared.get("buf", [])
        text_lines = shared.get("text_lines", [])
        raw_buf   = shared.get("raw", bytearray())
        log_buf   = shared.get("log", [])
        error     = shared.get("error")
        done      = shared.get("done", False)

        # Worker sets done=True when it exits; flip the UI flag here on the main thread
        if done and capturing:
            st.session_state["capture_streaming"] = False
            capturing = False

        live_window_s = None
        if capture_mode == "Binary framed" and capturing and st.session_state.get("capture_live_mode"):
            live_window_s = st.radio(
                "Display window",
                [5, 10],
                horizontal=True,
                format_func=lambda value: f"{value} s",
                key="capture_window_s",
            )

        item_count = len(buf) if capture_mode == "Binary framed" else len(text_lines)
        item_unit = "samples" if capture_mode == "Binary framed" else "lines"

        if capturing or (done and item_count):
            requested = st.session_state.get("capture_n_samples", 1)
            pct = min(int(item_count / max(requested, 1) * 100), 100)
            stopped = error or st.session_state.get(
                "capture_stop_event", threading.Event()
            ).is_set()
            label = (f"Receiving… {item_count}/{requested} {item_unit}"
                     if capturing else
                     f"{'Stopped' if stopped else 'Complete'} — {item_count} {item_unit}")
            st.progress(pct, text=label)

        if error:
            st.error(f"Stream error: {error}")

        if capture_mode == "Binary framed" and buf and (capturing or done):
            _render_capture_chart(buf, key="live", window_s=live_window_s if capturing else None)
            _render_capture_metrics(buf, raw_buf, key_sfx="")
        elif capture_mode == "Human-readable text" and text_lines:
            _render_text_capture(text_lines, raw_buf, key="live_text")

        # Finalise exactly once — copy shared buffer to stable session state keys
        if done and (buf or text_lines) and not capturing and not st.session_state.get("_capture_finalised"):
            _finalise_capture(buf, text_lines, raw_buf, log_buf, error, capture_mode)
            st.rerun()

        if log_buf:
            with st.expander("Stream log"):
                st.code("\n".join(log_buf), language="text")

    _capture_fragment()

    # ── Static display of last completed capture ───────────────────────────────
    # Shown when no capture is in progress and the shared buffer has been cleared.
    # This lets the researcher examine data after the live fragment stops updating.
    samples   = st.session_state.get("_capture_last_samples", [])
    text_lines = st.session_state.get("_capture_last_text_lines", [])
    raw_bytes = st.session_state.get("_capture_last_raw", b"")
    log       = st.session_state.get("_capture_last_log", [])
    capture_mode = st.session_state.get("_capture_last_format", st.session_state.get("capture_format", "Binary framed"))
    show_static = (
        (samples or text_lines) and not is_capturing
        and not st.session_state.get("_sshared_capture", {}).get("done")
    )
    if show_static:
        if capture_mode == "Binary framed" and samples:
            _render_capture_chart(samples, key="static")
            _render_capture_metrics(samples, raw_bytes, key_sfx="_s")
        elif capture_mode == "Human-readable text" and text_lines:
            _render_text_capture(text_lines, raw_bytes, key="static_text")
        if log:
            with st.expander("Stream log"):
                st.code("\n".join(log), language="text")
        if st.button("Clear Captured Data", key="capture_clear"):
            for k in ("_capture_last_samples", "_capture_last_text_lines", "_capture_last_raw", "_capture_last_log", "_capture_last_format",
                      "_sshared_capture", "_capture_finalised"):
                st.session_state.pop(k, None)
            st.rerun()


def _start_capture(control_port, stream_port, baud, n_samples, timeout_s, live_mode,
                   slot: str = "slota", hr_channel: str | None = None, binary: bool = True):
    """Kick off the binary stream worker in a daemon thread.

    A shared dict is used for thread-safe communication: the worker appends
    to buf/raw/log and sets done=True on exit.  The Streamlit fragment polls
    this dict every 0.5 s via run_every.

    slot/hr/mode are forwarded to the serial helper so the correct stream command is sent.
    """
    stop_ev = threading.Event()
    shared: dict = {
        "buf": [],
        "text_lines": [],
        "raw": bytearray(),
        "log": [],
        "error": None,
        "done": False,
        "format": "Binary framed" if binary else "Human-readable text",
    }
    st.session_state["capture_streaming"]  = True
    st.session_state["capture_stop_event"] = stop_ev
    st.session_state["_sshared_capture"]   = shared
    st.session_state["_capture_finalised"] = False

    def _worker():
        try:
            stream_iter = (
                stream_binary_live_dual_port(
                    control_port, stream_port, baud, n_samples, stream_timeout_s=timeout_s,
                    slot=slot, hr_channel=hr_channel,
                )
                if binary else
                stream_text_live_dual_port(
                    control_port, stream_port, baud, n_samples, stream_timeout_s=timeout_s,
                    slot=slot, hr_channel=hr_channel,
                )
            )
            for new_s, new_raw, new_log, _ in stream_iter:
                if stop_ev.is_set():
                    break
                if binary:
                    shared["buf"].extend(new_s)
                else:
                    shared["text_lines"].extend(new_s)
                shared["raw"].extend(new_raw)
                shared["log"].extend(new_log)
                # Surface stream-level errors (e.g. frame sync lost) immediately
                for ll in new_log:
                    if ll.startswith("ERROR:"):
                        shared["error"] = ll[6:].strip()
                        stop_ev.set()
                        break
        except Exception as exc:
            shared["error"] = str(exc)
        finally:
            # Always mark done so the fragment knows the worker has exited
            shared["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    _log(
        f"Capture start: {n_samples} {'binary samples' if binary else 'text lines'} via control {control_port} / stream {stream_port} "
        f"({'live' if live_mode else 'batch'})",
        "info",
    )


def _select_capture_window(buf: list, window_s: int | None) -> list:
    """Return only the most recent N seconds of capture samples."""
    if not buf or not window_s:
        return buf
    latest_ts = buf[-1][0]
    min_ts = latest_ts - window_s * 1000
    return [sample for sample in buf if sample[0] >= min_ts]


def _render_capture_chart(buf: list, key: str, window_s: int | None = None):
    """Plot ADC channels (and optionally HR) against elapsed time.

    Adapts to variable sample tuple length:
      5 fields  → Slot A, no HR  (4 channels)
      7 fields  → Slot A + HR   (4 channels + HR secondary axis + Peak markers)
      9 fields  → Slot AB, no HR (8 channels)
      11 fields → Slot AB + HR  (8 channels + HR secondary axis + Peak markers)

    uirevision="capture" keeps zoom/pan state stable across fragment reruns.
    """
    view_buf = _select_capture_window(buf, window_s)
    ts_ms   = [s[0] for s in view_buf]
    n_tuple = len(view_buf[0])
    # Determine mode from tuple length
    n_ch    = {5: 4, 7: 4, 9: 8, 11: 8}.get(n_tuple, 4)
    has_hr  = n_tuple in (7, 11)

    fig = go.Figure()

    # ADC channel traces — colour/visibility from _CH_INFO lookup
    for i in range(n_ch):
        label, color, visible = _CH_INFO[i]
        fig.add_trace(go.Scatter(
            x=ts_ms, y=[s[i + 1] for s in view_buf],
            mode="lines", name=label,
            line=dict(color=color, width=1),
            visible=visible,
        ))

    if has_hr:
        hr_idx   = n_ch + 1   # float32 HR BPM field
        peak_idx = n_ch + 2   # uint32 peak flag
        hr_vals  = [s[hr_idx] for s in view_buf]
        # Peak timestamps — only points where peak == 1
        peak_ts  = [ts_ms[i] for i, s in enumerate(view_buf) if s[peak_idx]]
        peak_hr  = [s[hr_idx] for s in view_buf if s[peak_idx]]

        # HR on a secondary y-axis so it doesn't compress the ADC scale
        fig.add_trace(go.Scatter(
            x=ts_ms, y=hr_vals, mode="lines", name="HR (BPM)",
            line=dict(color="#00CC96", width=1.5, dash="dot"),
            yaxis="y2",
        ))
        if peak_ts:
            fig.add_trace(go.Scatter(
                x=peak_ts, y=peak_hr, mode="markers", name="Peak",
                marker=dict(color="#EF553B", size=6, symbol="circle"),
                yaxis="y2",
            ))
        fig.update_layout(
            yaxis2=dict(
                title="HR (BPM)", overlaying="y", side="right",
                showgrid=False, range=[30, 180],
            )
        )

    fig.update_layout(
        xaxis_title="Time (ms from stream start)",
        yaxis_title="ADC value",
        margin=dict(l=0, r=0, t=30, b=0),
        height=360,
        legend=dict(orientation="h", y=1.07),
        yaxis=dict(autorange=True),
    )
    if window_s and len(ts_ms) > 1:
        fig.update_xaxes(range=[ts_ms[0], ts_ms[-1]])
    else:
        fig.update_layout(uirevision="capture")
    st.plotly_chart(fig, use_container_width=True, key=f"chart_capture_{key}")
    slot_label = "Slot AB (8-ch)" if n_ch == 8 else "Slot A (4-ch)"
    hr_label   = " · HR + Peak on right axis" if has_hr else ""
    st.caption(f"{slot_label} · Ch3/Ch4 = PPG · Ch1/Ch2 = ambient · toggle traces in legend{hr_label}")


def _render_capture_metrics(buf: list, raw_bytes, key_sfx: str):
    """Show metric strip and download buttons — adapts to 4-ch or 8-ch captures."""
    n_tuple = len(buf[0])
    n_ch    = {5: 4, 7: 4, 9: 8, 11: 8}.get(n_tuple, 4)
    has_hr  = n_tuple in (7, 11)

    ts_ms = [s[0] for s in buf]
    ch3   = [s[3] for s in buf]   # Ch3 is always the primary PPG channel
    dur   = (ts_ms[-1] - ts_ms[0]) / 1000 if len(ts_ms) > 1 else 0

    cols = st.columns(6 if has_hr else 5)
    cols[0].metric("Samples",  len(buf))
    cols[1].metric("Duration", f"{dur:.2f} s")
    cols[2].metric("Ch3 mean", f"{int(sum(ch3) / len(ch3)):,}")
    cols[3].metric("Ch3 min",  f"{min(ch3):,}")
    cols[4].metric("Ch3 max",  f"{max(ch3):,}")
    if has_hr:
        hr_vals = [s[n_ch + 1] for s in buf]
        # Mean HR ignoring zero values (transient at stream start)
        valid_hr = [v for v in hr_vals if v > 0]
        cols[5].metric("Mean HR", f"{sum(valid_hr) / len(valid_hr):.1f} bpm" if valid_hr else "—")

    # Build export DataFrame — column count depends on slot/HR config
    row: dict = {"timestamp_ms": ts_ms}
    for i in range(n_ch):
        label, _, _ = _CH_INFO[i]
        col_name = label.replace(" ", "_").lower()
        row[col_name] = [s[i + 1] for s in buf]
    if has_hr:
        row["hr_bpm"]  = [s[n_ch + 1] for s in buf]
        row["hr_peak"] = [s[n_ch + 2] for s in buf]
    df = pd.DataFrame(row)

    rb = bytes(raw_bytes)
    bytes_per_frame = n_tuple * 4 - (4 if has_hr else 0) + (8 if has_hr else 0)
    # Actual frame bytes = payload size (already reflected in raw_bytes)
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button("Export Parsed CSV", df.to_csv(index=False).encode(),
                           "capture.csv", "text/csv",
                           key=f"dl_cap_csv{key_sfx}", width="stretch")
    with dl2:
        # Raw binary preserves original framed payload for offline analysis
        st.download_button("Export Raw Binary (.bin)", rb,
                           "capture.bin", "application/octet-stream",
                           key=f"dl_cap_bin{key_sfx}", width="stretch",
                           help=f"{len(rb):,} bytes raw payload")


def _render_text_capture(lines: list[str], raw_bytes: bytes | bytearray, key: str):
    """Render captured text lines and text-mode exports."""
    st.text_area(
        "Captured text stream",
        value="\n".join(lines[-200:]),
        height=320,
        key=f"text_capture_{key}",
    )
    cols = st.columns(3)
    cols[0].metric("Lines", len(lines))
    cols[1].metric("Bytes", len(raw_bytes))
    cols[2].metric("Preview", min(len(lines), 200))

    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "Export Text (.txt)",
            "\n".join(lines).encode(),
            "capture.txt",
            "text/plain",
            key=f"dl_cap_txt_{key}",
            width="stretch",
        )
    with dl2:
        st.download_button(
            "Export Raw Bytes (.bin)",
            bytes(raw_bytes),
            "capture_text_raw.bin",
            "application/octet-stream",
            key=f"dl_cap_text_raw_{key}",
            width="stretch",
        )


def _finalise_capture(buf, text_lines, raw_buf, log_buf, error, capture_mode: str):
    """Copy the shared mutable buffers into stable session state keys.

    Called exactly once per capture run (guarded by _capture_finalised flag).
    After this, the shared dict can be cleared without losing captured data.
    """
    st.session_state["_capture_last_samples"] = list(buf)
    st.session_state["_capture_last_text_lines"] = list(text_lines)
    st.session_state["_capture_last_raw"]     = bytes(raw_buf)
    st.session_state["_capture_last_log"]     = list(log_buf)
    st.session_state["_capture_last_format"]  = capture_mode
    if error:
        _log(f"Capture ended with error: {error}", "error")
    elif st.session_state.get("capture_stop_event", threading.Event()).is_set():
        kept = len(text_lines) if capture_mode == "Human-readable text" else len(buf)
        unit = "lines" if capture_mode == "Human-readable text" else "samples"
        _log(f"Capture stopped: {kept} {unit} kept", "warn")
    else:
        kept = len(text_lines) if capture_mode == "Human-readable text" else len(buf)
        unit = "lines" if capture_mode == "Human-readable text" else "samples"
        _log(f"Capture complete: {kept} {unit}", "ok")
    st.session_state["_capture_finalised"] = True
