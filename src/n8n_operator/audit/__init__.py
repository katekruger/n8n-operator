"""The append-only, hash-chained audit log.

Every state transition and every decision is a record. No code path updates or deletes
an audit row, in any version — a contract test greps for one (boundary B11).

This is tamper-evidence, not tamper-proofing: an attacker with write access to the
database can rewrite the whole chain (BUILD_PLAN section 9.4, residual risk RR-4).
"""
