using System;
using System.IO;
using Game.Shared;

public static class ResourceCardPlayedEventOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python class-16 fixture path");

    var expected = new ResourceCardPlayedSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID((1234UL << 8) | 244UL),
      SessionCardId = new SessionCardId(new UID((201UL << 8) | 1UL)),
      free = false
    };
    byte[] expectedBytes = expected.ToByteArray();
    const int payloadLength = 29;
    Require(expectedBytes.Length >= payloadLength,
            "C# resource-play writer returned a short buffer");

    byte[] actualBytes = File.ReadAllBytes(args[0]);
    Require(actualBytes.Length == payloadLength,
            "Python resource-play event has unexpected length");
    for (int index = 0; index < payloadLength; index++)
      Require(actualBytes[index] == expectedBytes[index],
              "Python class-16 event differs from C# writer at byte " + index);

    var parsed = new ResourceCardPlayedSessionEventArgs(actualBytes);
    Require(parsed.Class == 16 && parsed.SessionId.m_UID64 == 42UL,
            "event class and session UID");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "resource controller UID");
    Require(parsed.SessionCardId.UID.m_UID64 == ((201UL << 8) | 1UL),
            "resource SessionCardId");
    Require(parsed.free == false, "PlayingForFree field");

    Console.WriteLine(
      "PASS extracted C# class-16 writer/reader: resource card played");
  }
}
