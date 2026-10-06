using System;
using System.IO;
using Game.Shared;

public static class SetupLifecycleEventsOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void MatchWriter(byte[] expected, byte[] actual,
                                  string name) {
    Require(expected.Length >= actual.Length,
            name + " C# writer returned a short buffer");
    for (int index = 0; index < actual.Length; index++)
      Require(actual[index] == expected[index],
              name + " differs from C# writer at byte " + index);
  }

  private static byte[] Read(string path) {
    return File.ReadAllBytes(path);
  }

  private static void CheckBase(SessionEventArgs value, int classId,
                                string name) {
    Require(value.Class == classId, name + " class ID");
    Require(value.SessionId.m_UID64 == 42UL, name + " session UID");
  }

  public static void Main(string[] args) {
    if (args.Length != 6)
      throw new ArgumentException(
        "Expected Python class-5, class-6, class-58, class-59, class-60, " +
        "and class-53 fixtures");

    const ulong player = (1234UL << 8) | 244UL;

    var expectedMulligan = new PlayerMulliganedHandSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID(player),
      NewCardCount = 6
    };
    byte[] mulliganBytes = Read(args[0]);
    MatchWriter(expectedMulligan.ToByteArray(), mulliganBytes,
                "class-5 PlayerMulliganedHand");
    var mulligan = new PlayerMulliganedHandSessionEventArgs(mulliganBytes);
    CheckBase(mulligan, 5, "class-5 PlayerMulliganedHand");
    Require(mulligan.PlayerId.m_UID64 == player,
            "class-5 player UID");
    Require(mulligan.NewCardCount == 6, "class-5 replacement-card count");

    var expectedAccepted = new PlayerAcceptedStartingHandSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID(player),
      Mulliganed = true
    };
    byte[] acceptedBytes = Read(args[1]);
    MatchWriter(expectedAccepted.ToByteArray(), acceptedBytes,
                "class-6 PlayerAcceptedStartingHand");
    var accepted = new PlayerAcceptedStartingHandSessionEventArgs(acceptedBytes);
    CheckBase(accepted, 6, "class-6 PlayerAcceptedStartingHand");
    Require(accepted.PlayerId.m_UID64 == player,
            "class-6 player UID");
    Require(accepted.Mulliganed, "class-6 mulligan flag");

    var expectedDraw = new PlayerWishesToDrawFirstSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID(player)
    };
    byte[] drawBytes = Read(args[2]);
    MatchWriter(expectedDraw.ToByteArray(), drawBytes,
                "class-58 PlayerWishesToDrawFirst");
    var draw = new PlayerWishesToDrawFirstSessionEventArgs(drawBytes);
    CheckBase(draw, 58, "class-58 PlayerWishesToDrawFirst");
    Require(draw.PlayerId.m_UID64 == player, "class-58 player UID");

    var expectedPlay = new PlayerWishesToPlayFirstSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID(player)
    };
    byte[] playBytes = Read(args[3]);
    MatchWriter(expectedPlay.ToByteArray(), playBytes,
                "class-59 PlayerWishesToPlayFirst");
    var play = new PlayerWishesToPlayFirstSessionEventArgs(playBytes);
    CheckBase(play, 59, "class-59 PlayerWishesToPlayFirst");
    Require(play.PlayerId.m_UID64 == player, "class-59 player UID");

    var expectedForcedFirst = new FirstPlayerDictatedSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID(player)
    };
    byte[] forcedFirstBytes = Read(args[4]);
    MatchWriter(expectedForcedFirst.ToByteArray(), forcedFirstBytes,
                "class-60 FirstPlayerDictated");
    var forcedFirst = new FirstPlayerDictatedSessionEventArgs(forcedFirstBytes);
    CheckBase(forcedFirst, 60, "class-60 FirstPlayerDictated");
    Require(forcedFirst.PlayerId.m_UID64 == player,
            "class-60 player UID");

    var expectedReconnect = new ReconnectDoneSessionEventArgs {
      SessionId = new UID(42UL)
    };
    byte[] reconnectBytes = Read(args[5]);
    MatchWriter(expectedReconnect.ToByteArray(), reconnectBytes,
                "class-53 ReconnectDone");
    var reconnect = new ReconnectDoneSessionEventArgs(reconnectBytes);
    CheckBase(reconnect, 53, "class-53 ReconnectDone");

    Console.WriteLine(
      "PASS extracted C# setup lifecycle writers/readers: classes 5, 6, 58, 59, 60, 53");
  }
}
