from __future__ import annotations

import unittest
from unittest import mock


from ui.live_session import (
    LIVE_CHANNEL_KEY,
    LIVE_COMPUTED_SR_KEY,
    LIVE_FINALISED_KEY,
    LIVE_SHARED_KEY,
    LIVE_SLOT_KEY,
    LIVE_STREAMING_KEY,
    LIVE_STOP_EVENT_KEY,
    LiveSample,
    append_live_samples,
    channel_names_for_slot,
    ensure_live_session_state,
    finalize_live_session,
    get_live_capture_status,
    get_live_channel_options,
    launch_live_stream,
    normalize_live_sample,
    normalize_slot,
    set_live_computed_sr,
    start_live_session,
    stop_live_session,
)


class DummyStopEvent:
    def __init__(self) -> None:
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1


class LiveSessionTests(unittest.TestCase):
    def test_normalize_live_sample_slot_a_without_hr(self) -> None:
        sample = normalize_live_sample((101.5, 11, 22, 33, 44), slot="Slot A")

        self.assertIsInstance(sample, LiveSample)
        self.assertEqual(sample.timestamp_ms, 101.5)
        self.assertEqual(sample.slot, "slota")
        self.assertEqual(sample.channel_names, ("ch1", "ch2", "ch3", "ch4"))
        self.assertEqual(
            sample.channel_map,
            {"ch1": 11.0, "ch2": 22.0, "ch3": 33.0, "ch4": 44.0},
        )
        self.assertIsNone(sample.hr_bpm)
        self.assertIsNone(sample.peak)
        self.assertFalse(sample.hr_enabled)
        self.assertEqual(sample.to_row()["slot"], "slota")

    def test_normalize_live_sample_slot_ab_with_hr_and_peak(self) -> None:
        sample = normalize_live_sample(
            (200.0, 1, 2, 3, 4, 5, 6, 7, 8, 91.25, 1),
            slot="slotab",
        )

        self.assertEqual(sample.slot, "slotab")
        self.assertEqual(
            sample.channel_names,
            ("ch1", "ch2", "ch3", "ch4", "ch5", "ch6", "ch7", "ch8"),
        )
        self.assertEqual(sample.hr_bpm, 91.25)
        self.assertEqual(sample.peak, 1)
        self.assertTrue(sample.hr_enabled)
        self.assertEqual(sample.to_row()["hr_bpm"], 91.25)
        self.assertEqual(get_live_channel_options(sample), channel_names_for_slot("slotab"))

    def test_normalize_slot_aliases(self) -> None:
        self.assertEqual(normalize_slot("slot a"), "slota")
        self.assertEqual(normalize_slot("Slot_AB"), "slotab")

    def test_live_session_state_helpers_manage_shared_state_and_stop_event(self) -> None:
        state: dict[str, object] = {LIVE_CHANNEL_KEY: "ch5"}
        stop_event = DummyStopEvent()

        ensure_live_session_state(state)
        self.assertEqual(state[LIVE_CHANNEL_KEY], "ch5")
        self.assertIn(LIVE_SHARED_KEY, state)

        start_live_session(state, stop_event=stop_event)
        self.assertTrue(state[LIVE_STREAMING_KEY])
        self.assertIs(state[LIVE_STOP_EVENT_KEY], stop_event)
        self.assertFalse(state[LIVE_FINALISED_KEY])
        self.assertEqual(state[LIVE_COMPUTED_SR_KEY], 0.0)

        sample = normalize_live_sample((1.0, 10, 20, 30, 40), slot="slota")
        shared = state[LIVE_SHARED_KEY]
        append_live_samples(
            state,
            samples=[sample],
            raw=b"abc",
            log=["first frame"],
            done=False,
        )

        self.assertEqual(shared["buf"], [sample])
        self.assertEqual(shared["raw"], bytearray(b"abc"))
        self.assertEqual(shared["log"], ["first frame"])
        self.assertFalse(shared["done"])

        set_live_computed_sr(state, 123.4)
        self.assertEqual(state[LIVE_COMPUTED_SR_KEY], 123.4)

        returned = stop_live_session(state)
        self.assertIs(returned, stop_event)
        self.assertEqual(stop_event.set_calls, 1)
        self.assertFalse(state[LIVE_STREAMING_KEY])

        finalize_live_session(state)
        self.assertTrue(state[LIVE_FINALISED_KEY])

    def test_live_capture_status_summarizes_connection_and_stream_state(self) -> None:
        state: dict[str, object] = {}
        ensure_live_session_state(state)

        status = get_live_capture_status(state)
        self.assertEqual(status["phase"], "needs_connection")

        state.update(
            conn_connected=True,
            conn_control_port="/dev/control",
            conn_stream_port="/dev/stream",
            conn_baud=115200,
        )
        ready_status = get_live_capture_status(state)
        self.assertEqual(ready_status["phase"], "ready")

        sample = normalize_live_sample((1.0, 10, 20, 30, 40), slot="slota")
        start_live_session(state, stop_event=DummyStopEvent())
        append_live_samples(state, samples=[sample], raw=b"raw")
        streaming_status = get_live_capture_status(state)
        self.assertEqual(streaming_status["phase"], "streaming")
        self.assertEqual(streaming_status["sample_count"], 1)
        self.assertIn("Receiving", streaming_status["detail"])

    def test_live_capture_status_reports_waiting_and_complete_states(self) -> None:
        state: dict[str, object] = {
            "conn_connected": True,
            "conn_control_port": "/dev/control",
            "conn_stream_port": "/dev/stream",
            "conn_baud": 115200,
        }
        ensure_live_session_state(state)
        start_live_session(state, stop_event=DummyStopEvent())

        waiting_status = get_live_capture_status(state)
        self.assertEqual(waiting_status["phase"], "streaming")
        self.assertIn("Waiting for first samples", waiting_status["detail"])

        sample = normalize_live_sample((1.0, 10, 20, 30, 40), slot="slota")
        append_live_samples(state, samples=[sample], done=True)
        state[LIVE_STREAMING_KEY] = False

        complete_status = get_live_capture_status(state)
        self.assertEqual(complete_status["phase"], "complete")
        self.assertEqual(complete_status["tone"], "success")
        self.assertIn("1 samples", complete_status["detail"])

    def test_launch_live_stream_accumulates_samples_and_finalizes(self) -> None:
        state: dict[str, object] = {
            "conn_connected": True,
            "conn_control_port": "/dev/control",
            "conn_stream_port": "/dev/stream",
            "conn_baud": 115200,
        }
        sample_a = (10.0, 1, 2, 3, 4)
        sample_b = (20.0, 5, 6, 7, 8)

        def fake_stream(*_args, **_kwargs):
            yield [sample_a], b"a", ["first"], False
            yield [sample_b], b"b", ["second"], True

        thread = launch_live_stream(
            state,
            control_port="/dev/control",
            stream_port="/dev/stream",
            baud=115200,
            num_samples=2,
            slot="slota",
            stream_factory=fake_stream,
        )
        thread.join(timeout=2.0)

        shared = state[LIVE_SHARED_KEY]
        self.assertEqual(len(shared["buf"]), 2)
        self.assertEqual(shared["raw"], bytearray(b"ab"))
        self.assertEqual(shared["log"], ["first", "second"])
        self.assertTrue(shared["done"])
        self.assertTrue(state[LIVE_STREAMING_KEY])

    def test_launch_live_stream_promotes_error_logs_and_stops(self) -> None:
        state: dict[str, object] = {}

        def fake_stream(*_args, **_kwargs):
            yield [(10.0, 1, 2, 3, 4)], b"a", ["ERROR: lost sync"], False
            yield [(20.0, 5, 6, 7, 8)], b"b", ["second"], True

        thread = launch_live_stream(
            state,
            control_port="/dev/control",
            stream_port="/dev/stream",
            baud=115200,
            num_samples=2,
            slot="slota",
            stream_factory=fake_stream,
        )
        thread.join(timeout=2.0)

        shared = state[LIVE_SHARED_KEY]
        self.assertEqual(shared["error"], "lost sync")
        self.assertEqual(len(shared["buf"]), 1)
        self.assertTrue(shared["done"])

    def test_launch_live_stream_records_generator_exception(self) -> None:
        state: dict[str, object] = {}

        def fake_stream(*_args, **_kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        thread = launch_live_stream(
            state,
            control_port="/dev/control",
            stream_port="/dev/stream",
            baud=115200,
            num_samples=2,
            slot="slota",
            stream_factory=fake_stream,
        )
        thread.join(timeout=2.0)

        shared = state[LIVE_SHARED_KEY]
        self.assertEqual(shared["error"], "boom")
        self.assertTrue(shared["done"])

    def test_launch_live_stream_does_not_overwrite_widget_owned_slot_state(self) -> None:
        state: dict[str, object] = {LIVE_SLOT_KEY: "slotab"}

        def fake_stream(*_args, **_kwargs):
            yield [], b"", [], True

        thread = launch_live_stream(
            state,
            control_port="/dev/control",
            stream_port="/dev/stream",
            baud=115200,
            num_samples=1,
            slot="slota",
            stream_factory=fake_stream,
        )
        thread.join(timeout=2.0)

        self.assertEqual(state[LIVE_SLOT_KEY], "slotab")


if __name__ == "__main__":
    unittest.main()
