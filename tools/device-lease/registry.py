"""DLP-1 device inventory and eligibility, with no discovery or command I/O.

Aliases are trusted inventory, not identity guesses from model or IP address.
Eligibility is only a candidate filter; it grants no lease or device permission.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path


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

    def __post_init__(self):
        for name in ("device_id", "pool"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be a nonempty, trimmed string")
        if not isinstance(self.aliases, (tuple, list)) or any(
            not isinstance(alias, str) or not alias.strip() or alias != alias.strip()
            for alias in self.aliases
        ):
            raise ValueError("aliases must be a list or tuple of nonempty, trimmed strings")
        object.__setattr__(self, "aliases", tuple(self.aliases))
        if type(self.restricted) is not bool or type(self.identity_confirmed) is not bool:
            raise ValueError("restricted and identity_confirmed must be booleans")
        if self.api is not None and (type(self.api) is not int or self.api < 1):
            raise ValueError("api must be a positive integer or null")
        for name in ("model", "abi", "restricted_policy"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or null")
        # CHE-2082/G5: renaming the canonical id must not bypass the serial restriction.
        if "500c592" in (self.device_id, *self.aliases):
            object.__setattr__(self, "restricted", True)


class DeviceRegistry:
    def __init__(self, devices: Iterable[Device] = ()):
        self._aliases: dict[str, Device] = {}
        for device in devices:
            keys = set((device.device_id, *device.aliases))
            if keys.intersection(self._aliases):
                raise ValueError("duplicate device id or ambiguous alias")
            self._aliases.update(dict.fromkeys(keys, device))
        if "500c592" not in self._aliases:
            self._aliases["500c592"] = Device(device_id="500c592", pool="root")

    def devices(self) -> list[Device]:
        """Every registered physical device once, ordered by canonical id."""
        unique = {device.device_id: device for device in self._aliases.values()}
        return [unique[key] for key in sorted(unique)]

    def resolve(self, alias: str) -> Device | None:
        """Resolve a canonical id, USB serial or inventoried wireless alias."""
        return self._aliases.get(alias)

    def eligible(self, alias: str, state: str | None, *, route: str = "raw_adb") -> bool:
        """Filter a fresh, trusted observation; never infer readiness from inventory.

        Restricted devices can only be candidates for CHE-2129's audited route.
        The consumer still enforces grants, claims, lease ownership and command policy.
        """
        device = self.resolve(alias)
        if device is None or state != "device" or not device.identity_confirmed:
            return False
        if device.restricted:
            return route == "che-2129" and device.restricted_policy == "che-2129"
        return route == "raw_adb"

    @classmethod
    def load(cls, path: str | Path) -> "DeviceRegistry":
        """Read a trusted UTF-8 JSON array. Missing/unreadable files propagate OSError."""
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("registry must be a JSON array of device objects")
        try:
            devices = [Device(**row) for row in rows]
        except TypeError as exc:
            raise ValueError("invalid device fields") from exc
        return cls(devices)
