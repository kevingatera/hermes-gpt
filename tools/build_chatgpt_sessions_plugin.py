"""Build a local Hermes Sessions plugin with a registered ChatGPT app ID."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

_APP_ID_RE = re.compile(r"asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_APP_ALIAS = "hermes-gpt"


def normalize_app_id(value: str) -> str:
    """Convert the ChatGPT URL ID to the canonical ID stored in `.app.json`."""
    app_id = value.strip()
    # ChatGPT shows `plugin_asdk_app_...`; `.app.json` stores `asdk_app_...`.
    if app_id.startswith("plugin_"):
        app_id = app_id.removeprefix("plugin_")
    if not _APP_ID_RE.fullmatch(app_id):
        raise ValueError(
            "app ID must be a ChatGPT plugin_asdk_app_... or asdk_app_... ID"
        )
    return app_id


def build_package(app_id: str, output: Path) -> Path:
    """Copy the portable plugin source and add its account-specific app binding."""
    canonical_app_id = normalize_app_id(app_id)
    source = Path(__file__).resolve().parents[1] / "plugins" / "hermes-sessions"
    destination = output.expanduser().resolve()

    if destination == source or source in destination.parents:
        raise ValueError("output must be outside the plugin source directory")
    if destination.exists():
        raise FileExistsError(f"output already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)

    manifest_path = destination / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extensions"]["com.openai"]["apps"] = "./.app.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    app_manifest = {
        "apps": {
            _APP_ALIAS: {
                "id": canonical_app_id,
            }
        }
    }
    (destination / ".app.json").write_text(
        json.dumps(app_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="ChatGPT Developer Mode app ID")
    parser.add_argument(
        "--output", required=True, type=Path, help="new package output directory"
    )
    args = parser.parse_args()

    try:
        output = build_package(args.app_id, args.output)
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    print(f"Created Hermes Sessions plugin package at {output}")


if __name__ == "__main__":
    main()
