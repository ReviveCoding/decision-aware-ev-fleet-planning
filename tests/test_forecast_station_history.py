import numpy as np
import pandas as pd

from ev_fleet.forecasting import _build_next_day_features


def test_station_histories_and_dummy_rows_remain_isolated():
    dates = pd.date_range("2026-01-01", periods=8)
    frame = pd.DataFrame(
        {
            "station_id": np.repeat([2**53 + 1, 2**53 + 3], 8),
            "date": np.tile(dates, 2),
            "route_count": np.r_[np.arange(8), 100 + np.arange(8)],
            "avg_cube": 2.0,
            "avg_distance_miles": 3.0,
            "avg_route_energy_kwh": 4.0,
        }
    )
    columns = [f"station_{2**53 + 1}", f"station_{2**53 + 3}"]
    result = _build_next_day_features(frame.sample(frac=1, random_state=5), columns).set_index("station_id")
    assert result.loc[2**53 + 1, "route_count_lag_1"] == 7
    assert result.loc[2**53 + 3, "route_count_lag_7"] == 101
    assert result.loc[2**53 + 1, columns].tolist() == [1.0, 0.0]
    assert result.loc[2**53 + 3, columns].tolist() == [0.0, 1.0]
    assert (result["capacity_event"] == 0).all()
