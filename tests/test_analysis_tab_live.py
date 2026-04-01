from __future__ import annotations

import types
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from ui.live_session import LiveSample
import ui.analysis_tab as analysis_tab


class AnalysisTabLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_st = types.SimpleNamespace(
            session_state={},
            info=mock.Mock(),
            error=mock.Mock(),
            rerun=mock.Mock(),
        )
        self.scfg = {
            "transform_mode": "none",
            "adc_bits": 24,
            "flip_ac_sliding": True,
            "flip_ac_window_s": 2.0,
        }

    def _sample(self, ts: float, *channels: float) -> LiveSample:
        return LiveSample(
            timestamp_ms=ts,
            slot="slota",
            channels=tuple((f"ch{idx + 1}", float(value)) for idx, value in enumerate(channels)),
        )

    def test_build_live_context_waits_for_first_samples(self) -> None:
        shared = {"buf": [], "done": False, "error": None}
        self.fake_st.session_state["live_streaming"] = True

        with mock.patch.object(analysis_tab, "st", self.fake_st), \
             mock.patch.object(analysis_tab, "get_live_shared_state", return_value=shared):
            ctx = analysis_tab._build_live_context(self.scfg)

        self.assertIsNone(ctx)
        self.fake_st.info.assert_called_once_with("Streaming… waiting for first samples.")

    def test_build_live_context_finalizes_empty_completed_stream_once(self) -> None:
        shared = {"buf": [], "done": True, "error": None}

        with mock.patch.object(analysis_tab, "st", self.fake_st), \
             mock.patch.object(analysis_tab, "get_live_shared_state", return_value=shared):
            ctx = analysis_tab._build_live_context(self.scfg)

        self.assertIsNone(ctx)
        self.assertTrue(self.fake_st.session_state[analysis_tab.LIVE_FINALISED_KEY])
        self.fake_st.rerun.assert_called_once()

    def test_build_live_context_turns_off_streaming_and_honors_manual_sr(self) -> None:
        shared = {
            "buf": [
                self._sample(0.0, 1, 2, 3, 4),
                self._sample(10.0, 5, 6, 7, 8),
                self._sample(20.0, 9, 10, 11, 12),
                self._sample(30.0, 13, 14, 15, 16),
                self._sample(40.0, 17, 18, 19, 20),
                self._sample(50.0, 21, 22, 23, 24),
                self._sample(60.0, 25, 26, 27, 28),
                self._sample(70.0, 29, 30, 31, 32),
                self._sample(80.0, 33, 34, 35, 36),
                self._sample(90.0, 37, 38, 39, 40),
            ],
            "done": True,
            "error": None,
        }
        self.fake_st.session_state.update(
            live_streaming=True,
            live_channel="ch4",
            live_override_sr=True,
            live_manual_sr=250.0,
            live_analysis_window_s=5,
        )
        run_pipeline_result = {
            "cleaned": np.arange(10, dtype=float),
            "signals_df": pd.DataFrame({"PPG_Peaks": np.zeros(10, dtype=int)}),
            "info": {"PPG_Peaks": np.array([], dtype=int)},
            "quality": np.ones(10),
            "analysis": None,
        }

        with mock.patch.object(analysis_tab, "st", self.fake_st), \
             mock.patch.object(analysis_tab, "get_live_shared_state", return_value=shared), \
             mock.patch.object(analysis_tab, "apply_signal_transform", return_value=(np.arange(10, dtype=float), None)), \
             mock.patch.object(analysis_tab, "run_pipeline", return_value=run_pipeline_result), \
             mock.patch.object(analysis_tab, "compute_hr_metrics", return_value=(None, None, None)):
            ctx = analysis_tab._build_live_context(self.scfg)

        self.assertIsNotNone(ctx)
        self.assertFalse(self.fake_st.session_state[analysis_tab.LIVE_STREAMING_KEY])
        self.assertEqual(ctx["sr"], 250.0)
        self.assertEqual(ctx["sig_orig"].tolist(), [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0])
        self.assertEqual(self.fake_st.session_state[analysis_tab.LIVE_COMPUTED_SR_KEY], 250.0)
        self.assertFalse(ctx["streaming"])

    def test_build_live_context_surfaces_pipeline_error(self) -> None:
        shared = {
            "buf": [self._sample(float(idx * 10), 1, 2, 3, 4) for idx in range(10)],
            "done": False,
            "error": None,
        }
        self.fake_st.session_state.update(live_streaming=True, live_analysis_window_s=5)

        with mock.patch.object(analysis_tab, "st", self.fake_st), \
             mock.patch.object(analysis_tab, "get_live_shared_state", return_value=shared), \
             mock.patch.object(analysis_tab, "apply_signal_transform", return_value=(np.arange(10, dtype=float), None)), \
             mock.patch.object(analysis_tab, "run_pipeline", side_effect=RuntimeError("bad pipeline")):
            ctx = analysis_tab._build_live_context(self.scfg)

        self.assertIsNone(ctx)
        self.fake_st.error.assert_called_once_with("Pipeline error: bad pipeline")


if __name__ == "__main__":
    unittest.main()
