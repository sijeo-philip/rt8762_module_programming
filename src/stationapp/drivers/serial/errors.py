"""Serial-driver exceptions."""


class SerialDriverError(RuntimeError):
    """Base error for station serial communication."""


class SerialOpenError(SerialDriverError):
    """Serial port could not be opened."""


class SerialWriteError(SerialDriverError):
    """Serial write failed."""


class SerialReadError(SerialDriverError):
    """Serial read failed."""


class SerialTimeoutError(SerialDriverError):
    """Expected serial response was not received in time."""


class SerialSessionClosedError(SerialDriverError):
    """Operation attempted on a closed serial session."""

    