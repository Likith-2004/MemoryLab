import pytest

from runlog.envfile import secret_env


def test_a_plain_value_a_file_and_a_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "s"
    f.write_text("from-file\n")
    assert secret_env("X", {"X": "plain"}) == "plain"
    assert secret_env("X", {"X_FILE": str(f)}) == "from-file"          # the trailing newline of the file is not part of the secret
    assert secret_env("X", {}, default="d") == "d"
    assert secret_env("X", {"X": ""}, default="d") == ""               # a present empty value is not "missing"


def test_missing_and_ambiguous_fail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "s"
    f.write_text("v")
    with pytest.raises(KeyError):
        secret_env("X", {})
    with pytest.raises(ValueError):
        secret_env("X", {"X": "a", "X_FILE": str(f)})
    with pytest.raises(FileNotFoundError):
        secret_env("X", {"X_FILE": str(tmp_path / "nope")})
