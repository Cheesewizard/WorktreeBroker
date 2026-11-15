# Standalone player C# patch prototype

This prototype patches a copied Windows Mono player of the project. It loads one C# patch and one PNG into a running
match. A small Cecil tool (`PreparePlayer.cs`) inserts a call to a bridge at the start of one of the game's own
per-frame methods. Preparing a copy needs a .NET SDK and Unity's `Unity.Cecil.dll`, and runs without an editor.

Everything game-specific comes from `project.json`'s `player` section (see [Project configuration](../docs/configuration.md#project-configuration)):

| Key | Meaning |
| --- | --- |
| `bridge_source` | The bridge's C# source (`{broker}` = the broker folder, `{main}` = the main checkout). It defines a static `Tick(<app type>)` that watches for `-broker-patch <dll>`, loads that DLL once on the main thread, calls its entry point and writes `patched.png` next to it. |
| `bridge_assembly` | The DLL name the bridge is compiled to inside `<Player>_Data/Managed`. |
| `references` | Game and Unity module assemblies the bridge and patches compile against (mscorlib, System, System.Core, netstandard and UnityEngine.CoreModule are always included). |
| `hook` | `assembly`, `type` and `method` of the game method to hook, and `bridge_type`, the bridge's type name. |
| `patch_artwork_name` | The file name the PNG is copied to next to the patch DLL (default `artwork.png`). |

A bridge is a few dozen lines of your game's own C#. A minimal shape:

```csharp
public static class PlayerBridge
{
    static bool done;
    public static void Tick(MyGameApp app)          // the hooked per-frame method passes its object in
    {
        if (done) return;
        var args = System.Environment.GetCommandLineArgs();
        var at = System.Array.IndexOf(args, "-broker-patch");
        if (at < 0 || !System.IO.File.Exists(args[at + 1])) return;
        done = true;
        var patch = System.Reflection.Assembly.LoadFrom(args[at + 1]);
        patch.GetType("MyGame.BrokerPatch").GetMethod("Apply").Invoke(null, new object[] { app });
        // then render a frame and write patched.png next to the patch DLL
    }
}
```

The patch itself is any static `Apply` method compiled against the same references.

1. `python live_reload.py prepare --source-player <original exe> --player <new folder>/<same exe name> --cecil <Unity editor Data/Managed/Unity.Cecil.dll>`. The destination folder must be new.
2. Launch the copied player with `-broker-patch <new run folder>/Patch.dll -logFile <new run folder>/player.log` plus
   whatever the bridge needs. Use a fresh run folder.
3. `python live_reload.py patch --player <copied exe> --patch-file <run folder>/Patch.dll --source <C# file> --artwork <PNG file>`.
4. Check that the log records the bridge's "applied" line and inspect `patched.png` in the run folder. The command
   reports compile-and-publish time and the time to the first captured patched frame.

The PNG is copied before the patch DLL is published atomically. The bridge invokes the patch once on the Unity main
thread. The prototype handles one entry point and one patch per run, and the artwork is a loose PNG decoded by the player.
Addressables catalogs and serialized prefab or scene changes would need a separate implementation. `bot_start` uses
this when a player route has a `startup_patch`.
