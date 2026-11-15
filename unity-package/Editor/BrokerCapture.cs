using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using UnityEditor;
using UnityEngine;
using UnityEngine.UIElements;

namespace WorktreeBroker.Editor
{
	/// <summary>
	/// Off-screen captures of the running game for the broker's ui_live_reload and ui_capture, working with the editor
	/// unfocused or the desktop locked (both stop the player loop and the Game view's repaints).
	///
	/// <see cref="StepFrames"/> advances paused Play mode frame by frame with a short real-time sleep. <see cref="Capture"/>
	/// renders the first UIDocument's panel into one render texture (through its PanelSettings) and the main camera into
	/// another, composites the UI over the camera (honouring the camera's viewport rect) and writes a PNG at any size.
	/// <see cref="WriteLayout"/> writes layout/&lt;capture&gt;.json beside it: every visible named or classed element's bounds,
	/// type, classes, font and text, which the broker's element and region views read. <see cref="ForBroker"/> is the
	/// broker's default capture hook: rerun reloaded view methods, capture, write the layout.
	/// </summary>
	public static class BrokerCapture
	{
		private const int MAX_ELEMENTS = 4000;
		private const int MAX_TEXT = 120;

		/// <summary>The broker's default capture: called through script eval with the PNG path and size.</summary>
		public static string ForBroker(string path, int width, int height)
		{
			var ran = LiveReload.Flush();
			var saved = Capture(path, width, height);
			var document = UnityEngine.Object.FindAnyObjectByType<UIDocument>();
			if (document != null) WriteLayout(path, document.rootVisualElement, width, height);
			return "flushed=" + ran + " (0 = the editor had already rerun them) " + saved;
		}

		public static void StepFrames(int frames, int sleepMilliseconds = 16)
		{
			if (!EditorApplication.isPlaying) return;

			EditorApplication.isPaused = true;
			for (var i = 0; i < frames; i++)
			{
				EditorApplication.Step();
				if (sleepMilliseconds > 0) Thread.Sleep(sleepMilliseconds);
			}
		}

		/// <summary>Writes the UI over the main camera at <paramref name="width"/> x <paramref name="height"/> to <paramref name="path"/>. Returns a short report.</summary>
		public static string Capture(string path, int width, int height, int settleFrames = 4)
		{
			var document = UnityEngine.Object.FindAnyObjectByType<UIDocument>();
			var camera = Camera.main;
			if (document == null && camera == null) return "No UIDocument or main camera.";

			var settings = document != null ? document.panelSettings : null;
			var previousTarget = settings != null ? settings.targetTexture : null;
			// Without a clear the texture keeps earlier frames: translucent panels pile up and moving labels ghost.
			var previousClear = settings != null && settings.clearColor;
			var previousClearValue = settings != null ? settings.colorClearValue : default;
			var uiTexture = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
			var cameraTexture = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
			try
			{
				if (settings != null)
				{
					settings.clearColor = true;
					settings.colorClearValue = new Color(0f, 0f, 0f, 0f);
					settings.targetTexture = uiTexture;
				}
				// Both targets are set while the frames settle, so the UI lays out and the camera fits its viewport
				// rect at the capture size, not the hidden Game view's.
				if (camera != null) camera.targetTexture = cameraTexture;
				StepFrames(Mathf.Max(2, settleFrames), 10);
				if (camera != null)
				{
					camera.Render();
					camera.targetTexture = null;
				}

				var below = Read(cameraTexture, width, height);
				var belowPixels = below.GetPixels();
				var background = camera != null ? camera.backgroundColor : Color.black;
				var rect = camera != null ? camera.rect : new Rect(0f, 0f, 1f, 1f);
				var hud = settings != null ? Read(uiTexture, width, height) : null;
				var hudPixels = hud != null ? hud.GetPixels() : null;
				for (var i = 0; i < belowPixels.Length; i++)
				{
					var x = (i % width) / (float)width;
					var y = (i / width) / (float)height;
					var under = camera != null && rect.Contains(new Vector2(x, y)) ? belowPixels[i] : background;
					var over = hudPixels != null ? hudPixels[i] : Color.clear;
					belowPixels[i] = new Color(over.r + under.r * (1f - over.a), over.g + under.g * (1f - over.a), over.b + under.b * (1f - over.a), 1f);
				}

				below.SetPixels(belowPixels);
				below.Apply();
				Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
				File.WriteAllBytes(path, below.EncodeToPNG());
				UnityEngine.Object.DestroyImmediate(below);
				if (hud != null) UnityEngine.Object.DestroyImmediate(hud);
				return $"Captured {width}x{height} to {path}";
			}
			finally
			{
				if (settings != null)
				{
					settings.targetTexture = previousTarget;
					settings.clearColor = previousClear;
					settings.colorClearValue = previousClearValue;
				}
				if (camera != null) camera.targetTexture = null;
				uiTexture.Release();
				cameraTexture.Release();
				UnityEngine.Object.DestroyImmediate(uiTexture);
				UnityEngine.Object.DestroyImmediate(cameraTexture);
			}
		}

		[Serializable]
		private class LayoutRecord
		{
			public string @case;
			public string state;
			public int width;
			public int height;
			public float scale;
			public List<ElementRecord> elements = new List<ElementRecord>();
			public List<string> styleSheets = new List<string>();
			public List<string> viewTypes = new List<string>();
		}

		[Serializable]
		private class ElementRecord
		{
			public string key;
			public string name;
			public string type;
			public List<string> classes;
			public float[] bounds;
			public bool visible;
			public float fontSize;
			public string font;
			public string text;
		}

		/// <summary>
		/// Writes layout/&lt;capture name&gt;.json beside <paramref name="capturePath"/>. <paramref name="isViewType"/> picks
		/// which element (and userData) types are listed as view types; by default every type outside Unity's own
		/// namespaces.
		/// </summary>
		public static void WriteLayout(string capturePath, VisualElement view, int width, int height, Func<Type, bool> isViewType = null)
		{
			if (view == null) return;
			isViewType = isViewType ?? IsProjectType;
			var caseName = Path.GetFileNameWithoutExtension(capturePath);
			var panelHeight = view.panel?.visualTree?.layout.height ?? float.NaN;
			var record = new LayoutRecord
			{
				@case = caseName,
				state = caseName.Substring(0, Math.Max(0, caseName.LastIndexOf('-'))),
				width = width,
				height = height,
				scale = float.IsNaN(panelHeight) || panelHeight <= 0f ? 1f : height / panelHeight,
			};
			var sheets = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
			var types = new HashSet<string>();
			Walk(view, "", record, sheets, types, isViewType);
			for (var element = view.parent; element != null; element = element.parent)
			{
				AddSheets(element, sheets);
			}
			record.styleSheets = sheets.OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToList();
			record.viewTypes = types.OrderBy(name => name, StringComparer.Ordinal).ToList();
			var folder = Path.Combine(Path.GetDirectoryName(capturePath), "layout");
			Directory.CreateDirectory(folder);
			File.WriteAllText(Path.Combine(folder, caseName + ".json"), JsonUtility.ToJson(record, false));
		}

		private static bool IsProjectType(Type type)
		{
			var space = type.Namespace;
			return space != null && !space.StartsWith("UnityEngine", StringComparison.Ordinal)
				&& !space.StartsWith("UnityEditor", StringComparison.Ordinal) && !space.StartsWith("Unity.", StringComparison.Ordinal)
				&& !space.StartsWith("System", StringComparison.Ordinal);
		}

		private static void Walk(VisualElement element, string parentKey, LayoutRecord record, HashSet<string> sheets,
			HashSet<string> types, Func<Type, bool> isViewType)
		{
			if (record.elements.Count >= MAX_ELEMENTS) return;
			AddSheets(element, sheets);
			var type = element.GetType();
			if (isViewType(type)) types.Add(type.FullName);
			var data = element.userData?.GetType();
			if (data != null && isViewType(data)) types.Add(data.FullName);
			var visible = IsShown(element);
			var classes = element.GetClasses().ToList();
			var key = parentKey;
			if (!string.IsNullOrEmpty(element.name) || classes.Count > 0 || element is TextElement)
			{
				var label = type.Name + (string.IsNullOrEmpty(element.name) ? "" : "#" + element.name)
					+ (classes.Count > 0 ? "." + classes[0] : "");
				key = string.IsNullOrEmpty(parentKey) ? label : parentKey + "/" + label;
				var bounds = element.worldBound;
				var style = element.resolvedStyle;
				var font = style.unityFontDefinition.fontAsset != null ? style.unityFontDefinition.fontAsset.name
					: style.unityFontDefinition.font != null ? style.unityFontDefinition.font.name
					: style.unityFont != null ? style.unityFont.name : "";
				var text = element is TextElement textElement ? textElement.text ?? "" : "";
				record.elements.Add(new ElementRecord
				{
					key = key,
					name = element.name ?? "",
					type = type.Name,
					classes = classes,
					bounds = new[] { Round(bounds.x), Round(bounds.y), Round(bounds.width), Round(bounds.height) },
					visible = visible,
					fontSize = Round(style.fontSize),
					font = font,
					text = text.Length > MAX_TEXT ? text.Substring(0, MAX_TEXT) : text,
				});
			}
			if (!visible) return;
			foreach (var child in element.hierarchy.Children())
			{
				Walk(child, key, record, sheets, types, isViewType);
			}
		}

		private static bool IsShown(VisualElement element)
		{
			if (!element.visible || element.resolvedStyle.display == DisplayStyle.None || element.resolvedStyle.opacity <= 0.01f) return false;
			var bounds = element.worldBound;
			return !float.IsNaN(bounds.width) && bounds.width > 0f && bounds.height > 0f;
		}

		private static void AddSheets(VisualElement element, HashSet<string> sheets)
		{
			for (var index = 0; index < element.styleSheets.count; index++)
			{
				var path = AssetDatabase.GetAssetPath(element.styleSheets[index]);
				if (!string.IsNullOrEmpty(path)) sheets.Add(path);
			}
		}

		private static float Round(float value)
		{
			return float.IsNaN(value) || float.IsInfinity(value) ? 0f : (float)Math.Round(value, 2);
		}

		private static Texture2D Read(RenderTexture texture, int width, int height)
		{
			var previous = RenderTexture.active;
			RenderTexture.active = texture;
			var result = new Texture2D(width, height, TextureFormat.RGBA32, false);
			result.ReadPixels(new Rect(0, 0, width, height), 0, 0);
			result.Apply();
			RenderTexture.active = previous;
			return result;
		}
	}
}
