using System.Reflection;
using UnityEngine;

public static class DebugLogEnabler
{
    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
    static void OnBeforeSceneLoad()
    {
        EnableLoggers();
    }

    static void EnableLoggers()
    {
        var logBase = System.Type.GetType("LogBase, Assembly-CSharp-firstpass");
        var elogLevel = System.Type.GetType("ELogLevel, Assembly-CSharp-firstpass");
        if (logBase == null || elogLevel == null) return;

        var configure = logBase.GetMethod("Configure", BindingFlags.Public | BindingFlags.Static);
        if (configure == null) return;

        int traceVal = 0; // ELogLevel.Trace
        object trace = System.Enum.ToObject(elogLevel, traceVal);

        configure.Invoke(null, new object[] { "Game.Shared.ClientSessionBase", trace });
        configure.Invoke(null, new object[] { "UIBattle", trace });
        configure.Invoke(null, new object[] { "Game.Client.Tournament.TournamentManager", trace });
        configure.Invoke(null, new object[] { "Game.Client.GameClient", trace });
    }
}
