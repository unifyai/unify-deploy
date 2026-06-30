"""GitHub user profile and repository listing functions.

All data and imports are kept inside function bodies so that
FunctionManager can exec each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def get_user(username: str, mock: bool = True) -> dict:
    """Get a GitHub user's public profile.

    Parameters
    ----------
    username : str
        GitHub login handle (e.g. ``'octocat'``).
    mock : bool
        If True, returns hardcoded sample data without hitting the API.

    Returns
    -------
    dict
        User profile fields (login, id, name, company, location, ...).
    """
    if mock:
        mock_users = {
            "octocat": {
                "login": "octocat",
                "id": 583231,
                "name": "The Octocat",
                "company": "@github",
                "blog": "https://github.blog",
                "location": "San Francisco",
                "bio": "GitHub's mascot",
                "public_repos": 8,
                "followers": 12345,
                "following": 9,
                "created_at": "2011-01-25T18:44:36Z",
            },
            "torvalds": {
                "login": "torvalds",
                "id": 1024025,
                "name": "Linus Torvalds",
                "company": None,
                "blog": "",
                "location": "Portland, OR",
                "bio": None,
                "public_repos": 7,
                "followers": 220000,
                "following": 0,
                "created_at": "2011-09-03T15:26:22Z",
            },
        }
        user = mock_users.get(username)
        if user is None:
            return {
                "error": f"User '{username}' not found",
                "available_mocks": list(mock_users),
            }
        return user

    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/users/{username}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


@custom_function()
async def get_user_repos(username: str, mock: bool = True) -> dict:
    """List public repositories for a GitHub user.

    Parameters
    ----------
    username : str
        GitHub login handle.
    mock : bool
        If True, returns hardcoded sample data.

    Returns
    -------
    dict
        ``{"username": str, "repos": list[dict], "count": int}``
    """
    if mock:
        mock_repos = {
            "octocat": [
                {
                    "name": "Hello-World",
                    "full_name": "octocat/Hello-World",
                    "description": "My first repository on GitHub!",
                    "language": "Ruby",
                    "stargazers_count": 2500,
                    "forks_count": 2100,
                    "open_issues_count": 120,
                    "html_url": "https://github.com/octocat/Hello-World",
                },
                {
                    "name": "Spoon-Knife",
                    "full_name": "octocat/Spoon-Knife",
                    "description": "This repo is for demonstration purposes.",
                    "language": None,
                    "stargazers_count": 12300,
                    "forks_count": 143000,
                    "open_issues_count": 540,
                    "html_url": "https://github.com/octocat/Spoon-Knife",
                },
            ],
            "torvalds": [
                {
                    "name": "linux",
                    "full_name": "torvalds/linux",
                    "description": "Linux kernel source tree",
                    "language": "C",
                    "stargazers_count": 180000,
                    "forks_count": 54000,
                    "open_issues_count": 350,
                    "html_url": "https://github.com/torvalds/linux",
                },
            ],
        }
        repos = mock_repos.get(username, [])
        return {"username": username, "repos": repos, "count": len(repos)}

    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/users/{username}/repos",
            params={"sort": "updated", "per_page": 30},
            headers=headers,
        )
        resp.raise_for_status()
        repos = resp.json()
        return {"username": username, "repos": repos, "count": len(repos)}
