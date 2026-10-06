using System;
using System.Collections.Generic;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class CardUpdatedEventOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static ResourceId Rid(string value) {
    return new ResourceId(Guid.Parse(value));
  }

  private static Dictionary<ResourceId, int> RidValues(string id, int value) {
    return new Dictionary<ResourceId, int> { { Rid(id), value } };
  }

  private static CardUpdatedSessionEventArgs Fixture() {
    return new CardUpdatedSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID((1234UL << 8) | 244UL),
      SessionCardId = new SessionCardId(new UID((201UL << 8) | 1UL)),
      Cost = 3, Armor = 1, CurrentArmor = 2, Rage = 4, DMult = 5, CDMult = 6,
      Attack = 7, Defense = 8, MinConst = 9, Tunneling = 10, MinLimited = 11,
      Escalation = 12, Lethal = true, ExtendedArt = false,
      Controller = new UID((987UL << 8) | 3UL), SubType = "FixtureSubType",
      Id = Rid("11111111-2222-3333-4444-555555555555"),
      OrigTemplate = Rid("22222222-3333-4444-5555-666666666666"),
      AffectedId = Rid("33333333-4444-5555-6666-777777777777"),
      Type = (ECardTypes)2, State = (ECardStates)1,
      AICardStates = (EAICardStates)1, Attributes = (ECardAttributes)6,
      Collection = (ECardCollections)8,
      Abilities = new List<ResourceId> {
        Rid("44444444-5555-6666-7777-888888888888")
      },
      IntegerVariables = new Dictionary<string, int> { { "P2", 17 } },
      IntAttributes = new Dictionary<string, int> { { "Charge", 2 } },
      CounterTemplates = new List<ResourceId> {
        Rid("55555555-6666-7777-8888-999999999999")
      },
      CounterCounts = new List<int> { 9 },
      Gems = (EGemTypesNew)1UL,
      ThresholdList = new List<ECardShards> {
        ECardShards.Colorless, ECardShards.Blood
      },
      Nulling = false,
      RelatedCards = new List<SessionCardId> {
        new SessionCardId(new UID((202UL << 8) | 1UL))
      },
      StringAttrs = new Dictionary<string, string> {
        { "StoredName", "FixtureName" }
      },
      ActivationCostModifiers = RidValues(
        "66666666-7777-8888-9999-aaaaaaaaaaaa", 2),
      ChargePointCostModifiers = RidValues(
        "77777777-8888-9999-aaaa-bbbbbbbbbbbb", 3),
      SpellPointCostModifiers = RidValues(
        "88888888-9999-aaaa-bbbb-cccccccccccc", 4),
      UsesPerGameCounts = RidValues(
        "99999999-aaaa-bbbb-cccc-dddddddddddd", 5),
      CooldownCounts = RidValues(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 6),
      DamageShield = true,
      AffectingAbilities = new List<ResourceId> {
        Rid("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
      },
      Feral = true
    };
  }

  private static bool SameBytes(byte[] left, byte[] right) {
    if (left.Length != right.Length) return false;
    for (int index = 0; index < left.Length; index++)
      if (left[index] != right[index]) return false;
    return true;
  }

  private static void VerifyParsed(CardUpdatedSessionEventArgs card) {
    Require(card.Class == 64 && card.SessionId.m_UID64 == 42UL,
            "class and session UID");
    Require(card.PlayerId.m_UID64 == ((1234UL << 8) | 244UL), "player UID");
    Require(card.SessionCardId.UID.m_UID64 == ((201UL << 8) | 1UL),
            "SessionCardId");
    Require(card.Cost == 3 && card.Armor == 1 && card.CurrentArmor == 2 &&
            card.Rage == 4 && card.DMult == 5 && card.CDMult == 6 &&
            card.Attack == 7 && card.Defense == 8 && card.MinConst == 9 &&
            card.Tunneling == 10 && card.MinLimited == 11 &&
            card.Escalation == 12, "ordered card stats");
    Require(card.Lethal && !card.ExtendedArt, "card boolean fields");
    Require(card.Controller.m_UID64 == ((987UL << 8) | 3UL), "controller UID");
    Require(card.SubType == "FixtureSubType", "subtype string");
    Require(card.Id.ToString() == "11111111-2222-3333-4444-555555555555" &&
            card.OrigTemplate.ToString() ==
              "22222222-3333-4444-5555-666666666666" &&
            card.AffectedId.ToString() ==
              "33333333-4444-5555-6666-777777777777", "template IDs");
    Require(Convert.ToInt32(card.Type) == 2 &&
            Convert.ToInt32(card.State) == 1 &&
            Convert.ToInt32(card.AICardStates) == 1 &&
            Convert.ToInt32(card.Attributes) == 6 &&
            Convert.ToInt32(card.Collection) == 8, "card enum fields");
    Require(card.Abilities.Count == 1 && card.Abilities[0].ToString() ==
            "44444444-5555-6666-7777-888888888888", "abilities");
    Require(card.IntegerVariables["P2"] == 17 &&
            card.IntAttributes["Charge"] == 2, "integer maps");
    Require(card.CounterTemplates.Count == 1 && card.CounterCounts.Count == 1 &&
            card.CounterCounts[0] == 9, "counter templates/counts");
    Require(Convert.ToUInt64(card.Gems) == 1UL, "gem flags");
    Require(card.ThresholdList.Count == 2 &&
            Convert.ToInt32(card.ThresholdList[0]) == 1 &&
            Convert.ToInt32(card.ThresholdList[1]) == 4, "threshold list");
    Require(!card.Nulling, "nulling flag");
    Require(card.RelatedCards.Count == 1 &&
            card.RelatedCards[0].UID.m_UID64 == ((202UL << 8) | 1UL),
            "related card IDs");
    Require(card.StringAttrs["StoredName"] == "FixtureName", "string attrs");
    Require(card.ActivationCostModifiers.Count == 1 &&
            card.ChargePointCostModifiers.Count == 1 &&
            card.SpellPointCostModifiers.Count == 1 &&
            card.UsesPerGameCounts.Count == 1 &&
            card.CooldownCounts.Count == 1, "ability modifier maps");
    Require(card.DamageShield && card.AffectingAbilities.Count == 1 &&
            card.AffectingAbilities[0].ToString() ==
              "bbbbbbbb-cccc-dddd-eeee-ffffffffffff" && card.Feral,
            "damage shield, affecting ability, and feral fields");
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python class-64 fixture path");
    byte[] expected = Fixture().ToByteArray();
    byte[] actual = File.ReadAllBytes(args[0]);
    Require(actual.Length <= expected.Length,
            "Python card event exceeds the C# writer buffer");
    var prefix = new byte[actual.Length];
    Array.Copy(expected, prefix, actual.Length);
    Require(SameBytes(actual, prefix),
            "Python class-64 bytes differ from extracted C# writer");
    VerifyParsed(new CardUpdatedSessionEventArgs(actual));
    Console.WriteLine("PASS extracted C# class-64 writer/reader: CardUpdated");
  }
}
