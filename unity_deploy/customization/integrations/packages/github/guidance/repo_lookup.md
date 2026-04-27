# Repository Lookup

## Overview
How to help a user find information about GitHub repositories and users.

## User Lookup
1. Ask for the GitHub username
2. Call `get_user` with the username to retrieve their profile
3. If they want to see repositories, call `get_user_repos`
4. Highlight key stats: public repos, followers, account age

## Repository Details
1. Ask for the owner and repository name (e.g. "torvalds/linux")
2. Call `get_repo` to get full details: stars, forks, language, license
3. Mention the default branch and last update time

## Search
1. Ask what the user is looking for (language, topic, minimum stars)
2. Build a GitHub search query (e.g. "language:python stars:>10000")
3. Call `search_repos` and present the top results with descriptions
