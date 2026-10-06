using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class TransformEventReaderOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  public static void Main(string[] args) {
    if (args.Length != 2)
      throw new ArgumentException("Expected Python class-44 and class-45 fixtures");

    var transformed = new CardTransformedSessionEventArgs(
        File.ReadAllBytes(args[0]));
    Require(transformed.Class == 44, "CardTransformed class ID");
    Require(transformed.SessionId.m_UID64 == 42UL, "transformed session UID");
    Require(transformed.SessionCardId.UID.m_UID64 == ((456UL << 8) | 1UL),
            "transformed SessionCardId");
    Require(transformed.CardTemplateId.ToString() ==
            "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd",
            "transformed ResourceId");
    Require(transformed.IsReplica, "replica flag");
    Require((ulong)transformed.Gems == 0x123456789ABCDEF0UL,
            "64-bit gem enum");

    var reverted = new CardRevertedSessionEventArgs(
        File.ReadAllBytes(args[1]));
    Require(reverted.Class == 45, "CardReverted class ID");
    Require(reverted.SessionId.m_UID64 == 42UL, "reverted session UID");
    Require(reverted.SessionCardId.UID.m_UID64 == ((456UL << 8) | 1UL),
            "reverted SessionCardId");
    Require(reverted.CardTemplateId.ToString() ==
            "5badd4e4-5af4-409d-9d1f-1b839b39c35e",
            "reverted ResourceId");

    Console.WriteLine("PASS extracted C# transform readers: classes 44 and 45");
  }
}
