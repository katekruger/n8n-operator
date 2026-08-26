# n8n Operator — Threat Model

> Scope: v1 as specified in [BUILD_PLAN.md](BUILD_PLAN.md). Trust zones and boundary
> controls (B1–B11) are normative there in section 9; this document derives the threats
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
| T-12 | Denial of service | Caller exhausts disk by preparing operations with huge argument payloads. | Medium | Tool argument size cap at the transport; results capped by `output.max_bytes`. Argument caps are transport-level in v1. | partial |
| T-13 | Repudiation | Caller denies having requested an action. | High | Every transition writes an `operation_events` row and a hash-chained `audit_log` row in the same transaction (I6). B11. | mitigated |
| T-14 | Spoofing | Caller claims to be a different principal. | High | v1 has exactly one local principal; there is nothing to spoof. Becomes real in v2 with OIDC. | accepted (v1 by design) |

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

### 5.3 TB3 — Operator to n8n

| ID | STRIDE | Threat | Severity | Mitigation | v1 |
|---|---|---|---|---|---|
| T-22 | Information disclosure | Credentials are committed to source control in the registry file. | Critical | `secret_ref` must be an indirect reference; a literal secret is a load-time error (rule R6, [ADR-006](adr/ADR-006-server-owned-n8n-credentials.md)). | mitigated |
| T-23 | Information disclosure | Credentials leak into logs or the database. | High | Secrets held in memory only; structured logging scrubs configured secret values; no secret column exists in any table. B7. | mitigated |
| T-24 | Tampering | The workflow is modified in n8n between registration and use. | Critical | `definition_hash` checked at preflight; `BLOCKED` on drift (AC-06). | mitigated |
| T-25 | Tampering | The workflow is modified between **approval** and **execution** — the TOCTOU window that matters. | Critical | Hash re-checked at execute; `DEFINITION_DRIFT` and nothing is dispatched (B8, AC-13). | mitigated |
| T-26 | Spoofing | Operator is pointed at an attacker-controlled n8n instance. | High | Instance URL comes from validated config, never from a tool argument or the registry; TLS verification is not disableable. | mitigated |
| T-27 | Denial of service | A hung n8n exhausts Operator's connections. | Medium | Explicit connect and read timeouts on every request; no unbounded waits; no retries (ADR-005). | mitigated |
| T-28 | Elevation | A registered workflow does more than its description claims — a benign title over a destructive graph. | Critical | `definition_hash` pins *what was reviewed*. Operator cannot read intent from a node graph; the operator must review before registering. v3 evaluation lab narrows this. | partial |

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
| T-35 | Tampering | ADV-6 edits the SQLite file to erase or fabricate audit records. | High | Hash-chained audit log makes edits detectable via `audit verify` (AC-22). Detection, not prevention. | partial |
| T-36 | Information disclosure | ADV-6 reads PII from the database at rest. | Medium | Arguments and results are stored redacted; no secrets are stored. Filesystem permissions are the operator's responsibility; v1 adds no encryption at rest. | accepted |
| T-37 | Tampering | An operation is left stranded in `EXECUTING` by a crash, and later resolved wrongly. | Medium | `EXECUTING` commits before dispatch; recovery resolves stranded operations to `UNKNOWN`, never to `SUCCEEDED` or a retry. | mitigated |

---

## 6. LLM-specific threats

STRIDE assumes an adversary with intent. The characteristic failure here is an agent
with no intent at all, doing the wrong thing confidently.

| ID | Threat | Why the architecture answers it |
|---|---|---|
| L-01 | **Indirect prompt injection.** The model reads hostile content and is steered into calling a destructive workflow. | The model can only reach registered workflows (T-01), only with schema-valid arguments (T-03), and — for anything with side effects — only after a human outside the model's channel approved *these* arguments (T-07, T-19). Injection can produce a *request*; it cannot produce an *approval*. |
| L-02 | **Tool-description poisoning.** Hostile text in a workflow title or description manipulates the model. | Titles and descriptions are operator-authored in the registry, never fetched from n8n at runtime. The registry is the only source of tool-adjacent text. |
| L-03 | **Confused deputy.** The model borrows Operator's credentials for something it should not do. | Authority is enumerated, not delegated (BUILD_PLAN section 9.3). Operator can only do the finite set of registered things. |
| L-04 | **Retry storms.** The model, seeing an ambiguous failure, retries and duplicates a side effect. | No automatic retry anywhere; `UNKNOWN` is terminal; handles are single-use; `retryable: false` on every side-effect-adjacent error; the `DISPATCH_INDETERMINATE` message tells the model plainly not to retry ([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)). |
| L-05 | **Argument drift under multi-turn pressure.** The model gradually alters arguments across turns after an approval. | Fingerprint binding: the approved arguments are the executed arguments or nothing runs (T-05). |
| L-06 | **Plausible-sounding justification.** The model supplies a persuasive `reason` to get a dangerous call through. | `reason` is displayed to the human and recorded in the audit log. It never affects policy — no gate reads it ([ADR-007](adr/ADR-007-deterministic-before-llm.md)). |
| L-07 | **Over-broad discovery.** The model finds a powerful workflow and uses it for an unrelated purpose. | Risk and side-effect classes are surfaced at discovery, and anything above `read_only` requires human approval per invocation. |

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
| ADR-005 no retries | L-04, T-27, T-37 |
| ADR-007 deterministic gates | L-06, L-01 |

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
6. **Approval-fatigue as a systemic risk (T-20).** Mitigated by page design and honest
   risk labeling, but a human who always clicks approve is not a solvable software problem.
7. **Availability.** Operator is not designed for high availability; an outage means
   workflows cannot be run through it, which is the safe failure direction.

---

## 9. Residual risk register

| ID | Residual risk | Severity after v1 controls | Owner | Planned |
|---|---|---|---|---|
| RR-1 | Prompt injection can still *cause a request* for any registered workflow; a fatigued approver may pass it (T-20, T-29, L-01). | Medium | Operator | v2 team approvals and quorum raise the bar. |
| RR-2 | A registered workflow may do more than its description claims (T-28). | Medium | Operator | v3 evaluation lab and governed change review. |
| RR-3 | Redaction completeness depends on operator-authored paths (T-30). | Medium | Operator | v2 default redaction heuristics with explicit opt-out. |
| RR-4 | Audit tampering is detectable, not preventable (T-35). | Medium | Operator | v2 external anchoring; v3 signed evidence packs. |
| RR-5 | `UNKNOWN` outcomes require a human to reconcile downstream (BUILD_PLAN 9.5). | Medium | Operator | v2 governed retry with explicit reconciliation. |
| RR-6 | Data at rest is unencrypted (T-36). | Low–Medium | Operator | v3 enterprise controls. |
| RR-7 | Rate limiting and argument-size caps are coarse (T-11, T-12). | Low | Engineering | v2 per-principal quotas. |

---

## 10. Review triggers

Re-run this analysis when any of the following changes:

- a new MCP tool is added, or an existing tool gains an argument;
- the state machine gains a transition;
- a new trust boundary appears (multi-user in v2, workflow writes in v3);
- the registry gains a field that affects policy;
- any control in BUILD_PLAN section 9.2 is weakened or removed;
- a transport is added or a default bind address changes.

Phase 9 of the build plan requires a full review of this document against the shipped
v1 code before release, including re-confirmation of every accepted risk.
