"""ui/helpers.py — Small UI utility functions."""

import numpy as np
import pandas as pd
import streamlit as st

from ui.cache import cached_pipeline


def extract_box_x(event) -> tuple[float, float] | None:
    """Extract x-range [lo, hi] from a Plotly box-selection event, or None."""
    try:
        if event and event.selection and event.selection.box:
            box = event.selection.box[0]
            if box.get("x"):
                return float(box["x"][0]), float(box["x"][1])
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return None


def dl_button(label: str, df: pd.DataFrame, filename: str, key: str):
    """Render a CSV download button for a DataFrame."""
    st.download_button(label, df.to_csv(index=False).encode(),
                       filename, "text/csv", key=key, width="stretch")


def build_export_df(
    timestamps_w,
    signal_w,
    cleaned,
    signals_df,
    signal_col: str,
    signal_bytes: bytes,
    sampling_rate: float,
    clean_method: str,
    peak_method: str,
    quality_methods: list[str],
) -> pd.DataFrame:
    """Build the master export DataFrame for the current analysis window."""
    n = len(timestamps_w)
    col = signal_col.replace("-", "_")
    df = pd.DataFrame({
        "timestamp_ms":   timestamps_w,
        f"{col}_raw":     signal_w,
        f"{col}_cleaned": cleaned,
        "PPG_Peak":       signals_df["PPG_Peaks"].values.astype(int),
        "PPG_Rate_bpm":   (
            signals_df["PPG_Rate"].values
            if "PPG_Rate" in signals_df.columns
            else np.full(n, np.nan)
        ),
    })
    for qm in quality_methods:
        qr = cached_pipeline(signal_bytes, sampling_rate, clean_method, peak_method, qm)
        q = qr["quality"]
        if q is not None:
            qa = np.array(q)
            mn = min(n, len(qa))
            df[f"quality_{qm}"] = np.concatenate([qa[:mn], np.full(n - mn, np.nan)])
        else:
            df[f"quality_{qm}"] = np.nan
    return df
