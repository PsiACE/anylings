"""Backup custody: an archive that cannot be restored is not a backup.

Every case builds real files with the real tools, because the property under
test is tool behaviour, not a mock's. Encryption uses a synthetic age key made
for the test and discarded with its temporary directory; no tenant identity is
involved, and no payload leaves it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup  # noqa: E402

TOOLS_PRESENT = all(shutil.which(tool) for tool in ("sops", "age-keygen"))


@unittest.skipUnless(TOOLS_PRESENT, "sops and age-keygen are required")
class Custody(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.key = self.directory / "age.key"
        subprocess.run(["age-keygen", "-o", str(self.key)], check=True,
                       capture_output=True)
        self.recipient = subprocess.run(
            ["age-keygen", "-y", str(self.key)], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def dataset(self, name: str = "data") -> Path:
        """A tar shaped like one read-only dataset export."""
        source = self.directory / "payload"
        source.mkdir(exist_ok=True)
        (source / "records.ndjson").write_text('{"seq":1}\n{"seq":2}\n')
        (source / "state").mkdir(exist_ok=True)
        (source / "state" / "owner").write_text("synthetic\n")
        tar_path = self.directory / f"{name}.tar"
        with tarfile.open(tar_path, "w") as archive:
            archive.add(source, arcname=".")
        return tar_path

    def test_archive_round_trips_and_reports_only_hashes(self) -> None:
        tar_path = self.dataset()
        archive = backup.verified(
            "gateway", tar_path, self.directory / "sealed.sops",
            self.directory / "restored.tar", self.recipient, self.key,
        )
        self.assertTrue(archive.consistent)
        self.assertEqual(archive.plain_sha256, archive.restored_sha256)
        self.assertGreater(archive.sealed_bytes, archive.plain_bytes)
        self.assertGreaterEqual(archive.members, 3)
        self.assertNotIn("synthetic", archive.line())
        self.assertNotIn("records.ndjson", archive.line())

    def test_ciphertext_is_private_to_its_custody(self) -> None:
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(self.dataset(), sealed, self.recipient), 0)
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o600)

    def test_ciphertext_path_is_private_even_under_a_permissive_umask(self) -> None:
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(self.dataset(), sealed, self.recipient), 0)
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o600)

    def test_custody_directories_are_private_under_a_permissive_umask(self) -> None:
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        nested = self.directory / "a" / "b"
        backup.private_dir(nested)
        self.assertEqual(nested.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "a").stat().st_mode & 0o777, 0o700)

    def test_streamed_plaintext_tar_is_created_private(self) -> None:
        """The stream sink is a descriptor with an explicit 0600, not Path.open."""
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        tar_path = self.directory / "stream.tar"
        with backup.open_private(tar_path) as sink:
            sink.write(b"placeholder")
        self.assertEqual(tar_path.stat().st_mode & 0o777, 0o600)

    def test_failed_encrypt_leaves_no_ciphertext(self) -> None:
        sealed = self.directory / "sealed.sops"
        status = backup.encrypt(self.dataset(), sealed, "age1notarealrecipient")
        self.assertNotEqual(status, 0)
        self.assertFalse(sealed.exists())

    def test_failed_decrypt_leaves_no_plaintext(self) -> None:
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(self.dataset(), sealed, self.recipient), 0)
        restored = self.directory / "restored.tar"
        status = backup.decrypt(sealed, restored, self.directory / "absent.key")
        self.assertNotEqual(status, 0)
        self.assertFalse(restored.exists())

    def test_verified_removes_its_plaintext_intermediates(self) -> None:
        tar_path = self.dataset()
        restored = self.directory / "restored.tar"
        backup.verified("gateway", tar_path, self.directory / "s.sops", restored,
                        self.recipient, self.key)
        self.assertFalse(tar_path.exists())
        self.assertFalse(restored.exists())
        self.assertTrue((self.directory / "s.sops").exists())

    def test_ciphertext_does_not_contain_the_payload(self) -> None:
        tar_path = self.dataset()
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(tar_path, sealed, self.recipient), 0)
        self.assertNotIn(b'{"seq":1}', sealed.read_bytes())

    def test_blank_recipient_is_refused_before_encrypting(self) -> None:
        with self.assertRaises(backup.BackupError) as caught:
            backup.encrypt(self.dataset(), self.directory / "x", "  ")
        self.assertEqual(caught.exception.code, "recipient-blank")

    def test_missing_identity_is_reported_not_silently_skipped(self) -> None:
        tar_path = self.dataset()
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(tar_path, sealed, self.recipient), 0)
        # The correct recipient, but no usable identity: the run must stop rather
        # than hand over a file it never opened.
        absent = self.directory / "absent.key"
        self.assertNotEqual(backup.decrypt(sealed, self.directory / "r.tar", absent), 0)

    def test_empty_dataset_is_refused(self) -> None:
        empty = self.directory / "empty.tar"
        with tarfile.open(empty, "w"):
            pass
        # A tar of nothing is still bytes, so the refusal must come from content.
        with self.assertRaises(backup.BackupError) as caught:
            backup.verified("gateway", empty, self.directory / "sealed.sops",
                            self.directory / "restored.tar", self.recipient, self.key)
        self.assertEqual(caught.exception.code, "archive-empty")

    def test_corrupt_archive_is_refused(self) -> None:
        broken = self.directory / "broken.tar"
        broken.write_bytes(b"not a tar at all" * 64)
        with self.assertRaises(backup.BackupError) as caught:
            backup.members(broken)
        self.assertEqual(caught.exception.code, "archive-unreadable")

    def test_truncated_ciphertext_is_refused(self) -> None:
        tar_path = self.dataset()
        sealed = self.directory / "sealed.sops"
        self.assertEqual(backup.encrypt(tar_path, sealed, self.recipient), 0)
        sealed.write_bytes(sealed.read_bytes()[:32])
        # Decrypt directly: `verified` would encrypt again and hide the damage.
        self.assertNotEqual(
            backup.decrypt(sealed, self.directory / "restored.tar", self.key), 0
        )


if __name__ == "__main__":
    unittest.main()
