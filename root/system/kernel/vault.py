"""Encrypted secret storage.

A drive you might lose must never hold a plaintext API key, so the vault seals
secrets under a passphrase you type at boot.

Construction: scrypt(passphrase, salt) -> 64 bytes, split into an encryption key
and a MAC key. ChaCha20 for confidentiality, encrypt-then-MAC with HMAC-SHA256
for integrity. scrypt, hmac and sha256 all come from hashlib; ChaCha20 is
implemented here only because the stdlib ships no cipher and pip dependencies
are not available at boot. It is RFC 8439 to the letter and is checked against
the RFC's own test vectors in tests/test_vault.py -- no novel cryptography.

File format:
    b"AIOSV1" || salt(16) || nonce(12) || mac(32) || ciphertext
"""

import hashlib
import hmac
import json
import os
import struct
from pathlib import Path

from . import paths

MAGIC = b"AIOSV1"
SALT_LEN = 16
NONCE_LEN = 12
MAC_LEN = 32

# ~100ms on a 2017 laptop; the cost is paid once per boot.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


class VaultError(Exception):
    pass


class BadPassphrase(VaultError):
    pass


# --- ChaCha20 (RFC 8439) ------------------------------------------------------

_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
_MASK = 0xFFFFFFFF


def _rotl(v: int, c: int) -> int:
    return ((v << c) & _MASK) | (v >> (32 - c))


def _quarter_round(x, a, b, c, d) -> None:
    x[a] = (x[a] + x[b]) & _MASK
    x[d] = _rotl(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & _MASK
    x[b] = _rotl(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & _MASK
    x[d] = _rotl(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & _MASK
    x[b] = _rotl(x[b] ^ x[c], 7)


def _block(key: bytes, counter: int, nonce: bytes) -> bytes:
    state = list(_CONSTANTS)
    state += list(struct.unpack("<8I", key))
    state.append(counter & _MASK)
    state += list(struct.unpack("<3I", nonce))

    w = list(state)
    for _ in range(10):  # 20 rounds = 10 double-rounds
        _quarter_round(w, 0, 4, 8, 12)
        _quarter_round(w, 1, 5, 9, 13)
        _quarter_round(w, 2, 6, 10, 14)
        _quarter_round(w, 3, 7, 11, 15)
        _quarter_round(w, 0, 5, 10, 15)
        _quarter_round(w, 1, 6, 11, 12)
        _quarter_round(w, 2, 7, 8, 13)
        _quarter_round(w, 3, 4, 9, 14)

    return struct.pack("<16I", *[(w[i] + state[i]) & _MASK for i in range(16)])


def chacha20(key: bytes, nonce: bytes, data: bytes, counter: int = 1) -> bytes:
    """XOR data with the ChaCha20 keystream. Encryption and decryption alike."""
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")

    out = bytearray(len(data))
    for offset in range(0, len(data), 64):
        stream = _block(key, counter + offset // 64, nonce)
        chunk = data[offset : offset + 64]
        for i, byte in enumerate(chunk):
            out[offset + i] = byte ^ stream[i]
    return bytes(out)


# --- vault --------------------------------------------------------------------


def _derive(passphrase: str, salt: bytes):
    dk = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=64,
        maxmem=128 * SCRYPT_N * SCRYPT_R * 2,
    )
    return dk[:32], dk[32:]


class Vault:
    """Passphrase-sealed key/value store, persisted as a single opaque file."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.VAULT / "keys.enc"

    def exists(self) -> bool:
        return self.path.exists()

    def seal(self, secrets: dict, passphrase: str) -> None:
        salt = os.urandom(SALT_LEN)
        nonce = os.urandom(NONCE_LEN)
        enc_key, mac_key = _derive(passphrase, salt)

        plaintext = json.dumps(secrets).encode("utf-8")
        ciphertext = chacha20(enc_key, nonce, plaintext)
        mac = hmac.new(mac_key, MAGIC + salt + nonce + ciphertext, hashlib.sha256).digest()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(MAGIC + salt + nonce + mac + ciphertext)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)  # atomic; a half-written vault locks you out

    def unseal(self, passphrase: str) -> dict:
        if not self.exists():
            raise VaultError(f"no vault at {paths.rel(self.path)}")

        blob = self.path.read_bytes()
        head = len(MAGIC) + SALT_LEN + NONCE_LEN + MAC_LEN
        if len(blob) < head or not blob.startswith(MAGIC):
            raise VaultError("vault is corrupt or not an aiOS vault")

        salt = blob[len(MAGIC) : len(MAGIC) + SALT_LEN]
        nonce = blob[len(MAGIC) + SALT_LEN : len(MAGIC) + SALT_LEN + NONCE_LEN]
        mac = blob[len(MAGIC) + SALT_LEN + NONCE_LEN : head]
        ciphertext = blob[head:]

        enc_key, mac_key = _derive(passphrase, salt)
        expect = hmac.new(mac_key, MAGIC + salt + nonce + ciphertext, hashlib.sha256).digest()
        # Verify before decrypting, in constant time.
        if not hmac.compare_digest(mac, expect):
            raise BadPassphrase("wrong passphrase, or the vault has been tampered with")

        return json.loads(chacha20(enc_key, nonce, ciphertext).decode("utf-8"))
