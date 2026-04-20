"""Tests for the Loki detector plugin."""

import json
import time
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
    return json.dumps(
        {
            "status": "success",
            "data": {"resultType": "streams", "result": result},
        }
    ).encode()


class TestDetectorInit(unittest.TestCase):
    def test_defaults(self):
        plugin = DetectorPlugin({"settings_json": "{}"})
        self.assertEqual(plugin.loki_url, "http://localhost:3100")
        self.assertEqual(plugin.org_id, "")
        self.assertEqual(plugin.extra_labels, "")
        self.assertEqual(plugin.custom_query, "")

    def test_custom_settings(self):
        plugin = DetectorPlugin(
            _make_settings(
                org_id="tenant-1",
                extra_labels='{namespace="prod"}',
                query='{app="myapp"} |= "error"',
            )
        )
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
        resp.read.return_value = _loki_response(
            [
                ({"app": "web"}, lines),
            ]
        )
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
        resp.read.return_value = _loki_response(
            [
                ({"app": "api", "namespace": "prod"}, lines),
            ]
        )
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
        resp.read.return_value = _loki_response(
            [
                ({"host": "worker-1"}, lines),
            ]
        )
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
        resp.read.return_value = _loki_response(
            [
                ({"service": "gateway"}, lines),
            ]
        )
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
        resp.read.return_value = _loki_response(
            [
                ({"pod": "api-1"}, [error_line] * 8),
                ({"pod": "api-2"}, [error_line] * 7),
            ]
        )
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


class TestDenyLabels(unittest.TestCase):
    """deny_labels setting suppresses matching anomalies."""

    @staticmethod
    def _label_set(**labels):
        """Return a deterministic frozenset representation for deny label set checks."""
        return frozenset(labels.items())

    def test_deny_labels_init_empty_by_default(self):
        plugin = DetectorPlugin(_make_settings())
        self.assertEqual(plugin.deny_labels, set())
        self.assertEqual(plugin.deny_label_sets, [])

    def test_deny_labels_parsed(self):
        plugin = DetectorPlugin(
            _make_settings(deny_labels=["app=homeassistant", "namespace=legacy"])
        )
        self.assertIn(("app", "homeassistant"), plugin.deny_labels)
        self.assertIn(("namespace", "legacy"), plugin.deny_labels)

    def test_deny_labels_malformed_entry_skipped(self):
        plugin = DetectorPlugin(_make_settings(deny_labels=["noequalssign"]))
        self.assertEqual(plugin.deny_labels, set())

    def test_deny_labels_empty_key_or_value_skipped(self):
        plugin = DetectorPlugin(_make_settings(deny_labels=["=value", "key="]))
        self.assertEqual(plugin.deny_labels, set())

    def test_deny_labels_compound_entry_parsed(self):
        plugin = DetectorPlugin(
            _make_settings(
                deny_labels=[
                    "app=homeassistant",
                    ["hostname=home2.cooperlees.com", "unit=systemd-networkd.service"],
                ]
            )
        )
        self.assertIn(("app", "homeassistant"), plugin.deny_labels)
        self.assertIn(
            self._label_set(
                hostname="home2.cooperlees.com", unit="systemd-networkd.service"
            ),
            plugin.deny_label_sets,
        )

    def test_deny_label_sets_parsed(self):
        plugin = DetectorPlugin(
            _make_settings(
                deny_label_sets=[
                    ["hostname=home2.cooperlees.com", "unit=systemd-networkd.service"]
                ]
            )
        )
        self.assertIn(
            self._label_set(
                hostname="home2.cooperlees.com", unit="systemd-networkd.service"
            ),
            plugin.deny_label_sets,
        )

    def test_deny_labels_malformed_compound_entry_skipped(self):
        plugin = DetectorPlugin(
            _make_settings(
                deny_labels=[["hostname=home2.cooperlees.com", "broken-entry"]]
            )
        )
        self.assertEqual(plugin.deny_label_sets, [])

    @patch("loki_detector.urlopen")
    def test_matching_anomaly_is_suppressed(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _loki_response(
            [
                ({"app": "homeassistant"}, ["ERROR: mqtt connection lost"] * 20),
                ({"app": "prometheus"}, ["ERROR: scrape target unreachable"] * 15),
            ]
        )
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings(deny_labels=["app=homeassistant"]))
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["labels"]["app"], "prometheus")

    @patch("loki_detector.urlopen")
    def test_multiple_deny_entries(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _loki_response(
            [
                ({"app": "homeassistant"}, ["ERROR: mqtt connection lost"] * 20),
                ({"namespace": "legacy"}, ["ERROR: deprecated api call"] * 15),
                ({"app": "grafana"}, ["ERROR: datasource timeout"] * 10),
            ]
        )
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(
            _make_settings(deny_labels=["app=homeassistant", "namespace=legacy"])
        )
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["labels"]["app"], "grafana")

    @patch("loki_detector.urlopen")
    def test_no_deny_list_passes_all(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _loki_response(
            [
                ({"app": "homeassistant"}, ["ERROR: mqtt connection lost"] * 20),
                ({"app": "grafana"}, ["ERROR: datasource timeout"] * 10),
            ]
        )
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(_make_settings())
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 2)

    @patch("loki_detector.urlopen")
    def test_compound_deny_entry_requires_all_labels(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _loki_response(
            [
                (
                    {
                        "hostname": "home2.cooperlees.com",
                        "unit": "systemd-networkd.service",
                    },
                    ["ERROR: noisy debug line"] * 20,
                ),
                (
                    {"hostname": "home2.cooperlees.com", "unit": "nginx.service"},
                    ["ERROR: nginx failed"] * 15,
                ),
            ]
        )
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = DetectorPlugin(
            _make_settings(
                deny_labels=[
                    ["hostname=home2.cooperlees.com", "unit=systemd-networkd.service"]
                ]
            )
        )
        anomalies = plugin.detect("1h", 5)

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["labels"]["unit"], "nginx.service")


class TestLogOldestLine(unittest.TestCase):
    """_log_oldest_line finds the minimum-timestamp line across all streams."""

    def test_finds_oldest_across_streams(self):
        # 5 entries across 2 streams, out of order; the global min is ts=1000...
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"app": "a"},
                        "values": [
                            ["3000000000000000000", "third line"],
                            ["5000000000000000000", "fifth line"],
                        ],
                    },
                    {
                        "stream": {"app": "b"},
                        "values": [
                            ["4000000000000000000", "fourth line"],
                            ["1000000000000000000", "the oldest line"],
                            ["2000000000000000000", "second line"],
                        ],
                    },
                ],
            },
        }

        plugin = DetectorPlugin(_make_settings())
        with self.assertLogs("logmedic.loki_detector", level="DEBUG") as cm:
            plugin._log_oldest_line(data, "1h")

        joined = "\n".join(cm.output)
        self.assertIn("the oldest line", joined)
        self.assertIn("1000000000000000000 ns", joined)
        # 1e18 ns = 2001-09-09T01:46:40+00:00
        self.assertIn("2001-09-09T01:46:40+00:00", joined)

    def test_warns_when_limit_hit_and_window_truncated(self):
        """limit=5 returned and oldest is only 30s ago under a 1h lookback → warn."""
        now_ns = time.time_ns()
        thirty_secs_ago = now_ns - 30 * 1_000_000_000
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"app": "noisy"},
                        "values": [
                            [str(thirty_secs_ago + i), f"line {i}"] for i in range(5)
                        ],
                    },
                ],
            },
        }

        plugin = DetectorPlugin(_make_settings(limit=5))
        with self.assertLogs("logmedic.loki_detector", level="DEBUG") as cm:
            plugin._log_oldest_line(data, "1h")

        joined = "\n".join(cm.output)
        self.assertIn("WARNING", joined)
        self.assertIn("hit limit=5", joined)
        self.assertIn("lookback=1h", joined)
        self.assertIn("increasing the `limit`", joined)

    def test_no_warning_when_below_limit(self):
        """Fewer lines than the limit → window wasn't clipped, no warning."""
        now_ns = time.time_ns()
        thirty_secs_ago = now_ns - 30 * 1_000_000_000
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"app": "quiet"},
                        "values": [
                            [str(thirty_secs_ago + i), f"line {i}"] for i in range(3)
                        ],
                    },
                ],
            },
        }

        plugin = DetectorPlugin(_make_settings(limit=10000))
        with self.assertLogs("logmedic.loki_detector", level="DEBUG") as cm:
            plugin._log_oldest_line(data, "1h")

        joined = "\n".join(cm.output)
        self.assertNotIn("WARNING", joined)

    def test_no_warning_when_oldest_reaches_lookback(self):
        """At-limit but oldest line covers the lookback → no warning."""
        now_ns = time.time_ns()
        one_hour_ns = 3600 * 1_000_000_000
        # Place 5 lines spanning from ~1h ago to ~50min ago — full window covered.
        data = {
            "data": {
                "result": [
                    {
                        "stream": {"app": "ok"},
                        "values": [
                            [str(now_ns - one_hour_ns + i * 10_000_000_000), f"l{i}"]
                            for i in range(5)
                        ],
                    },
                ],
            },
        }

        plugin = DetectorPlugin(_make_settings(limit=5))
        with self.assertLogs("logmedic.loki_detector", level="DEBUG") as cm:
            plugin._log_oldest_line(data, "1h")

        joined = "\n".join(cm.output)
        self.assertNotIn("WARNING", joined)


if __name__ == "__main__":
    unittest.main()
