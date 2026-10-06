using System;
using System.Collections.Generic;
using System.IO;
using Game.Shared;

public static class TriggeredAbilityEventOracle {
  private const ulong Player = (1234UL << 8) | 244UL;
  private const long FirstAbilityId = 0x0102030405060708L;
  private const long SecondAbilityId = 0x1112131415161718L;
  private const ulong FirstSourceId = (201UL << 8) | 1UL;
  private const ulong SecondSourceId = (202UL << 8) | 1UL;
  private const string FirstTemplate = "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd";
  private const string SecondTemplate = "9cfd4fc2-9877-49c0-8182-877a5ccf4105";

  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void MatchWriter(byte[] expected, byte[] actual) {
    Require(expected.Length >= actual.Length,
            "class 39 C# writer buffer shorter than Python payload");
    for (int index = 0; index < actual.Length; index++)
      Require(actual[index] == expected[index],
              "class 39 differs from C# writer at byte " + index);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one class-39 fixture path");

    var expected = new TriggeredAbilityActivationDataRequiredSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID(Player),
      AbilityInstanceIds = new List<long> {
        FirstAbilityId, SecondAbilityId
      },
      AbilityTemplateIds = new List<ResourceId> {
        new ResourceId(Guid.Parse(FirstTemplate)),
        new ResourceId(Guid.Parse(SecondTemplate))
      },
      SourceCardIds = new List<SessionCardId> {
        new SessionCardId(new UID(FirstSourceId)),
        new SessionCardId(new UID(SecondSourceId))
      }
    };
    byte[] payload = File.ReadAllBytes(args[0]);
    MatchWriter(expected.ToByteArray(), payload);

    var parsed = SessionEventArgs.BuildArgs(39, payload)
                 as TriggeredAbilityActivationDataRequiredSessionEventArgs;
    Require(parsed != null && parsed.Class == 39,
            "class 39 dispatch");
    Require(parsed.SessionId.m_UID64 == 42UL &&
            parsed.PlayerId.m_UID64 == Player,
            "class 39 session/player UID");
    Require(parsed.AbilityInstanceIds.Count == 2 &&
            parsed.AbilityInstanceIds[0] == FirstAbilityId &&
            parsed.AbilityInstanceIds[1] == SecondAbilityId,
            "class 39 ordered ability instance IDs");
    Require(parsed.AbilityTemplateIds.Count == 2 &&
            parsed.AbilityTemplateIds[0].ToString() == FirstTemplate &&
            parsed.AbilityTemplateIds[1].ToString() == SecondTemplate,
            "class 39 ordered ability template IDs");
    Require(parsed.SourceCardIds.Count == 2 &&
            parsed.SourceCardIds[0].UID.m_UID64 == FirstSourceId &&
            parsed.SourceCardIds[1].UID.m_UID64 == SecondSourceId,
            "class 39 ordered source SessionCardIds");

    Console.WriteLine(
      "PASS extracted C# triggered-ability activation writer/reader: class 39");
  }
}
