"""
Claude AI remediator plugin for logmedic.

Uses the Anthropic Claude API to analyze log anomalies and propose fixes.
Can raise PRs via the GitHub REST API or SSH into hosts to apply fixes.

The shared propose/execute flow (prompt building, repo-context fetching,
PR/SSH execution, response parsing) lives in
``plugins/common/remediator_base.py`` — this module only handles
Anthropic-specific settings and the Messages API call.

Settings (passed via TOML config):
    anthropic_api_key: str  - Anthropic API key
    model: str              - Model to use (default: claude-opus-4-6)
    max_tokens: int         - Max tokens for Claude response (default: 16384)
    github_token: str   - GitHub token for raising PRs
    default_repo: str   - Default repo for PRs (e.g. "org/ansible-infra")
    local_repo_path: str - Local checkout of default_repo used for repo
                          context instead of the GitHub API.
    auto_execute: bool  - Whether to auto-execute proposed actions (default: false)
    enable_ssh: bool    - Allow SSH command execution (default: false)
    ssh_key_path: str   - Path to SSH key for remote execution
    system_prompt: str  - Additional system context about your infrastructure
"""

import json
import logging
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from remediator_base import (
        DEFAULT_MAX_TOKENS,
        BaseRemediatorPlugin,
        github,
        subprocess,
    )
except ImportError:  # daemon/test run: plugins/common is not on sys.path yet
    _COMMON_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "common"
    )
    sys.path.insert(0, _COMMON_DIR)
    from remediator_base import (
        DEFAULT_MAX_TOKENS,  # noqa: F401  (re-exported for backwards compat)
        BaseRemediatorPlugin,
        github,  # noqa: F401  (re-exported for test patches)
        subprocess,  # noqa: F401  (re-exported for test patches)
    )


log = logging.getLogger("logmedic.claude_remediator")

DEFAULT_MODEL = "claude-opus-4-6"


class RemediatorPlugin(BaseRemediatorPlugin):
    """Claude remediator — Anthropic Messages API plus the shared flow."""

    provider_label = "Claude"
    logger_name = "logmedic.claude_remediator"
    default_model = DEFAULT_MODEL

    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = raw.get(
            "anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", "")
        )
        super().__init__(settings)
        log.debug(
            "initialized: model=%s default_repo=%s auto_execute=%s enable_ssh=%s max_tokens=%d api_key=%s",
            self.model,
            self.default_repo or "(none)",
            self.auto_execute,
            self.enable_ssh,
            self.max_tokens,
            "set" if self.api_key else "MISSING",
        )

    def name(self) -> str:
        return "claude_remediator"

    def _call_llm(self, system: str, user_msg: str, model: str) -> str:
        """Call the Anthropic Messages API."""
        log.debug("calling Claude API: model=%s", model)
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            }
        ).encode()

        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            resp_body = e.read().decode(errors="replace")
            log.error("Claude API error: %d %s — %s", e.code, e.reason, resp_body)
            raise RuntimeError(
                f"Claude API request failed ({e.code}): {resp_body}"
            ) from e

        stop_reason = data.get("stop_reason", "?")
        log.debug(
            "Claude API response: model=%s usage=%s stop_reason=%s",
            data.get("model", "?"),
            data.get("usage", {}),
            stop_reason,
        )
        if stop_reason == "max_tokens":
            log.warning(
                "Claude response truncated at max_tokens=%d — consider increasing "
                "max_tokens in plugin settings",
                self.max_tokens,
            )

        # Extract text from response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]
        return text
