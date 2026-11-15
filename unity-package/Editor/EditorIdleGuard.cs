using System;
using System.IO;
using UnityEditor;
using UnityEditorInternal;
using UnityEngine;

namespace WorktreeBroker.Editor
{
	/// <summary>
	/// Stops Play mode that was left running, so the shared editor is free to compile and import again
	/// (docs/shared-editor.md, "Editor guard"). An agent polling a stuck capture can otherwise hold Play mode for half an hour
	/// while compiles wait behind it.
	///
	/// It acts only while nobody is at the editor (the editor window has not been focused for the whole wait):
	/// - Stalled: playing, not paused, and no new frame for <see cref="StallMinutes"/>. If run-in-background was off
	///   (the usual reason an unfocused game stops), it turns that on first and waits once more before stopping.
	/// - Idle: no Unity Worktree Broker call that touched the editor (Library/WorktreeBroker/editor-last-activity)
	///   and no focus for <see cref="IdleMinutes"/>, counted from when Play mode started at the earliest.
	/// - Too long: Play mode has run <see cref="MaxUnfocusedMinutes"/> with nobody at the editor, whatever the broker
	///   traffic. This catches a chat polling a stuck capture: each poll counts as use and steps frames, so neither rule
	///   above fires. A console warning goes out 5 minutes before, so a working chat can wrap up or take a fresh lease.
	/// It only ever stops Play mode: it never closes the editor, saves, or discards anything outside Play mode.
	/// Each stop is logged to the console (agents polling the editor see it) and appended to
	/// Library/WorktreeBroker/editor-guard.jsonl for the dashboard. Menu: Tools > Worktree Broker > Idle Play Mode Guard.
	/// </summary>
	[InitializeOnLoad]
	public static class EditorIdleGuard
	{
		private const string ENABLED_KEY = "WorktreeBroker.EditorIdleGuard.Enabled";
		private const string IDLE_KEY = "WorktreeBroker.EditorIdleGuard.IdleMinutes";
		private const string STALL_KEY = "WorktreeBroker.EditorIdleGuard.StallMinutes";
		private const string MAX_KEY = "WorktreeBroker.EditorIdleGuard.MaxUnfocusedMinutes";
		private const double WARN_BEFORE_SECONDS = 300;
		private const string MENU = "Tools/Worktree Broker/Idle Play Mode Guard";
		private const double CHECK_SECONDS = 5;
		private const double NUDGE_GRACE_SECONDS = 60;

		public enum Verdict { Keep, NudgeBackground, Warn, Stop }

		private static double nextCheck;
		private static int lastFrame = -1;
		private static double lastFrameAt;
		private static double lastFocusAt;
		private static double playStartedAt;
		private static double nudgedAt;
		private static bool warned;

		static EditorIdleGuard()
		{
			EditorApplication.update += Tick;
			EditorApplication.playModeStateChanged += change =>
			{
				if (change == PlayModeStateChange.EnteredPlayMode)
				{
					var now = Now();
					playStartedAt = now;
					lastFrameAt = now;
					lastFrame = -1;
					nudgedAt = 0;
					warned = false;
				}
			};
			var start = Now();
			lastFocusAt = start;
			playStartedAt = start;
			lastFrameAt = start;
		}

		public static bool Enabled
		{
			get => EditorPrefs.GetBool(ENABLED_KEY, true);
			set => EditorPrefs.SetBool(ENABLED_KEY, value);
		}

		/// <summary>Minutes with no broker editor call and no focus before an idle Play mode stops (default 10).</summary>
		public static float IdleMinutes
		{
			get => EditorPrefs.GetFloat(IDLE_KEY, 10f);
			set => EditorPrefs.SetFloat(IDLE_KEY, value);
		}

		/// <summary>Minutes with no new frame (and no focus) before a stalled Play mode stops (default 5).</summary>
		public static float StallMinutes
		{
			get => EditorPrefs.GetFloat(STALL_KEY, 5f);
			set => EditorPrefs.SetFloat(STALL_KEY, value);
		}

		/// <summary>Minutes of Play mode with nobody at the editor before it stops regardless of broker use (default 30).</summary>
		public static float MaxUnfocusedMinutes
		{
			get => EditorPrefs.GetFloat(MAX_KEY, 30f);
			set => EditorPrefs.SetFloat(MAX_KEY, value);
		}

		[MenuItem(MENU)]
		private static void Toggle()
		{
			Enabled = !Enabled;
			Debug.Log($"[EditorIdleGuard] {(Enabled ? "on" : "off")}: idle {IdleMinutes} min, stalled {StallMinutes} min, " +
				$"at most {MaxUnfocusedMinutes} min unfocused.");
		}

		[MenuItem(MENU, true)]
		private static bool ToggleValidate()
		{
			Menu.SetChecked(MENU, Enabled);
			return true;
		}

		/// <summary>
		/// The rule on its own, so it can be checked without waiting minutes. All times are seconds since an event;
		/// <paramref name="sinceFocus"/> is how long the editor window has been unfocused; <paramref name="unfocusedPlay"/>
		/// how long Play mode has run with nobody at the editor (the shorter of time in Play mode and time unfocused).
		/// </summary>
		public static Verdict Decide(bool paused, double sinceFrame, double sinceFocus, double sinceEditorUse,
			bool runInBackground, double sinceNudge, double idleSeconds, double stallSeconds, out string reason)
		{
			return Decide(paused, sinceFrame, sinceFocus, sinceEditorUse, runInBackground, sinceNudge, idleSeconds,
				stallSeconds, 0, double.MaxValue, true, out reason);
		}

		public static Verdict Decide(bool paused, double sinceFrame, double sinceFocus, double sinceEditorUse,
			bool runInBackground, double sinceNudge, double idleSeconds, double stallSeconds, double unfocusedPlay,
			double maxSeconds, bool alreadyWarned, out string reason)
		{
			reason = null;
			if (unfocusedPlay >= maxSeconds)
			{
				reason = $"too long: Play mode ran {unfocusedPlay / 60:0.#} min with nobody at the editor";
				return Verdict.Stop;
			}
			if (!paused && sinceFrame >= stallSeconds && sinceFocus >= stallSeconds)
			{
				if (!runInBackground && sinceNudge < 0)
				{
					reason = $"no new frame for {sinceFrame / 60:0.#} min while unfocused; turning run-in-background on";
					return Verdict.NudgeBackground;
				}
				if (sinceNudge < 0 || sinceNudge >= NUDGE_GRACE_SECONDS)
				{
					reason = $"stalled: no new frame for {sinceFrame / 60:0.#} min and nobody at the editor";
					return Verdict.Stop;
				}
			}
			if (sinceEditorUse >= idleSeconds && sinceFocus >= idleSeconds)
			{
				reason = $"idle: no broker editor call and no focus for {Math.Min(sinceEditorUse, sinceFocus) / 60:0.#} min";
				return Verdict.Stop;
			}
			if (!alreadyWarned && unfocusedPlay >= maxSeconds - WARN_BEFORE_SECONDS)
			{
				reason = $"Play mode has run {unfocusedPlay / 60:0.#} min with nobody at the editor; it stops at " +
					$"{maxSeconds / 60:0.#} min. Finish up, capture what you need, and release your editor lease";
				return Verdict.Warn;
			}
			return Verdict.Keep;
		}

		private static void Tick()
		{
			var now = Now();
			if (InternalEditorUtility.isApplicationActive)
			{
				lastFocusAt = now;
			}
			if (now < nextCheck)
			{
				return;
			}
			nextCheck = now + CHECK_SECONDS;
			if (!EditorApplication.isPlaying || EditorApplication.isCompiling || !Enabled)
			{
				return;
			}
			if (Time.frameCount != lastFrame)
			{
				lastFrame = Time.frameCount;
				lastFrameAt = now;
			}
			var lastUse = Math.Max(LastBrokerEditorUse(), playStartedAt);
			var unfocusedPlay = Math.Min(now - playStartedAt, now - lastFocusAt);
			var verdict = Decide(EditorApplication.isPaused, now - lastFrameAt, now - lastFocusAt, now - lastUse,
				Application.runInBackground, nudgedAt > 0 ? now - nudgedAt : -1, IdleMinutes * 60.0, StallMinutes * 60.0,
				unfocusedPlay, MaxUnfocusedMinutes * 60.0, warned, out var reason);
			if (verdict == Verdict.Warn)
			{
				warned = true;
				Debug.LogWarning($"[EditorIdleGuard] {reason}.");
				Record("warn", reason, now);
			}
			else if (verdict == Verdict.NudgeBackground)
			{
				nudgedAt = now;
				Application.runInBackground = true;
				Debug.LogWarning($"[EditorIdleGuard] {reason}.");
				Record("nudge", reason, now);
			}
			else if (verdict == Verdict.Stop)
			{
				Debug.LogWarning($"[EditorIdleGuard] Stopped Play mode after {(now - playStartedAt) / 60:0.#} min: {reason}. " +
					"The shared editor is free again (Tools > Worktree Broker > Idle Play Mode Guard to turn this off).");
				Record("stop", reason, now);
				EditorApplication.isPlaying = false;
			}
		}

		private static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

		private static string BrokerRuns() => BrokerFiles.Folder;

		/// <summary>Epoch seconds of the last broker call that touched the editor, or 0.</summary>
		private static double LastBrokerEditorUse()
		{
			try
			{
				var text = File.ReadAllText(Path.Combine(BrokerRuns(), "editor-last-activity")).Trim();
				var space = text.IndexOf(' ');
				var stamp = space > 0 ? text.Substring(0, space) : text;
				return double.TryParse(stamp, System.Globalization.NumberStyles.Float,
					System.Globalization.CultureInfo.InvariantCulture, out var value) ? value : 0;
			}
			catch (IOException)
			{
				return 0;
			}
			catch (UnauthorizedAccessException)
			{
				return 0;
			}
		}

		private static void Record(string action, string reason, double now)
		{
			try
			{
				var folder = BrokerRuns();
				Directory.CreateDirectory(folder);
				var line = "{\"time\": " + now.ToString("0.000", System.Globalization.CultureInfo.InvariantCulture) +
					", \"action\": \"" + action + "\", \"played_minutes\": " +
					((now - playStartedAt) / 60).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) +
					", \"reason\": \"" + reason.Replace("\"", "'") + "\"}";
				File.AppendAllText(Path.Combine(folder, "editor-guard.jsonl"), line + Environment.NewLine);
			}
			catch (IOException)
			{
			}
			catch (UnauthorizedAccessException)
			{
			}
		}
	}
}
