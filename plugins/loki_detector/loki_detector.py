"""
Loki detector plugin for logmedic.

Queries Grafana Loki for high-frequency error/warning log lines.
Uses only Python stdlib (no third-party packages required).

Settings (passed via TOML config):
    loki_url: str        - Loki base URL (e.g. "http://loki:3100")
    org_id: str          - Optional Loki tenant/org ID header
    query: str           - LogQL query override (default: error/warn filter)
    extra_labels: str    - Additional label matchers (e.g. '{namespace="prod"}')
    limit: int           - Max log lines to fetch per query (default: 10000)
    deny_labels: list    - Skip anomalies whose stream labels match deny rules:
                           - "key=value" string entries use OR semantics
                           - nested ["k=v", "x=y"] entries use AND semantics
    deny_label_sets: list - Optional explicit AND-only rules, each as ["key=value", ...]
"""

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("logmedic.loki_detector")

# Loki `since` accepts Go duration syntax; we support the units likely in a config file.
_LOOKBACK_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_LOOKBACK_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_lookback_seconds(lookback: str) -> int | None:
    m = _LOOKBACK_RE.match(lookback)
    if not m:
        return None
    return int(m.group(1)) * _LOOKBACK_UNIT_SECS[m.group(2)]


class DetectorPlugin:
    @staticmethod
    def _parse_label_pair(entry: str) -> tuple[str, str] | None:
        if "=" not in entry:
            return None
        return tuple(entry.split("=", 1))

    @classmethod
    def _parse_label_set(cls, entry: list) -> frozenset[tuple[str, str]] | None:
        label_set = set()
        for set_entry in entry:
            if not isinstance(set_entry, str):
                return None
            label_pair = cls._parse_label_pair(set_entry)
            if label_pair is None:
                return None
            label_set.add(label_pair)
        if not label_set:
            return None
        return frozenset(label_set)

    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.loki_url = raw.get("loki_url", "http://localhost:3100")
        self.org_id = raw.get("org_id", "")
        self.extra_labels = raw.get("extra_labels", "")
        self.custom_query = raw.get("query", "")
        self.limit = int(raw.get("limit", 10000))
        self.deny_labels: set[tuple[str, str]] = set()
        self.deny_label_sets: list[frozenset[tuple[str, str]]] = []
        for entry in raw.get("deny_labels", []):
            if isinstance(entry, str):
                label_pair = self._parse_label_pair(entry)
                if label_pair is None:
                    log.warning(
                        "deny_labels entry %r has no '=' separator, skipping", entry
                    )
                else:
                    self.deny_labels.add(label_pair)
            elif isinstance(entry, list):
                label_set = self._parse_label_set(entry)
                if label_set is None:
                    log.warning(
                        "deny_labels compound entry %r is malformed, skipping", entry
                    )
                else:
                    self.deny_label_sets.append(label_set)
            else:
                log.warning(
                    "deny_labels entry %r is not a string or list, skipping", entry
                )
        for entry in raw.get("deny_label_sets", []):
            if not isinstance(entry, list):
                log.warning("deny_label_sets entry %r is not a list, skipping", entry)
                continue
            label_set = self._parse_label_set(entry)
            if label_set is None:
                log.warning("deny_label_sets entry %r is malformed, skipping", entry)
            else:
                self.deny_label_sets.append(label_set)
        log.debug(
            "initialized: loki_url=%s org_id=%s extra_labels=%s custom_query=%s limit=%d deny_labels=%s deny_label_sets=%s",
            self.loki_url,
            self.org_id or "(none)",
            self.extra_labels or "(none)",
            self.custom_query or "(default)",
            self.limit,
            self.deny_labels or "(none)",
            self.deny_label_sets or "(none)",
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
                "limit": str(self.limit),
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

        self._log_oldest_line(data, lookback)
        anomalies = self._analyze(data, threshold)
        log.debug("analysis complete: %d anomalies above threshold", len(anomalies))
        return anomalies

    def _log_oldest_line(self, data: dict, lookback: str) -> None:
        """Debug-log the oldest log line; warn if the lookback window was truncated."""
        oldest_ns: int | None = None
        oldest_line = ""
        total_lines = 0
        for stream in data.get("data", {}).get("result", []):
            for ts, line in stream.get("values", []):
                total_lines += 1
                try:
                    ns = int(ts)
                except (TypeError, ValueError):
                    continue
                if oldest_ns is None or ns < oldest_ns:
                    oldest_ns = ns
                    oldest_line = line
        if oldest_ns is None:
            log.debug("oldest log line: (no lines returned)")
            return
        iso = datetime.fromtimestamp(oldest_ns / 1e9, tz=timezone.utc).isoformat()
        log.debug(
            "oldest log line: ts=%s (%d ns) line=%.200s", iso, oldest_ns, oldest_line
        )

        # If Loki returned exactly `limit` lines AND the oldest is newer than the
        # lookback window start, the limit clipped our window — warn the operator.
        lookback_secs = _parse_lookback_seconds(lookback)
        if lookback_secs is None:
            log.debug("could not parse lookback %r; skipping coverage check", lookback)
            return
        expected_start_ns = time.time_ns() - lookback_secs * 1_000_000_000
        if total_lines >= self.limit and oldest_ns > expected_start_ns:
            shortfall_secs = (oldest_ns - expected_start_ns) / 1e9
            log.warning(
                "loki returned %d lines (hit limit=%d) but the oldest line is %.1fs "
                "newer than the requested lookback=%s start — the window was "
                "truncated. Consider increasing the `limit` setting in logmedic.toml.",
                total_lines,
                self.limit,
                shortfall_secs,
                lookback,
            )

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
            # Skip streams that satisfy any AND deny label set
            if self.deny_label_sets and any(
                all(stream_labels.get(k) == v for k, v in label_set)
                for label_set in self.deny_label_sets
            ):
                log.info("deny_label_sets suppressed stream: labels=%s", stream_labels)
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
