"""MPCLI driver-specific errors."""


class MpCliError(Exception):
    """Base class for MPCLI driver errors."""


class MpCliExecutableNotFound(MpCliError):
    """Configured MPCLI executable does not exist."""


class MpCliProcessStartError(MpCliError):
    """Operating system could not start MPCLI."""


class MpCliInvalidCommand(MpCliError):
    """An invalid MPCLI command was requested."""