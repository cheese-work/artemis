import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from apps.admin_console.core.access_control import AccessIdentity, AdminAPIError, require_admin
from apps.admin_console.services.config_store import ConfigStoreError
from apps.admin_console.services.provider_settings import get_provider_settings

router = APIRouter(
    prefix="/api/system/providers", tags=["providers"], dependencies=[Depends(require_admin)]
)


class ProviderWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str
    base_url: str | None = None
    key: SecretStr | None = None
    enabled: bool | None = None


def _api_error(error: ConfigStoreError) -> AdminAPIError:
    return AdminAPIError(
        error.status_code,
        error.detail,
        error.code,
        "Reload provider settings, correct the values, and retry.",
    )


@router.get("")
async def read_providers():
    try:
        return await get_provider_settings().read()
    except ConfigStoreError as exc:
        raise _api_error(exc) from exc


@router.put("/{provider}")
async def save_provider(
    provider: str, request: Request, identity: AccessIdentity = Depends(require_admin)
):
    try:
        payload = ProviderWrite.model_validate(await request.json())
    except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AdminAPIError(
            400,
            "Invalid provider settings payload.",
            "provider_invalid",
            "Send a version, URL, write-only key, and enabled flag.",
        ) from exc
    try:
        providers = get_provider_settings()
        snapshot = await providers.save(
            provider,
            payload.version,
            actor=identity.email,
            base_url=payload.base_url,
            key=payload.key.get_secret_value() if payload.key is not None else None,
            enabled=payload.enabled,
        )
        await providers.activate_runtime()
        return snapshot
    except ConfigStoreError as exc:
        raise _api_error(exc) from exc


@router.post("/{provider}/test")
async def test_provider(provider: str):
    try:
        return await get_provider_settings().test(provider)
    except ConfigStoreError as exc:
        raise _api_error(exc) from exc


@router.post("/{provider}/sync")
async def sync_provider(provider: str):
    try:
        return await get_provider_settings().sync(provider)
    except ConfigStoreError as exc:
        raise _api_error(exc) from exc
