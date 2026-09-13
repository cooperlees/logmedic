"""Shared base class for logmedic AI remediator plugins.

Both the Claude (Anthropic) and Meta (Muse Spark) remediators share the same
propose/execute flow: prompt building, repo-context fetching (local checkout
or GitHub API), PR creation with dedup, SSH execution, and JSON response
parsing. Provider subclasses only supply API-key loading, model resolution,
and the actual LLM HTTP call.

``github.py`` lives in this directory too. Plugin modules add this directory
to ``sys.path`` on import (see ``plugins/claude_remediator/``), so
``import github`` and ``from remediator_base import ...`` resolve both under
the daemon (which puts only the plugin's own directory on ``sys.path``) and
when running unit tests from the plugin directory.
"""

import json
import logging
import os
import subprocess

# Re-exported so provider plugins (and their tests) can reference
# ``<plugin_module>.github`` / ``<plugin_module>.subprocess`` for mocking.
import github

__all__ = ["DEFAULT_MAX_TOKENS", "BaseRemediatorPlugin", "github", "subprocess"]

DEFAULT_MAX_TOKENS = 16384


class BaseRemediatorPlugin:
    """Common remediator flow. Subclasses must set the class attributes and
    implement :meth:`_call_llm`."""

    #: Used for log messages and the parse-failure description, e.g. "Claude".
    provider_label = "LLM"
    #: Logger name, e.g. "logmedic.claude_remediator".
    logger_name = "logmedic.remediator"
    #: Default model when ``model`` is not set in settings.
    default_model = ""

    # Ansible vault marker — files starting with this are encrypted blobs
    _VAULT_HEADER = "$ANSIBLE_VAULT;"

    # Subdirs of each role/ considered safe to share (group_vars, host_vars
    # and inventory/ commonly contain secrets or vault-encrypted data).
    _SAFE_ROLE_SUBDIRS = ("defaults", "tasks", "vars", "templates")

    def __init__(self, settings: dict):
        """Parse settings shared by all remediators.

        Args:
            settings: plugin settings dict with a ``settings_json`` key
                (what the daemon passes to ``RemediatorPlugin.__init__``).
                Subclasses parse provider-specific keys (api key, model
                extras) in their own ``__init__`` and delegate the rest here.
        """
        raw = json.loads(settings.get("settings_json", "{}"))
        self.github_token = raw.get("github_token", os.environ.get("GITHUB_TOKEN", ""))
        self.default_repo = raw.get("default_repo", "")
        self.local_repo_path = raw.get("local_repo_path", "")
        self.auto_execute = raw.get("auto_execute", False)
        self.enable_ssh = raw.get("enable_ssh", False)
        self.ssh_key_path = raw.get("ssh_key_path", "")
        self.max_tokens = raw.get("max_tokens", DEFAULT_MAX_TOKENS)
        self.system_prompt = raw.get("system_prompt", "")
        self.model = raw.get("model", self.default_model)

    @property
    def _log(self) -> logging.Logger:
        return logging.getLogger(self.logger_name)

    # ── Hooks for subclasses ────────────────────────────────────────

    def _resolve_model(self) -> str:
        """Return the model ID to use for this propose() call."""
        return self.model

    def _call_llm(self, system: str, user_msg: str, model: str) -> str:
        """Call the provider's chat API. Returns the raw text response."""
        raise NotImplementedError

    # ── Shared flow ─────────────────────────────────────────────────

    def propose(self, anomalies_json: str) -> str:
        """Send anomalies to the LLM and get back proposed remediation actions."""
        anomalies = json.loads(anomalies_json)
        if not anomalies:
            self._log.debug("no anomalies to propose on, returning empty")
            return "[]"

        self._log.debug("proposing remediations for %d anomalies", len(anomalies))

        model = self._resolve_model()

        # Fetch repo context so the model can see actual files
        repo_context = self._fetch_repo_context()

        system = self._build_system_prompt(repo_context)
        user_msg = self._build_user_prompt(anomalies)
        self._log.debug(
            "system prompt length=%d, user prompt length=%d", len(system), len(user_msg)
        )

        response = self._call_llm(system, user_msg, model)
        self._log.debug("%s response length=%d", self.provider_label, len(response))
        actions = self._parse_response(response)
        self._log.debug("parsed %d actions from response", len(actions))

        # Attach anomaly context to each valid action so execute() can embed
        # it in PRs and check for duplicate PRs
        safe_actions = []
        for idx, action in enumerate(actions):
            if not isinstance(action, dict):
                self._log.warning(
                    "skipping non-dict action at index %d (type=%s)",
                    idx,
                    type(action).__name__,
                )
                continue
            action["_anomaly_context"] = anomalies
            safe_actions.append(action)

        return json.dumps(safe_actions)

    def execute(self, action_json: str) -> str:
        """Execute a proposed remediation action."""
        action = json.loads(action_json)
        kind = action.get("kind", {})
        anomaly_context = action.get("_anomaly_context", [])
        self._log.debug(
            "executing action: description=%s kind_keys=%s",
            action.get("description", "?"),
            list(kind.keys()),
        )

        if "pull_request" in kind:
            result = self._execute_pr(kind["pull_request"], anomaly_context)
        elif "ssh_command" in kind:
            if not self.enable_ssh:
                self._log.warning("SSH action rejected: enable_ssh is false")
                result = {
                    "failed": {
                        "reason": "SSH execution is disabled (set enable_ssh = true to allow)"
                    }
                }
            else:
                result = self._execute_ssh(kind["ssh_command"])
        elif "report" in kind:
            self._log.info("report: %s", kind["report"]["message"])
            result = {"applied": None}
        else:
            result = {"failed": {"reason": "unknown action kind"}}

        self._log.debug("execute result: %s", list(result.keys()))
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

    def _parse_response(self, response: str) -> list:
        """Parse the model's JSON response into remediation actions.

        Accepts a bare JSON array, a single action object, or an envelope
        object like ``{"response": [...]}`` (some models wrap their output
        despite the system prompt). Anything unparseable becomes a single
        report action carrying the raw text.
        """
        try:
            # Handle potential markdown fences
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])
            actions = json.loads(cleaned)
            if isinstance(actions, dict):
                if "description" in actions and "kind" in actions:
                    actions = [actions]
                else:
                    for key in (
                        "response",
                        "actions",
                        "remediation_actions",
                        "remediations",
                    ):
                        nested = actions.get(key)
                        if isinstance(nested, list):
                            actions = nested
                            break
                    else:
                        actions = [actions]
            self._log.debug(
                "successfully parsed %d actions from %s response",
                len(actions),
                self.provider_label,
            )
            return actions
        except json.JSONDecodeError as e:
            self._log.error(
                "failed to parse %s response as JSON: %s", self.provider_label, e
            )
            self._log.debug("raw response: %.500s", response)
            return [
                {
                    "description": f"{self.provider_label} response parsing failed",
                    "kind": {"report": {"message": response}},
                    "status": "proposed",
                }
            ]

    def _fetch_repo_context(self) -> str:
        """Fetch repo context for the model.

        Uses the local checkout (``local_repo_path``) when configured,
        otherwise the GitHub API. Returns an empty string when neither is
        available or fetching fails.
        """
        if self.local_repo_path:
            try:
                return self._fetch_local_repo_context()
            except Exception as e:  # noqa: BLE001
                self._log.warning(
                    "failed to read local repo context from %s: %s",
                    self.local_repo_path,
                    e,
                )
                return ""
        return self._fetch_github_repo_context()

    def _fetch_local_repo_context(self) -> str:
        """Read repo structure + key role files from a local checkout.

        Mirrors the GitHub-backed context (top-level listing plus up to 15
        small files from ``roles/*/{defaults,tasks,vars,templates}``) while
        skipping vault-encrypted and oversized files.
        """
        base = os.path.expanduser(self.local_repo_path)
        if not os.path.isdir(base):
            raise RuntimeError(f"local_repo_path is not a directory: {base}")

        lines: list[str] = [
            f"Repository: {self.default_repo or base} (local checkout: {base})",
            "",
            "Top-level files and directories:",
        ]
        for name in sorted(os.listdir(base)):
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            lines.append(f"  {name}/" if os.path.isdir(full) else f"  {name}")

        roles_dir = os.path.join(base, "roles")
        if not os.path.isdir(roles_dir):
            self._log.debug("no roles/ directory found, skipping file content fetch")
            return "\n".join(lines)

        files_fetched = 0
        max_files = 15
        max_file_size = 4096

        for role in sorted(os.listdir(roles_dir)):
            if files_fetched >= max_files:
                break
            role_dir = os.path.join(roles_dir, role)
            if not os.path.isdir(role_dir):
                continue
            for sub in self._SAFE_ROLE_SUBDIRS:
                if files_fetched >= max_files:
                    break
                sub_dir = os.path.join(role_dir, sub)
                if not os.path.isdir(sub_dir):
                    continue
                for leaf in sorted(os.listdir(sub_dir)):
                    if files_fetched >= max_files:
                        break
                    full_path = os.path.join(sub_dir, leaf)
                    if not os.path.isfile(full_path):
                        continue
                    if os.path.getsize(full_path) > max_file_size:
                        self._log.debug("skipping large file %s", full_path)
                        continue
                    with open(full_path, encoding="utf-8", errors="replace") as f:
                        content = f.read(max_file_size + 1)
                    if content.startswith(self._VAULT_HEADER):
                        self._log.debug("skipping vault-encrypted file %s", full_path)
                        continue
                    rel = os.path.relpath(full_path, base)
                    lines.append(f"\n--- {rel} ---")
                    lines.append(content[:max_file_size])
                    files_fetched += 1

        self._log.info(
            "read local repo context from %s: %d files, %d chars",
            base,
            files_fetched,
            sum(len(line) for line in lines),
        )
        return "\n".join(lines)

    def _fetch_github_repo_context(self) -> str:
        """Fetch the default_repo tree and key file contents from GitHub.

        Returns a formatted string describing the repo structure and contents
        of relevant files (Ansible roles defaults/tasks/vars/templates) so
        the model can propose changes grounded in real code.

        Skips ``group_vars/``, ``host_vars/``, and ``inventory/`` which
        commonly contain secrets or ansible-vault encrypted data.  Also skips
        files that are ansible-vault encrypted (useless ciphertext) or exceed
        ``max_file_size``.

        Returns an empty string if no repo is configured or fetching fails.
        """
        if not self.default_repo or not self.github_token:
            return ""

        # Resolve default branch once to avoid repeated /repos/{repo} calls
        try:
            ref = github.get_default_branch(self.github_token, self.default_repo)
        except Exception as e:  # noqa: BLE001
            self._log.warning(
                "failed to get default branch for %s: %s", self.default_repo, e
            )
            return ""

        try:
            entries = github.get_repo_tree(
                self.github_token, self.default_repo, ref=ref
            )
        except Exception as e:  # noqa: BLE001
            self._log.warning(
                "failed to fetch repo tree for %s: %s", self.default_repo, e
            )
            return ""

        lines: list[str] = [f"Repository: {self.default_repo}", ""]

        # Show top-level structure
        lines.append("Top-level files and directories:")
        for entry in entries:
            is_dir = entry.get("type") == "dir"
            lines.append(f"  {entry['name']}/" if is_dir else f"  {entry['name']}")

        # Only descend into roles/ — skip group_vars, host_vars, inventory
        # which commonly contain secrets or vault-encrypted data.
        files_fetched = 0
        max_files = 15
        max_file_size = 4096

        has_roles = any(
            e.get("type") == "dir" and e.get("name") == "roles" for e in entries
        )
        if not has_roles:
            self._log.debug("no roles/ directory found, skipping file content fetch")
            return "\n".join(lines)

        try:
            role_entries = github.get_repo_tree(
                self.github_token, self.default_repo, "roles", ref=ref
            )
        except Exception:  # noqa: BLE001
            return "\n".join(lines)

        for role in role_entries:
            if files_fetched >= max_files:
                break
            if role.get("type") != "dir":
                continue
            try:
                role_contents = github.get_repo_tree(
                    self.github_token,
                    self.default_repo,
                    f"roles/{role['name']}",
                    ref=ref,
                )
            except Exception:  # noqa: BLE001, S112  # skip one bad entry, keep building context
                continue
            for role_sub in role_contents:
                if files_fetched >= max_files:
                    break
                if role_sub.get("type") != "dir" or role_sub["name"] not in (
                    self._SAFE_ROLE_SUBDIRS
                ):
                    continue
                try:
                    leaf_entries = github.get_repo_tree(
                        self.github_token,
                        self.default_repo,
                        f"roles/{role['name']}/{role_sub['name']}",
                        ref=ref,
                    )
                except Exception:  # noqa: BLE001, S112  # skip one bad entry, keep building context
                    continue
                for leaf in leaf_entries:
                    if files_fetched >= max_files:
                        break
                    if leaf.get("type") != "file":
                        continue
                    # Skip large files before fetching content
                    if leaf.get("size", 0) > max_file_size:
                        self._log.debug(
                            "skipping large file %s (%d bytes)",
                            leaf["path"],
                            leaf.get("size", 0),
                        )
                        continue
                    full_path = leaf["path"]
                    try:
                        content = github.get_file_content(
                            self.github_token,
                            self.default_repo,
                            full_path,
                        )
                    except Exception:  # noqa: BLE001, S112  # skip one bad entry, keep building context
                        continue
                    # Skip ansible-vault encrypted files
                    if content.startswith(self._VAULT_HEADER):
                        self._log.debug("skipping vault-encrypted file %s", full_path)
                        continue
                    lines.append(f"\n--- {full_path} ---")
                    lines.append(content)
                    files_fetched += 1

        self._log.info(
            "fetched repo context for %s: %d files, %d chars",
            self.default_repo,
            files_fetched,
            sum(len(line) for line in lines),
        )
        return "\n".join(lines)

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
            "This PR was automatically generated by [logmedic](https://github.com/cooperlees/logmedic) "  # noqa: ISC004
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
                    lines.append("  ```")
                    lines.append(f"  {s}")
                    lines.append("  ```")
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
            self._log.warning(
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
            self._log.error("no repo specified for PR creation")
            return {"failed": {"reason": "no repo specified"}}

        if not self.github_token:
            self._log.error("no github_token configured")
            return {"failed": {"reason": "no github_token configured"}}

        # --- Dedup: check for existing open PRs matching these anomaly patterns ---
        if anomalies:
            search_terms = [a["pattern"] for a in anomalies if a.get("pattern")]
            try:
                existing = github.find_open_prs(self.github_token, repo, search_terms)
                if existing:
                    urls = ", ".join(p["html_url"] for p in existing)
                    self._log.info(
                        "skipping PR creation — existing open PR(s) already address "
                        "these anomaly patterns: %s",
                        urls,
                    )
                    return {
                        "applied": None,
                        "skipped": True,
                        "reason": f"existing open PR(s): {urls}",
                    }
            except Exception as e:  # noqa: BLE001
                self._log.warning(
                    "PR dedup search failed, proceeding with creation: %s", e
                )

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
        except Exception as e:  # noqa: BLE001
            self._log.error("PR creation failed: %s", e)
            return {"failed": {"reason": str(e)}}

    def _execute_ssh(self, ssh: dict) -> dict:
        """SSH into a host and run commands."""
        host = ssh.get("host", "")
        commands = ssh.get("commands", [])

        if not host or not commands:
            self._log.error("missing host or commands for SSH execution")
            return {"failed": {"reason": "missing host or commands"}}

        self._log.debug("SSH executing on %s: %d commands", host, len(commands))
        try:
            ssh_args = ["ssh"]
            if self.ssh_key_path:
                ssh_args.extend(["-i", self.ssh_key_path])
            ssh_args.extend(["-o", "StrictHostKeyChecking=accept-new", host])

            combined = " && ".join(commands)
            ssh_args.append(combined)
            self._log.debug("ssh command: %s", " ".join(ssh_args))

            result = subprocess.run(  # noqa: PLW1510  # returncode checked below
                ssh_args, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                self._log.error(
                    "SSH failed (rc=%d): %s", result.returncode, result.stderr
                )
                return {"failed": {"reason": f"ssh failed: {result.stderr}"}}
            self._log.debug("SSH completed successfully")
            return {"applied": None}
        except Exception as e:  # noqa: BLE001
            self._log.error("SSH execution error: %s", e)
            return {"failed": {"reason": str(e)}}
