# Stage 11 Evidence: Tool-Count and Protocol-Session Verification

Captured 2026-08-30 on branch `feat/v2-stage-11-integration-release-and-proof`,
commit `88ee347a51050d41db5b8813083f10ee01f29246`, against a freshly built wheel
(`n8n_operator-1.0.0rc3-py3-none-any.whl`, rebuilt from a clean `dist/` for this run).

## 1. stdio MCP session smoke test (`scripts/mcp_session_smoke.py`)

The script hard-codes the v1-compatibility tool/resource surface (it has no mode
flag), so it was run via `scripts/release_smoke.sh`, which builds an isolated venv,
installs the fresh wheel into it, runs the full CLI lifecycle (`db init`, `registry
validate`, `audit verify`, `registry reload`), and then drives a real MCP client
session over stdio against the installed `n8n-operator` entry point.

Command:

```bash
rm -rf dist/ && uv build
bash scripts/release_smoke.sh
```

Sanitized output (local temp-directory paths under the OS temp root replaced with
`<smoke-venv>`; everything else is verbatim):

```
Using Python 3.12.14 environment at: <smoke-venv>
Resolved 54 packages in 668ms
Installed 54 packages in 81ms
 + n8n-operator==1.0.0rc3 (from file://.../dist/n8n_operator-1.0.0rc3-py3-none-any.whl)
 ...
Database initialized (sqlite+pysqlite:///<smoke-venv>/operator.db); schema is at head.
Registry is valid.
  path:          examples/registry/workflows.example.yaml
  content_hash:  sha256:493bc9b79064f3d519ae1aa5a4561d460daf2965bc63d01721e0ad3f52e93ebf
  workflows:     4 (3 enabled)
OK — the audit chain is intact.
Registry reloaded; a new snapshot is now active.
  snapshot_id:   01M1A4TZFPDDXEY7F36EQYNEES
  content_hash:  sha256:493bc9b79064f3d519ae1aa5a4561d460daf2965bc63d01721e0ad3f52e93ebf
initialized: server=n8n-operator
tool surface confirmed: exactly 12 tools
resource surface confirmed: ['audit://operations/{operation_id}', 'registry://workflows']
list_workflows call succeeded, no leaked credentials/IDs
registry://workflows resource read succeeded, no leaked credentials/IDs
MCP session smoke passed: initialize, tool surface, resource surface, safe tool call, resource read, clean shutdown
release smoke passed: wheel install, import, CLI, migration, registry, audit, MCP stdio session
```

**Result: PASS.** A real `initialize` / `list_tools` / `list_resources` /
`call_tool` / clean-shutdown round trip over stdio against the built wheel confirms
exactly 12 tools and 2 resources in v1-compatibility mode, with no credential or
identifier leakage in tool results or resource reads.

## 2. v1/v2 tool-count contract (`tests/contract/test_mcp_tool_inventory.py`)

`scripts/mcp_session_smoke.py` only exercises the v1-compatibility surface (12
tools) — it has no v2 mode flag. Per the task brief, the v1/v2 (12-vs-20) split
required by AC-23 was verified instead via the existing contract test suite, which
asserts both tool counts directly against `build_tools()`.

Command:

```bash
uv run pytest tests/contract/test_mcp_tool_inventory.py -v
```

Output (verbatim, in full — 43 tests):

```
collecting ... collected 43 items

tests/contract/test_mcp_tool_inventory.py::test_exactly_twelve_tools_registered PASSED [  2%]
tests/contract/test_mcp_tool_inventory.py::test_no_unplanned_tool_is_registered PASSED [  4%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[cancel_operation] PASSED [  6%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[describe_workflow] PASSED [  9%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[execute_operation] PASSED [ 11%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[get_execution_log] PASSED [ 13%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[get_execution_result] PASSED [ 16%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[get_instance_health] PASSED [ 18%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[get_operation] PASSED [ 20%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[list_operations] PASSED [ 23%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[list_workflows] PASSED [ 25%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[preflight_workflow] PASSED [ 27%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[prepare_operation] PASSED [ 30%]
tests/contract/test_mcp_tool_inventory.py::test_every_tool_schema_rejects_unknown_properties[validate_input] PASSED [ 32%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[cancel_operation] PASSED [ 34%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[describe_workflow] PASSED [ 37%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[execute_operation] PASSED [ 39%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[get_execution_log] PASSED [ 41%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[get_execution_result] PASSED [ 44%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[get_instance_health] PASSED [ 46%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[get_operation] PASSED [ 48%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[list_operations] PASSED [ 51%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[list_workflows] PASSED [ 53%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[preflight_workflow] PASSED [ 55%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[prepare_operation] PASSED [ 58%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_schema_field_shapes_a_raw_n8n_identifier_or_url[validate_input] PASSED [ 60%]
tests/contract/test_mcp_tool_inventory.py::test_no_tool_grants_approval PASSED [ 62%]
tests/contract/test_mcp_tool_inventory.py::test_execute_operation_is_annotated_side_effecting PASSED [ 65%]
tests/contract/test_mcp_tool_inventory.py::test_cancel_operation_is_annotated_state_changing_but_not_destructive PASSED [ 67%]
tests/contract/test_mcp_tool_inventory.py::test_prepare_operation_is_annotated_non_readonly_but_does_not_run_n8n PASSED [ 69%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[describe_workflow] PASSED [ 72%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[get_execution_log] PASSED [ 74%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[get_execution_result] PASSED [ 76%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[get_instance_health] PASSED [ 79%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[get_operation] PASSED [ 81%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[list_operations] PASSED [ 83%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[list_workflows] PASSED [ 86%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[preflight_workflow] PASSED [ 88%]
tests/contract/test_mcp_tool_inventory.py::test_pure_read_tools_are_annotated_read_only[validate_input] PASSED [ 90%]
tests/contract/test_mcp_tool_inventory.py::test_exactly_the_two_v1_resources_are_registered PASSED [ 93%]
tests/contract/test_mcp_tool_inventory.py::test_same_tool_schemas_across_local_and_remote_deps PASSED [ 95%]
tests/contract/test_mcp_tool_inventory.py::test_v2_tools_including_retry_operation_are_identical_across_transports PASSED [ 97%]
tests/contract/test_mcp_tool_inventory.py::test_whoami_is_registered_only_when_v2_is_enabled PASSED [100%]

============================== 43 passed in 0.72s ===============================
```

The decisive assertions, from `tests/contract/test_mcp_tool_inventory.py::test_whoami_is_registered_only_when_v2_is_enabled`:

- `v1_names == EXPECTED_TOOL_NAMES` and `len(v1_tools) == 12` — v1-compatibility mode
  (`enable_v2` unset) exposes exactly 12 tools.
- `v2_names == EXPECTED_TOOL_NAMES | {whoami, list_environments, request_approval,
  get_approval_status, retry_operation, diff_workflow_definition, get_metrics,
  list_audit_events}` and `len(v2_tools) == 20` — v2 mode (`enable_v2=True`) adds the
  eight stage-02/04/05/06/07/08 tools for a total of 20.

**Result: PASS.** Confirms AC-23's 12-tool (v1) / 20-tool (v2) split, and that the
schema/annotations for every tool are identical regardless of caller locality
(local vs. remote) in both modes.

## 3. OpenAI-compatible Streamable HTTP session test

Command:

```bash
uv run pytest tests/integration/test_mcp_http_openai_compat.py -v
```

Output (verbatim):

```
collecting ... collected 5 items

tests/integration/test_mcp_http_openai_compat.py::test_full_session_with_the_documented_openai_header_shape PASSED [ 20%]
tests/integration/test_mcp_http_openai_compat.py::test_missing_bearer_token_is_rejected_over_a_real_session PASSED [ 40%]
tests/integration/test_mcp_http_openai_compat.py::test_missing_origin_is_rejected_over_a_real_session PASSED [ 60%]
tests/integration/test_mcp_http_openai_compat.py::test_disallowed_origin_is_rejected_over_a_real_session PASSED [ 80%]
tests/integration/test_mcp_http_openai_compat.py::test_http_tool_and_resource_surface_matches_stdio PASSED [100%]

============================== 5 passed in 0.95s ================================
```

This suite drives the real production Streamable HTTP ASGI wiring (`build_server`
plus the actual lifespan protocol, not a mock) and exercises:

- a full session using the documented OpenAI Responses API `mcp` tool header shape
  (`Authorization: Bearer <token>` + explicit `Origin`), including a safe tool call
  and session continuation on a second request;
- rejection of a missing bearer token, a missing `Origin`, and a disallowed `Origin`,
  each over a real session (not a unit-level header check);
- that the tool and resource surface over Streamable HTTP is identical to the stdio
  surface verified in section 1 above (AC-23).

**Result: PASS.** This is protocol-conformance evidence for the OpenAI Responses API
`mcp` tool shape against the current code — the test was run, not rewritten.

## 4. Hosted client validation status (explicitly pending)

No hosted Claude or OpenAI API credentials exist anywhere in this environment:

```
$ env | grep -iE "anthropic|openai|claude" | grep -iE "api_key|api-key"
(no output)
$ env | grep -iE "api_key|api-key"
(no output)
```

The only `CLAUDE_*` / `ANTHROPIC_*` environment variables present are this Claude
Code CLI session's own harness variables (session IDs, socket paths, SDK version
markers) — none of them are, or resemble, a hosted Anthropic or OpenAI API key.

Consistent with the plan's constraint on this stage (confirmed during
brainstorming): **no credential should be added to make hosted-client validation
"complete."** Sections 1–3 above establish that the MCP protocol surface itself
(stdio tool/resource inventory, v1/v2 tool-count split, and the OpenAI-compatible
Streamable HTTP session shape) is correct and tested against real client sessions.
What remains **explicitly unverified and pending** is an end-to-end session from an
actual hosted client (e.g., Claude Desktop or the OpenAI Responses API `mcp` tool
talking to a real, deployed server) — that verification requires a hosted API
credential this environment does not have and is not expected to acquire for this
task. This pending status is cross-referenced from Task 7 and Task 10 of this plan.
