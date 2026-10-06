using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class CardZoneEventsOracle {
  private const ulong Session = 42UL;
  private const ulong Player = (1234UL << 8) | 244UL;
  private const ulong Card = (201UL << 8) | 1UL;
  private const ulong ResponsibleCard = (202UL << 8) | 1UL;
  private const ulong ChampionCard = (203UL << 8) | 1UL;
  private const ulong TroopCard = (204UL << 8) | 1UL;
  private const ulong ResourceCard = (205UL << 8) | 1UL;
  private const ulong SpellCard = (206UL << 8) | 1UL;
  private const ulong ArtifactCard = (207UL << 8) | 1UL;

  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void Compare(byte[] actual, byte[] expected, int length,
                              string label) {
    Require(expected.Length >= length, label + " C# writer returned a short buffer");
    Require(actual.Length == length, label + " Python event length");
    for (int index = 0; index < length; index++)
      Require(actual[index] == expected[index],
              label + " differs from C# writer at byte " + index);
  }

  private static SessionCardId CardId(ulong uid) {
    return new SessionCardId(new UID(uid));
  }

  private static void VerifyDrawn(string path) {
    var expected = new CardDrawnSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(Card), NthCardDrawnThisTurnByThisPlayer = 3
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 32, "class 7 CardDrawn");
    var parsed = new CardDrawnSessionEventArgs(actual);
    Require(parsed.Class == 7 && parsed.SessionId.m_UID64 == Session,
            "class 7 identity");
    Require(parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == Card &&
            parsed.NthCardDrawnThisTurnByThisPlayer == 3,
            "class 7 player, card, and draw ordinal");
  }

  private static void VerifyDestroyed(string path) {
    var expected = new CardDestroyedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(Card), ResponsibleCardId = CardId(ResponsibleCard)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 36, "class 8 CardDestroyed");
    var parsed = new CardDestroyedSessionEventArgs(actual);
    Require(parsed.Class == 8 && parsed.SessionId.m_UID64 == Session,
            "class 8 identity");
    Require(parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == Card &&
            parsed.ResponsibleCardId.UID.m_UID64 == ResponsibleCard,
            "class 8 player, destroyed card, and responsible card");
  }

  private static void VerifyDiscarded(string path) {
    var expected = new CardDiscardedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(Card)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28, "class 10 CardDiscarded");
    var parsed = new CardDiscardedSessionEventArgs(actual);
    Require(parsed.Class == 10 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == Card,
            "class 10 identity and card");
  }

  private static void VerifyVoided(string path) {
    var expected = new CardVoidedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(Card)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28, "class 11 CardVoided");
    var parsed = new CardVoidedSessionEventArgs(actual);
    Require(parsed.Class == 11 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == Card,
            "class 11 identity and card");
  }

  private static void VerifyGraveyarded(string path) {
    var expected = new CardGraveyardedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(Card)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28, "class 12 CardGraveyarded");
    var parsed = new CardGraveyardedSessionEventArgs(actual);
    Require(parsed.Class == 12 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == Card,
            "class 12 identity and card");
  }

  private static void VerifyChampionPlayed(string path) {
    var expected = new ChampionCardPlayedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player), IsAI = true,
      PlayerName = "Alpha", SessionCardId = CardId(ChampionCard)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 35,
            "class 14 ChampionCardPlayed");
    var parsed = new ChampionCardPlayedSessionEventArgs(actual);
    Require(parsed.Class == 14 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player && parsed.IsAI &&
            parsed.PlayerName == "Alpha" &&
            parsed.SessionCardId.UID.m_UID64 == ChampionCard,
            "class 14 player, AI flag, name, and card");
  }

  private static void VerifyTroopPlayed(string path) {
    var expected = new TroopCardPlayedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(TroopCard)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28, "class 15 TroopCardPlayed");
    var parsed = new TroopCardPlayedSessionEventArgs(actual);
    Require(parsed.Class == 15 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == TroopCard,
            "class 15 player and card");
  }

  private static void VerifyResourcePlayed(string path) {
    var expected = new ResourceCardPlayedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(ResourceCard), free = true
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 29,
            "class 16 ResourceCardPlayed");
    var parsed = new ResourceCardPlayedSessionEventArgs(actual);
    Require(parsed.Class == 16 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == ResourceCard && parsed.free,
            "class 16 player, card, and free-play flag");
  }

  private static void VerifySpellPlayed(string path) {
    var expected = new SpellCardPlayedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(SpellCard)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28, "class 17 SpellCardPlayed");
    var parsed = new SpellCardPlayedSessionEventArgs(actual);
    Require(parsed.Class == 17 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == SpellCard,
            "class 17 player and card");
  }

  private static void VerifySpellCast(string path) {
    var expected = new SpellCardCastSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(SpellCard), PlayedForFree = true
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 29, "class 18 SpellCardCast");
    var parsed = new SpellCardCastSessionEventArgs(actual);
    Require(parsed.Class == 18 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == SpellCard &&
            parsed.PlayedForFree,
            "class 18 player, card, and free-play flag");
  }

  private static void VerifyArtifactPlayed(string path) {
    var expected = new ArtifactCardPlayedSessionEventArgs {
      SessionId = new UID(Session), PlayerId = new UID(Player),
      SessionCardId = CardId(ArtifactCard)
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 28,
            "class 19 ArtifactCardPlayed");
    var parsed = new ArtifactCardPlayedSessionEventArgs(actual);
    Require(parsed.Class == 19 && parsed.SessionId.m_UID64 == Session &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.SessionCardId.UID.m_UID64 == ArtifactCard,
            "class 19 player and card");
  }

  private static void VerifyMoved(string path) {
    var expected = new CardMovedSessionEventArgs {
      SessionId = new UID(Session), SessionCardId = CardId(Card),
      PlayerId = new UID(Player), Collection = ECardCollections.Warzone,
      Location = ECardLocations.Bottom, Index = 17
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), 40, "class 50 CardMoved");
    var parsed = new CardMovedSessionEventArgs(actual);
    Require(parsed.Class == 50 && parsed.SessionId.m_UID64 == Session &&
            parsed.SessionCardId.UID.m_UID64 == Card &&
            parsed.PlayerId.m_UID64 == Player &&
            parsed.Collection == ECardCollections.Warzone &&
            parsed.Location == ECardLocations.Bottom && parsed.Index == 17,
            "class 50 identity, controller, and destination");
  }

  public static void Main(string[] args) {
    if (args.Length != 12)
      throw new ArgumentException(
        "Expected class-7/8/10/11/12/14-19/50 Python fixture paths");
    VerifyDrawn(args[0]);
    VerifyDestroyed(args[1]);
    VerifyDiscarded(args[2]);
    VerifyVoided(args[3]);
    VerifyGraveyarded(args[4]);
    VerifyChampionPlayed(args[5]);
    VerifyTroopPlayed(args[6]);
    VerifyResourcePlayed(args[7]);
    VerifySpellPlayed(args[8]);
    VerifySpellCast(args[9]);
    VerifyArtifactPlayed(args[10]);
    VerifyMoved(args[11]);
    Console.WriteLine(
      "PASS extracted C# card event writers/readers: classes 7, 8, 10, 11, 12, 14-19, 50");
  }
}
