using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class PhasePriorityEventsOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void MatchWriter(byte[] expected, byte[] actual,
                                  int length, string name) {
    Require(expected.Length >= length, name + " C# writer returned a short buffer");
    Require(actual.Length == length, name + " Python payload length");
    for (int index = 0; index < length; index++)
      Require(actual[index] == expected[index],
              name + " differs from C# writer at byte " + index);
  }

  public static void Main(string[] args) {
    if (args.Length != 3)
      throw new ArgumentException(
        "Expected Python class-3, class-48, and class-22 fixture paths");

    const ulong player = (1234UL << 8) | 244UL;
    const ulong opponent = (1000UL << 8) | 3UL;
    var expectedPhase = new TurnPhaseUpdatedSessionEventArgs {
      SessionId = new UID(42UL),
      TurnPhase = (ETurnPhases)10,
      ActivePlayerId = new UID(player),
      PriorityPlayerId = new UID(opponent),
      PriorityPlayerChessTimerElapsed = 0x0102030405060708L
    };
    var phaseBytes = File.ReadAllBytes(args[0]);
    MatchWriter(expectedPhase.ToByteArray(), phaseBytes, 40,
                "class-3 TurnPhaseUpdated");
    var phase = SessionEventArgs.BuildArgs(3, phaseBytes)
                as TurnPhaseUpdatedSessionEventArgs;
    Require(phase != null && phase.Class == 3, "class-3 dispatch");
    Require(phase.SessionId.m_UID64 == 42UL, "class-3 session UID");
    Require((int)phase.TurnPhase == 10, "class-3 phase enum");
    Require(phase.ActivePlayerId.m_UID64 == player,
            "class-3 active-player UID");
    Require(phase.PriorityPlayerId.m_UID64 == opponent,
            "class-3 priority-player UID");
    Require(phase.PriorityPlayerChessTimerElapsed == 0x0102030405060708L,
            "class-3 timer field");

    var expectedGreenLight = new GreenLightSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID(opponent),
      Context = (EPriorityContext)6
    };
    var greenBytes = File.ReadAllBytes(args[1]);
    MatchWriter(expectedGreenLight.ToByteArray(), greenBytes, 24,
                "class-48 GreenLight");
    var green = SessionEventArgs.BuildArgs(48, greenBytes)
                as GreenLightSessionEventArgs;
    Require(green != null && green.Class == 48, "class-48 dispatch");
    Require(green.SessionId.m_UID64 == 42UL, "class-48 session UID");
    Require(green.PlayerId.m_UID64 == opponent, "class-48 player UID");
    Require((int)green.Context == 6, "class-48 priority-context enum");

    var expectedChain = new AbilityPushedOnChainSessionEventArgs {
      SessionId = new UID(42UL),
      AbilityInstanceId = 0x0102030405060708L,
      SourceCardId = new SessionCardId(new UID((201UL << 8) | 1UL)),
      TargetCardIds = new System.Collections.Generic.List<SessionCardId> {
        new SessionCardId(new UID((202UL << 8) | 1UL)),
        new SessionCardId(new UID((203UL << 8) | 1UL))
      },
      ChargePointCost = 2,
      LifeCost = 3,
      ResourceCost = 4,
      free = true,
      IgnoresChain = false,
      SecondaryAbility = true,
      AbilityTemplateId = new ResourceId(
        Guid.Parse("4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd")),
      abilityIndex = 7
    };
    var chainBytes = File.ReadAllBytes(args[2]);
    MatchWriter(expectedChain.ToByteArray(), chainBytes, 87,
                "class-22 AbilityPushedOnChain");
    var chain = SessionEventArgs.BuildArgs(22, chainBytes)
                as AbilityPushedOnChainSessionEventArgs;
    Require(chain != null && chain.Class == 22, "class-22 dispatch");
    Require(chain.SessionId.m_UID64 == 42UL, "class-22 session UID");
    Require(chain.AbilityInstanceId == 0x0102030405060708L,
            "class-22 ability instance ID");
    Require(chain.SourceCardId.UID.m_UID64 == ((201UL << 8) | 1UL),
            "class-22 source SessionCardId");
    Require(chain.TargetCardIds.Count == 2 &&
            chain.TargetCardIds[0].UID.m_UID64 == ((202UL << 8) | 1UL) &&
            chain.TargetCardIds[1].UID.m_UID64 == ((203UL << 8) | 1UL),
            "class-22 ordered target SessionCardIds");
    Require(chain.ChargePointCost == 2 && chain.LifeCost == 3 &&
            chain.ResourceCost == 4, "class-22 cost fields");
    Require(chain.free && !chain.IgnoresChain && chain.SecondaryAbility,
            "class-22 boolean fields");
    Require(chain.AbilityTemplateId.ToString() ==
            "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd",
            "class-22 ability ResourceId");
    Require(chain.abilityIndex == 7, "class-22 ability index");

    Console.WriteLine(
      "PASS extracted C# phase/priority/chain writers/readers: classes 3, 48, 22");
  }
}
