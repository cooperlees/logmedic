"""Shared-flow tests for plugins/logmedic_common/remediator_base.py.

Exercises the provider-agnostic base (prompt building, repo-context
fetching, PR dedup + log context, anomaly-context attachment) through a
minimal concrete subclass. Provider-specific behavior (LLM HTTP calls,
model discovery) is tested in each plugin's own test module.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(  # plugins/ (logmedic_common package)
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from logmedic_common.remediator_base import BaseRemediatorPlugin


def _make_settings(**overrides):
    raw = {
        "model": "test-model",
        "default_repo": "cooperlees/clc_ansible",
    }
    raw.update(overrides)
    return {"settings_json": json.dumps(raw)}


def _anomalies_json(anomalies):
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


class RemediatorPlugin(BaseRemediatorPlugin):
    """Minimal concrete plugin: fake LLM returns canned actions."""

    provider_label = "Test"
    logger_name = "logmedic.test_remediator"
    default_model = "test-model"

    def __init__(self, settings: dict):
        raw = json.loads(settings.get("settings_json", "{}"))
        self.api_key = "test-key"
        self.canned_response = raw.get("canned_response", json.dumps([PR_ACTION]))
        super().__init__(settings)

    def name(self) -> str:
        return "test_remediator"

    def _call_llm(self, system: str, user_msg: str, model: str) -> str:
        return self.canned_response


class TestParseResponse(unittest.TestCase):
    """Base _parse_response: arrays, single objects, envelopes, garbage."""

    def test_bare_array(self):
        plugin = RemediatorPlugin(_make_settings())
        result = plugin._parse_response(json.dumps([PR_ACTION, REPORT_ACTION]))
        self.assertEqual(len(result), 2)

    def test_single_object_wrapped(self):
        plugin = RemediatorPlugin(_make_settings())
        result = plugin._parse_response(json.dumps(REPORT_ACTION))
        self.assertEqual(len(result), 1)
        self.assertIn("report", result[0]["kind"])

    def test_envelope_unwrapped(self):
        plugin = RemediatorPlugin(_make_settings())
        result = plugin._parse_response(
            json.dumps({"response": [PR_ACTION, REPORT_ACTION]})
        )
        self.assertEqual(len(result), 2)

    def test_markdown_fences_stripped(self):
        plugin = RemediatorPlugin(_make_settings())
        fenced = "```json\n" + json.dumps([REPORT_ACTION]) + "\n```"
        result = plugin._parse_response(fenced)
        self.assertEqual(len(result), 1)

    def test_invalid_json_fallback(self):
        plugin = RemediatorPlugin(_make_settings())
        result = plugin._parse_response("not json at all")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["description"], "Test response parsing failed")
        self.assertIn("report", result[0]["kind"])

    def test_json_scalars_fallback(self):
        """Valid JSON non-arrays (null/42/string) degrade to a report."""
        plugin = RemediatorPlugin(_make_settings())
        for raw in ("null", "42", '"just refused"'):
            result = plugin._parse_response(raw)
            self.assertEqual(len(result), 1, raw)
            self.assertIn("report", result[0]["kind"])


class TestAutoExecuteGate(unittest.TestCase):
    """Mutating kinds require auto_execute=true; reports always apply."""

    def test_pr_gated_when_auto_execute_false(self):
        plugin = RemediatorPlugin(_make_settings(auto_execute=False))
        action = {
            "description": "test",
            "kind": PR_ACTION["kind"],
            "status": "proposed",
        }
        with patch.object(
            RemediatorPlugin, "_execute_pr", side_effect=AssertionError("must not run")
        ):
            result = json.loads(plugin.execute(json.dumps(action)))
        # Deserializes to ActionStatus::Proposed on the Rust side.
        self.assertEqual(result, {"proposed": None})

    def test_ssh_gated_when_auto_execute_false(self):
        plugin = RemediatorPlugin(_make_settings(auto_execute=False))
        action = {
            "description": "restart",
            "kind": {"ssh_command": {"host": "web-1", "commands": ["uptime"]}},
            "status": "proposed",
        }
        with patch.object(
            RemediatorPlugin, "_execute_ssh", side_effect=AssertionError("must not run")
        ):
            result = json.loads(plugin.execute(json.dumps(action)))
        self.assertEqual(result, {"proposed": None})

    def test_pr_runs_when_auto_execute_true(self):
        plugin = RemediatorPlugin(
            _make_settings(auto_execute=True, github_token="ghp_test")
        )
        action = {
            "description": "test",
            "kind": PR_ACTION["kind"],
            "status": "proposed",
        }
        with (
            patch.object(
                RemediatorPlugin, "_execute_pr", return_value={"applied": None}
            ) as mock_pr,
            patch(
                "logmedic_common.remediator_base.github.find_open_prs", return_value=[]
            ),
        ):
            result = json.loads(plugin.execute(json.dumps(action)))
        self.assertIn("applied", result)
        mock_pr.assert_called_once()

    def test_report_always_applies(self):
        for auto_execute in (False, True):
            plugin = RemediatorPlugin(_make_settings(auto_execute=auto_execute))
            action = {
                "description": "test",
                "kind": REPORT_ACTION["kind"],
                "status": "proposed",
            }
            result = json.loads(plugin.execute(json.dumps(action)))
            self.assertIn("applied", result)


class TestSharedProposeExecute(unittest.TestCase):
    """End-to-end propose()/execute() through the fake LLM."""

    def test_propose_attaches_context_and_executes_report(self):
        plugin = RemediatorPlugin(
            _make_settings(canned_response=json.dumps([REPORT_ACTION]))
        )
        with patch.object(RemediatorPlugin, "_fetch_repo_context", return_value=""):
            result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))
        self.assertEqual(len(result), 1)
        self.assertIn("anomaly_context", result[0])
        status = json.loads(plugin.execute(json.dumps(result[0])))
        self.assertIn("applied", status)

    def test_empty_anomalies_short_circuits(self):
        plugin = RemediatorPlugin(_make_settings())
        self.assertEqual(json.loads(plugin.propose("[]")), [])

    def test_propose_emits_rust_bridge_key(self):
        """propose() output uses `anomaly_context` so src/plugin/python.rs
        maps it into RemediationAction::context (underscore-prefixed keys
        would be dropped at the boundary)."""
        plugin = RemediatorPlugin(_make_settings())
        with patch.object(RemediatorPlugin, "_fetch_repo_context", return_value=""):
            result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))
        self.assertIn("anomaly_context", result[0])
        self.assertNotIn("_anomaly_context", result[0])
        # The attached value deserializes as Vec<LogAnomaly>-shaped dicts.
        ctx = result[0]["anomaly_context"]
        self.assertEqual(ctx[0]["pattern"], SAMPLE_ANOMALIES[0]["pattern"])
        self.assertEqual(ctx[0]["count"], 150)


class TestFetchRepoContext(unittest.TestCase):
    """Test _fetch_repo_context() which fetches files from the default repo."""

    @patch("logmedic_common.remediator_base.github.get_file_content")
    @patch("logmedic_common.remediator_base.github.get_repo_tree")
    @patch(
        "logmedic_common.remediator_base.github.get_default_branch", return_value="main"
    )
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

    @patch("logmedic_common.remediator_base.github.get_default_branch")
    def test_handles_api_error_gracefully(self, mock_branch):
        mock_branch.side_effect = RuntimeError("403 forbidden")
        plugin = RemediatorPlugin(_make_settings(github_token="ghp_test"))
        result = plugin._fetch_repo_context()
        self.assertEqual(result, "")

    @patch("logmedic_common.remediator_base.github.get_file_content")
    @patch("logmedic_common.remediator_base.github.get_repo_tree")
    @patch(
        "logmedic_common.remediator_base.github.get_default_branch", return_value="main"
    )
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

    @patch("logmedic_common.remediator_base.github.get_file_content")
    @patch("logmedic_common.remediator_base.github.get_repo_tree")
    @patch(
        "logmedic_common.remediator_base.github.get_default_branch", return_value="main"
    )
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

    @patch("logmedic_common.remediator_base.github.get_repo_tree")
    @patch(
        "logmedic_common.remediator_base.github.get_default_branch", return_value="main"
    )
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

    @patch("logmedic_common.remediator_base.github.find_open_prs")
    @patch("logmedic_common.remediator_base.github.create_pull_request")
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

    @patch("logmedic_common.remediator_base.github.find_open_prs")
    @patch("logmedic_common.remediator_base.github.create_pull_request")
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

    @patch("logmedic_common.remediator_base.github.find_open_prs")
    @patch("logmedic_common.remediator_base.github.create_pull_request")
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

    @patch("logmedic_common.remediator_base.github.find_open_prs")
    @patch("logmedic_common.remediator_base.github.create_pull_request")
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

    @patch("logmedic_common.remediator_base.github.create_pull_request")
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

    @patch.object(RemediatorPlugin, "_fetch_repo_context", return_value="")
    def test_actions_include_anomaly_context(self, mock_fetch):
        plugin = RemediatorPlugin(_make_settings())
        result = json.loads(plugin.propose(_anomalies_json(SAMPLE_ANOMALIES)))

        self.assertEqual(len(result), 1)
        self.assertIn("anomaly_context", result[0])
        self.assertEqual(len(result[0]["anomaly_context"]), 1)
        self.assertEqual(
            result[0]["anomaly_context"][0]["pattern"], SAMPLE_ANOMALIES[0]["pattern"]
        )


if __name__ == "__main__":
    unittest.main()
