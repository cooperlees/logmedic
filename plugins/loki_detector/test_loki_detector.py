"""Tests for the Loki detector plugin."""

import json
import unittest
from unittest.mock import MagicMock, patch

from loki_detector import DetectorPlugin


def _make_settings(**overrides):
    raw = {"loki_url": "http://loki:3100"}
    raw.update(overrides)
    return {"settings_json": json.dumps(raw)}


def _loki_response(streams):
    """Build a Loki query_range JSON response from a list of (labels, lines) tuples."""
    result = []
    for labels, lines in streams:
        values = [[str(i), line] for i, line in enumerate(lines)]
        result.append({"stream": labels, "values": values})
    return json.dumps({
        "status": "success",
        "data": {"resultType": "streams", "result": result},
    }).encode()


class TestDetectorInit(unittest.TestCase):
    def test_defaults(self):
        plugin = DetectorPlugin({"settings_json": "{}"})
        self.assertEqual(plugin.loki_url, "http://localhost:3100")
        self.assertEqual(plugin.org_id, "")
        self.assertEqual(plugin.extra_labels, "")
        self.assertEqual(plugin.custom_query, "")

    def test_custom_settings(self):
        plugin = DetectorPlugin(_make_settings(
            org_id="tenant-1",
            extra_labels='{namespace="prod"}',
            query='{app="myapp"} |= "error"',
        ))
        self.assertEqual(plugin.loki_url, "http://loki:3100")
        self.assertEqual(plugin.org_id, "tenant-1")
        self.assertEqual(plugin.extra_labels, '{namespace="prod"}')
        self.assertEqual(plugin.custom_query, '{app="myapp"} |= "error"')

    def test_name(self):
        plugin = DetectorPlugin(_make_settings())
        self.assertEqual(plugin.name(), "loki_detector")


class TestDetectNoAnomalies(unittest.TestCase):
    """Loki returns data but nothing exceeds the threshold."""

    @patch("loki_detector.urlopen")
    def test_empty_result(self, mock_urlopen):
        """Loki returns zero streams → empty list."""
        resp = MagicMock()
        resp.read.return_value = _loki_response([])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 10)

        self.assertEqual(anomalies, [])
        mock_urlopen.assert_called_once()

    @patch("loki_detector.urlopen")
    def test_below_threshold(self, mock_urlopen):
        """Lines exist but all counts are below the threshold."""
        lines = [f"error: something happened on host-{i}" for i in range(5)]
        resp = MagicMock()
        resp.read.return_value = _loki_response([
            ({"app": "web"}, lines),
        ])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        # Each line is unique after normalization, so count=1 each
        anomalies = plugin.detect("1h", 10)

        self.assertEqual(anomalies, [])

    @patch("loki_detector.urlopen")
    def test_query_failure_returns_empty(self, mock_urlopen):
        """HTTP error → graceful empty return, not an exception."""
        mock_urlopen.side_effect = Exception("connection refused")

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(anomalies, [])


class TestDetectFindsAnomalies(unittest.TestCase):
    """Loki returns data with patterns exceeding the threshold."""

    @patch("loki_detector.urlopen")
    def test_repeated_error_detected(self, mock_urlopen):
        """Same error line repeated → detected as anomaly."""
        repeated = "ERROR: connection refused to database at 10.0.0.5:5432"
        lines = [repeated] * 20
        resp = MagicMock()
        resp.read.return_value = _loki_response([
            ({"app": "api", "namespace": "prod"}, lines),
        ])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 1)
        a = anomalies[0]
        self.assertEqual(a["count"], 20)
        self.assertEqual(a["level"], "error")
        self.assertIn("<IP>", a["pattern"])  # IP should be normalized
        self.assertEqual(a["labels"], {"app": "api", "namespace": "prod"})
        self.assertLessEqual(len(a["samples"]), 3)

    @patch("loki_detector.urlopen")
    def test_multiple_patterns(self, mock_urlopen):
        """Two distinct error patterns, both above threshold."""
        lines = (
            ["FATAL: out of memory"] * 15
            + ["WARN: disk usage high on /dev/sda1"] * 10
            + ["INFO: health check passed"] * 3  # below threshold
        )
        resp = MagicMock()
        resp.read.return_value = _loki_response([
            ({"host": "worker-1"}, lines),
        ])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("30m", 5)

        self.assertEqual(len(anomalies), 2)
        # Should be sorted by count descending (most_common)
        self.assertEqual(anomalies[0]["count"], 15)
        self.assertEqual(anomalies[0]["level"], "error")  # "fatal" maps to error
        self.assertEqual(anomalies[1]["count"], 10)
        self.assertEqual(anomalies[1]["level"], "warn")

    @patch("loki_detector.urlopen")
    def test_uuid_and_number_normalization(self, mock_urlopen):
        """Lines differing only by UUID/number should collapse into one pattern."""
        lines = [
            f"error processing request 550e8400-e29b-41d4-a716-44665544{i:04d}: timeout after 123456 ms"
            for i in range(25)
        ]
        resp = MagicMock()
        resp.read.return_value = _loki_response([
            ({"service": "gateway"}, lines),
        ])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["count"], 25)
        self.assertIn("<UUID>", anomalies[0]["pattern"])
        self.assertIn("<NUM>", anomalies[0]["pattern"])

    @patch("loki_detector.urlopen")
    def test_multiple_streams_merged(self, mock_urlopen):
        """Same pattern across multiple Loki streams should be combined."""
        error_line = "error: service unavailable"
        resp = MagicMock()
        resp.read.return_value = _loki_response([
            ({"pod": "api-1"}, [error_line] * 8),
            ({"pod": "api-2"}, [error_line] * 7),
        ])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 10)

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["count"], 15)

    @patch("loki_detector.urlopen")
    def test_org_id_header_sent(self, mock_urlopen):
        """When org_id is set, X-Scope-OrgID header should be included."""
        resp = MagicMock()
        resp.read.return_value = _loki_response([])
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings(org_id="my-tenant"))
        plugin.detect("1h", 10)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertEqual(req.get_header("X-scope-orgid"), "my-tenant")


class TestNormalize(unittest.TestCase):
    def setUp(self):
        self.plugin = DetectorPlugin(_make_settings())

    def test_uuid_replaced(self):
        line = "request 550e8400-e29b-41d4-a716-446655440000 failed"
        self.assertIn("<UUID>", self.plugin._normalize(line))

    def test_ip_replaced(self):
        line = "connection to 192.168.1.100 refused"
        self.assertIn("<IP>", self.plugin._normalize(line))

    def test_long_number_replaced(self):
        line = "timestamp 1234567890 expired"
        self.assertIn("<NUM>", self.plugin._normalize(line))

    def test_hex_replaced(self):
        line = "address 0xDEADBEEF invalid"
        self.assertIn("<HEX>", self.plugin._normalize(line))


if __name__ == "__main__":
    unittest.main()
