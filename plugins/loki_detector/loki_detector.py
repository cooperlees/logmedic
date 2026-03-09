"""
Loki detector plugin for logmedic.

Queries Grafana Loki for high-frequency error/warning log lines.
Requires `requests` to be installed in the Python environment.

Settings (passed via TOML config):
    loki_url: str       - Loki base URL (e.g. "http://loki:3100")
    org_id: str         - Optional Loki tenant/org ID header
    query: str          - LogQL query override (default: error/warn filter)
    extra_labels: str   - Additional label matchers (e.g. '{namespace="prod"}')
"""

import json
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DetectorPlugin:
    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.loki_url = raw.get("loki_url", "http://localhost:3100")
        self.org_id = raw.get("org_id", "")
        self.extra_labels = raw.get("extra_labels", "")
        self.custom_query = raw.get("query", "")

    def name(self) -> str:
        return "loki_detector"

    def detect(self, lookback: str, threshold: int) -> list:
        """Query Loki and return high-frequency error/warning patterns."""
        query = self.custom_query or self._default_query()

        params = urlencode({
            "query": query,
            "since": lookback,
            "limit": "5000",
            "direction": "backward",
        })
        url = f"{self.loki_url}/loki/api/v1/query_range?{params}"

        headers = {"Accept": "application/json"}
        if self.org_id:
            headers["X-Scope-OrgID"] = self.org_id

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"[loki_detector] query failed: {e}")
            return []

        return self._analyze(data, threshold)

    def _default_query(self) -> str:
        labels = self.extra_labels or '{}'
        return f'{labels} |~ "(?i)(error|warn|fatal|panic|exception)"'

    def _analyze(self, data: dict, threshold: int) -> list:
        """Group log lines by pattern and find high-frequency ones."""
        line_counter = Counter()
        samples_map = {}
        labels_map = {}

        status = data.get("data", {}).get("resultType", "")
        results = data.get("data", {}).get("result", [])

        for stream in results:
            stream_labels = stream.get("stream", {})
            values = stream.get("values", [])
            for _ts, line in values:
                # Simple pattern: normalize numbers and UUIDs
                pattern = self._normalize(line)
                line_counter[pattern] += 1
                if pattern not in samples_map:
                    samples_map[pattern] = []
                    labels_map[pattern] = stream_labels
                if len(samples_map[pattern]) < 3:
                    samples_map[pattern].append(line)

        anomalies = []
        for pattern, count in line_counter.most_common():
            if count < threshold:
                break
            level = self._guess_level(pattern)
            anomalies.append({
                "pattern": pattern,
                "count": count,
                "level": level,
                "labels": labels_map.get(pattern, {}),
                "samples": samples_map.get(pattern, []),
            })

        return anomalies

    def _normalize(self, line: str) -> str:
        """Collapse variable parts of log lines into placeholders."""
        import re
        # Replace UUIDs
        line = re.sub(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            '<UUID>', line, flags=re.IGNORECASE
        )
        # Replace IP addresses
        line = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '<IP>', line)
        # Replace long numbers (timestamps, IDs)
        line = re.sub(r'\b\d{6,}\b', '<NUM>', line)
        # Replace hex sequences
        line = re.sub(r'0x[0-9a-fA-F]+', '<HEX>', line)
        return line

    def _guess_level(self, text: str) -> str:
        t = text.lower()
        if "error" in t or "fatal" in t or "panic" in t or "exception" in t:
            return "error"
        if "warn" in t:
            return "warn"
        return "unknown"
