"""Evidence integrity and HMAC/Ed25519 attestation helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Mapping


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_payload(
    payload: Mapping[str, Any], *, key: bytes, key_id: str = "default"
) -> dict[str, str]:
    canonical = canonical_json_bytes(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {
        "algorithm": "hmac-sha256",
        "key_id": key_id,
        "canonical_payload_sha256": digest,
        "signature": signature,
    }


def sign_payload_ed25519(
    payload: Mapping[str, Any], *, private_key_pem: bytes, key_id: str = "default"
) -> dict[str, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise RuntimeError(
            "Ed25519 signing requires the optional 'signing' dependency"
        ) from exc

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("evidence signing key is not an Ed25519 private key")
    canonical = canonical_json_bytes(payload)
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "ed25519",
        "key_id": key_id,
        "canonical_payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "public_key_sha256": hashlib.sha256(public_raw).hexdigest(),
        "signature": base64.b64encode(key.sign(canonical)).decode("ascii"),
    }


def verify_payload_signature(
    payload: Mapping[str, Any],
    signature: Mapping[str, Any],
    *,
    key: bytes | None = None,
    public_key_pem: bytes | None = None,
) -> bool:
    algorithm = signature.get("algorithm")
    canonical = canonical_json_bytes(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(
        str(signature.get("canonical_payload_sha256") or ""), digest
    ):
        return False
    if algorithm == "hmac-sha256":
        if key is None:
            return False
        expected = sign_payload(
            payload, key=key, key_id=str(signature.get("key_id") or "default")
        )
        return hmac.compare_digest(
            str(signature.get("signature") or ""), expected["signature"]
        )
    if algorithm == "ed25519":
        if public_key_pem is None:
            return False
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            public = serialization.load_pem_public_key(public_key_pem)
            if not isinstance(public, Ed25519PublicKey):
                return False
            raw = public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if not hmac.compare_digest(
                str(signature.get("public_key_sha256") or ""),
                hashlib.sha256(raw).hexdigest(),
            ):
                return False
            public.verify(
                base64.b64decode(str(signature.get("signature") or "")), canonical
            )
            return True
        except (ValueError, TypeError, InvalidSignature):
            return False
    return False


def verify_evidence_file(
    evidence_path: str | Path,
    signature_path: str | Path,
    *,
    key: bytes | None = None,
    public_key_pem: bytes | None = None,
) -> bool:
    payload = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    signature = json.loads(Path(signature_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        return False
    return verify_payload_signature(
        payload, signature, key=key, public_key_pem=public_key_pem
    )
