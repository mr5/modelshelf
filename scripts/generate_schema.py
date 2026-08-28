import json
from pathlib import Path

from modelshelf_core import ArtifactManifest


def main() -> None:
    target = Path(__file__).parent.parent / "schemas/manifest.schema.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(ArtifactManifest.model_json_schema(by_alias=True), indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
