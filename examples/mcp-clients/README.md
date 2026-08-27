# MCP client configuration examples

Two files here, matching Operator's two transports (BUILD_PLAN section 7.2):

| File | Transport | When to use it |
|---|---|---|
| [`claude_desktop_config.json`](claude_desktop_config.json) | stdio | Claude Desktop, or any MCP host that launches Operator as a local subprocess. This is the default transport and needs no network configuration at all. |
| [`streamable_http_client.json`](streamable_http_client.json) | Streamable HTTP | A remote MCP client (a hosted agent, a browser-based client, ChatGPT/OpenAI's MCP connector) that cannot spawn a local subprocess and must reach Operator over the network. |
| [`openai_responses_tool.json`](openai_responses_tool.json) | Streamable HTTP | The MCP tool object for an OpenAI Responses API request. |

Both were verified against a real build of this package — a full stdio session and a
full Streamable HTTP session, each confirming the documented 12-tool/2-resource
surface and live tool calls, not just that the process starts. Both sessions used the
reference `mcp` Python client, not Claude Desktop's own GUI application, which has not
been separately launched. The stdio session is no longer a one-time check: it's
automated in [`scripts/mcp_session_smoke.py`](../../scripts/mcp_session_smoke.py) and
runs inside `scripts/release_smoke.sh` on every CI push. The Streamable HTTP session
remains the one-time result from phase 9 release testing. See
`docs/BUILD_PLAN.md`'s phase 9 checklist entry for what was run.

## stdio (Claude Desktop)

1. Install from a local checkout with `uv tool install .` — see the
   [README quickstart](../../README.md#quickstart). PyPI publishing is intentionally
   deferred until the live-n8n and hosted OpenAI-connector checks pass for real.
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

For OpenAI's Responses API, use
[`openai_responses_tool.json`](openai_responses_tool.json) as the object inside the
request's `tools` array. Add its explicit `Origin` value to the server-side
`N8N_OPERATOR_HTTP_ALLOWED_ORIGINS` list. The official API supports optional custom
headers for remote MCP authentication, which lets this profile satisfy Operator's
bearer-token and DNS-rebinding controls without weakening either one.

The example sets OpenAI's `require_approval` to `never` because Operator applies its
own risk-classified, out-of-band approval after the tool call. Set it to `always` if you
also want OpenAI's client-side confirmation; that produces two independent gates.

A loopback bind (the default, `127.0.0.1:8000`) needs none of this — it's only
reachable from the same machine, which is the same trust level as stdio, so no bearer
token or Origin allowlist is required (and none is enforced).

### A note on OpenAI / other remote MCP connectors

The config shape above is generic Streamable HTTP — the same `url`+bearer-token pattern
most remote MCP connectors expect, OpenAI's included. The exact place to paste it
(a connector settings page, a `mcp_servers` block in an API request, etc.) is specific
to whichever client you're using; consult that client's own MCP documentation for
where the `url` and `Authorization` header go.

[`openai_responses_tool.json`](openai_responses_tool.json)'s `Authorization`+`Origin`
`headers` map matches the OpenAI Responses API's own documented `mcp` tool schema
(`https://developers.openai.com/api/docs/api-reference/responses/create`, `tools[].mcp`):
`headers` is real and documented ("Optional HTTP headers to send to the MCP server.
Use for authentication or other purposes."), confirmed directly against that page
rather than assumed. What that page does *not* say is whether OpenAI's hosted backend
forwards a literal `Origin` header override verbatim on its outbound request — that's
unverified without a real hosted call.

No credentials and no publicly reachable TLS endpoint have been available for a live
OpenAI/remote-connector run. What has been verified instead, automated and run on
every CI pytest pass: `tests/integration/test_mcp_http_openai_compat.py` drives the
real `build_server`/`serve_http` middleware stack in-process, configured non-loopback
so the actual bearer-token and Origin-allowlist enforcement (boundary B9) runs exactly
as a real remote deployment would hit it — a full MCP session using the documented
`Authorization`+`Origin` header shape (init, the identical 12-tool/2-resource surface,
a safe tool call, session continuation), plus the missing-bearer, missing-origin, and
disallowed-origin rejections. This is protocol-and-transport-level evidence, not a
substitute for an actual hosted OpenAI request; see `docs/BUILD_PLAN.md`'s phase 9
notes for the full detail of what's covered and what still isn't.
