"""maintenance.py: predicts when each bay's equipment needs repair, from its sensor history.

Each bay has three pieces of equipment:

  water    the pump and nozzle. Bay 1's rain sensor stands in for a water flow meter
           (fully wet = FLOW_FULL_SCALE_LPM litres per minute). Every wash records its
           average flow. Compared with the bay's normal flow:
             - no water during a wash          -> "Not functioning"
             - under FLOW_LOW_FRACTION of normal -> "Low flow", may be malfunctioning
             - falling steadily                -> predicts the day it will become low
  valve    the servo that opens the bay. Every wash is one open/close cycle; it's due for
           service every VALVE_SERVICE_CYCLES cycles, predicted from recent usage.
  counter  the ultrasonic car counter. Washes with no cars counted means it may be broken.

When something goes wrong, evaluate_after_run() raises a fault (shown on the Maintenance
page and Operations), and clears it again once the readings recover. Marking a piece of
equipment serviced starts its history afresh.
"""

from __future__ import annotations

import statistics
import time
from typing import Optional

from carwash.config import (
    FLOW_LOW_FRACTION,
    FLOW_NOMINAL_LPM,
    FLOW_NONE_LPM,
    HARDWARE_BAY,
    VALVE_SERVICE_CYCLES,
)
from carwash.storage.database import MonitorDatabase

DAY = 86400
EQUIPMENT = ("water", "valve", "counter")
STATUS_LABELS = {
    "ok": "OK",
    "soon": "Service soon",
    "failing": "May be failing",
    "down": "Not functioning",
    "no_data": "Not enough data",
    "not_installed": "Not installed",
}
BASELINE_READINGS = 20      # the bay's "normal" flow = median of its first washes after a service
RECENT_READINGS = 5         # current flow = average of the last few washes
TREND_DAYS = 14             # the trend line uses the last two weeks of washes
PREDICT_DAYS = 60           # don't predict further ahead than this


def _slope_per_day(points: list[tuple[float, float]]) -> Optional[float]:
    """Least-squares slope of (timestamp, value) points, in value per day."""
    if len(points) < 6:
        return None
    xs = [t / DAY for t, _ in points]
    if max(xs) - min(xs) < 2:   # need at least two days of history for a trend
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(v for _, v in points)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (v - mean_y) for x, (_, v) in zip(xs, points)) / denominator


def _plural(n: int, word: str, plural: str = "") -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _when(days: float, now: float) -> str:
    days = max(0.0, days)
    if days < 1:
        return "within a day"
    date = time.strftime("%b %d", time.localtime(now + days * DAY)).replace(" 0", " ")
    return f"in about {round(days)} day{'s' if round(days) != 1 else ''} ({date})"


class MaintenanceAnalyzer:
    def __init__(self, database: MonitorDatabase) -> None:
        self.database = database

    # ------------------------------------------------------------------
    # water flow
    # ------------------------------------------------------------------
    def water(self, bay: dict, now: Optional[float] = None) -> dict:
        now = now or time.time()
        bay_number = bay["bay_number"]
        serviced = self.database.serviced_at(bay_number, "water") or 0.0
        readings = self.database.readings(bay_number, "flow", since=max(serviced, now - 90 * DAY))
        result = {"equipment": "water", "name": "Water flow", "unit": "L/min", "days_until": None,
                  "history": [round(r["value"], 2) for r in readings[-40:]], "serviced_at": serviced or None}
        if bay_number == HARDWARE_BAY and not bay.get("rain_sensor_installed"):
            return {**result, "status": "not_installed", "reading": "No flow meter", "prediction": ""}
        if not readings:
            return {**result, "status": "no_data", "baseline": FLOW_NOMINAL_LPM,
                    "low_threshold": round(FLOW_NOMINAL_LPM * FLOW_LOW_FRACTION, 2),
                    "reading": "No washes recorded yet", "prediction": "Predictions start after a few washes."}

        values = [r["value"] for r in readings]
        baseline = statistics.median(values[:BASELINE_READINGS]) if len(values) >= 5 else FLOW_NOMINAL_LPM
        baseline = max(baseline, FLOW_NONE_LPM * 2)
        low_threshold = baseline * FLOW_LOW_FRACTION
        latest = values[-1]
        recent = statistics.fmean(values[-RECENT_READINGS:])
        percent = round(recent / baseline * 100)
        trend = _slope_per_day([(r["recorded_at"], r["value"]) for r in readings if r["recorded_at"] >= now - TREND_DAYS * DAY])
        result.update(baseline=round(baseline, 2), low_threshold=round(low_threshold, 2), latest=round(latest, 2),
                      recent=round(recent, 2), percent_of_normal=percent,
                      trend_per_day=None if trend is None else round(trend, 3))

        if latest < FLOW_NONE_LPM:
            result.update(status="down", reading=f"No water in the last wash ({latest:.1f} L/min)",
                          prediction="Repair now: check the pump, supply valve and hoses.", days_until=0)
        elif recent < low_threshold:
            result.update(status="failing", reading=f"{recent:.1f} L/min, {percent}% of normal ({baseline:.1f})",
                          prediction="Flow is low: the nozzle may be clogged or the pump wearing. Service now.",
                          days_until=0)
        else:
            result.update(status="ok", reading=f"{recent:.1f} L/min, {percent}% of normal ({baseline:.1f})")
            if trend is not None and trend < 0:
                days = (recent - low_threshold) / -trend
                if days <= PREDICT_DAYS:
                    result.update(
                        status="soon" if days <= 7 else "ok", days_until=round(days, 1),
                        prediction=f"Falling {abs(trend):.2f} L/min a day; likely to drop below "
                                   f"{FLOW_LOW_FRACTION:.0%} of normal {_when(days, now)}.")
                else:
                    result["prediction"] = f"Dropping slightly; no service expected in the next {PREDICT_DAYS} days."
            elif trend is None:
                result["prediction"] = "Not enough history for a trend yet."
            else:
                result["prediction"] = "Steady; no service predicted."
        return result

    # ------------------------------------------------------------------
    # valve (servo)
    # ------------------------------------------------------------------
    def valve(self, bay: dict, now: Optional[float] = None) -> dict:
        now = now or time.time()
        bay_number = bay["bay_number"]
        serviced = self.database.serviced_at(bay_number, "valve") or 0.0
        cycles = self.database.count_sessions(bay_number, since=serviced)
        per_day = self.database.count_sessions(bay_number, since=now - 7 * DAY) / 7
        remaining = VALVE_SERVICE_CYCLES - cycles
        result = {"equipment": "valve", "name": "Valve (servo)", "cycles": cycles, "per_day": round(per_day, 1),
                  "service_every": VALVE_SERVICE_CYCLES, "serviced_at": serviced or None, "days_until": None,
                  "reading": f"{cycles:,} of {VALVE_SERVICE_CYCLES:,} cycles used · "
                             f"{per_day:.0f}/day" if per_day >= 1 or per_day == 0 else
                             f"{cycles:,} of {VALVE_SERVICE_CYCLES:,} cycles used · {per_day:.1f}/day"}
        no_feedback = any(f["active"] and f["fault_type"] == "motor_feedback_missing" for f in bay.get("faults", []))
        if no_feedback:
            return {**result, "status": "down", "days_until": 0,
                    "prediction": "The valve was told to open but didn't respond. Repair now."}
        if remaining <= 0:
            return {**result, "status": "soon", "days_until": 0, "prediction": "Service due now."}
        if per_day <= 0:
            return {**result, "status": "ok", "prediction": "Not used this week; no service predicted."}
        days = remaining / per_day
        if days > PREDICT_DAYS:
            return {**result, "status": "ok", "prediction": f"No service expected in the next {PREDICT_DAYS} days."}
        return {**result, "status": "soon" if days <= 7 else "ok", "days_until": round(days, 1),
                "prediction": f"Service due {_when(days, now)}, at the current {per_day:.0f} washes a day."}

    # ------------------------------------------------------------------
    # car counter (ultrasonic)
    # ------------------------------------------------------------------
    def counter(self, bay: dict, now: Optional[float] = None) -> dict:
        now = now or time.time()
        bay_number = bay["bay_number"]
        serviced = self.database.serviced_at(bay_number, "counter") or 0.0
        since_day = max(serviced, now - DAY)
        cars_day = len(self.database.readings(bay_number, "car", since=since_day))
        washes_day = self.database.count_sessions(bay_number, since=since_day)
        cars_week = len(self.database.readings(bay_number, "car", since=now - 7 * DAY))
        result = {"equipment": "counter", "name": "Car counter", "cars_24h": cars_day, "washes_24h": washes_day,
                  "cars_7d": cars_week, "serviced_at": serviced or None, "days_until": None,
                  "reading": f"{_plural(cars_day, 'car')} and {_plural(washes_day, 'wash', 'washes')} in 24 h"}
        if not bay.get("vehicle_sensor_installed"):
            return {**result, "status": "not_installed", "reading": "No car counter on this bay's firmware",
                    "prediction": ""}
        if washes_day >= 3 and cars_day == 0:
            return {**result, "status": "down", "days_until": 0,
                    "prediction": f"{washes_day} washes but no cars counted: the sensor may be blocked or broken."}
        if washes_day >= 5 and cars_day < washes_day * 0.5:
            return {**result, "status": "failing", "days_until": 0,
                    "prediction": "Counting far fewer cars than washes: check the sensor's aim and wiring."}
        return {**result, "status": "ok", "prediction": f"Counting normally ({_plural(cars_week, 'car')} this week)."}

    # ------------------------------------------------------------------
    # all bays
    # ------------------------------------------------------------------
    def report(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        bays = []
        counts = {status: 0 for status in STATUS_LABELS}
        for summary in self.database.list_bays():
            bay = self.database.get_bay(summary["bay_number"])
            equipment = [self.water(bay, now), self.valve(bay, now), self.counter(bay, now)]
            for item in equipment:
                item["status_label"] = STATUS_LABELS[item["status"]]
                counts[item["status"]] += 1
            bays.append({"bay_number": bay["bay_number"], "label": bay["label"], "source": bay["source"],
                         "equipment": equipment})
        return {"bays": bays, "counts": counts, "generated_at": now,
                "settings": {"flow_low_fraction": FLOW_LOW_FRACTION, "flow_none_lpm": FLOW_NONE_LPM,
                             "valve_service_cycles": VALVE_SERVICE_CYCLES}}

    # ------------------------------------------------------------------
    # alerts
    # ------------------------------------------------------------------
    def evaluate_after_run(self, bay_number: int, source: str) -> None:
        """After a wash: raise or clear the water-flow and valve-service alerts."""
        bay = self.database.get_bay(bay_number)
        water = self.water(bay)
        if water["status"] == "down":
            self.no_flow_alert(bay_number, source, water["reading"])
            self.database.resolve_faults(bay_number, "flow_low")
        elif water["status"] == "failing":
            self.database.add_fault(
                bay_number, source, "flow_low", "warning",
                f"Water flow is low: {water['reading']}. The pump or nozzle may be malfunctioning; "
                "check it soon.",
            )
            self.database.resolve_faults(bay_number, "flow_none")
        elif water["status"] in ("ok", "soon"):
            self.database.resolve_faults(bay_number, "flow_low")
            self.database.resolve_faults(bay_number, "flow_none")
        valve = self.valve(bay)
        if valve["days_until"] == 0 and valve["status"] == "soon":
            self.database.add_fault(
                bay_number, source, "valve_service_due", "warning",
                f"The valve has done {valve['cycles']:,} cycles since its last service "
                f"(service every {VALVE_SERVICE_CYCLES:,}). Service it and press Mark serviced.",
            )

    def no_flow_alert(self, bay_number: int, source: str, detail: str = "") -> None:
        self.database.add_fault(
            bay_number, source, "flow_none", "warning",
            "No water flow while the bay was active" + (f" ({detail})" if detail else "")
            + ". The pump, supply valve or hoses may not be working.",
        )

    def mark_serviced(self, bay_number: int, equipment: str) -> None:
        self.database.mark_serviced(bay_number, equipment)
        for fault_type in {"water": ("flow_low", "flow_none"), "valve": ("valve_service_due",),
                           "counter": ()}[equipment]:
            self.database.resolve_faults(bay_number, fault_type)
