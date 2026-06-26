DROP TABLE IF EXISTS mart_station_day_features;
CREATE TABLE mart_station_day_features AS
SELECT
    date,
    station_id,
    route_count,
    avg_cube,
    avg_distance_miles,
    avg_route_energy_kwh,
    cold_regime,
    capacity_event,
    day_of_week,
    route_count_rolling_7,
    route_count_lag_1,
    route_count_lag_7,
    avg_cube_lag_1,
    avg_distance_miles_lag_1,
    avg_route_energy_kwh_lag_1
FROM int_station_features;
