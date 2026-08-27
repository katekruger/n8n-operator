# MCP client configuration examples

Two files here, matching Operator's two transports (BUILD_PLAN section 7.2):

| File | Transport | When to use it |
|---|---|---|
| [`claude_desktop_config.json`](claude_desktop_config.json) | stdio | Claude Desktop, or any MCP host that launches Operator as a local subprocess. This is the default transport and needs no network configuration at all. |
| [`streamable_http_client.json`](streamable_http_client.json) | Streamable HTTP | A remote MCP client (a hosted agent, a browser-based client, ChatGPT/OpenAI's MCP connector) that cannot spawn a local subprocess and must reach Operator over the network. |

Both were verified against a real build of this package during phase 9 release
testing — a full stdio session and a full Streamable HTTP session, each confirming the
documented 12-tool/2-resource surface and live tool calls, not just that the process
starts. See `docs/BUILD_PLAN.md`'s phase 9 checklist entry for what was run.

## stdio (Claude Desktop)

1. Install: `uv tool install n8n-operator` (or `pip install n8n-operator` — see the
   [README quickstart](../../README.md#quickstart)).
2. `n8n-operator db init`, then `n8n-operator registry reload --path <your registry>`
   — do this once, outside Claude Desktop, so the database and registry snapshot exist
   before the first launch.
3. Copy [`claude_desktop_config.json`](claude_desktop_config.json)'s `mcpServers` entry
   into your own `claude_desktop_config.json`
   (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS;
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows), filling in your real
   `N8N_OPERATOR_N8N_BASE_URL`, `N8N_OPERATOR_N8N_API_KEY`, `N8N_OPERATOR_DATABASE_URL`,
   and `N8N_OPERATOR_REGISTRY_PATH`.
4. Restart Claude Desktop.

The API key can be a literal value (fine for a single-user desktop install where the
config file itself is already local-only), or an indirect reference —
`env:SOME_VAR_NAME` (resolved from the *launched process's* environment — you'd add
`SOME_VAR_NAME` as a sibling key in the same `env` block) or `keyring:SERVICE/ACCOUNT`
(resolved from the OS keychain via the optional `keyring` extra:
`pip install 'n8n-operator[keyring]'`) — the same scheme the registry's own
`trigger.secret_ref` uses (ADR-006). A literal value in the config file is not itself a
regression: nothing in Operator ever writes it anywhere else, logs it, or returns it in
a tool result, regardless of which form you use.

## Streamable HTTP (a remote client)

Requires the server to actually be running as a network listener — this is not a
"point a client at nothing and it works" setup:

1. On the machine running Operator: set `N8N_OPERATOR_HTTP_BIND` to a non-loopback
   address (e.g. `0.0.0.0:8000`), and set **both**
   `N8N_OPERATOR_HTTP_BEARER_TOKEN` and `N8N_OPERATOR_HTTP_ALLOWED_ORIGINS` — the server
   refuses to start on a non-loopback bind without both (boundary B9, AC-20). Put
   Operator behind a reverse proxy that terminates TLS; it does not do so itself.
2. `n8n-operator serve http`.
3. On the client side, use [`streamable_http_client.json`](streamable_http_client.json)
   as a template: the `url` is `https://<your-host>/mcp`, and the `Authorization`
   header carries the exact bearer token from step 1.

A loopback bind (the default, `127.0.0.1:8000`) needs none of this — it's only
reachable from the same machine, which is the same trust level as stdio, so no bearer
token or Origin allowlist is required (and none is enforced).

### A note on OpenAI / other remote MCP connectors

The config shape above is generic Streamable HTTP — the same `url`+bearer-token pattern
most remote MCP connectors expect, OpenAI's included. The exact place to paste it
(a connector settings page, a `mcp_servers` block in an API request, etc.) is specific
to whichever client you're using; consult that client's own MCP documentation for
where the `url` and `Authorization` header go. Phase 9 release testing did not have
credentials for a live OpenAI/remote-connector run — the transport itself (bearer
token enforcement, Origin allowlist, the 12-tool surface) was verified directly against
the Streamable HTTP protocol instead; see `docs/BUILD_PLAN.md`'s phase 9 notes for
exactly what that covered and what it didn't.
