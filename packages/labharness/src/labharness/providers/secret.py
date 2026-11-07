"""A holder for an API key that cannot be printed, pickled, or logged by accident."""

from typing import NoReturn


class SecretKey:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not 8 <= len(value) <= 512 or not value.isascii() \
                or not value.isprintable() or any(c.isspace() for c in value):
            raise ValueError("API key has an invalid shape")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The only way to read the key; call it only to build a request header."""
        value: str = object.__getattribute__(self, "_value")
        return value

    def __repr__(self) -> str:
        return "SecretKey(****)"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return "SecretKey(****)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretKey cannot be pickled or copied across processes")

    def __copy__(self) -> NoReturn:
        raise TypeError("SecretKey cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("SecretKey cannot be copied")

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("SecretKey is immutable")

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)
