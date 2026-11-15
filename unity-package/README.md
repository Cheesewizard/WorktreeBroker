# Worktree Broker for Unity: editor package (`com.worktreebroker.unity`)

The Unity side of the Worktree Broker for Unity (`../README.md`). Install it in the project the broker serves.

## Install

- **Local copy** (the broker sits inside your repo, for example at `tools/WorktreeBroker`): add to `Packages/manifest.json`
  `"com.worktreebroker.unity": "file:../tools/WorktreeBroker/unity-package"`.
- **From git**: `"com.worktreebroker.unity": "https://github.com/Cheesewizard/WorktreeBroker.git?path=/unity-package"` (the broker's own repository; from a copy inside another repository, use that repository's URL and folder).

If you edit `manifest.json` while the editor is open, an asset refresh does not load the package: Package Manager only
re-resolves when the editor regains focus or when something calls `UnityEditor.PackageManager.Client.Resolve()` (from
the broker: `execute_code`). Until then, code that uses `WorktreeBroker` fails to compile. The package's
`CompileReport` starts with the first compile after it loads.

Optional: `com.unity.pipeline` (0.10.0-exp.1 or later) for C# view reload. Without it the package still compiles, and
`LiveReload` does nothing.

## What it does

| Part | Assembly | Broker feature |
| --- | --- | --- |
| `CompileReport` | Editor | Writes every compile's errors to `Library/WorktreeBroker/compile-status.json` (`editor_compile_status`, the dashboard's compile line). |
| `LiveStyles` | Editor | `ui_live_reload` `styles` and `ui_revert`: compiles a worktree's `.uss` text into the project's existing StyleSheet in memory, restyles every panel, and reverts when Play mode ends. Uses UI Toolkit internals by reflection (tested on Unity 6000.4). |
| `LiveReload` | Runtime | Views call `LiveReload.Track(this)` once and mark idempotent rebuild methods `[MethodReload] public void Rebuild()`. After a reload swaps one of those bodies, it reruns it on every tracked live view. `Track` is `[Conditional("UNITY_EDITOR")]`, so players carry none of it. |
| `BrokerCapture` | Editor | The default capture hook (`ForBroker`): reruns pending reloads, renders the first `UIDocument` over `Camera.main` to a PNG at any size with the editor unfocused, and writes `layout/<capture>.json` (element bounds, classes, fonts, text) beside it. |
| `EditorIdleGuard` | Editor | Stops Play mode left running while nobody is at the editor (idle, stalled, or running too long unfocused) so compiles and imports aren't blocked. Menu: **Tools > Worktree Broker > Idle Play Mode Guard**. |
| `BrokerFiles` | Editor | The exchange folder, `<project>/Library/WorktreeBroker/`. The broker finds it from its main checkout, so the broker can be installed anywhere. |

A project whose game isn't a single `UIDocument` over `Camera.main` can set its own capture in the broker's
`project.json` (`editor.capture`) and keep the rest.

## Leave Pipeline's runtime toggles off

`com.unity.pipeline` can also run a command server inside players. Keep that off, so a shipped or multiplayer
build never accepts code reloads.
