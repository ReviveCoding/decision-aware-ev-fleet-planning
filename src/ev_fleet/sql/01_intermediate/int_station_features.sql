DROP TABLE IF EXISTS int_station_features;
CREATE TABLE int_station_features AS
SELECT
    date,
    station_id,
    route_count,
    avg_cube,
    avg_distance_miles,
    avg_route_energy_kwh,
    cold_regime,
    capacity_event,
    CAST(strftime('%w', date) AS INTEGER) AS day_of_week,
    AVG(route_count) OVER (
        PARTITION BY station_id ORDER BY date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
    ) AS route_count_rolling_7,
    LAG(route_count, 1) OVER (PARTITION BY station_id ORDER BY date) AS route_count_lag_1,
    LAG(route_count, 7) OVER (PARTITION BY station_id ORDER BY date) AS route_count_lag_7,
    LAG(avg_cube, 1) OVER (PARTITION BY station_id ORDER BY date) AS avg_cube_lag_1,
    LAG(avg_distance_miles, 1) OVER (PARTITION BY station_id ORDER BY date) AS avg_distance_miles_lag_1,
    LAG(avg_route_energy_kwh, 1) OVER (PARTITION BY station_id ORDER BY date) AS avg_route_energy_kwh_lag_1
FROM stg_station_day;
