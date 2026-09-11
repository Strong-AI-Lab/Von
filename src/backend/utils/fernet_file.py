"""Bounded-memory files in the existing Fernet token format.

Ciphertext is spooled privately so its entire HMAC can be verified before any
plaintext is decrypted. Completed output replaces its destination atomically.
The algorithm and format are specified at https://github.com/fernet/spec.
"""

from __future__ import annotations

import base64
import binascii
import os
import struct
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Divisible by the AES block size and both base64 input/output group sizes.
_CHUNK_BYTES = 3 * 256 * 1024
_HEADER_BYTES = 25
_SIGNATURE_BYTES = 32


def _keys(key: str | bytes) -> tuple[bytes, bytes]:
    Fernet(key)  # Apply the library's public key validation contract.
    decoded = base64.urlsafe_b64decode(key)
    return decoded[:16], decoded[16:]


@contextmanager
def _atomic_output(destination: Path):
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def encrypt_file(source: Path, destination: Path, *, key: str | bytes) -> None:
    """Write a standard Fernet token without loading the file into memory."""
    if source.resolve() == destination.resolve():
        raise ValueError("Fernet input and output must be different files")
    signing_key, encryption_key = _keys(key)
    iv = os.urandom(16)
    header = b"\x80" + struct.pack(">Q", int(time.time())) + iv
    signer = hmac.HMAC(signing_key, hashes.SHA256())
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(encryption_key), modes.CBC(iv)).encryptor()
    with tempfile.TemporaryFile(dir=destination.parent) as ciphertext:
        ciphertext.write(header)
        signer.update(header)
        with source.open("rb") as input_file:
            while chunk := input_file.read(_CHUNK_BYTES):
                encrypted = encryptor.update(padder.update(chunk))
                ciphertext.write(encrypted)
                signer.update(encrypted)
        encrypted = encryptor.update(padder.finalize()) + encryptor.finalize()
        ciphertext.write(encrypted)
        signer.update(encrypted)
        ciphertext.write(signer.finalize())
        ciphertext.seek(0)
        with _atomic_output(destination) as output:
            while chunk := ciphertext.read(_CHUNK_BYTES):
                output.write(base64.urlsafe_b64encode(chunk))


def decrypt_file(source: Path, destination: Path, *, key: str | bytes) -> None:
    """Authenticate a complete Fernet file, then decrypt with bounded memory.

    Backup tokens have no expiry. Invalid or truncated input never publishes
    plaintext and leaves any existing destination unchanged.
    """
    if source.resolve() == destination.resolve():
        raise ValueError("Fernet input and output must be different files")
    signing_key, encryption_key = _keys(key)
    with tempfile.TemporaryFile(dir=destination.parent) as ciphertext:
        padded = False
        with source.open("rb") as input_file:
            while chunk := input_file.read(_CHUNK_BYTES):
                if padded:
                    raise InvalidToken
                try:
                    decoded = base64.b64decode(chunk, altchars=b"-_", validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise InvalidToken from exc
                ciphertext.write(decoded)
                padded = b"=" in chunk
        size = ciphertext.tell()
        payload_size = size - _HEADER_BYTES - _SIGNATURE_BYTES
        ciphertext.seek(0)
        header = ciphertext.read(_HEADER_BYTES)
        if (
            len(header) != _HEADER_BYTES
            or header[0] != 0x80
            or payload_size < 16
            or payload_size % 16
        ):
            raise InvalidToken
        signer = hmac.HMAC(signing_key, hashes.SHA256())
        signer.update(header)
        remaining = payload_size
        while remaining:
            chunk = ciphertext.read(min(_CHUNK_BYTES, remaining))
            if not chunk:
                raise InvalidToken
            signer.update(chunk)
            remaining -= len(chunk)
        try:
            signer.verify(ciphertext.read(_SIGNATURE_BYTES))
        except InvalidSignature as exc:
            raise InvalidToken from exc

        # Only authenticated ciphertext reaches the decryptor or output file.
        decryptor = Cipher(
            algorithms.AES(encryption_key), modes.CBC(header[9:25])
        ).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        ciphertext.seek(_HEADER_BYTES)
        remaining = payload_size
        with _atomic_output(destination) as output:
            while remaining:
                chunk = ciphertext.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise InvalidToken
                output.write(unpadder.update(decryptor.update(chunk)))
                remaining -= len(chunk)
            try:
                output.write(unpadder.update(decryptor.finalize()))
                output.write(unpadder.finalize())
            except ValueError as exc:
                raise InvalidToken from exc
