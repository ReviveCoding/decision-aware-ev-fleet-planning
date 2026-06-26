DROP TABLE IF EXISTS mart_station_capacity;
CREATE TABLE mart_station_capacity AS
SELECT
    s.station_id,
    s.charger_ports,
    s.charger_power_kw,
    s.site_power_kw,
    s.mean_dwell_hours,
    s.repair_capacity,
    s.parking_capacity,
    s.region,
    SUM(f.initial_count) AS total_vehicles,
    MIN(s.site_power_kw * s.mean_dwell_hours,
        s.charger_ports * s.charger_power_kw * s.mean_dwell_hours) AS nominal_daily_charge_energy_kwh
FROM stg_stations s
JOIN stg_initial_fleet f USING (station_id)
GROUP BY s.station_id, s.charger_ports, s.charger_power_kw, s.site_power_kw,
         s.mean_dwell_hours, s.repair_capacity, s.parking_capacity, s.region;
