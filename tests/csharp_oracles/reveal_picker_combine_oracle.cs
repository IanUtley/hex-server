using System;
using System.Collections.Generic;
using System.IO;
using Game.Shared;

public static class RevealPickerCombineOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static SessionEventArgs Find(List<SessionEventArgs> events, int classId) {
    foreach (SessionEventArgs item in events) {
      if (item.Class == classId) return item;
    }
    return null;
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one reveal plus picker event batch");

    var packet = new NetworkPacketSessionEventArgs();
    packet.SessionId = new UID(42UL);
    packet.PlayerId = new UID(((ulong)1234 << 8) | 244UL);
    using (var input = new BinaryReader(File.OpenRead(args[0]))) {
      int count = input.ReadInt32();
      for (int i = 0; i < count; i++) {
        packet.EventIds.Add(input.ReadInt32());
        int size = input.ReadInt32();
        packet.EventData.Add(input.ReadBytes(size));
      }
      Require(input.BaseStream.Position == input.BaseStream.Length,
              "fixture has trailing bytes");
    }

    BulkSessionEventSessionEventArgs decoded = packet.Get();
    Require(Find(decoded.EventList, 51) == null,
            "matching reveal should be consumed into the picker");
    var request = Find(decoded.EventList, 23) as
      AbilityActivationDataRequiredSessionEventArgs;
    Require(request != null && request.AbilityParentId == 700L,
            "activation request and reveal parent should match");

    var list = Find(decoded.EventList, 70) as PlayerOptionListSessionEventArgs;
    Require(list != null && list.Options.Count == 2,
            "both option cards should remain in the option list");
    var sourceOption = list.Options[0] as PlayerOptionSessionEventArgs;
    var otherOption = list.Options[1] as PlayerOptionSessionEventArgs;
    Require(sourceOption != null && sourceOption.Card.UID.m_UID64 ==
            ((ulong)101 << 8 | 1UL), "activation source card");

    var sourceInstance = sourceOption.Instances[0] as OptionInstanceSessionEventArgs;
    var sourceTarget = sourceInstance.TargetInstances[0] as
      TargetInstanceSessionEventArgs;
    Require(sourceTarget != null && sourceTarget.TargetIndex == 2,
            "authored target index survives decoding");
    Require(sourceTarget.Targets.Count == 1 &&
            sourceTarget.Targets[0].UID.m_UID64 == ((ulong)77 << 8 | 1UL),
            "legal target remains in Targets");
    Require(sourceTarget.AdditionalTargets.Count == 1 &&
            sourceTarget.AdditionalTargets[0].UID.m_UID64 ==
              ((ulong)78 << 8 | 1UL),
            "revealed non-legal target is appended to AdditionalTargets");

    var unrelatedInstance = (otherOption.Instances[0] as
      OptionInstanceSessionEventArgs);
    var unrelatedTarget = unrelatedInstance.TargetInstances[0] as
      TargetInstanceSessionEventArgs;
    Require(unrelatedTarget.AdditionalTargets.Count == 0,
            "revealed cards must not be added to another source card's option");

    Console.WriteLine("PASS original C# reveal-to-picker packet rewrite");
  }
}
