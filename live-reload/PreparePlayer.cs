using Mono.Cecil;
using Mono.Cecil.Cil;

namespace WorktreeBroker
{
	// Inserts `BridgeType.Tick(this)` at the start of the game's own per-frame method in a copied Mono player.
	// Arguments: <Managed folder> <app assembly> <app type> <app method> <bridge assembly> <bridge type>
	// (live_reload.py passes them from project.json's player.hook).
	internal static class PreparePlayer
	{
		private static void Main(string[] args)
		{
			if (args.Length != 6) throw new ArgumentException("Expected: managed appAssembly appType appMethod bridgeAssembly bridgeType");
			var (managed, appAssembly, appType, appMethod, bridgeAssembly, bridgeType) = (args[0], args[1], args[2], args[3], args[4], args[5]);
			var appPath = Path.Combine(managed, appAssembly + ".dll");
			var options = new ReaderParameters { InMemory = true };
			using var app = ModuleDefinition.ReadModule(appPath, options);
			using var bridge = ModuleDefinition.ReadModule(Path.Combine(managed, bridgeAssembly + ".dll"), options);
			var update = app.GetType(appType).Methods.Single(method => method.Name == appMethod);
			var tick = bridge.GetType(bridgeType).Methods.Single(method => method.Name == "Tick");
			var il = update.Body.GetILProcessor();
			var first = update.Body.Instructions[0];
			il.InsertBefore(first, il.Create(OpCodes.Ldarg_0));
			il.InsertBefore(first, il.Create(OpCodes.Call, app.ImportReference(tick)));
			var temporary = appPath + ".broker";
			app.Write(temporary);
			File.Move(temporary, appPath, overwrite: true);
		}
	}
}
