"""SQLite persistence for bays, pricing, wash sessions, and incidents."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from carwash.config import BAY_COUNT, DATABASE_PATH, HARDWARE_BAY


class MonitorDatabase:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DATABASE_PATH

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bays (
                    bay_number INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('hardware', 'simulator')),
                    connection_source TEXT NOT NULL DEFAULT 'hardware'
                        CHECK (connection_source IN ('hardware', 'simulator')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available', 'occupied', 'faulted', 'offline')),
                    wash_mode TEXT NOT NULL DEFAULT 'Standard',
                    default_wash_mode TEXT NOT NULL DEFAULT 'Standard',
                    vehicle_sensor_installed INTEGER NOT NULL DEFAULT 0,
                    water_sensor_installed INTEGER NOT NULL DEFAULT 0,
                    motor_feedback_installed INTEGER NOT NULL DEFAULT 0,
                    rain_sensor_installed INTEGER NOT NULL DEFAULT 0,
                    vehicle_present INTEGER,
                    water_detected INTEGER,
                    rain_detected INTEGER,
                    rain_raw INTEGER,
                    rain_wet_percent REAL,
                    rain_updated_at REAL,
                    motor_running INTEGER,
                    motor_remaining_s REAL,
                    motor_updated_at REAL,
                    last_seen_at REAL,
                    fault_message TEXT
                );

                CREATE TABLE IF NOT EXISTS pricing_settings (
                    bay_number INTEGER PRIMARY KEY REFERENCES bays(bay_number),
                    base_price_cents INTEGER NOT NULL CHECK (base_price_cents >= 0),
                    included_seconds INTEGER NOT NULL CHECK (included_seconds >= 0),
                    additional_price_per_minute_cents INTEGER NOT NULL
                        CHECK (additional_price_per_minute_cents >= 0),
                    maximum_duration_seconds INTEGER NOT NULL CHECK (maximum_duration_seconds > 0),
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bay_number INTEGER NOT NULL REFERENCES bays(bay_number),
                    source TEXT NOT NULL CHECK (source IN ('hardware', 'simulator')),
                    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'aborted')),
                    wash_mode TEXT NOT NULL,
                    purchased_seconds INTEGER NOT NULL CHECK (purchased_seconds >= 0),
                    granted_seconds INTEGER NOT NULL CHECK (granted_seconds >= 0),
                    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                    anomaly_estimate_cents INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    ended_at REAL
                );

                CREATE TABLE IF NOT EXISTS faults (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bay_number INTEGER NOT NULL REFERENCES bays(bay_number),
                    source TEXT NOT NULL CHECK (source IN ('hardware', 'simulator')),
                    fault_type TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
                    explanation TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    related_session_id INTEGER REFERENCES sessions(id),
                    anomaly_estimate_cents INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS sessions_bay_started
                    ON sessions(bay_number, started_at DESC);
                CREATE INDEX IF NOT EXISTS faults_active_created
                    ON faults(active, created_at DESC);
                PRAGMA user_version = 1;
                """
            )
            now = time.time()
            for number in range(1, BAY_COUNT + 1):
                physical = number == HARDWARE_BAY
                source = "hardware" if physical else "simulator"
                db.execute(
                    """
                    INSERT OR IGNORE INTO bays (
                        bay_number, label, source, status, wash_mode, default_wash_mode,
                        vehicle_sensor_installed, water_sensor_installed,
                        motor_feedback_installed, rain_sensor_installed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        number,
                        f"Bay {number}",
                        source,
                        "offline" if physical else "available",
                        "Standard",
                        "Standard",
                        0 if physical else 1,
                        0 if physical else 1,
                        0 if physical else 1,
                        1 if physical else 0,
                    ),
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO pricing_settings (
                        bay_number, base_price_cents, included_seconds,
                        additional_price_per_minute_cents, maximum_duration_seconds, updated_at
                    ) VALUES (?, 500, 120, 100, 300, ?)
                    """,
                    (number, now),
                )

    def list_bays(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT b.*, p.base_price_cents, p.included_seconds,
                       p.additional_price_per_minute_cents, p.maximum_duration_seconds,
                       (SELECT COUNT(*) FROM faults f
                        WHERE f.bay_number = b.bay_number AND f.active = 1) AS active_fault_count,
                       (SELECT MAX(s.started_at) FROM sessions s
                        WHERE s.bay_number = b.bay_number) AS last_session_at,
                       (SELECT s.started_at FROM sessions s
                        WHERE s.bay_number = b.bay_number AND s.status = 'active') AS active_started_at,
                       (SELECT s.granted_seconds FROM sessions s
                        WHERE s.bay_number = b.bay_number AND s.status = 'active') AS active_granted_seconds
                FROM bays b JOIN pricing_settings p USING (bay_number)
                ORDER BY b.bay_number
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_bay(self, bay_number: int) -> Optional[dict]:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT b.*, p.base_price_cents, p.included_seconds,
                       p.additional_price_per_minute_cents, p.maximum_duration_seconds
                FROM bays b JOIN pricing_settings p USING (bay_number)
                WHERE b.bay_number = ?
                """,
                (bay_number,),
            ).fetchone()
            if row is None:
                return None
            bay = dict(row)
            bay["sessions"] = [
                dict(session)
                for session in db.execute(
                    "SELECT * FROM sessions WHERE bay_number = ? ORDER BY started_at DESC LIMIT 10",
                    (bay_number,),
                ).fetchall()
            ]
            bay["faults"] = [
                dict(fault)
                for fault in db.execute(
                    "SELECT * FROM faults WHERE bay_number = ? ORDER BY created_at DESC LIMIT 10",
                    (bay_number,),
                ).fetchall()
            ]
            bay["active_fault_count"] = db.execute(
                "SELECT COUNT(*) FROM faults WHERE bay_number = ? AND active = 1",
                (bay_number,),
            ).fetchone()[0]
            return bay

    def update_settings(self, bay_number: int, values: dict) -> None:
        now = time.time()
        with self.connection() as db:
            db.execute(
                """
                UPDATE bays SET label = ?, enabled = ?, default_wash_mode = ?
                WHERE bay_number = ?
                """,
                (values["label"], values["enabled"], values["default_wash_mode"], bay_number),
            )
            db.execute(
                """
                UPDATE pricing_settings SET base_price_cents = ?, included_seconds = ?,
                    additional_price_per_minute_cents = ?, maximum_duration_seconds = ?,
                    updated_at = ? WHERE bay_number = ?
                """,
                (
                    values["base_price_cents"],
                    values["included_seconds"],
                    values["additional_price_per_minute_cents"],
                    values["maximum_duration_seconds"],
                    now,
                    bay_number,
                ),
            )

    def update_bay(self, bay_number: int, values: dict) -> None:
        with self.connection() as db:
            assignments = ", ".join(f"{key} = ?" for key in values)
            db.execute(
                f"UPDATE bays SET {assignments} WHERE bay_number = ?",
                (*values.values(), bay_number),
            )

    def start_session(
        self,
        bay_number: int,
        source: str,
        mode: str,
        purchased_seconds: int,
        granted_seconds: int,
        price_cents: int,
        anomaly_estimate_cents: int = 0,
    ) -> int:
        now = time.time()
        with self.connection() as db:
            active = db.execute(
                "SELECT id FROM sessions WHERE bay_number = ? AND status = 'active'",
                (bay_number,),
            ).fetchone()
            if active:
                raise ValueError(f"Bay {bay_number} already has an active session")
            cursor = db.execute(
                """
                INSERT INTO sessions (
                    bay_number, source, status, wash_mode, purchased_seconds,
                    granted_seconds, price_cents, anomaly_estimate_cents, started_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    bay_number, source, mode, purchased_seconds, granted_seconds,
                    price_cents, anomaly_estimate_cents, now,
                ),
            )
            db.execute(
                """
                UPDATE bays SET status = 'occupied', vehicle_present = 1,
                    water_detected = CASE WHEN source = 'simulator' THEN 1 ELSE water_detected END,
                    wash_mode = ? WHERE bay_number = ?
                """,
                (mode, bay_number),
            )
            return int(cursor.lastrowid)

    def end_session(self, bay_number: int, status: str = "completed") -> Optional[int]:
        now = time.time()
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM sessions WHERE bay_number = ? AND status = 'active'",
                (bay_number,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
                (status, now, row["id"]),
            )
            db.execute(
                """
                UPDATE bays SET status = CASE WHEN source = 'hardware' AND last_seen_at IS NULL
                        THEN 'offline'
                        WHEN EXISTS (SELECT 1 FROM faults
                            WHERE faults.bay_number = bays.bay_number AND faults.active = 1
                                AND faults.severity = 'critical') THEN 'faulted'
                        ELSE 'available' END,
                    vehicle_present = CASE WHEN source = 'simulator' THEN 0 ELSE vehicle_present END,
                    water_detected = CASE WHEN source = 'simulator' THEN 0 ELSE water_detected END,
                    motor_running = CASE WHEN source = 'simulator' THEN 0 ELSE motor_running END,
                    motor_remaining_s = CASE WHEN source = 'simulator' THEN 0 ELSE motor_remaining_s END,
                    wash_mode = default_wash_mode
                WHERE bay_number = ?
                """,
                (bay_number,),
            )
            return int(row["id"])

    def add_fault(
        self,
        bay_number: int,
        source: str,
        fault_type: str,
        severity: str,
        explanation: str,
        related_session_id: Optional[int] = None,
        anomaly_estimate_cents: int = 0,
    ) -> int:
        now = time.time()
        with self.connection() as db:
            row = db.execute(
                """
                SELECT id FROM faults WHERE bay_number = ? AND fault_type = ? AND active = 1
                """,
                (bay_number, fault_type),
            ).fetchone()
            if row:
                return int(row["id"])
            cursor = db.execute(
                """
                INSERT INTO faults (
                    bay_number, source, fault_type, severity, explanation,
                    created_at, related_session_id, anomaly_estimate_cents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bay_number, source, fault_type, severity, explanation,
                    now, related_session_id, anomaly_estimate_cents,
                ),
            )
            if severity == "critical":
                db.execute(
                    "UPDATE bays SET status = 'faulted', fault_message = ? WHERE bay_number = ?",
                    (explanation, bay_number),
                )
            return int(cursor.lastrowid)

    def resolve_faults(self, bay_number: int, fault_type: str) -> None:
        with self.connection() as db:
            db.execute(
                """
                UPDATE faults SET active = 0, resolved_at = ?
                WHERE bay_number = ? AND fault_type = ? AND active = 1
                """,
                (time.time(), bay_number, fault_type),
            )
            remaining = db.execute(
                "SELECT COUNT(*) FROM faults WHERE bay_number = ? AND active = 1",
                (bay_number,),
            ).fetchone()[0]
            if not remaining:
                db.execute(
                    """
                    UPDATE bays SET fault_message = NULL,
                        status = CASE
                            WHEN source = 'hardware' AND last_seen_at IS NULL THEN 'offline'
                            WHEN EXISTS (SELECT 1 FROM sessions
                                WHERE sessions.bay_number = bays.bay_number AND status = 'active')
                                THEN 'occupied'
                            ELSE 'available'
                        END
                    WHERE bay_number = ?
                    """,
                    (bay_number,),
                )

    def resolve_fault(self, fault_id: int) -> Optional[int]:
        with self.connection() as db:
            row = db.execute(
                "SELECT bay_number FROM faults WHERE id = ? AND active = 1",
                (fault_id,),
            ).fetchone()
            if row is None:
                return None
            bay_number = int(row["bay_number"])
            db.execute(
                "UPDATE faults SET active = 0, resolved_at = ? WHERE id = ?",
                (time.time(), fault_id),
            )
            db.execute(
                """
                UPDATE bays SET
                    fault_message = (SELECT explanation FROM faults
                        WHERE bay_number = ? AND active = 1 AND severity = 'critical'
                        ORDER BY created_at DESC LIMIT 1),
                    status = CASE
                        WHEN source = 'hardware' AND last_seen_at IS NULL THEN 'offline'
                        WHEN EXISTS (SELECT 1 FROM faults
                            WHERE faults.bay_number = bays.bay_number AND faults.active = 1
                                AND faults.severity = 'critical') THEN 'faulted'
                        WHEN EXISTS (SELECT 1 FROM sessions
                            WHERE sessions.bay_number = bays.bay_number AND status = 'active')
                            THEN 'occupied'
                        ELSE 'available'
                    END
                WHERE bay_number = ?
                """,
                (bay_number, bay_number),
            )
            return bay_number

    def recent_faults(self, limit: int = 8) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT f.*, b.label FROM faults f JOIN bays b USING (bay_number)
                    ORDER BY f.created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]

    def dashboard(self) -> dict:
        bays = self.list_bays()
        with self.connection() as db:
            simulated_revenue = db.execute(
                "SELECT COALESCE(SUM(price_cents), 0) FROM sessions WHERE source = 'simulator'"
            ).fetchone()[0]
            visits = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            runs_today = db.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (midnight,)
            ).fetchone()[0]
            active_faults = db.execute("SELECT COUNT(*) FROM faults WHERE active = 1").fetchone()[0]
        return {
            "bays": bays,
            "simulated_revenue_cents": simulated_revenue,
            "active_bays": sum(bay["status"] == "occupied" for bay in bays),
            "available_bays": sum(
                bay["status"] == "available" and bool(bay["enabled"]) for bay in bays
            ),
            "faulted_bays": sum(bay["status"] == "faulted" for bay in bays),
            "customer_visits": visits,
            "runs_today": runs_today,
            "active_faults": active_faults,
            "recent_faults": self.recent_faults(),
        }

    def analytics(self) -> dict:
        with self.connection() as db:
            bays = []
            now = time.time()
            for row in db.execute(
                """
                SELECT b.bay_number, b.label, b.source,
                    COALESCE(SUM(CASE WHEN s.source = 'simulator' THEN s.price_cents ELSE 0 END), 0)
                        AS revenue_cents,
                    COUNT(s.id) AS sessions,
                    COALESCE(SUM(CASE WHEN s.ended_at IS NOT NULL
                        THEN MIN(s.ended_at - s.started_at, s.granted_seconds)
                        ELSE MIN(? - s.started_at, s.granted_seconds) END), 0) AS used_seconds,
                    COALESCE(SUM(s.anomaly_estimate_cents), 0) AS anomaly_estimate_cents
                FROM bays b LEFT JOIN sessions s USING (bay_number)
                GROUP BY b.bay_number ORDER BY b.bay_number
                """,
                (now,),
            ).fetchall():
                item = dict(row)
                item["utilization_percent"] = round(
                    min(100.0, item["used_seconds"] * 100.0 / 86400.0), 1
                )
                bays.append(item)
            faults = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT fault_type, COUNT(*) AS count,
                        COALESCE(SUM(anomaly_estimate_cents), 0) AS anomaly_estimate_cents
                    FROM faults GROUP BY fault_type ORDER BY count DESC
                    """
                ).fetchall()
            ]
            totals = db.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN source = 'simulator' THEN price_cents ELSE 0 END), 0)
                    AS simulated_revenue_cents,
                    COUNT(*) AS sessions,
                    COALESCE(SUM(anomaly_estimate_cents), 0) AS anomaly_estimate_cents
                FROM sessions
                """
            ).fetchone()
        return {"bays": bays, "faults": faults, "totals": dict(totals)}
