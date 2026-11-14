from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class SchedulingTests(unittest.TestCase):
    def test_export_is_daily_shanghai_without_registering(self):
        from jobhunt.scheduling import export_task
        from jobhunt.config import Context
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(Path(directory), True)
            output = export_task(ctx)
            root = ET.parse(output).getroot()
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            self.assertTrue(root.find(".//t:StartBoundary", ns).text.endswith("T01:00:00+08:00"))
            self.assertEqual(root.find(".//t:DaysInterval", ns).text, "1")
            arguments = root.find(".//t:Arguments", ns).text
            self.assertIn("--once-per-day", arguments)
            self.assertIn("--demo", arguments)
            self.assertNotIn("--execute", arguments)


if __name__ == "__main__":
    unittest.main()
