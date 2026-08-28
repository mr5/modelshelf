from __future__ import annotations

import io
import sys

import pytest
from argon2 import PasswordHasher
from modelshelf_server.main import read_password, run
from modelshelf_server.password_hash import generate_password_hash


def test_generated_password_hash_is_argon2id_and_verifies() -> None:
    password_hash = generate_password_hash("correct horse battery staple")

    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PasswordHasher().verify(password_hash, "correct horse battery staple")


def test_hash_password_command_reads_explicit_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("correct horse battery staple\n"))

    run(["hash-password", "--stdin"])

    captured = capsys.readouterr()
    assert captured.out.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert "correct horse" not in captured.out
    assert not captured.err


def test_password_input_rejects_empty_and_requires_stdin_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ValueError, match="not a terminal"):
        read_password(read_stdin=False)
    with pytest.raises(ValueError, match="must not be empty"):
        generate_password_hash(read_password(read_stdin=True))
