"""Tests for the Claude remediator plugin."""

import json
import unittest
from unittest.mock import MagicMock, patch

from claude_remediator import RemediatorPlugin


def _make_settings(**overrides):
    raw = {
        "anthropic_api_key": "sk-ant-test-key",
        "model": "claude-sonnet-4-6",
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


def _claude_api_response(actions_json):
    """Build a mock Anthropic Messages API response."""
    return json.dumps(
        {
            "id": "msg_test_123",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": actions_json}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
    ).encode()


# The kind of JSON Claude would return for a PR-based fix
PR_ACTION = {
    "description": "Fix database connection pool configuration",
    "kind": {
        "pull_request": {
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
    },
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
        self.assertEqual(plugin.model, "claude-sonnet-4-6")
        self.assertEqual(plugin.api_key, "")
        self.assertEqual(plugin.default_repo, "")
        self.assertFalse(plugin.auto_execute)

    def test_custom_settings(self):
        plugin = RemediatorPlugin(_make_settings(auto_execute=True))
        self.assertEqual(plugin.api_key, "sk-ant-test-key")
        self.assertEqual(plugin.default_repo, "cooperlees/clc_ansible")
        assertTrue = self.assertTrue
        assertTrue(plugin.auto_execute)

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
        self.assertEqual(payload["model"], "claude-sonnet-4-6")
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertIn("messages", payload)
        self.assertIn("system", payload)
        # User message should contain anomaly data
        user_content = payload["messages"][0]["content"]
        self.assertIn("connection refused", user_content)
        self.assertIn("150", user_content)

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

        error_body = b'{"type":"error","error":{"type":"invalid_request_error","message":"model: claude-sonnet-4-20250514 is not available"}}'
        err = HTTPError(
            "https://api.anthropic.com/v1/messages",
            400,
            "Bad Request",
            {},  # type: ignore[arg-type]
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
            files=PR_ACTION["kind"]["pull_request"]["files_changed"],  # type: ignore[index]
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

        self.assertIn("report", result)
        self.assertIn("message", result["report"])

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

    def test_system_prompt_includes_custom_context(self):
        plugin = RemediatorPlugin(
            _make_settings(system_prompt="We use Ansible for all config management.")
        )
        prompt = plugin._build_system_prompt()
        self.assertIn("Ansible", prompt)

    def test_user_prompt_contains_anomaly_data(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_user_prompt(SAMPLE_ANOMALIES)
        self.assertIn("connection refused", prompt)
        self.assertIn("150", prompt)
        self.assertIn("api-server", prompt)
        self.assertIn("Anomaly 1", prompt)


if __name__ == "__main__":
    unittest.main()
