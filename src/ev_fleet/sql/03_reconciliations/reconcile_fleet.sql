DROP TABLE IF EXISTS reconciliation_fleet;
CREATE TABLE reconciliation_fleet AS
SELECT
    f.station_id,
    SUM(f.initial_count) AS initial_vehicle_count,
    s.parking_capacity,
    CASE WHEN SUM(f.initial_count) >= 0 THEN 1 ELSE 0 END AS nonnegative_check,
    CASE WHEN SUM(f.initial_count) <= s.parking_capacity THEN 1 ELSE 0 END AS parking_capacity_check
FROM stg_initial_fleet f
JOIN stg_stations s USING (station_id)
GROUP BY f.station_id, s.parking_capacity;
