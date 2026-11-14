"""Consistent, encrypted, verified dataset archives — local custody only.

A backup is only a backup if it can be restored. This module therefore treats
the encrypt step as one step of four, and refuses to call a stream a backup
until all four have happened:

1. stream the dataset out of the read-only mount as one tar, hashing as it goes;
2. encrypt that tar with a synthetic local age recipient, file to file, checking
   the exit status of both producer and encryptor;
3. decrypt it into private custody and parse the tar, so a truncated or
   corrupted archive fails here rather than during a recovery;
4. compare the restored content hash with the streamed one.

No payload, key or decrypted name is returned to the caller or printed: every
value leaving this module is a hash, a byte count or a closed status code.
SOPS is invoked with explicit file arguments only. A binary stream cannot carry
its own age recipient, so the recipient is always an argument, never a guess.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


class BackupError(Exception):
    """A failed step, named by a code: never a path or a payload fragment."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Archive:
    """What is safe to journal about one dataset archive."""

    dataset: str
    plain_bytes: int
    plain_sha256: str
    sealed_bytes: int
    sealed_sha256: str
    members: int
    restored_sha256: str

    @property
    def consistent(self) -> bool:
        return self.plain_sha256 == self.restored_sha256

    def line(self) -> str:
        """One CSV row: dataset, sizes, hashes, members, equality."""
        return (
            f"{self.dataset},{self.plain_bytes},{self.plain_sha256[:16]},"
            f"{self.sealed_bytes},{self.sealed_sha256[:16]},"
            f"{self.members},{self.restored_sha256[:16]},{self.consistent}"
        )


def head() -> str:
    return "dataset,plain_bytes,plain_sha256,sealed_bytes,sealed_sha256,members,restored_sha256,consistent"


def private_dir(path: Path) -> Path:
    """Create a custody directory that is private from its first instant.

    `mkdir(mode=...)` alone is not enough: the mode is masked by the caller's
    umask, so a permissive umask would leave the directory group- or
    world-traversable. The explicit chmod closes that window.
    """
    # `mkdir(parents=True, mode=...)` applies the mode to the leaf only: a
    # permissive umask would leave every created parent world-traversable. Each
    # missing level is therefore created and restricted on its own.
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for level in reversed(missing):
        level.mkdir(mode=PRIVATE_DIR)
        level.chmod(PRIVATE_DIR)
    path.chmod(PRIVATE_DIR)
    return path


def open_private(path: Path, mode: str = "wb") -> "object":
    """Open a custody file created 0600, whatever the caller's umask says."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE
    )
    os.fchmod(descriptor, PRIVATE_FILE)
    return os.fdopen(descriptor, mode)


def _restrict_umask() -> None:
    """Child-only umask: every file SOPS creates is private by construction."""
    os.umask(0o077)


def _run(arguments: list[str], stdout: int | None = None) -> int:
    """One child process, argv only, no shell, no inherited secret in argv."""
    finished = subprocess.run(
        arguments,
        stdout=stdout,
        stderr=subprocess.PIPE,
        preexec_fn=_restrict_umask,
    )
    return finished.returncode


def _private_target(path: Path) -> None:
    """Create an empty 0600 file, so a tool writing into it inherits custody.

    A tool that merely opens with `O_CREAT` honours its own umask; creating the
    inode here means the mode is decided by this procedure, for the successful
    path and for every partial write on a failure path alike.
    """
    if path.exists():
        path.unlink()
    with open_private(path):
        pass
    path.chmod(PRIVATE_FILE)


def _is_private(path: Path) -> bool:
    return path.exists() and (path.stat().st_mode & 0o777) == PRIVATE_FILE


def fsync(path: Path) -> None:
    """Force the bytes out before the archive is called durable."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def encrypt(tar_path: Path, sealed_path: Path, recipient: str) -> int:
    """Encrypt file to file. A blank recipient is refused, never defaulted."""
    if not recipient.strip():
        raise BackupError("recipient-blank")
    _private_target(sealed_path)
    status = _run(
        [
            "sops",
            "--encrypt",
            "--input-type",
            "binary",
            "--output-type",
            "binary",
            "--age",
            recipient,
            "--output",
            str(sealed_path),
            str(tar_path),
        ]
    )
    if status != 0:
        # A refused or interrupted encrypt may leave a partial archive; custody
        # of a failed attempt is still not worth keeping.
        with contextlib.suppress(FileNotFoundError):
            sealed_path.unlink()
        return status
    if not _is_private(sealed_path):
        raise BackupError("ciphertext-not-private")
    fsync(sealed_path)
    return status


def decrypt(sealed_path: Path, plain_path: Path, key_file: Path | None = None) -> int:
    """Decrypt file to file with an explicit, synthetic identity file.

    The identity is passed to this child process only: the caller's ambient
    `SOPS_AGE_KEY_FILE` is not consulted, so a real identity in the environment
    can never decrypt a rehearsal archive.
    """
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if key_file is not None:
        environment["SOPS_AGE_KEY_FILE"] = str(key_file)
    _private_target(plain_path)
    finished = subprocess.run(
        [
            "sops",
            "decrypt",
            "--input-type",
            "binary",
            "--output-type",
            "binary",
            "--output",
            str(plain_path),
            str(sealed_path),
        ],
        stderr=subprocess.PIPE,
        env=environment,
        preexec_fn=_restrict_umask,
    )
    if finished.returncode != 0:
        # Plaintext is the most sensitive thing this procedure touches: a failed
        # decrypt must not leave a partial one behind.
        with contextlib.suppress(FileNotFoundError):
            plain_path.unlink()
        return finished.returncode
    if not _is_private(plain_path):
        raise BackupError("restore-not-private")
    return finished.returncode


def sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def members(tar_path: Path) -> int:
    """Parse the tar: a corrupt or truncated archive raises here."""
    try:
        with tarfile.open(tar_path) as archive:
            return sum(1 for _ in archive)
    except (tarfile.TarError, OSError) as error:
        raise BackupError("archive-unreadable") from error


def verified(
    dataset: str,
    plain_tar: Path,
    sealed_path: Path,
    restored_tar: Path,
    recipient: str,
    key_file: Path | None = None,
) -> Archive:
    """Encrypt, decrypt and compare one already-streamed dataset tar."""
    plain_bytes, plain_digest = sha256(plain_tar)
    if plain_bytes == 0:
        raise BackupError("dataset-empty")
    if encrypt(plain_tar, sealed_path, recipient) != 0:
        raise BackupError("encrypt-failed")
    plain_tar.unlink()  # the streamed plaintext tar has served its purpose
    sealed_bytes, sealed_digest = sha256(sealed_path)
    if sealed_bytes <= plain_bytes:
        raise BackupError("ciphertext-not-larger")
    if decrypt(sealed_path, restored_tar, key_file) != 0:
        raise BackupError("decrypt-failed")
    _, restored_digest = sha256(restored_tar)
    count = members(restored_tar)
    if count == 0:
        # An export with no entries cannot restore a dataset; refusing here is
        # what keeps an empty stream from being journalled as a backup.
        raise BackupError("archive-empty")
    archive = Archive(
        dataset=dataset,
        plain_bytes=plain_bytes,
        plain_sha256=plain_digest,
        sealed_bytes=sealed_bytes,
        sealed_sha256=sealed_digest,
        members=count,
        restored_sha256=restored_digest,
    )
    if not archive.consistent:
        raise BackupError("restore-hash-mismatch")
    # The restored tar has been parsed and hashed; the ciphertext is the archive
    # and the plaintext does not stay on disk.
    restored_tar.unlink()
    return archive
