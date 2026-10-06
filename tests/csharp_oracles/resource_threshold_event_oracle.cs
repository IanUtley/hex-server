using System;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class ResourceThresholdEventOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python class-35 fixture path");

    var expected = new PlayerResourceThresholdChangedSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID((1234UL << 8) | 244UL),
      Color = ECardShards.Blood,
      Operation = (EOperations)2,
      Delta = -2,
      NewValue = 3
    };
    byte[] expectedBytes = expected.ToByteArray();
    const int payloadLength = 36;
    Require(expectedBytes.Length >= payloadLength,
            "C# threshold event writer returned a short buffer");

    byte[] actualBytes = File.ReadAllBytes(args[0]);
    Require(actualBytes.Length == payloadLength,
            "Python threshold event has unexpected trailing or missing bytes");
    for (int index = 0; index < payloadLength; index++)
      Require(actualBytes[index] == expectedBytes[index],
              "Python threshold event differs from C# writer at byte " + index);

    var parsed = new PlayerResourceThresholdChangedSessionEventArgs(actualBytes);
    Require(parsed.Class == 35, "event class ID");
    Require(parsed.SessionId.m_UID64 == 42UL, "session UID");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "player UID");
    Require(parsed.Color == ECardShards.Blood, "shard enum");
    Require(Convert.ToInt32(parsed.Operation) == 2, "operation enum");
    Require(parsed.Delta == -2, "signed threshold delta");
    Require(parsed.NewValue == 3, "resulting threshold value");

    Console.WriteLine(
      "PASS extracted C# class-35 writer/reader: player threshold change");
  }
}
