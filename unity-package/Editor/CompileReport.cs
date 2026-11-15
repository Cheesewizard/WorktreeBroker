using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEngine;

namespace WorktreeBroker.Editor
{
	/// <summary>
	/// Writes the shared editor's last compile result to Library/WorktreeBroker/compile-status.json, so the Unity
	/// Worktree Broker can tell every chat (and the dashboard) when the main checkout doesn't compile and which files
	/// break it (the broker README, "Compile status"). One chat's unfinished file used to block everyone
	/// silently: each chat found the errors on its own and guessed whose they were.
	/// Format: {"time": epoch s, "ok": bool, "errors": [{"file", "line", "message", "assembly"}]}, at most 200 errors.
	/// </summary>
	[InitializeOnLoad]
	public static class CompileReport
	{
		private const int MAX_ERRORS = 200;
		private static readonly List<(string File, int Line, string Message, string Assembly)> errors =
			new List<(string, int, string, string)>();

		static CompileReport()
		{
			CompilationPipeline.compilationStarted += _ => errors.Clear();
			CompilationPipeline.assemblyCompilationFinished += Collect;
			CompilationPipeline.compilationFinished += _ => Write();
		}

		private static void Collect(string assemblyPath, CompilerMessage[] messages)
		{
			var assembly = Path.GetFileNameWithoutExtension(assemblyPath);
			foreach (var message in messages)
			{
				if (message.type == CompilerMessageType.Error && errors.Count < MAX_ERRORS)
				{
					errors.Add((message.file ?? string.Empty, message.line, FirstLine(message.message), assembly));
				}
			}
		}

		private static string FirstLine(string text)
		{
			text = text ?? string.Empty;
			var end = text.IndexOf('\n');
			return end < 0 ? text : text.Substring(0, end);
		}

		private static void Write()
		{
			try
			{
				var folder = BrokerFiles.Folder;
				Directory.CreateDirectory(folder);
				var json = new StringBuilder();
				var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
				json.Append("{\"time\": ").Append(now.ToString("0.000", System.Globalization.CultureInfo.InvariantCulture));
				json.Append(", \"ok\": ").Append(errors.Count == 0 ? "true" : "false").Append(", \"errors\": [");
				for (var i = 0; i < errors.Count; i++)
				{
					var error = errors[i];
					if (i > 0) json.Append(", ");
					json.Append("{\"file\": ").Append(Quote(error.File.Replace('\\', '/')))
						.Append(", \"line\": ").Append(error.Line)
						.Append(", \"message\": ").Append(Quote(error.Message))
						.Append(", \"assembly\": ").Append(Quote(error.Assembly)).Append('}');
				}
				json.Append("]}");
				var path = Path.Combine(folder, "compile-status.json");
				File.WriteAllText(path + ".tmp", json.ToString());
				File.Copy(path + ".tmp", path, true);
				File.Delete(path + ".tmp");
			}
			catch (IOException)
			{
			}
			catch (UnauthorizedAccessException)
			{
			}
		}

		private static string Quote(string text)
		{
			var builder = new StringBuilder("\"");
			foreach (var c in text ?? string.Empty)
			{
				switch (c)
				{
					case '"': builder.Append("\\\""); break;
					case '\\': builder.Append("\\\\"); break;
					case '\n': builder.Append("\\n"); break;
					case '\r': break;
					case '\t': builder.Append("\\t"); break;
					default:
						if (c < ' ') builder.Append("\\u").Append(((int)c).ToString("x4"));
						else builder.Append(c);
						break;
				}
			}
			return builder.Append('"').ToString();
		}
	}
}
