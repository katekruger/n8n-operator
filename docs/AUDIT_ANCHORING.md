# External audit anchoring

Stage 09 implements ADR-012 §2: publishing chain state somewhere an attacker with only
database write access does not control. This document is the operator-facing procedure
— key generation, permissions, rotation, backup/restore — and, most importantly, an
honest statement of what protection this actually buys you and what it does not.

## What this protects against, and what it does not

The audit log (`n8n-operator audit verify`) is tamper-*evident*: any row edited in place
breaks the hash chain and is detected. But an attacker with database write access can
rewrite **every** row, recomputing the whole chain from genesis — the chain verifies
perfectly, and nothing in the database itself reveals the tamper (T-35).

**External anchoring closes that gap by publishing `(covers_through_seq, entry_hash)`,
signed, somewhere outside the database** — a local file on a different filesystem path,
or a webhook to a different host under different credentials. An attacker who only
compromises the database can rewrite the chain, but cannot make the rewritten chain's
hash at the anchored sequence number match what was already signed and published
elsewhere.

**What this does *not* solve** — stated exactly, not implied: **an attacker who holds
the database, the host, the signing key (local file) or the webhook's own credential,
*and* the anchor sink itself defeats both implementations.** This is not a bug or an
oversight; ADR-012 names it explicitly as the honest scope of what a single-machine or
even a two-host anchoring scheme can achieve without external infrastructure (a KMS, a
transparency log, WORM storage — all v3, all raising the bar further without ever
reaching "unconditionally unforgeable"). If your threat model includes an attacker who
can plausibly reach both the database host and wherever the anchor file or webhook
credential lives, anchoring alone will not detect them. Consider a v3 mechanism, or an
anchor sink genuinely outside your own infrastructure (a third-party log service you do
not administer).

## Key generation

```bash
n8n-operator anchor init-key
```

Generates an Ed25519 keypair and writes the private key to
`N8N_OPERATOR_ANCHOR_SIGNING_KEY_PATH` (default `~/.n8n-operator/anchor_signing_key`)
with `0600` permissions — readable and writable only by the user running the command.
**The private key is never stored in the database** (ADR-012's own explicit
requirement) — back it up separately, and treat it with the same care as any other
credential: if it leaks, an attacker can forge anchors indistinguishable from real ones.

The command prints the **public key** — share this with anyone who needs to
independently verify your anchors (an auditor, a compliance reviewer). The public key
is not sensitive; it authenticates, it cannot forge.

`init-key` refuses to overwrite an existing key file. This is deliberate: rotating a
key is a decision an operator makes explicitly, not something that happens by re-running
a command.

## Rotation

There is no automated rotation command in this stage — rotating means:

1. Move or back up the existing key file (`mv ~/.n8n-operator/anchor_signing_key
   ~/.n8n-operator/anchor_signing_key.$(date +%Y%m%d)`).
2. Run `n8n-operator anchor init-key` again to generate a new key.
3. **Anchors published under the old key remain verifiable under the old public
   key** — a verifier needs to know which public key was in effect when a given anchor
   was published (each anchor's own receipt carries the public key that signed it, so
   this is self-describing; you do not need to separately track "which key was active
   when").

## Publishing

```bash
n8n-operator anchor publish [--implementation local_file|https_webhook]
```

Publishes the current chain tip as a new anchor. **Idempotent**: if nothing has changed
since the last successful anchor, this is a no-op (exit 0, "Nothing to anchor yet" or an
unchanged tip is silently skipped without a duplicate publish). Safe to run on any
schedule.

**No in-process scheduler exists** — this is a deliberate, consistent choice across this
whole codebase (`operations expire`, `notifications check-alerts` are the same shape).
Wire it to cron or a systemd timer:

```
# /etc/cron.d/n8n-operator-anchor
*/15 * * * * operator n8n-operator anchor publish >> /var/log/n8n-operator-anchor.log 2>&1
```

A failed publish is visible three independent ways: the command's own exit code (`1`),
a new `audit_anchors` row with `publish_failed=true` (queryable via `anchor status`),
and a structured log line (`anchor_publish_failed`). None of these depend on the others
— a monitoring setup watching only logs, only exit codes, or only the database will
still catch a failure.

## Verification

```bash
n8n-operator anchor verify                                    # self-check, live database
n8n-operator anchor verify --database-url <copy-url>           # independent copy
```

Without `--database-url`, this checks the latest anchor's signature against your own
live database — useful as a sanity check, but it cannot detect a tamper of the *whole*
database (an attacker who rewrites everything, including in a way consistent with the
anchor they also control, defeats a self-check by definition — this is exactly the
scope ADR-012 draws).

**The real value is `--database-url` against a genuinely independent copy** — a restore
from backup, a replica, anything not reachable by whoever might have tampered with the
primary. This connects read-only, recomputes the chain from genesis through the
anchored sequence number, and confirms it matches — mutating neither the primary nor
the copy.

## Backup and restore

The anchor file (`<signing-key-path>.anchors.jsonl`) and the signing key are both
plain files — back them up with your normal filesystem backup process. There is nothing
anchor-specific about backing them up; the append-only JSON-lines format is
human-readable and diff-friendly if you want to inspect backup history directly.

**Restoring the database from an older backup** is exactly the case
`anchor publish`'s monotonicity check exists for: if the restored database's chain tip
is *behind* what the anchor file already recorded, the next `anchor publish` attempt is
refused (never silently regresses the anchor file to match the stale database) — you'll
see this as an error, which is the correct, safe behavior: it means the anchor file
still reflects a more complete history than what you just restored, and something needs
investigating before publishing continues.

## Status

```bash
n8n-operator anchor status [--json]
```

One line per implementation that has ever published: the last anchored sequence
number, the live chain's current tip, how many entries have accumulated since the last
anchor (a growing number with no fresh anchor means the schedule isn't running, not
that anything is wrong with the chain itself), and whether the last attempt failed.
