from getpass import getpass

from modelshelf_server.password_hash import generate_password_hash


def main() -> None:
    first = getpass("Admin password: ")
    second = getpass("Confirm password: ")
    if not first or first != second:
        raise SystemExit("passwords are empty or do not match")
    print(generate_password_hash(first))


if __name__ == "__main__":
    main()
