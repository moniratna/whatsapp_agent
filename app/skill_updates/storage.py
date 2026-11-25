from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.config.settings import Settings

CUSTOMER_SOURCE_TYPES = frozenset({"buyer", "seller", "portfolio", "general", "unknown"})
ALL_SOURCE_TYPES = CUSTOMER_SOURCE_TYPES | {"reviewer"}
_SAFE_WORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()./-]{7,}\d(?!\w)")
_TRANSCRIPT_LINE = re.compile(r"^\s*(?:>\s*)*(?:customer|assistant|agent|admin|user)\s*:", re.I)
_TRANSCRIPT_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:raw\s+)?(?:chat\s+)?transcript\b", re.I | re.M
)


class ArtifactStorageError(RuntimeError):
    """Base error for unsafe or unavailable skill update artifacts."""


class ArtifactIntegrityError(ArtifactStorageError):
    """An artifact does not match its recorded hash or expected format."""


@dataclass(frozen=True)
class CandidateManifestEntry:
    source_type: str
    sequence: int
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    sha256: str
    size_bytes: int


class CandidateStoragePort(Protocol):
    def write_candidate(
        self, source_type: str, sequence: int, content: str
    ) -> CandidateManifestEntry: ...

    def read_candidate(self, entry: CandidateManifestEntry) -> str: ...

    def delete_manifest(self, entries: list[CandidateManifestEntry]) -> list[str]: ...


def _candidate_relative_path(source_type: str, sequence: int) -> str:
    if source_type not in ALL_SOURCE_TYPES:
        raise ArtifactStorageError("unsupported candidate source type")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ArtifactStorageError("candidate sequence must be a positive integer")
    if source_type == "reviewer":
        filename = f"skill_reviewer_{sequence}.md"
    else:
        filename = f"{source_type}_conversation_skill_{sequence}.md"
    return f"candidates/{source_type}/{filename}"


def _validate_candidate_markdown(content: str, source_type: str, max_bytes: int) -> bytes:
    if not isinstance(content, str):
        raise ArtifactStorageError("candidate content must be Markdown text")
    try:
        body_bytes = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ArtifactStorageError("candidate content must be valid UTF-8") from exc
    if not body_bytes or len(body_bytes) > max_bytes:
        raise ArtifactStorageError("candidate content is empty or exceeds its byte limit")
    if "\x00" in content:
        raise ArtifactStorageError("candidate content contains a NUL byte")

    match = re.match(r"\A---\s*\n(?P<header>.*?)\n---\s*\n(?P<body>.*)\Z", content, re.S)
    if match is None:
        raise ArtifactStorageError("candidate Markdown must begin with front matter")
    header: dict[str, str] = {}
    for line in match.group("header").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise ArtifactStorageError("candidate front matter contains an invalid line")
        normalized_key = key.strip()
        if normalized_key in header:
            raise ArtifactStorageError("candidate front matter contains a duplicate field")
        header[normalized_key] = value.strip().strip("\"'")
    expected_kind = "reviewer_skill" if source_type == "reviewer" else "customer_skill"
    if header.get("candidate_type") != expected_kind:
        raise ArtifactStorageError("candidate type does not match its storage namespace")
    if source_type == "reviewer":
        if header.get("target_skill_key") != "skill_reviewer":
            raise ArtifactStorageError("reviewer candidates must target skill_reviewer")
        if "## Proposed update" not in match.group("body") and "## Learning" not in match.group(
            "body"
        ):
            raise ArtifactStorageError("reviewer candidate must contain a proposed update")
    else:
        if header.get("source_conversation_type") != source_type:
            raise ArtifactStorageError("candidate source type does not match its storage namespace")
        if "## Learning" not in match.group("body"):
            raise ArtifactStorageError("customer candidate must contain a Learning section")

    evidence_line = next(
        (
            line.partition(":")[2].strip()
            for line in match.group("header").splitlines()
            if line.partition(":")[0].strip() == "evidence_refs"
        ),
        None,
    )
    if (
        evidence_line is None
        or not evidence_line.startswith("[")
        or not evidence_line.endswith("]")
    ):
        raise ArtifactStorageError("candidate evidence_refs must be a list of opaque references")
    refs = re.findall(r"[\"'](ev_[A-Za-z0-9_-]{1,80})[\"']", evidence_line)
    residue = re.sub(r"[\"']ev_[A-Za-z0-9_-]{1,80}[\"']", "", evidence_line)
    if not refs or residue.strip().strip("[] ,"):
        raise ArtifactStorageError("candidate evidence_refs must contain only opaque evidence IDs")

    markdown_body = match.group("body")
    # IDs, hashes, and opaque refs in front matter are not personal data and
    # can resemble phone numbers. Scan human-authored learning text only.
    if _EMAIL.search(markdown_body) or _URL.search(markdown_body) or _PHONE.search(markdown_body):
        raise ArtifactStorageError("candidate contains a direct personal identifier")
    if _TRANSCRIPT_HEADING.search(markdown_body) or any(
        _TRANSCRIPT_LINE.match(line) for line in markdown_body.splitlines()
    ):
        raise ArtifactStorageError("raw transcript content is not allowed in candidate artifacts")
    return body_bytes


class CandidateStorage:
    """Constrained filesystem adapter for inactive candidate and work artifacts.

    The storage root must already be mounted, outside the repository and temp
    directories, and private to the service account. Public methods accept
    source types, sequences, or safe work names; callers cannot supply arbitrary
    filesystem paths.
    """

    def __init__(
        self,
        storage_root: Path,
        *,
        max_candidate_bytes: int = 32_768,
        max_work_bytes: int = 1_048_576,
        allow_ephemeral_for_tests: bool = False,
    ) -> None:
        root = Path(storage_root).expanduser()
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ArtifactStorageError("storage root must be an existing absolute directory")
        self.root = root.resolve(strict=True)
        repo_root = Path(__file__).resolve().parents[2]
        temp_roots = {
            Path(tempfile.gettempdir()).resolve(),
            Path("/tmp").resolve(),
            Path("/var/tmp").resolve(),
            Path("/private/tmp").resolve(),
        }
        if self.root == repo_root or repo_root in self.root.parents:
            raise ArtifactStorageError("storage root must be outside the application repository")
        if not allow_ephemeral_for_tests and any(
            self.root == item or item in self.root.parents for item in temp_roots
        ):
            raise ArtifactStorageError("storage root must not use temporary storage")
        if self.root.stat().st_mode & 0o077:
            raise ArtifactStorageError("storage root permissions must be restricted to its owner")
        if not os.access(self.root, os.W_OK | os.X_OK):
            raise ArtifactStorageError("storage root is not writable by the application")
        if max_candidate_bytes <= 0 or max_work_bytes <= 0:
            raise ValueError("artifact size limits must be positive")
        self.max_candidate_bytes = max_candidate_bytes
        self.max_work_bytes = max_work_bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> CandidateStorage:
        if settings.skill_update_storage_dir is None:
            raise ArtifactStorageError("SKILL_UPDATE_STORAGE_DIR is not configured")
        return cls(
            settings.skill_update_storage_dir,
            max_candidate_bytes=settings.skill_update_candidate_max_bytes,
            max_work_bytes=max(
                settings.skill_update_consolidated_max_bytes,
                settings.skill_update_snapshot_max_bytes,
            ),
        )

    def write_candidate(
        self, source_type: str, sequence: int, content: str
    ) -> CandidateManifestEntry:
        relative = _candidate_relative_path(source_type, sequence)
        payload = _validate_candidate_markdown(content, source_type, self.max_candidate_bytes)
        path = self._path_for(relative, create_parents=True)
        digest = self._write_atomic(path, payload, replace=False)
        return CandidateManifestEntry(source_type, sequence, relative, digest)

    def read_candidate(self, entry: CandidateManifestEntry) -> str:
        path = self._validate_entry_path(entry)
        payload = self._read_file(path, max_bytes=self.max_candidate_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != entry.sha256:
            raise ArtifactIntegrityError("candidate hash does not match its manifest")
        _validate_candidate_markdown(
            payload.decode("utf-8"), entry.source_type, self.max_candidate_bytes
        )
        return payload.decode("utf-8")

    def reconcile_candidate(
        self,
        source_type: str,
        sequence: int,
        expected_sha256: str | None,
    ) -> CandidateManifestEntry | None:
        """Recover a candidate file written before its DB hash/status commit."""
        relative = _candidate_relative_path(source_type, sequence)
        path = self._path_for(relative)
        if not path.exists():
            return None
        payload = self._read_file(path, max_bytes=self.max_candidate_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ArtifactIntegrityError("candidate hash differs from its database record")
        _validate_candidate_markdown(payload.decode("utf-8"), source_type, self.max_candidate_bytes)
        return CandidateManifestEntry(source_type, sequence, relative, digest)

    def delete_manifest(self, entries: list[CandidateManifestEntry]) -> list[str]:
        """Delete only files named by a hash-verified manifest; missing files are idempotent."""
        if not entries:
            raise ArtifactStorageError("candidate cleanup requires an explicit non-empty manifest")
        paths: list[tuple[CandidateManifestEntry, Path]] = []
        seen: set[str] = set()
        for entry in entries:
            path = self._validate_entry_path(entry)
            if entry.relative_path in seen:
                raise ArtifactStorageError("candidate manifest contains a duplicate path")
            seen.add(entry.relative_path)
            if path.exists():
                payload = self._read_file(path, max_bytes=self.max_candidate_bytes)
                if hashlib.sha256(payload).hexdigest() != entry.sha256:
                    raise ArtifactIntegrityError("candidate hash does not match its manifest")
            paths.append((entry, path))

        removed: list[str] = []
        for entry, path in paths:
            if path.exists():
                path.unlink()
                self._fsync_directory(path.parent)
                removed.append(entry.relative_path)
        return removed

    def write_work_artifact(self, name: str, content: str | bytes) -> StoredArtifact:
        if not isinstance(name, str) or not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("work artifact name is invalid")
        if not isinstance(content, (str, bytes)):
            raise ArtifactStorageError("work artifact content must be text or bytes")
        payload = content.encode("utf-8") if isinstance(content, str) else content
        if not payload or len(payload) > self.max_work_bytes:
            raise ArtifactStorageError("work artifact is empty or exceeds its byte limit")
        if b"\x00" in payload:
            raise ArtifactStorageError("work artifact contains a NUL byte")
        relative = f"work/{name}"
        path = self._path_for(relative, create_parents=True)
        digest = self._write_atomic(path, payload, replace=True)
        return StoredArtifact(relative, digest, len(payload))

    def read_work_artifact(self, name: str, expected_sha256: str | None = None) -> bytes:
        if not isinstance(name, str) or not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("work artifact name is invalid")
        path = self._path_for(f"work/{name}")
        payload = self._read_file(path, max_bytes=self.max_work_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ArtifactIntegrityError("work artifact hash does not match")
        return payload

    def delete_work_artifact(self, name: str, expected_sha256: str) -> None:
        if not isinstance(name, str) or not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("work artifact name is invalid")
        if not _SHA256.fullmatch(expected_sha256):
            raise ArtifactStorageError("work artifact deletion requires a SHA-256 hash")
        path = self._path_for(f"work/{name}")
        if not path.exists():
            return
        payload = self._read_file(path, max_bytes=self.max_work_bytes)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ArtifactIntegrityError("work artifact hash does not match")
        path.unlink()
        self._fsync_directory(path.parent)

    def write_backup_artifact(self, name: str, content: str | bytes) -> StoredArtifact:
        if not isinstance(name, str) or not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("backup artifact name is invalid")
        if not isinstance(content, (str, bytes)):
            raise ArtifactStorageError("backup artifact content must be text or bytes")
        payload = content.encode("utf-8") if isinstance(content, str) else content
        if not payload or len(payload) > self.max_work_bytes or b"\x00" in payload:
            raise ArtifactStorageError("backup artifact is invalid or exceeds its byte limit")
        relative = f"backups/{name}"
        path = self._path_for(relative, create_parents=True)
        if path.exists():
            existing = self._read_file(path, max_bytes=self.max_work_bytes)
            if existing != payload:
                raise ArtifactIntegrityError(
                    "backup artifact already exists with different content"
                )
            return StoredArtifact(relative, hashlib.sha256(existing).hexdigest(), len(existing))
        digest = self._write_atomic(path, payload, replace=False)
        return StoredArtifact(relative, digest, len(payload))

    def read_backup_artifact(self, name: str, expected_sha256: str) -> bytes:
        if not isinstance(name, str) or not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("backup artifact name is invalid")
        if not _SHA256.fullmatch(expected_sha256):
            raise ArtifactStorageError("backup artifact read requires a SHA-256 hash")
        path = self._path_for(f"backups/{name}")
        payload = self._read_file(path, max_bytes=self.max_work_bytes)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ArtifactIntegrityError("backup artifact hash does not match")
        return payload

    def _validate_entry_path(self, entry: CandidateManifestEntry) -> Path:
        if (
            not isinstance(entry.sequence, int)
            or isinstance(entry.sequence, bool)
            or entry.sequence < 1
        ):
            raise ArtifactStorageError("candidate sequence must be a positive integer")
        expected = _candidate_relative_path(entry.source_type, entry.sequence)
        if (
            entry.relative_path != expected
            or not isinstance(entry.sha256, str)
            or not _SHA256.fullmatch(entry.sha256)
        ):
            raise ArtifactStorageError("candidate manifest entry is invalid")
        return self._path_for(expected)

    def _path_for(self, relative_path: str, *, create_parents: bool = False) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ArtifactStorageError("artifact path is not allowlisted")
        if relative.parts[0] not in {"candidates", "work", "backups"}:
            raise ArtifactStorageError("artifact path is not in a managed storage namespace")
        path = self.root / relative
        current = self.root
        for component in relative.parts[:-1]:
            current = current / component
            if current.is_symlink():
                raise ArtifactStorageError("artifact directory is missing or is a symlink")
            if not current.exists() and create_parents:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            if not current.exists():
                break
            if not current.is_dir():
                raise ArtifactStorageError("artifact directory is missing or is a symlink")
            if current.stat().st_mode & 0o077:
                raise ArtifactStorageError("artifact directory permissions are too broad")
        if path.is_symlink():
            raise ArtifactStorageError("artifact path must not be a symlink")
        if path.exists() and not path.is_file():
            raise ArtifactStorageError("artifact path is not a regular file")
        if path.resolve(strict=False).parent != path.parent.resolve(strict=False):
            raise ArtifactStorageError("artifact path escaped the storage root")
        return path

    def _write_atomic(self, path: Path, payload: bytes, *, replace: bool) -> str:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                # A hard link publishes the completed inode atomically and fails
                # if another writer already claimed this monotonically allocated name.
                os.link(temporary, path, follow_symlinks=False)
                temporary.unlink()
            self._fsync_directory(path.parent)
        except FileExistsError as exc:
            raise ArtifactStorageError("candidate artifact already exists") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _read_file(path: Path, *, max_bytes: int) -> bytes:
        if path.is_symlink():
            raise ArtifactStorageError("artifact is missing or is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ArtifactStorageError("artifact is missing or is not a regular file") from exc
        except OSError as exc:
            raise ArtifactStorageError("artifact is missing or is not a regular file") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactStorageError("artifact is missing or is not a regular file")
            if metadata.st_size > max_bytes:
                raise ArtifactStorageError("artifact exceeds its configured byte limit")
            if metadata.st_mode & 0o077:
                raise ArtifactStorageError("artifact file permissions are too broad")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class InMemoryCandidateStorage:
    """Deterministic in-memory fake implementing the candidate storage contract."""

    def __init__(
        self, *, max_candidate_bytes: int = 32_768, max_work_bytes: int = 1_048_576
    ) -> None:
        self.max_candidate_bytes = max_candidate_bytes
        self.max_work_bytes = max_work_bytes
        self._candidates: dict[str, bytes] = {}
        self._work: dict[str, bytes] = {}

    def write_candidate(
        self, source_type: str, sequence: int, content: str
    ) -> CandidateManifestEntry:
        relative = _candidate_relative_path(source_type, sequence)
        payload = _validate_candidate_markdown(content, source_type, self.max_candidate_bytes)
        if relative in self._candidates:
            raise ArtifactStorageError("candidate artifact already exists")
        self._candidates[relative] = payload
        return CandidateManifestEntry(
            source_type, sequence, relative, hashlib.sha256(payload).hexdigest()
        )

    def read_candidate(self, entry: CandidateManifestEntry) -> str:
        relative = _candidate_relative_path(entry.source_type, entry.sequence)
        if relative != entry.relative_path or not _SHA256.fullmatch(entry.sha256):
            raise ArtifactStorageError("candidate manifest entry is invalid")
        payload = self._candidates.get(relative)
        if payload is None:
            raise ArtifactStorageError("candidate artifact is missing")
        if hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ArtifactIntegrityError("candidate hash does not match its manifest")
        return payload.decode("utf-8")

    def delete_manifest(self, entries: list[CandidateManifestEntry]) -> list[str]:
        if not entries:
            raise ArtifactStorageError("candidate cleanup requires an explicit non-empty manifest")
        validated: list[tuple[str, CandidateManifestEntry]] = []
        for entry in entries:
            relative = _candidate_relative_path(entry.source_type, entry.sequence)
            if relative != entry.relative_path or not _SHA256.fullmatch(entry.sha256):
                raise ArtifactStorageError("candidate manifest entry is invalid")
            if any(relative == seen for seen, _ in validated):
                raise ArtifactStorageError("candidate manifest contains a duplicate path")
            existing = self._candidates.get(relative)
            if existing is not None and hashlib.sha256(existing).hexdigest() != entry.sha256:
                raise ArtifactIntegrityError("candidate hash does not match its manifest")
            validated.append((relative, entry))
        removed: list[str] = []
        for relative, _ in validated:
            if self._candidates.pop(relative, None) is not None:
                removed.append(relative)
        return removed

    def write_work_artifact(self, name: str, content: str | bytes) -> StoredArtifact:
        if not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("work artifact name is invalid")
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if not payload or len(payload) > self.max_work_bytes or b"\x00" in payload:
            raise ArtifactStorageError("work artifact is invalid or exceeds its byte limit")
        relative = f"work/{name}"
        self._work[relative] = payload
        return StoredArtifact(relative, hashlib.sha256(payload).hexdigest(), len(payload))

    def read_work_artifact(self, name: str, expected_sha256: str | None = None) -> bytes:
        if not _SAFE_WORK_NAME.fullmatch(name) or name in {".", ".."}:
            raise ArtifactStorageError("work artifact name is invalid")
        relative = f"work/{name}"
        payload = self._work.get(relative)
        if payload is None:
            raise ArtifactStorageError("work artifact is missing")
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ArtifactIntegrityError("work artifact hash does not match")
        return payload

    def delete_work_artifact(self, name: str, expected_sha256: str) -> None:
        relative = f"work/{name}"
        payload = self.read_work_artifact(name, expected_sha256)
        del payload
        del self._work[relative]
