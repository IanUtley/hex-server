using System;
using System.IO;
using Game.Shared;

public static class ChainLifecycleEventsOracle {
  private const long AbilityInstanceId = 0x0102030405060708L;

  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static void MatchWriter(byte[] expected, byte[] actual, string name) {
    Require(expected.Length >= actual.Length,
            name + " C# writer buffer shorter than Python payload");
    for (int index = 0; index < actual.Length; index++)
      Require(actual[index] == expected[index],
              name + " differs from C# writer at byte " + index);
  }

  public static void Main(string[] args) {
    if (args.Length != 3)
      throw new ArgumentException("Expected classes 41, 42, and 77 fixtures");

    var resolvedExpected = new TopOfChainResolvedSessionEventArgs {
      SessionId = new UID(42UL), AbilityInstanceId = AbilityInstanceId
    };
    byte[] resolvedBytes = File.ReadAllBytes(args[0]);
    MatchWriter(resolvedExpected.ToByteArray(), resolvedBytes, "class 41");
    var resolved = SessionEventArgs.BuildArgs(41, resolvedBytes)
                    as TopOfChainResolvedSessionEventArgs;
    Require(resolved != null && resolved.Class == 41 &&
            resolved.SessionId.m_UID64 == 42UL &&
            resolved.AbilityInstanceId == AbilityInstanceId,
            "class 41 dispatch and fields");

    var removedExpected = new RemovedTopOfChainSessionEventArgs {
      SessionId = new UID(42UL), AbilityInstanceId = AbilityInstanceId
    };
    byte[] removedBytes = File.ReadAllBytes(args[1]);
    MatchWriter(removedExpected.ToByteArray(), removedBytes, "class 42");
    var removed = SessionEventArgs.BuildArgs(42, removedBytes)
                   as RemovedTopOfChainSessionEventArgs;
    Require(removed != null && removed.Class == 42 &&
            removed.SessionId.m_UID64 == 42UL &&
            removed.AbilityInstanceId == AbilityInstanceId,
            "class 42 dispatch and fields");

    var emptyExpected = new ChainEmptySessionEventArgs {
      SessionId = new UID(42UL)
    };
    byte[] emptyBytes = File.ReadAllBytes(args[2]);
    MatchWriter(emptyExpected.ToByteArray(), emptyBytes, "class 77");
    var empty = SessionEventArgs.BuildArgs(77, emptyBytes)
                as ChainEmptySessionEventArgs;
    Require(empty != null && empty.Class == 77 &&
            empty.SessionId.m_UID64 == 42UL,
            "class 77 dispatch and fields");

    Console.WriteLine(
      "PASS extracted C# chain lifecycle writers/readers: classes 41, 42, 77");
  }
}
