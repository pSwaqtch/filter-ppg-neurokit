"""Shared live-session helpers for the Streamlit app.

This module keeps the live-stream sample shape and the Streamlit session-state
keys in one place so the UI can treat live analysis as a small, well-defined
state machine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from typing import Any, Callable

try:
    import streamlit as _st
except Exception:  # pragma: no cover
    _st = None


LIVE_STREAMING_KEY = "live_streaming"
LIVE_STOP_EVENT_KEY = "live_stop_event"
LIVE_SHARED_KEY = "_sshared_live"
LIVE_FINALISED_KEY = "_live_finalised"
LIVE_COMPUTED_SR_KEY = "_live_computed_sr"
LIVE_CHANNEL_KEY = "live_channel"
LIVE_SLOT_KEY = "live_slot"
LIVE_ODR_KEY = "live_odr"
LIVE_SAMPLE_COUNT_KEY = "live_n_samples"
LIVE_ANALYSIS_WINDOW_KEY = "live_analysis_window_s"
LIVE_OVERRIDE_SR_KEY = "live_override_sr"
LIVE_MANUAL_SR_KEY = "live_manual_sr"

_SLOT_ALIASES = {
    "a": "slota",
    "slot a": "slota",
    "slot_a": "slota",
    "slota": "slota",
    "ab": "slotab",
    "slot ab": "slotab",
    "slot_ab": "slotab",
    "slotab": "slotab",
}

_CHANNEL_NAMES = {
    "slota": ("ch1", "ch2", "ch3", "ch4"),
    "slotab": ("ch1", "ch2", "ch3", "ch4", "ch5", "ch6", "ch7", "ch8"),
}


@dataclass(frozen=True, slots=True)
class LiveSample:
    """Normalized live PPG sample."""

    timestamp_ms: float
    slot: str
    channels: tuple[tuple[str, float], ...]
    hr_bpm: float | None = None
    peak: int | None = None
    sequence: int | None = None
    hr_enabled: bool = False

    @property
    def channel_map(self) -> dict[str, float]:
        return dict(self.channels)

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.channels)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "timestamp_ms": self.timestamp_ms,
            "slot": self.slot,
            "hr_enabled": self.hr_enabled,
        }
        row.update(self.channel_map)
        if self.hr_bpm is not None:
            row["hr_bpm"] = self.hr_bpm
        if self.peak is not None:
            row["peak"] = self.peak
        if self.sequence is not None:
            row["sequence"] = self.sequence
        return row


def normalize_slot(slot: str) -> str:
    key = slot.strip().lower().replace("-", " ").replace("_", " ")
    if key not in _SLOT_ALIASES:
        raise ValueError(f"Unsupported live slot: {slot!r}")
    return _SLOT_ALIASES[key]


def channel_names_for_slot(slot: str) -> tuple[str, ...]:
    return _CHANNEL_NAMES[normalize_slot(slot)]


def channel_count_for_slot(slot: str) -> int:
    return len(channel_names_for_slot(slot))


def _default_shared_live_state() -> dict[str, Any]:
    return {"buf": [], "raw": bytearray(), "log": [], "error": None, "done": False}


def _default_live_state() -> dict[str, Any]:
    return {
        LIVE_STREAMING_KEY: False,
        LIVE_STOP_EVENT_KEY: None,
        LIVE_SHARED_KEY: _default_shared_live_state(),
        LIVE_FINALISED_KEY: False,
        LIVE_COMPUTED_SR_KEY: 0.0,
        LIVE_CHANNEL_KEY: "ch3",
        LIVE_SLOT_KEY: "slota",
        LIVE_ODR_KEY: 100,
        LIVE_SAMPLE_COUNT_KEY: 10_000,
        LIVE_ANALYSIS_WINDOW_KEY: 5,
        LIVE_OVERRIDE_SR_KEY: False,
        LIVE_MANUAL_SR_KEY: 100.0,
    }


_LIVE_DEFAULT_FACTORIES = {
    key: (lambda value=value: value) for key, value in _default_live_state().items()
}
_LIVE_DEFAULT_FACTORIES[LIVE_SHARED_KEY] = _default_shared_live_state


def _resolve_state(state: MutableMapping[str, Any] | None) -> MutableMapping[str, Any]:
    if state is not None:
        return state
    if _st is None:  # pragma: no cover
        raise RuntimeError("streamlit is required when no session_state mapping is provided")
    return _st.session_state


def ensure_live_session_state(
    state: MutableMapping[str, Any] | None = None,
) -> MutableMapping[str, Any]:
    session = _resolve_state(state)
    for key, factory in _LIVE_DEFAULT_FACTORIES.items():
        if key not in session:
            session[key] = factory()
    return session


def create_live_shared_state() -> dict[str, Any]:
    return _default_shared_live_state()


def start_live_session(
    state: MutableMapping[str, Any] | None = None,
    *,
    shared: MutableMapping[str, Any] | None = None,
    stop_event: Any = None,
) -> MutableMapping[str, Any]:
    session = ensure_live_session_state(state)
    session[LIVE_STREAMING_KEY] = True
    session[LIVE_STOP_EVENT_KEY] = stop_event
    session[LIVE_SHARED_KEY] = shared if shared is not None else create_live_shared_state()
    session[LIVE_FINALISED_KEY] = False
    session[LIVE_COMPUTED_SR_KEY] = 0.0
    return session


def stop_live_session(state: MutableMapping[str, Any] | None = None) -> Any:
    session = ensure_live_session_state(state)
    stop_event = session.get(LIVE_STOP_EVENT_KEY)
    if hasattr(stop_event, "set"):
        stop_event.set()
    session[LIVE_STREAMING_KEY] = False
    return stop_event


def finalize_live_session(
    state: MutableMapping[str, Any] | None = None,
) -> MutableMapping[str, Any]:
    session = ensure_live_session_state(state)
    session[LIVE_STREAMING_KEY] = False
    session[LIVE_FINALISED_KEY] = True
    shared = session.get(LIVE_SHARED_KEY)
    if isinstance(shared, MutableMapping):
        shared["done"] = True
    return session


def get_live_shared_state(
    state: MutableMapping[str, Any] | None = None,
) -> MutableMapping[str, Any]:
    session = ensure_live_session_state(state)
    shared = session[LIVE_SHARED_KEY]
    if not isinstance(shared, MutableMapping):
        shared = create_live_shared_state()
        session[LIVE_SHARED_KEY] = shared
    return shared


def append_live_samples(
    state: MutableMapping[str, Any] | None = None,
    *,
    samples: Iterable[LiveSample] | None = None,
    raw: bytes | bytearray = b"",
    log: Iterable[str] | None = None,
    error: str | None = None,
    done: bool | None = None,
) -> MutableMapping[str, Any]:
    shared = get_live_shared_state(state)
    if samples is not None:
        shared.setdefault("buf", []).extend(samples)
    if raw:
        shared.setdefault("raw", bytearray()).extend(raw)
    if log is not None:
        shared.setdefault("log", []).extend(log)
    if error is not None:
        shared["error"] = error
    if done is not None:
        shared["done"] = done
    return shared


def set_live_computed_sr(
    state: MutableMapping[str, Any] | None,
    sr: float,
) -> MutableMapping[str, Any]:
    session = ensure_live_session_state(state)
    session[LIVE_COMPUTED_SR_KEY] = float(sr)
    return session


def get_live_channel_options(slot_or_sample: str | LiveSample) -> tuple[str, ...]:
    if isinstance(slot_or_sample, LiveSample):
        return slot_or_sample.channel_names
    return channel_names_for_slot(slot_or_sample)


def normalize_live_sample(
    sample: Sequence[Any] | Mapping[str, Any],
    *,
    slot: str | None = None,
    sequence: int | None = None,
) -> LiveSample:
    if isinstance(sample, Mapping):
        return _normalize_mapping_sample(sample, slot=slot, sequence=sequence)
    return _normalize_sequence_sample(sample, slot=slot, sequence=sequence)


def normalize_live_samples(
    samples: Iterable[Sequence[Any] | Mapping[str, Any]],
    *,
    slot: str | None = None,
) -> list[LiveSample]:
    return [normalize_live_sample(sample, slot=slot) for sample in samples]


def _normalize_sequence_sample(
    sample: Sequence[Any],
    *,
    slot: str | None,
    sequence: int | None,
) -> LiveSample:
    values = tuple(sample)
    if len(values) not in (5, 7, 9, 11):
        raise ValueError(f"Unsupported live sample layout with {len(values)} fields")

    inferred_slot = normalize_slot(slot or ("slota" if len(values) in (5, 7) else "slotab"))
    channel_count = channel_count_for_slot(inferred_slot)
    expected_len = 1 + channel_count + (2 if len(values) in (7, 11) else 0)
    if len(values) != expected_len:
        raise ValueError(
            f"Live sample layout {len(values)} fields does not match slot {inferred_slot}"
        )

    names = channel_names_for_slot(inferred_slot)
    channels = tuple((names[idx], float(values[idx + 1])) for idx in range(channel_count))
    has_hr = len(values) in (7, 11)
    hr_bpm = float(values[1 + channel_count]) if has_hr else None
    peak = int(values[1 + channel_count + 1]) if has_hr else None

    return LiveSample(
        timestamp_ms=float(values[0]),
        slot=inferred_slot,
        channels=channels,
        hr_bpm=hr_bpm,
        peak=peak,
        sequence=sequence,
        hr_enabled=has_hr,
    )


def _normalize_mapping_sample(
    sample: Mapping[str, Any],
    *,
    slot: str | None,
    sequence: int | None,
) -> LiveSample:
    slot_value = normalize_slot(slot or str(sample.get("slot", "slota")))
    raw_channels = sample.get("channels", {})
    if isinstance(raw_channels, Mapping):
        channel_items = tuple(raw_channels.items())
    else:
        channel_items = tuple(raw_channels)

    if not channel_items:
        raise ValueError("Live sample mapping must include channel values")

    expected_names = channel_names_for_slot(slot_value)
    normalized_channels = [(str(name), float(value)) for name, value in channel_items]

    if tuple(name for name, _ in normalized_channels) != expected_names[: len(normalized_channels)]:
        raise ValueError("Live sample mapping channel names do not match the selected slot")

    hr_bpm = sample.get("hr_bpm")
    peak = sample.get("peak")
    hr_enabled = bool(sample.get("hr_enabled", hr_bpm is not None or peak is not None))

    return LiveSample(
        timestamp_ms=float(sample["timestamp_ms"]),
        slot=slot_value,
        channels=tuple(normalized_channels),
        hr_bpm=None if hr_bpm is None else float(hr_bpm),
        peak=None if peak is None else int(peak),
        sequence=sequence if sequence is not None else sample.get("sequence"),
        hr_enabled=hr_enabled,
    )


def get_live_capture_status(
    state: MutableMapping[str, Any] | None = None,
) -> dict[str, Any]:
    session = ensure_live_session_state(state)
    shared = get_live_shared_state(session)
    control_port = session.get("conn_control_port") or session.get("conn_port") or ""
    stream_port = session.get("conn_stream_port") or ""
    connected = bool(session.get("conn_connected") and control_port and stream_port)
    streaming = bool(session.get(LIVE_STREAMING_KEY, False))
    done = bool(shared.get("done", False))
    error = shared.get("error")
    sample_count = len(shared.get("buf", []))
    slot = str(session.get(LIVE_SLOT_KEY, "slota"))
    odr_hz = int(session.get(LIVE_ODR_KEY, 100))
    requested_samples = int(session.get(LIVE_SAMPLE_COUNT_KEY, 10_000))
    progress_ratio = min(sample_count / max(requested_samples, 1), 1.0)

    if not connected:
        return {
            "phase": "needs_connection",
            "tone": "info",
            "title": "Pair device",
            "detail": "Use Connect Device to pair the ADPD7000 ports before starting live analysis.",
            "action_hint": "Pair control and stream ports first.",
            "sample_count": sample_count,
            "control_port": control_port,
            "stream_port": stream_port,
            "slot": slot,
            "odr_hz": odr_hz,
            "requested_samples": requested_samples,
            "progress_ratio": progress_ratio,
        }
    if error:
        return {
            "phase": "error",
            "tone": "error",
            "title": "Live feed error",
            "detail": str(error),
            "action_hint": "Review the error, then restart the live feed.",
            "sample_count": sample_count,
            "control_port": control_port,
            "stream_port": stream_port,
            "slot": slot,
            "odr_hz": odr_hz,
            "requested_samples": requested_samples,
            "progress_ratio": progress_ratio,
        }
    if streaming:
        is_waiting = sample_count == 0
        detail = (
            f"Waiting for first samples from {stream_port}."
            if is_waiting
            else f"Receiving samples on {stream_port}: {sample_count:,}/{requested_samples:,} captured."
        )
        return {
            "phase": "streaming",
            "tone": "info",
            "title": "Waiting for stream" if is_waiting else "Live feed running",
            "detail": detail,
            "action_hint": "Watch Analyze Signal for the rolling window as samples arrive.",
            "sample_count": sample_count,
            "control_port": control_port,
            "stream_port": stream_port,
            "slot": slot,
            "odr_hz": odr_hz,
            "requested_samples": requested_samples,
            "progress_ratio": progress_ratio,
        }
    if done and sample_count:
        return {
            "phase": "complete",
            "tone": "success",
            "title": "Live feed complete",
            "detail": f"Last capture kept {sample_count:,} samples for analysis from {slot} at {odr_hz} Hz.",
            "action_hint": "Review the kept window in Analyze Signal or start another live feed.",
            "sample_count": sample_count,
            "control_port": control_port,
            "stream_port": stream_port,
            "slot": slot,
            "odr_hz": odr_hz,
            "requested_samples": requested_samples,
            "progress_ratio": progress_ratio,
        }
    return {
        "phase": "ready",
        "tone": "success",
        "title": "Ready to start",
        "detail": f"Ports are paired. The next live feed will use {slot} at {odr_hz} Hz.",
        "action_hint": "Start the live feed from Connect Device when ready.",
        "sample_count": sample_count,
        "control_port": control_port,
        "stream_port": stream_port,
        "slot": slot,
        "odr_hz": odr_hz,
        "requested_samples": requested_samples,
        "progress_ratio": progress_ratio,
    }


def launch_live_stream(
    state: MutableMapping[str, Any] | None = None,
    *,
    control_port: str,
    stream_port: str,
    baud: int,
    num_samples: int,
    slot: str,
    hr_channel: str | None = None,
    stream_factory: Callable[..., Iterable[tuple[list[Any], bytes, list[str], bool]]] | None = None,
) -> threading.Thread:
    """Start the shared live-stream worker and return its thread."""

    session = ensure_live_session_state(state)
    stop_event = threading.Event()
    shared = create_live_shared_state()
    start_live_session(session, shared=shared, stop_event=stop_event)

    if stream_factory is None:
        from usb_serial import stream_binary_live_dual_port as stream_factory

    def _worker() -> None:
        try:
            for new_samples, new_raw, new_log, is_final in stream_factory(
                control_port,
                stream_port,
                baud,
                num_samples,
                slot=slot,
                hr_channel=hr_channel,
            ):
                if stop_event.is_set():
                    break
                normalized = normalize_live_samples(new_samples, slot=slot) if new_samples else []
                append_live_samples(
                    session,
                    samples=normalized,
                    raw=new_raw,
                    log=new_log,
                )
                for line in new_log:
                    if line.startswith("ERROR:"):
                        append_live_samples(session, error=line[6:].strip())
                        stop_event.set()
                        break
                if is_final or stop_event.is_set():
                    break
        except Exception as exc:
            append_live_samples(session, error=str(exc))
        finally:
            append_live_samples(session, done=True)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread
