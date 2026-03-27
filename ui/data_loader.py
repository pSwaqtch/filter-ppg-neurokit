"""ui/data_loader.py — File I/O helpers (Streamlit-aware, not C-portable)."""

import os

import numpy as np
import pandas as pd
import streamlit as st

from ppg_processing import TIMESTAMP_COL


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DEMO_FILES = [
    "trial_data_1.csv",
    "trial_data_2.csv",
    "trial_data_3.csv",
    "trial_data_4.csv",
    "trial_data_5.xlsx",
]


@st.cache_data(show_spinner="Loading file…")
def load_data(source, file_ext: str) -> pd.DataFrame:
    """Load CSV or XLSX from a file path or file-like object."""
    try:
        if file_ext in (".xlsx", ".xls"):
            df = pd.read_excel(source, sheet_name=0, engine="openpyxl")
        else:
            df = pd.read_csv(source)
    except Exception as e:
        st.error(f"Failed to load file: {e}")
        st.stop()
    return df


def get_signal_columns(df: pd.DataFrame) -> list[str]:
    """Return numeric non-timestamp columns with <80% NaN."""
    cols = []
    for col in df.columns:
        if col.lower() == TIMESTAMP_COL:
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        if df[col].isna().mean() < 0.8:
            cols.append(col)
    return cols


def find_timestamp_col(df: pd.DataFrame) -> str:
    """Find timestamp column: prefer 'timestamp', else first monotone int col."""
    if TIMESTAMP_COL in df.columns:
        return TIMESTAMP_COL
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]):
            vals = df[col].dropna().values
            if len(vals) > 1 and np.all(np.diff(vals) >= 0):
                return col
    return df.columns[0]
