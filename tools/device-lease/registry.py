"""DLP-1 registry API scaffold for the first behavioral red run."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    device_id: str
    aliases: tuple[str, ...] = ()
    pool: str = "rig"
    model: str | None = None
    api: int | None = None
    abi: str | None = None
    restricted: bool = True
    identity_confirmed: bool = False
    restricted_policy: str | None = None


class DeviceRegistry:
    def __init__(self, devices=()):
        pass

    def resolve(self, alias):
        return None

    def eligible(self, alias, state, *, route="raw_adb"):
        return True

    @classmethod
    def load(cls, path):
        return cls()
