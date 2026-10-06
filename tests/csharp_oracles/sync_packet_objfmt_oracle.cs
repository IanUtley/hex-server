using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.Serialization;
using Game.Shared;
using Game.Shared.Network;
using Game.Shared.Network.GameSession;

namespace Game.Shared {
  [DataContract]
  public struct UID {
    [DataMember] public ulong m_UID64;
    public UID(ulong value) { m_UID64 = value; }
  }

  [DataContract]
  public abstract class SessionEventArgs {
    [DataMember] public int Class;
    [DataMember] public UID SessionId;
  }

  [DataContract]
  public sealed class NetworkPacketSessionEventArgs : SessionEventArgs {
    [DataMember] public UID PlayerId;
    [DataMember] public List<int> EventIds;
    [DataMember] public List<byte[]> EventData;
  }
}

namespace Game.Shared.Network.GameSession {
  [DataContract]
  public sealed class SessionSyncEventEventArgs {
    [DataMember] public UID RoutingPlayerId { get; set; }
    [DataMember] public SessionEventArgs SessionArgs { get; set; }
  }
}

public static class SyncPacketObjFmtOracle {
  private static string Describe(Type type) {
    if (!type.IsGenericType)
      return type.FullName;
    Type[] arguments = type.GetGenericArguments();
    string result = type.Namespace + "." + type.Name + "#" +
      Describe(arguments[0]);
    for (int index = 1; index < arguments.Length; index++)
      result += "!" + Describe(arguments[index]);
    return result;
  }

  private static string Pick(Type expected, object value) {
    return Describe(value == null ? expected : value.GetType());
  }

  private static bool CheckMember(MemberInfo member) {
    return member.IsDefined(typeof(DataMemberAttribute), false);
  }

  private static byte[] Encode(object value, Type declaredType) {
    var encoder = ObjFmt.MakeEncoder(Pick, CheckMember);
    using (var stream = new MemoryStream()) {
      encoder.Encode(stream, null, value, null, declaredType);
      return stream.ToArray();
    }
  }

  public static void Main(string[] args) {
    int[] eventIds;
    int payloadStart;
    int outputIndex;
    ulong playerId;
    int singleEventId;
    bool hasGenericSingleEventShape = args.Length == 4 &&
      (args[0].IndexOf(',') >= 0 || Int32.TryParse(args[0], out singleEventId));
    if (args.Length == 4 && !hasGenericSingleEventShape) {
      // Preserve the original phase/priority/chain fixture interface.
      eventIds = new int[] { 3, 48, 22 };
      payloadStart = 0;
      outputIndex = 3;
      playerId = (1234UL << 8) | 244UL;
    } else {
      if (args.Length < 4)
        throw new ArgumentException(
          "Expected event IDs, a routing UID, payload paths, and output path");
      string[] labels = args[0].Split(',');
      eventIds = new int[labels.Length];
      for (int index = 0; index < labels.Length; index++)
        eventIds[index] = Int32.Parse(labels[index]);
      if (args.Length != eventIds.Length + 3)
        throw new ArgumentException(
          "Event ID count does not match nested payload path count");
      playerId = UInt64.Parse(args[1]);
      payloadStart = 2;
      outputIndex = args.Length - 1;
    }

    var payloads = new List<byte[]>();
    for (int index = 0; index < eventIds.Length; index++)
      payloads.Add(File.ReadAllBytes(args[payloadStart + index]));

    var packet = new NetworkPacketSessionEventArgs {
      Class = 255,
      SessionId = new UID(42UL),
      PlayerId = new UID(playerId),
      EventIds = new List<int>(eventIds),
      EventData = payloads
    };
    var sync = new SessionSyncEventEventArgs {
      RoutingPlayerId = new UID(playerId),
      SessionArgs = packet
    };
    File.WriteAllBytes(
      args[outputIndex], Encode(sync, typeof(SessionSyncEventEventArgs)));
  }
}
