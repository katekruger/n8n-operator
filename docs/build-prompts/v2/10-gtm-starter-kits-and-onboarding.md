# Stage 10 prompt — GTM starter kits and onboarding

Copy this entire file into a fresh Claude Code session after Stage 09 is merged.

## Mission

Make the sophisticated platform understandable and useful to real GTM engineers without
shipping vendor credentials, pretending example workflows are production-ready, or adding
new product scope.

## Required work

- Rewrite the first-use journey so a new operator reaches a safe, no-credentials demo in
  under five minutes and a local staging environment in under fifteen.
- Add sanitized, annotated registry/overlay starter kits for these common patterns:
  - read-only pipeline and campaign reporting;
  - lead/account enrichment;
  - CRM contact/account upsert;
  - lead routing or ownership update;
  - campaign audience sync;
  - customer or prospect communication requiring explicit approval;
  - data-quality check and exception report.
- For every starter, explain side-effect classification, required approval, recommended
  roles, environment differences, idempotency key, correlation expectation, redaction,
  argument limits, rollback/reconciliation, and what must be customized.
- Provide policy packs for a two-person startup GTM team, a scaling RevOps function, and a
  Series C organization with separate sales operations, marketing operations, security,
  and legal approvers. Policy packs are examples and must default deny.
- Add end-to-end recipes for Claude and OpenAI-compatible MCP clients using only the shipped
  twenty-tool surface. Include discovery, validation, preflight, approval routing, polling,
  execution, failure inspection, retry, diff review, and audit/metrics investigation.
- Add an operator onboarding guide, approver guide, troubleshooting decision tree, and
  “what this refuses to do” page. Use screenshots only if reproducible and sanitized;
  text-based examples must remain the source of truth.
- Improve demo fixtures and scripts so all examples can be tested without external SaaS.

## Presentation requirements

The README’s first screen must answer what it is, who it is for, why it is safer than raw
n8n access, current release status, and the fastest proof. Avoid inflated “AI platform”
language. Distinguish automated protocol evidence from real-client evidence and examples
from supported integrations.

## Completion gate

Contract-test every configuration and command in the docs. Run secret and placeholder
scans, link checks, a clean-machine onboarding rehearsal, and usability walkthroughs for
one startup and one Series C scenario. Return measured time-to-first-success, friction
found, corrections made, and Stage 11 entry criteria.
