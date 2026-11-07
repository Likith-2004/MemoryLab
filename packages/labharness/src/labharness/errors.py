"""Typed errors raised by the harness. Every failure path surfaces one of these."""


class HarnessError(Exception):
    """Base class for all harness errors."""


class InvalidAction(HarnessError):
    """An action failed closed-schema validation."""


class FrameLimitExceeded(HarnessError):
    """Executing the action would exceed the run's total frame budget."""


class StepLimitExceeded(HarnessError):
    """The run's step cap has been reached."""


class EmulatorFailure(HarnessError):
    """The emulator child process failed, crashed, or violated an invariant."""


class EmulatorTimeout(EmulatorFailure):
    """A single emulator call exceeded its wall-clock timeout; the child was killed."""


class BundleError(HarnessError):
    """A game bundle is missing, malformed, or fails hash/version verification."""


class RomError(BundleError):
    """The ROM header is invalid or the cartridge type is refused."""


class StateMapError(BundleError):
    """The state map is malformed or fails address validation."""


class StateParseError(HarnessError):
    """RAM contents could not be normalised under the state map."""


class CheckpointError(HarnessError):
    """A checkpoint was requested or restored outside the allowed conditions."""
