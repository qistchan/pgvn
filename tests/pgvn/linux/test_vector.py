#!/usr/bin/env python3
"""Confirm the published AES-GCM vector with an independent implementation."""
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

key = bytes(range(32))
nonce = bytes(range(12))
aad = b"pgvn\nalice\ndeployment-v1"
plain = "s3cret-密码".encode()
sealed = AESGCM(key).encrypt(nonce, plain, aad)
assert sealed[:-16].hex() == "3431b569a091effe22c7702b30"
assert sealed[-16:].hex() == "8abff343da73e0692c9e37026007713b"
assert AESGCM(key).decrypt(nonce, sealed, aad) == plain
try:
    AESGCM(key).decrypt(nonce, sealed, b"pgvn\nbob\ndeployment-v1")
except Exception:
    pass
else:
    raise SystemExit("wrong AAD was accepted")
print("vector checks passed")
