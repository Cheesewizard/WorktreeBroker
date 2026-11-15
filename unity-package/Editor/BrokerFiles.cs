using System.IO;
using UnityEngine;

namespace WorktreeBroker.Editor
{
	/// <summary>
	/// The one folder the editor and the broker exchange files through: &lt;project&gt;/Library/WorktreeBroker. The broker
	/// finds it from the main checkout in its routes.json, so it works wherever the broker itself is installed, and
	/// Library is never committed. Files: compile-status.json (written here), editor-guard.jsonl (written here),
	/// editor-last-activity (written by the broker).
	/// </summary>
	public static class BrokerFiles
	{
		public static string Folder
		{
			get
			{
				var project = Directory.GetParent(Application.dataPath)?.FullName ?? ".";
				return Path.Combine(project, "Library", "WorktreeBroker");
			}
		}

		public static string PathOf(string name) => Path.Combine(Folder, name);
	}
}
