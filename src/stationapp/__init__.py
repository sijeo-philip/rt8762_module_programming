""" BLE Module programming and traceability station application.

This package is layered. Import rules (enforced by review, and later by a lint rule in Lesson 17):

ui/             ->  services/
services/       -> domain/, data/ drivers/
data/           -> domain/
drivers/        -> domain/
domain/         -> nothing from this package

'ui/' must never import 'data/' or 'drivers/' directly. That single rule is what allows the
LAN server (Stage 2) to arrive as a change to 'services/' only, with the UI untouched.
"""

__version__ = "0.1.0"
