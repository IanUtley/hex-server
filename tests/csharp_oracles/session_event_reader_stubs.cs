// Minimal value-type/base-class stubs for compiling the extracted client
// SessionEventArgs readers without Unity assemblies. The readers and
// Serializer.cs supplied by each oracle are the original client files.
using System;
using System.Collections.Generic;
using System.IO;

namespace Game.Shared {
  public struct UID {
    public static readonly UID Invalid = new UID(0UL);
    public ulong m_UID64;
    public UID(ulong value) { m_UID64 = value; }
    public UID(BinaryReader reader) { m_UID64 = reader.ReadUInt64(); }
    public void AddToStream(BinaryWriter writer) { writer.Write(m_UID64); }
    public bool IsValid() { return m_UID64 != 0UL; }
    public static bool operator ==(UID left, UID right) {
      return left.m_UID64 == right.m_UID64;
    }
    public static bool operator !=(UID left, UID right) {
      return !(left == right);
    }
    public override bool Equals(object other) {
      return other is UID && this == (UID)other;
    }
    public override int GetHashCode() { return m_UID64.GetHashCode(); }
  }

  public struct ResourceId {
    private Guid value;
    public ResourceId(Guid guid) { value = guid; }
    public ResourceId(byte[] bytes) { value = new Guid(bytes); }
    public byte[] ToByteArray() { return value.ToByteArray(); }
    public override string ToString() { return value.ToString(); }
  }

  public struct SessionCardId {
    private UID value;
    public SessionCardId(UID uid) { value = uid; }
    public UID UID { get { return value; } }
    public static bool operator ==(SessionCardId left, SessionCardId right) {
      return left.value.m_UID64 == right.value.m_UID64;
    }
    public static bool operator !=(SessionCardId left, SessionCardId right) {
      return !(left == right);
    }
    public override bool Equals(object other) {
      return other is SessionCardId && this == (SessionCardId)other;
    }
    public override int GetHashCode() { return value.m_UID64.GetHashCode(); }
  }

  public abstract class SessionEventArgs : EventArgs {
    public int Class;
    public UID SessionId;
    protected Serializer ser = new Serializer();

    protected void BeginWrite() {
      ser.BeginWrite();
      ser.Add(Class);
      ser.Add(SessionId);
    }
    protected byte[] EndWrite() { return ser.EndWrite(); }
    protected void BeginRead(byte[] data) {
      ser.BeginRead(data);
      ser.Read(out Class);
      ser.Read(out SessionId);
    }
    protected void EndRead() { ser.EndRead(); }
    public abstract byte[] ToByteArray();

    public static SessionEventArgs BuildArgs(int classId, byte[] data) {
#if COMBAT_EVENT_ORACLE
      switch (classId) {
        case 27: return new AttackDeclaredSessionEventArgs(data);
        case 28: return new BlockersAssignedSessionEventArgs(data);
        case 29: return new CombatPhaseResolvedSessionEventArgs(data);
        case 30: return new BeginCombatResolutionSessionEventArgs(data);
        case 31: return new EndCombatResolutionSessionEventArgs(data);
        case 32: return new CombatRemovedSessionEventArgs(data);
        default: throw new ArgumentException("Unexpected combat event class " + classId);
      }
#elif CHAIN_EVENT_ORACLE
      switch (classId) {
        case 41: return new TopOfChainResolvedSessionEventArgs(data);
        case 42: return new RemovedTopOfChainSessionEventArgs(data);
        case 77: return new ChainEmptySessionEventArgs(data);
        default: throw new ArgumentException("Unexpected chain event class " + classId);
      }
#elif TRIGGERED_ABILITY_EVENT_ORACLE
      switch (classId) {
        case 39: return new TriggeredAbilityActivationDataRequiredSessionEventArgs(data);
        default: throw new ArgumentException("Unexpected triggered ability event class " + classId);
      }
#else
      switch (classId) {
        case 1: return new GameStartedSessionEventArgs(data);
        case 3: return new TurnPhaseUpdatedSessionEventArgs(data);
        case 22: return new AbilityPushedOnChainSessionEventArgs(data);
        case 23: return new AbilityActivationDataRequiredSessionEventArgs(data);
        case 51: return new CardsRevealedSessionEventArgs(data);
        case 54: return new CardCountersChangedSessionEventArgs(data);
        case 55: return new EncounterModDialogSessionEventArgs(data);
        case 65: return new PlayerUpdatedSessionEventArgs(data);
        case 66: return new CostInstanceSessionEventArgs(data);
        case 67: return new TargetInstanceSessionEventArgs(data);
        case 68: return new OptionInstanceSessionEventArgs(data);
        case 69: return new PlayerOptionSessionEventArgs(data);
        case 70: return new PlayerOptionListSessionEventArgs(data);
        case 48: return new GreenLightSessionEventArgs(data);
        default: throw new ArgumentException("Unexpected option event class " + classId);
      }
#endif
    }
  }

  public static class LogBase {
    public static string GetClassLogger() { return "oracle"; }
    public static void Exception(string logger, Exception exception) { }
  }

  public class BulkSessionEventSessionEventArgs : SessionEventArgs {
    public UID PlayerId;
    public List<SessionEventArgs> EventList = new List<SessionEventArgs>();
  public override byte[] ToByteArray() { throw new NotImplementedException(); }
  }
}

namespace Game.Shared.Mechanics {
  public enum EDeckPersonality { None }
  public enum ETurnPhases { Unknown }
  [Flags]
  public enum ECombatPhase { None = 0, Standard = 1, FirstStrike = 2 }
  public enum EOperations { None }
  public enum ECardShards {
    Unknown = 0, Colorless = 1, Blood = 4, Ruby = 8, Sapphire = 16,
    Wild = 32, Diamond = 64, Any = 125, AnyColor = 124
  }
  [Flags]
  public enum ECardCollections {
    None = 0, Deck = 1, Hand = 2, Champions = 4, Warzone = 8,
    Discard = 16, Void = 32, PlayedResources = 64, CastSpells = 128,
    Underground = 256, Choosing = 512, Mod = 1024, Simulacrum = 2048,
    UI_Warzone = 4096, UI_Constant = 8192
  }
  public enum ECardLocations { Unknown = 0, Top = 1, Bottom = 2 }
  public enum EGemTypesNew : ulong { None }
  public enum EPriorityContext { None }
  public enum EAICommand { None }
  public enum ECardTypes { None }
  public enum ECardStates { None }
  public enum EAICardStates { None }
  public enum ECardAttributes { None }
  public enum EAbilityCostType {
    Default = 0,
    ExhaustAbilityCostType = 1,
    SacrificeAbilityCostType = 2,
    ShuffleIntoDeckAbilityCostType = 4,
    DiscardAbilityCostType = 8,
    VoidAbilityCostType = 16,
    PutIntoDeckAbilityCostType = 32,
    RevealAbilityCostType = 64,
    PutIntoHandAbilityCostType = 128,
    XCostAbilityCostType = 256,
    MobilizeAbilityCostType = 512
  }
  public enum ECardUsage {
    None = 0, Play = 1, Activate = 2, Attack = 4, Defend = 8,
    ForcedAttack = 16, PlayForFree = 32
  }
  public enum EAnimationTrigger { None }

  public class CombatId {
    public Game.Shared.UID Attacker;
    public long SerialNumber;
    public CombatId(Game.Shared.UID attacker, long serial) {
      Attacker = attacker;
      SerialNumber = serial;
    }
  }

  public class IntAttrs {
    public string name;
    public static readonly Dictionary<string, IntAttrs> AttributeDictionary =
      new Dictionary<string, IntAttrs>();
  }
  public class StringAttrs {
    public string name;
    public static readonly Dictionary<string, StringAttrs> AttributeDictionary =
      new Dictionary<string, StringAttrs>();
  }
  public class TACAttrs {
    public string name;
    public static TACAttrs FindAttr(string name) { return new TACAttrs { name = name }; }
  }
  public class ListAttrs {
    public string name;
    public static ListAttrs FindAttr(string name) { return new ListAttrs { name = name }; }
  }
  public class TAC {
    public void AddToStream(Game.Shared.Serializer serializer) { }
    public void ReadFromStream(Game.Shared.Serializer serializer) { }
  }
  public class TACList : List<TAC> { }
}

namespace Game.Shared.Mechanics.Abilities {
  public class XCostData {
    public enum ESetValues { None }
    public void AddToStream(Game.Shared.Serializer serializer) { }
    public void ReadFromStream(Game.Shared.Serializer serializer) { }
  }
}

namespace Game.Shared.Mechanics.Abilities.TargetTemplates {
  public class AbilityTargetInstance {
    public List<Game.Shared.UID> m_PlayerIds = new List<Game.Shared.UID>();
    public List<Game.Shared.SessionCardId> m_SessionCardIds =
      new List<Game.Shared.SessionCardId>();
    public Game.Shared.ResourceId m_TargetTemplateId;
  }
}

namespace Game.Shared {
  public class SessionStateEncounterData {
    public enum SeriesType { None }
  }
}
