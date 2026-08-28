from __future__ import annotations

from argon2 import PasswordHasher

_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def generate_password_hash(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return _HASHER.hash(password)
