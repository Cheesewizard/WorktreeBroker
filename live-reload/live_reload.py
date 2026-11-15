"""Prepare one copied Mono player and publish a C# patch to a single bot run."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from broker_state import PROJECT, REPOSITORY, fill  # noqa: E402 - the broker folder is the parent

# Everything game-specific comes from project.json's "player" section: the bridge source compiled into the copied
# player, the assemblies it and the patches compile against, and the game method the bridge is hooked into.
PLAYER = PROJECT.get("player", {})
BASE_REFERENCES = ("mscorlib", "System", "System.Core", "netstandard", "UnityEngine.CoreModule")
REFERENCES = (*BASE_REFERENCES, *PLAYER.get("references", []))
BRIDGE_ASSEMBLY = PLAYER.get("bridge_assembly", "WorktreeBroker.PlayerBridge")


def bridge_source():
    source = PLAYER.get("bridge_source")
    if not source:
        raise SystemExit("project.json has no player.bridge_source: the standalone patch prototype is not configured.")
    return Path(fill(source, broker=HERE.parent, main=REPOSITORY))


def compile_source(source, output, managed):
    sdk = Path(subprocess.check_output(
        ["dotnet", "--list-sdks"], text=True, timeout=10
    ).splitlines()[-1].split("[", 1)[1].rstrip("]"))
    compiler = sorted(sdk.glob("*/Roslyn/bincore/csc.dll"), key=lambda path: path.stat().st_mtime)[-1]
    subprocess.run([
        "dotnet", str(compiler), "/nologo", "/noconfig", "/nostdlib+", "/target:library",
        f"/out:{output}", *(f"/reference:{managed / (name + '.dll')}" for name in REFERENCES),
        str(source),
    ], check=True, timeout=90)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "refresh-bridge", "patch"))
    parser.add_argument("--player", type=Path, required=True)
    parser.add_argument("--source-player", type=Path)
    parser.add_argument("--cecil", type=Path)
    parser.add_argument("--patch-file", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--artwork", type=Path)
    args = parser.parse_args()
    player = args.player.resolve()
    managed = player.parent / (player.stem + "_Data") / "Managed"

    if args.action == "refresh-bridge":
        compile_source(bridge_source(), managed / f"{BRIDGE_ASSEMBLY}.dll", managed)
        return

    if args.action == "prepare":
        if args.source_player is None or args.cecil is None:
            parser.error("prepare requires --source-player and --cecil")
        source_player = args.source_player.resolve(strict=True)
        if player.name != source_player.name:
            parser.error("the copied player must use the original executable name")
        hook = PLAYER.get("hook") or {}
        if not all(hook.get(key) for key in ("assembly", "type", "method", "bridge_type")):
            parser.error("project.json player.hook needs assembly, type, method and bridge_type")
        shutil.copytree(source_player.parent, player.parent)
        compile_source(bridge_source(), managed / f"{BRIDGE_ASSEMBLY}.dll", managed)
        subprocess.run([
            "dotnet", "run", "--project", str(HERE / "PreparePlayer.csproj"),
            f"-p:UnityCecil={args.cecil.resolve(strict=True)}", "--", str(managed),
            *(hook[key] for key in ("assembly", "type", "method")), BRIDGE_ASSEMBLY, hook["bridge_type"],
        ], check=True, timeout=120)
        print(f"Prepared {player}")
        return

    if args.patch_file is None or args.source is None or args.artwork is None:
        parser.error("patch requires --patch-file, --source and --artwork")
    patch = args.patch_file.resolve()
    if patch.exists():
        parser.error("this player run already has a published patch")
    temp = patch.with_name(patch.stem + ".pending.dll")
    source = args.source.resolve(strict=True)
    artwork = args.artwork.resolve(strict=True)
    start = time.perf_counter()
    compile_source(source, temp, managed)
    shutil.copyfile(artwork, patch.with_name(PLAYER.get("patch_artwork_name", "artwork.png")))
    os.replace(temp, patch)
    published = time.perf_counter()
    print(f"Compiled and published {patch} in {published - start:.3f}s", flush=True)
    screenshot = patch.with_name("patched.png")
    while not screenshot.exists() and time.perf_counter() - published < 60:
        time.sleep(0.02)
    if not screenshot.exists():
        raise RuntimeError("Player did not capture the patched frame within 60 seconds")
    print(f"Patched frame captured in {time.perf_counter() - start:.3f}s total")


if __name__ == "__main__":
    main()
