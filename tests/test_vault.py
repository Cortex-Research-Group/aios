"""Verify the vault's ChaCha20 against the RFC 8439 test vectors, then the
seal/unseal round trip and its failure modes.

The cipher is hand-written (no pip deps at boot), so it does not get trusted
until it reproduces the RFC's published output byte for byte.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root" / "system"))

from kernel import vault  # noqa: E402


class TestChaCha20RFC8439(unittest.TestCase):
    KEY = bytes(range(32))

    def test_block_function(self):
        """RFC 8439 section 2.3.2."""
        nonce = bytes.fromhex("000000090000004a00000000")
        expected = bytes.fromhex(
            "10f1e7e4d13b5915500fdd1fa32071c4"
            "c7d1f4c733c068030422aa9ac3d46c4e"
            "d2826446079faa0914c2d705d98b02a2"
            "b5129cd1de164eb9cbd083e8a2503c4e"
        )
        self.assertEqual(vault._block(self.KEY, 1, nonce), expected)

    def test_encryption_vector(self):
        """RFC 8439 section 2.4.2."""
        nonce = bytes.fromhex("000000000000004a00000000")
        plaintext = (
            b"Ladies and Gentlemen of the class of '99: If I could offer you "
            b"only one tip for the future, sunscreen would be it."
        )
        expected = bytes.fromhex(
            "6e2e359a2568f98041ba0728dd0d6981"
            "e97e7aec1d4360c20a27afccfd9fae0b"
            "f91b65c5524733ab8f593dabcd62b357"
            "1639d624e65152ab8f530c359f0861d8"
            "07ca0dbf500d6a6156a38e088a22b65e"
            "52bc514d16ccf806818ce91ab7793736"
            "5af90bbf74a35be6b40b8eedf2785e42"
            "874d"
        )
        self.assertEqual(vault.chacha20(self.KEY, nonce, plaintext, counter=1), expected)

    def test_is_involution(self):
        nonce = bytes(12)
        msg = b"x" * 200  # spans multiple 64-byte blocks
        ct = vault.chacha20(self.KEY, nonce, msg)
        self.assertNotEqual(ct, msg)
        self.assertEqual(vault.chacha20(self.KEY, nonce, ct), msg)


class TestVault(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "keys.enc"
        self.v = vault.Vault(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        secrets = {"OPENROUTER_API_KEY": "sk-or-v1-abc123", "note": "hello"}
        self.v.seal(secrets, "correct horse battery staple")
        self.assertEqual(self.v.unseal("correct horse battery staple"), secrets)

    def test_wrong_passphrase_rejected(self):
        self.v.seal({"k": "v"}, "right")
        with self.assertRaises(vault.BadPassphrase):
            self.v.unseal("wrong")

    def test_key_absent_from_disk(self):
        """The whole point: the secret must not be recoverable from the file."""
        self.v.seal({"OPENROUTER_API_KEY": "sk-or-v1-SECRETVALUE"}, "pw")
        blob = self.path.read_bytes()
        self.assertNotIn(b"SECRETVALUE", blob)
        self.assertNotIn(b"OPENROUTER_API_KEY", blob)

    def test_tampering_detected(self):
        self.v.seal({"k": "v"}, "pw")
        blob = bytearray(self.path.read_bytes())
        blob[-1] ^= 0x01  # flip one bit of ciphertext
        self.path.write_bytes(bytes(blob))
        with self.assertRaises(vault.BadPassphrase):
            self.v.unseal("pw")

    def test_missing_vault(self):
        with self.assertRaises(vault.VaultError):
            self.v.unseal("pw")

    def test_permissions_locked_down(self):
        self.v.seal({"k": "v"}, "pw")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
