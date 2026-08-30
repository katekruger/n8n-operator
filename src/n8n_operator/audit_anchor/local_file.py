"""Signed local anchor file (ADR-012 section 2) — append-only, outside the database,
each line signed with a key held outside the database too. Protects against the
realistic threat: an attacker who edits the SQLite/Postgres content but does not also
hold this file and its signing key (T-35/RR-4).

One JSON line per anchor: ``{covers_through_seq, entry_hash, entry_count, anchored_at,
signature, public_key}`` — no audit content, ever (ADR-012's own hard requirement;
enforced structurally, since a ``ChainAnchorLike`` has no field to leak one from).

Locking: ``fcntl.flock`` (POSIX-only — this codebase already assumes a Linux/macOS
deployment target for its Postgres/Docker harness; Windows is not a supported
deployment for this implementation). The lock is held for the read-last-line +
monotonicity-check + append + fsync sequence as one atomic unit, so two concurrent
``publish`` calls against the same file can never interleave writes or both believe
they are extending the same "last line."
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from n8n_operator.audit_anchor.base import ChainAnchorLike, sign_anchor, verify_signature
from n8n_operator.audit_anchor.keys import load_public_key, public_key_b64

__all__ = [
    "AnchorPublishRefusedError",
    "AnchorReceipt",
    "AnchorVerification",
    "FileVerificationReport",
    "LineIssue",
    "LocalFileAnchor",
]


class AnchorPublishRefusedError(Exception):
    """The new anchor's ``covers_through_seq`` does not strictly exceed the file's own
    last line — publishing it would regress the anchor file (a restored-from-backup or
    otherwise stale caller), which is refused rather than silently accepted."""


@dataclass(frozen=True)
class AnchorReceipt:
    implementation: str
    detail: dict[str, Any]
    signature: str
    public_key: str


@dataclass(frozen=True)
class AnchorVerification:
    ok: bool
    reason: str | None
    checked_through_seq: int | None


@dataclass(frozen=True)
class LineIssue:
    line_number: int
    reason: str


@dataclass(frozen=True)
class FileVerificationReport:
    """The whole-file audit result — every line's signature and monotonicity checked,
    a trailing unparseable line distinguished from tampering (a crash mid-append can
    only ever corrupt the *last* line; an unparseable or invalid interior line is
    evidence of tampering)."""

    ok: bool
    lines_checked: int
    issues: list[LineIssue] = field(default_factory=list)
    possible_interrupted_write: bool = False


class LocalFileAnchor:
    """Satisfies ``core.service.AuditAnchorPort`` structurally (via the composition
    root's adapter, exactly the ``notifications/`` package's own pattern)."""

    def __init__(self, *, path: Path, private_key: Ed25519PrivateKey) -> None:
        self._path = path
        self._private_key = private_key

    def publish(self, anchor: ChainAnchorLike) -> AnchorReceipt:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                last = self._last_line(fd)
                if last is not None and anchor.covers_through_seq <= last["covers_through_seq"]:
                    raise AnchorPublishRefusedError(
                        f"covers_through_seq {anchor.covers_through_seq} does not exceed "
                        f"the file's last anchored seq {last['covers_through_seq']}"
                    )
                signature = sign_anchor(self._private_key, anchor)
                signature_b64 = base64.b64encode(signature).decode("ascii")
                public_key = public_key_b64(self._private_key)
                line: dict[str, Any] = {
                    "covers_through_seq": anchor.covers_through_seq,
                    "entry_hash": anchor.entry_hash,
                    "entry_count": anchor.entry_count,
                    "anchored_at": anchor.anchored_at.astimezone(UTC).isoformat(),
                    "signature": signature_b64,
                    "public_key": public_key,
                }
                raw = (json.dumps(line, sort_keys=True) + "\n").encode("utf-8")
                os.write(fd, raw)
                os.fsync(fd)
                line_number = self._count_lines(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return AnchorReceipt(
            implementation="local_file",
            detail={"file_path": str(self._path), "line_number": line_number},
            signature=signature_b64,
            public_key=public_key,
        )

    def verify(self, anchor: ChainAnchorLike, receipt: AnchorReceipt) -> AnchorVerification:
        """Confirms one specific anchor's receipt: the signature verifies, and the
        named line in the file actually contains this exact anchor's fields (a
        receipt pointing at a line that was since altered fails here, distinct from
        the signature check alone — both must hold)."""
        try:
            public_key = load_public_key(receipt.public_key)
            signature = base64.b64decode(receipt.signature)
        except Exception as exc:
            return AnchorVerification(
                ok=False, reason=f"malformed receipt: {exc}", checked_through_seq=None
            )
        if not verify_signature(public_key, anchor, signature):
            return AnchorVerification(
                ok=False, reason="signature does not verify", checked_through_seq=None
            )
        line_number = receipt.detail.get("line_number")
        if not isinstance(line_number, int):
            return AnchorVerification(
                ok=False, reason="receipt has no line_number", checked_through_seq=None
            )
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if not (1 <= line_number <= len(lines)):
            return AnchorVerification(
                ok=False, reason="receipt's line_number is out of range", checked_through_seq=None
            )
        try:
            on_disk = json.loads(lines[line_number - 1])
        except json.JSONDecodeError:
            return AnchorVerification(
                ok=False, reason=f"line {line_number} is not valid JSON", checked_through_seq=None
            )
        if (
            on_disk.get("covers_through_seq") != anchor.covers_through_seq
            or on_disk.get("entry_hash") != anchor.entry_hash
        ):
            return AnchorVerification(
                ok=False,
                reason=f"line {line_number} no longer matches the anchored content",
                checked_through_seq=None,
            )
        return AnchorVerification(
            ok=True, reason=None, checked_through_seq=anchor.covers_through_seq
        )

    def verify_file(self) -> FileVerificationReport:
        """The whole-file audit: every line's signature verifies under *its own*
        embedded public key, ``covers_through_seq`` strictly increases line over line,
        and a trailing line that fails to parse is reported as a possible interrupted
        write (crash mid-append), never as tampering — only an *interior* line failing
        parse, signature, or monotonicity is tampering evidence."""
        if not self._path.exists():
            return FileVerificationReport(ok=True, lines_checked=0)
        raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        issues: list[LineIssue] = []
        possible_interrupted_write = False
        previous_seq: int | None = None
        for index, raw_line in enumerate(raw_lines, start=1):
            is_last = index == len(raw_lines)
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                if is_last:
                    possible_interrupted_write = True
                else:
                    issues.append(LineIssue(index, "unparseable line (not the last line)"))
                continue
            try:
                covers_through_seq = int(parsed["covers_through_seq"])
                public_key = load_public_key(parsed["public_key"])
                signature = base64.b64decode(parsed["signature"])
            except Exception as exc:
                issues.append(LineIssue(index, f"malformed line: {exc}"))
                continue
            anchor_like = _StaticAnchor(
                covers_through_seq=covers_through_seq,
                entry_hash=parsed["entry_hash"],
                entry_count=int(parsed["entry_count"]),
                anchored_at=_parse_iso(parsed["anchored_at"]),
            )
            if not verify_signature(public_key, anchor_like, signature):
                issues.append(LineIssue(index, "signature does not verify"))
                continue
            if previous_seq is not None and covers_through_seq <= previous_seq:
                issues.append(LineIssue(index, "covers_through_seq does not strictly increase"))
                continue
            previous_seq = covers_through_seq
        return FileVerificationReport(
            ok=not issues,
            lines_checked=len(raw_lines),
            issues=issues,
            possible_interrupted_write=possible_interrupted_write,
        )

    def _last_line(self, fd: int) -> dict[str, Any] | None:
        os.lseek(fd, 0, os.SEEK_SET)
        content = os.read(fd, os.fstat(fd).st_size).decode("utf-8")
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            parsed: dict[str, Any] = json.loads(lines[-1])
        except json.JSONDecodeError:
            # A trailing unparseable line is a possible interrupted write, not a hard
            # stop — treat "no confirmed last anchor" the same as an empty file, so a
            # fresh publish can still proceed and simply appends past it.
            return None
        return parsed

    def _count_lines(self, fd: int) -> int:
        os.lseek(fd, 0, os.SEEK_SET)
        content = os.read(fd, os.fstat(fd).st_size).decode("utf-8")
        return len([line for line in content.splitlines() if line.strip()])


@dataclass(frozen=True)
class _StaticAnchor:
    covers_through_seq: int
    entry_hash: str
    entry_count: int
    anchored_at: datetime


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
