"""Immutable run selection, independent of the current transport preference."""

from dataclasses import dataclass
from typing import Any, Mapping

from artemis.runtime.adb_endpoint import AdbEndpoint, AdbTarget


@dataclass(frozen=True, slots=True)
class RunDeviceBinding:
    target: AdbTarget
    bridge_session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target.serial, str) or not self.target.serial.strip():
            raise ValueError("Run binding requires a selected device")
        if self.target.host_id != self.target.endpoint.host_id:
            raise ValueError("Run binding host identity does not match its endpoint")
        if self.target.host_id and (not self.target.serial or self.bridge_session_id):
            raise ValueError("Host run binding requires one device and no browser lease")

    @property
    def device_lease(self) -> str:
        if self.bridge_session_id:
            return f"bridge:{self.bridge_session_id}"
        return self.target.lock_key

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.target.to_dict(),
            "bridge_session_id": self.bridge_session_id,
            "device_lease": self.device_lease,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RunDeviceBinding":
        binding = cls(
            AdbTarget(
                AdbEndpoint.from_mapping(data["endpoint"]),
                data.get("serial"),
                data.get("host_id"),
            ),
            data.get("bridge_session_id"),
        )
        if data.get("device_lease") != binding.device_lease:
            raise ValueError("Run binding device lease does not match its identity")
        return binding

    def require_selection(self, target: AdbTarget, *, recovery: bool = False) -> None:
        if (
            self.target.host_id != target.host_id
            or self.target.serial != target.serial
            or self.target.endpoint.identity != target.endpoint.identity
        ):
            raise ValueError("Run is bound to another device or endpoint identity")
        if self.target.endpoint != target.endpoint and not (
            recovery
            and self.target.host_id
            and target.endpoint.generation > self.target.endpoint.generation
        ):
            raise ValueError("Run binding rejects a stale or changed endpoint generation")
