"""
Claude AI remediator plugin for logmedic.

Uses the Anthropic Claude API to analyze log anomalies and propose fixes.
Can raise PRs via the GitHub REST API or SSH into hosts to apply fixes.

Settings (passed via TOML config):
    anthropic_api_key: str  - Anthropic API key
    model: str              - Model to use (default: claude-sonnet-4-6)
    max_tokens: int         - Max tokens for Claude response (default: 16384)
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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import github

log = logging.getLogger("logmedic.claude_remediator")

DEFAULT_MAX_TOKENS = 16384


class RemediatorPlugin:
    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = raw.get(
            "anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.model = raw.get("model", "claude-sonnet-4-6")
        self.github_token = raw.get("github_token", os.environ.get("GITHUB_TOKEN", ""))
        self.default_repo = raw.get("default_repo", "")
        self.auto_execute = raw.get("auto_execute", False)
        self.enable_ssh = raw.get("enable_ssh", False)
        self.ssh_key_path = raw.get("ssh_key_path", "")
        self.max_tokens = raw.get("max_tokens", DEFAULT_MAX_TOKENS)
        self.system_prompt = raw.get("system_prompt", "")
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

    def propose(self, anomalies_json: str) -> str:
        """Send anomalies to Claude and get back proposed remediation actions."""
        anomalies = json.loads(anomalies_json)
        if not anomalies:
            log.debug("no anomalies to propose on, returning empty")
            return "[]"

        log.debug("proposing remediations for %d anomalies", len(anomalies))

        # Fetch repo context so Claude can see actual files
        repo_context = self._fetch_repo_context()

        system = self._build_system_prompt(repo_context)
        user_msg = self._build_user_prompt(anomalies)
        log.debug(
            "system prompt length=%d, user prompt length=%d", len(system), len(user_msg)
        )

        response = self._call_claude(system, user_msg)
        log.debug("claude response length=%d", len(response))
        actions = self._parse_response(response)
        log.debug("parsed %d actions from response", len(actions))

        # Attach anomaly context to each action so execute() can embed it in PRs
        # and check for duplicate PRs
        for action in actions:
            action["_anomaly_context"] = anomalies

        return json.dumps(actions)

    def execute(self, action_json: str) -> str:
        """Execute a proposed remediation action."""
        action = json.loads(action_json)
        kind = action.get("kind", {})
        anomaly_context = action.get("_anomaly_context", [])
        log.debug(
            "executing action: description=%s kind_keys=%s",
            action.get("description", "?"),
            list(kind.keys()),
        )

        if "pull_request" in kind:
            result = self._execute_pr(kind["pull_request"], anomaly_context)
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
            log.info("report: %s", kind["report"]["message"])
            result = {"applied": None}
        else:
            result = {"failed": {"reason": "unknown action kind"}}

        log.debug("execute result: %s", list(result.keys()))
        return json.dumps(result)

    def _build_system_prompt(self, repo_context: str = "") -> str:
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
        if self.default_repo:
            base += (
                f"\n\nIMPORTANT: When proposing pull_request actions, always use "
                f'repo "{self.default_repo}" unless a different repo is clearly '
                f"indicated by the anomaly context."
            )
        if repo_context:
            base += f"\n\nRepository structure and contents:\n{repo_context}"
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

    def _fetch_repo_context(self) -> str:
        """Fetch the default_repo tree and key file contents from GitHub.

        Returns a formatted string describing the repo structure and contents
        of relevant files (Ansible roles, config, etc.) so Claude can propose
        changes grounded in real code.  Returns an empty string if no repo is
        configured or fetching fails.
        """
        if not self.default_repo or not self.github_token:
            return ""

        try:
            entries = github.get_repo_tree(self.github_token, self.default_repo)
        except Exception as e:
            log.warning("failed to fetch repo tree for %s: %s", self.default_repo, e)
            return ""

        lines = [f"Repository: {self.default_repo}", ""]

        # Show top-level structure
        lines.append("Top-level files and directories:")
        for entry in entries:
            kind = "dir" if entry.get("type") == "tree" else "file"
            lines.append(f"  {entry['path']}/ " if kind == "dir" else f"  {entry['path']}")

        # Fetch contents of relevant config files (Ansible defaults, vars, tasks)
        # Cap at a reasonable number to stay within token limits
        interesting_dirs = []
        for entry in entries:
            if entry.get("type") == "tree" and entry["path"] in ("roles", "group_vars", "host_vars", "inventory"):
                interesting_dirs.append(entry["path"])

        files_fetched = 0
        max_files = 15
        max_file_size = 4096

        for dirname in interesting_dirs:
            try:
                sub_entries = github.get_repo_tree(self.github_token, self.default_repo, dirname)
            except Exception:
                continue
            for sub in sub_entries:
                if files_fetched >= max_files:
                    break
                # For roles/, descend one more level to get defaults/tasks/vars
                if sub.get("type") == "tree" and dirname == "roles":
                    try:
                        role_entries = github.get_repo_tree(
                            self.github_token, self.default_repo, f"{dirname}/{sub['path']}"
                        )
                    except Exception:
                        continue
                    for role_sub in role_entries:
                        if files_fetched >= max_files:
                            break
                        if role_sub.get("type") == "tree" and role_sub["path"] in (
                            "defaults", "tasks", "vars", "templates",
                        ):
                            try:
                                leaf_entries = github.get_repo_tree(
                                    self.github_token,
                                    self.default_repo,
                                    f"{dirname}/{sub['path']}/{role_sub['path']}",
                                )
                            except Exception:
                                continue
                            for leaf in leaf_entries:
                                if files_fetched >= max_files:
                                    break
                                if leaf.get("type") != "blob":
                                    continue
                                full_path = f"{dirname}/{sub['path']}/{role_sub['path']}/{leaf['path']}"
                                try:
                                    content = github.get_file_content(
                                        self.github_token, self.default_repo, full_path,
                                    )
                                    if len(content) <= max_file_size:
                                        lines.append(f"\n--- {full_path} ---")
                                        lines.append(content)
                                        files_fetched += 1
                                except Exception:
                                    continue
                elif sub.get("type") == "blob":
                    full_path = f"{dirname}/{sub['path']}"
                    try:
                        content = github.get_file_content(
                            self.github_token, self.default_repo, full_path,
                        )
                        if len(content) <= max_file_size:
                            lines.append(f"\n--- {full_path} ---")
                            lines.append(content)
                            files_fetched += 1
                    except Exception:
                        continue

        log.info(
            "fetched repo context for %s: %d files, %d chars",
            self.default_repo,
            files_fetched,
            sum(len(l) for l in lines),
        )
        return "\n".join(lines)

    def _call_claude(self, system: str, user_msg: str) -> str:
        """Call the Anthropic Messages API."""
        log.debug("calling Claude API: model=%s", self.model)
        payload = json.dumps(
            {
                "model": self.model,
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

    @staticmethod
    def _build_anomaly_section(anomalies: list) -> str:
        """Format anomaly context as a markdown section for PR bodies."""
        if not anomalies:
            return ""
        lines = [
            "",
            "---",
            "## Triggering Log Anomalies",
            "",
            "This PR was automatically generated by [logmedic](https://github.com/cooperlees/logmedic) "
            "in response to the following high-frequency log patterns:",
            "",
        ]
        for a in anomalies:
            lines.append(f"### `{a.get('level', 'unknown').upper()}` — {a['pattern']}")
            lines.append(f"- **Count**: {a['count']} occurrences")
            labels = a.get("labels", {})
            if labels:
                label_str = ", ".join(f"`{k}={v}`" for k, v in labels.items())
                lines.append(f"- **Labels**: {label_str}")
            samples = a.get("samples", [])
            if samples:
                lines.append("- **Sample log lines**:")
                for s in samples[:3]:
                    lines.append(f"  ```")
                    lines.append(f"  {s}")
                    lines.append(f"  ```")
            lines.append("")
        return "\n".join(lines)

    def _execute_pr(self, pr: dict, anomalies: list | None = None) -> dict:
        """Create a PR using the GitHub REST API.

        Delegates to the ``github`` module which uses the Git Data API to
        create a commit on a new branch, then the Pulls API to open the PR.
        No local git or gh CLI required.

        Before creating the PR, checks whether an open PR already addresses
        the same anomaly patterns.  If so, skips creation to avoid duplicates.
        """
        if anomalies is None:
            anomalies = []

        repo = pr.get("repo", self.default_repo)
        if self.default_repo and repo != self.default_repo:
            log.warning(
                "PR repo %r differs from configured default_repo %r, using default",
                repo,
                self.default_repo,
            )
            repo = self.default_repo
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

        # --- Dedup: check for existing open PRs matching these anomaly patterns ---
        if anomalies:
            search_terms = [a["pattern"] for a in anomalies if a.get("pattern")]
            try:
                existing = github.find_open_prs(self.github_token, repo, search_terms)
                if existing:
                    urls = ", ".join(p["html_url"] for p in existing)
                    log.info(
                        "skipping PR creation — existing open PR(s) already address "
                        "these anomaly patterns: %s",
                        urls,
                    )
                    return {
                        "applied": None,
                        "skipped": True,
                        "reason": f"existing open PR(s): {urls}",
                    }
            except Exception as e:
                log.warning("PR dedup search failed, proceeding with creation: %s", e)

        # --- Append triggering log line context to PR body ---
        anomaly_section = self._build_anomaly_section(anomalies)
        if anomaly_section:
            body = body + anomaly_section

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
