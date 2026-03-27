"""ui/cache.py — st.cache_data wrappers (pass numpy arrays as bytes for hashability)."""

import numpy as np
import pandas as pd
import streamlit as st

from ppg_processing import run_pipeline, extract_epochs, prepare_signal


@st.cache_data(show_spinner="Running NeuroKit2 pipeline…")
def cached_pipeline(
    signal_bytes: bytes,
    sampling_rate: float,
    clean_method: str,
    peak_method: str,
    quality_method: str = "templatematch",
) -> dict:
    """Cache wrapper around run_pipeline(). Signal passed as bytes for hashability."""
    signal = np.frombuffer(signal_bytes, dtype=np.float64)
    return run_pipeline(signal, sampling_rate, clean_method, peak_method, quality_method)


@st.cache_data(show_spinner=False)
def cached_epochs(
    signal_bytes: bytes,
    peak_indices_bytes: bytes,
    sampling_rate: float,
    epochs_start: float,
    epochs_end: float,
) -> dict:
    """Cache wrapper around extract_epochs(). Arrays passed as bytes for hashability."""
    signal = np.frombuffer(signal_bytes, dtype=np.float64)
    peak_indices = np.frombuffer(peak_indices_bytes, dtype=np.int64)
    return extract_epochs(signal, peak_indices, sampling_rate, epochs_start, epochs_end)


@st.cache_data(show_spinner=False)
def cached_prepare_signal(df: pd.DataFrame, signal_col: str, timestamp_col: str):
    """Cache wrapper around prepare_signal()."""
    return prepare_signal(df, signal_col, timestamp_col)
