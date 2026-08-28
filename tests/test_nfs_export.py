import re
from pathlib import Path


def test_ganesha_exports_only_the_artifact_directory() -> None:
    template = (
        Path(__file__).parent.parent / "docker" / "nfs" / "ganesha.conf.template"
    ).read_text(encoding="utf-8")
    exported_paths = re.findall(r"^\s*Path\s*=\s*([^;]+);", template, flags=re.MULTILINE)

    assert exported_paths == ["/export/artifacts"]
    assert "/export/.modelshelf" not in template
