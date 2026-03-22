"""ppg_charts.py — Plotly chart builders for PPG signal visualization.

All functions return go.Figure objects; no Streamlit calls here.
Import QUALITY_REFS from ppg_processing for threshold reference lines.
"""

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ppg_processing import QUALITY_REFS

DARK = "plotly_dark"

_QUALITY_COLORS = [
    "#AB63FA", "#19D3F3", "#FFA15A", "#00CC96",
    "#EF553B", "#636EFA", "#FF6692", "#B6E880",
]


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def downsample(timestamps, signal, max_pts: int = 5_000):
    """Stride-based downsample for display only — preserves overall shape.

    step = max(1, N // max_pts); returns every step-th sample.
    Not used in any signal processing calculation.
    """
    n = len(timestamps)
    if n <= max_pts:
        return timestamps, signal
    step = max(1, n // max_pts)
    return timestamps[::step], signal[::step]


# ─────────────────────────────────────────────────────────────────────────────
# Chart builders
# ─────────────────────────────────────────────────────────────────────────────

def plot_signal_overview(
    timestamps_ms, signal, cleaned, peak_indices, quality, signal_col, quality_method="templatematch"
) -> go.Figure:
    """Combined 3–4 row subplot: raw / cleaned+raw / peaks / quality (shared X)."""
    has_q = quality is not None
    n_rows = 4 if has_q else 3
    row_heights = [0.28, 0.28, 0.28, 0.16] if has_q else [0.34, 0.33, 0.33]
    subplot_titles = ["Raw Signal", "Processed Signal", "Peak Detection"]
    if has_q:
        subplot_titles.append("Signal Quality")

    fig = make_subplots(
        rows=n_rows, cols=1,
        shared_xaxes=True,
        subplot_titles=subplot_titles,
        row_heights=row_heights,
        vertical_spacing=0.05,
    )

    ts, sig_d = downsample(timestamps_ms, signal)
    _, cln_d  = downsample(timestamps_ms, cleaned)

    fig.add_trace(go.Scatter(x=ts, y=sig_d, mode="lines",
        line=dict(color="#636EFA", width=1), name=signal_col), row=1, col=1)

    fig.add_trace(go.Scatter(x=ts, y=sig_d, mode="lines",
        line=dict(color="rgba(160,160,160,0.35)", width=1),
        name="Raw", legendgroup="overlay"), row=2, col=1)
    fig.add_trace(go.Scatter(x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned", legendgroup="overlay"), row=2, col=1)

    fig.add_trace(go.Scatter(x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Signal", showlegend=False), row=3, col=1)
    if len(peak_indices) > 0:
        fig.add_trace(go.Scatter(
            x=timestamps_ms[peak_indices], y=cleaned[peak_indices], mode="markers",
            marker=dict(color="#EF553B", size=7, symbol="triangle-up"),
            name="Peaks"), row=3, col=1)

    if has_q:
        q_arr = np.array(quality)
        min_len = min(len(timestamps_ms), len(q_arr))
        ts_q, q_d = downsample(timestamps_ms[:min_len], q_arr[:min_len])
        fig.add_trace(go.Scatter(x=ts_q, y=q_d, mode="lines",
            line=dict(color="#AB63FA", width=1.5), name="Quality",
            fill="tozeroy", fillcolor="rgba(171,99,250,0.15)"), row=4, col=1)
        for _v, _c, _lbl in QUALITY_REFS.get(quality_method, []):
            fig.add_hline(y=_v, line_dash="dash", line_color=_c, row=4, col=1)
        fig.update_yaxes(autorange=True, fixedrange=True, row=4, col=1)

    for r in range(1, 4):
        fig.update_yaxes(autorange=True, row=r, col=1)

    fig.update_xaxes(title_text="Timestamp (ms)", row=n_rows, col=1)
    fig.update_layout(
        template=DARK,
        height=820 if has_q else 680,
        legend=dict(orientation="h", y=1.02, x=0),
        margin=dict(l=50, r=20, t=60, b=40),
    )
    return fig


def plot_raw_signal(
    timestamps_ms, signal, signal_col: str, original=None, baseline=None
) -> go.Figure:
    """Raw signal chart with optional original (grey) and baseline (dotted orange) overlays.

    original:  pre-transform signal shown in grey behind the active signal.
    baseline:  AC-flip sliding mean shown as dotted orange line.
    """
    ts, sig = downsample(timestamps_ms, signal)
    fig = go.Figure()
    if original is not None:
        _, orig_d = downsample(timestamps_ms, original)
        fig.add_trace(go.Scatter(
            x=ts, y=orig_d, mode="lines",
            line=dict(color="rgba(160,160,160,0.35)", width=1),
            name=f"{signal_col} (original)",
        ))
    fig.add_trace(go.Scatter(
        x=ts, y=sig, mode="lines",
        line=dict(color="#636EFA", width=1.5),
        name=f"{signal_col} (transformed)" if original is not None else signal_col,
    ))
    if baseline is not None:
        _, bl_d = downsample(timestamps_ms, baseline)
        fig.add_trace(go.Scatter(
            x=ts, y=bl_d, mode="lines",
            line=dict(color="#FFA15A", width=1.2, dash="dot"),
            name="sliding baseline",
        ))
    fig.update_layout(
        template=DARK,
        title=f"Raw Signal — {signal_col}",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        margin=dict(l=40, r=20, t=40, b=40),
    )
    return fig


def plot_cleaned_overlay(timestamps_ms, raw, cleaned, signal_col: str) -> go.Figure:
    """Cleaned signal with optional raw overlay (grey)."""
    ts, cln_d = downsample(timestamps_ms, cleaned)
    fig = go.Figure()
    if raw is not None:
        _, raw_d = downsample(timestamps_ms, raw)
        fig.add_trace(go.Scatter(
            x=ts, y=raw_d, mode="lines",
            line=dict(color="rgba(160,160,160,0.4)", width=1),
            name="Raw",
        ))
    fig.add_trace(go.Scatter(
        x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned",
    ))
    fig.update_layout(
        template=DARK,
        title=f"Processed Signal — {signal_col}",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        legend=dict(orientation="h", y=1.02),
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def plot_peaks(timestamps_ms, cleaned, peak_indices) -> go.Figure:
    """Cleaned signal with detected peak markers (red triangles)."""
    ts, cln_d = downsample(timestamps_ms, cleaned)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=cln_d, mode="lines",
        line=dict(color="#00CC96", width=1.5),
        name="Cleaned",
    ))
    if len(peak_indices) > 0:
        fig.add_trace(go.Scatter(
            x=timestamps_ms[peak_indices], y=cleaned[peak_indices], mode="markers",
            marker=dict(color="#EF553B", size=8, symbol="triangle-up"),
            name="Peaks",
        ))
    fig.update_layout(
        template=DARK,
        title="Peak Detection",
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Amplitude", autorange=True),
        dragmode="select",
        height=350,
        legend=dict(orientation="h", y=1.02),
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def plot_individual_beats(epochs: dict, hr_mean: float) -> go.Figure:
    """Overlay individual beat waveforms + NaN-safe average, x-axis in seconds.

    Algorithm:
      1. Find global [time_min, time_max] across all epochs.
      2. Build common_t: 300-point linspace over that range.
      3. For each beat: interpolate onto common_t within its actual time range;
         set NaN outside (avoids flat-line artefact from np.interp clipping).
      4. Average: np.nanmean, only where ≥50% of beats have real data.
         Prevents truncated end-of-recording beats from pulling the average down.

    C equivalent: ALGORITHM.md §6
    """
    time_min, time_max = np.inf, -np.inf
    for ep in epochs.values():
        t = ep.index.astype(float)
        time_min = min(time_min, t.min())
        time_max = max(time_max, t.max())

    common_t = np.linspace(time_min, time_max, 300)
    beat_matrix = []

    for ep in epochs.values():
        t = ep.index.astype(float)
        s = ep["Signal"].values
        if len(t) < 2:
            continue
        row = np.full(len(common_t), np.nan)
        in_range = (common_t >= t.min()) & (common_t <= t.max())
        row[in_range] = np.interp(common_t[in_range], t, s)
        beat_matrix.append(row)

    if not beat_matrix:
        return go.Figure()

    beat_matrix = np.array(beat_matrix)
    coverage = np.sum(~np.isnan(beat_matrix), axis=0)
    min_coverage = max(1, len(beat_matrix) // 2)
    avg = np.where(coverage >= min_coverage, np.nanmean(beat_matrix, axis=0), np.nan)

    fig = go.Figure()
    first = True
    for beat in beat_matrix:
        fig.add_trace(go.Scatter(
            x=common_t, y=beat, mode="lines",
            line=dict(color="rgba(180,180,180,0.25)", width=1),
            name="Individual beats", legendgroup="beats", showlegend=first,
        ))
        first = False

    fig.add_trace(go.Scatter(
        x=common_t, y=avg, mode="lines",
        line=dict(color="#C0392B", width=4),
        name="Average beat shape",
    ))
    fig.add_vline(x=0, line_dash="dash", line_color="grey", line_width=1.5)

    title = f"Individual beats ({len(beat_matrix)} beats"
    if hr_mean is not None:
        title += f", average heart rate: {hr_mean:.1f} bpm"
    title += ")"

    fig.update_layout(
        template=DARK, title=title,
        xaxis_title="Time (seconds)", yaxis_title="PPG",
        height=420,
        legend=dict(orientation="h", y=1.05),
        margin=dict(l=50, r=20, t=60, b=50),
    )
    return fig


def plot_quality(timestamps_ms, quality_map: dict) -> go.Figure:
    """Overlay one or more quality signals on a single figure.

    quality_map: {method_name: np.ndarray} — arrays must match timestamps_ms length.
    Reference lines from QUALITY_REFS are drawn per method, deduplicated by value+colour.
    See ALGORITHM.md §8 for threshold sources and method formulas.
    """
    fig = go.Figure()
    seen_refs: set = set()
    for i, (method, quality) in enumerate(quality_map.items()):
        color = _QUALITY_COLORS[i % len(_QUALITY_COLORS)]
        ts, q_d = downsample(timestamps_ms, np.array(quality))
        fig.add_trace(go.Scatter(
            x=ts, y=q_d, mode="lines",
            line=dict(color=color, width=1.5),
            name=method,
        ))
        for val, ref_color, label in QUALITY_REFS.get(method, []):
            ref_key = (val, ref_color)
            if ref_key not in seen_refs:
                fig.add_hline(y=val, line_dash="dash", line_color=ref_color,
                              annotation_text=label)
                seen_refs.add(ref_key)

    title = "Signal Quality — " + ", ".join(quality_map.keys())
    fig.update_layout(
        template=DARK, title=title,
        xaxis_title="Timestamp (ms)",
        yaxis=dict(title="Quality", autorange=True),
        dragmode="select",
        height=350,
        margin=dict(l=40, r=20, t=40, b=40),
        legend=dict(orientation="h", y=1.08, x=0),
    )
    return fig
