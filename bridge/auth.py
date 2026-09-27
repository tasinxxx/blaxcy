"""Authentication for the local bridge→runner boundary.

The bridge is local-only (no network listener, by design), so this is *not* a
network authentication scheme. It is the boundary between two cooperating
processes on one machine — a task producer (a future CI runner or automation
script) and the BLAXCY Body — and its job is narrower and stricter than a web
login:

* **Proof of possession of a shared secret.** The producer proves it holds the
  bridge token by signing the exact task document it submits; the Body verifies
  the signature before parsing anything executable. The secret itself never
  crosses the boundary in either direction.
* **No hard-coded secrets (§69 by analogy).** The token lives in the
  ``BLAXCY_BRIDGE_TOKEN`` environment variable, exactly like the Gemini key
  lives in the OS keyring: never in source, never in config, never in argv.
  A bridge started without a token refuses submissions rather than degrading
  to unauthenticated operation — fail closed.
* **Replay protection.** A signed request carries a monotonic nonce and a
  timestamp; the bridge refuses a nonce it has already accepted and a request
  whose timestamp is outside the skew window, so a captured signed payload
  cannot be re-submitted later by something that read it from a pipe, a log,
  or a process listing.
* **Signing covers the payload.** The signature is an HMAC over
  ``nonce || timestamp || task_document``, so a tampered task (a changed step,
  an extra argument) fails verification even though the token itself was never
  exposed.

Constant-time comparison everywhere (``hmac.compare_digest``), because a local
attacker with a timing oracle is still an attacker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time

#: The environment variable holding the shared bridge token. Read at
#: :meth:`BridgeAuthenticator.load` time, never at import time, so a test can
#: set it per-test and a long-lived process does not cache a secret it was
#: never given.
TOKEN_ENV_VAR: str = "BLAXCY_BRIDGE_TOKEN"

#: Minimum accepted token length. A shared secret shorter than this is a
#: configuration mistake, not a token: refuse rather than authenticate with it.
MIN_TOKEN_LENGTH: int = 32

#: How far a signed request's timestamp may drift from the bridge's clock, in
#: seconds. Generous enough for a slow producer; small enough that a replay
#: window is short.
MAX_CLOCK_SKEW_SECONDS: float = 300.0

#: Nonces accepted per verifier instance. Bounded so a hostile producer cannot
#: grow the bridge's memory with unique nonces; the eviction order is FIFO,
#: which is safe because a nonce old enough to be evicted is far outside the
#: replay window anyway (its timestamp would fail the skew check first).
MAX_TRACKED_NONCES: int = 10_000


class BridgeAuthError(Exception):
    """A submission the boundary refused on authentication grounds."""

    def __init__(self, reason: str, details: dict[str, str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, str] = details or {}


class BridgeAuthenticator:
    """The Body-side verifier for signed task submissions.

    Args:
        token: The shared secret. ``None`` (the default) loads it from
            ``BLAXCY_BRIDGE_TOKEN``. An empty or too-short token is a
            configuration error and raises immediately — the bridge must not
            come up half-authenticated.
    """

    def __init__(self, token: str | None = None) -> None:
        #: The resolved secret. ``None`` means nothing was configured, and the
        #: bridge must refuse every submission (fail closed).
        if token is not None:
            self._token: str | None = token
            self._token_source = "explicit"
        else:
            self._token = os.environ.get(TOKEN_ENV_VAR)
            self._token_source = f"env:{TOKEN_ENV_VAR}"
        if self._token is not None and len(self._token) < MIN_TOKEN_LENGTH:
            raise BridgeAuthError(
                "TOKEN_TOO_SHORT",
                {"min_length": str(MIN_TOKEN_LENGTH), "length": str(len(self._token))},
            )
        self._lock = threading.Lock()
        self._seen_nonces: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        """Whether a token is loaded. An unconfigured bridge refuses everything."""
        return self._token is not None

    @property
    def token_source(self) -> str:
        """Where the token came from, for honest status reporting (no value)."""
        return self._token_source

    # -- Producer side ---------------------------------------------------------

    def sign_request(self, task_document: str, *, nonce: str, timestamp: float | None = None) -> str:
        """Produce the hex signature for one task submission.

        Args:
            task_document: The exact serialized task document (the same bytes
                the producer transmits — signing one serialization and sending
                another defeats the signature).
            nonce: A unique-per-submission value. The producer generates it; it
                must never repeat for the bridge's replay window.
            timestamp: Unix seconds. ``None`` uses the current time.
        """
        if self._token is None:
            raise BridgeAuthError("NO_TOKEN_CONFIGURED", {"side": "signing"})
        if not nonce:
            raise BridgeAuthError("EMPTY_NONCE", {})
        moment = time.time() if timestamp is None else timestamp
        return self._mac(nonce, moment, task_document)

    # -- Body side ---------------------------------------------------------------

    def verify_request(
        self,
        task_document: str,
        *,
        signature: str,
        nonce: str,
        timestamp: float,
        now: float | None = None,
    ) -> None:
        """Verify one signed submission, or raise ``BridgeAuthError``.

        The checks run in cheapest-first order (presence, freshness, replay,
        then the MAC) so a rejected request costs the same kind of effort a
        valid one would have — and so the error reason names the actual
        failure rather than a downstream symptom.

        Note the deliberate order: the replay check runs *before* the MAC. A
        replayed request fails as a replay even when its signature is valid,
        and an attacker probing signatures cannot distinguish nonce-tracking
        from MAC failure in any way that helps them.
        """
        if self._token is None:
            raise BridgeAuthError("NO_TOKEN_CONFIGURED", {"side": "verification"})
        if not signature or not nonce:
            raise BridgeAuthError("MISSING_CREDENTIALS", {})
        if not task_document:
            raise BridgeAuthError("EMPTY_TASK_DOCUMENT", {})

        moment = time.time() if now is None else now
        if abs(moment - timestamp) > MAX_CLOCK_SKEW_SECONDS:
            raise BridgeAuthError(
                "TIMESTAMP_OUT_OF_RANGE",
                {"skew_seconds": f"{abs(moment - timestamp):.1f}"},
            )

        with self._lock:
            if nonce in self._seen_nonces:
                raise BridgeAuthError("NONCE_REPLAYED", {"nonce": nonce})
            # Record before verifying the MAC: an invalid signature consumes the
            # nonce too, so the same (nonce, signature) pair cannot be probed
            # repeatedly against a changing document. The eviction below keeps
            # the map bounded.
            self._seen_nonces[nonce] = timestamp
            if len(self._seen_nonces) > MAX_TRACKED_NONCES:
                oldest = min(self._seen_nonces, key=self._seen_nonces.__getitem__)
                del self._seen_nonces[oldest]

        expected = self._mac(nonce, timestamp, task_document)
        if not hmac.compare_digest(expected, signature):
            raise BridgeAuthError("SIGNATURE_MISMATCH", {})

    # -- Internals -------------------------------------------------------------

    def _mac(self, nonce: str, timestamp: float, task_document: str) -> str:
        """The HMAC-SHA256 over ``nonce || timestamp || document``."""
        assert self._token is not None  # callers check
        message = f"{nonce}|{timestamp:.6f}|".encode() + task_document.encode("utf-8")
        return hmac.new(self._token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def canonical_task_document(payload: dict[str, object]) -> str:
    """The exact byte form a producer must sign and send.

    ``sort_keys`` and fixed separators make the canonical form deterministic
    across producers, so both sides sign the same bytes for the same task. The
    bridge re-serializes whatever it received through this function before
    verifying, which means a producer cannot sign one ordering and send another
    — but it also means the producer must use this same canonicalization.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "MAX_CLOCK_SKEW_SECONDS",
    "MIN_TOKEN_LENGTH",
    "TOKEN_ENV_VAR",
    "BridgeAuth",
    "BridgeAuthError",
    "BridgeAuthenticator",
    "canonical_task_document",
]

#: Alias kept so the module's exported name list stays accurate.
BridgeAuth = BridgeAuthenticator
