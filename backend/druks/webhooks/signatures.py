import hashlib
import hmac

from fastapi import HTTPException, status


def verify_hmac_sha256(
    raw_body: bytes,
    signature: str | None,
    secret: str,
    *,
    prefix: str = "sha256=",
) -> None:
    """Verify a GitHub-style HMAC-SHA256 signature header.

    Raises ``HTTPException`` itself, so that a handler can call it inline from
    ``request_is_authentic``.
    """
    if not secret:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Druks has no webhook secret for this provider. Set the webhook secret of its service.",
        )
    if not signature:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The request has no webhook signature. Make sure that the webhook of the provider "
            "has the secret that Druks has.",
        )

    expected = prefix + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The webhook signature is not correct. Make sure that the webhook of the provider "
            "has the secret that Druks has.",
        )
