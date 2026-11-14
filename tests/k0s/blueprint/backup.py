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

import hashlib
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


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


def _run(arguments: list[str], stdout: int | None = None) -> int:
    """One child process, argv only, no shell, no inherited secret in argv."""
    finished = subprocess.run(arguments, stdout=stdout, stderr=subprocess.PIPE)
    return finished.returncode


def encrypt(tar_path: Path, sealed_path: Path, recipient: str) -> int:
    """Encrypt file to file. A blank recipient is refused, never defaulted."""
    if not recipient.strip():
        raise BackupError("recipient-blank")
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
    # SOPS creates the archive with the ambient umask, which is not this
    # procedure's confidentiality promise: a ciphertext is still private custody.
    if status == 0 and sealed_path.exists():
        sealed_path.chmod(0o600)
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
    )
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
    return archive
