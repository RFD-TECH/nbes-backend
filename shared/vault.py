"""
shared/vault.py — AES-256-GCM Vault Operations
===============================================

Stores approved exam items in encrypted form. Keys are held in the HSM
in production. In dev (VAULT_DEV_MODE=True), a software AES key is
derived from settings.SECRET_KEY — no HSM required.

Every vault READ must produce an AuditEvent (VAULT_READ).
Vault export requires 2-of-3 NBEC officer co-authorisation via viewflow.
Daily integrity check: decrypt → SHA-256 → compare to stored content_hash.

Reference: NBES System Architecture §4 — Vault & Multi-Party Authorisation
"""

import os
import hashlib
from django.conf import settings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key(item_id: str) -> bytes:
    """
    Retrieve the AES-256 key for a vault item.

    Dev (VAULT_DEV_MODE=True):
        Derives a 32-byte key from SECRET_KEY + item_id using SHA-256.
        No HSM required.

    Production (VAULT_DEV_MODE=False):
        TODO: Implement PKCS#11 HSM key retrieval.
        import pkcs11
        lib = pkcs11.lib(settings.PKCS11_LIB_PATH)
        token = lib.get_token(token_label=settings.HSM_TOKEN_LABEL)
        with token.open(user_pin=settings.HSM_PIN) as session:
            key = session.get_key(label=f"vault-item-{item_id}")
            return bytes(key[pkcs11.Attribute.VALUE])
    """
    if settings.VAULT_DEV_MODE:
        raw = f"{settings.SECRET_KEY}:vault-item-{item_id}".encode()
        return hashlib.sha256(raw).digest()  # 32 bytes → AES-256
    else:
        raise NotImplementedError(
            "HSM key retrieval not implemented. "
            "Set VAULT_DEV_MODE=True for local development, "
            "or implement PKCS#11 integration for production."
        )


def encrypt_item(plaintext: bytes, item_id: str) -> tuple[bytes, bytes, str]:
    """
    Encrypt item content with AES-256-GCM.

    Returns:
        ciphertext (bytes): Encrypted content — store in Item.content_encrypted
        nonce (bytes):      GCM nonce — store in Item.vault_nonce
        content_hash (str): SHA-256 of plaintext — store in Item.content_hash
    """
    key = _get_key(item_id)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=item_id.encode())
    content_hash = hashlib.sha256(plaintext).hexdigest()
    return ciphertext, nonce, content_hash


def decrypt_item(ciphertext: bytes, nonce: bytes, item_id: str) -> bytes:
    """
    Decrypt item content and verify GCM authentication tag.
    Raises cryptography.exceptions.InvalidTag on tampering.
    """
    key = _get_key(item_id)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=item_id.encode())


def verify_vault_integrity(item_id: str, stored_hash: str) -> bool:
    """
    Decrypt item and verify SHA-256 matches stored content_hash.
    Called by the daily vault-integrity Celery Beat task.

    Returns True if integrity is confirmed, False on hash mismatch.
    Raises on decryption failure (tampered ciphertext).
    """
    from apps.itembank.models import Item
    item = Item.objects.get(id=item_id)
    plaintext = decrypt_item(item.content_encrypted, bytes(item.vault_nonce), item_id)
    computed = hashlib.sha256(plaintext).hexdigest()
    return computed == stored_hash
