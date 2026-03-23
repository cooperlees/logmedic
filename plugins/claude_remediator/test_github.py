"""Tests for the GitHub REST API client module."""

import json
import unittest
from email.message import Message
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

import github


def _mock_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestApi(unittest.TestCase):
    """Test the low-level github.api() function."""

    @patch("github.urlopen")
    def test_get_request(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"id": 1})

        result = github.api("ghp_token", "GET", "/repos/org/repo")

        self.assertEqual(result, {"id": 1})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/org/repo")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer ghp_token")
        self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(req.get_header("X-github-api-version"), "2022-11-28")

    @patch("github.urlopen")
    def test_post_request_with_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"sha": "abc"})

        result = github.api(
            "ghp_token", "POST", "/repos/org/repo/git/refs", {"ref": "refs/heads/fix"}
        )

        self.assertEqual(result, {"sha": "abc"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        body = json.loads(req.data)
        self.assertEqual(body["ref"], "refs/heads/fix")

    @patch("github.urlopen")
    def test_http_error_raises_runtime_error(self, mock_urlopen):
        error = HTTPError(
            "https://api.github.com/repos/x",
            404,
            "Not Found",
            Message(),
            MagicMock(read=lambda: b'{"message": "Not Found"}'),
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(RuntimeError) as ctx:
            github.api("ghp_token", "GET", "/repos/x")

        self.assertIn("404", str(ctx.exception))
        self.assertIn("Not Found", str(ctx.exception))


class TestCreatePullRequest(unittest.TestCase):
    """Test the create_pull_request() high-level function."""

    @patch("github.api")
    def test_full_pr_flow(self, mock_api):
        """Verify the sequence of API calls for creating a PR."""
        mock_api.side_effect = [
            # 1. GET /repos/{repo} → repo info
            {"default_branch": "main"},
            # 2. GET /repos/{repo}/git/ref/heads/main → HEAD ref
            {"object": {"sha": "base_sha_123"}},
            # 3. GET /repos/{repo}/git/commits/{sha} → base commit
            {"tree": {"sha": "base_tree_456"}},
            # 4. POST /repos/{repo}/git/trees → new tree
            {"sha": "new_tree_789"},
            # 5. POST /repos/{repo}/git/commits → new commit
            {"sha": "new_commit_abc"},
            # 6. POST /repos/{repo}/git/refs → create branch
            {"ref": "refs/heads/fix/pool"},
            # 7. POST /repos/{repo}/pulls → create PR
            {
                "html_url": "https://github.com/cooperlees/clc_ansible/pull/42",
                "number": 42,
            },
        ]

        files = [
            {"path": "roles/api/defaults/main.yml", "content": "pool_size: 50\n"},
            {"path": "roles/api/tasks/main.yml", "content": "- restart: api\n"},
        ]

        result = github.create_pull_request(
            token="ghp_test",
            repo="cooperlees/clc_ansible",
            branch="fix/pool",
            title="fix: connection pool",
            body="Increase pool size",
            files=files,
        )

        self.assertEqual(result["number"], 42)
        self.assertEqual(len(mock_api.call_args_list), 7)

        # Verify call sequence
        calls = mock_api.call_args_list

        # 1. Get repo info
        self.assertEqual(
            calls[0], call("ghp_test", "GET", "/repos/cooperlees/clc_ansible")
        )

        # 2. Get default branch HEAD
        self.assertEqual(
            calls[1],
            call("ghp_test", "GET", "/repos/cooperlees/clc_ansible/git/ref/heads/main"),
        )

        # 3. Get base commit
        self.assertEqual(
            calls[2],
            call(
                "ghp_test",
                "GET",
                "/repos/cooperlees/clc_ansible/git/commits/base_sha_123",
            ),
        )

        # 4. Create tree — verify files are included
        tree_body = calls[3][0][3]
        self.assertEqual(tree_body["base_tree"], "base_tree_456")
        self.assertEqual(len(tree_body["tree"]), 2)
        self.assertEqual(tree_body["tree"][0]["path"], "roles/api/defaults/main.yml")
        self.assertEqual(tree_body["tree"][0]["content"], "pool_size: 50\n")
        self.assertEqual(tree_body["tree"][1]["path"], "roles/api/tasks/main.yml")

        # 5. Create commit
        commit_body = calls[4][0][3]
        self.assertEqual(commit_body["message"], "fix: connection pool")
        self.assertEqual(commit_body["tree"], "new_tree_789")
        self.assertEqual(commit_body["parents"], ["base_sha_123"])

        # 6. Create branch ref
        ref_body = calls[5][0][3]
        self.assertEqual(ref_body["ref"], "refs/heads/fix/pool")
        self.assertEqual(ref_body["sha"], "new_commit_abc")

        # 7. Create PR
        pr_body = calls[6][0][3]
        self.assertEqual(pr_body["title"], "fix: connection pool")
        self.assertEqual(pr_body["body"], "Increase pool size")
        self.assertEqual(pr_body["head"], "fix/pool")
        self.assertEqual(pr_body["base"], "main")

    @patch("github.api")
    def test_api_error_propagates(self, mock_api):
        """Errors from the API should propagate up."""
        mock_api.side_effect = RuntimeError("GitHub API failed (403): forbidden")

        with self.assertRaises(RuntimeError) as ctx:
            github.create_pull_request(
                token="ghp_test",
                repo="cooperlees/clc_ansible",
                branch="fix/x",
                title="t",
                body="b",
                files=[],
            )

        self.assertIn("403", str(ctx.exception))


class TestGetRepoTree(unittest.TestCase):
    """Test the get_repo_tree() function."""

    @patch("github.api")
    def test_returns_tree_entries(self, mock_api):
        mock_api.side_effect = [
            {"default_branch": "main"},
            {
                "tree": [
                    {"path": "roles", "type": "tree", "sha": "abc"},
                    {"path": "README.md", "type": "blob", "sha": "def", "size": 100},
                ]
            },
        ]

        result = github.get_repo_tree("ghp_tok", "cooperlees/clc_ansible")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["path"], "roles")
        self.assertEqual(result[0]["type"], "tree")
        self.assertEqual(result[1]["path"], "README.md")

    @patch("github.api")
    def test_with_subpath(self, mock_api):
        mock_api.side_effect = [
            {"default_branch": "main"},
            {"tree": [{"path": "defaults", "type": "tree", "sha": "x"}]},
        ]

        result = github.get_repo_tree("ghp_tok", "cooperlees/clc_ansible", "roles/nginx")

        # Should request main:roles/nginx
        tree_call = mock_api.call_args_list[1]
        self.assertIn("main:roles/nginx", tree_call[0][2])
        self.assertEqual(len(result), 1)

    @patch("github.api")
    def test_error_propagates(self, mock_api):
        mock_api.side_effect = RuntimeError("404")

        with self.assertRaises(RuntimeError):
            github.get_repo_tree("ghp_tok", "cooperlees/clc_ansible")


class TestGetFileContent(unittest.TestCase):
    """Test the get_file_content() function."""

    @patch("github.api")
    def test_base64_content(self, mock_api):
        import base64

        content = "db_pool_size: 50\n"
        encoded = base64.b64encode(content.encode()).decode()
        mock_api.return_value = {"content": encoded, "encoding": "base64"}

        result = github.get_file_content("ghp_tok", "cooperlees/clc_ansible", "roles/api/defaults/main.yml")

        self.assertEqual(result, content)
        mock_api.assert_called_once_with(
            "ghp_tok", "GET", "/repos/cooperlees/clc_ansible/contents/roles/api/defaults/main.yml"
        )

    @patch("github.api")
    def test_non_base64_content(self, mock_api):
        mock_api.return_value = {"content": "raw text", "encoding": "none"}

        result = github.get_file_content("ghp_tok", "cooperlees/clc_ansible", "README.md")

        self.assertEqual(result, "raw text")


class TestFindOpenPrs(unittest.TestCase):
    """Test the find_open_prs() function."""

    @patch("github.api")
    def test_returns_matching_prs(self, mock_api):
        mock_api.return_value = {
            "total_count": 1,
            "items": [
                {
                    "number": 42,
                    "title": "fix: connection pool",
                    "html_url": "https://github.com/cooperlees/clc_ansible/pull/42",
                    "body": "Fix connection refused errors",
                },
            ],
        }

        result = github.find_open_prs(
            "ghp_tok",
            "cooperlees/clc_ansible",
            ["connection refused to database"],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 42)
        # Verify search query format
        call_path = mock_api.call_args[0][2]
        self.assertIn("is%3Apr", call_path)
        self.assertIn("is%3Aopen", call_path)
        self.assertIn("cooperlees/clc_ansible", call_path)

    @patch("github.api")
    def test_no_results(self, mock_api):
        mock_api.return_value = {"total_count": 0, "items": []}

        result = github.find_open_prs("ghp_tok", "cooperlees/clc_ansible", ["some pattern"])

        self.assertEqual(result, [])

    def test_empty_search_terms(self):
        """No API call when search_terms is empty."""
        result = github.find_open_prs("ghp_tok", "cooperlees/clc_ansible", [])
        self.assertEqual(result, [])

    @patch("github.api")
    def test_caps_search_terms_at_five(self, mock_api):
        mock_api.return_value = {"total_count": 0, "items": []}

        terms = [f"pattern_{i}" for i in range(10)]
        github.find_open_prs("ghp_tok", "cooperlees/clc_ansible", terms)

        call_path = mock_api.call_args[0][2]
        # Only first 5 terms should be in the query
        self.assertIn("pattern_0", call_path)
        self.assertIn("pattern_4", call_path)
        self.assertNotIn("pattern_5", call_path)


if __name__ == "__main__":
    unittest.main()
