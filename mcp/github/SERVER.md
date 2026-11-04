---
name: github
title: GitHub
description: Issues, pull requests, code search, Actions and releases on GitHub, through GitHub's own MCP server.
homepage: https://github.com/github/github-mcp-server
categories: [developer]
url: https://api.githubcopilot.com/mcp/
secrets:
  TOKEN:
    description: A GitHub personal access token. A fine-grained one, limited to the repositories and permissions you want the agent to have, is safest.
    header: Authorization
    prefix: "Bearer "
---

GitHub's hosted MCP server. It reads and writes what your token allows: issues, pull
requests and their reviews, code search across repositories, Actions runs and logs,
releases and notifications.

GitHub's sign-in does not register clients, so this entry takes a personal access token
instead: make one at *Settings > Developer settings > Personal access tokens*. Install
asks for it and keeps it in `~/.ultron/.env`; `/mcp key github` replaces it.
