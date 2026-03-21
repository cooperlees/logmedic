"""
Loki detector plugin for logmedic.

Queries Grafana Loki for high-frequency error/warning log lines.
Requires `requests` to be installed in the Python environment.

Settings (passed via TOML config):
    loki_url: str        - Loki base URL (e.g. "http://loki:3100")
    org_id: str          - Optional Loki tenant/org ID header
    query: str           - LogQL query override (default: error/warn filter)
    extra_labels: str    - Additional label matchers (e.g. '{namespace="prod"}')
    deny_labels: list    - Skip anomalies whose stream labels match any "key=value" entry
                           (e.g. ["app=homeassistant", "namespace=legacy"])
"""

import json
import logging
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("logmedic.loki_detector")


class DetectorPlugin:
    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.loki_url = raw.get("loki_url", "http://localhost:3100")
        self.org_id = raw.get("org_id", "")
        self.extra_labels = raw.get("extra_labels", "")
        self.custom_query = raw.get("query", "")
        self.deny_labels: set[tuple[str, str]] = set()
        for entry in raw.get("deny_labels", []):
            if "=" in entry:
                k, v = entry.split("=", 1)
                self.deny_labels.add((k, v))
            else:
                log.warning(
                    "deny_labels entry %r has no '=' separator, skipping", entry
                )
        log.debug(
            "initialized: loki_url=%s org_id=%s extra_labels=%s custom_query=%s deny_labels=%s",
            self.loki_url,
            self.org_id or "(none)",
            self.extra_labels or "(none)",
            self.custom_query or "(default)",
            self.deny_labels or "(none)",
        )

    def name(self) -> str:
        return "loki_detector"

    def detect(self, lookback: str, threshold: int) -> list:
        """Query Loki and return high-frequency error/warning patterns."""
        query = self.custom_query or self._default_query()
        log.debug(
            "detect called: lookback=%s threshold=%d query=%s",
            lookback,
            threshold,
            query,
        )

        params = urlencode(
            {
                "query": query,
                "since": lookback,
                "limit": "5000",
                "direction": "backward",
            }
        )
        url = f"{self.loki_url}/loki/api/v1/query_range?{params}"
        log.debug("requesting %s", url)

        headers = {"Accept": "application/json"}
        if self.org_id:
            headers["X-Scope-OrgID"] = self.org_id

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                log.debug(
                    "loki response: status=%s resultType=%s streams=%d",
                    resp.status,
                    data.get("data", {}).get("resultType", "?"),
                    len(data.get("data", {}).get("result", [])),
                )
        except Exception as e:
            log.error("query failed: %s", e)
            return []

        anomalies = self._analyze(data, threshold)
        log.debug("analysis complete: %d anomalies above threshold", len(anomalies))
        return anomalies

    def _default_query(self) -> str:
        # Loki requires at least one label matcher; use a match-all if none configured
        labels = self.extra_labels or '{__name__=~".+"}'
        return f'{labels} |~ "(?i)(error|warn|fatal|panic|exception)"'

    def _analyze(self, data: dict, threshold: int) -> list:
        """Group log lines by pattern and find high-frequency ones."""
        line_counter = Counter()
        samples_map = {}
        labels_map = {}

        results = data.get("data", {}).get("result", [])

        total_lines = 0
        skipped_streams = 0
        for stream in results:
            stream_labels = stream.get("stream", {})

            # Skip streams whose labels match any deny_labels entry
            if self.deny_labels and not self.deny_labels.isdisjoint(
                stream_labels.items()
            ):
                log.info("deny_labels suppressed stream: labels=%s", stream_labels)
                skipped_streams += 1
                continue

            values = stream.get("values", [])
            for _ts, line in values:
                total_lines += 1
                # Simple pattern: normalize numbers and UUIDs
                pattern = self._normalize(line)
                line_counter[pattern] += 1
                if pattern not in samples_map:
                    samples_map[pattern] = []
                    labels_map[pattern] = stream_labels
                if len(samples_map[pattern]) < 3:
                    samples_map[pattern].append(line)

        log.debug(
            "processed %d log lines across %d streams (%d skipped by deny_labels), %d unique patterns",
            total_lines,
            len(results),
            skipped_streams,
            len(line_counter),
        )

        anomalies = []
        for pattern, count in line_counter.most_common():
            if count < threshold:
                break
            level = self._guess_level(pattern)
            anomalies.append(
                {
                    "pattern": pattern,
                    "count": count,
                    "level": level,
                    "labels": labels_map.get(pattern, {}),
                    "samples": samples_map.get(pattern, []),
                }
            )
            log.debug(
                "anomaly: count=%d level=%s pattern=%.120s", count, level, pattern
            )

        return anomalies

    def _normalize(self, line: str) -> str:
        """Collapse variable parts of log lines into placeholders."""
        import re

        # Replace UUIDs
        line = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "<UUID>",
            line,
            flags=re.IGNORECASE,
        )
        # Replace IP addresses
        line = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "<IP>", line)
        # Replace long numbers (timestamps, IDs)
        line = re.sub(r"\b\d{6,}\b", "<NUM>", line)
        # Replace hex sequences
        line = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", line)
        return line

    def _guess_level(self, text: str) -> str:
        t = text.lower()
        if "error" in t or "fatal" in t or "panic" in t or "exception" in t:
            return "error"
        if "warn" in t:
            return "warn"
        return "unknown"
