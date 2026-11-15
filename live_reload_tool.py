"""ui_live_reload and ui_capture: reload a view's C# into the one shared editor and see the result.

Talks to Unity's com.unity.pipeline server in the main checkout's editor (loopback HTTP; port and token
from Library/Pipeline/.unity-pipeline-port). Works from any worktree: the editor compiles the file from
the worktree's own path, so the main checkout's files are never written. See README "UI live reload".
"""

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from uuid import uuid4

from broker_state import HERE, PROJECT, fill, read_json
import integrate
import capture_view

DESCRIPTOR = Path("Library") / "Pipeline" / ".unity-pipeline-port"
BACKENDS = {
    "interpreter": "script.methodreload reload_editor",
    "compiled": "script.methodreload reload",
}
# The capture and live-style hooks are C# run in the editor ({path}/{width}/{height} and {asset}/{file} slots). They
# default to the broker's Unity package; project.json's "editor" section can replace any of them. A capture hook
# renders the game to a texture (so it works with the editor unfocused) and may write `layout/<capture>.json` beside the
# PNG for capture_view.py's element and region views.
DEFAULT_HOOKS = {
    "capture": 'return WorktreeBroker.Editor.BrokerCapture.ForBroker(@"{path}", {width}, {height});',
    "style_apply": 'return WorktreeBroker.Editor.LiveStyles.Apply(@"{asset}", @"{file}");',
    "style_revert": "return WorktreeBroker.Editor.LiveStyles.RevertAll();",
}
EDITOR_HOOKS = {**DEFAULT_HOOKS, **PROJECT.get("editor", {})}


def editor_hook(name, **values):
    template = EDITOR_HOOKS.get(name)
    if not template:
        raise RuntimeError(f'project.json has no editor.{name}: this project has not configured that hook '
                           "(see project.example.json and the README's \"Project configuration\").")
    return fill(template, **values)


CLEAR_RELOADS_CODE = (
    "var before = Unity.Pipeline.MethodReload.MethodReloadRegistry.GetStats().ActiveOverrideCount;"
    " Unity.Pipeline.MethodReload.MethodReloadRegistry.ClearAllOverrides();"
    " return \"cleared \" + before + \" C# reload(s)\";"
)

RELOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "Saved .cs file in this worktree, e.g. Assets/Scripts/UI/MainMenuView.cs"},
        "styles": {"type": "array", "items": {"type": "string"},
                   "description": "Saved .uss files in this worktree (same Assets/... path as in the main project), applied in memory before the C# reload."},
        "integration": {"type": "string", "description": "An id from integrate: resolve source and styles inside that combined tree (several worktrees overlaid on main) instead of this worktree."},
        "backend": {"enum": sorted(BACKENDS), "description": "interpreter (default): private members allowed. compiled: full C#, public members only."},
        "capture": {"type": "boolean", "description": "Capture the game after the reload (default true)."},
        "width": {"type": "integer", "minimum": 320, "maximum": 3840},
        "height": {"type": "integer", "minimum": 180, "maximum": 2160},
        **capture_view.SCHEMA_PROPERTIES,
    },
    "additionalProperties": False,
}
REVERT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
CAPTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "width": {"type": "integer", "minimum": 320, "maximum": 3840},
        "height": {"type": "integer", "minimum": 180, "maximum": 2160},
        **capture_view.SCHEMA_PROPERTIES,
    },
    "additionalProperties": False,
}


def main_worktree():
    import broker_state

    route = broker_state.main_route()
    return Path(route["worktree"]).resolve()


class Pipeline:
    def __init__(self, project):
        path = project / DESCRIPTOR
        if not path.is_file():
            raise RuntimeError("Main Unity has no Pipeline server. Is the editor open, and is com.unity.pipeline installed?")
        descriptor = read_json(path)
        beat = datetime.fromisoformat(descriptor["lastHeartbeat"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - beat).total_seconds() > 60:
            raise RuntimeError("Main Unity's Pipeline server has not checked in for a minute (closed, frozen or reloading).")
        self.base = f"http://127.0.0.1:{descriptor['port']}"
        self.token = descriptor["evalToken"]

    def request(self, path, body=None, timeout=90):
        request = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return json.loads(reply.read() or b"null")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            try:
                return json.loads(text)
            except ValueError:
                raise RuntimeError(f"Pipeline HTTP {error.code}: {text[:500]}") from error

    def run(self, command, **parameters):
        reply = self.request("/api/exec", {"command": command, "parameters": parameters})
        if not reply.get("success"):
            raise RuntimeError(f"{command}: {reply.get('error')}: {reply.get('errorDetails', '')}".strip())
        return reply.get("result")


def checked_file(root, relative, suffix):
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.lower() != suffix:
        raise ValueError(f"{relative}: expected a saved {suffix} file inside this worktree")
    return path


def checked_style(root, relative):
    path = checked_file(root, relative, ".uss")
    asset = path.relative_to(root).as_posix()
    if not asset.startswith("Assets/"):
        raise ValueError(f"{relative}: stylesheets must be under Assets/")
    return asset, path


def require_playing(pipeline):
    status = pipeline.request("/api/status")
    if status.get("status") != "ready":
        raise RuntimeError(f"Main Unity is busy ({status.get('busyReason') or status.get('status')}). Try again shortly.")
    # Pipeline 0.10's /api/status omits playMode despite its docs; the editor status command has it.
    editor = pipeline.run("editor status")
    if editor.get("compiling") or editor.get("domainReloadInProgress"):
        raise RuntimeError("Main Unity is compiling or reloading. Try again when it settles.")
    # "paused" counts: a capture hook may leave Play mode paused after stepping frames (stepping frames by hand).
    if editor.get("playMode") not in {"playing", "paused"}:
        raise RuntimeError("Main Unity is not in Play mode. Live reload needs a running game: enter Play mode and open the screen first.")


def console_cursor(pipeline):
    result = pipeline.run("console get", tail=0)
    return result["cursor"], result["session"]


def console_since(pipeline, cursor):
    result = pipeline.run("console get", tail=50, level="warn", since=cursor[0], **{"since-session": cursor[1]})
    return [f"{item['level']}: {item['message'].splitlines()[0][:300]}" for item in result.get("entries", [])]


def capture(pipeline, route_name, width, height):
    folder = HERE / ".runs" / route_name / "captures"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}.png"
    code = editor_hook("capture", path=str(path), width=int(width), height=int(height))
    result = pipeline.run("script eval", code=code, timeout=30)
    if not path.is_file():
        raise RuntimeError(f"The capture did not produce a file: {result}")
    return path, (result or {}).get("result")


def live_reload(root, route_name, source=None, styles=(), backend="interpreter", capture_after=True, width=1280, height=720,
                integration=None, view="fit", region=None, element=None):
    """Returns (summary dict, PNG bytes or None). With integration, files resolve inside that integrate tree, not root."""
    if integration:
        root = integrate.integration_tree(integration)
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {sorted(BACKENDS)}")
    if not source and not styles:
        raise ValueError("Give a .cs source, one or more .uss styles, or both")
    path = checked_file(root, source, ".cs") if source else None
    sheets = [checked_style(root, item) for item in styles]
    pipeline = Pipeline(main_worktree())
    require_playing(pipeline)
    cursor = console_cursor(pipeline)
    summary = {"backend": backend}
    if sheets:
        started = time.perf_counter()
        results = []
        for asset, file in sheets:
            reply = pipeline.request("/api/exec", {"command": "script eval", "parameters": {
                "code": editor_hook("style_apply", asset=asset, file=str(file)), "timeout": 30}})
            result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            if not reply.get("success") or result.get("success") is False:
                detail = reply.get("errorDetails") or result.get("error") or result.get("diagnostics") or reply
                summary["styles_error"] = f"{asset}: {detail}"
                summary["console"] = console_since(pipeline, cursor)
                raise RuntimeError(json.dumps(summary, indent=2, default=str))
            results.append(result.get("result"))
        summary["styles"] = results
        summary["styles_seconds"] = round(time.perf_counter() - started, 3)
    if path:
        started = time.perf_counter()
        reload = pipeline.run(BACKENDS[backend], filename=str(path))
        summary.update({
            "source": str(path),
            "reloaded": reload.get("Items", []), "diagnostics": reload.get("Diagnostics", []),
            "reload_seconds": round(time.perf_counter() - started, 3),
        })
        if not reload.get("success"):
            summary["error"] = f"{reload.get('error')}: {reload.get('errorDetails', '')}"
            summary["console"] = console_since(pipeline, cursor)
            raise RuntimeError(json.dumps(summary, indent=2))
        if not summary["reloaded"]:
            summary["warning"] = "No [MethodReload] method was found in this file. Tag it before entering Play mode."
    image = None
    if capture_after:
        shot, result = capture(pipeline, route_name, width, height)
        summary["capture"] = str(shot)
        summary["capture_result"] = result
        image, extra = capture_view.shape(shot, view, region, element)
        summary.update(extra)
    summary["console"] = console_since(pipeline, cursor)
    return summary, image


def revert_all():
    """Drops every live override: stylesheets back to the main project's files, C# reloads back to compiled code."""
    pipeline = Pipeline(main_worktree())
    styles = (pipeline.run("script eval", code=editor_hook("style_revert"), timeout=60)
              if EDITOR_HOOKS.get("style_revert") else {"result": "no live-style hook configured"})
    # Not `script.methodreload cleanup`: it also requests a domain reload, too disruptive for a shared editor.
    code = pipeline.run("script eval", code=CLEAR_RELOADS_CODE, timeout=30)
    return {"styles": (styles or {}).get("result"), "code": (code or {}).get("result")}


def capture_only(route_name, width=1280, height=720, view="fit", region=None, element=None):
    pipeline = Pipeline(main_worktree())
    require_playing(pipeline)
    cursor = console_cursor(pipeline)
    shot, result = capture(pipeline, route_name, width, height)
    image, extra = capture_view.shape(shot, view, region, element)
    return {"capture": str(shot), "capture_result": result, **extra, "console": console_since(pipeline, cursor)}, image


def as_content(types, summary, image):
    content = [types.TextContent(type="text", text=json.dumps(summary, indent=2))]
    if image:
        content.append(types.ImageContent(type="image", data=base64.b64encode(image).decode(), mimeType="image/png"))
    return content
