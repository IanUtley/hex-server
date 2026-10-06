using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class ResourcePoolEventsOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void Compare(byte[] actual, byte[] expected, string label) {
    const int serializedLength = 32;
    Require(actual.Length == serializedLength,
            label + " Python event has unexpected length");
    Require(expected.Length >= serializedLength,
            label + " C# writer returned a short buffer");
    for (int index = 0; index < serializedLength; index++)
      Require(actual[index] == expected[index],
              label + " differs from C# writer at byte " + index);
  }

  private static void VerifyCurrentPool(string path) {
    var expected = new PlayerCurrentResourcePoolChangedSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID((1234UL << 8) | 244UL),
      Operation = (EOperations)1, Delta = 5, NewValue = 11
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), "class 33");
    var parsed = new PlayerCurrentResourcePoolChangedSessionEventArgs(actual);
    Require(parsed.Class == 33 && parsed.SessionId.m_UID64 == 42UL,
            "class 33 identity");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "class 33 player UID");
    Require(Convert.ToInt32(parsed.Operation) == 1 && parsed.Delta == 5 &&
            parsed.NewValue == 11, "class 33 operation and values");
  }

  private static void VerifyTotalPool(string path) {
    var expected = new PlayerTotalResourcePoolChangedSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID((1234UL << 8) | 244UL),
      Operation = (EOperations)2, Delta = -2, NewValue = 9
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), "class 34");
    var parsed = new PlayerTotalResourcePoolChangedSessionEventArgs(actual);
    Require(parsed.Class == 34 && parsed.SessionId.m_UID64 == 42UL,
            "class 34 identity");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "class 34 player UID");
    Require(Convert.ToInt32(parsed.Operation) == 2 && parsed.Delta == -2 &&
            parsed.NewValue == 9, "class 34 operation and values");
  }

  private static void VerifyCharge(string path) {
    var expected = new ChampionChargePointsChangedSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID((1234UL << 8) | 244UL),
      Operation = (EOperations)3, Delta = 1, NewValue = 1
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), "class 36");
    var parsed = new ChampionChargePointsChangedSessionEventArgs(actual);
    Require(parsed.Class == 36 && parsed.SessionId.m_UID64 == 42UL,
            "class 36 identity");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "class 36 player UID");
    Require(Convert.ToInt32(parsed.Operation) == 3 && parsed.Delta == 1 &&
            parsed.NewValue == 1, "class 36 operation and values");
  }

  private static void VerifySpellPoints(string path) {
    var expected = new ChampionSpellPointsChangedSessionEventArgs {
      SessionId = new UID(42UL), PlayerId = new UID((1234UL << 8) | 244UL),
      Operation = (EOperations)2, Delta = -1, NewValue = 0
    };
    byte[] actual = File.ReadAllBytes(path);
    Compare(actual, expected.ToByteArray(), "class 37");
    var parsed = new ChampionSpellPointsChangedSessionEventArgs(actual);
    Require(parsed.Class == 37 && parsed.SessionId.m_UID64 == 42UL,
            "class 37 identity");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "class 37 player UID");
    Require(Convert.ToInt32(parsed.Operation) == 2 && parsed.Delta == -1 &&
            parsed.NewValue == 0, "class 37 operation and values");
  }

  public static void Main(string[] args) {
    if (args.Length != 4)
      throw new ArgumentException(
        "Expected Python class-33, 34, 36, and 37 fixture paths");
    VerifyCurrentPool(args[0]);
    VerifyTotalPool(args[1]);
    VerifyCharge(args[2]);
    VerifySpellPoints(args[3]);
    Console.WriteLine(
      "PASS extracted C# resource event writers/readers: classes 33, 34, 36, 37");
  }
}
