"""
Claude AI remediator plugin for logmedic.

Uses the Anthropic Claude API to analyze log anomalies and propose fixes.
Can raise PRs via the GitHub REST API or SSH into hosts to apply fixes.

Settings (passed via TOML config):
    anthropic_api_key: str  - Anthropic API key
    model: str              - Model to use (default: claude-sonnet-4-20250514)
    github_token: str       - GitHub token for raising PRs
    default_repo: str       - Default repo for PRs (e.g. "org/ansible-infra")
    auto_execute: bool      - Whether to auto-execute proposed actions (default: false)
    enable_ssh: bool        - Allow SSH command execution (default: false)
    ssh_key_path: str       - Path to SSH key for remote execution
    system_prompt: str      - Additional system context about your infrastructure
"""

import json
import logging
import os
import subprocess
from urllib.request import Request, urlopen

import github

log = logging.getLogger("logmedic.claude_remediator")


class RemediatorPlugin:
    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = raw.get(
            "anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.model = raw.get("model", "claude-sonnet-4-20250514")
        self.github_token = raw.get("github_token", os.environ.get("GITHUB_TOKEN", ""))
        self.default_repo = raw.get("default_repo", "")
        self.auto_execute = raw.get("auto_execute", False)
        self.enable_ssh = raw.get("enable_ssh", False)
        self.ssh_key_path = raw.get("ssh_key_path", "")
        self.system_prompt = raw.get("system_prompt", "")
        log.debug(
            "initialized: model=%s default_repo=%s auto_execute=%s enable_ssh=%s api_key=%s",
            self.model,
            self.default_repo or "(none)",
            self.auto_execute,
            self.enable_ssh,
            "set" if self.api_key else "MISSING",
        )

    def name(self) -> str:
        return "claude_remediator"

    def propose(self, anomalies_json: str) -> str:
        """Send anomalies to Claude and get back proposed remediation actions."""
        anomalies = json.loads(anomalies_json)
        if not anomalies:
            log.debug("no anomalies to propose on, returning empty")
            return "[]"

        log.debug("proposing remediations for %d anomalies", len(anomalies))
        system = self._build_system_prompt()
        user_msg = self._build_user_prompt(anomalies)
        log.debug(
            "system prompt length=%d, user prompt length=%d", len(system), len(user_msg)
        )

        response = self._call_claude(system, user_msg)
        log.debug("claude response length=%d", len(response))
        actions = self._parse_response(response)
        log.debug("parsed %d actions from response", len(actions))
        return json.dumps(actions)

    def execute(self, action_json: str) -> str:
        """Execute a proposed remediation action."""
        action = json.loads(action_json)
        kind = action.get("kind", {})
        log.debug(
            "executing action: description=%s kind_keys=%s",
            action.get("description", "?"),
            list(kind.keys()),
        )

        if "pull_request" in kind:
            result = self._execute_pr(kind["pull_request"])
        elif "ssh_command" in kind:
            if not self.enable_ssh:
                log.warning("SSH action rejected: enable_ssh is false")
                result = {
                    "failed": {
                        "reason": "SSH execution is disabled (set enable_ssh = true to allow)"
                    }
                }
            else:
                result = self._execute_ssh(kind["ssh_command"])
        elif "report" in kind:
            result = {"report": {"message": kind["report"]["message"]}}
        else:
            result = {"failed": {"reason": "unknown action kind"}}

        log.debug("execute result: %s", list(result.keys()))
        return json.dumps(result)

    def _build_system_prompt(self) -> str:
        base = (
            "You are a senior SRE / DevOps engineer. You are given high-frequency "
            "log error patterns from production systems. Your job is to:\n"
            "1. Diagnose the root cause\n"
            "2. Propose concrete fixes\n"
            "3. Output your response as a JSON array of remediation actions\n\n"
            "Each action must be one of:\n"
            '- {"description": "...", "kind": {"pull_request": {"repo": "org/repo", '
            '"branch": "fix/...", "title": "...", "body": "...", '
            '"files_changed": [{"path": "...", "content": "..."}]}}, '
            '"status": "proposed"}\n'
            '- {"description": "...", "kind": {"ssh_command": {"host": "...", '
            '"commands": ["..."]}}, "status": "proposed"}\n'
            '- {"description": "...", "kind": {"report": {"message": "..."}}, '
            '"status": "proposed"}\n\n'
            "Output ONLY the JSON array, no markdown fences or explanation."
        )
        if self.system_prompt:
            base += f"\n\nAdditional infrastructure context:\n{self.system_prompt}"
        return base

    def _build_user_prompt(self, anomalies: list) -> str:
        lines = ["High-frequency log anomalies detected:\n"]
        for i, a in enumerate(anomalies, 1):
            lines.append(f"--- Anomaly {i} ---")
            lines.append(f"Pattern: {a['pattern']}")
            lines.append(f"Count: {a['count']}")
            lines.append(f"Level: {a['level']}")
            lines.append(f"Labels: {json.dumps(a.get('labels', {}))}")
            if a.get("samples"):
                lines.append("Samples:")
                for s in a["samples"][:3]:
                    lines.append(f"  {s}")
            lines.append("")
        return "\n".join(lines)

    def _call_claude(self, system: str, user_msg: str) -> str:
        """Call the Anthropic Messages API."""
        log.debug("calling Claude API: model=%s", self.model)
        payload = json.dumps(
            {
                "model": self.model,
                "max_tokens": 4096,
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

        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        log.debug(
            "Claude API response: model=%s usage=%s stop_reason=%s",
            data.get("model", "?"),
            data.get("usage", {}),
            data.get("stop_reason", "?"),
        )

        # Extract text from response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]
        return text

    def _parse_response(self, response: str) -> list:
        """Parse Claude's JSON response into remediation actions."""
        try:
            # Handle potential markdown fences
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])
            actions = json.loads(cleaned)
            log.debug(
                "successfully parsed %d actions from Claude response", len(actions)
            )
            return actions
        except json.JSONDecodeError as e:
            log.error("failed to parse Claude response as JSON: %s", e)
            log.debug("raw response: %.500s", response)
            return [
                {
                    "description": "Claude response parsing failed",
                    "kind": {"report": {"message": response}},
                    "status": "proposed",
                }
            ]

    def _execute_pr(self, pr: dict) -> dict:
        """Create a PR using the GitHub REST API.

        Delegates to the ``github`` module which uses the Git Data API to
        create a commit on a new branch, then the Pulls API to open the PR.
        No local git or gh CLI required.
        """
        repo = pr.get("repo", self.default_repo)
        branch = pr.get("branch", "logmedic/auto-fix")
        title = pr.get("title", "logmedic: automated fix")
        body = pr.get("body", "")
        files = pr.get("files_changed", [])

        if not repo:
            log.error("no repo specified for PR creation")
            return {"failed": {"reason": "no repo specified"}}

        if not self.github_token:
            log.error("no github_token configured")
            return {"failed": {"reason": "no github_token configured"}}

        try:
            github.create_pull_request(
                token=self.github_token,
                repo=repo,
                branch=branch,
                title=title,
                body=body,
                files=files,
            )
            return {"applied": None}
        except Exception as e:
            log.error("PR creation failed: %s", e)
            return {"failed": {"reason": str(e)}}

    def _execute_ssh(self, ssh: dict) -> dict:
        """SSH into a host and run commands."""
        host = ssh.get("host", "")
        commands = ssh.get("commands", [])

        if not host or not commands:
            log.error("missing host or commands for SSH execution")
            return {"failed": {"reason": "missing host or commands"}}

        log.debug("SSH executing on %s: %d commands", host, len(commands))
        try:
            ssh_args = ["ssh"]
            if self.ssh_key_path:
                ssh_args.extend(["-i", self.ssh_key_path])
            ssh_args.extend(["-o", "StrictHostKeyChecking=accept-new", host])

            combined = " && ".join(commands)
            ssh_args.append(combined)
            log.debug("ssh command: %s", " ".join(ssh_args))

            result = subprocess.run(
                ssh_args, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                log.error("SSH failed (rc=%d): %s", result.returncode, result.stderr)
                return {"failed": {"reason": f"ssh failed: {result.stderr}"}}
            log.debug("SSH completed successfully")
            return {"applied": None}
        except Exception as e:
            log.error("SSH execution error: %s", e)
            return {"failed": {"reason": str(e)}}
