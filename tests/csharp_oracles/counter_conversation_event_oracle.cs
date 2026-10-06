using System;
using System.IO;
using Game.Shared;

public static class CounterConversationEventOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  private static bool Same(byte[] left, byte[] right) {
    if (left.Length != right.Length) return false;
    for (int i = 0; i < left.Length; i++) {
      if (left[i] != right[i]) return false;
    }
    return true;
  }

  private static CardCountersChangedSessionEventArgs CounterEvent() {
    return new CardCountersChangedSessionEventArgs {
      SessionId = new UID(42UL),
      SessionCardId = new SessionCardId(new UID((201UL << 8) | 1UL)),
      CardCounterTemplateId = new ResourceId(
        Guid.Parse("11111111-2222-3333-4444-555555555555")),
      NewValue = 9,
      OldValue = 4
    };
  }

  private static EncounterModDialogSessionEventArgs ConversationEvent() {
    return new EncounterModDialogSessionEventArgs {
      SessionId = new UID(42UL),
      PlayerId = new UID((1234UL << 8) | 244UL),
      ConversationTemplateId = new ResourceId(
        Guid.Parse("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    };
  }

  private static void WriteFixtures(string counterPath, string conversationPath) {
    File.WriteAllBytes(counterPath, CounterEvent().ToByteArray());
    File.WriteAllBytes(conversationPath, ConversationEvent().ToByteArray());
  }

  private static void VerifyCounter(string path) {
    byte[] actual = File.ReadAllBytes(path);
    byte[] expected = CounterEvent().ToByteArray();
    Require(Same(actual, expected), "class-54 C# writer bytes");
    var parsed = SessionEventArgs.BuildArgs(54, actual)
      as CardCountersChangedSessionEventArgs;
    Require(parsed != null, "class-54 BuildArgs dispatch");
    Require(parsed.SessionId.m_UID64 == 42UL, "class-54 session UID");
    Require(parsed.SessionCardId.UID.m_UID64 == ((201UL << 8) | 1UL),
            "class-54 typed SessionCardId");
    Require(parsed.CardCounterTemplateId.ToString() ==
            "11111111-2222-3333-4444-555555555555",
            "class-54 counter ResourceId");
    Require(parsed.NewValue == 9 && parsed.OldValue == 4,
            "class-54 new/old counter values");
    Require(Same(parsed.ToByteArray(), actual), "class-54 reader/writer roundtrip");
  }

  private static void VerifyConversation(string path) {
    byte[] actual = File.ReadAllBytes(path);
    byte[] expected = ConversationEvent().ToByteArray();
    Require(Same(actual, expected), "class-55 C# writer bytes");
    var parsed = SessionEventArgs.BuildArgs(55, actual)
      as EncounterModDialogSessionEventArgs;
    Require(parsed != null, "class-55 BuildArgs dispatch");
    Require(parsed.SessionId.m_UID64 == 42UL, "class-55 session UID");
    Require(parsed.PlayerId.m_UID64 == ((1234UL << 8) | 244UL),
            "class-55 player UID");
    Require(parsed.ConversationTemplateId.ToString() ==
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "class-55 conversation ResourceId");
    Require(Same(parsed.ToByteArray(), actual), "class-55 reader/writer roundtrip");
  }

  public static void Main(string[] args) {
    if (args.Length == 3 && args[0] == "--write") {
      WriteFixtures(args[1], args[2]);
      Console.WriteLine("WROTE extracted C# class-54 and class-55 fixtures");
      return;
    }
    if (args.Length != 2)
      throw new ArgumentException("Expected counter and conversation event fixture paths");
    VerifyCounter(args[0]);
    VerifyConversation(args[1]);
    Console.WriteLine("PASS extracted C# class-54 and class-55 writers/readers");
  }
}
