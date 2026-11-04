import json
import os
from pathlib import Path
import stat
import time

from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import PyJWTError

from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier

MAX_BUNDLE_AGE_SECONDS = 3600
MAX_BUNDLE_BYTES = 65536
_PRIVATE_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "oth"}


class PreviewKeyBundle:
    def __init__(self, path: Path, issuer: str):
        self.path = path
        self.issuer = issuer

    def fetch(self, url: str) -> dict:
        if url != f"{self.issuer}/cdn-cgi/access/certs":
            raise ValueError("Preview JWKS issuer does not match the configured issuer.")
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Preview JWKS bundle must be a regular file.")
            data = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError("Preview JWKS bundle exceeds the size limit.")
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("issuer") != self.issuer:
            raise ValueError("Preview JWKS issuer does not match the configured issuer.")
        fetched_at = document.get("fetched_at")
        now = time.time()
        if (
            isinstance(fetched_at, bool)
            or not isinstance(fetched_at, (float, int))
            or not now - MAX_BUNDLE_AGE_SECONDS <= fetched_at <= now
        ):
            raise ValueError("Preview JWKS bundle is stale or has an invalid timestamp.")
        keys = document.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
            raise ValueError("Preview JWKS bundle has no usable public keys.")
        key_ids = set()
        for key in keys:
            if (
                not isinstance(key, dict)
                or key.get("kty") != "RSA"
                or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig"
                or not isinstance(key.get("kid"), str)
                or not key["kid"]
                or key["kid"] in key_ids
                or _PRIVATE_FIELDS.intersection(key)
            ):
                raise ValueError("Preview JWKS bundle contains an invalid public key.")
            key_ids.add(key["kid"])
            try:
                RSAAlgorithm.from_jwk(json.dumps(key))
            except (TypeError, ValueError, PyJWTError) as exc:
                raise ValueError("Preview JWKS bundle contains an invalid RSA public key.") from exc
        return {"keys": keys}


def preview_access_verifier(config: AccessConfig) -> CloudflareAccessVerifier:
    if config.auth_mode != "cloudflare" or not config.issuer or not config.audience:
        raise ValueError(
            "A preview requires ARTEMIS_AUTH_MODE=cloudflare with issuer and audience."
        )
    path = os.environ.get("ARTEMIS_PREVIEW_JWKS_BUNDLE", "")
    if not path or not Path(path).is_absolute():
        raise ValueError("ARTEMIS_PREVIEW_JWKS_BUNDLE must name an absolute public-key bundle.")
    bundle = PreviewKeyBundle(Path(path), config.issuer)
    bundle.fetch(f"{config.issuer}/cdn-cgi/access/certs")
    return CloudflareAccessVerifier(fetch_jwks=bundle.fetch, ttl_seconds=0)
