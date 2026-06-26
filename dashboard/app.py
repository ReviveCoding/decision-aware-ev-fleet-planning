from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from ev_fleet.dashboard_data import load_dashboard_payload

st.set_page_config(page_title="EV Fleet Planning Reliability", layout="wide")
artifacts = Path(os.getenv("EV_FLEET_ARTIFACTS", "artifacts"))
st.title("Decision-Aware EV Fleet Planning & Reliability Digital Twin")
try:
    payload = load_dashboard_payload(artifacts)
except (FileNotFoundError, ValueError) as exc:
    st.warning(f"Artifacts are not ready: {exc}. Run `make smoke` first.")
    st.stop()

summary = payload["summary"]
rcce = payload["rcce"]
plan = payload["plan"]
metrics = payload["metrics"]
cols = st.columns(6)
cols[0].metric("Forecast champion", metrics["forecast_champion"])
cols[1].metric("Base policy", metrics["base_policy"])
cols[2].metric("Base RCCE", metrics["base_rcce"])
cols[3].metric("Recommended RCCE", metrics["recommended_rcce"])
cols[4].metric("Formal VSS", f"{metrics['formal_sample_vss']:.1f}")
cols[5].metric("Formal EVPI", f"{metrics['formal_sample_evpi']:.1f}")
st.subheader("Reliability-Calibrated Capacity Envelope")
st.json(rcce)
st.subheader("Recommended allocation")
st.dataframe(pd.DataFrame(plan["allocation"]), use_container_width=True)
st.subheader("Redeployment actions")
st.dataframe(pd.DataFrame(plan["moves"]), use_container_width=True)
st.caption(f"Pipeline status: {summary['pipeline_status']}")
