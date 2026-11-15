# UI live reload and captures

`ui_live_reload`, `ui_revert`, `ui_capture` and cheaper capture views. Part of [Worktree Broker for Unity](../README.md).

## UI live reload (`ui_live_reload`, `ui_revert`, `ui_capture`)

See a view change on screen in well under a second of editor time, from any worktree, in the one shared editor. Both
halves of a UI Toolkit view reload: its **stylesheets** (`.uss`, where the look lives) and its **C# rebuild methods**
(where structure and state-driven classes live).

**What happens on a call**

1. The broker checks every file is a saved `.uss` or `.cs` inside *your* worktree.
2. It takes the main editor's lock, checks the main checkout's branch, and finds Unity's Pipeline server
   (`Library/Pipeline/.unity-pipeline-port`: loopback port and token).
3. It refuses unless the editor is idle and in Play mode (playing or paused). It never starts or stops Play mode
   itself, because that would disturb whoever set up the screen.
4. **Stylesheets** (`styles`): the package's editor-only `WorktreeBroker.Editor.LiveStyles` (the default
   `editor.style_apply`) compiles each file's text into the main project's stylesheet at the same `Assets/...` path, **in
   memory**, with UI Toolkit's own importer, then restyles every panel. About 0.25-0.45 s warm. The main project's `.uss`
   file is never written.
5. **C#** (`source`): Unity recompiles just the `[MethodReload]` methods in your file, from your worktree's path, and
   swaps them into the running game. 0.1-0.2 s warm, about 1.8 s the first time in a session. The package's runtime
   `WorktreeBroker.LiveReload` then reruns those rebuild methods on the live views.
6. The capture hook (`editor.capture`; the package's `WorktreeBroker.Editor.BrokerCapture.ForBroker` by default) renders
   the UI, and you get back the results, any new console warnings or errors, the PNG path and the image itself.

A broken stylesheet comes back as an error with UI Toolkit's own line-numbered messages (for example
`Unknown property 'colr' (did you mean 'color'?)`), and that stylesheet is restored from the main project's file, so a
broken file never stays on screen. A failed C# reload (a compile error, or a private member with `backend: compiled`)
comes back as an error with Unity's reason.

**Arguments**

| Argument | Default | Meaning |
| --- | --- | --- |
| `styles` | none | Saved `.uss` files, relative to your worktree (same `Assets/...` path as in the main project). Applied first. |
| `source` | none | Saved `.cs` file, relative to your worktree, e.g. `Assets/<Project>/Presentation/Hud/MainMenuView.cs`. |
| `backend` | `interpreter` | For `source`. `interpreter`: the body may use private fields and UI Toolkit freely. `compiled`: full C#, public members only. |
| `capture` | `true` | Return a screenshot afterwards. |
| `width`, `height` | 1280 x 720 | Screenshot size. |

Give `styles`, `source`, or both.

`ui_revert` puts everything back: stylesheets are re-imported from the main project's files, and every C# reload is
dropped (back to the compiled code) without a domain reload. Stylesheet overrides are also reverted automatically when
Play mode ends; C# reloads end with Play mode by themselves. `ui_capture` (optional `width`, `height`) takes a screenshot
without changing anything.

**Stylesheets first.** The look belongs in USS: anything a C# method sets inline overrides the stylesheets. So tune colours, sizes, spacing and positions in `.uss` with `styles`, and use tagged C# only
for structure and for which classes an element carries. A tagged method that sets layout numbers inline is a smell.

**What a view needs for C# reload (one-time, in the project's code; `LiveReload.Track` is the package's hook)**

```csharp
using WorktreeBroker;

public MyView()
{
    // ...build the view...
    LiveReload.Track(this);            // the package's hook; editor-only, stripped from player builds
}

[Unity.Pipeline.MethodReload.MethodReload]
public void ApplyState()               // public, void, no parameters, safe to run twice
{
    panel.EnableInClassList("my-panel--compact", isCompact);   // classes here, numbers in USS
}
```

The tag has to be in the code the editor compiled **before** entering Play mode, so tag the method (and get it into the
main checkout, or have the main editor recompile) before you start iterating. Stylesheets need nothing: any `.uss`
that already exists in the main project can be reloaded.

**Workflow from a worktree chat**

1. Make sure the main editor is in Play mode on the screen you're changing. Ask whoever is at the editor, or use your
   project's own way of opening a screen (a test scene or fixture). The broker won't change Play mode for you.
2. Edit the `.uss` (and, if needed, the tagged method) in your worktree and save.
3. Call `ui_live_reload` with the files. Look at the returned image.
4. Repeat. When it looks right, call `ui_revert`; the change goes through your branch as usual.

**What it can't do**

- A stylesheet must already exist in the main project. A brand-new `.uss` file, or a new `@import`, needs a normal
  import in the main editor. `@import` lines in your file are ignored: the main project's import list is kept, and the
  result warns when yours differs. `url()` paths resolve against the main project's copy, so new images in your
  worktree aren't seen.
- UXML isn't reloaded.
- For C#, only bodies of tagged methods change. New fields, new tagged methods and signature changes need a normal
  compile in the main editor. Fast Script Reload still covers untagged method bodies there.
- The interpreter can't call every .NET API (for example `System.IO.File`). Unity reports an unsupported call as a
  warning when you reload and as an error when it runs.
- Overrides are shared: until `ui_revert` or the end of Play mode, other chats' captures show your changes too.
- The main checkout must be on the branch in `routes.json`. A tag added only on a feature branch exists in the editor
  only after the main checkout has it.
- If a call hits its time limit, the broker keeps the editor lock until the worker thread really ends (bounded), so
  another chat's calls never interleave with work still talking to Unity.
- Timing: the Unity side is 0.25-2 s. A broker that's already running (a normal MCP session) adds little.
  `broker_client.py` from a terminal pays Python's start and the `mcp` import (about 4 s, more on a busy machine).

**Player builds.** Live reload is editor-only. Install `com.unity.pipeline` with both of its runtime toggles off, so
player builds contain no Pipeline runtime, compiler, interpreter or command server. Don't turn the toggles on for builds
you share. To check a build, look in its `<Player>_Data/Managed` folder: with the toggles off, the only Pipeline file is a
small attributes DLL.

### Cheaper captures (`view`, `region`, `element`)

A chat pays for every image by its pixels (about width x height / 750 tokens for Claude) and keeps paying while it stays
in context: a 1280 x 720 capture costs about 1,200 tokens, a 1080p frame about 2,760. The capture is still rendered at `width` x `height`, since layout depends on it; only what comes
back changes (`capture_view.py`):

| `view` | Returns | About |
| --- | --- | --- |
| `fit` (default) | the whole frame scaled to 768 px on its long edge | 440 tokens |
| `crop` | `region` [x, y, w, h] in capture pixels, or the first UI element matching `element`, at full resolution, 16 px padding | a 828 x 206 button row: 227 |
| `full` | the frame as rendered | 1,229 at 720p |
| `none` | no image (the PNG path is still in the result) | 0 |

Every capture also writes `layout/<capture>.json` beside it, if the capture hook does that (the package's
`BrokerCapture.WriteLayout` does). With
`element` (a substring of a name, class or type), the matching visible elements come back as text: bounds in capture
pixels, text and font size. That often answers "did it move, wrap or fit" without an image. A crop that matches nothing
lists the visible classes.
