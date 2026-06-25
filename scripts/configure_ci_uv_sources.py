import os
from pathlib import Path

FIRST_PARTY_REPOS = {
    "unity": "unity",
    "unify": "unify",
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
