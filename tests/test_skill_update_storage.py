from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from app.skill_updates.storage import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    CandidateManifestEntry,
    CandidateStorage,
    InMemoryCandidateStorage,
)


def _candidate(source_type: str, learning: str = "Ask one focused follow-up.") -> str:
    return (
        "---\n"
        "candidate_id: 1042\n"
        "candidate_type: customer_skill\n"
        f"source_conversation_type: {source_type}\n"
        "target_skill_key: null\n"
        'evidence_refs: ["ev_opaque_1"]\n'
        "---\n\n"
        "## Learning\n\n"
        f"{learning}\n\n"
        "## Regression cases\n\n"
        "- Ask one clear question.\n"
    )


@pytest.fixture
def storage(tmp_path: Path) -> CandidateStorage:
    root = tmp_path / "skill-update-store"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return CandidateStorage(root, allow_ephemeral_for_tests=True)


def test_candidate_write_read_hash_and_reconcile(storage: CandidateStorage) -> None:
    content = _candidate("buyer")
    entry = storage.write_candidate("buyer", 7, content)

    assert entry.relative_path == "candidates/buyer/buyer_conversation_skill_7.md"
    assert len(entry.sha256) == 64
    path = storage.root / entry.relative_path
    assert path.stat().st_mode & 0o777 == 0o600
    assert storage.read_candidate(entry) == content
    assert storage.reconcile_candidate("buyer", 7, None) == entry
    assert storage.reconcile_candidate("buyer", 7, entry.sha256) == entry
    with pytest.raises(ArtifactIntegrityError):
        storage.reconcile_candidate("buyer", 7, "0" * 64)


def test_candidate_sequence_path_is_allowlisted_and_never_overwritten(
    storage: CandidateStorage,
) -> None:
    first = storage.write_candidate("buyer", 1, _candidate("buyer"))
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("buyer", 1, _candidate("buyer", "Different learning."))
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("../outside", 2, _candidate("buyer"))
    with pytest.raises(ArtifactStorageError):
        storage.read_candidate(
            CandidateManifestEntry("buyer", 1, "../../outside.md", first.sha256)
        )


def test_manifest_cleanup_verifies_every_hash_and_leaves_unlisted_files(
    storage: CandidateStorage,
) -> None:
    first = storage.write_candidate("seller", 1, _candidate("seller"))
    second = storage.write_candidate("seller", 2, _candidate("seller", "Clarify timing."))
    unlisted = storage.write_candidate("seller", 3, _candidate("seller", "Ask about location."))
    second_path = storage.root / second.relative_path
    second_path.write_text("tampered", encoding="utf-8")
    os.chmod(second_path, 0o600)

    with pytest.raises(ArtifactIntegrityError):
        storage.delete_manifest([first, second])
    assert (storage.root / first.relative_path).exists()

    second_path.write_text(_candidate("seller", "Clarify timing."), encoding="utf-8")
    os.chmod(second_path, 0o600)
    removed = storage.delete_manifest([first, second])
    assert set(removed) == {first.relative_path, second.relative_path}
    assert not (storage.root / first.relative_path).exists()
    assert not second_path.exists()
    assert (storage.root / unlisted.relative_path).is_file()
    assert storage.delete_manifest([first, second]) == []


def test_candidate_cleanup_rejects_duplicate_and_bad_hash_entries(
    storage: CandidateStorage,
) -> None:
    entry = storage.write_candidate("general", 1, _candidate("general"))
    with pytest.raises(ArtifactStorageError):
        storage.delete_manifest([entry, entry])
    with pytest.raises(ArtifactStorageError):
        storage.delete_manifest(
            [CandidateManifestEntry("general", 1, entry.relative_path, "not-a-hash")]
        )
    assert storage.read_candidate(entry) == _candidate("general")


def test_candidate_symlink_is_rejected(storage: CandidateStorage, tmp_path: Path) -> None:
    entry = storage.write_candidate("portfolio", 1, _candidate("portfolio"))
    path = storage.root / entry.relative_path
    target = tmp_path / "outside.md"
    target.write_text(_candidate("portfolio"), encoding="utf-8")
    os.chmod(target, 0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ArtifactStorageError):
        storage.read_candidate(entry)
    with pytest.raises(ArtifactStorageError):
        storage.delete_manifest([entry])


@pytest.mark.parametrize(
    "learning",
    [
        "Customer: please call me at +971 50 123 4567.",
        "Email me at person@example.test.",
        "See https://example.test/listing.",
    ],
)
def test_candidate_rejects_identifiers_and_transcript_content(
    storage: CandidateStorage, learning: str
) -> None:
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("buyer", 1, _candidate("buyer", learning))
    assert not (storage.root / "candidates/buyer/buyer_conversation_skill_1.md").exists()


def test_candidate_rejects_invalid_evidence_and_wrong_namespace(storage: CandidateStorage) -> None:
    invalid_evidence = _candidate("buyer").replace("ev_opaque_1", "Jane Doe")
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("buyer", 1, invalid_evidence)
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("seller", 1, _candidate("buyer"))


def test_work_artifacts_are_named_hashed_and_removed_individually(
    storage: CandidateStorage,
) -> None:
    artifact = storage.write_work_artifact("batch-123-part-1.md", "temporary consolidation")
    assert artifact.relative_path == "work/batch-123-part-1.md"
    assert storage.read_work_artifact(
        "batch-123-part-1.md", artifact.sha256
    ) == b"temporary consolidation"
    with pytest.raises(ArtifactStorageError):
        storage.write_work_artifact("../outside.md", "bad")
    with pytest.raises(ArtifactIntegrityError):
        storage.delete_work_artifact("batch-123-part-1.md", "0" * 64)
    storage.delete_work_artifact("batch-123-part-1.md", artifact.sha256)
    with pytest.raises(ArtifactStorageError):
        storage.read_work_artifact("batch-123-part-1.md")


def test_in_memory_fake_uses_same_candidate_policy() -> None:
    storage = InMemoryCandidateStorage()
    content = _candidate("unknown")
    entry = storage.write_candidate("unknown", 2, content)
    assert storage.read_candidate(entry) == content
    assert storage.delete_manifest([entry]) == [entry.relative_path]
    with pytest.raises(ArtifactStorageError):
        storage.write_candidate("unknown", 2, _candidate("unknown", "User: raw transcript."))


def test_storage_root_rejects_repository_and_temp_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[1]
    with pytest.raises(ArtifactStorageError):
        CandidateStorage(repo_root)
    with pytest.raises(ArtifactStorageError):
        CandidateStorage(tmp_path)


def test_manifest_hash_is_sha256_of_exact_utf8_candidate(storage: CandidateStorage) -> None:
    content = _candidate("buyer")
    entry = storage.write_candidate("buyer", 5, content)
    assert entry.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
