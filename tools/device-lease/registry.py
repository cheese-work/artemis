"""DLP-1 device inventory and eligibility, with no discovery or command I/O.

Aliases are trusted inventory, not identity guesses from model or IP address.
Eligibility is only a candidate filter; it grants no lease or device permission.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class AdmissionSnapshot:
    """Validated owner readiness, including the authority for each endpoint."""

    endpoints: dict[str, str]
    grants: dict[str, dict]


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
    kind: str = "physical"
    avd_name: str | None = None
    avd_data_path: str | None = None
    avd_definition_path: str | None = None
    console_port: int | None = None
    adb_port: int | None = None
    emulator_path: str | None = None
    emulator_sha256: str | None = None
    config_sha256: str | None = None
    image_sha256: str | None = None

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
        if self.kind not in ("physical", "avd"):
            raise ValueError("kind must be physical or avd")
        lifecycle = (
            self.avd_name,
            self.avd_data_path,
            self.avd_definition_path,
            self.console_port,
            self.adb_port,
            self.emulator_path,
            self.emulator_sha256,
            self.config_sha256,
            self.image_sha256,
        )
        if self.kind == "physical":
            if self.pool == "avd" or any(value is not None for value in lifecycle):
                raise ValueError("physical devices cannot carry AVD lifecycle metadata or pool")
            return
        if self.pool != "avd":
            raise ValueError("AVD requires the dedicated avd pool")
        if not isinstance(self.avd_name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.avd_name
        ):
            raise ValueError("avd_name must be a literal AVD name")
        if (
            self.api is None
            or not isinstance(self.abi, str)
            or not self.abi.strip()
            or self.abi != self.abi.strip()
        ):
            raise ValueError("AVD requires a pinned API and ABI")
        for name in ("avd_data_path", "avd_definition_path", "emulator_path"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or "\x00" in value
            ):
                raise ValueError(f"{name} must be an absolute canonical path")
            try:
                path = Path(value)
                canonical = str(path.resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"{name} cannot be resolved") from exc
            if not path.is_absolute() or value != canonical or value == path.anchor:
                raise ValueError(f"{name} must be an absolute canonical path")
        if (
            type(self.console_port) is not int
            or not 5554 <= self.console_port <= 5682
            or self.console_port % 2
            or type(self.adb_port) is not int
            or self.adb_port != self.console_port + 1
        ):
            raise ValueError("AVD requires an even console port (5554..5682) and adjacent ADB port")
        serial = f"emulator-{self.console_port}"
        endpoints = {serial, f"127.0.0.1:{self.adb_port}"}
        if (
            serial not in self.aliases
            or not set(self.aliases) <= endpoints
            or len(set(self.aliases)) != len(self.aliases)
            or self.device_id in endpoints
        ):
            raise ValueError("AVD aliases must be distinct endpoints for its fixed port pair")
        for name in ("emulator_sha256", "config_sha256", "image_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")


class DeviceRegistry:
    def __init__(self, devices: Iterable[Device] = ()):
        self._aliases: dict[str, Device] = {}
        avd_seen = False
        for device in devices:
            if device.kind == "avd":
                if avd_seen:
                    raise ValueError("only one canonical AVD slot is supported")
                avd_seen = True
            keys = set((device.device_id, *device.aliases))
            if keys.intersection(self._aliases):
                raise ValueError("duplicate device id or ambiguous alias")
            self._aliases.update(dict.fromkeys(keys, device))
        if "500c592" not in self._aliases:
            self._aliases["500c592"] = Device(device_id="500c592", pool="root")

    def devices(self) -> list[Device]:
        """Every registered device once, ordered by canonical id."""
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
        # Lifecycle metadata is not a verified startup/work lease. Until that
        # route is implemented, even an ordinary admitted-map entry must refuse.
        if (
            device is None
            or device.kind == "avd"
            or state != "device"
            or not device.identity_confirmed
        ):
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
