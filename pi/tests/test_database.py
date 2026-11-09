import tempfile
import unittest
from pathlib import Path

from carwash.storage.database import MonitorDatabase


class MonitorDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.sqlite3"
        self.database = MonitorDatabase(self.db_path)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialization_creates_eleven_bays_and_is_persistent(self):
        bays = self.database.list_bays()
        self.assertEqual(len(bays), 11)
        self.assertEqual(bays[0]["source"], "hardware")
        self.assertTrue(all(bay["source"] == "simulator" for bay in bays[1:]))

        self.database.update_settings(
            2,
            {
                "label": "North Bay",
                "enabled": 1,
                "default_wash_mode": "Foam",
                "base_price_cents": 725,
                "included_seconds": 90,
                "additional_price_per_minute_cents": 125,
                "maximum_duration_seconds": 240,
            },
        )
        reopened = MonitorDatabase(self.db_path)
        self.assertEqual(reopened.get_bay(2)["label"], "North Bay")
        self.assertEqual(reopened.get_bay(2)["base_price_cents"], 725)


if __name__ == "__main__":
    unittest.main()
