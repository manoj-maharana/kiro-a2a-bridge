"""Signed agent cards (JWS, RFC 7515) so callers can tell a card is genuine.

The signed bytes are the card without its `signatures` field, in canonical
JSON (sorted keys, no spaces, UTF-8; RFC 8785 for the values agent cards use).
The signature is a detached JWS: {"protected": <header>, "signature": <sig>}.

Supported algorithms: ES256 / RS256 (private key in a PEM file) and HS256
(shared secret). Needs PyJWT with crypto: pip install "kiro-a2a-bridge[oauth]".
"""

from __future__ import annotations

import base64
import json


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(card: dict) -> bytes:
    body = {k: v for k, v in card.items() if k != "signatures"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _algorithm(alg: str):
    try:
        from jwt.algorithms import get_default_algorithms
    except ImportError:
        raise SystemExit('Card signing needs PyJWT: pip install "kiro-a2a-bridge[oauth]"') from None
    algorithms = get_default_algorithms()
    if alg not in algorithms:
        raise ValueError(f"unsupported signing algorithm {alg}")
    return algorithms[alg]


def load_key(value: str) -> bytes:
    """A PEM file path, or the secret itself (for HS256)."""
    try:
        with open(value, "rb") as fh:
            return fh.read()
    except OSError:
        return value.encode()


class CardSigner:
    def __init__(self, key: bytes, kid: str, alg: str = "ES256"):
        self.alg = alg
        self.kid = kid
        self._algo = _algorithm(alg)
        self._key = self._algo.prepare_key(key)

    def sign(self, card: dict) -> dict:
        header = _b64(json.dumps({"alg": self.alg, "kid": self.kid, "typ": "JOSE"}, separators=(",", ":")).encode())
        signing_input = f"{header}.{_b64(canonical(card))}".encode()
        signature = _b64(self._algo.sign(signing_input, self._key))
        return {**card, "signatures": [{"protected": header, "signature": signature}]}


def verify_card(card: dict, key: bytes) -> str:
    """Check the card's signatures with a public key (or HS256 secret).
    Returns the kid that matched; raises ValueError otherwise."""
    signatures = card.get("signatures") or []
    if not signatures:
        raise ValueError("the agent card is not signed")
    payload = _b64(canonical(card))
    for entry in signatures:
        try:
            header = json.loads(_unb64(entry["protected"]))
            algo = _algorithm(header["alg"])
            prepared = algo.prepare_key(key)
            if algo.verify(f"{entry['protected']}.{payload}".encode(), prepared, _unb64(entry["signature"])):
                return header.get("kid", "")
        except Exception:
            continue
    raise ValueError("the agent card signature does not match the key")
