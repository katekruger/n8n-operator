"""``audit_anchor.local_file.LocalFileAnchor`` (stage 09, ADR-012 section 2) —
property and tamper tests: publish/verify round trips, interior-line tampering is
always caught, a trailing corrupt line is distinguished from tampering (crash
mid-append), a non-increasing publish is refused, and concurrent publishers never
interleave writes into a corrupt line. Uses a fixed, deterministic test keypair —
never a freshly generated one — so a failure here is reproducible.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from n8n_operator.audit_anchor.local_file import AnchorPublishRefusedError, LocalFileAnchor

TEST_PRIVATE_KEY_B64 = "/HS6Tvlpf8WhdTRy1zxiU6PjcZu+ea8fZhjTlu2iywI="


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_PRIVATE_KEY_B64))


@dataclass(frozen=True)
class _Anchor:
    covers_through_seq: int
    entry_hash: str
    entry_count: int
    anchored_at: datetime


def _anchor(seq: int) -> _Anchor:
    return _Anchor(
        covers_through_seq=seq,
        entry_hash=f"sha256:{'a' * 60}{seq:04d}",
        entry_count=seq,
        anchored_at=datetime.now(UTC),
    )


@pytest.mark.integration
def test_publish_then_verify_round_trips(tmp_path: Path) -> None:
    sink = LocalFileAnchor(path=tmp_path / "anchors.jsonl", private_key=_private_key())
    anchor = _anchor(10)
    receipt = sink.publish(anchor)
    result = sink.verify(anchor, receipt)
    assert result.ok is True
    assert result.checked_through_seq == 10


@pytest.mark.integration
def test_publishing_several_anchors_all_verify(tmp_path: Path) -> None:
    sink = LocalFileAnchor(path=tmp_path / "anchors.jsonl", private_key=_private_key())
    for seq in (10, 20, 30, 40, 50):
        anchor = _anchor(seq)
        receipt = sink.publish(anchor)
        assert sink.verify(anchor, receipt).ok is True
    report = sink.verify_file()
    assert report.ok is True
    assert report.lines_checked == 5


@pytest.mark.integration
def test_publish_refuses_a_non_increasing_covers_through_seq(tmp_path: Path) -> None:
    sink = LocalFileAnchor(path=tmp_path / "anchors.jsonl", private_key=_private_key())
    sink.publish(_anchor(20))
    with pytest.raises(AnchorPublishRefusedError):
        sink.publish(_anchor(20))
    with pytest.raises(AnchorPublishRefusedError):
        sink.publish(_anchor(10))


@pytest.mark.integration
def test_verify_file_on_a_missing_file_is_trivially_ok(tmp_path: Path) -> None:
    sink = LocalFileAnchor(path=tmp_path / "does-not-exist.jsonl", private_key=_private_key())
    report = sink.verify_file()
    assert report.ok is True
    assert report.lines_checked == 0


@pytest.mark.integration
def test_verify_file_detects_a_tampered_interior_line(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    sink.publish(_anchor(10))
    sink.publish(_anchor(20))

    lines = path.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["entry_hash"] = "sha256:" + "f" * 64
    lines[0] = json.dumps(obj, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    report = sink.verify_file()
    assert report.ok is False
    assert report.issues
    assert report.issues[0].line_number == 1
    assert "signature" in report.issues[0].reason
    assert report.possible_interrupted_write is False


@pytest.mark.integration
def test_verify_file_detects_a_regressed_covers_through_seq(tmp_path: Path) -> None:
    """A line-by-line monotonicity violation that a tampered-in-place rewrite could
    introduce without breaking any single line's own signature (e.g. two valid,
    independently-signed lines swapped in order)."""
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    sink.publish(_anchor(10))
    sink.publish(_anchor(20))

    lines = path.read_text().splitlines()
    path.write_text(lines[1] + "\n" + lines[0] + "\n")  # swap: 20 then 10

    report = sink.verify_file()
    assert report.ok is False
    assert any("increase" in issue.reason for issue in report.issues)


@pytest.mark.integration
def test_verify_file_treats_a_trailing_corrupt_line_as_a_possible_interrupted_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    sink.publish(_anchor(10))
    sink.publish(_anchor(20))

    raw = path.read_bytes()
    path.write_bytes(raw[:-10])  # truncate mid-way through the last line

    report = sink.verify_file()
    assert report.possible_interrupted_write is True
    # The truncated trailing line is not counted as tampering evidence on its own.
    assert not any(issue.line_number == 2 for issue in report.issues)


@pytest.mark.integration
def test_verify_file_still_flags_a_truncated_interior_line_as_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    sink.publish(_anchor(10))
    sink.publish(_anchor(20))
    sink.publish(_anchor(30))

    lines = path.read_text().splitlines()
    lines[0] = lines[0][:10]  # corrupt the FIRST (interior) line, not the last
    path.write_text("\n".join(lines) + "\n")

    report = sink.verify_file()
    assert report.ok is False
    assert any(issue.line_number == 1 for issue in report.issues)


@pytest.mark.integration
def test_verify_rejects_a_receipt_whose_line_no_longer_matches(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    anchor = _anchor(10)
    receipt = sink.publish(anchor)

    lines = path.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["entry_hash"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(obj, sort_keys=True) + "\n")

    result = sink.verify(anchor, receipt)
    assert result.ok is False


@pytest.mark.integration
def test_concurrent_publishers_never_interleave_writes(tmp_path: Path) -> None:
    """Two threads racing to publish the *same* ``covers_through_seq`` (the
    "repeated anchor" edge case, forced concurrent) must never produce a corrupt or
    interleaved line — the ``fcntl.flock`` guard around the read-last-line +
    monotonicity-check + append sequence is what this proves. Exactly one of the two
    wins (appends a line); the other is correctly refused, since the second
    attempt's seq no longer strictly exceeds the file's own last line once the
    first has landed — this is the *same* single publisher racing itself (e.g. two
    overlapping cron ticks), not two distinct, out-of-order anchors, whose relative
    ordering a caller cannot control and which the lock makes no promise about."""
    path = tmp_path / "anchors.jsonl"
    sink = LocalFileAnchor(path=path, private_key=_private_key())
    outcomes: list[Exception | None] = [None, None]

    def _publish(index: int) -> None:
        try:
            sink.publish(_anchor(100))
        except Exception as exc:
            outcomes[index] = exc

    threads = [
        threading.Thread(target=_publish, args=(0,)),
        threading.Thread(target=_publish, args=(1,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report = sink.verify_file()
    assert report.ok is True
    assert report.lines_checked == 1  # exactly one write landed, never zero or two
    # Exactly one of the two calls was refused (raced the other) — never both
    # succeeding (a duplicate the lock should have prevented) and never both
    # failing (neither call actually got to publish).
    refusals = [o for o in outcomes if isinstance(o, AnchorPublishRefusedError)]
    assert len(refusals) == 1
