import base64
import hashlib
import hmac
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from scripts.backup_von_db import _encrypt_file_fernet
from scripts.restore_von_db import _decrypt_encrypted_zip
from src.backend.utils import fernet_file


@pytest.mark.parametrize("size", [0, 15, 16, 17, 47, 48, 49, 96, 257])
def test_file_tokens_interoperate_with_fernet(tmp_path, monkeypatch, size):
    monkeypatch.setattr(fernet_file, "_CHUNK_BYTES", 48)
    key = Fernet.generate_key()
    data = os.urandom(size)
    source = tmp_path / "source"
    encrypted = tmp_path / "encrypted"
    restored = tmp_path / "restored"
    source.write_bytes(data)

    fernet_file.encrypt_file(source, encrypted, key=key)
    assert Fernet(key).decrypt(encrypted.read_bytes()) == data
    encrypted.write_bytes(Fernet(key).encrypt(data))
    fernet_file.decrypt_file(encrypted, restored, key=key)
    assert restored.read_bytes() == data
    if os.name != "nt":
        assert restored.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("offset", [0, 1, 9, 25, -1])
def test_tampered_tokens_are_rejected_before_decryption(
    tmp_path, monkeypatch, offset
):
    key = Fernet.generate_key()
    token = bytearray(base64.urlsafe_b64decode(Fernet(key).encrypt(b"private data")))
    token[offset] ^= 1
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(base64.urlsafe_b64encode(token))
    destination.write_bytes(b"preserve existing output")

    def reject_decryptor(*args, **kwargs):
        pytest.fail("Unauthenticated ciphertext reached the decryptor")

    monkeypatch.setattr(fernet_file, "Cipher", reject_decryptor)
    with pytest.raises(InvalidToken):
        fernet_file.decrypt_file(source, destination, key=key)
    assert destination.read_bytes() == b"preserve existing output"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["destination", "source"]


@pytest.mark.parametrize("damage", ["wrong_key", "truncated", "invalid_base64", "appended"])
def test_invalid_files_never_publish_plaintext(tmp_path, damage):
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b"private data")
    if damage == "wrong_key":
        key = Fernet.generate_key()
    elif damage == "truncated":
        token = token[:-8]
    elif damage == "invalid_base64":
        token = b"!" + token[1:]
    else:
        token += b"AAAA"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(token)
    with pytest.raises(InvalidToken):
        fernet_file.decrypt_file(source, destination, key=key)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_authenticated_invalid_padding_preserves_destination(tmp_path):
    key = Fernet.generate_key()
    token = bytearray(base64.urlsafe_b64decode(Fernet(key).encrypt(b"A" * 16)))
    # The second block is all padding. This changes its final byte to zero.
    token[25 + 15] ^= 16
    token[-32:] = hmac.digest(base64.urlsafe_b64decode(key)[:16], token[:-32], hashlib.sha256)
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(base64.urlsafe_b64encode(token))
    destination.write_bytes(b"previous")
    with pytest.raises(InvalidToken):
        fernet_file.decrypt_file(source, destination, key=key)
    assert destination.read_bytes() == b"previous"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["destination", "source"]


def test_backup_restore_entrypoints_do_not_read_whole_files(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    data = os.urandom(fernet_file._CHUNK_BYTES * 3 + 17)
    source = tmp_path / "backup.zip"
    destination = tmp_path / "restored.zip"
    source.write_bytes(data)
    timestamp = 1_700_000_000
    os.utime(source, (timestamp, timestamp))

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", lambda *args: pytest.fail("Whole-file read"))
        encrypted = _encrypt_file_fernet(source, key=key)
        _decrypt_encrypted_zip(encrypted, fernet_key=key, output_zip_path=destination)
    assert encrypted.stat().st_mtime == timestamp
    assert destination.read_bytes() == data
    assert Fernet(key).decrypt(encrypted.read_bytes()) == data


@pytest.mark.parametrize("operation", [fernet_file.encrypt_file, fernet_file.decrypt_file])
def test_failed_publication_preserves_existing_output(tmp_path, monkeypatch, operation):
    key = Fernet.generate_key()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    data = b"private data"
    source.write_bytes(Fernet(key).encrypt(data) if operation is fernet_file.decrypt_file else data)
    destination.write_bytes(b"previous")

    def fail_replace(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(fernet_file.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failure"):
        operation(source, destination, key=key)
    assert destination.read_bytes() == b"previous"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["destination", "source"]


@pytest.mark.parametrize("operation", [fernet_file.encrypt_file, fernet_file.decrypt_file])
def test_same_path_is_rejected(tmp_path, operation):
    source = tmp_path / "source"
    source.write_bytes(b"keep")
    with pytest.raises(ValueError, match="different files"):
        operation(source, source, key=Fernet.generate_key())
    assert source.read_bytes() == b"keep"
