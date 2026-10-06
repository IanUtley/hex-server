using System;
using System.Collections.Generic;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class SessionEventReaderOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python PlayerOptionList fixture path");

    var packet = new PlayerOptionListSessionEventArgs(File.ReadAllBytes(args[0]));
    Require(packet.Class == 70, "PlayerOptionList class ID");
    Require(packet.SessionId.m_UID64 == 42UL, "session UID");
    Require(packet.PlayerId.m_UID64 == ((1234UL << 8) | 244UL), "choosing player UID");
    Require(packet.Options.Count == 1, "option count");

    var cardOption = packet.Options[0] as PlayerOptionSessionEventArgs;
    Require(cardOption != null, "nested class 69");
    Require(cardOption.Card.UID.m_UID64 == ((101UL << 8) | 1UL), "option card ID");
    Require(cardOption.State == ECardUsage.Activate, "card usage enum");
    Require(cardOption.Instances.Count == 1, "ability option count");

    var option = cardOption.Instances[0] as OptionInstanceSessionEventArgs;
    Require(option != null, "nested class 68");
    Require(option.Id.ToString() == "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd",
            "option template ResourceId");
    Require(option.TargetIds.Count == 2, "target/cost target-template IDs");
    Require(option.MinTargetCounts.Count == 2 &&
            option.MaxTargetCounts.Count == 2, "min/max target counts");
    Require(option.TargetInstances.Count == 2, "nested target/cost records");

    var target = option.TargetInstances[0] as TargetInstanceSessionEventArgs;
    Require(target != null, "nested class 67");
    Require(target.TargetIndex == 2, "authored target index");
    Require(target.TargetId.ToString() == "5badd4e4-5af4-409d-9d1f-1b839b39c35e",
            "target template ResourceId");
    Require(target.Targets.Count == 1 &&
            target.Targets[0].UID.m_UID64 == ((77UL << 8) | 1UL), "legal target UID");
    Require(target.AdditionalTargets.Count == 1 &&
            target.AdditionalTargets[0].UID.m_UID64 == ((78UL << 8) | 1UL),
            "additional target UID");

    var cost = option.TargetInstances[1] as CostInstanceSessionEventArgs;
    Require(cost != null, "nested class 66");
    Require(cost.CostType == EAbilityCostType.SacrificeAbilityCostType,
            "cost-type enum");
    Require(cost.Targets.Count == 1 &&
            cost.Targets[0].UID.m_UID64 == ((99UL << 8) | 1UL), "legal cost UID");
    Require(cost.TargetTemplateId.ToString() ==
            "00000000-0000-0000-0000-000000000123", "cost template ResourceId");

    Console.WriteLine("PASS extracted C# PlayerOptionList parser: classes 70, 69, 68, 67, 66");
  }
}
