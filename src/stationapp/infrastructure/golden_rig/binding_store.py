
"""Persistent Golden Rig binding store."""

from __future__ import annotations

import json
import os
import tempfile

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from stationapp.domain.golden_rig_binding import (GoldenRigBinding, GoldenRigBindingError)


class GoldenRigBindingStoreError(Exception):
    """Binding file cannot be read or written safely."""


class JsonGoldenRigBindingStore:

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> GoldenRigBinding | None:
        if not self._path.exists():
            return None

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))

            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")

            if set(data) != {"station_id", "rig_id", "host", "port", "certificate_sha256", "approved_by", "approved_at", "enabled"}:
                raise ValueError("Unexpected Golden Rig binding fields")

            data["approved_at"] = datetime.fromisoformat(data["approved_at"])

            return GoldenRigBinding(**data)

        except (OSError, ValueError, TypeError, GoldenRigBindingError) as exc:
            raise GoldenRigBindingStoreError("Golden Rig binding file is invalid or unreadable") from exc

    def save(self, binding: GoldenRigBinding) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

        data = asdict(binding)
        data["approved_at"] = (binding.approved_at.isoformat())
        temporary_name = None

        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._path.parent,
                prefix=".golden_rig_", suffix=".tmp", delete=False) as handle:
                temporary_name = handle.name
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temporary_name, self._path)

        except OSError as exc:
            raise GoldenRigBindingStoreError("Could not save Golden Rig binding") from exc

        finally:
            if (temporary_name is not None and os.path.exists(temporary_name)):
                os.unlink(temporary_name)
