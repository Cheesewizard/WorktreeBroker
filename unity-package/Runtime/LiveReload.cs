using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
#if UNITY_EDITOR && WORKTREE_BROKER_PIPELINE
using Unity.Pipeline.MethodReload;
#endif

namespace WorktreeBroker
{
	/// <summary>
	/// Editor-only live reload for views, on top of com.unity.pipeline's method reload (the broker README, "UI live
	/// reload"). Without com.unity.pipeline in the project this compiles to no-ops.
	///
	/// A view that wants sub-second iteration calls <c>LiveReload.Track(this)</c> once and marks its
	/// idempotent rebuild methods <c>[MethodReload] public void Something()</c> (no parameters). When a
	/// reload swaps one of those bodies, the method only changes; this hook runs it again on every
	/// tracked live view so the new body shows on screen.
	///
	/// Nothing here exists in a player: <see cref="Track"/> is [Conditional("UNITY_EDITOR")], so calls are
	/// stripped from builds. Leave Pipeline's runtime toggles off so no player carries its command server either
	/// (reload must never be a way into a shipped or multiplayer game).
	/// </summary>
	public static class LiveReload
	{
		[Conditional("UNITY_EDITOR")]
		public static void Track(object view)
		{
#if UNITY_EDITOR && WORKTREE_BROKER_PIPELINE
			if (view == null)
			{
				return;
			}
			tracked.RemoveAll(entry => !entry.IsAlive);
			tracked.Add(new WeakReference(view));
			Hook();
#endif
		}

		/// <summary>
		/// Runs a pending rerun now instead of on the next editor update and returns how many methods ran.
		/// The worktree broker's ui_live_reload calls it right before its capture, on the main thread, so
		/// the capture is ordered after the rebuild by construction rather than by waiting.
		/// </summary>
		public static int Flush()
		{
#if UNITY_EDITOR && WORKTREE_BROKER_PIPELINE
			if (!isPending)
			{
				return 0;
			}
			UnityEditor.EditorApplication.delayCall -= Rerun;
			return RerunCount();
#else
			return 0;
#endif
		}

#if UNITY_EDITOR && WORKTREE_BROKER_PIPELINE
		private static readonly List<WeakReference> tracked = new List<WeakReference>();
		private static bool isHooked;
		private static bool isPending;

		// Pipeline 0.10 keeps its "an override was added or removed" event internal and documents it as the
		// intended hook once public; until then it is reached by reflection. A missing event logs once.
		private static void Hook()
		{
			if (isHooked)
			{
				return;
			}
			isHooked = true;
			var changed = typeof(MethodReloadRegistry).GetEvent(
				"OverridesChanged", BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);
			if (changed == null)
			{
				UnityEngine.Debug.LogWarning("LiveReload: Pipeline's OverridesChanged event is gone; tagged view methods won't rerun after a reload.");
				return;
			}
			changed.GetAddMethod(true).Invoke(null, new object[] { (Action)Schedule });
		}

		// A reload of several methods raises the event once per method, possibly off the main thread:
		// collapse them into one rerun on the editor's next update.
		private static void Schedule()
		{
			if (isPending)
			{
				return;
			}
			isPending = true;
			UnityEditor.EditorApplication.delayCall += Rerun;
		}

		private static void Rerun()
		{
			RerunCount();
		}

		private static int RerunCount()
		{
			isPending = false;
			var ran = 0;
			if (!UnityEngine.Application.isPlaying)
			{
				return ran;
			}
			foreach (var entry in tracked.ToArray())
			{
				var view = entry.Target;
				if (view == null)
				{
					continue;
				}
				var type = view.GetType();
				foreach (var method in type.GetMethods(BindingFlags.Instance | BindingFlags.Public))
				{
					if (method.ReturnType != typeof(void) || method.GetParameters().Length != 0
						|| method.GetCustomAttribute<MethodReloadAttribute>() == null
						|| !MethodReloadRegistry.HasOverride(type.Name + "." + method.Name))
					{
						continue;
					}
					try
					{
						method.Invoke(view, null);
						ran++;
					}
					catch (Exception error)
					{
						UnityEngine.Debug.LogException(error.InnerException ?? error);
					}
				}
			}
			return ran;
		}
#endif
	}
}
