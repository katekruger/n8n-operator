# n8n Operator — Threat Model

> Scope: v1 as specified in [BUILD_PLAN.md](BUILD_PLAN.md). Trust zones and boundary
> controls (B1–B13) are normative there in section 9; this document derives the threats
> those controls exist to answer, states what is *not* mitigated, and records what
> changes in v2 and v3.
>
> Method: STRIDE across the trust boundaries, plus a dedicated section for LLM-specific
> threats that STRIDE does not naturally cover.

---

## 1. Why this system needs a threat model at all

n8n Operator exists to hold credentials that an LLM must not hold, and to perform
actions an LLM must not perform unsupervised. It is, by construction, a **confused
deputy**: privileged, and driven by an untrusted caller. Nearly every threat below is
a variation on one question — *can the caller make the deputy use its authority in a
way the operator did not intend?*

The defensive posture follows from that: authority is enumerated in a registry rather
than delegated, every side effect is bound to a specific approved operation, and the
human sits on a channel the caller cannot reach.

---

## 2. Assets

| # | Asset | Why it matters | Impact if lost |
|---|---|---|---|
| A1 | n8n API key | Full control of the n8n instance and everything it can reach. | Critical |
| A2 | Webhook secrets | Direct workflow invocation, bypassing Operator entirely. | Critical |
| A3 | n8n instance URL | Enables direct attack on Zone C, bypassing Zone B. | High |
| A4 | The registry (allowlist) | Defines the entire set of permitted actions. | Critical |
| A5 | Operation handles | Each is a one-shot capability to cause a side effect. | High |
| A6 | Approval tokens | Each converts a pending intent into executable authority. | High |
| A7 | The audit log | The only record of what happened and on whose authority. | High |
| A8 | Execution results | May contain customer PII from downstream systems. | High |
| A9 | Operation arguments | May contain PII supplied by the caller. | Medium |
| A10 | Downstream systems (Zone D) | Where irreversible effects land. | Critical |
| A11 | *(v2, stage 02)* OIDC bearer tokens and service-principal credentials | Each is an identity assertion; a service-principal credential is a durable, rotatable secret in its own right (never a literal value Operator stores — `credential_ref` indirection, ADR-006/ADR-013). | Critical |

---

## 3. Trust boundaries

Zones A–D are defined in BUILD_PLAN section 9.1. Four boundaries carry threats:

| Boundary | Crossing | Trust change |
|---|---|---|
| **TB1** | MCP client → Operator | Untrusted → trusted. The primary attack surface. |
| **TB2** | Human → approval app | Out-of-band authority injection. Deliberately unreachable from TB1. |
| **TB3** | Operator → n8n | Trusted → privileged. Credentials are used here. |
| **TB4** | n8n → Operator (results) | Privileged-but-untrusted-content returning into the trusted zone. |

---

## 4. Adversaries

| # | Adversary | Capability | Assumed goal |
|---|---|---|---|
| ADV-1 | **Manipulated agent** | Full control of MCP tool calls, via prompt injection in content the model is reading. The primary adversary. | Cause an unapproved or altered side effect; exfiltrate credentials or data. |
| ADV-2 | **Malicious MCP client** | Same as ADV-1, without needing to fool a model. A hostile or compromised client process. | Same. |
| ADV-3 | **Network attacker** | Observes or reaches the Streamable HTTP listener or the loopback approval app. | Reach an unauthenticated control surface. |
| ADV-4 | **Malicious web page** | Runs JavaScript in the operator's browser. | Reach a loopback listener via DNS rebinding or CSRF. |
| ADV-5 | **Compromised n8n** | Controls workflow definitions and returned results. | Alter behavior post-approval; inject content into the model's context. |
| ADV-6 | **Curious insider** | Local read access to the database or logs. | Read secrets or PII at rest. |
| ADV-7 | **Local malware** | Code execution as the operator. | Everything. **Out of scope** — see section 8. |

---

## 5. Threats

Severity is pre-mitigation. `v1` status values: **mitigated**, **partial**, **accepted**.

### 5.1 TB1 — MCP client to Operator

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-01 | Elevation | Caller invokes a workflow the operator never registered. | Critical | Default-deny registry; `workflow_id` resolves against a snapshot or fails ([ADR-002](adr/ADR-002-default-deny-registry.md)). B1. | mitigated |
| T-02 | Elevation | Caller supplies a raw n8n workflow ID, URL, or webhook path to reach an unregistered workflow. | Critical | No tool argument accepts one; impossible by schema, not by check. B1. | mitigated |
| T-03 | Tampering | Caller passes extra fields to reach node behavior outside the declared contract. | High | `additionalProperties: false` required on every `input_schema` (rule R4); unknown fields are a hard error. B2. | mitigated |
| T-04 | Elevation | Caller executes without preparing — fabricating or guessing a handle. | Critical | Handles are server-minted ULIDs bound to principal, workflow, and argument fingerprint; unguessable and verified ([ADR-003](adr/ADR-003-operation-handles.md)). B3. | mitigated |
| T-05 | Tampering | Caller prepares benign arguments, gets approval, then executes with different arguments. | Critical | The argument fingerprint recorded at prepare is re-checked at execute; mismatch is `ARGUMENT_MISMATCH` (invariant I5). | mitigated |
| T-06 | Elevation | Caller replays a handle to run a side effect twice. | High | Single-use burn via conditional update with a checked affected-row count (I4); second attempt is `HANDLE_ALREADY_USED` (AC-10). | mitigated |
| T-07 | Elevation | Caller self-approves by calling an approval tool. | Critical | No approval tool exists in any version. Approval crosses TB2 only. B4. | mitigated |
| T-08 | Elevation | Caller fetches the approval URL returned by `prepare_operation` to approve itself. | Critical | `GET` renders a page and grants nothing; approval requires a `POST` from a human session with a CSRF token. Possessing the URL is not authority. | mitigated |
| T-09 | Information disclosure | Caller extracts credentials, instance URL, or n8n IDs from a tool result. | Critical | Response-shaping allowlist; secrets never enter result objects; property test asserts non-appearance (B5, AC-18). | mitigated |
| T-10 | Information disclosure | Caller enumerates unregistered workflows by probing IDs and reading distinct errors. | Medium | `WORKFLOW_NOT_FOUND` is returned identically for unregistered and nonexistent IDs (AC-01). | mitigated |
| T-11 | Denial of service | Caller floods `prepare_operation` or `execute_operation`. | Medium | Per-workflow `max_concurrent` and `rate_limit_per_minute`; `RATE_LIMITED` and `CONCURRENCY_LIMIT_REACHED`. | partial |
| T-12 | Denial of service | Caller exhausts disk by preparing operations with huge argument payloads. | Medium | Core-enforced cap on the canonical argument size, applied identically for every adapter and **before** persistence; transport caps remain as defense in depth; results capped by `output.max_bytes`. B12, invariant I10, [ADR-011](adr/ADR-011-argument-limits-and-idempotency.md). | mitigated |
| T-13 | Repudiation | Caller denies having requested an action. | High | Every transition writes an `operation_events` row and a hash-chained `audit_log` row in the same transaction (I6). B11. | mitigated |
| T-14 | Spoofing | Caller claims to be a different principal. | High | v1 has exactly one local principal; there is nothing to spoof. **Stage 02:** real in v2 OIDC mode — resolved server-side from a validated bearer token's `(iss, sub)` (identity anchor, never `sub` alone), never from a tool argument; no tool accepts a field asserting "act as principal X" (B14, [ADR-014](adr/ADR-014-oidc-trust-and-session-model.md)). | mitigated (v2 OIDC mode); accepted (v1, and v2 `identity_mode=dev`, by design) |
| T-42 | Spoofing | A forged or algorithm-confused JWT (unsigned `alg: none`, or an RS256 public key replayed as an HS256 secret) is accepted as a valid identity assertion. | Critical | `ALLOWED_ALGORITHMS` is a fixed allowlist (RS/ES/PS 256/384/512 only); `none` and every `HS*` algorithm are rejected before signature verification runs, regardless of what the token's own header claims (`identity/oidc.py`). Every validation failure — wrong issuer, wrong audience, forbidden algorithm, unsigned, expired, not-yet-valid, unknown `kid` after one rate-limited re-fetch — returns the identical `None`/`INVALID_TOKEN` outcome; no distinguishing error tells a prober which check it failed (ADR-014's anti-oracle discipline, extended from B1/T-10's "no enumeration oracle" pattern to identity). `tests/unit/test_identity_oidc.py`. | mitigated |
| T-43 | Denial of service | A forged or rotating `kid` value is used to force an unbounded stream of JWKS re-fetches against the IdP. | Medium | Exactly one re-fetch per unknown `kid`; a `kid` still not found after that re-fetch is rate-limited (not re-fetched again) until a cooldown elapses, distinct from a `kid` a re-fetch *does* find (rotation is picked up immediately, never penalized). `tests/unit/test_identity_oidc.py::test_unknown_kid_refetch_is_rate_limited`, `::test_jwks_rotation_is_picked_up_after_the_old_key_stops_working`. | mitigated |
| T-44 | Elevation | A principal is disabled (or its organization membership removed) mid-session, but a long-lived bearer token or a cached authorization decision keeps working. | High | Stateless bearer auth — no server-side session table to go stale — and `disabled_at`/membership state is re-checked against the live database on *every* call, never cached; the identical, still-cryptographically-valid token is rejected the instant the row changes (ADR-014 section 4). `tests/integration/test_operator_token_verifier.py::test_a_disabled_principal_is_rejected_even_with_a_valid_token`, `::test_a_removed_membership_leaves_whoami_empty_but_the_principal_still_authenticates`. | mitigated |
| T-45 | Elevation | JIT provisioning of a first-seen `(iss, sub)` grants more than "this subject can now authenticate" — e.g. an implicit membership or role. | Critical | `resolve_user_principal` creates a bare `principals` row and nothing else; granting any role in any organization is always a separate, explicit admin act (`identity add-membership`). A freshly JIT-provisioned user calling `whoami` gets `"organizations": []` — authenticated, authorized for nothing (ADR-013 section 2). `tests/integration/test_mcp_whoami_tool.py::test_whoami_for_a_principal_with_no_active_organization_returns_an_empty_list`. | mitigated |
| T-46 | Elevation | A valid token for principal A is used to see or influence principal B's organizations, roles, or environments — "token substitution across organizations". | Critical | `whoami` (and every future org-scoped v2 read) is built entirely from a database query keyed on the already-resolved `principal_id`; no tool argument or JWT claim is ever consulted to decide which organization a caller sees (B15, ADR-013). Two distinct principals, each a member of a different single-organization, each see only their own. `tests/integration/test_mcp_whoami_tool.py::test_whoami_reflects_only_database_membership_never_a_claim_the_caller_asserts`. | mitigated |
| T-47 | Information disclosure | A service principal's resolved `credential_ref` value (or another identity-adjacent secret) leaks into a structured log line or a CLI's own output. | High | Never printed by any CLI command (`create-service-principal`, `rotate-service-credential`); registered with the existing log-scrubbing mechanism (`logging_setup.register_secret`) the instant it is resolved, in both the CLI validation path and the server's per-request service-credential match path — not only on a successful match, so scrubbing does not depend on which request happened to use it (extends B7/T-23 to a value that is resolved live, per request, rather than once at startup). `tests/integration/test_cli_identity.py::test_create_service_principal_registers_the_resolved_secret_for_log_scrubbing`, `tests/integration/test_operator_token_verifier.py::test_resolving_a_service_credential_registers_it_for_log_scrubbing`. `whoami`'s own result additionally never carries a provider token or raw claim — `tests/integration/test_mcp_whoami_tool.py::test_whoami_never_leaks_a_provider_token_or_raw_claim`. | mitigated |
| T-48 | Elevation | `identity_mode=dev`'s fixed, unauthenticated development principal is reachable from a non-loopback (network-exposed) deployment, letting anyone who can reach the port act as a privileged, un-gated identity. | Critical | A `config.py` startup validator (`_validate_v2_identity_mode`) refuses to start with `identity_mode=dev` on a non-loopback HTTP bind; the dev principal's own `display_name` is set to `"local development (identity_mode=dev — never for production)"` so it is unmistakable in any audit trail or `whoami` result even in the loopback case it's meant for. stdio always uses this fixed principal regardless of configured mode (ADR-014 section 5) — but stdio has no network listener to expose in the first place, so that case carries no equivalent risk. | mitigated |
| T-49 | Elevation | A caller whose role lacks a tool's capability (e.g. a `viewer` calling `prepare_operation`) reaches it anyway — a role-capability bypass. | Critical | One evaluator (`core.authorization.evaluate`), called from every gated `core.service` function; the caller-facing denial is `WORKFLOW_NOT_FOUND`/`OPERATION_NOT_FOUND`, identical to absence (invariant I14). Exhaustively property-tested against all 20 v1+v2 tool names × 4 roles (AC-38). `tests/property/test_rbac_matrix.py`. | mitigated |
| T-50 | Elevation | A principal's memberships in two organizations are combined into a broader implicit grant than either alone authorizes — e.g. `operator` scope from org A applied to a workflow only org B's (narrower) grant covers. | Critical | Each membership's role ∧ workflow-scope ∧ environment-scope is evaluated as a single, independent, self-contained unit (`core.authorization.evaluate`'s per-membership loop) — a call is authorized only if *one* membership satisfies it entirely on its own terms, never by combining fields from two (ADR-015's own rejected alternative). `tests/integration/test_authorization_service.py::test_a_principal_in_two_organizations_is_authorized_by_either_grant_independently`; monotonicity proven generally by `tests/property/test_no_enumeration.py`. | mitigated |
| T-51 | Information disclosure | A caller infers the existence of an out-of-scope operation via a pagination side channel — a page returned short of the requested `limit`, or a cursor that skips past a hidden row, reveals that *something* was filtered out. | Medium | Scope filtering (`workflow_id_like_patterns`, translated from `workflow_scope` glob patterns) is pushed into the SQL query itself, applied *before* `LIMIT`, never after (`storage.repository.OperationRepository.list`) — a page is always the true page, and a cursor never walks past a row a filter would have hidden. `tests/integration/test_authorization_service.py::test_list_operations_scope_filter_applies_before_the_page_limit`. | mitigated |
| T-62 | Information disclosure | A `get_metrics` (stage 08) breakdown reveals the existence of an out-of-scope workflow via aggregation rather than a direct lookup — a distinct count in a `group_by=workflow` response, or a non-empty `by_outcome` bucket, is exactly the "distinct result reveals existence" pattern T-10 already names for direct lookups, applied here to an aggregate. | Medium | The same filter-*before*-aggregation rule ADR-019 states as its own core decision: every count, breakdown entry, and percentile is computed only over operations already scoped to the caller's authorized workflows (`OperationRepository.count_by_outcome`/`breakdown_by_workflow`'s own `workflow_id_like_patterns` argument, applied as a SQL `WHERE`, never as post-hoc filtering of an already-aggregated result). `tests/property/test_metrics_audit_scope.py::test_get_metrics_breakdown_never_includes_the_out_of_scope_workflow`. | mitigated |
| T-63 | Information disclosure | `list_audit_events` (stage 08) misclassifies an out-of-scope event as in-scope through its `subject_type`-dependent scope logic — four different code paths (`workflow`/`operation` via an `EXISTS` join/`environment`/`registry_snapshot`), any one of which could regress independently and leak an event a direct `list_operations`-style query would never have leaked. | Medium | Each `subject_type` branch is exercised by its own dedicated test against a real database (`AuditLogRepository.list_page`), plus a cross-cutting scope scenario proving no branch leaks a sales-only workflow's events to a marketing-scoped viewer, and a dedicated non-admin `registry_snapshot`-exclusion test. `tests/integration/test_metrics_audit_repository.py::test_audit_log_list_page_scopes_workflow_subject_type`, `::test_audit_log_list_page_scopes_operation_subject_type_via_join`, `::test_audit_log_list_page_scopes_environment_subject_type_exactly`, `::test_audit_log_list_page_excludes_registry_snapshot_events_unless_included`; `tests/property/test_metrics_audit_scope.py::test_list_audit_events_never_returns_an_out_of_scope_operation_subject`, `::test_list_audit_events_never_leaks_a_registry_snapshot_event_to_a_non_admin`. | mitigated |
| T-52 | Elevation | A principal holding `approver` (or `admin`) decides their own operation — the role-level self-dealing case, distinct from ADR-017's per-operation `approval_policy_snapshot` exclusion (T-55, stage 05). | Critical | `core.authorization.evaluate`'s `requester_principal_id == decider_principal_id` check, using `operations.principal_id` (already recorded at prepare time) — denies regardless of role, raising the identical `OperationNotFoundError` a nonexistent operation would. Proven at the `core.service` level and through the real CLI (the CLI's own fixed identity in v2 mode makes this the *only* self-dealing shape currently reachable through it — see section 8 item 5's addendum). `tests/integration/test_authorization_service.py::test_an_approver_may_never_decide_their_own_operation`, `tests/integration/test_cli_operations.py::test_v2_cli_cannot_approve_its_own_operation`. | mitigated |
| T-53 | Elevation | An adapter (MCP, CLI, or the approval app) reimplements a role check locally instead of calling the shared evaluator, drifting out of sync with the real matrix over time as tools are added. | Medium | A dedicated layering contract test statically scans every adapter file for a reconstructed role-name collection (two or more of the four role strings in one list/set/tuple literal) — the signature of redefining the role vocabulary locally — and fails the build if found, with one documented, narrow exception (`cli/commands/identity.py`'s `VALID_ROLES`, grant-time *input* validation, never a decision). `tests/contract/test_layering.py::test_no_adapter_reconstructs_the_role_vocabulary_itself`. | mitigated |
| T-54 | Elevation | A membership's `environment_scope: ["*"]` — "every environment in *my own* organization" (ADR-016 section 2) — was, until this stage made `environment_id` reachable at all, evaluated with no organization-ownership check: a membership in organization A with a wildcard environment grant would have authorized *any* `environment_id`, including one belonging to an unrelated organization B the caller has no membership in at all. Found and fixed within this same stage, before ever reaching a real v1 tool call (RR-13 was exactly "not reachable yet"), via a real-database regression test (`test_cross_environment_operation_access_is_denied`) written against this stage's own new scenarios. | Critical | `core.authorization.evaluate` gained an `environment_organization_id` parameter — the organization the caller-visible `environment_id` actually belongs to, resolved by the caller (`identity.resolve_environment`, or an `EnvironmentRepository` lookup for an operation's already-recorded `environment_id`) — checked *before* a membership's own `environment_scope` pattern, so a membership whose own organization does not own the environment is refused regardless of how wide its grant is. `tests/property/test_no_enumeration.py::test_a_wildcard_environment_scope_never_authorizes_another_organizations_environment`, `tests/integration/test_environment_service.py::test_cross_environment_operation_access_is_denied`. | mitigated |
| T-55 | Elevation | The per-operation approval-policy snapshot (ADR-017 section 1, stage 05) includes the requester among their own operation's eligible approvers, or re-expands to admit a principal granted `approver` after `PENDING_APPROVAL` was entered — the snapshot-level self-dealing/scope-creep case, distinct from T-52's role-level check. | Critical | `_compute_eligible_approvers` structurally excludes `requester_principal_id` from the computed set before it is ever written; the snapshot is written once, at T04, onto `operations.approval_policy_snapshot`, and never recomputed or unioned with a later membership state (invariant I13) — `approve_operation`'s v2 branch reads only the frozen snapshot, never a live membership query. `tests/property/test_approval_snapshot.py::test_requester_is_excluded_from_their_own_snapshot_regardless_of_other_roles`, `::test_membership_added_after_snapshot_never_joins_it`. | mitigated |
| T-56 | Information disclosure / Tampering | The `NotificationSink` webhook surface (ADR-018; stage 05) leaks operation content (arguments, workflow title/description, execution results) in its outbound payload, or an unauthenticated/unbounded delivery lets a slow or hostile endpoint back up retries indefinitely. | High | `NotificationEvent` (`core.models`) carries only `event_type`, `subject_type`, `subject_id`, `principal_id`, `occurred_at`, and `fetch_reference` (a CLI command hint, never a URL or the detail itself) — boundary B16; `WebhookNotificationSink.deliver` builds its JSON body from this same fixed field list, so there is no path by which operation content could reach it. The bearer token is resolved via the same `env:`/`keyring:` indirection as `n8n_api_key` (ADR-006) and registered for log scrubbing before use; TLS verification is never disableable (mirroring `n8n/client.py`, T-26). Retry is bounded (`retry_failed_notifications`, `max_attempts`) and terminates in a permanent `DELIVERY_FAILED` status, never retried again. `tests/unit/test_notification_sink.py::test_notification_event_never_carries_operation_content`, `::test_bounded_retry_marks_delivery_failed_after_max_attempts`, `tests/integration/test_quorum_approval.py::test_notification_payload_never_carries_the_operations_own_argument_value`. | mitigated |
| T-57 | Spoofing | A per-approver web approval token (stage 05) is forged, replayed, or reused to decide as a different eligible approver than the one it was minted for — the v2 analogue of v1's single shared-token binding (T-08's own predecessor concern). | High | `compute_approval_binding` includes the deciding principal (`approval_row.assigned_to or row.principal_id`) as one of its bound fields, so a token minted for approver A can never verify as approver B's decision — the binding check fails structurally, not by a runtime identity comparison that could be bypassed. Each token is single-use (`ApprovalTokenAlreadyUsedError` on a second presentation) and TTL-bounded, identical to v1. `tests/integration/test_approval_app_quorum.py::test_each_approver_token_decides_only_as_its_own_principal`, `::test_a_tokens_second_use_is_rejected_as_already_used`. | mitigated |
| T-58 | Tampering | Reconciliation evidence for an `UNKNOWN` operation (stage 06, ADR-009) is recorded on an operator's unverified say-so — a human types "it succeeded" and the audit trail treats that as fact, no different from a forged record. | High | `reconcile_operation` never records an annotation from a bare claim: it always performs a live `ReconciliationPort.get_execution(execution_id)` lookup against the real n8n instance and cross-checks the returned `n8n_workflow_id` against the operation's own registered one — a mismatched workflow, a nonexistent execution, or an unreachable instance all refuse and record nothing (`ReconciliationNotApplicableError`). The human's own `--note` is stored alongside the verified evidence, never in place of it. `tests/unit/test_reconciliation.py::test_exact_id_match_records_exactly_one_annotation`, `::test_mismatched_workflow_id_refuses_and_records_nothing`, `::test_unreachable_instance_refuses_and_records_nothing`. | mitigated |
| T-59 | Elevation | The requester of an operation (or any principal who happens to own it) bypasses `retry_operation`'s/`reconcile_operation`'s `admin`-only gate via the "an operation's own owner may always see it" shortcut most other read paths use (`_get_owned_operation_row`). | Critical | `retry_operation` is gated purely by `authorization.ROLE_CAPABILITIES` membership (`admin` only) — the same evaluator every other v2 capability check uses, with no ownership shortcut in its path at all. `reconcile_operation` goes further: it deliberately does **not** call `_get_owned_operation_row` (the one function in `core/service.py` with an owner-sees-their-own-work early return) — it fetches the row plainly and always runs the `admin`-only `_authorize` check, so the operation's own requester gets no special access to reconcile or retry it themselves. `tests/integration/test_retry_service.py::test_retry_by_a_non_admin_role_is_not_found`. | mitigated |

### 5.2 TB2 — Human to approval app

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-15 | Elevation | ADV-4 uses DNS rebinding to reach the loopback approval app from a web page. | High | `Origin` and `Host` validation on every state-changing route; loopback-only bind (B10). | mitigated |
| T-16 | Elevation | CSRF causes the operator's browser to approve an operation. | High | `POST`-only approval with a per-token CSRF token and `SameSite=Strict` cookies. | mitigated |
| T-17 | Elevation | Approval token is guessed. | High | 256-bit random token; stored only as a sha256 hash; single-use; TTL-bounded (AC-21). | mitigated |
| T-18 | Elevation | Approval token is replayed after use or after expiry. | High | Single-use burn plus TTL check; the operation must be in `PENDING_APPROVAL` (AC-21). | mitigated |
| T-19 | Tampering | The approval page shows different arguments than those that will execute. | Critical | The page renders the exact stored arguments; the same fingerprint gates execution (I5). | mitigated |
| T-20 | Elevation | Human approves without reading — "approval fatigue" turns the gate into a rubber stamp. | High | Page leads with risk, side-effect class, and full arguments; drift is shown prominently. Ultimately a human factor. | partial |
| T-21 | Information disclosure | Approval page leaks PII to a shoulder-surfer or browser history. | Medium | Tokens in the path are single-use and short-lived; arguments render redacted per `output.redact`. | partial |
| T-38 | Spoofing / Denial of service | A remote MCP client is handed a `127.0.0.1` approval URL. The model reports that it "sent the link", the human follows an address that resolves to nothing on their own machine, and the operation silently expires unapproved — or, worse, the URL reaches something else listening on that port on the caller's host. | Medium | `approval_url` is returned only to callers the transport proves are local; remote callers receive `approval_required`, the operation ID, and CLI instructions. B13, invariant I12, [ADR-010](adr/ADR-010-approval-delivery-and-expiry.md), AC-31. | mitigated |

### 5.3 TB3 — Operator to n8n

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-22 | Information disclosure | Credentials are committed to source control in the registry file. | Critical | `secret_ref` must be an indirect reference; a literal secret is a load-time error (rule R6, [ADR-006](adr/ADR-006-server-owned-n8n-credentials.md)). | mitigated |
| T-23 | Information disclosure | Credentials leak into logs or the database. | High | Secrets held in memory only; structured logging scrubs configured secret values; no secret column exists in any table. B7. | mitigated |
| T-24 | Tampering | The workflow is modified in n8n between registration and use. | Critical | `definition_hash` checked at preflight; `BLOCKED` on drift (AC-06). Coverage depends on canonicalization being conservative — see T-39. | mitigated |
| T-25 | Tampering | The workflow is modified between **approval** and **execution** — the TOCTOU window that matters. | Critical | Hash re-checked at execute; `DEFINITION_DRIFT` and nothing is dispatched (B8, AC-13). | mitigated |
| T-26 | Spoofing | Operator is pointed at an attacker-controlled n8n instance. | High | Instance URL comes from validated config, never from a tool argument or the registry; TLS verification is not disableable. | mitigated |
| T-27 | Denial of service | A hung n8n exhausts Operator's connections. | Medium | Explicit connect and read timeouts on every request; no unbounded waits; no retries (ADR-005). | mitigated |
| T-28 | Elevation | A registered workflow does more than its description claims — a benign title over a destructive graph. | Critical | `definition_hash` pins *what was reviewed*. Operator cannot read intent from a node graph; the operator must review before registering. v3 evaluation lab narrows this. | partial |
| T-39 | Tampering | **Canonicalization excludes a field that turns out to be behaviourally significant.** A semantic change to the workflow then preserves the hash, the drift check passes, and Operator executes a graph nobody reviewed — silently defeating B8, T-24, and T-25 at once. | Critical | Inclusion by default; a field is excluded only after an empirical harness proves it cannot alter behavior; the allowlist is explicit, enumerated, evidence-bearing and version-scoped; semantic categories are never excludable; phase 4 ships with an empty allowlist. CAN-01 - CAN-07, [ADR-008](adr/ADR-008-conservative-definition-canonicalization.md), AC-26 - AC-28. | mitigated |
| T-40 | Repudiation | An indeterminate dispatch cannot be reconciled, because the workflow returns no execution identifier — so whether the side effect occurred is unrecoverable from Operator's records. | Medium | Opt-in response envelope carries the n8n execution ID; preflight reports `NO_EXECUTION_CORRELATION` as a non-blocking `warn` **before** approval, so the limitation is visible when it can still be fixed. When an execution ID *is* available, stage 06's `reconcile_operation` closes the rest of the gap: a verified, exact-ID annotation recorded against `audit_log`, never inferred or trusted unverified (T-58). Still only `partial` because a workflow declaring no correlation at all remains genuinely unreconcilable — that residual gap is inherent to the workflow's own design, not something Operator can close from its side. [ADR-009](adr/ADR-009-dispatch-correlation.md), [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md), AC-29, AC-30. | partial |
| T-41 | Spoofing | Preflight reports a credential check as passing and an operator reads it as "the credential works", approving on false assurance when the credential is bound but expired or revoked. | Medium | The check is named and worded for bindings only; validity is reported `unverifiable` with `CREDENTIAL_VALIDITY_UNVERIFIED` rather than `pass`, and Operator never asserts validity without a supported n8n mechanism that tests it. [ADR-009](adr/ADR-009-dispatch-correlation.md), AC-30. | mitigated |
| T-61 | Information disclosure | A caller compares credential-identifier digests returned across separate `diff_workflow_definition` (stage 07) calls to infer that two workflows, or two diff entries within one workflow, share the same underlying credential binding — a relationship the raw identifier itself was never meant to reveal. | Medium | `_redact_credential_ids` generates a fresh random salt per call (`secrets.token_hex`); equal raw ids within one call map to equal digests (so an unchanged binding still shows no false `modified`), but no digest is ever comparable across two separate calls, defeating cross-call correlation by construction. `tests/integration/test_definition_diff_service.py::test_credential_id_change_is_visible_without_echoing_raw_ids`. | mitigated |

### 5.4 TB4 — n8n results returning into the trusted zone

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-29 | Tampering | A result contains text engineered to steer the model reading it (indirect prompt injection). | High | Results are returned as structured data, never as instructions; no tool interprets a result as a request to act. Every further side effect needs its own prepare, approval, and handle. Content cannot be sanitized semantically. | partial |
| T-30 | Information disclosure | A result carries PII or secrets from a downstream system into the model's context. | High | Per-workflow `output.redact` paths and `max_bytes` cap; redaction verified as a property over nested and array positions (AC-19). Redaction is operator-configured, so completeness depends on the operator. | partial |
| T-31 | Denial of service | An enormous result exhausts memory or the client's context. | Medium | Streaming read with a hard byte cap; truncation marked explicitly. | mitigated |
| T-32 | Tampering | A malformed result crashes result parsing. | Low | Results parsed into typed models; parse failure yields `FAILED` with a structured error, never an unhandled exception. | mitigated |

### 5.5 Transport and storage

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-33 | Spoofing | Anyone on the network reaches an exposed Streamable HTTP listener. | Critical | Loopback bind by default; non-loopback requires a bearer token **and** an `Origin` allowlist, or startup fails (B9, AC-20). | mitigated |
| T-34 | Elevation | ADV-4 reaches the loopback MCP HTTP listener via DNS rebinding. | High | `Origin` validation on the MCP HTTP transport, same as the approval app. | mitigated |
| T-35 | Tampering | ADV-6 edits the SQLite file to erase or fabricate audit records. | High | Hash-chained audit log makes edits detectable: `n8n-operator audit verify` (phase 8) walks the whole chain and reports the exact sequence number of the first break, exiting a distinct code; `audit export` embeds the same verification in a portable record a separate process can independently re-check (AC-22, AC-25). Detection, not prevention. | mitigated |
| T-36 | Information disclosure | ADV-6 reads PII from the database at rest. | Medium | `execution_results.redacted_payload`/`error` are redacted and size-capped before they are ever written (`record_execution_outcome`). `operations.arguments`, however, is stored **raw** — phase 7 moved redaction from write-time to the read boundary (`get_operation`, `audit export`) because dispatch and the execute-time argument-fingerprint re-check both need the real values, and a value redacted at rest can never be un-redacted later. So an operator with database read access sees caller-supplied arguments in the clear; no credential is ever stored (ADR-006), and filesystem permissions remain the operator's sole protection. v1 adds no encryption at rest. | accepted |
| T-37 | Tampering | An operation is left stranded in `EXECUTING` by a crash, and later resolved wrongly. | Medium | `EXECUTING` commits (the handle burn) before dispatch, so a crash can never make an operation appear to have both never run and already been claimed. What v1 does *not* have: any automatic or CLI-driven path that resolves a crash-stranded `EXECUTING` operation forward at all — it is not silently promoted to `SUCCEEDED`, and nothing retries it (ADR-005), but nothing moves it to `UNKNOWN` either. It stays `EXECUTING` — correctly inert, not correctly resolved — until an operator manually confirms the outcome against n8n and updates the row directly; see [v1 limitations](V1_LIMITATIONS.md) and the [UNKNOWN reconciliation guide](RECONCILING_UNKNOWN.md). This is a narrower guarantee than "recovery resolves stranded operations" implied — corrected here for the v1 release audit (phase 9). | partial |
| T-60 | Information disclosure | ADV-6 with database read access reads a workflow's full, unredacted canonical structural definition — node graph, parameters, business logic — from `workflow_definition_snapshots` (stage 07), a new at-rest asset beyond the bare `definition_hash` previously stored. | Low | Contains no secret *values* (n8n's own `GET /workflows/{id}` never returns credential secrets, only id/name bindings), only workflow structure — the same "no credential ever stored" guarantee ADR-006 already gives every other table. Read access to it is scoped by the same role/workflow-scope authorization as any other `diff_workflow_definition` read. Extends T-36's acceptance (arguments stored raw) to this table: filesystem permissions remain the operator's sole protection, v1/v2 add no encryption at rest. | accepted |
| T-64 | Tampering | ADV-6 (database write access) also steals or edits the local anchor signing key file (`audit_anchor/local_file.py`, stage 09) — the three-way compromise (database + host + key) ADR-012 section 2 explicitly names as unsolved by either `AuditAnchor` implementation. With the key, the attacker can rewrite both the database *and* the anchor file with matching, correctly-signed content, defeating detection entirely. | High (pre-mitigation) | `n8n-operator anchor init-key` writes the private key with `0600` permissions, outside the database, refusing to overwrite an existing key silently (`KeyFileExistsError`) — raises the bar (an attacker needs host-level file read, not just database access) but does not close the gap: this is exactly RR-4's residual, restated as its own entry rather than left only in the ADR's prose. `tests/integration/test_cli_anchor.py::test_init_key_writes_a_key_with_0600_permissions`, `tests/integration/test_audit_anchor_secret_inspection.py`. | accepted |
| T-65 | Spoofing | A forged or replayed anchor receipt is presented to `anchor verify` as if it were genuine — either a receipt for a `covers_through_seq`/`entry_hash` pair that was never actually signed, or a stale, previously-valid receipt replayed against a *different* (rolled-back or since-tampered) chain state. | Medium | Every verification recomputes the Ed25519 signature over the anchor's own canonical bytes (`covers_through_seq`, `entry_hash`, `entry_count`, `anchored_at`) under the claimed public key — a forged receipt fails signature verification outright (`verify_signature` returns `False`, never raises, so a caller cannot forget to check it). A replayed *genuine* receipt for stale chain state is caught by `verify_anchor_against_database`'s own independent-copy recomputation: the entry at `covers_through_seq` on the copy must itself hash-chain-verify and match `entry_hash`, so a receipt whose claimed state no longer matches the real chain (including a chain that advanced, or one restored from an older backup) fails there even if the signature alone would have passed. `tests/unit/test_audit_anchor_base.py::test_verify_fails_when_the_anchor_content_changes_after_signing`, `::test_verify_fails_with_a_flipped_signature_byte`, `tests/integration/test_audit_anchor_service.py::test_verify_anchor_against_database_fails_on_a_stale_copy`, `::test_verify_anchor_against_database_fails_on_a_mismatched_entry_hash`. | mitigated |
| T-66 | Information disclosure | **Found and fixed, Stage 11 (internal security review).** `AuditLogRepository.list_page`'s `subject_type="operation"` branch (stage 08, ADR-012 section 3) matched an operation's own `workflow_id` against a caller's `workflow_id_like_patterns` via a correlated `EXISTS` subquery, but never also checked that the operation's own `environment_id` matched the caller's resolved `environment_id` — unlike the sibling `environment_clause`, which always has. Because this deployment uses one global workflow registry (a `workflow_id` such as `crm.sync_contact` is the same row referenced by every organization's operations, the identical reasoning T-54/RR-13 already established for environment-scope authorization generally), a principal holding a broad `workflow_scope`/`environment_scope=["*"]` grant in *any* organization could call `list_audit_events` and see another organization's `operation`-subject audit events — including their `detail` — for any workflow id both organizations happened to share. `core.service.get_metrics` was independently verified **not** vulnerable to the same class: it filters on `Operation.environment` (the legacy string column), which `prepare_operation`/`retry_operation` populate with the *same resolved environment ULID* as `Operation.environment_id` in v2 mode, so its scope filter was always a correct per-row comparison, not a separate unscoped subquery. A systematic audit of every other v2 read surface (`OperationRepository`/`ExecutionResultRepository`/`EnvironmentRepository` reads, `get_approval_status`, `list_reconciliation_events`, `diff_workflow_definition`, `check_and_deliver_alerts`, `AuditAnchorRepository`) found no further instance of this class — each already scopes per-row by `environment_id`/`organization_id` (via `_get_owned_operation_row`/`_authorize`/`_resolve_scope`) or is deliberately org-agnostic by design (workflow *definitions* are global, matching the audit log's own untouched `subject_type="workflow"` branch; anchors cover the whole chain by design, ADR-012 section 2). | Critical | `operation_clause`'s `EXISTS` subquery now also requires `Operation.environment_id == environment_id` (`false()` when `environment_id` is `None`, mirroring `environment_clause`'s own existing pattern) — closing the gap the same way T-54 closed the analogous authorization-evaluator gap. `tests/integration/test_metrics_audit_repository.py` (repository-level matrix: two organizations, three environments, one shared workflow id, wildcard/exact/prefix/empty `workflow_id_like_patterns`, a pagination-cursor boundary test, and a workflow-subject-detail-never-carries-tenant-data regression test), `tests/integration/postgres/test_audit_log_cross_org_isolation.py` (same matrix against real PostgreSQL), `tests/integration/test_metrics_audit_service.py` (service-level, through the real `_resolve_scope` authorization path, plus a `get_metrics` cross-org regression test and its own pagination-cursor boundary test), `tests/integration/test_mcp_metrics_audit_tools.py::test_list_audit_events_never_leaks_another_orgs_operation_through_mcp` (full MCP `call_tool` round trip, anti-enumeration: absent, never present-with-redacted-detail), `tests/integration/test_v2_integrated_scenario.py::TestTwoOrgThreeEnvironmentScenario::test_get_metrics_and_audit_events_never_cross_the_org_boundary`, `tests/integration/test_tenant_isolation_matrix.py` (parameterized guard covering `list_audit_events`, `get_metrics`, `list_operations`, `get_execution_result`, `get_approval_status`, `list_reconciliation_events`, and `list_environments` against the same two-org fixture, extensible to future read surfaces). Full findings: `docs/evidence/stage11-security-review.md`. | mitigated |

---

## 6. LLM-specific threats

STRIDE assumes an adversary with intent. The characteristic failure here is an agent
with no intent at all, doing the wrong thing confidently.

| ID | Threat | Why the architecture answers it |
|---|---|---|
| L-01 | **Indirect prompt injection.** The model reads hostile content and is steered into calling a destructive workflow. | The model can only reach registered workflows (T-01), only with schema-valid arguments (T-03), and — for anything with side effects — only after a human outside the model's channel approved *these* arguments (T-07, T-19). Injection can produce a *request*; it cannot produce an *approval*. |
| L-02 | **Tool-description poisoning.** Hostile text in a workflow title or description manipulates the model. | Titles and descriptions are operator-authored in the registry, never fetched from n8n at runtime. The registry is the only source of tool-adjacent text. |
| L-03 | **Confused deputy.** The model borrows Operator's credentials for something it should not do. | Authority is enumerated, not delegated (BUILD_PLAN section 9.3). Operator can only do the finite set of registered things. |
| L-04 | **Retry storms.** The model, seeing an ambiguous failure, retries and duplicates a side effect. | No automatic retry anywhere; `UNKNOWN` is terminal; handles are single-use; `retryable: false` on every side-effect-adjacent error; the `DISPATCH_INDETERMINATE` message tells the model plainly not to retry ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)). Operator itself never infers that a timed-out dispatch was a non-event ([ADR-009](adr/ADR-009-dispatch-correlation.md)), and v2's governed retry recalculates rather than reusing an approval ([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md), invariant I11). |
| L-05 | **Argument drift under multi-turn pressure.** The model gradually alters arguments across turns after an approval. | Fingerprint binding: the approved arguments are the executed arguments or nothing runs (T-05). |
| L-06 | **Plausible-sounding justification.** The model supplies a persuasive `reason` to get a dangerous call through. | `reason` is displayed to the human and recorded in the audit log. It never affects policy — no gate reads it ([ADR-007](adr/ADR-007-deterministic-before-llm.md)). |
| L-07 | **Over-broad discovery.** The model finds a powerful workflow and uses it for an unrelated purpose. | Risk and side-effect classes are surfaced at discovery, and anything above `read_only` requires human approval per invocation. |
| L-08 | **Approval theatre.** The model reports that it "sent an approval link" and treats the operation as effectively approved, because it was handed a URL-shaped string it cannot reach and cannot act on. | Remote callers never receive a URL; they receive `approval_required: true`, the operation ID, and instructions naming the human action required (T-38, invariant I12). `approval_required` is a boolean to branch on rather than a presence-of-URL inference. |

---

## 7. Control-to-threat coverage

| Control (BUILD_PLAN 9.2) | Threats answered |
|---|---|
| B1 registry IDs only | T-01, T-02, L-01, L-03 |
| B2 schema validation | T-03 |
| B3 handles | T-04, T-05, T-06, L-05 |
| B4 out-of-band approval | T-07, T-08, L-01 |
| B5 no credentials in results | T-09 |
| B6 redaction and caps | T-30, T-31 |
| B7 server-owned secrets | T-22, T-23 |
| B8 drift checks | T-24, T-25 |
| B9 transport guards | T-33, T-34 |
| B10 loopback approval app | T-15, T-16, T-17, T-18 |
| B11 append-only audit | T-13, T-35 |
| B12 core argument limits | T-12 |
| B13 approval reachability | T-38, L-08 |
| ADR-005 no retries | L-04, T-27, T-37 |
| ADR-007 deterministic gates | L-06, L-01 |
| ADR-008 conservative canonicalization | T-39, and the coverage T-24/T-25 depend on |
| ADR-009 correlation and honest reporting | T-40, T-41, L-04 |
| ADR-010 approval delivery and lazy expiry | T-38, L-08 |
| ADR-011 argument limits and namespaces | T-12 |
| ADR-012 retry recalculation and anchoring | L-04, T-35 |
| B14 identity through validated bearer token only *(v2, stage 02)* | T-14, T-42, T-45 |
| B15 organization isolation *(v2, stage 02 — `whoami`; stage 03 — every v1 tool's `describe_workflow`/`list_workflows`/`prepare_operation`/etc.)* | T-46, T-50 |
| ADR-014 OIDC trust and session model *(v2, stage 02)* | T-14, T-42, T-43, T-44, T-48 |
| ADR-013 organization/tenant/principal model *(v2, stage 02)* | T-45, T-46 |
| ADR-006 secret indirection, extended to service-principal credentials *(v2, stage 02)* | T-47 |
| `core.authorization.evaluate` — the one RBAC evaluator *(v2, stage 03, ADR-015)* | T-14 (v2 half), T-49, T-50, T-52 |
| SQL-level scope filtering before pagination *(v2, stage 03)* | T-51 |
| Layering contract: no adapter role logic *(v2, stage 03)* | T-53 |
| ADR-006 no credential ever stored, extended to `workflow_definition_snapshots` *(v2, stage 07)* | T-60 |
| Salted per-call credential-id digesting in `diff_workflow_definition` *(v2, stage 07)* | T-61 |
| ADR-019 filter-before-aggregation in `get_metrics` *(v2, stage 08)* | T-62 |
| Per-subject-type scope filtering in `list_audit_events` *(v2, stage 08, ADR-012 section 3)* | T-63 |
| `anchor init-key`'s `0600` key file, outside the database *(v2, stage 09, ADR-012 section 2)* | T-64 |
| Ed25519 signature verification + independent-database-copy recomputation in `anchor verify` *(v2, stage 09)* | T-65 |

Every boundary control traces to at least one threat, and every Critical or High threat
traces to at least one control or an explicit acceptance in section 8.

---

## 8. Out of scope for v1

Stated plainly so no one mistakes silence for coverage:

1. **Compromise of the operator's machine (ADV-7).** Local code execution as the
   operator defeats every control here. Not mitigated, not mitigable at this layer.
2. **n8n's own security.** Operator trusts n8n to execute what its definition says.
   It does not sandbox, monitor, or constrain a workflow once dispatched.
3. **Downstream authorization.** Whether the CRM should have accepted the write is the
   CRM's decision. Operator governs invocation, not downstream policy.
4. **Encryption at rest.** The v1 database relies on filesystem permissions (T-36).
5. **Multi-tenant isolation.** v1 is single-user; there is no tenancy to isolate (T-14).
   **Stage 02 update:** v2 introduces real organizations, memberships, and OIDC identity
   resolution (T-14 mitigated, T-45/T-46 mitigated for the surface that exists today —
   `whoami`). What stage 02 explicitly does **not** add: no v1 or v2 tool yet accepts an
   `environment`/organization-scoping argument, so there is no per-tool authorization
   *enforcement* to bypass yet, and no role-capability evaluation exists at all (RBAC
   arrives stage 03, ADR-015). An authenticated v2 caller today can still reach every v1
   tool exactly as any v1 caller could — identity is resolved and recorded, but does not
   yet gate anything. Treat this as an explicit, temporary, load-bearing scoping decision
   for stage 02, not a silent gap: it closes in stage 03 (authorization intersection) and
   stage 04 (environment-scoped organization resolution for the other twelve tools).
   Also unresolved by identity alone: **deleted identity-provider account.** Operator has
   no IdP-side revocation signal — an account deleted at the IdP is indistinguishable,
   from Operator's side, from any other subject that simply stops presenting new tokens.
   A token issued before deletion remains cryptographically valid until its own natural
   `exp`. The actionable mitigation is the same one T-44 already covers: an admin
   proactively runs `identity disable-principal` the moment an IdP-side removal is known,
   which takes effect immediately (live re-check, no caching) regardless of the token's
   remaining lifetime. No separate test exists for "deleted account" as such, because
   there is no distinguishable signal for a test to assert on — the code path is
   identical to, and already covered by, T-44's disabled-principal tests.
   **Stage 03 update:** the gap this item named — "identity is resolved and recorded,
   but does not yet gate anything" — is closed. Every v1 tool now calls
   `core.authorization.evaluate` (T-49 through T-53), workflow-scope and role-capability
   are real and enforced, and `list_operations` filters by scope before pagination.
   What remains open, precisely: (a) **environment-scope enforcement against a real v1
   tool call** — the evaluator's environment-scope conjunct is fully implemented and
   property-tested, but no v1 tool carries an `environment` argument yet to check it
   against, so a call today is honored only when a grant's `environment_scope` is `*`
   (`core/authorization.py`'s own documented decision) — Stage 04's job to complete
   once the argument exists; (b) **granting a role to an existing service principal**
   — `identity add-membership` currently JIT-resolves a `user` principal via
   `--issuer`/`--subject` only, with no CLI path to grant a membership to a
   `service`-kind principal by ID (found while writing
   [LEAST_PRIVILEGE.md](LEAST_PRIVILEGE.md)'s worked examples, tracked there rather
   than worked around); (c) **"insufficient role" is untestable through the CLI's own
   identity** — the CLI always resolves to the fixed dev/service principal in v2 mode
   (ADR-014 section 5, extended stage 03), which `ensure_dev_principal` always grants
   `admin`, so every CLI-issued command is, by this stage's design, maximally
   privileged; the negative case is proven at the `core.service` level instead
   (`tests/integration/test_authorization_service.py`), a deliberate consequence of
   keeping local dev easy (stage 02's own stated goal), not an oversight.
6. **Approval-fatigue as a systemic risk (T-20).** Mitigated by page design and honest
   risk labeling, but a human who always clicks approve is not a solvable software problem.
7. **Availability.** Operator is not designed for high availability; an outage means
   workflows cannot be run through it, which is the safe failure direction. Preparation
   stays coupled to a successful live preflight, so an n8n outage blocks new work rather
   than queueing unverified work ([ADR-009](adr/ADR-009-dispatch-correlation.md)). Approval
   *correctness* depends on no process being up: expiry is applied lazily inside the
   transaction that acts on an operation (invariant I9,
   [ADR-010](adr/ADR-010-approval-delivery-and-expiry.md)).
8. **Credential validity.** Operator reports whether credentials are *bound*, never
   whether they work (T-41). Testing a credential means using it, which is the side effect
   preflight exists to gate.
9. **Reconciling an `UNKNOWN` without correlation data.** For a workflow that returns no
   execution identifier, deciding whether the side effect occurred is a human task against
   the downstream system (T-40).
10. **Automatic or CLI-driven resolution of a crash-stranded `EXECUTING` operation.**
    v1 detects nothing wrong (the operation simply never resolves) and provides no
    command to move it forward; an operator must confirm the outcome against n8n and
    edit the row directly (T-37, RR-10). See the
    [UNKNOWN reconciliation guide](RECONCILING_UNKNOWN.md), which also covers this
    case despite the operation technically sitting in `EXECUTING`, not `UNKNOWN`.

---

## 9. Residual risk register

| ID | Residual risk | Severity after v1 controls | Owner | Planned |
|---|---|---|---|---|
| RR-1 | Prompt injection can still *cause a request* for any registered workflow; a fatigued approver may pass it (T-20, T-29, L-01). | Medium | Operator | v2 team approvals and quorum (stage 05, ADR-017) raise the bar — a single fatigued approver can no longer unilaterally pass a `quorum_count > 1` workflow; the residual risk is now N fatigued approvers agreeing independently, not one. |
| RR-2 | A registered workflow may do more than its description claims (T-28). | Medium | Operator | v3 evaluation lab and governed change review. |
| RR-3 | Redaction completeness depends on operator-authored paths (T-30). | Medium | Operator | v2 default redaction heuristics with explicit opt-out. |
| RR-4 | Audit tampering is detectable, not preventable, even after v2 `AuditAnchor` (stage 09): a database-only attacker (T-35) is now caught by publishing chain state somewhere they don't control, but an attacker who additionally holds the host, the local signing key file or the webhook's own credential, *and* the anchor sink itself, defeats both implementations — explicitly out of scope for either (ADR-012 section 2's own scoping, see T-64/T-65). | Low (was Medium pre-anchoring) | Operator | Implemented (stage 09): signed local file (`audit_anchor/local_file.py`) and authenticated HTTPS webhook (`audit_anchor/webhook.py`), both behind the `AuditAnchor` interface. v3 KMS-backed signing, transparency-log submission, WORM storage — each raises the bar on the *same* three-way-compromise residual, never eliminates it entirely ([ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md)). |
| RR-5 | `UNKNOWN` outcomes require a human to reconcile downstream (BUILD_PLAN 9.5), and without correlation data there is nothing exact to reconcile against (T-40). | Low | Operator | Closed for the correlated case in stage 06: `retry_operation` (governed recalculation, never reusing the parent's approval) and `reconcile_operation` (verified exact-ID annotations, admin-only, CLI-only) are both implemented and tested (`tests/integration/test_retry_service.py`, `tests/unit/test_reconciliation.py`, `tests/integration/test_gtm_usability_stage06.py`). What remains: a workflow with no correlation ID at all still has nothing exact to reconcile against — an inherent limit of the workflow's own trigger configuration, not a gap in Operator's own machinery ([ADR-009](adr/ADR-009-dispatch-correlation.md), [ADR-012](adr/ADR-012-governed-retry-and-audit-anchoring.md)). |
| RR-6 | Data at rest is unencrypted, and operation arguments are stored **raw** (not redacted) since phase 7 — dispatch and execute-time fingerprint re-verification both need the real values, and a value redacted at rest can never be un-redacted for that check. Execution results *are* redacted before they are ever written (T-36). | Low–Medium | Operator | v3 enterprise controls; encryption at rest. |
| RR-7 | Rate limiting remains coarse (T-11). Argument-size caps are no longer coarse — T-12 is mitigated by B12. | Low | Engineering | v2 per-principal quotas. |
| RR-8 | Early canonicalization is deliberately over-inclusive, so cosmetic n8n edits produce false `DEFINITION_DRIFT` until the harness justifies exclusions. Friction on a security control invites routing around it (T-39, [ADR-008](adr/ADR-008-conservative-definition-canonicalization.md)). | Low–Medium | Engineering | Phase-4 harness narrows the allowlist on evidence; v2 `diff_workflow_definition` makes re-review a diff. |
| RR-9 | In a stdio-only deployment with no sweeper and no scheduled `operations expire`, `EXPIRED` audit events are written at next touch rather than at the deadline, and may never be written for an operation nobody touches again. Audit-timeline fidelity only; no expired operation is executable (invariant I9). | Low | Operator | Run `operations expire` on a timer, or the approval app. |
| RR-10 | An operation crash-stranded in `EXECUTING` (process killed between the handle burn and dispatch completing) has no automatic or CLI-driven resolution in v1 — it stays `EXECUTING` indefinitely, correctly inert but not resolved, and (since `max_concurrent` counts `EXECUTING` operations) permanently occupies one concurrency slot for that workflow until an operator manually confirms the outcome against n8n and updates the row directly (T-37). Narrower in practice than it sounds: the window is one process between two adjacent statements, not an extended period. | Low | Operator | v2: a supported reconciliation command instead of a direct database edit. |
| RR-11 | **Closed, stage 03.** Was: Stage 02 resolved and recorded real identity but enforced no authorization on it. Now: `core.authorization.evaluate` gates every v1 tool + the CLI's approve/reject/audit paths (T-49–T-53). Superseded by RR-13 for the one dimension still genuinely open (environment-scope). | — | — | Closed by this stage; see RR-13 for what remains. |
| RR-12 | A deleted identity-provider account has no distinguishing signal Operator can detect on its own; a token issued before deletion remains valid until its natural expiry unless an admin proactively disables the mapped principal. | Low | Operator | No code change planned — this is the same live-recheck mechanism T-44 already provides; the residual risk is the admin's IdP-deletion → `identity disable-principal` operational habit, not a software gap. |
| RR-13 | **Closed, stage 04.** Was: environment-scope authorization was fully implemented and property-tested (AC-39) but not reachable from any real v1 tool call. Now: every v1 tool + `whoami` + `list_environments` carries a real, resolved `environment`/`environment_id`, `_authorize`/`evaluate` are called with it throughout `core/service.py`, and (T-54) the evaluator's own environment-scope check gained the organization-ownership guard that reachability revealed was missing. | — | — | Closed by this stage. |
| RR-14 | No CLI path grants an organization membership to an existing `service`-kind principal by ID — `identity add-membership` JIT-resolves a `user` principal via `--issuer`/`--subject` only (section 8 item 5(b)). A service principal (a scheduled job, a webhook relay) therefore cannot be scoped narrower than whatever ad hoc workaround an operator improvises today. | Low–Medium | Engineering | No stage currently owns this explicitly; propose it as a small addition (a `--service-principal <id>` alternative to `--issuer`/`--subject` on `add-membership`) whenever service-principal authorization scoping is next touched. |
| RR-15 | "Insufficient role" cannot be exercised through the CLI's own identity, since the CLI always resolves to the fixed dev/service principal in v2 mode, which is always granted `admin` (section 8 item 5(c)). Proven instead at the `core.service` level. | Low | Operator/Engineering | Accepted as a deliberate consequence of "local dev stays easy" (stage 02's own goal); revisit only if a real deployment needs the CLI itself to run as a non-admin identity, which would need a different CLI identity mechanism than "always the fixed dev principal." |

---

## 10. Review triggers

Re-run this analysis when any of the following changes:

- a new MCP tool is added, or an existing tool gains an argument;
- the state machine gains a transition;
- a new trust boundary appears (multi-user in v2, workflow writes in v3);
- the registry gains a field that affects policy;
- any control in BUILD_PLAN section 9.2 is weakened or removed;
- a transport is added or a default bind address changes;
- **a field is added to the canonicalization exclusion allowlist** (CAN-02 — this is a
  reduction in drift-detection coverage and must be reviewed as one);
- an approval channel is added, or the rule deciding caller locality changes;
- an `AuditAnchor` implementation is added.
- a v1 or v2 tool gains a new organization-scoping concept beyond `environment`
  (RR-13/T-54, stage 04, are the precedent: a scope check that is pure and
  role/pattern-based needs an explicit ownership check the moment the thing it scopes
  belongs to an organization at all — "wildcard" must never silently mean "anywhere");
- the OIDC algorithm allowlist, clock-skew tolerance, or JWKS re-fetch/rate-limit
  policy changes (ADR-014);
- the ADR-015 role-capability matrix changes, a role or a tool is added or removed, or
  the workflow-scope/environment-scope combination rule changes (stage 03 —
  `tests/property/test_rbac_matrix.py` should already fail first, but this document's
  own T-49–T-53 entries need re-checking against the new matrix by hand too);
- a new identity provider becomes a supported reference configuration
  ([OIDC_SETUP.md](OIDC_SETUP.md)).

Phase 9 of the build plan requires a full review of this document against the shipped
v1 code before release, including re-confirmation of every accepted risk.
