using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class PlayerUpdatedReaderOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void MatchWriter(byte[] expected, byte[] actual) {
    Require(expected.Length >= actual.Length,
            "class-65 C# writer buffer shorter than Python payload");
    for (int index = 0; index < actual.Length; index++)
      Require(actual[index] == expected[index],
              "class-65 differs from C# writer at byte " + index);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python class-65 fixture path");

    byte[] payload = File.ReadAllBytes(args[0]);
    var expected = new PlayerUpdatedSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID((1234UL << 8) | 244UL),
      Health = 17,
      Charges = 2,
      Resources = 4,
      TurnNumber = 9,
      TotalResources = 11,
      RemainingTime = new TimeSpan(1, 2, 3),
      ThresholdValues = new System.Collections.Generic.List<int> {
        1, 2, 3, 4, 5, 6
      },
      ChampionId = new SessionCardId(new UID((456UL << 8) | 1UL)),
      Thresholds = new System.Collections.Generic.List<ECardShards> {
        ECardShards.Colorless, ECardShards.Blood, ECardShards.Ruby,
        ECardShards.Sapphire, ECardShards.Wild, ECardShards.Diamond
      },
      MaxHandSize = 6,
      CanSeeEnemyHand = true,
      CanSeeEnemyUnderground = true,
      DeckSleeveId = new ResourceId(Guid.Parse(
        "11111111-2222-3333-4444-555555555555")),
      SpellPoints = 7
    };
    MatchWriter(expected.ToByteArray(), payload);

    var update = SessionEventArgs.BuildArgs(65, payload)
                 as PlayerUpdatedSessionEventArgs;
    Require(update != null, "BuildArgs class-65 dispatch");
    Require(update.Class == 65, "class ID");
    Require(update.SessionId.m_UID64 == 42UL, "session UID");
    Require(update.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "player UID");
    Require(update.Health == 17 && update.Charges == 2 &&
            update.Resources == 4 && update.TurnNumber == 9 &&
            update.TotalResources == 11, "player resource fields");
    Require(update.RemainingTime.Hours == 1 &&
            update.RemainingTime.Minutes == 2 &&
            update.RemainingTime.Seconds == 3, "remaining time");
    Require(update.ThresholdValues.Count == 6, "threshold value count");
    int[] expectedValues = { 1, 2, 3, 4, 5, 6 };
    for (int i = 0; i < expectedValues.Length; i++)
      Require(update.ThresholdValues[i] == expectedValues[i],
              "threshold value order " + i);
    ECardShards[] expectedThresholds = {
      ECardShards.Colorless, ECardShards.Blood, ECardShards.Ruby,
      ECardShards.Sapphire, ECardShards.Wild, ECardShards.Diamond
    };
    Require(update.Thresholds.Count == expectedThresholds.Length,
            "threshold key count");
    for (int i = 0; i < expectedThresholds.Length; i++)
      Require(update.Thresholds[i] == expectedThresholds[i],
              "threshold key/value alignment " + i);
    Require(update.ChampionId.UID.m_UID64 == ((456UL << 8) | 1UL),
            "typed champion SessionCardId");
    Require(update.MaxHandSize == 6, "max hand size");
    Require(update.CanSeeEnemyHand, "enemy hand visibility");
    Require(update.CanSeeEnemyUnderground, "enemy underground visibility");
    Require(update.DeckSleeveId.ToString() ==
            "11111111-2222-3333-4444-555555555555", "deck sleeve");
    Require(update.SpellPoints == 7, "spell points");

    Console.WriteLine(
      "PASS extracted C# PlayerUpdated writer/reader: class 65 fields and thresholds");
  }
}
