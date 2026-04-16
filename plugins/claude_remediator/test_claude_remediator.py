"""Tests for the Claude remediator plugin."""

import json
import unittest
from unittest.mock import MagicMock, patch

from claude_remediator import DEFAULT_MAX_TOKENS, RemediatorPlugin


def _make_settings(**overrides):
    raw = {
        "anthropic_api_key": "sk-ant-test-key",
        "model": "claude-opus-4-6",
        "default_repo": "cooperlees/clc_ansible",
    }
    raw.update(overrides)
    return {"settings_json": json.dumps(raw)}


def _anomalies_json(anomalies):
    """Wrap anomaly dicts into the JSON string the plugin expects."""
    return json.dumps(anomalies)


SAMPLE_ANOMALIES = [
    {
        "pattern": "ERROR: connection refused to database at <IP>:<NUM>",
        "count": 150,
        "level": "error",
        "labels": {"app": "api-server", "namespace": "prod"},
        "samples": [
            "ERROR: connection refused to database at 10.0.0.5:5432",
            "ERROR: connection refused to database at 10.0.0.6:5432",
        ],
    },
]


def _claude_api_response(actions_json, stop_reason="end_turn"):
    """Build a mock Anthropic Messages API response."""
    return json.dumps(
        {
            "id": "msg_test_123",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": actions_json}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
    ).encode()


# The kind of JSON Claude would return for a PR-based fix
PR_DATA: dict = {
    "repo": "cooperlees/clc_ansible",
    "branch": "fix/db-connection-pool",
    "title": "fix: increase database connection pool size",
    "body": "The API server is exhausting its connection pool under load.",
    "files_changed": [
        {
            "path": "roles/api-server/defaults/main.yml",
            "content": "db_pool_size: 50\ndb_pool_timeout: 30\n",
        },
    ],
}

PR_ACTION: dict = {
    "description": "Fix database connection pool configuration",
    "kind": {"pull_request": PR_DATA},
    "status": "proposed",
}

REPORT_ACTION = {
    "description": "Database connection errors detected",
    "kind": {
        "report": {
            "message": "High frequency of database connection errors from api-server pods. "
            "Root cause appears to be connection pool exhaustion.",
        }
    },
    "status": "proposed",
}


class TestRemediatorInit(unittest.TestCase):
    def test_defaults(self):
        plugin = RemediatorPlugin({"settings_json": "{}"})
        self.assertEqual(plugin.model, "claude-opus-4-6")
        self.assertEqual(plugin.api_key, "")
        self.assertEqual(plugin.default_repo, "")
        self.assertFalse(plugin.auto_execute)
        self.assertEqual(plugin.max_tokens, DEFAULT_MAX_TOKENS)

    def test_custom_settings(self):
        plugin = RemediatorPlugin(_make_settings(auto_execute=True))
        self.assertEqual(plugin.api_key, "sk-ant-test-key")
        self.assertEqual(plugin.default_repo, "cooperlees/clc_ansible")
        assertTrue = self.assertTrue
        assertTrue(plugin.auto_execute)

    def test_custom_max_tokens(self):
        """max_tokens can be overridden via settings."""
        plugin = RemediatorPlugin(_make_settings(max_tokens=8192))
        self.assertEqual(plugin.max_tokens, 8192)

    def test_name(self):
        plugin = RemediatorPlugin(_make_settings())
        self.assertEqual(plugin.name(), "claude_remediator")


class TestPropose(unittest.TestCase):
    """Test the propose() method which sends anomalies to Claude."""

    def test_empty_anomalies(self):
        """Empty anomalies → empty actions, no API call."""
        plugin = RemediatorPlugin(_make_settings())
        result = plugin.propose("[]")
        self.assertEqual(json.loads(result), [])

    @patch("claude_remediator.urlopen")
    def test_propose_pr_action(self, mock_urlopen):
        """Claude returns a PR-based remediation action."""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(json.dumps([PR_ACTION]))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        action = result[0]
        self.assertEqual(
            action["description"], "Fix database connection pool configuration"
        )
        self.assertIn("pull_request", action["kind"])
        pr = action["kind"]["pull_request"]
        self.assertEqual(pr["repo"], "cooperlees/clc_ansible")
        self.assertEqual(pr["branch"], "fix/db-connection-pool")
        self.assertEqual(len(pr["files_changed"]), 1)
        self.assertIn("db_pool_size", pr["files_changed"][0]["content"])

    @patch("claude_remediator.urlopen")
    def test_propose_report_action(self, mock_urlopen):
        """Claude returns a report-only action (no automated fix)."""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(json.dumps([REPORT_ACTION]))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    @patch("claude_remediator.urlopen")
    def test_propose_sends_correct_request(self, mock_urlopen):
        """Verify the API request has correct headers and payload shape."""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response("[]")
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        plugin.propose(_anomalies_json(SAMPLE_ANOMALIES))

        call_args = mock_urlopen.call_args
        req = call_args[0][0]

        # Check URL and headers
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "sk-ant-test-key")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(req.get_header("Content-type"), "application/json")

        # Check payload
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "claude-opus-4-6")
        self.assertEqual(payload["max_tokens"], 16384)
        self.assertIn("messages", payload)
        self.assertIn("system", payload)
        # User message should contain anomaly data
        user_content = payload["messages"][0]["content"]
        self.assertIn("connection refused", user_content)
        self.assertIn("150", user_content)

    @patch("claude_remediator.urlopen")
    def test_propose_uses_custom_max_tokens(self, mock_urlopen):
        """Custom max_tokens setting is sent to the Claude API."""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response("[]")
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings(max_tokens=8192))
        plugin.propose(_anomalies_json(SAMPLE_ANOMALIES))

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        self.assertEqual(payload["max_tokens"], 8192)

    @patch("claude_remediator.urlopen")
    def test_propose_warns_on_max_tokens_truncation(self, mock_urlopen):
        """A warning is logged when Claude's response is truncated."""
        # Return truncated JSON that will fail to parse
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(
            '[{"description": "trunca', stop_reason="max_tokens"
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        with self.assertLogs("logmedic.claude_remediator", level="WARNING") as cm:
            result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        # Should have logged a truncation warning
        self.assertTrue(
            any("truncated" in msg for msg in cm.output),
            f"Expected truncation warning, got: {cm.output}",
        )
        # Should still return a fallback report action
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "Claude response parsing failed")

    @patch("claude_remediator.urlopen")
    def test_propose_handles_markdown_fenced_response(self, mock_urlopen):
        """Claude sometimes wraps JSON in markdown code fences."""
        fenced = "```json\n" + json.dumps([REPORT_ACTION]) + "\n```"
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(fenced)
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    @patch("claude_remediator.urlopen")
    def test_propose_invalid_json_fallback(self, mock_urlopen):
        """If Claude returns non-JSON, plugin wraps it in a report action."""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(
            "I'm sorry, I can't help with that."
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "Claude response parsing failed")
        self.assertIn("report", result[0]["kind"])

    @patch("claude_remediator.urlopen")
    def test_propose_multiple_actions(self, mock_urlopen):
        """Claude can return multiple actions for one set of anomalies."""
        actions = [PR_ACTION, REPORT_ACTION]
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(json.dumps(actions))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 2)

    @patch("claude_remediator.urlopen")
    def test_propose_http_error_includes_body(self, mock_urlopen):
        """HTTP errors from the Claude API should include the response body."""
        from urllib.error import HTTPError
        from io import BytesIO
        from http.client import HTTPMessage

        error_body = b'{"type":"error","error":{"type":"invalid_request_error","message":"model: claude-sonnet-4-20250514 is not available"}}'
        err = HTTPError(
            "https://api.anthropic.com/v1/messages",
            400,
            "Bad Request",
            HTTPMessage(),
            BytesIO(error_body),
        )
        mock_urlopen.side_effect = err

        plugin = RemediatorPlugin(_make_settings())
        with self.assertRaises(RuntimeError) as ctx:
            plugin.propose(_anomalies_json(SAMPLE_ANOMALIES))

        self.assertIn("400", str(ctx.exception))
        self.assertIn("invalid_request_error", str(ctx.exception))


class TestExecute(unittest.TestCase):
    """Test the execute() method which carries out proposed actions."""

    @patch("claude_remediator.github.create_pull_request")
    def test_execute_pr(self, mock_create_pr):
        """PR execution should call github.create_pull_request."""
        mock_create_pr.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/42",
            "number": 42,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test123"))
        action = {
            "description": "test",
            "kind": PR_ACTION["kind"],
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("applied", result)

        # Verify create_pull_request was called with correct args
        mock_create_pr.assert_called_once_with(
            token="ghp_test123",
            repo="cooperlees/clc_ansible",
            branch="fix/db-connection-pool",
            title="fix: increase database connection pool size",
            body="The API server is exhausting its connection pool under load.",
            files=PR_DATA["files_changed"],
        )

    @patch("claude_remediator.github.create_pull_request")
    def test_execute_pr_api_failure(self, mock_create_pr):
        """GitHub API error should return failed status."""
        mock_create_pr.side_effect = RuntimeError(
            "GitHub API GET /repos/x failed (404): not found"
        )

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test123"))
        action = {
            "description": "test",
            "kind": PR_ACTION["kind"],
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("404", result["failed"]["reason"])

    def test_execute_report(self):
        """Report actions just echo the message back."""
        plugin = RemediatorPlugin(_make_settings())
        action = {
            "description": "test",
            "kind": REPORT_ACTION["kind"],
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("applied", result)

    @patch("claude_remediator.subprocess.run")
    def test_execute_ssh(self, mock_run):
        """SSH execution should call ssh with the right host and commands."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        plugin = RemediatorPlugin(
            _make_settings(enable_ssh=True, ssh_key_path="/tmp/test_key")
        )
        action = {
            "description": "restart service",
            "kind": {
                "ssh_command": {
                    "host": "web-1.prod",
                    "commands": ["systemctl restart api"],
                }
            },
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("applied", result)
        call_args = mock_run.call_args[0][0]
        self.assertIn("ssh", call_args)
        self.assertIn("web-1.prod", call_args)
        self.assertIn("-i", call_args)
        self.assertIn("/tmp/test_key", call_args)

    @patch("claude_remediator.subprocess.run")
    def test_execute_ssh_failure(self, mock_run):
        """SSH command failure should return failed status."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Permission denied"
        )

        plugin = RemediatorPlugin(_make_settings(enable_ssh=True))
        action = {
            "description": "restart",
            "kind": {
                "ssh_command": {"host": "web-1", "commands": ["systemctl restart api"]}
            },
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("Permission denied", result["failed"]["reason"])

    def test_execute_ssh_disabled_by_default(self):
        """SSH actions should be rejected when enable_ssh is false (default)."""
        plugin = RemediatorPlugin(_make_settings())
        action = {
            "description": "restart",
            "kind": {
                "ssh_command": {"host": "web-1", "commands": ["systemctl restart api"]}
            },
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("disabled", result["failed"]["reason"])

    def test_execute_no_repo(self):
        """PR with no repo specified → failure."""
        plugin = RemediatorPlugin(_make_settings(default_repo=""))
        action = {
            "description": "test",
            "kind": {
                "pull_request": {
                    "branch": "fix/x",
                    "title": "t",
                    "body": "b",
                    "files_changed": [],
                }
            },
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("no repo", result["failed"]["reason"])

    def test_execute_no_github_token(self):
        """PR with no github_token → failure."""
        plugin = RemediatorPlugin(_make_settings(github_token=""))
        action = {
            "description": "test",
            "kind": PR_ACTION["kind"],
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("github_token", result["failed"]["reason"])

    def test_execute_unknown_kind(self):
        """Unknown action kind → failure."""
        plugin = RemediatorPlugin(_make_settings())
        action = {"description": "test", "kind": {"magic": {}}, "status": "proposed"}
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("unknown", result["failed"]["reason"])

    def test_execute_ssh_missing_host(self):
        """SSH with no host → failure."""
        plugin = RemediatorPlugin(_make_settings(enable_ssh=True))
        action = {
            "description": "test",
            "kind": {"ssh_command": {"host": "", "commands": ["echo hi"]}},
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("failed", result)
        self.assertIn("missing", result["failed"]["reason"])


class TestBuildPrompts(unittest.TestCase):
    """Test prompt construction (no API calls)."""

    def test_system_prompt_contains_instructions(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_system_prompt()
        self.assertIn("SRE", prompt)
        self.assertIn("JSON array", prompt)
        self.assertIn("pull_request", prompt)
        self.assertIn("ssh_command", prompt)

    def test_system_prompt_includes_default_repo(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_system_prompt()
        self.assertIn("cooperlees/clc_ansible", prompt)

    def test_system_prompt_includes_custom_context(self):
        plugin = RemediatorPlugin(
            _make_settings(system_prompt="We use Ansible for all config management.")
        )
        prompt = plugin._build_system_prompt()
        self.assertIn("Ansible", prompt)

    def test_system_prompt_includes_repo_context(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_system_prompt(
            repo_context="roles/nginx/defaults/main.yml:\nworker_connections: 1024"
        )
        self.assertIn("Repository structure", prompt)
        self.assertIn("worker_connections", prompt)

    def test_user_prompt_contains_anomaly_data(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_user_prompt(SAMPLE_ANOMALIES)
        self.assertIn("connection refused", prompt)
        self.assertIn("150", prompt)
        self.assertIn("api-server", prompt)
        self.assertIn("Anomaly 1", prompt)

    @patch("claude_remediator.github.create_pull_request")
    def test_execute_pr_overrides_wrong_repo(self, mock_create_pr):
        """When default_repo is set, ignore Claude's hallucinated repo name."""
        mock_create_pr.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/99",
            "number": 99,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test123"))
        # Claude returned "cooperlees/ansible" instead of "cooperlees/clc_ansible"
        wrong_repo_pr = {
            "repo": "cooperlees/ansible",
            "branch": "fix/mariadb-upgrade",
            "title": "fix: add mariadb-upgrade task",
            "body": "Adds upgrade task.",
            "files_changed": [
                {"path": "roles/mariadb/tasks/main.yml", "content": "upgraded"}
            ],
        }
        action = {
            "description": "test",
            "kind": {"pull_request": wrong_repo_pr},
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))

        self.assertIn("applied", result)
        # Should have called with the configured default_repo, not the hallucinated one
        mock_create_pr.assert_called_once_with(
            token="ghp_test123",
            repo="cooperlees/clc_ansible",
            branch="fix/mariadb-upgrade",
            title="fix: add mariadb-upgrade task",
            body="Adds upgrade task.",
            files=wrong_repo_pr["files_changed"],
        )


class TestFetchRepoContext(unittest.TestCase):
    """Test _fetch_repo_context() which fetches files from the default repo."""

    @patch("claude_remediator.github.get_file_content")
    @patch("claude_remediator.github.get_repo_tree")
    @patch("claude_remediator.github.get_default_branch", return_value="main")
    def test_fetches_repo_tree_and_files(self, _mock_branch, mock_tree, mock_content):
        # Contents API returns "name", "type"="dir"/"file", "path", "size"
        mock_tree.side_effect = [
            # top-level
            [
                {"name": "roles", "path": "roles", "type": "dir", "size": 0},
                {"name": "README.md", "path": "README.md", "type": "file", "size": 50},
            ],
            # roles/
            [{"name": "nginx", "path": "roles/nginx", "type": "dir", "size": 0}],
            # roles/nginx/
            [
                {
                    "name": "defaults",
                    "path": "roles/nginx/defaults",
                    "type": "dir",
                    "size": 0,
                },
                {
                    "name": "tasks",
                    "path": "roles/nginx/tasks",
                    "type": "dir",
                    "size": 0,
                },
            ],
            # roles/nginx/defaults/
            [
                {
                    "name": "main.yml",
                    "path": "roles/nginx/defaults/main.yml",
                    "type": "file",
                    "size": 30,
                }
            ],
            # roles/nginx/tasks/
            [
                {
                    "name": "main.yml",
                    "path": "roles/nginx/tasks/main.yml",
                    "type": "file",
                    "size": 25,
                }
            ],
        ]
        mock_content.side_effect = [
            "worker_connections: 1024\n",
            "- name: install nginx\n",
        ]

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()

        self.assertIn("cooperlees/clc_ansible", result)
        self.assertIn("roles/", result)
        self.assertIn("worker_connections", result)
        self.assertIn("install nginx", result)
        self.assertEqual(mock_content.call_count, 2)
        # Verify ref is passed to get_repo_tree
        for call in mock_tree.call_args_list:
            self.assertEqual(
                call[1].get("ref", call[0][3] if len(call[0]) > 3 else ""), "main"
            )

    def test_returns_empty_without_repo(self):
        plugin = RemediatorPlugin(_make_settings(default_repo=""))
        self.assertEqual(plugin._fetch_repo_context(), "")

    def test_returns_empty_without_token(self):
        plugin = RemediatorPlugin(_make_settings(github_token=""))
        self.assertEqual(plugin._fetch_repo_context(), "")

    @patch("claude_remediator.github.get_default_branch")
    def test_handles_api_error_gracefully(self, mock_branch):
        mock_branch.side_effect = RuntimeError("403 forbidden")
        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()
        self.assertEqual(result, "")

    @patch("claude_remediator.github.get_file_content")
    @patch("claude_remediator.github.get_repo_tree")
    @patch("claude_remediator.github.get_default_branch", return_value="main")
    def test_skips_vault_encrypted_files(self, _mock_branch, mock_tree, mock_content):
        mock_tree.side_effect = [
            # top-level
            [{"name": "roles", "path": "roles", "type": "dir", "size": 0}],
            # roles/
            [{"name": "db", "path": "roles/db", "type": "dir", "size": 0}],
            # roles/db/
            [{"name": "vars", "path": "roles/db/vars", "type": "dir", "size": 0}],
            # roles/db/vars/
            [
                {
                    "name": "main.yml",
                    "path": "roles/db/vars/main.yml",
                    "type": "file",
                    "size": 100,
                }
            ],
        ]
        mock_content.return_value = "$ANSIBLE_VAULT;1.1;AES256\n6162636465660a..."

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()

        # Vault file should not appear in context
        self.assertNotIn("ANSIBLE_VAULT", result)
        self.assertNotIn("6162636465660a", result)

    @patch("claude_remediator.github.get_file_content")
    @patch("claude_remediator.github.get_repo_tree")
    @patch("claude_remediator.github.get_default_branch", return_value="main")
    def test_skips_large_files_by_size(self, _mock_branch, mock_tree, mock_content):
        mock_tree.side_effect = [
            # top-level
            [{"name": "roles", "path": "roles", "type": "dir", "size": 0}],
            # roles/
            [{"name": "app", "path": "roles/app", "type": "dir", "size": 0}],
            # roles/app/
            [
                {
                    "name": "defaults",
                    "path": "roles/app/defaults",
                    "type": "dir",
                    "size": 0,
                }
            ],
            # roles/app/defaults/ — one small, one large
            [
                {
                    "name": "main.yml",
                    "path": "roles/app/defaults/main.yml",
                    "type": "file",
                    "size": 100,
                },
                {
                    "name": "big.bin",
                    "path": "roles/app/defaults/big.bin",
                    "type": "file",
                    "size": 999999,
                },
            ],
        ]
        mock_content.return_value = "pool_size: 50\n"

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()

        # Only the small file should be fetched
        self.assertIn("pool_size", result)
        self.assertEqual(mock_content.call_count, 1)

    @patch("claude_remediator.github.get_repo_tree")
    @patch("claude_remediator.github.get_default_branch", return_value="main")
    def test_does_not_fetch_group_vars_or_inventory(self, _mock_branch, mock_tree):
        """Directories that commonly contain secrets are not traversed."""
        mock_tree.return_value = [
            {"name": "group_vars", "path": "group_vars", "type": "dir", "size": 0},
            {"name": "host_vars", "path": "host_vars", "type": "dir", "size": 0},
            {"name": "inventory", "path": "inventory", "type": "dir", "size": 0},
        ]

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()

        # Only 1 call for top-level — no further descent into secret dirs
        self.assertEqual(mock_tree.call_count, 1)
        self.assertNotIn("---", result)  # no file contents fetched


class TestBuildAnomalySection(unittest.TestCase):
    """Test the static _build_anomaly_section() method."""

    def test_formats_anomaly_context(self):
        section = RemediatorPlugin._build_anomaly_section(SAMPLE_ANOMALIES)
        self.assertIn("Triggering Log Anomalies", section)
        self.assertIn("connection refused", section)
        self.assertIn("150 occurrences", section)
        self.assertIn("api-server", section)
        self.assertIn("10.0.0.5:5432", section)

    def test_empty_anomalies(self):
        self.assertEqual(RemediatorPlugin._build_anomaly_section([]), "")


class TestPrDedup(unittest.TestCase):
    """Test that _execute_pr() checks for existing open PRs."""

    @patch("claude_remediator.github.find_open_prs")
    @patch("claude_remediator.github.create_pull_request")
    def test_skips_when_existing_pr_found(self, mock_create, mock_find):
        mock_find.return_value = [
            {
                "number": 99,
                "title": "fix: connection pool",
                "html_url": "https://github.com/cooperlees/clc_ansible/pull/99",
                "body": "...",
            }
        ]

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._execute_pr(
            PR_DATA,
            anomalies=SAMPLE_ANOMALIES,
        )

        self.assertIn("applied", result)
        self.assertTrue(result.get("skipped"))
        self.assertIn("pull/99", result["reason"])
        mock_create.assert_not_called()

    @patch("claude_remediator.github.find_open_prs")
    @patch("claude_remediator.github.create_pull_request")
    def test_creates_pr_when_no_existing(self, mock_create, mock_find):
        mock_find.return_value = []
        mock_create.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/100",
            "number": 100,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._execute_pr(
            PR_DATA,
            anomalies=SAMPLE_ANOMALIES,
        )

        self.assertIn("applied", result)
        self.assertNotIn("skipped", result)
        mock_create.assert_called_once()

    @patch("claude_remediator.github.find_open_prs")
    @patch("claude_remediator.github.create_pull_request")
    def test_dedup_error_proceeds_with_creation(self, mock_create, mock_find):
        """If the dedup search fails, we still try to create the PR."""
        mock_find.side_effect = RuntimeError("search API error")
        mock_create.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/101",
            "number": 101,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._execute_pr(
            PR_DATA,
            anomalies=SAMPLE_ANOMALIES,
        )

        self.assertIn("applied", result)
        mock_create.assert_called_once()


class TestPrLogContext(unittest.TestCase):
    """Test that PR body includes triggering log line context."""

    @patch("claude_remediator.github.find_open_prs")
    @patch("claude_remediator.github.create_pull_request")
    def test_pr_body_includes_anomaly_section(self, mock_create, mock_find):
        mock_find.return_value = []
        mock_create.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/50",
            "number": 50,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        plugin._execute_pr(
            PR_DATA,
            anomalies=SAMPLE_ANOMALIES,
        )

        # Check the body passed to create_pull_request includes anomaly context
        call_kwargs = mock_create.call_args
        body = call_kwargs[1]["body"] if call_kwargs[1] else call_kwargs[0][4]
        self.assertIn("Triggering Log Anomalies", body)
        self.assertIn("connection refused", body)
        self.assertIn("150 occurrences", body)

    @patch("claude_remediator.github.create_pull_request")
    def test_pr_body_unchanged_without_anomalies(self, mock_create):
        mock_create.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/51",
            "number": 51,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        plugin._execute_pr(PR_DATA)

        call_kwargs = mock_create.call_args[1]
        body = call_kwargs["body"]
        self.assertNotIn("Triggering Log Anomalies", body)


class TestProposeAttachesContext(unittest.TestCase):
    """Test that propose() attaches anomaly context to returned actions."""

    @patch("claude_remediator.RemediatorPlugin._fetch_repo_context")
    @patch("claude_remediator.urlopen")
    def test_actions_include_anomaly_context(self, mock_urlopen, mock_fetch):
        mock_fetch.return_value = ""
        resp = MagicMock()
        resp.read.return_value = _claude_api_response(json.dumps([PR_ACTION]))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("_anomaly_context", result[0])
        self.assertEqual(len(result[0]["_anomaly_context"]), 1)
        self.assertEqual(
            result[0]["_anomaly_context"][0]["pattern"], SAMPLE_ANOMALIES[0]["pattern"]
        )


if __name__ == "__main__":
    unittest.main()
