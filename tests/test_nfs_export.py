import re
from pathlib import Path


def test_ganesha_exports_only_the_artifact_directory() -> None:
    template = (
        Path(__file__).parent.parent / "docker" / "nfs" / "ganesha.conf.template"
    ).read_text(encoding="utf-8")
    exported_paths = re.findall(r"^\s*Path\s*=\s*([^;]+);", template, flags=re.MULTILINE)

    assert exported_paths == ["/export/artifacts"]
    assert "/export/.modelshelf" not in template
    assert "Allow_Set_Io_Flusher_Fail = true" in template
    assert "DisableDirCaching" not in template


def test_public_cidr_is_normalized_only_after_explicit_opt_in() -> None:
    entrypoint = (
        Path(__file__).parent.parent / "docker" / "nfs" / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert 'MODELSHELF_NFS_ALLOW_PUBLIC:-false' in entrypoint
    assert '*,0.0.0.0/0,*|*,::/0,*) ganesha_clients="*"' in entrypoint
    assert "s|__CLIENTS__|$ganesha_clients|g" in entrypoint
