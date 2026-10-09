
"""Authorize a Golden Rig transport for the current station."""

from __future__ import annotations
from pathlib import Path
from stationapp.infrastructure.golden_rig.secure_transport import (
    SecureGoldenRigTransport,
)
from stationapp.services.golden_rig_secure_binding import (
    SecureGoldenRigBindingService,
)


class GoldenRigAuthorizationService:
    """Build secure transports only from approved bindings."""

    def __init__(self, binding_service: SecureGoldenRigBindingService, ca_certificate: Path) -> None:
        self._binding_service = binding_service
        self._ca_certificate = ca_certificate

    def create_transport(self) -> SecureGoldenRigTransport:
        binding = self._binding_service.require_binding()
        return SecureGoldenRigTransport(binding=binding, ca_certificate=self._ca_certificate)



    
