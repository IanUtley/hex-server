using System;
using System.Collections.Generic;
using System.IO;
using Game.Shared;
using Game.Shared.Mechanics;

public static class CombatEventsOracle {
  private const ulong Player = (1234UL << 8) | 244UL;
  private const ulong Attacker = (101UL << 8) | 1UL;
  private const ulong Defender = (900UL << 8) | 1UL;
  private const long Serial = 0x0102030405060708L;

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

  private static SessionCardId Card(ulong value) {
    return new SessionCardId(new UID(value));
  }

  private static void CheckCombatId(CombatId actual, string name) {
    Require(actual.Attacker.m_UID64 == Player, name + " combat attacker UID");
    Require(actual.SerialNumber == Serial, name + " combat serial");
  }

  private static void CheckCards(List<SessionCardId> actual,
                                 ulong[] expected, string name) {
    Require(actual != null && actual.Count == expected.Length,
            name + " card count");
    for (int index = 0; index < expected.Length; index++)
      Require(actual[index].UID.m_UID64 == expected[index],
              name + " card order at " + index);
  }

  public static void Main(string[] args) {
    if (args.Length != 7)
      throw new ArgumentException("Expected seven combat event fixture paths");

    var combatId = new CombatId(new UID(Player), Serial);
    var attacker = Card(Attacker);
    var defender = Card(Defender);
    var blockers = new List<SessionCardId> {
      Card((102UL << 8) | 1UL), Card((103UL << 8) | 1UL)
    };

    var attackExpected = new AttackDeclaredSessionEventArgs {
      SessionId = new UID(42UL), CombatId = combatId,
      AttackingPlayerId = new UID(Player), DefendingCardId = defender,
      AttackingCardId = attacker, ForcedOnReconnect = true
    };
    byte[] attackBytes = File.ReadAllBytes(args[0]);
    MatchWriter(attackExpected.ToByteArray(), attackBytes, "class 27");
    var attack = SessionEventArgs.BuildArgs(27, attackBytes)
                 as AttackDeclaredSessionEventArgs;
    Require(attack != null && attack.Class == 27, "class 27 dispatch");
    Require(attack.SessionId.m_UID64 == 42UL, "class 27 session UID");
    CheckCombatId(attack.CombatId, "class 27");
    Require(attack.AttackingPlayerId.m_UID64 == Player,
            "class 27 attacking player UID");
    Require(attack.DefendingCardId.UID.m_UID64 == Defender &&
            attack.AttackingCardId.UID.m_UID64 == Attacker,
            "class 27 SessionCardIds");
    Require(attack.ForcedOnReconnect, "class 27 reconnect flag");

    var blockersExpected = new BlockersAssignedSessionEventArgs {
      SessionId = new UID(42UL), CombatId = combatId, AttackerId = attacker,
      DefenderId = defender, BlockingCardIds = blockers
    };
    byte[] blockersBytes = File.ReadAllBytes(args[1]);
    MatchWriter(blockersExpected.ToByteArray(), blockersBytes, "class 28");
    var assigned = SessionEventArgs.BuildArgs(28, blockersBytes)
                   as BlockersAssignedSessionEventArgs;
    Require(assigned != null && assigned.Class == 28, "class 28 dispatch");
    Require(assigned.SessionId.m_UID64 == 42UL, "class 28 session UID");
    CheckCombatId(assigned.CombatId, "class 28");
    Require(assigned.AttackerId.UID.m_UID64 == Attacker &&
            assigned.DefenderId.UID.m_UID64 == Defender,
            "class 28 attacker/defender IDs");
    CheckCards(assigned.BlockingCardIds,
              new ulong[] { (102UL << 8) | 1UL, (103UL << 8) | 1UL },
              "class 28 blockers");

    var firstStrikeExpected = new CombatPhaseResolvedSessionEventArgs {
      SessionId = new UID(42UL), CombatId = combatId, AttackerId = attacker,
      DefenderId = defender, BlockingCardIds = blockers,
      CombatPhase = ECombatPhase.FirstStrike
    };
    byte[] firstStrikeBytes = File.ReadAllBytes(args[3]);
    MatchWriter(firstStrikeExpected.ToByteArray(), firstStrikeBytes,
                "class 29 first strike");
    var firstStrike = SessionEventArgs.BuildArgs(29, firstStrikeBytes)
                      as CombatPhaseResolvedSessionEventArgs;
    Require(firstStrike != null && firstStrike.Class == 29,
            "class 29 first-strike dispatch");
    Require(firstStrike.SessionId.m_UID64 == 42UL,
            "class 29 first-strike session UID");
    CheckCombatId(firstStrike.CombatId, "class 29 first strike");
    Require((int)firstStrike.CombatPhase == 2,
            "class 29 first-strike enum");
    CheckCards(firstStrike.BlockingCardIds,
              new ulong[] { (102UL << 8) | 1UL, (103UL << 8) | 1UL },
              "class 29 blockers");

    var standardExpected = new CombatPhaseResolvedSessionEventArgs {
      SessionId = new UID(42UL), CombatId = combatId, AttackerId = attacker,
      DefenderId = defender, BlockingCardIds = blockers,
      CombatPhase = ECombatPhase.Standard
    };
    byte[] standardBytes = File.ReadAllBytes(args[4]);
    MatchWriter(standardExpected.ToByteArray(), standardBytes,
                "class 29 standard damage");
    var standard = SessionEventArgs.BuildArgs(29, standardBytes)
                   as CombatPhaseResolvedSessionEventArgs;
    Require(standard != null && standard.Class == 29,
            "class 29 standard dispatch");
    Require((int)standard.CombatPhase == 1, "class 29 standard enum");

    var beginExpected = new BeginCombatResolutionSessionEventArgs {
      SessionId = new UID(42UL)
    };
    byte[] beginBytes = File.ReadAllBytes(args[2]);
    MatchWriter(beginExpected.ToByteArray(), beginBytes, "class 30");
    var begin = SessionEventArgs.BuildArgs(30, beginBytes)
                as BeginCombatResolutionSessionEventArgs;
    Require(begin != null && begin.Class == 30 &&
            begin.SessionId.m_UID64 == 42UL, "class 30 dispatch and fields");

    var endExpected = new EndCombatResolutionSessionEventArgs {
      SessionId = new UID(42UL)
    };
    byte[] endBytes = File.ReadAllBytes(args[5]);
    MatchWriter(endExpected.ToByteArray(), endBytes, "class 31");
    var end = SessionEventArgs.BuildArgs(31, endBytes)
              as EndCombatResolutionSessionEventArgs;
    Require(end != null && end.Class == 31 && end.SessionId.m_UID64 == 42UL,
            "class 31 dispatch and fields");

    var removedExpected = new CombatRemovedSessionEventArgs {
      SessionId = new UID(42UL), CombatId = combatId, AttackerId = attacker,
      DefenderId = defender, BlockingCardIds = blockers
    };
    byte[] removedBytes = File.ReadAllBytes(args[6]);
    MatchWriter(removedExpected.ToByteArray(), removedBytes, "class 32");
    var removed = SessionEventArgs.BuildArgs(32, removedBytes)
                  as CombatRemovedSessionEventArgs;
    Require(removed != null && removed.Class == 32,
            "class 32 dispatch");
    Require(removed.SessionId.m_UID64 == 42UL,
            "class 32 session UID");
    CheckCombatId(removed.CombatId, "class 32");
    CheckCards(removed.BlockingCardIds,
              new ulong[] { (102UL << 8) | 1UL, (103UL << 8) | 1UL },
              "class 32 blockers");

    Console.WriteLine(
      "PASS extracted C# combat event writers/readers: classes 27-32");
  }
}
