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

from usb_serial import (
    SERIAL_AVAILABLE, list_serial_ports, describe_ports,
    find_port_owner, force_release_port, test_connection,
    send_command, stream_binary_live,
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
/* Response area — green left border for ok, red for error */
.resp-ok  { border-left: 3px solid #00CC96; padding: 0.35rem 0.75rem;
            background: rgba(0,204,150,0.06); border-radius: 0 4px 4px 0;
            margin-bottom: 0.4rem; }
.resp-err { border-left: 3px solid #EF553B; padding: 0.35rem 0.75rem;
            background: rgba(239,85,59,0.06); border-radius: 0 4px 4px 0;
            margin-bottom: 0.4rem; }
.resp-cmd { font-family: monospace; font-size: 0.8rem; opacity: 0.9; }
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
    st.header("USB Serial")

    if not SERIAL_AVAILABLE:
        st.error("`pyserial` is not installed — `pip install pyserial`")
        st.stop()

    _render_connection_panel()

    # Command console and binary capture only appear once connected
    if not st.session_state.get("conn_connected"):
        return

    st.divider()
    _render_command_console()
    st.divider()
    _render_binary_capture()


# ─────────────────────────────────────────────────────────────────────────────
# Connection panel
# ─────────────────────────────────────────────────────────────────────────────

def _render_connection_panel():
    is_connected = st.session_state.get("conn_connected", False)
    active_port  = st.session_state.get("conn_port", "")
    active_baud  = st.session_state.get("conn_baud", 115200)

    ports = list_serial_ports()
    # Mirror detected ports to the browser console for USB debugging
    st.components.v1.html(
        f"<script>console.log('[USB] ports:', {ports});</script>", height=1
    )

    pc1, pc2, pc3, pc4 = st.columns([3, 2, 1, 1])

    with pc1:
        if ports:
            # Pre-select the previously used port if it is still present
            idx = ports.index(active_port) if active_port in ports else 0
            port = st.selectbox("Port", ports, index=idx,
                                key="tab_conn_port", disabled=is_connected)
        else:
            # No ports enumerated — fall back to free-text entry
            port = st.text_input("Port (manual)",
                                 value=active_port or "/dev/tty.usbmodem101",
                                 key="tab_conn_port_txt", disabled=is_connected)

    with pc2:
        baud_opts = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
        baud_idx  = baud_opts.index(active_baud) if active_baud in baud_opts else 4
        baud = st.selectbox("Baud", baud_opts, index=baud_idx,
                            key="tab_conn_baud", disabled=is_connected)

    with pc3:
        if not is_connected:
            if st.button("Connect", type="primary", width="stretch", key="tab_conn_btn"):
                with st.spinner(f"Connecting to {port}…"):
                    chk = test_connection(port, baud)
                if chk.ok:
                    # Write shared conn_* keys — sidebar status panel reads these
                    st.session_state.update(conn_connected=True, conn_port=port, conn_baud=baud)
                    st.session_state.pop("_tab_conn_err", None)
                    _log(f"Connected — {port} @ {baud}", "ok")
                else:
                    st.session_state["_tab_conn_err"] = chk.error or "Connection failed"
                    _log(f"Connect failed: {chk.error}", "error")
                st.rerun()
        else:
            if st.button("Disconnect", type="secondary", width="stretch", key="tab_disconn_btn"):
                st.session_state["conn_connected"] = False
                st.session_state.pop("_tab_conn_err", None)
                _log(f"Disconnected from {active_port}", "info")
                st.rerun()

    with pc4:
        if st.button("Refresh", width="stretch", key="tab_refresh_btn", disabled=is_connected):
            st.rerun()

    # ── Status badge ──────────────────────────────────────────────────────────
    last_err = st.session_state.get("_tab_conn_err", "")
    if is_connected:
        desc_map = {p["device"]: p["description"] for p in describe_ports()}
        st.success(f"Connected — **{active_port}** @ {active_baud}  |  {desc_map.get(active_port, active_port)}")
    else:
        if last_err and "PORT_BUSY" in last_err:
            # Port is held by another process — offer a force-release path
            owner = find_port_owner(port)
            owner_s = f"held by **{owner[1]}** (PID {owner[0]})" if owner else "owner unknown"
            st.error(f"Port busy — {owner_s}")
            fc1, fc2 = st.columns([3, 1])
            fc1.caption("Another process has the port open. Force-disconnect terminates it and reconnects.")
            with fc2:
                if st.button("Force & Reconnect", type="primary", width="stretch", key="tab_force_btn"):
                    with st.spinner("Releasing port…"):
                        rel = force_release_port(port)
                    if rel.ok:
                        _log(f"Force release: {rel.response}", "warn")
                        # Brief pause to let the OS reclaim the port before retrying
                        time.sleep(0.5)
                        chk2 = test_connection(port, baud)
                        if chk2.ok:
                            st.session_state.update(conn_connected=True, conn_port=port, conn_baud=baud)
                            st.session_state.pop("_tab_conn_err", None)
                            _log(f"Reconnected after force release — {port} @ {baud}", "ok")
                        else:
                            st.session_state["_tab_conn_err"] = chk2.error or ""
                            _log(f"Reconnect failed: {chk2.error}", "error")
                    else:
                        _log(f"Force release failed: {rel.error}", "error")
                        st.session_state["_tab_conn_err"] = f"FORCE_FAILED: {rel.error}"
                    st.rerun()
        elif last_err:
            st.error(last_err)
        else:
            if ports:
                desc_map = {p["device"]: p["description"] for p in describe_ports()}
                if port in desc_map:
                    st.caption(f"Device: {desc_map[port]}")
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
    """Send a command and store the result in session state.

    Result is stored first, then st.rerun() is called so the response banner
    at the top of the console is populated on the very next render.  Without
    the rerun the Streamlit top-to-bottom execution order would render the
    response area *before* the button handler runs, leaving it blank.
    """
    port    = st.session_state.get("conn_port", "")
    baud    = st.session_state.get("conn_baud", 115200)
    timeout = st.session_state.get("serial_resp_timeout", 3.0)
    with st.spinner(f"`{cmd}`"):
        result = send_command(port, baud, cmd, response_timeout_s=timeout)
    if result.ok:
        _log(f">> {cmd}", "info")
        if result.response:
            # Truncate long responses in the log to avoid noise
            _log(f"<< {result.response[:120]}", "info")
        st.session_state["_cmd_last_response"] = {
            "cmd": cmd, "text": result.response or "(no response)", "ok": True,
        }
    else:
        _log(f"Error ({cmd}): {result.error}", "error")
        st.session_state["_cmd_last_response"] = {
            "cmd": cmd, "text": result.error or "Unknown error", "ok": False,
        }
    # Rerun so the response banner at the top of the console is immediately visible
    st.rerun()


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


def _render_command_console():
    st.subheader("Command Console")

    # ── Response timeout — top-level, not buried in an expander ───────────────
    # Placed first so it is always visible; value is read by _send() on every call.
    tc1, tc2, tc3 = st.columns([2, 1, 4])
    tc1.number_input(
        "Response timeout (s)",
        min_value=0.5, max_value=30.0, value=3.0, step=0.5,
        key="serial_resp_timeout",
    )
    with tc2:
        # Spacer aligns the button to the vertical midpoint of the number input
        st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
        if st.button("Clear response", key="clear_resp", help="Clear last response display"):
            st.session_state.pop("_cmd_last_response", None)
            st.rerun()

    # ── Last response banner — always at the top so eyes don't hunt for it ────
    resp = st.session_state.get("_cmd_last_response")
    if resp:
        css  = "resp-ok" if resp["ok"] else "resp-err"
        icon = "✓" if resp["ok"] else "✗"
        st.markdown(
            f'<div class="{css}"><span class="resp-cmd">{icon}  {resp["cmd"]}</span></div>',
            unsafe_allow_html=True,
        )
        if resp["text"] and resp["text"] != "(no response)":
            st.code(resp["text"], language="text")
        elif resp["text"] == "(no response)":
            st.caption("_(no response within timeout)_")

    # ─────────────────────────────────────────────────────────────────────────
    # SYSTEM — board-level utility commands
    # ─────────────────────────────────────────────────────────────────────────
    _sec("System")
    _btn_row([
        ("help",      "help"),
        ("reset",     "reset"),
        ("scan i2c",  "scan i2c"),
        ("scan spi",  "scan spi"),
    ], "sys", n_cols=4)

    # ─────────────────────────────────────────────────────────────────────────
    # PPG CONTROL — start/stop streaming and ODR/sample-count configuration
    # ─────────────────────────────────────────────────────────────────────────
    _sec("PPG Control")

    # Slot selector — drives the prefix for freq/stream/stream-bin commands.
    # slota = 4-channel (Ch1–4); slotab = 8-channel (Ch1–4 + Ch5–8).
    slot = st.radio(
        "Slot", ["slota", "slotab"], horizontal=True,
        key="ppg_slot_sel",
        help="slota = 4 channels (Slot A only) · slotab = 8 channels (Slot A + B)",
    )

    # Start buttons always show both slot variants; stop needs no slot prefix
    _btn_row([
        ("slota start", "adpd ppg slota start"),
        ("slotab start", "adpd ppg slotab start"),
        ("stop",         "adpd ppg stop"),
    ], "ppg_ctrl", n_cols=4)

    # ODR — query current setting (no arg) or set a new one (selectbox).
    # Command uses selected slot; form_key includes slot to force re-render on change.
    _btn_row([(f"freq?", f"adpd ppg {slot} freq")], f"ppg_freq_q_{slot}", n_cols=4)
    _form_row_1(f"f_ppg_freq_{slot}", f"adpd ppg {slot} freq",
                placeholder="", select_opts=_ODR_OPTIONS, select_default=100)

    # ASCII text stream — N samples, human-readable CSV over serial.
    # Optional HR column: appends `hr on <ch>` when a channel is selected.
    _form_ppg_stream(f"f_ppg_stream_{slot}",     slot, binary=False)

    # Binary framed stream — N × 20-byte frames (ts + ch1–4 uint32 LE).
    # Same HR option; use for high-speed capture and offline analysis.
    _form_ppg_stream(f"f_ppg_stream_bin_{slot}", slot, binary=True)

    # ─────────────────────────────────────────────────────────────────────────
    # ADPD DEVICE — register access and device diagnostics
    # ─────────────────────────────────────────────────────────────────────────
    _sec("ADPD Device")
    _btn_row([
        ("probe",       "adpd probe"),       # SPI comms check — expects chip ID 0x01C6
        ("probe sdk",   "adpd probe sdk"),   # SDK-level initialisation path check
        ("diag",        "adpd diag"),        # Current ODR, FIFO status, channel config
        ("read all",    "adpd read"),        # Dump first 32 registers (0x00–0x1F)
        ("read slota",  "adpd read slota"),  # Global + Slot A config vs expected values
        ("adpd reset",  "adpd reset"),       # Reset ADPD chip (not the MCU)
    ], "adpd", n_cols=4)  # 6 items → 2 rows of 4

    # Single-register read — e.g. "adpd read 0x128" (LED_POW12)
    _form_row_1("f_adpd_read",       "adpd read",
                placeholder="register  e.g. 0x128")
    # Address-range read — e.g. "adpd read 0010 0138" for a contiguous block
    _form_row_2("f_adpd_read_range", "adpd read",
                ph1="start  e.g. 0010", ph2="end  e.g. 0138")
    # Register write — use with caution while PPG is running
    _form_row_2("f_adpd_write",      "adpd write",
                ph1="register  e.g. 0x128", ph2="value  e.g. 0x000A")

    # ─────────────────────────────────────────────────────────────────────────
    # INTERFACE — enable/disable USB and UART physical interfaces
    # ─────────────────────────────────────────────────────────────────────────
    _sec("Interface")
    _btn_row([
        ("usb on",    "interface usb on"),
        ("usb off",   "interface usb off"),
        ("uart on",   "interface uart on"),
        ("uart off",  "interface uart off"),
    ], "iface", n_cols=4)

    # ─────────────────────────────────────────────────────────────────────────
    # EEPROM — non-volatile storage read/write for calibration data
    # ─────────────────────────────────────────────────────────────────────────
    _sec("EEPROM")
    _btn_row([
        ("info", "eeprom info"),
        ("test", "eeprom test"),
    ], "ee", n_cols=4)

    _form_row_1("f_ee_read",  "eeprom read",  placeholder="address  e.g. 0x0100")
    _form_row_2("f_ee_write", "eeprom write",
                ph1="address  e.g. 0x0100", ph2="value  e.g. 0xFF")

    # ─────────────────────────────────────────────────────────────────────────
    # CUSTOM COMMAND — free-form entry for anything not covered above
    # ─────────────────────────────────────────────────────────────────────────
    _sec("Custom command")
    with st.form("f_custom", enter_to_submit=True, border=False):
        cc1, cc2 = st.columns([9, 1.5])
        custom = cc1.text_input("custom", placeholder="enter any command…",
                                label_visibility="collapsed")
        sent = cc2.form_submit_button("Run ↵", use_container_width=True, type="primary")
        if sent and custom.strip():
            _send(custom.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Binary stream capture
# ─────────────────────────────────────────────────────────────────────────────

def _render_binary_capture():
    active_port = st.session_state.get("conn_port", "")
    active_baud = st.session_state.get("conn_baud", 115200)

    st.subheader("Binary Stream Capture")
    st.caption(
        "Sends `adpd ppg <slot> stream-bin N [hr on <ch>]` — "
        "Slot A: 20 bytes/frame · Slot AB: 36 bytes/frame · +HR: +8 bytes.  "
        "Ch3/Ch4 = PPG · Ch1/Ch2 = ambient."
    )

    bs1, bs2, bs3 = st.columns([2, 2, 1])
    with bs1:
        n_samples = st.number_input("Samples", min_value=10, max_value=100_000,
                                    value=500, step=50, key="capture_n_samples")
    with bs2:
        stream_timeout = st.number_input("Timeout (s)", min_value=5.0, max_value=120.0,
                                         value=30.0, step=5.0, key="capture_timeout")
    with bs3:
        live_mode = st.toggle("Live graph", value=True, key="capture_live_mode",
                              help="Update chart while data arrives")

    # Slot and HR selectors — must match what was set via the PPG Control section
    bs4, bs5 = st.columns([2, 3])
    with bs4:
        cap_slot = st.radio(
            "Slot", ["slota", "slotab"], horizontal=True, key="capture_slot",
            help="slota = 4 ch (20 B/frame) · slotab = 8 ch (36 B/frame)",
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
    btn1, btn2 = st.columns([3, 1])
    with btn1:
        capture_btn = st.button("Capture Stream", type="primary", width="stretch",
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
        _start_capture(active_port, active_baud, int(n_samples),
                       float(stream_timeout), live_mode, cap_slot, hr_channel)
        st.rerun()

    # run_every=0.5 s while streaming for live updates; None when idle (no polling)
    refresh = 0.5 if is_capturing and live_mode else None

    @st.fragment(run_every=refresh)
    def _capture_fragment():
        shared    = st.session_state.get("_sshared_capture", {})
        capturing = st.session_state.get("capture_streaming", False)
        buf       = shared.get("buf", [])
        raw_buf   = shared.get("raw", bytearray())
        log_buf   = shared.get("log", [])
        error     = shared.get("error")
        done      = shared.get("done", False)

        # Worker sets done=True when it exits; flip the UI flag here on the main thread
        if done and capturing:
            st.session_state["capture_streaming"] = False
            capturing = False

        if capturing or (done and buf):
            requested = st.session_state.get("capture_n_samples", 1)
            pct = min(int(len(buf) / max(requested, 1) * 100), 100)
            stopped = error or st.session_state.get(
                "capture_stop_event", threading.Event()
            ).is_set()
            label = (f"Receiving… {len(buf)}/{requested}"
                     if capturing else
                     f"{'Stopped' if stopped else 'Complete'} — {len(buf)} samples")
            st.progress(pct, text=label)

        if error:
            st.error(f"Stream error: {error}")

        if buf and (capturing or done):
            _render_capture_chart(buf, key="live")
            _render_capture_metrics(buf, raw_buf, key_sfx="")

        # Finalise exactly once — copy shared buffer to stable session state keys
        if done and buf and not capturing and not st.session_state.get("_capture_finalised"):
            _finalise_capture(buf, raw_buf, log_buf, error)
            st.rerun()

        if log_buf:
            with st.expander("Stream log"):
                st.code("\n".join(log_buf), language="text")

    _capture_fragment()

    # ── Static display of last completed capture ───────────────────────────────
    # Shown when no capture is in progress and the shared buffer has been cleared.
    # This lets the researcher examine data after the live fragment stops updating.
    samples   = st.session_state.get("_capture_last_samples", [])
    raw_bytes = st.session_state.get("_capture_last_raw", b"")
    log       = st.session_state.get("_capture_last_log", [])
    show_static = (
        samples and not is_capturing
        and not st.session_state.get("_sshared_capture", {}).get("done")
    )
    if show_static:
        _render_capture_chart(samples, key="static")
        _render_capture_metrics(samples, raw_bytes, key_sfx="_s")
        if log:
            with st.expander("Stream log"):
                st.code("\n".join(log), language="text")
        if st.button("Clear Captured Data", key="capture_clear"):
            for k in ("_capture_last_samples", "_capture_last_raw", "_capture_last_log",
                      "_sshared_capture", "_capture_finalised"):
                st.session_state.pop(k, None)
            st.rerun()


def _start_capture(port, baud, n_samples, timeout_s, live_mode,
                   slot: str = "slota", hr_channel: str | None = None):
    """Kick off the binary stream worker in a daemon thread.

    A shared dict is used for thread-safe communication: the worker appends
    to buf/raw/log and sets done=True on exit.  The Streamlit fragment polls
    this dict every 0.5 s via run_every.

    slot and hr_channel are forwarded to stream_binary_live so the correct
    command is sent (e.g. ``adpd ppg slotab stream-bin 500 hr on sBch3``).
    """
    stop_ev = threading.Event()
    shared: dict = {"buf": [], "raw": bytearray(), "log": [], "error": None, "done": False}
    st.session_state["capture_streaming"]  = True
    st.session_state["capture_stop_event"] = stop_ev
    st.session_state["_sshared_capture"]   = shared
    st.session_state["_capture_finalised"] = False

    def _worker():
        try:
            for new_s, new_raw, new_log, _ in stream_binary_live(
                port, baud, n_samples, stream_timeout_s=timeout_s,
                slot=slot, hr_channel=hr_channel,
            ):
                if stop_ev.is_set():
                    break
                shared["buf"].extend(new_s)
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
    _log(f"Capture start: {n_samples} samples from {port} "
         f"({'live' if live_mode else 'batch'})", "info")


def _render_capture_chart(buf: list, key: str):
    """Plot ADC channels (and optionally HR) against elapsed time.

    Adapts to variable sample tuple length:
      5 fields  → Slot A, no HR  (4 channels)
      7 fields  → Slot A + HR   (4 channels + HR secondary axis + Peak markers)
      9 fields  → Slot AB, no HR (8 channels)
      11 fields → Slot AB + HR  (8 channels + HR secondary axis + Peak markers)

    uirevision="capture" keeps zoom/pan state stable across fragment reruns.
    """
    ts_ms   = [s[0] for s in buf]
    n_tuple = len(buf[0])
    # Determine mode from tuple length
    n_ch    = {5: 4, 7: 4, 9: 8, 11: 8}.get(n_tuple, 4)
    has_hr  = n_tuple in (7, 11)

    fig = go.Figure()

    # ADC channel traces — colour/visibility from _CH_INFO lookup
    for i in range(n_ch):
        label, color, visible = _CH_INFO[i]
        fig.add_trace(go.Scatter(
            x=ts_ms, y=[s[i + 1] for s in buf],
            mode="lines", name=label,
            line=dict(color=color, width=1),
            visible=visible,
        ))

    if has_hr:
        hr_idx   = n_ch + 1   # float32 HR BPM field
        peak_idx = n_ch + 2   # uint32 peak flag
        hr_vals  = [s[hr_idx] for s in buf]
        # Peak timestamps — only points where peak == 1
        peak_ts  = [ts_ms[i] for i, s in enumerate(buf) if s[peak_idx]]
        peak_hr  = [s[hr_idx] for s in buf if s[peak_idx]]

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
        uirevision="capture",  # preserves zoom state between fragment reruns
    )
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


def _finalise_capture(buf, raw_buf, log_buf, error):
    """Copy the shared mutable buffers into stable session state keys.

    Called exactly once per capture run (guarded by _capture_finalised flag).
    After this, the shared dict can be cleared without losing captured data.
    """
    st.session_state["_capture_last_samples"] = list(buf)
    st.session_state["_capture_last_raw"]     = bytes(raw_buf)
    st.session_state["_capture_last_log"]     = list(log_buf)
    if error:
        _log(f"Capture ended with error: {error}", "error")
    elif st.session_state.get("capture_stop_event", threading.Event()).is_set():
        _log(f"Capture stopped: {len(buf)} samples kept", "warn")
    else:
        _log(f"Capture complete: {len(buf)} samples", "ok")
    st.session_state["_capture_finalised"] = True
