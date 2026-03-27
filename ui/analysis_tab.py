"""ui/analysis_tab.py — Dashboard-style Analysis tab.

Layout (minimal scroll):
  ① Streaming status banner (USB mode only)
  ② Metric strip  — SR / Duration / Peaks / HR / Samples
  ③ Chart grid    — Raw | Peaks  (row 1)
                    Quality | Beats  (row 2)
  ④ Expanders     — HRV / Analysis, NK native plot, Data preview, Export

All controls (methods, time window, channel, transform, SR) live in the
sidebar. The analysis tab is pure visualisation.
"""

import io

import matplotlib.pyplot as plt
import neurokit2 as nk
import numpy as np
import pandas as pd
import streamlit as st

from ppg_processing import (
    CLEAN_METHODS, PEAK_METHODS, QUALITY_METHODS, QUALITY_REFS,
    apply_signal_transform, run_pipeline, compute_hr_metrics,
)
from ppg_charts import plot_raw_signal, plot_cleaned_overlay, plot_peaks, plot_individual_beats, plot_quality
from ui.cache import cached_pipeline, cached_epochs
from ui.helpers import extract_box_x

_LIVE_DISPLAY_S = 15.0
_CHART_H        = 290  # chart height in px for the 2-column grid

# ─────────────────────────────────────────────────────────────────────────────
# Dashboard CSS
# ─────────────────────────────────────────────────────────────────────────────

_DASH_CSS = """
<style>
/* Metric card styling */
[data-testid="stMetric"] {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
}
[data-testid="stMetricLabel"]  { font-size: 0.7rem !important; opacity: 0.55; }
[data-testid="stMetricValue"]  { font-size: 1.25rem !important; }
[data-testid="stMetricDelta"]  { font-size: 0.7rem !important; }

/* Chart card-like border */
[data-testid="stPlotlyChart"] > div {
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px;
    overflow: hidden;
}

/* Tighter expander */
[data-testid="stExpander"] summary {
    font-size: 0.85rem;
    padding: 0.4rem 0;
}

/* Status banner */
.stream-banner {
    background: rgba(0,204,150,0.12);
    border-left: 3px solid #00CC96;
    border-radius: 4px;
    padding: 0.4rem 0.75rem;
    font-size: 0.85rem;
    margin-bottom: 0.5rem;
}
</style>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def render_analysis_tab(sidebar_cfg: dict, pipeline_ctx: dict):
    """Render the Analysis dashboard tab.

    Parameters
    ----------
    sidebar_cfg:   dict returned by render_sidebar().
    pipeline_ctx:  dict with pre-computed pipeline results from app.py
                   (timestamps_w, signal_w, signal_w_orig, cleaned, …).
                   In live mode all arrays are empty — the fragment builds
                   its own context from the stream buffer.
    """
    st.markdown(_DASH_CSS, unsafe_allow_html=True)

    live = sidebar_cfg["live_stream_mode"]
    live_refresh = 0.5 if st.session_state.get("live_streaming") and live else None

    @st.fragment(run_every=live_refresh)
    def _dashboard(_scfg=sidebar_cfg, _pctx=pipeline_ctx):
        if _scfg["live_stream_mode"]:
            ctx = _build_live_context(_scfg)
            if ctx is None:
                return
        else:
            ctx = _build_file_context(_scfg, _pctx)

        # Read method selections from session state (widgets are in sidebar)
        clean_method    = st.session_state.get("clean_method",    CLEAN_METHODS[0])
        peak_method     = st.session_state.get("peak_method",     PEAK_METHODS[0])
        quality_methods = st.session_state.get("quality_methods") or [QUALITY_METHODS[0]]

        _render_dashboard(ctx, _scfg, clean_method, peak_method, quality_methods)

    _dashboard()


# ─────────────────────────────────────────────────────────────────────────────
# Context builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_live_context(scfg: dict) -> dict | None:
    """Build analysis context from the rolling live stream buffer.
    Returns None when there is no data yet or a terminal error."""
    shared    = st.session_state.get("_sshared_live", {})
    buf       = shared.get("buf", [])
    done      = shared.get("done", False)
    streaming = st.session_state.get("live_streaming", False)
    error     = shared.get("error")

    if done and streaming:
        st.session_state["live_streaming"] = False
        streaming = False

    if error:
        st.error(f"Stream error: {error}")

    if not buf:
        msg = "Streaming… waiting for first samples." if streaming else \
              "No stream data yet. Select **USB Serial Stream**, connect, and press ▶ Start."
        st.info(msg)
        if done and not st.session_state.get("_live_finalised"):
            st.session_state["_live_finalised"] = True
            st.rerun()
        return None

    # Build arrays from buffer
    all_ts  = np.array([s[0] for s in buf], dtype=np.float64)
    ch_key  = st.session_state.get("live_channel", "ch3")
    ch_idx  = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}[ch_key]
    all_sig = np.array([s[ch_idx] for s in buf], dtype=np.float64)

    # Rolling SR from last 200 timestamps
    win = min(len(all_ts), 200)
    if win >= 2:
        diffs = np.diff(all_ts[-win:])
        pos   = diffs[diffs > 0]
        sr    = 1000.0 / float(np.median(pos)) if len(pos) else 100.0
    else:
        sr = 100.0

    if st.session_state.get("live_override_sr"):
        sr = float(st.session_state.get("live_manual_sr", sr))
    st.session_state["_live_computed_sr"] = sr

    # Display window
    keep_n   = max(10, int(_LIVE_DISPLAY_S * sr))
    disp_ts  = all_ts[-keep_n:]
    disp_sig = all_sig[-keep_n:]

    # Analysis window
    analysis_s = float(st.session_state.get("live_analysis_window_s", 5))
    anal_n     = max(10, int(analysis_s * sr))
    ts_w       = disp_ts[-anal_n:]
    sig_orig   = disp_sig[-anal_n:]

    sig_w, flip_bl = apply_signal_transform(
        sig_orig,
        mode=scfg["transform_mode"],
        adc_bits=scfg["adc_bits"],
        flip_sliding=scfg["flip_ac_sliding"],
        flip_window_s=scfg["flip_ac_window_s"],
        sampling_rate=sr,
    )

    if len(sig_w) < 10:
        st.info(f"Collecting data… {len(buf):,} samples (need ≥{anal_n} for {analysis_s}s window)")
        return None

    clean_method   = st.session_state.get("clean_method",  CLEAN_METHODS[0])
    peak_method    = st.session_state.get("peak_method",   PEAK_METHODS[0])
    quality_method = (st.session_state.get("quality_methods") or [QUALITY_METHODS[0]])[0]

    try:
        results = run_pipeline(sig_w, sr, clean_method, peak_method, quality_method)
    except Exception as le:
        st.error(f"Pipeline error: {le}")
        return None

    peaks           = results["info"].get("PPG_Peaks", np.array([], dtype=int))
    hr_m, hr_lo, hr_hi = compute_hr_metrics(peaks, sr)

    if done and not streaming and not st.session_state.get("_live_finalised"):
        st.session_state["_live_finalised"] = True
        st.rerun()

    return {
        "ts":        ts_w,
        "sig_w":     sig_w,
        "sig_orig":  sig_orig,
        "flip_bl":   flip_bl,
        "sr":        sr,
        "cleaned":   results["cleaned"],
        "sig_df":    results["signals_df"],
        "info":      results["info"],
        "quality":   results["quality"],
        "analysis":  results["analysis"],
        "peaks":     peaks,
        "hr_m":      hr_m,
        "hr_lo":     hr_lo,
        "hr_hi":     hr_hi,
        "sig_col":   ch_key,
        "t0":        float(ts_w[0]),
        "t1":        float(ts_w[-1]),
        "n_rows":    len(buf),
        "n_cols":    5,
        "df_raw":    None,
        "sig_bytes": sig_w.tobytes(),
        "streaming": streaming,
    }


def _build_file_context(scfg: dict, pctx: dict) -> dict:
    """Wrap pre-computed pipeline results for the dashboard."""
    return {
        "ts":        pctx["timestamps_w"],
        "sig_w":     pctx["signal_w"],
        "sig_orig":  pctx["signal_w_orig"],
        "flip_bl":   pctx["flip_baseline"],
        "sr":        scfg["sampling_rate"],
        "cleaned":   pctx["cleaned"],
        "sig_df":    pctx["signals_df"],
        "info":      pctx["info"],
        "quality":   pctx["quality"],
        "analysis":  pctx["analysis"],
        "peaks":     pctx["peak_indices"],
        "hr_m":      pctx["hr_mean"],
        "hr_lo":     pctx["hr_min"],
        "hr_hi":     pctx["hr_max"],
        "sig_col":   scfg["signal_col"],
        "t0":        scfg["t0"],
        "t1":        scfg["t1"],
        "n_rows":    len(scfg["df_raw"]),
        "n_cols":    len(scfg["df_raw"].columns),
        "df_raw":    scfg["df_raw"],
        "sig_bytes": pctx["signal_bytes"],
        "streaming": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard renderer
# ─────────────────────────────────────────────────────────────────────────────

def _render_dashboard(ctx: dict, scfg: dict,
                      clean_method: str, peak_method: str,
                      quality_methods: list[str]):
    live      = scfg["live_stream_mode"]
    xformed   = scfg["transform_mode"] != "none"
    col_safe  = ctx["sig_col"].replace("-", "_")

    # ── ① Streaming status banner ─────────────────────────────────────────────
    if live and ctx["streaming"]:
        st.markdown(
            f'<div class="stream-banner">● Live — {ctx["n_rows"]:,} samples'
            f' · {ctx["sr"]:.1f} Hz</div>',
            unsafe_allow_html=True,
        )

    # ── ② Metric strip ────────────────────────────────────────────────────────
    duration_s = (ctx["ts"][-1] - ctx["ts"][0]) / 1000 if len(ctx["ts"]) > 1 else 0
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("SR",       f"{ctx['sr']:.0f} Hz")
    m2.metric("Duration", f"{duration_s:.1f} s")
    m3.metric("Peaks",    len(ctx["peaks"]))
    m4.metric("Mean HR",  f"{ctx['hr_m']:.0f} bpm" if ctx["hr_m"] else "—")
    m5.metric("Samples",  f"{ctx['n_rows']:,}")

    # ── ③ Chart grid: row 1  Raw | Peaks ─────────────────────────────────────
    c_left, c_right = st.columns(2)

    with c_left:
        fig = plot_raw_signal(
            ctx["ts"], ctx["sig_w"], ctx["sig_col"],
            original=ctx["sig_orig"] if xformed else None,
            baseline=ctx["flip_bl"],
        )
        fig.update_layout(
            height=_CHART_H,
            title=dict(text="Raw Signal", font=dict(size=13)),
            margin=dict(l=40, r=10, t=36, b=30),
        )
        ev = st.plotly_chart(fig, use_container_width=True,
                             key="chart_raw", on_select="rerun", selection_mode="box")
        _handle_zoom(ev, ctx, live)

    with c_right:
        fig = plot_peaks(ctx["ts"], ctx["cleaned"], ctx["peaks"])
        fig.update_layout(
            height=_CHART_H,
            title=dict(text=f"Peak Detection — {peak_method}", font=dict(size=13)),
            margin=dict(l=40, r=10, t=36, b=30),
        )
        ev = st.plotly_chart(fig, use_container_width=True,
                             key="chart_peaks", on_select="rerun", selection_mode="box")
        _handle_zoom(ev, ctx, live)

    # ── ③ Chart grid: row 2  Quality | Beats ─────────────────────────────────
    c_left2, c_right2 = st.columns(2)

    with c_left2:
        quality_map = _compute_quality_map(
            ctx, scfg, clean_method, peak_method, quality_methods, live
        )
        if quality_map:
            common_len  = min(len(v) for v in quality_map.values())
            aligned     = {m: v[:common_len] for m, v in quality_map.items()}
            fig = plot_quality(ctx["ts"][:common_len], aligned)
            fig.update_layout(
                height=_CHART_H,
                title=dict(text="Signal Quality", font=dict(size=13)),
                margin=dict(l=40, r=10, t=36, b=30),
            )
            ev = st.plotly_chart(fig, use_container_width=True,
                                 key=f"chart_qual_{'_'.join(quality_methods)}",
                                 on_select="rerun", selection_mode="box")
            _handle_zoom(ev, ctx, live)

            # Quality sub-metrics
            qcols = st.columns(max(1, len(quality_map)))
            for qcol, (qm, qa) in zip(qcols, quality_map.items()):
                mean  = float(np.nanmean(qa))
                refs  = QUALITY_REFS.get(qm, [])
                thresh = next(
                    (v for v, _, lbl in refs if "good" in lbl or "boundary" in lbl), None
                )
                if thresh is not None:
                    pct = float(np.mean(qa >= thresh) * 100)
                    qcol.metric(qm, f"{mean:.3f}", f"{pct:.0f}% >{thresh}")
                elif qm == "skewness":
                    pct = float(np.mean(qa >= 0.0) * 100)
                    qcol.metric(qm, f"{mean:.3f}", f"{pct:.0f}% >0")
                else:
                    qcol.metric(qm, f"{mean:.3f}", f"σ={np.nanstd(qa):.3f}")
        else:
            st.info("No quality data available for the selected methods.")

    with c_right2:
        if len(ctx["peaks"]) >= 2:
            beat_pre  = st.session_state.get("beat_pre",  0.2)
            beat_post = st.session_state.get("beat_post", 0.5)
            try:
                epochs = cached_epochs(
                    ctx["cleaned"].tobytes(),
                    ctx["peaks"].astype(np.int64).tobytes(),
                    ctx["sr"], -beat_pre, beat_post,
                )
                hr_beats, _, _ = compute_hr_metrics(ctx["peaks"], ctx["sr"])
                fig = plot_individual_beats(epochs, hr_beats)
                fig.update_layout(
                    height=_CHART_H,
                    margin=dict(l=50, r=10, t=50, b=30),
                )
                st.plotly_chart(fig, use_container_width=True,
                                key=f"chart_beats_{beat_pre}_{beat_post}")
                with st.expander("Beat window"):
                    bc1, bc2 = st.columns(2)
                    bc1.slider("Pre-peak (s)",  0.1, 0.5, step=0.05, key="beat_pre")
                    bc2.slider("Post-peak (s)", 0.2, 1.0, step=0.05, key="beat_post")
            except Exception as e:
                st.error(f"Beat segmentation: {e}")
        else:
            st.info("Need ≥2 detected peaks for individual beat view.")

    # ── ④ HRV / Analysis expander ─────────────────────────────────────────────
    with st.expander("HRV / Analysis"):
        if ctx["analysis"] is not None:
            st.dataframe(ctx["analysis"], use_container_width=True)
            buf = io.BytesIO()
            ctx["analysis"].to_csv(buf, index=False)
            st.download_button("Download CSV", buf.getvalue(),
                               "ppg_analysis.csv", "text/csv", key="dl_hrv")
        else:
            st.info("Window too short for HRV — widen the time window or collect more data.")
            if not ctx["sig_df"].empty:
                st.dataframe(ctx["sig_df"].head(200), use_container_width=True)

    # ── NK native plot ────────────────────────────────────────────────────────
    if scfg.get("show_nk_plot") and not ctx["sig_df"].empty:
        with st.expander("NeuroKit2 native plot"):
            try:
                fig_nk = nk.ppg_plot(ctx["sig_df"], ctx["info"])
                st.pyplot(fig_nk)
                plt.close(fig_nk)
            except Exception as e:
                st.warning(f"Could not render: {e}")

    # ── Data preview ──────────────────────────────────────────────────────────
    if ctx["df_raw"] is not None:
        with st.expander(f"Raw data preview — {ctx['n_rows']:,} rows"):
            st.dataframe(ctx["df_raw"].head(200), use_container_width=True)
    elif live:
        shared_p = st.session_state.get("_sshared_live", {})
        buf_p    = shared_p.get("buf", [])
        if buf_p:
            with st.expander(f"Stream buffer preview — {len(buf_p):,} samples"):
                preview = pd.DataFrame(buf_p[-200:],
                                       columns=["timestamp_ms", "ch1", "ch2", "ch3", "ch4"])
                st.dataframe(preview, use_container_width=True)

    # ── Export ────────────────────────────────────────────────────────────────
    export_df = _build_export_df(ctx, clean_method, peak_method, quality_methods, live)

    if live:
        shared_ex = st.session_state.get("_sshared_live", {})
        buf_ex    = shared_ex.get("buf", [])
        raw_ex    = shared_ex.get("raw", b"")
        if buf_ex:
            with st.expander("Export stream data"):
                fdf = pd.DataFrame(buf_ex,
                                   columns=["timestamp_ms", "ch1", "ch2", "ch3", "ch4"])
                e1, e2, e3 = st.columns(3)
                with e1:
                    st.download_button(
                        "Analysis window CSV",
                        export_df.to_csv(index=False).encode(),
                        "analysis_window.csv", "text/csv", key="dl_win",
                        width="stretch",
                    )
                with e2:
                    st.download_button(
                        "Full stream CSV",
                        fdf.to_csv(index=False).encode(),
                        "stream_full.csv", "text/csv", key="dl_live_full",
                        width="stretch",
                    )
                with e3:
                    st.download_button(
                        "Raw binary",
                        bytes(raw_ex), "stream_full.bin",
                        "application/octet-stream", key="dl_live_bin",
                        width="stretch",
                        help=f"{len(raw_ex):,} bytes — 20 bytes/sample",
                    )
                if st.button("Clear stream data", key="live_clear_btn"):
                    for k in ("_sshared_live", "_live_finalised", "_live_computed_sr"):
                        st.session_state.pop(k, None)
                    st.rerun()
    else:
        with st.expander("Export"):
            e1, e2, e3 = st.columns(3)
            with e1:
                st.download_button(
                    "Raw CSV",
                    export_df[["timestamp_ms", f"{col_safe}_raw"]].to_csv(index=False).encode(),
                    "raw_signal.csv", "text/csv", key="dl_raw", width="stretch",
                )
            with e2:
                st.download_button(
                    "Processed CSV",
                    export_df[["timestamp_ms", f"{col_safe}_raw",
                               f"{col_safe}_cleaned"]].to_csv(index=False).encode(),
                    "processed_signal.csv", "text/csv", key="dl_proc", width="stretch",
                )
            with e3:
                st.download_button(
                    "All Data CSV",
                    export_df.to_csv(index=False).encode(),
                    "all_data.csv", "text/csv", key="dl_all", width="stretch",
                )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_zoom(event, ctx: dict, live: bool):
    """If the user box-selected a chart region, zoom all charts to that x range."""
    b = extract_box_x(event)
    if b and not live:
        st.session_state._pending_window = (
            max(ctx["t0"], min(b)), min(ctx["t1"], max(b))
        )
        st.rerun()


def _compute_quality_map(ctx, scfg, clean_method, peak_method,
                          quality_methods, live) -> dict:
    """Run quality methods and return {method: array} map."""
    quality_map = {}
    for qm in quality_methods:
        try:
            if live:
                qres = run_pipeline(ctx["sig_w"], ctx["sr"],
                                    clean_method, peak_method, qm)
            else:
                qres = cached_pipeline(ctx["sig_bytes"], ctx["sr"],
                                       clean_method, peak_method, qm)
            if qres["quality"] is not None:
                qa = np.array(qres["quality"])
                mn = min(len(ctx["ts"]), len(qa))
                quality_map[qm] = qa[:mn]
        except Exception:
            pass
    return quality_map


def _build_export_df(ctx, clean_method, peak_method, quality_methods, live) -> pd.DataFrame:
    """Build a master export DataFrame for the current analysis window."""
    n        = len(ctx["ts"])
    col_safe = ctx["sig_col"].replace("-", "_")
    df = pd.DataFrame({
        "timestamp_ms":        ctx["ts"],
        f"{col_safe}_raw":     ctx["sig_orig"],
        f"{col_safe}_cleaned": ctx["cleaned"],
        "PPG_Peak":   ctx["sig_df"]["PPG_Peaks"].values.astype(int)
                      if not ctx["sig_df"].empty else np.zeros(n, dtype=int),
        "PPG_Rate_bpm": ctx["sig_df"]["PPG_Rate"].values
                        if "PPG_Rate" in ctx["sig_df"].columns
                        else np.full(n, np.nan),
    })
    if not live:
        for qm in quality_methods:
            qr = cached_pipeline(ctx["sig_bytes"], ctx["sr"],
                                 clean_method, peak_method, qm)
            q = qr["quality"]
            if q is not None:
                qa = np.array(q)
                mn = min(n, len(qa))
                df[f"quality_{qm}"] = np.concatenate([qa[:mn], np.full(n - mn, np.nan)])
            else:
                df[f"quality_{qm}"] = np.nan
    return df
