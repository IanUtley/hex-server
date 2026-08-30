using BepInEx;
using HarmonyLib;
using System;
using UnityEngine;

namespace HexDebug
{
    [BepInPlugin("hex.debug.trace", "Hex Event Trace", "1.0.0")]
    public class EventTracePlugin : BaseUnityPlugin
    {
        void Awake()
        {
            var harmony = new Harmony("hex.debug.trace");
            harmony.PatchAll();
            Logger.LogInfo("Event trace patcher loaded");
        }
    }

    /// Patch BuildArgs to log every call + catch exceptions before the try/catch swallows them
    [HarmonyPatch(typeof(Game.Shared.SessionEventArgs), "BuildArgs")]
    class Patch_BuildArgs
    {
        static void Prefix(int classId, byte[] arg)
        {
            Debug.Log($"[TRACE] BuildArgs({classId}, {arg?.Length ?? 0}b)");
        }

        static void Postfix(int classId, byte[] arg, Game.Shared.SessionEventArgs __result)
        {
            if (__result == null)
                Debug.LogWarning($"[TRACE] BuildArgs({classId}) → null!");
        }
    }

    /// Patch OnPlayerAdded to confirm it fires
    [HarmonyPatch(typeof(Game.Client.GameClient), "OnPlayerAdded")]
    class Patch_OnPlayerAdded
    {
        static void Prefix(Game.Shared.Network.GameSession.PlayerAddedEventArgs args)
        {
            Debug.Log($"[TRACE] OnPlayerAdded — RoutingPlayerId={args?.RoutingPlayerId} PlayerState.PlayerId={args?.PlayerState?.PlayerId}");
        }
    }
}
