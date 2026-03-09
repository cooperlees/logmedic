"""
Claude AI remediator plugin for logmedic.

Uses the Anthropic Claude API to analyze log anomalies and propose fixes.
Can raise PRs via GitHub CLI or SSH into hosts to apply fixes.

Settings (passed via TOML config):
    anthropic_api_key: str  - Anthropic API key
    model: str              - Model to use (default: claude-sonnet-4-20250514)
    github_token: str       - GitHub token for raising PRs
    default_repo: str       - Default repo for PRs (e.g. "org/ansible-infra")
    auto_execute: bool      - Whether to auto-execute proposed actions (default: false)
    ssh_key_path: str       - Path to SSH key for remote execution
    system_prompt: str      - Additional system context about your infrastructure
"""

import json
import os
import subprocess
import tempfile
from urllib.request import Request, urlopen


class RemediatorPlugin:
    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = raw.get("anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", ""))
        self.model = raw.get("model", "claude-sonnet-4-20250514")
        self.github_token = raw.get("github_token", os.environ.get("GITHUB_TOKEN", ""))
        self.default_repo = raw.get("default_repo", "")
        self.auto_execute = raw.get("auto_execute", False)
        self.ssh_key_path = raw.get("ssh_key_path", "")
        self.system_prompt = raw.get("system_prompt", "")

    def name(self) -> str:
        return "claude_remediator"

    def propose(self, anomalies_json: str) -> str:
        """Send anomalies to Claude and get back proposed remediation actions."""
        anomalies = json.loads(anomalies_json)
        if not anomalies:
            return "[]"

        system = self._build_system_prompt()
        user_msg = self._build_user_prompt(anomalies)

        response = self._call_claude(system, user_msg)
        actions = self._parse_response(response)
        return json.dumps(actions)

    def execute(self, action_json: str) -> str:
        """Execute a proposed remediation action."""
        action = json.loads(action_json)
        kind = action.get("kind", {})

        if "pull_request" in kind:
            return json.dumps(self._execute_pr(kind["pull_request"]))
        elif "ssh_command" in kind:
            return json.dumps(self._execute_ssh(kind["ssh_command"]))
        elif "report" in kind:
            return json.dumps({"report": {"message": kind["report"]["message"]}})
        else:
            return json.dumps({"failed": {"reason": "unknown action kind"}})

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
        payload = json.dumps({
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user_msg}],
        }).encode()

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
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return [{
                "description": "Claude response parsing failed",
                "kind": {"report": {"message": response}},
                "status": "proposed",
            }]

    def _execute_pr(self, pr: dict) -> dict:
        """Create a PR using the GitHub CLI."""
        repo = pr.get("repo", self.default_repo)
        branch = pr.get("branch", "logmedic/auto-fix")
        title = pr.get("title", "logmedic: automated fix")
        body = pr.get("body", "")
        files = pr.get("files_changed", [])

        if not repo:
            return {"failed": {"reason": "no repo specified"}}

        try:
            # Clone, branch, commit, push, create PR
            with tempfile.TemporaryDirectory() as tmpdir:
                self._run(["gh", "repo", "clone", repo, tmpdir])
                self._run(["git", "checkout", "-b", branch], cwd=tmpdir)

                for f in files:
                    fpath = os.path.join(tmpdir, f["path"])
                    os.makedirs(os.path.dirname(fpath), exist_ok=True)
                    with open(fpath, "w") as fh:
                        fh.write(f["content"])

                self._run(["git", "add", "-A"], cwd=tmpdir)
                self._run(["git", "commit", "-m", title], cwd=tmpdir)
                self._run(["git", "push", "-u", "origin", branch], cwd=tmpdir)
                self._run([
                    "gh", "pr", "create",
                    "--repo", repo,
                    "--title", title,
                    "--body", body,
                ], cwd=tmpdir)

            return {"applied": None}
        except Exception as e:
            return {"failed": {"reason": str(e)}}

    def _execute_ssh(self, ssh: dict) -> dict:
        """SSH into a host and run commands."""
        host = ssh.get("host", "")
        commands = ssh.get("commands", [])

        if not host or not commands:
            return {"failed": {"reason": "missing host or commands"}}

        try:
            ssh_args = ["ssh"]
            if self.ssh_key_path:
                ssh_args.extend(["-i", self.ssh_key_path])
            ssh_args.extend(["-o", "StrictHostKeyChecking=accept-new", host])

            combined = " && ".join(commands)
            ssh_args.append(combined)

            result = subprocess.run(
                ssh_args, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return {"failed": {"reason": f"ssh failed: {result.stderr}"}}
            return {"applied": None}
        except Exception as e:
            return {"failed": {"reason": str(e)}}

    def _run(self, cmd: list, cwd: str = None) -> str:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"command failed: {' '.join(cmd)}\n{result.stderr}")
        return result.stdout
