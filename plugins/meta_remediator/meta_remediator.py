"""
Meta (Muse Spark) remediator plugin for logmedic.

Uses Meta's OpenAI-compatible Chat Completions API
(https://api.meta.ai/v1) to analyze log anomalies and propose fixes.
Can raise PRs via the GitHub REST API or SSH into hosts to apply fixes.

The shared propose/execute flow (prompt building, repo-context fetching,
PR/SSH execution, response parsing) lives in
``plugins/common/remediator_base.py`` — this module only handles
Meta-specific settings, model auto-discovery, and the chat-completions call.

Settings (passed via TOML config):
    meta_api_key: str   - Meta API key (fallbacks: muse_api_key, llama_api_key)
    model: str          - Model to use (default: muse-spark-1.3-contributor).
                          Use "latest-contributor" (or "latest"/"auto") to
                          auto-select the newest *-contributor model from
                          GET {base_url}/models.
    auto_latest_contributor: bool - Force auto-selection of the newest
                          *-contributor model regardless of `model` value
                          (default: false). `model` is used as fallback.
    meta_base_url: str  - API base URL (default: https://api.meta.ai/v1)
    reasoning_effort: str - Reasoning level, e.g. minimal/low/medium/high/
                          xhigh (default: low). Higher = better analysis but
                          more tokens (reasoning tokens count against
                          max_completion_tokens).
    max_tokens: int     - Max completion tokens incl. reasoning
                          (default: 16384)
    github_token: str   - GitHub token for raising PRs
    default_repo: str   - Default repo for PRs (e.g. "org/ansible-infra")
    local_repo_path: str - Local checkout of default_repo used for repo
                          context (e.g. "/home/cooper/repos/clc_ansible").
                          When set, files are read from disk instead of the
                          GitHub API (no token needed for context, sees
                          uncommitted state).
    auto_execute: bool  - Run mutating actions (PRs, SSH). When false
                          (default), they stay 'proposed' and nothing runs
    enable_ssh: bool    - Allow SSH command execution (default: false)
    ssh_key_path: str   - Path to SSH key for remote execution
    system_prompt: str  - Additional system context about your infrastructure

Env vars for the API key (checked in order): META_API_KEY, MUSE_API_KEY,
LLAMA_API_KEY.
"""

import json
import logging
import os
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from logmedic_common.remediator_base import (
        DEFAULT_MAX_TOKENS,
        BaseRemediatorPlugin,
        github,
        subprocess,
    )
except ImportError:  # daemon/test run: plugins/ is not on sys.path yet
    _PLUGINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, _PLUGINS_DIR)
    from logmedic_common.remediator_base import (
        DEFAULT_MAX_TOKENS,  # noqa: F401  (re-exported for backwards compat)
        BaseRemediatorPlugin,
        github,  # noqa: F401  (re-exported for test patches)
        subprocess,  # noqa: F401  (re-exported for test patches)
    )

log = logging.getLogger("logmedic.meta_remediator")

DEFAULT_MODEL = "muse-spark-1.3-contributor"
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_REASONING_EFFORT = "low"

# Values of `model` that trigger auto-discovery of the newest *-contributor
# model instead of using a pinned model ID.
#
# NOTE: Meta exposes `muse-spark-latest` but no `*-latest-contributor` alias
# (checked 2026-09-13: `muse-spark-latest-contributor` 404s as model_not_found).
# The client-side discovery in _resolve_model() below is the workaround — it
# does what that alias would do (GET /v1/models, pick newest *-contributor).
# If Meta ever ships the alias, prefer it and drop the extra models call.
# Feature request belongs with Meta (developers.meta.com / Muse dashboard),
# not this repo.
LATEST_ALIASES = {"latest", "latest-contributor", "auto"}


class RemediatorPlugin(BaseRemediatorPlugin):
    """Meta remediator — Muse Spark chat-completions API plus the shared flow."""

    provider_label = "Meta"
    logger_name = "logmedic.meta_remediator"
    default_model = DEFAULT_MODEL

    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = (
            raw.get("meta_api_key")
            or raw.get("muse_api_key")
            or raw.get("llama_api_key")
            or os.environ.get("META_API_KEY")
            or os.environ.get("MUSE_API_KEY")
            or os.environ.get("LLAMA_API_KEY", "")
        )
        self.auto_latest = raw.get("auto_latest_contributor", False)
        self.base_url = raw.get("meta_base_url", DEFAULT_BASE_URL).rstrip("/")
        self.reasoning_effort = raw.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
        super().__init__(settings)
        log.debug(
            "initialized: model=%s auto_latest=%s base_url=%s reasoning=%s "
            "default_repo=%s local_repo=%s auto_execute=%s enable_ssh=%s "
            "max_tokens=%d api_key=%s",
            self.model,
            self.auto_latest,
            self.base_url,
            self.reasoning_effort,
            self.default_repo or "(none)",
            self.local_repo_path or "(none)",
            self.auto_execute,
            self.enable_ssh,
            self.max_tokens,
            "set" if self.api_key else "MISSING",
        )

    def name(self) -> str:
        return "meta_remediator"

    # ── Model resolution ────────────────────────────────────────────

    def _resolve_model(self) -> str:
        """Return the model ID to use for this propose() call.

        Auto-discovers the newest ``*-contributor`` model when
        ``model`` is a latest-alias (``latest-contributor``/``latest``/``auto``)
        or ``auto_latest_contributor`` is true. Falls back to the configured
        (or default) model if discovery fails.
        """
        is_alias = self.model.strip().lower() in LATEST_ALIASES
        fallback = DEFAULT_MODEL if is_alias else self.model
        if not (self.auto_latest or is_alias):
            return self.model
        try:
            available = self._list_models()
            latest = self.select_latest_contributor(available)
            if latest:
                log.info(
                    "auto-selected latest -contributor model: %s (from %d models)",
                    latest,
                    len(available),
                )
                return latest
            log.warning(
                "no -contributor models found in %d models, falling back to %s",
                len(available),
                fallback,
            )
        except Exception as e:  # noqa: BLE001  # any discovery failure -> pinned fallback
            log.warning(
                "latest -contributor model discovery failed, falling back to %s: %s",
                fallback,
                e,
            )
        return fallback

    def _list_models(self) -> list[str]:
        """List available model IDs via GET {base_url}/models."""
        url = f"{self.base_url}/models"
        log.debug("listing Meta models: GET %s", url)
        req = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            resp_body = e.read().decode(errors="replace")
            log.error("Meta models API error: %d %s — %s", e.code, e.reason, resp_body)
            raise RuntimeError(
                f"Meta models request failed ({e.code}): {resp_body}"
            ) from e
        # OpenAI-compatible shape: {"data": [{"id": "..."}, ...]}
        # Accept a bare list too for forward-compat.
        if isinstance(data, dict):
            entries = data.get("data", [])
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        ids = [
            e.get("id", "") if isinstance(e, dict) else str(e)
            for e in entries
            if isinstance(e, (dict, str))
        ]
        ids = [i for i in ids if i]
        log.debug("found %d Meta models", len(ids))
        return ids

    @staticmethod
    def select_latest_contributor(model_ids: list[str]) -> str | None:
        """Pick the newest ``*-contributor`` model ID.

        Contributor models are versioned like ``muse-spark-1.3-contributor``.
        The version numbers are compared numerically; models without a
        parseable version sort below versioned ones and ties break lexically
        so selection is deterministic.
        """
        candidates = [m for m in model_ids if m.endswith("-contributor")]
        if not candidates:
            return None

        def version_key(m: str) -> tuple:
            nums = [int(n) for n in re.findall(r"\d+", m)]
            return (1 if nums else 0, nums, m)

        return max(candidates, key=version_key)

    def _call_llm(self, system: str, user_msg: str, model: str) -> str:
        """Call Meta's OpenAI-compatible Chat Completions API."""
        log.debug("calling Meta API: model=%s base_url=%s", model, self.base_url)
        url = f"{self.base_url}/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                # NOTE: max_completion_tokens (not max_tokens) — reasoning
                # tokens count against this budget too.
                "max_completion_tokens": self.max_tokens,
                "reasoning_effort": self.reasoning_effort,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            }
        ).encode()

        req = Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            resp_body = e.read().decode(errors="replace")
            log.error("Meta API error: %d %s — %s", e.code, e.reason, resp_body)
            raise RuntimeError(
                f"Meta API request failed ({e.code}): {resp_body}"
            ) from e

        finish_reason = ""
        text = ""
        try:
            choice = data.get("choices", [])[0]
            finish_reason = choice.get("finish_reason", "?")
            msg = choice.get("message", {})
            text = msg.get("content", "") or ""
            # Some servers return a list of content parts
            if isinstance(text, list):
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in text
                )
        except (IndexError, AttributeError) as e:
            log.error("unexpected Meta API response shape: %s", e)
            log.debug("raw response: %.500s", json.dumps(data)[:500])
            raise RuntimeError(f"unexpected Meta API response shape: {e}") from e

        log.debug(
            "Meta API response: model=%s usage=%s finish_reason=%s",
            data.get("model", "?"),
            data.get("usage", {}),
            finish_reason,
        )
        if finish_reason == "length":
            log.warning(
                "Meta response truncated at max_completion_tokens=%d — consider "
                "increasing max_tokens in plugin settings",
                self.max_tokens,
            )

        return text
