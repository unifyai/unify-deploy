import os
from pathlib import Path

# Maps the Python package name (left of the uv source entry) to the GitHub repo
# / sibling-folder name (which match each other). The package names are kept as
# `unity`/`unify` while the repos were renamed to `unify`/`unisdk`.
FIRST_PARTY_REPOS = {
    "unity": "unify",
    "unify": "unisdk",
    "unillm": "unillm",
}


def main() -> None:
    branch = os.environ.get("UNITY_DEPLOY_CI_FIRST_PARTY_BRANCH", "staging")
    pyproject = Path("pyproject.toml")
    text = pyproject.read_text()
    for package, repo in FIRST_PARTY_REPOS.items():
        text = text.replace(
            f'{package} = {{ path = "../{repo}", editable = true }}',
            f'{package} = {{ git = "https://github.com/unifyai/{repo}.git", branch = "{branch}" }}',
        )
    pyproject.write_text(text)


if __name__ == "__main__":
    main()
