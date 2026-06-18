"""GitHub repository detail and search functions.

All data and imports are kept inside function bodies so that
FunctionManager can exec each function in an isolated namespace.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def get_repo(owner: str, repo: str, mock: bool = True) -> dict:
    """Get detailed information about a GitHub repository.

    Parameters
    ----------
    owner : str
        Repository owner (user or org).
    repo : str
        Repository name.
    mock : bool
        If True, returns hardcoded sample data.

    Returns
    -------
    dict
        Repository details (full_name, description, language, stars, ...).
    """
    if mock:
        mock_repo_detail = {
            ("octocat", "Hello-World"): {
                "full_name": "octocat/Hello-World",
                "description": "My first repository on GitHub!",
                "language": "Ruby",
                "stargazers_count": 2500,
                "forks_count": 2100,
                "open_issues_count": 120,
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
                "created_at": "2011-01-26T19:01:12Z",
                "updated_at": "2024-01-15T10:30:00Z",
                "html_url": "https://github.com/octocat/Hello-World",
            },
            ("torvalds", "linux"): {
                "full_name": "torvalds/linux",
                "description": "Linux kernel source tree",
                "language": "C",
                "stargazers_count": 180000,
                "forks_count": 54000,
                "open_issues_count": 350,
                "default_branch": "master",
                "license": {"spdx_id": "GPL-2.0"},
                "created_at": "2011-09-04T22:48:12Z",
                "updated_at": "2024-01-20T08:00:00Z",
                "html_url": "https://github.com/torvalds/linux",
            },
        }
        detail = mock_repo_detail.get((owner, repo))
        if detail is None:
            return {
                "error": f"Repository '{owner}/{repo}' not found",
                "available_mocks": [f"{o}/{r}" for o, r in mock_repo_detail],
            }
        return detail

    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


@custom_function()
async def search_repos(query: str, mock: bool = True) -> dict:
    """Search GitHub repositories by query string.

    Parameters
    ----------
    query : str
        GitHub search query (e.g. ``'language:python stars:>10000'``).
    mock : bool
        If True, returns hardcoded sample data.

    Returns
    -------
    dict
        ``{"query": str, "total_count": int, "items": list[dict]}``
    """
    if mock:
        mock_search_results = [
            {
                "full_name": "tensorflow/tensorflow",
                "description": "An Open Source Machine Learning Framework for Everyone",
                "language": "C++",
                "stargazers_count": 185000,
            },
            {
                "full_name": "pytorch/pytorch",
                "description": "Tensors and Dynamic neural networks in Python",
                "language": "Python",
                "stargazers_count": 82000,
            },
            {
                "full_name": "huggingface/transformers",
                "description": "State-of-the-art Machine Learning for PyTorch, TensorFlow, and JAX.",
                "language": "Python",
                "stargazers_count": 130000,
            },
        ]
        return {
            "query": query,
            "total_count": len(mock_search_results),
            "items": mock_search_results,
        }

    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "per_page": 10},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "query": query,
            "total_count": data.get("total_count", 0),
            "items": data.get("items", []),
        }
