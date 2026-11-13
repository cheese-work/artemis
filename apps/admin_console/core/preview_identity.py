from starlette.requests import HTTPConnection

from apps.admin_console.core.access_control import AccessConfig, AccessIdentity, AdminAPIError
from apps.admin_console.core.preview_fixtures import preview_owners
from apps.admin_console.core.preview_profile import preview_identity_switch_selected

PREVIEW_ISSUER = "urn:artemis:preview"
PREVIEW_IDENTITY_HEADER = "X-Artemis-Preview-Identity"
PREVIEW_IDENTITY_COOKIE = "artemis_preview_identity"


def _fixture(alias: str, email: str, admin: bool) -> AccessIdentity:
    """A fixture identity lives in its own issuer namespace (docs/spaces-contract.md).

    It has no principal row, so it never reserves an email or binds a membership.
    """
    return AccessIdentity(
        email, admin, "preview", issuer=PREVIEW_ISSUER, subject=f"preview:{alias}"
    )


def configure_preview_identities(
    config: AccessConfig, *, preview_profile: bool
) -> dict[str, AccessIdentity]:
    if not preview_identity_switch_selected(preview_profile):
        return {}
    if config.auth_mode != "cloudflare":
        raise ValueError("Fixture identities require cloudflare ownership configuration.")
    qa_emails, admin_email = preview_owners(config)
    return {
        "qa-a": _fixture("qa-a", qa_emails[0], False),
        "qa-b": _fixture("qa-b", qa_emails[1], False),
        "admin": _fixture("admin", admin_email, True),
    }


def resolve_preview_identity(request: HTTPConnection) -> AccessIdentity | None:
    headers = request.headers.getlist(PREVIEW_IDENTITY_HEADER)
    cookie = request.cookies.get(PREVIEW_IDENTITY_COOKIE)
    identities = getattr(request.app.state, "preview_identities", {})
    enabled = getattr(request.app.state, "preview_profile", False) is True and bool(identities)
    if not enabled:
        if headers or cookie is not None:
            raise AdminAPIError(
                403,
                "Fixture identities are disabled outside an opted-in isolated preview.",
                "preview_identity_disabled",
                "Use Cloudflare Access, or an explicitly opted-in synthetic preview.",
            )
        return None
    if not headers and cookie is None:
        return AccessIdentity(None, False, "preview", "no_preview_identity")
    alias = headers[0] if headers else cookie
    if (
        len(headers) > 1
        or (headers and cookie is not None and cookie != alias)
        or alias not in identities
    ):
        raise AdminAPIError(
            400,
            "Select exactly one fixture identity: qa-a, qa-b or admin.",
            "preview_identity_invalid",
            "Use one preview identity header or a matching preview cookie.",
        )
    return identities[alias]
