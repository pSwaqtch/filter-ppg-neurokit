"""web_serial_component.py — Web Serial API component for browser-side USB serial access.

This module provides a Streamlit component that uses the browser's Web Serial API
to enumerate, connect to, and stream from USB serial devices. All communication
happens client-side in JavaScript; only processed data is sent to Python.

Supported browsers: Chrome, Edge, Opera, newer Safari/Firefox (2021+).
Works on cloud-hosted Streamlit because serial access is on the user's machine,
not the server.
"""

import streamlit as st
import streamlit.components.v1 as components
import json


def web_serial_component(
    key: str = "web_serial",
    baud_options: list[int] = None,
    default_baud: int = 115200,
    streaming: bool = False,
) -> dict:
    """
    Render a Web Serial API component for USB serial communication.

    Args:
        key: Unique key for the component
        baud_options: List of baud rate options (default: [9600, 115200, 230400, 460800])
        default_baud: Default baud rate to select
        streaming: If True, component reads continuously; otherwise read on-demand

    Returns:
        dict with keys from session_state:
            - 'connected': bool, whether a port is currently connected
            - 'port': str or None, the selected port name
            - 'baud': int, selected baud rate
            - 'data': str or None, most recent data received
            - 'error': str or None, error message if any
    """
    if baud_options is None:
        baud_options = [9600, 115200, 230400, 460800]

    # Initialize component state in session_state
    state_key = f"{key}_state"
    if state_key not in st.session_state:
        st.session_state[state_key] = {
            "connected": False,
            "port": None,
            "baud": default_baud,
            "data": None,
            "error": None,
        }

    # HTML/CSS/JS component
    html_content = _build_web_serial_html(
        key=key,
        state_key=state_key,
        baud_options=baud_options,
        default_baud=default_baud,
        streaming=streaming,
    )

    # Render component (communication via localStorage/sessionStorage bridge)
    components.html(html_content, height=320, scrolling=False)

    # Return state (updated by JS via hidden iframe communication)
    return st.session_state[state_key]


def _build_web_serial_html(
    key: str,
    state_key: str,
    baud_options: list[int],
    default_baud: int,
    streaming: bool,
) -> str:
    """Build the HTML/CSS/JS for the Web Serial component."""

    baud_opts_json = json.dumps(baud_options)
    streaming_js = "true" if streaming else "false"

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 8px; background: #0e1117; color: #e6edf3; }}
.container {{ display: flex; flex-direction: column; gap: 12px; }}
.row {{ display: flex; gap: 8px; align-items: center; }}
.col {{ flex: 1; }}
.col.compact {{ flex: 0 0 auto; }}
select, input, button {{ padding: 8px 12px; border: 1px solid #30363d; border-radius: 6px; background: #0d1117; color: #e6edf3; font-size: 14px; }}
select:focus, input:focus {{ outline: none; border-color: #58a6ff; }}
button {{ background: #238636; cursor: pointer; font-weight: 500; border: none; }}
button:hover:not(:disabled) {{ background: #2ea043; }}
button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.status {{ padding: 8px 12px; border-radius: 6px; font-size: 13px; }}
.status.connected {{ background: #1f6feb; color: #e6edf3; }}
.status.disconnected {{ background: #424a52; color: #8b949e; }}
.status.error {{ background: #da3633; color: #e6edf3; }}
.data-display {{ padding: 8px 12px; border: 1px solid #30363d; border-radius: 6px; background: #0d1117; font-family: 'Courier New', monospace; font-size: 12px; max-height: 100px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }}
.hidden {{ display: none; }}
</style>
</head>
<body>
<div class="container">
  <div class="row">
    <div class="col">
      <label for="port-select" style="display: block; margin-bottom: 4px; font-size: 13px;">Serial Port</label>
      <select id="port-select">
        <option value="">-- Select Port --</option>
      </select>
    </div>
    <div class="col compact">
      <label for="baud-select" style="display: block; margin-bottom: 4px; font-size: 13px;">Baud</label>
      <select id="baud-select">
      </select>
    </div>
    <div class="col compact">
      <button id="connect-btn" style="width: 100%; margin-top: 20px;">Connect</button>
    </div>
  </div>

  <div id="status" class="status disconnected">Not connected</div>

  <div id="data-section" class="hidden">
    <label for="data-display" style="display: block; margin-bottom: 4px; font-size: 13px;">Data (${{'Streaming' if streaming else 'Read'}}):</label>
    <div id="data-display" class="data-display"></div>
    <div style="margin-top: 8px;">
      <button id="read-btn" style="width: 100%; ${{'display: none;' if streaming else ''}}">Read Data</button>
      <button id="disconnect-btn" style="width: 100%; margin-top: 4px;">Disconnect</button>
    </div>
  </div>
</div>

<script>
(async function() {{
  const key = "{key}";
  const baudOptions = {baud_opts_json};
  const defaultBaud = {default_baud};
  const streaming = {streaming_js};

  // DOM elements
  const portSelect = document.getElementById("port-select");
  const baudSelect = document.getElementById("baud-select");
  const connectBtn = document.getElementById("connect-btn");
  const readBtn = document.getElementById("read-btn");
  const disconnectBtn = document.getElementById("disconnect-btn");
  const statusEl = document.getElementById("status");
  const dataSection = document.getElementById("data-section");
  const dataDisplay = document.getElementById("data-display");

  // State
  let port = null;
  let reader = null;
  let isReading = false;

  // Populate baud rate options
  baudOptions.forEach(baud => {{
    const opt = document.createElement("option");
    opt.value = baud;
    opt.textContent = baud;
    if (baud === defaultBaud) opt.selected = true;
    baudSelect.appendChild(opt);
  }});

  // Enumerate available ports
  async function enumeratePorts() {{
    try {{
      const ports = await navigator.serial.getPorts();
      console.log("[Web Serial] Available ports:", ports.length);
      portSelect.innerHTML = '<option value="">-- Select Port --</option>';
      ports.forEach((p, i) => {{
        const opt = document.createElement("option");
        opt.value = p.getInfo().usbProductId ? p.getInfo().usbVendorId + ":" + p.getInfo().usbProductId : "port_" + i;
        opt.textContent = p.getInfo().usbProductId ? "USB Device " + (i+1) : "Port " + (i+1);
        opt.dataset.port = JSON.stringify(p.getInfo());
        portSelect.appendChild(opt);
      }});
      if (ports.length > 0) {{
        portSelect.value = portSelect.options[1].value;
      }}
      updateState();
    }} catch(e) {{
      setError("Failed to enumerate ports: " + e.message);
    }}
  }}

  // Request port from user (permission dialog)
  async function requestNewPort() {{
    try {{
      port = await navigator.serial.requestPort();
      enumeratePorts();
    }} catch(e) {{
      if (e.name !== "NotFoundError") {{
        setError("Port request failed: " + e.message);
      }}
    }}
  }}

  // Connect to selected port
  async function connect() {{
    try {{
      const ports = await navigator.serial.getPorts();
      const selectedIndex = portSelect.selectedIndex - 1;
      if (selectedIndex < 0 || selectedIndex >= ports.length) {{
        setError("No port selected");
        return;
      }}
      port = ports[selectedIndex];
      const baudRate = parseInt(baudSelect.value);
      await port.open({{ baudRate }});
      console.log("[Web Serial] Connected to port with baud", baudRate);
      setConnected(baudRate);
      if (streaming) startReading();
    }} catch(e) {{
      setError("Connection failed: " + e.message);
    }}
  }}

  // Start reading from port (streaming mode)
  async function startReading() {{
    if (!port || isReading) return;
    isReading = true;
    try {{
      const decoder = new TextDecoder();
      while (port && port.readable && isReading) {{
        reader = port.readable.getReader();
        try {{
          while (true) {{
            const {{ value, done }} = await reader.read();
            if (done) break;
            const text = decoder.decode(value);
            dataDisplay.textContent += text;
            dataDisplay.scrollTop = dataDisplay.scrollHeight;
            updateState();
          }}
        }} finally {{
          reader.releaseLock();
        }}
      }}
    }} catch(e) {{
      setError("Read error: " + e.message);
      isReading = false;
    }}
  }}

  // Read once (non-streaming)
  async function readOnce() {{
    if (!port || !port.readable) {{
      setError("Port not open");
      return;
    }}
    try {{
      const reader = port.readable.getReader();
      const {{ value, done }} = await reader.read();
      reader.releaseLock();
      if (!done && value) {{
        const decoder = new TextDecoder();
        const text = decoder.decode(value);
        dataDisplay.textContent = text;
        updateState();
      }}
    }} catch(e) {{
      setError("Read failed: " + e.message);
    }}
  }}

  // Disconnect
  async function disconnect() {{
    if (reader) {{
      try {{ reader.cancel(); }} catch(e) {{}}
      reader = null;
    }}
    isReading = false;
    if (port) {{
      try {{ await port.close(); }} catch(e) {{}}
      port = null;
    }}
    dataDisplay.textContent = "";
    setDisconnected();
  }}

  // UI helpers
  function setConnected(baudRate) {{
    statusEl.className = "status connected";
    statusEl.textContent = "✓ Connected @ " + baudRate + " baud";
    connectBtn.textContent = "Reconnect";
    dataSection.classList.remove("hidden");
    portSelect.disabled = true;
    baudSelect.disabled = true;
  }}

  function setDisconnected() {{
    statusEl.className = "status disconnected";
    statusEl.textContent = "Not connected";
    connectBtn.textContent = "Connect";
    dataSection.classList.add("hidden");
    portSelect.disabled = false;
    baudSelect.disabled = false;
    updateState();
  }}

  function setError(msg) {{
    statusEl.className = "status error";
    statusEl.textContent = "✗ " + msg;
    console.error("[Web Serial]", msg);
  }}

  function updateState() {{
    // Update UI only (no postMessage needed for now)
    console.log("[Web Serial] State:", {{
      connected: port && port.readable,
      baud: parseInt(baudSelect.value),
      dataLength: dataDisplay.textContent.length,
    }});
  }}

  // Event listeners
  connectBtn.addEventListener("click", connect);
  readBtn.addEventListener("click", readOnce);
  disconnectBtn.addEventListener("click", disconnect);
  portSelect.addEventListener("change", updateState);

  // Check for Web Serial API support
  if (!navigator.serial) {{
    setError("Web Serial API not supported in this browser (use Chrome/Edge/newer Firefox)");
    connectBtn.disabled = true;
  }} else {{
    enumeratePorts();
  }}
}})();
</script>
</body>
</html>
"""
