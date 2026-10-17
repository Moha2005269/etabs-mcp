"""Build the Claude Desktop extension (.mcpb): bundle the package and its dependencies into mcpb/server/lib,
validate the manifest and pack it.

    python tools/build_mcpb.py [--python 3.12] [path/to/etabs_mcp.whl]

Compiled dependencies (pywin32, numpy, pydantic-core) are tied to one Python minor version, so one bundle is
built per version and declares that runtime in its manifest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A short staging path keeps deep dependency trees under Windows' 260-character path limit.
STAGE = Path(tempfile.gettempdir()) / "etabs-mcpb"


def main() -> None:
    args = sys.argv[1:]
    py = "3.12"
    if args[:1] == ["--python"]:
        py, args = args[1], args[2:]
    wheel = Path(args[0]) if args else max((ROOT / "dist").glob("etabs_mcp-*.whl"))
    shutil.rmtree(STAGE, ignore_errors=True)
    shutil.copytree(ROOT / "mcpb", STAGE)
    manifest = json.loads((STAGE / "manifest.json").read_text())
    version = manifest["version"]
    major, minor = py.split(".")
    manifest["compatibility"]["runtimes"]["python"] = f">={py},<{major}.{int(minor) + 1}"
    (STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if f"-{version}-" not in wheel.name:
        raise SystemExit(f"manifest version {version} does not match {wheel.name}")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(STAGE / "server" / "lib"),
                    "--platform", "win_amd64", "--only-binary=:all:", "--python-version", py, str(wheel)],
                   check=True)
    shutil.copy(ROOT / "LICENSE", STAGE / "LICENSE")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    subprocess.run([npx, "-y", "@anthropic-ai/mcpb@latest", "validate", str(STAGE / "manifest.json")], check=True)
    out = ROOT / "dist" / f"etabs-mcp-{version}-py{major}{minor}.mcpb"
    subprocess.run([npx, "-y", "@anthropic-ai/mcpb@latest", "pack", str(STAGE), str(out)], check=True)
    print(f"built {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
