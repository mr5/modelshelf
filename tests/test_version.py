from importlib.metadata import version

from modelshelf_server import __version__, _display_version


def test_server_version_comes_from_installed_distribution() -> None:
    assert _display_version("0.1.0b18") == "0.1.0-beta.18"
    assert __version__ == _display_version(version("modelshelf-server"))
