"""GitHub issue listing and filtering functions.

All data and imports are kept inside function bodies so that
FunctionManager can exec each function in an isolated namespace.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def get_repo_issues(
    owner: str,
    repo: str,
    state: str = "open",
    mock: bool = True,
) -> dict:
    """List issues for a GitHub repository.

    Parameters
    ----------
    owner : str
        Repository owner (user or org).
    repo : str
        Repository name.
    state : str
        Filter by state (``'open'``, ``'closed'``, ``'all'``).
    mock : bool
        If True, returns hardcoded sample data.

    Returns
    -------
    dict
        ``{"owner": str, "repo": str, "state_filter": str,
        "issues": list, "count": int, "labels": dict}``
    """
    if mock:
        mock_issues = {
            ("octocat", "Hello-World"): [
                {
                    "number": 1347,
                    "title": "Found a bug",
                    "state": "open",
                    "user": "octocat",
                    "labels": ["bug"],
                    "created_at": "2024-01-10T12:00:00Z",
                    "comments": 3,
                    "html_url": "https://github.com/octocat/Hello-World/issues/1347",
                },
                {
                    "number": 1350,
                    "title": "Add dark mode support",
                    "state": "open",
                    "user": "contributor-1",
                    "labels": ["enhancement"],
                    "created_at": "2024-01-12T09:30:00Z",
                    "comments": 7,
                    "html_url": "https://github.com/octocat/Hello-World/issues/1350",
                },
                {
                    "number": 1355,
                    "title": "Documentation typo on README",
                    "state": "open",
                    "user": "contributor-2",
                    "labels": ["documentation"],
                    "created_at": "2024-01-15T14:00:00Z",
                    "comments": 1,
                    "html_url": "https://github.com/octocat/Hello-World/issues/1355",
                },
            ],
        }
        issues = mock_issues.get((owner, repo), [])
        if state != "all":
            issues = [i for i in issues if i["state"] == state]
        label_counts: dict[str, int] = {}
        for issue in issues:
            for label in issue.get("labels", []):
                label_counts[label] = label_counts.get(label, 0) + 1
        return {
            "owner": owner,
            "repo": repo,
            "state_filter": state,
            "issues": issues,
            "count": len(issues),
            "labels": label_counts,
        }

    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": 30},
            headers=headers,
        )
        resp.raise_for_status()
        issues = resp.json()
        label_counts = {}
        for issue in issues:
            for label_obj in issue.get("labels", []):
                name = label_obj.get("name", str(label_obj))
                label_counts[name] = label_counts.get(name, 0) + 1
        return {
            "owner": owner,
            "repo": repo,
            "state_filter": state,
            "issues": issues,
            "count": len(issues),
            "labels": label_counts,
        }
