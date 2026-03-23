"""GitHub REST API client for creating branches, commits, and pull requests.

Uses only the stdlib (urllib + json) — no external dependencies.
"""

import json
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen

log = logging.getLogger("logmedic.github")

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"


def api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    """Call the GitHub REST API and return the parsed JSON response."""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    log.debug("GitHub API %s %s", method, path)

    req = Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        resp_body = e.read().decode(errors="replace")
        log.error("GitHub API %s %s → %d: %s", method, path, e.code, resp_body)
        raise RuntimeError(
            f"GitHub API {method} {path} failed ({e.code}): {resp_body}"
        ) from e


def create_pull_request(
    token: str,
    repo: str,
    branch: str,
    title: str,
    body: str,
    files: list[dict],
) -> dict:
    """Create a commit on a new branch and open a pull request.

    Uses the Git Data API so no local git clone is needed.

    Args:
        token: GitHub personal access token or app token.
        repo: Full repo name, e.g. "cooperlees/clc_ansible".
        branch: Branch name to create, e.g. "fix/db-pool".
        title: PR title (also used as the commit message).
        body: PR body / description.
        files: List of dicts with "path" and "content" keys.

    Returns:
        The PR object dict from the GitHub API.
    """
    # 1. Get the default branch and its HEAD SHA
    repo_info = api(token, "GET", f"/repos/{repo}")
    default_branch = repo_info["default_branch"]
    log.debug("default branch: %s", default_branch)

    ref_info = api(token, "GET", f"/repos/{repo}/git/ref/heads/{default_branch}")
    base_sha = ref_info["object"]["sha"]
    log.debug("base commit: %s", base_sha)

    # 2. Get the base tree
    base_commit = api(token, "GET", f"/repos/{repo}/git/commits/{base_sha}")
    base_tree_sha = base_commit["tree"]["sha"]

    # 3. Create tree with file changes
    tree_items = []
    for f in files:
        tree_items.append(
            {
                "path": f["path"],
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            }
        )
        log.debug("adding file %s", f["path"])

    new_tree = api(
        token,
        "POST",
        f"/repos/{repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": tree_items},
    )
    log.debug("created tree: %s", new_tree["sha"])

    # 4. Create commit
    new_commit = api(
        token,
        "POST",
        f"/repos/{repo}/git/commits",
        {"message": title, "tree": new_tree["sha"], "parents": [base_sha]},
    )
    log.debug("created commit: %s", new_commit["sha"])

    # 5. Create branch ref
    api(
        token,
        "POST",
        f"/repos/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]},
    )
    log.debug("created branch: %s", branch)

    # 6. Create pull request
    pr_result = api(
        token,
        "POST",
        f"/repos/{repo}/pulls",
        {"title": title, "body": body, "head": branch, "base": default_branch},
    )
    log.debug("PR created: %s (#%d)", pr_result["html_url"], pr_result["number"])
    return pr_result


def get_default_branch(token: str, repo: str) -> str:
    """Return the default branch name for a repo (e.g. ``"main"``)."""
    repo_info = api(token, "GET", f"/repos/{repo}")
    return repo_info["default_branch"]


def get_repo_tree(token: str, repo: str, path: str = "", ref: str = "") -> list[dict]:
    """Fetch the file/directory listing for a repo path.

    Uses the Contents API which supports ``ref:path`` natively and returns
    size information for blobs.  Pass *ref* (e.g. a branch name) to avoid
    an extra ``GET /repos/{repo}`` call on every invocation — callers that
    make many tree requests should resolve the default branch once via
    :func:`get_default_branch` and pass it here.

    Returns a list of entry dicts.  Each entry has at least ``name``,
    ``path``, ``type`` (``"file"`` or ``"dir"``), and ``size`` (bytes, 0
    for dirs).
    """
    contents_path = (
        f"/repos/{repo}/contents/{path}" if path else f"/repos/{repo}/contents"
    )
    if ref:
        contents_path += f"?ref={ref}"

    data = api(token, "GET", contents_path)
    # Contents API returns a list for directories, single object for files.
    entries = data if isinstance(data, list) else [data]
    log.debug("repo tree %s:%s — %d entries", repo, path or "/", len(entries))
    return entries


def get_file_content(token: str, repo: str, path: str) -> str:
    """Fetch the decoded text content of a single file from a repo."""
    import base64

    data = api(token, "GET", f"/repos/{repo}/contents/{path}")
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode(errors="replace")
    return data.get("content", "")


def find_open_prs(token: str, repo: str, search_terms: list[str]) -> list[dict]:
    """Search for open PRs whose title or body matches any of the search terms.

    Uses the GitHub search API with the ``is:pr is:open repo:<repo>`` qualifier
    combined with the given terms.  Returns a (possibly empty) list of PR
    result dicts containing ``number``, ``title``, ``html_url``, and ``body``.
    """
    if not search_terms:
        return []

    # Build an OR query from the search terms (quoted for exact matching)
    terms_query = " OR ".join(f'"{t}"' for t in search_terms[:5])  # cap at 5
    q = f"is:pr is:open repo:{repo} {terms_query}"
    log.debug("searching PRs: q=%s", q)

    from urllib.parse import quote

    data = api(token, "GET", f"/search/issues?q={quote(q)}&per_page=10")
    items = data.get("items", [])
    log.debug("PR search returned %d results", len(items))
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "html_url": item["html_url"],
            "body": item.get("body", ""),
        }
        for item in items
    ]
