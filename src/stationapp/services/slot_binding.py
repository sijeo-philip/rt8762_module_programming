"""Persistent physical-jig slot to USB-serial binding.

A jig slot is bound to a stable USB identity rather than to an unstable
Windows COM port.

The binding file is station configuration.  It is deliberately separate
from production genealogy stored in the database.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stationapp.drivers.serial.types import DiscoveredSerialPort
_BINDING_FORMAT_VERSION = 1

class SlotBindingError(Exception):
    """Base error for jig serial-slot binding."""

class SlotAlreadyBoundError(SlotBindingError):
    """Raised when attempting to overwrite a slot implicitly."""

class SerialIdentityAlreadyBoundError(SlotBindingError):
    """Raised when one USB identity is assigned to two jig positions."""

class InvalidBindingFileError(SlotBindingError):
    """Raised when a saved binding file is malformed or inconsistent."""

@dataclass(frozen=True, slots=True)
class SlotBinding:
    """Persistent identity assigned to one physical jig position."""

    slot_number: int
    stable_key: str

    # Metadata retained for diagnostics.
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None

    description: str | None = None
    manufacturer: str | None = None
    product: str | None = None

    def __post_init__(self) -> None:
        if self.slot_number <= 0:
            raise ValueError("slot_number must be positive")
        key = self.stable_key.strip()
        if not key:
            raise ValueError("stable_key cannot be empty")
        object.__setattr__(self, "stable_key", key)

    @classmethod
    def from_port(cls, slot_number: int, port: DiscoveredSerialPort) -> "SlotBinding":
        """Create a persistent slot binding from a discovered USB port."""

        return cls(
            slot_number=slot_number,
            stable_key=port.stable_key,
            vid=port.identity.vid,
            pid=port.identity.pid,
            serial_number=port.identity.serial_number,
            location=port.identity.location,
            description=port.description,
            manufacturer=port.manufacturer,
            product=port.product,
        )


@dataclass(frozen=True, slots=True)
class SlotBindingConfiguration:
    """Complete station/jig binding configuration."""

    station_id: str
    jig_id: str
    jig_positions: int
    bindings: tuple[SlotBinding, ...] = ()

    def __post_init__(self) -> None:
        station_id = self.station_id.strip()
        jig_id = self.jig_id.strip()

        if not station_id:
            raise ValueError("station_id cannot be empty")

        if not jig_id:
            raise ValueError("jig_id cannot be empty")

        if self.jig_positions not in {4, 8}:
            raise ValueError("jig_positions must be either 4 or 8")

        object.__setattr__(self, "station_id", station_id)
        object.__setattr__(self, "jig_id", jig_id)

        self._validate_bindings()

    def _validate_bindings(self) -> None:
        slots_seen: set[int] = set()
        identities_seen: set[str] = set()

        for binding in self.bindings:
            if binding.slot_number > self.jig_positions:
                raise ValueError(
                    f"Slot {binding.slot_number} exceeds jig capacity "
                    f"{self.jig_positions}"
                )

            if binding.slot_number in slots_seen:
                raise ValueError(
                    f"Duplicate binding for slot {binding.slot_number}"
                )

            if binding.stable_key in identities_seen:
                raise ValueError(
                    f"Serial identity {binding.stable_key} is bound "
                    "to more than one slot"
                )

            slots_seen.add(binding.slot_number)
            identities_seen.add(binding.stable_key)

    def get(self, slot_number: int) -> SlotBinding | None:
        for binding in self.bindings:
            if binding.slot_number == slot_number:
                return binding

        return None

    @property
    def bound_slot_numbers(self) -> tuple[int, ...]:
        return tuple(
            binding.slot_number
            for binding in sorted(
                self.bindings,
                key=lambda item: item.slot_number,
            )
        )

    @property
    def complete(self) -> bool:
        return len(self.bindings) == self.jig_positions


class SlotBindingService:
    """Loads, validates and atomically updates jig serial bindings."""

    def __init__(self, *, binding_file: Path, station_id: str, jig_id: str, jig_positions: int) -> None:
        self._binding_file = Path(binding_file)
        self._station_id = station_id.strip()
        self._jig_id = jig_id.strip()
        self._jig_positions = jig_positions

        if not self._station_id:
            raise ValueError("station_id cannot be empty")

        if not self._jig_id:
            raise ValueError("jig_id cannot be empty")

        if self._jig_positions not in {4, 8}:
            raise ValueError("jig_positions must be 4 or 8")

    @property
    def binding_file(self) -> Path:
        return self._binding_file

    def load(self) -> SlotBindingConfiguration:
        """Load binding configuration.

        If the file does not yet exist, or exists but is empty,
        return an empty configuration.
        """

        if not self._binding_file.exists():
            return self._empty_configuration()

        try:
            text = self._binding_file.read_text(
                encoding="utf-8"
            ).strip()

            # First-time setup may leave an empty placeholder file.
            if not text:
                return self._empty_configuration()

            raw = json.loads(text)

        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidBindingFileError(
                f"Could not read serial binding file "
                f"{self._binding_file}: {exc}"
            ) from exc

        return self._decode(raw)

    def bind(self, *, slot_number: int, port: DiscoveredSerialPort, replace: bool = False) -> SlotBindingConfiguration:
        """Bind one physical jig slot to a discovered USB serial interface."""

        self._validate_slot_number(slot_number)
        configuration = self.load()
        current = configuration.get(slot_number)
        if current is not None:
            if current.stable_key == port.stable_key:
                # Idempotent rebinding of the same physical interface.
                return configuration

            if not replace:
                raise SlotAlreadyBoundError(
                    f"Slot {slot_number} is already bound to "
                    f"{current.stable_key}"
                )

        for binding in configuration.bindings:
            if (
                binding.stable_key == port.stable_key
                and binding.slot_number != slot_number
            ):
                raise SerialIdentityAlreadyBoundError(
                    f"{port.stable_key} is already bound to "
                    f"slot {binding.slot_number}"
                )

        replacement = SlotBinding.from_port(slot_number, port)

        updated = [binding for binding in configuration.bindings if binding.slot_number != slot_number]
        updated.append(replacement)
        result = SlotBindingConfiguration(station_id=self._station_id, jig_id=self._jig_id, jig_positions=self._jig_positions,
            bindings=tuple(sorted(updated, key=lambda item: item.slot_number),
                        ),
                    )
        self.save(result)
        return result

    def unbind(self, slot_number: int) -> SlotBindingConfiguration:
        """Remove one slot binding."""
        self._validate_slot_number(slot_number)
        configuration = self.load()
        remaining = tuple(
            binding
            for binding in configuration.bindings
            if binding.slot_number != slot_number
        )

        result = SlotBindingConfiguration(
            station_id=self._station_id,
            jig_id=self._jig_id,
            jig_positions=self._jig_positions,
            bindings=remaining,
        )

        self.save(result)
        return result

    def save(self, configuration: SlotBindingConfiguration) -> None:
        """Atomically save the complete binding configuration."""

        self._validate_configuration_owner(configuration)
        payload = {
            "format_version": _BINDING_FORMAT_VERSION,
            "station_id": configuration.station_id,
            "jig_id": configuration.jig_id,
            "jig_positions": configuration.jig_positions,
            "bindings": [
                asdict(binding)
                for binding in sorted(
                    configuration.bindings,
                    key=lambda item: item.slot_number,
                )
            ],
        }

        self._binding_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._binding_file.with_suffix(self._binding_file.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")

            os.replace(temporary, self._binding_file)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _decode(self, raw: Any) -> SlotBindingConfiguration:
        if not isinstance(raw, dict):
            raise InvalidBindingFileError(
                "Binding file root must be a JSON object"
            )

        version = raw.get("format_version")
        if version != _BINDING_FORMAT_VERSION:
            raise InvalidBindingFileError(
                f"Unsupported serial binding format version "
                f"{version!r}; expected "
                f"{_BINDING_FORMAT_VERSION}"
            )

        try:
            station_id = str(raw["station_id"])
            jig_id = str(raw["jig_id"])
            jig_positions = int(raw["jig_positions"])
            raw_bindings = raw["bindings"]
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidBindingFileError(
                "Binding file is missing required fields"
            ) from exc

        if station_id != self._station_id:
            raise InvalidBindingFileError(
                f"Binding file belongs to station "
                f"{station_id!r}, not "
                f"{self._station_id!r}"
            )

        if jig_id != self._jig_id:
            raise InvalidBindingFileError(
                f"Binding file belongs to jig "
                f"{jig_id!r}, not "
                f"{self._jig_id!r}"
            )

        if jig_positions != self._jig_positions:
            raise InvalidBindingFileError(
                f"Binding file expects "
                f"{jig_positions} jig positions, "
                f"station is configured for "
                f"{self._jig_positions}"
            )

        if not isinstance(raw_bindings, list):
            raise InvalidBindingFileError(
                "'bindings' must be a JSON array"
            )

        try:
            bindings = tuple(
                SlotBinding(**item)
                for item in raw_bindings
            )

            return SlotBindingConfiguration(
                station_id=station_id,
                jig_id=jig_id,
                jig_positions=jig_positions,
                bindings=bindings,
            )

        except (TypeError, ValueError) as exc:
            raise InvalidBindingFileError(
                f"Invalid serial binding content: {exc}"
            ) from exc

    def _empty_configuration(self) -> SlotBindingConfiguration:
        return SlotBindingConfiguration(
            station_id=self._station_id,
            jig_id=self._jig_id,
            jig_positions=self._jig_positions,
        )

    def _validate_slot_number(self, slot_number: int) -> None:
        if not 1 <= slot_number <= self._jig_positions:
            raise ValueError(
                f"slot_number must be between 1 and "
                f"{self._jig_positions}"
            )

    def _validate_configuration_owner(self, configuration: SlotBindingConfiguration) -> None:
        if configuration.station_id != self._station_id:
            raise InvalidBindingFileError(
                "Cannot save bindings for another station"
            )

        if configuration.jig_id != self._jig_id:
            raise InvalidBindingFileError("Cannot save bindings for another jig")

        if configuration.jig_positions != self._jig_positions:
            raise InvalidBindingFileError(
                "Binding jig capacity does not match "
                "station configuration"
            )

        