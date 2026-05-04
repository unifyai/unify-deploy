# Issue Triage

## Overview
How to help a user review and triage issues on a GitHub repository.

## Listing Issues
1. Ask for the repository (owner/repo format)
2. Ask which state to filter: open, closed, or all
3. Call `get_repo_issues` with the owner, repo, and state
4. Present issues grouped by label if helpful

## Prioritisation Tips
- Issues with many comments often indicate community interest
- Look at labels: "bug" typically needs attention before "enhancement"
- Recently created issues with no comments may need initial triage
- Cross-reference issue count with the repo's open_issues_count from `get_repo`
