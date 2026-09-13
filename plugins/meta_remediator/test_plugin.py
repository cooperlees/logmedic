"""Tests for the Meta (Muse Spark) remediator plugin."""

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))  # plugins/ (logmedic_common)
sys.path.insert(0, _THIS_DIR)  # own dir first: `import <plugin>` finds sibling

import json
import unittest
from unittest.mock import MagicMock, patch

from meta_remediator.plugin import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    LATEST_ALIASES,
    RemediatorPlugin,
)


def _make_settings(**overrides):
    # Note: auto_execute=True — these tests cover the mutation path;
    # the gated (default-false) behavior is tested in test_remediator_base.
    raw = {
        "auto_execute": True,
        "meta_api_key": "meta-test-key",
        "model": "muse-spark-1.3-contributor",
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


def _meta_api_response(actions_json, finish_reason="stop"):
    """Build a mock Meta OpenAI-compatible Chat Completions response."""
    return json.dumps(
        {
            "id": "chatcmpl-test-123",
            "object": "chat.completion",
            "created": 1789245222,
            "model": "muse-spark-1.3-contributor",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": actions_json,
                        "refusal": None,
                    },
                    "logprobs": None,
                }
            ],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
            },
        }
    ).encode()


def _models_list_response(model_ids):
    """Build a mock GET /models response."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "meta"}
                for m in model_ids
            ],
        }
    ).encode()


# The kind of JSON the model would return for a PR-based fix
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
        import os as _os

        saved = {
            k: _os.environ.pop(k, None)
            for k in ("META_API_KEY", "MUSE_API_KEY", "LLAMA_API_KEY")
        }
        try:
            plugin = RemediatorPlugin({"settings_json": "{}"})
        finally:
            for k, v in saved.items():
                if v is not None:
                    _os.environ[k] = v
        self.assertEqual(plugin.model, DEFAULT_MODEL)
        self.assertEqual(plugin.api_key, "")
        self.assertEqual(plugin.default_repo, "")
        self.assertFalse(plugin.auto_execute)
        self.assertEqual(plugin.max_tokens, DEFAULT_MAX_TOKENS)
        self.assertEqual(plugin.base_url, "https://api.meta.ai/v1")

    def test_custom_settings(self):
        plugin = RemediatorPlugin(_make_settings(auto_execute=True))
        self.assertEqual(plugin.api_key, "meta-test-key")
        self.assertEqual(plugin.default_repo, "cooperlees/clc_ansible")
        self.assertTrue(plugin.auto_execute)

    def test_name(self):
        plugin = RemediatorPlugin(_make_settings())
        self.assertEqual(plugin.name(), "meta_remediator")

    def test_key_fallbacks(self):
        # muse_api_key setting fallback
        p = RemediatorPlugin(
            {"settings_json": json.dumps({"muse_api_key": "muse-key"})}
        )
        self.assertEqual(p.api_key, "muse-key")
        # llama_api_key setting fallback
        p = RemediatorPlugin(
            {"settings_json": json.dumps({"llama_api_key": "llama-key"})}
        )
        self.assertEqual(p.api_key, "llama-key")

    def test_latest_aliases_defined(self):
        self.assertIn("latest-contributor", LATEST_ALIASES)


class TestSelectLatestContributor(unittest.TestCase):
    def test_picks_highest_version(self):
        models = [
            "muse-spark-1.2-contributor",
            "muse-spark-1.3-contributor",
            "muse-spark-1.2",
            "muse-spark-1.3",
        ]
        self.assertEqual(
            RemediatorPlugin.select_latest_contributor(models),
            "muse-spark-1.3-contributor",
        )

    def test_ignores_non_contributor(self):
        models = ["muse-spark-1.3", "muse-spark-latest", "tbh-vllm"]
        self.assertIsNone(RemediatorPlugin.select_latest_contributor(models))

    def test_empty_list(self):
        self.assertIsNone(RemediatorPlugin.select_latest_contributor([]))

    def test_numeric_not_lexical(self):
        # 1.10 > 1.9 numerically, but "1.9" > "1.10" lexically
        models = ["muse-spark-1.9-contributor", "muse-spark-1.10-contributor"]
        self.assertEqual(
            RemediatorPlugin.select_latest_contributor(models),
            "muse-spark-1.10-contributor",
        )


class TestResolveModel(unittest.TestCase):
    def test_pinned_model_no_discovery(self):
        """Pinned model → _list_models is never called."""
        plugin = RemediatorPlugin(_make_settings())
        with patch.object(
            plugin, "_list_models", side_effect=AssertionError("should not call")
        ):
            self.assertEqual(plugin._resolve_model(), "muse-spark-1.3-contributor")

    @patch("meta_remediator.plugin.urlopen")
    def test_latest_alias_discovers(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _models_list_response(
            ["muse-spark-1.2-contributor", "muse-spark-1.3-contributor"]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings(model="latest-contributor"))
        self.assertEqual(plugin._resolve_model(), "muse-spark-1.3-contributor")

    @patch("meta_remediator.plugin.urlopen")
    def test_discovery_failure_falls_back_to_default(self, mock_urlopen):
        from http.client import HTTPMessage
        from io import BytesIO
        from urllib.error import HTTPError

        err = HTTPError(
            "https://api.meta.ai/v1/models",
            401,
            "Unauthorized",
            HTTPMessage(),
            BytesIO(b'{"title":"Authentication Error"}'),
        )
        mock_urlopen.side_effect = err

        plugin = RemediatorPlugin(_make_settings(model="latest-contributor"))
        self.assertEqual(plugin._resolve_model(), DEFAULT_MODEL)

    @patch("meta_remediator.plugin.urlopen")
    def test_auto_latest_flag_overrides_pinned(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = _models_list_response(
            ["muse-spark-1.2-contributor", "muse-spark-1.4-contributor"]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(
            _make_settings(
                model="muse-spark-1.2-contributor", auto_latest_contributor=True
            )
        )
        self.assertEqual(plugin._resolve_model(), "muse-spark-1.4-contributor")


class TestPropose(unittest.TestCase):
    """Test the propose() method which sends anomalies to Meta."""

    def test_empty_anomalies(self):
        """Empty anomalies → empty actions, no API call."""
        plugin = RemediatorPlugin(_make_settings())
        result = plugin.propose("[]")
        self.assertEqual(json.loads(result), [])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_pr_action(self, mock_urlopen):
        """Model returns a PR-based remediation action."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(json.dumps([PR_ACTION]))
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

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_report_action(self, mock_urlopen):
        """Model returns a report-only action (no automated fix)."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(json.dumps([REPORT_ACTION]))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_sends_correct_request(self, mock_urlopen):
        """Verify the API request has correct URL, headers and payload shape."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response("[]")
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        plugin.propose(_anomalies_json(SAMPLE_ANOMALIES))

        call_args = mock_urlopen.call_args
        req = call_args[0][0]

        # Check URL and headers
        self.assertEqual(req.full_url, "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer meta-test-key")
        self.assertEqual(req.get_header("Content-type"), "application/json")

        # Check payload — Meta uses max_completion_tokens, not max_tokens
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "muse-spark-1.3-contributor")
        self.assertEqual(payload["max_completion_tokens"], 16384)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn("messages", payload)
        roles = [m["role"] for m in payload["messages"]]
        self.assertEqual(roles, ["system", "user"])
        # User message should contain anomaly data
        self.assertIn("connection refused", payload["messages"][1]["content"])
        self.assertIn("150", payload["messages"][1]["content"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_warns_on_length_truncation(self, mock_urlopen):
        """A warning is logged when the response is truncated (finish=length)."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(
            '[{"description": "trunca', finish_reason="length"
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        with self.assertLogs("logmedic.meta_remediator", level="WARNING") as cm:
            result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertTrue(
            any("truncated" in msg for msg in cm.output),
            f"Expected truncation warning, got: {cm.output}",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "Meta response parsing failed")

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_handles_markdown_fenced_response(self, mock_urlopen):
        """Model sometimes wraps JSON in markdown code fences."""
        fenced = "```json\n" + json.dumps([REPORT_ACTION]) + "\n```"
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(fenced)
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_single_object_wrapped_in_list(self, mock_urlopen):
        """json_object mode can return a single object instead of an array."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(json.dumps(REPORT_ACTION))
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_envelope_object_unwrapped(self, mock_urlopen):
        """json_object mode can wrap the array in a {"response": [...]} envelope."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(
            json.dumps({"response": [PR_ACTION, REPORT_ACTION]})
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 2)
        self.assertIn("pull_request", result[0]["kind"])
        self.assertIn("report", result[1]["kind"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_invalid_json_fallback(self, mock_urlopen):
        """If the model returns non-JSON, plugin wraps it in a report action."""
        resp = MagicMock()
        resp.read.return_value = _meta_api_response(
            "I'm sorry, I can't help with that."
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "Meta response parsing failed")
        self.assertIn("report", result[0]["kind"])

    @patch("meta_remediator.plugin.urlopen")
    def test_propose_http_error_includes_body(self, mock_urlopen):
        """HTTP errors from the Meta API should include the response body."""
        from http.client import HTTPMessage
        from io import BytesIO
        from urllib.error import HTTPError

        error_body = b'{"title":"Bad Request","detail":"invalid model"}'
        err = HTTPError(
            "https://api.meta.ai/v1/chat/completions",
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
        self.assertIn("invalid model", str(ctx.exception))


class TestExecute(unittest.TestCase):
    """Test the execute() method which carries out proposed actions."""

    @patch("meta_remediator.plugin.github.create_pull_request")
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

        mock_create_pr.assert_called_once_with(
            token="ghp_test123",
            repo="cooperlees/clc_ansible",
            branch="fix/db-connection-pool",
            title="fix: increase database connection pool size",
            body="The API server is exhausting its connection pool under load.",
            files=PR_DATA["files_changed"],
        )

    @patch("meta_remediator.plugin.github.create_pull_request")
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

    @patch("meta_remediator.plugin.subprocess.run")
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
        self.assertIn("--", call_args)
        self.assertLess(call_args.index("--"), call_args.index("web-1.prod"))
        self.assertIn("web-1.prod", call_args)
        self.assertIn("-i", call_args)
        self.assertIn("/tmp/test_key", call_args)

    @patch("meta_remediator.plugin.subprocess.run")
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
        plugin = RemediatorPlugin(_make_settings(auto_execute=True))
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

    def test_execute_ssh_rejects_option_like_host(self):
        """Model-returned '-oProxyCommand=...' must not become an ssh flag."""
        plugin = RemediatorPlugin(_make_settings(enable_ssh=True))
        action = {
            "description": "evil",
            "kind": {
                "ssh_command": {
                    "host": "-oProxyCommand=touch /tmp/pwned",
                    "commands": ["uptime"],
                }
            },
            "status": "proposed",
        }
        result = json.loads(plugin.execute(json.dumps(action)))
        self.assertIn("failed", result)
        self.assertIn("option-like", result["failed"]["reason"])

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

    def test_user_prompt_contains_anomaly_data(self):
        plugin = RemediatorPlugin(_make_settings())
        prompt = plugin._build_user_prompt(SAMPLE_ANOMALIES)
        self.assertIn("connection refused", prompt)
        self.assertIn("150", prompt)
        self.assertIn("api-server", prompt)
        self.assertIn("Anomaly 1", prompt)

    @patch("meta_remediator.plugin.github.create_pull_request")
    def test_execute_pr_overrides_wrong_repo(self, mock_create_pr):
        """When default_repo is set, ignore the model's hallucinated repo name."""
        mock_create_pr.return_value = {
            "html_url": "https://github.com/cooperlees/clc_ansible/pull/99",
            "number": 99,
        }

        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test123"))
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
        mock_create_pr.assert_called_once_with(
            token="ghp_test123",
            repo="cooperlees/clc_ansible",
            branch="fix/mariadb-upgrade",
            title="fix: add mariadb-upgrade task",
            body="Adds upgrade task.",
            files=wrong_repo_pr["files_changed"],
        )


class TestFetchLocalRepoContext(unittest.TestCase):
    """Test _fetch_local_repo_context() against a fake checkout on disk."""

    def _make_fake_repo(self, tmpdir):
        import os

        os.makedirs(os.path.join(tmpdir, "roles", "nginx", "defaults"))
        os.makedirs(os.path.join(tmpdir, "roles", "nginx", "tasks"))
        os.makedirs(os.path.join(tmpdir, "group_vars"))
        with open(os.path.join(tmpdir, "site.yaml"), "w") as f:
            f.write("- hosts: all\n")
        with open(
            os.path.join(tmpdir, "roles", "nginx", "defaults", "main.yml"), "w"
        ) as f:
            f.write("worker_connections: 1024\n")
        with open(
            os.path.join(tmpdir, "roles", "nginx", "tasks", "main.yml"), "w"
        ) as f:
            f.write("- name: install nginx\n")
        # Secret dir that must NOT be traversed
        with open(os.path.join(tmpdir, "group_vars", "secret.yml"), "w") as f:
            f.write("password: hunter2\n")
        return tmpdir

    def test_reads_roles_files_not_secrets(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._make_fake_repo(tmp)
            plugin = RemediatorPlugin(
                _make_settings(
                    default_repo="cooperlees/clc_ansible", local_repo_path=tmp
                )
            )
            result = plugin._fetch_repo_context()
            self.assertIn("worker_connections", result)
            self.assertIn("install nginx", result)
            self.assertNotIn("hunter2", result)

    def test_symlink_escape_rejected(self):
        """A symlinked role dir pointing outside the checkout is skipped."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.txt"), "w") as f:
                f.write("TOP-SECRET-OUTSIDE-CHECKOUT\n")

            checkout = os.path.join(tmp, "checkout")
            os.makedirs(os.path.join(checkout, "roles"))
            os.symlink(outside, os.path.join(checkout, "roles", "evil"))
            # roles/evil -> outside: traversal must not enter it
            plugin = RemediatorPlugin(
                _make_settings(
                    default_repo="cooperlees/clc_ansible", local_repo_path=checkout
                )
            )
            with self.assertLogs("logmedic.meta_remediator", level="WARNING") as cm:
                result = plugin._fetch_repo_context()
            self.assertNotIn("TOP-SECRET", result)
            self.assertTrue(
                any("escaping local checkout" in m for m in cm.output),
                f"expected escape warning, got: {cm.output}",
            )

    def test_symlink_file_escape_rejected(self):
        """A symlinked file inside tasks/ pointing outside is skipped."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "secret.txt"), "w") as f:
                f.write("FILE-SECRET-OUTSIDE\n")
            checkout = os.path.join(tmp, "checkout")
            tasks = os.path.join(checkout, "roles", "app", "tasks")
            os.makedirs(tasks)
            with open(os.path.join(tasks, "ok.yml"), "w") as f:
                f.write("- name: ok\n")
            os.symlink(os.path.join(tmp, "secret.txt"), os.path.join(tasks, "evil.yml"))

            plugin = RemediatorPlugin(_make_settings(local_repo_path=checkout))
            result = plugin._fetch_repo_context()
            self.assertNotIn("FILE-SECRET", result)
            self.assertIn("- name: ok", result)

    def test_missing_dir_returns_empty(self):
        plugin = RemediatorPlugin(
            _make_settings(local_repo_path="/nonexistent/path/xyz")
        )
        self.assertEqual(plugin._fetch_repo_context(), "")

    def test_skips_vault_and_large_files(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            subdir = os.path.join(tmp, "roles", "db", "vars")
            os.makedirs(subdir)
            with open(os.path.join(subdir, "secret.yml"), "w") as f:
                f.write("$ANSIBLE_VAULT;1.1;AES256\n6162630a...")
            with open(os.path.join(subdir, "big.yml"), "w") as f:
                f.write("x" * 9000)
            with open(os.path.join(subdir, "ok.yml"), "w") as f:
                f.write("pool_size: 50\n")
            plugin = RemediatorPlugin(_make_settings(local_repo_path=tmp))
            result = plugin._fetch_repo_context()
            self.assertIn("pool_size", result)
            self.assertNotIn("ANSIBLE_VAULT", result)


if __name__ == "__main__":
    unittest.main()
