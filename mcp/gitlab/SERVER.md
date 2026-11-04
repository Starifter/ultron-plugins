---
name: gitlab
title: GitLab
description: Issues, merge requests, pipelines and code search on GitLab.
homepage: https://docs.gitlab.com/user/gitlab_duo/model_context_protocol/mcp_server/
logo: logo.svg
categories: [developer]
url: https://gitlab.com/api/v4/mcp
---

GitLab's own MCP server on GitLab.com: read and create issues and merge requests, look at
pipelines and their jobs, and search code in the projects you can see.

After installing, run `/mcp login gitlab` (or press *Sign in* on its page) and approve Ultron in the browser. For a self-managed GitLab, add it with `/mcp add gitlab
https://<your-host>/api/v4/mcp` instead.
