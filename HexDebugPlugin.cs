using BepInEx;
using UnityEngine;

namespace HexDebug
{
    [BepInPlugin("hex.debug.log", "Hex Debug Log Enabler", "1.0.0")]
    public class DebugLogEnablerPlugin : BaseUnityPlugin
    {
        void Awake()
        {
            LogBase.Configure("Game.Shared.ClientSessionBase", ELogLevel.Trace);
            LogBase.Configure("UIBattle", ELogLevel.Trace);
            LogBase.Configure("Game.Client.Tournament.TournamentManager", ELogLevel.Trace);
            LogBase.Configure("Game.Client.GameClient", ELogLevel.Trace);
            Logger.LogInfo("Debug logging enabled for battle/tournament trace");
        }
    }
}
