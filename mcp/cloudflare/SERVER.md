---
name: cloudflare
title: Cloudflare
description: Workers, KV, R2, D1 and Hyperdrive in your Cloudflare account.
homepage: https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/
logo: logo.svg
categories: [developer, hosting]
url: https://bindings.mcp.cloudflare.com/mcp
---

Cloudflare's Workers Bindings MCP server: list and read your Workers, and create and
manage KV namespaces, R2 buckets, D1 databases and Hyperdrive configurations in the
account you choose.

After installing, run `/mcp login cloudflare` (or press *Sign in* on its page) and approve Ultron in the browser. Cloudflare runs several more servers - documentation, observability,
Radar and others - which `/mcp add` can connect by their own addresses.
