using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using UnityEditor;
using UnityEngine;
using UnityEngine.UIElements;

namespace WorktreeBroker.Editor
{
	/// <summary>
	/// Live stylesheet reload for the shared editor (docs/live-reload.md).
	///
	/// <see cref="Apply"/> compiles a USS file's text (from this checkout or any worktree) into the project's existing
	/// StyleSheet of the same asset path, in memory, and restyles every panel. Nothing is written to the project: the
	/// asset's source file is untouched and <see cref="RevertAll"/> re-imports it from disk. Overrides are reverted
	/// automatically when Play mode ends, so a worktree's styles never outlive the session that applied them.
	///
	/// Uses UI Toolkit's own USS importer (UnityEditor.UIElements.StyleSheets.StyleSheetImporterImpl, internal) and the
	/// panels' DirtyStyleSheets (internal), reached by reflection; Unity 6000.4. Editor-only assembly, never in a player.
	/// Limits: the stylesheet must already exist in the project (a new .uss needs a normal import), and `@import` /
	/// `url()` resolve against the project's copy of that path, so a worktree's new imported files or images are not seen.
	/// </summary>
	[InitializeOnLoad]
	public static class LiveStyles
	{
		private const string OVERRIDDEN_KEY = "WorktreeBroker.LiveStyles.Overridden";
		private const string SOURCES_KEY = "WorktreeBroker.LiveStyles.Sources";
		private const BindingFlags ANY = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

		static LiveStyles()
		{
			EditorApplication.playModeStateChanged += change =>
			{
				if (change == PlayModeStateChange.EnteredEditMode)
				{
					RevertAll();
				}
			};
		}

		/// <summary>Paths currently overridden in memory (survives domain reloads within the editor session).</summary>
		public static IReadOnlyList<string> Overridden => Read();

		/// <summary>
		/// Compiles <paramref name="sourceFile"/> into the project's StyleSheet at <paramref name="assetPath"/> and restyles
		/// all panels. Returns a one-line summary; throws with the USS errors when the text doesn't import cleanly (the
		/// stylesheet is then restored from disk, so a broken file never stays on screen).
		/// </summary>
		public static string Apply(string assetPath, string sourceFile)
		{
			assetPath = (assetPath ?? "").Replace('\\', '/');
			if (!assetPath.StartsWith("Assets/", StringComparison.Ordinal) || !assetPath.EndsWith(".uss", StringComparison.OrdinalIgnoreCase))
			{
				throw new ArgumentException("assetPath must be a project-relative Assets/... .uss path", nameof(assetPath));
			}
			var sheet = AssetDatabase.LoadAssetAtPath<StyleSheet>(assetPath);
			if (sheet == null)
			{
				throw new InvalidOperationException($"No StyleSheet at {assetPath} in this project. A new .uss file needs a normal import in this editor.");
			}
			var contents = File.ReadAllText(sourceFile);
			// @import crashes the importer outside a real import (it reaches for the import context). Imports point at
			// other project stylesheets this override doesn't change, so blank those lines (line numbers stay right for
			// errors) and keep the sheet's existing import list; a worktree that changes its imports gets a warning.
			var importList = typeof(StyleSheet).GetField("imports", ANY);
			var keptImports = importList?.GetValue(sheet);
			var importWarning = SameImports(contents, File.ReadAllText(assetPath))
				? ""
				: "this file's @import lines differ from the project's copy; the project's imports are kept (import the file normally to change them)";
			contents = BlankImports(contents);
			// UI Builder's importer: the base StyleSheetImporterImpl records url()/@import dependencies on the asset
			// import context, which doesn't exist outside a real import (NullReferenceException); UI Builder overrides
			// exactly that (DeclareDependencyAndLoad) to load through the AssetDatabase, as it does for live edits.
			var importerType = FindType("Unity.UI.Builder.BuilderStyleSheetImporter")
				?? throw new InvalidOperationException("UI Builder's BuilderStyleSheetImporter is missing (Unity version change?)");
			var importer = Activator.CreateInstance(importerType, true);
			// Resolve url() and @import against the project's own copy of this stylesheet, as the real import does.
			FindField(importerType, "m_AssetPath").SetValue(importer, assetPath);
			Remember(assetPath);
			var replaced = RememberSource(assetPath, sourceFile);
			var replacedWarning = replaced == null ? ""
				: $"replaced another worktree's live override of this sheet (from {replaced}); its captures now show this version";
			importWarning = string.Join("; ", new[] { importWarning, replacedWarning }).Trim(' ', ';');
			importerType.GetMethod("Import", ANY, null, new[] { typeof(StyleSheet), typeof(string) }, null).Invoke(importer, new object[] { sheet, contents });
			importList?.SetValue(sheet, keptImports);
			typeof(StyleSheet).GetField("m_FlattenedImportedStyleSheets", ANY)?.SetValue(sheet, null);
			// Rebuild the selector lookup tables a real import builds on load, or matching keeps the old rules.
			typeof(StyleSheet).GetMethod("SetupReferences", ANY, null, Type.EmptyTypes, null)?.Invoke(sheet, null);

			var errors = importerType.GetProperty("importErrors", ANY)?.GetValue(importer);
			var errorText = string.Join("; ", new[] { Describe(errors), importWarning }).Trim(' ', ';');
			var hasErrors = (bool)(typeof(StyleSheet).GetProperty("importedWithErrors", ANY)?.GetValue(sheet) ?? false);
			if (hasErrors)
			{
				Revert(assetPath);
				throw new InvalidOperationException($"USS errors in {sourceFile}; {assetPath} was restored from disk.\n{errorText}");
			}
			// A new content hash makes UI Toolkit drop its cached style data for this sheet.
			typeof(StyleSheet).GetProperty("contentHash", ANY)?.SetValue(sheet, unchecked(Environment.TickCount ^ contents.GetHashCode()));
			var panels = DirtyAllPanels();
			return $"applied {assetPath} from {sourceFile}; restyled {panels} panel(s)" + (string.IsNullOrEmpty(errorText) ? "" : $"; warnings: {errorText}");
		}

		/// <summary>Re-imports every overridden stylesheet from the project's files and restyles all panels.</summary>
		public static string RevertAll()
		{
			var paths = Read();
			foreach (var path in paths)
			{
				AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceUpdate);
			}
			SessionState.EraseString(OVERRIDDEN_KEY);
			SessionState.EraseString(SOURCES_KEY);
			if (paths.Count > 0)
			{
				DirtyAllPanels();
			}
			return $"reverted {paths.Count} stylesheet(s)";
		}

		private static void Revert(string path)
		{
			AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceUpdate);
			var paths = Read();
			paths.Remove(path);
			SessionState.SetString(OVERRIDDEN_KEY, string.Join("\n", paths));
			var sources = ReadSources();
			sources.Remove(path);
			WriteSources(sources);
			DirtyAllPanels();
		}

		/// <summary>
		/// Which file each live override came from (asset path → absolute source path, so the worktree is visible), for
		/// tools that want to record or refuse captures taken with live overrides.
		/// </summary>
		public static IReadOnlyList<KeyValuePair<string, string>> Sources => ReadSources().ToList();

		/// <summary>Records the source of an override; returns the previous source when a different file had it, else null.</summary>
		private static string RememberSource(string path, string sourceFile)
		{
			var sources = ReadSources();
			var source = Path.GetFullPath(sourceFile).Replace('\\', '/');
			sources.TryGetValue(path, out var previous);
			sources[path] = source;
			WriteSources(sources);
			return previous != null && !string.Equals(previous, source, StringComparison.OrdinalIgnoreCase) ? previous : null;
		}

		private static Dictionary<string, string> ReadSources()
		{
			var result = new Dictionary<string, string>(StringComparer.Ordinal);
			foreach (var line in SessionState.GetString(SOURCES_KEY, "").Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries))
			{
				var tab = line.IndexOf('\t');
				if (tab > 0) result[line.Substring(0, tab)] = line.Substring(tab + 1);
			}
			return result;
		}

		private static void WriteSources(Dictionary<string, string> sources)
		{
			SessionState.SetString(SOURCES_KEY, string.Join("\n", sources.Select(pair => pair.Key + "\t" + pair.Value)));
		}

		private static int DirtyAllPanels()
		{
			var utility = typeof(VisualElement).Assembly.GetType("UnityEngine.UIElements.UIElementsUtility", true);
			var iterator = utility.GetMethod("GetPanelsIterator", ANY).Invoke(null, null);
			var count = 0;
			var enumerator = (IEnumerator)iterator;
			while (enumerator.MoveNext())
			{
				var pair = enumerator.Current;
				var panel = pair.GetType().GetProperty("Value").GetValue(pair);
				var dirty = panel.GetType().GetMethod("DirtyStyleSheets", ANY);
				if (dirty == null)
				{
					continue;
				}
				dirty.Invoke(panel, null);
				count++;
			}
			EditorApplication.QueuePlayerLoopUpdate();
			return count;
		}

		private static void Remember(string path)
		{
			var paths = Read();
			if (!paths.Contains(path))
			{
				paths.Add(path);
				SessionState.SetString(OVERRIDDEN_KEY, string.Join("\n", paths));
			}
		}

		private static List<string> Read()
		{
			var text = SessionState.GetString(OVERRIDDEN_KEY, "");
			return new List<string>(text.Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries));
		}

		private static readonly System.Text.RegularExpressions.Regex ImportLine =
			new System.Text.RegularExpressions.Regex(@"^[ \t]*@import[^;\n]*;?", System.Text.RegularExpressions.RegexOptions.Multiline);

		private static string BlankImports(string text)
		{
			return ImportLine.Replace(text, "");
		}

		private static bool SameImports(string a, string b)
		{
			var first = new List<string>();
			foreach (System.Text.RegularExpressions.Match match in ImportLine.Matches(a)) first.Add(match.Value.Trim());
			var second = new List<string>();
			foreach (System.Text.RegularExpressions.Match match in ImportLine.Matches(b)) second.Add(match.Value.Trim());
			return string.Join("\n", first) == string.Join("\n", second);
		}

		private static Type FindType(string fullName)
		{
			foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
			{
				var type = assembly.GetType(fullName);
				if (type != null)
				{
					return type;
				}
			}
			return null;
		}

		private static FieldInfo FindField(Type type, string name)
		{
			for (var current = type; current != null; current = current.BaseType)
			{
				var field = current.GetField(name, ANY | BindingFlags.DeclaredOnly);
				if (field != null)
				{
					return field;
				}
			}
			throw new MissingFieldException(type.FullName, name);
		}

		private static string Describe(object errors)
		{
			if (errors == null)
			{
				return "";
			}
			var text = new StringBuilder();
			if (errors is IEnumerable list)
			{
				foreach (var error in list)
				{
					text.AppendLine(error?.ToString());
				}
			}
			return text.ToString().Trim();
		}
	}
}
