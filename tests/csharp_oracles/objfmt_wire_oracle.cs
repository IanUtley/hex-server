// Generates one client-shaped 3029 DataWrapper using the client's ObjFmt
// encoder. Serializable member names, types, and DataMember annotations below
// match the extracted client declarations at:
//   Game/Shared/UID.cs
//   Game/Shared/Mechanics/ETurnPhases.cs
//   Game/Shared/Mechanics/Transactions/Transaction.cs
//   Game/Shared/Mechanics/Transactions/PassPriorityTransaction.cs
//   Game/Shared/Mechanics/Transactions/ActivateAbilityTransaction.cs
//   Game/Shared/Mechanics/Transactions/SetTurnPhasesTransaction.cs
//   Game/Shared/Mechanics/Transactions/ChoosePlayTransaction.cs
//   Game/Shared/Mechanics/Transactions/ChooseDrawTransaction.cs
//   Game/Shared/Mechanics/Transactions/MulliganTransaction.cs
//   Game/Shared/Mechanics/Transactions/AcceptStartingHandTransaction.cs
//   Game/Shared/Mechanics/Transactions/PlayResourceTransaction.cs
//   Game/Shared/Mechanics/Transactions/PlayTroopTransaction.cs
//   Game/Shared/Mechanics/Transactions/DiscardTransaction.cs
//   Game/Shared/Mechanics/Transactions/PlayArtifactTransaction.cs
//   Game/Shared/Mechanics/Transactions/PlaySpellTransaction.cs
//   Game/Shared/Mechanics/Transactions/PlayChampionTransaction.cs
//   Game/Shared/Mechanics/Transactions/SetAbilityActivationDataTransaction.cs
//   Game/Shared/Mechanics/Transactions/ActivateTriggeredAbiliesTransaction.cs
//   Game/Shared/Mechanics/Transactions/CommitTroopsToAttackTransaction.cs
//   Game/Shared/Mechanics/Transactions/CommitTroopsToDefenseTransaction.cs
//   Game/Shared/Mechanics/Transactions/AssignDamageOrderTransaction.cs
//   Game/Shared/Mechanics/Transactions/SetAutoPassTransaction.cs
//   Game/Shared/Mechanics/Transactions/CancelAutoPassTransaction.cs
//   Game/Shared/Mechanics/Transactions/RequestPrioritySyncTransaction.cs
//   Game/Shared/Mechanics/Transactions/QuitGameTransaction.cs
//   Game/Shared/Mechanics/Transactions/EncounterModDialogTransaction.cs
//   Game/Shared/Mechanics/Transactions/ReadyCardTransaction.cs
//   Game/Shared/Mechanics/Transactions/RequestPlayerOptionsTransaction.cs
//   Game/Shared/Mechanics/Transactions/TipWindowClosed.cs
//   Game/Shared/Mechanics/Transactions/DebugCheatTransaction.cs
//   Game/Shared/Mechanics/Transactions/SendGameStateChecksumTransaction.cs
//   Game/Shared/Mechanics/Abilities/AbilityActivationData.cs
//   Game/Shared/Mechanics/Abilities/TargetTemplates/AbilityTargetInstance.cs
//   Game/Shared/SessionCardId.cs
//   Game/Shared/ResourceId.cs
//   Game/Shared/Network/GameSession/PlayerTransactionRequestArgs.cs
//   Game/Shared/Network/GameSession/GameStartedEventArgs.cs
//   Game/Shared/Network/DataWrapper.cs
// This keeps the oracle buildable with Mono without the Unity assemblies.
// Describe() below follows EncData.DescribeTypes for generic type names;
// the full EncData class references Unity-only runtime dependencies.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.Serialization;
using Game.Shared;
using Game.Shared.Mechanics;
using Game.Shared.Mechanics.Abilities;
using Game.Shared.Mechanics.Abilities.TargetTemplates;
using Game.Shared.Mechanics.Transactions;
using Game.Shared.Network;
using Game.Shared.Network.GameSession;

namespace Game.Shared {
  [DataContract]
  public struct ResourceId {
    [DataMember] public Guid m_Guid;
    public ResourceId(Guid value) { m_Guid = value; }
  }

  [DataContract]
  public struct SessionCardId {
    [DataMember] public UID value;
    public SessionCardId(UID uid) { value = uid; }
  }

  [DataContract]
  public struct UID {
    [DataMember] public ulong m_UID64;
    public UID(ulong value) { m_UID64 = value; }
  }

  [DataContract]
  public sealed class SessionChecksumData {
    [DataMember] public int SessionChecksum;
    [DataMember] public Dictionary<UID, int> PlayerChecksums =
      new Dictionary<UID, int>();
    [DataMember] public List<byte[]> CS;
    [DataMember] public bool WantData;
  }

  [DataContract]
  public class SessionEventArgs {
    [DataMember] public int Class;
    [DataMember] public UID SessionId;
  }

  [DataContract]
  public sealed class NetworkPacketSessionEventArgs : SessionEventArgs {
    [DataMember] public UID PlayerId;
    [DataMember] public List<int> EventIds;
    [DataMember] public List<byte[]> EventData;
  }
}

namespace Game.Shared.Mechanics {
  // Values follow the extracted client's ETurnPhases declaration order.
  public enum ETurnPhases {
    Unknown, NotPlaying, PreGame, PickGoesFirst, Mulligan, StartGame,
    StartTurn, Ready, Prep, Draw, FirstMainPhase,
    DeclareCombatPriorityWindow, DeclareAttack, DeclareAttackPriorityWindow,
    DeclareDefense, DeclareDefensePriorityWindow, AssignFirstStrikeDamage,
    FirstStrikePriorityWindow, AssignDamage, SecondMainPhase, EndPhase,
    Discard, EndTurn, Checksum, EndGame
  }

  public enum EPassingState { None, Attack, EndOfTurn, EndPhase }
  public enum ECardShards { Unknown, Colorless, Blood, Ruby, Sapphire, Wild, Diamond }
  public enum EAICommand { Reset }

  [DataContract]
  public struct CombatId {
    [DataMember] public UID Attacker;
    [DataMember] public long SerialNumber;
    public CombatId(UID attacker, long serial) {
      Attacker = attacker;
      SerialNumber = serial;
    }
  }
}

namespace Game.Shared.Mechanics.Transactions {
  [DataContract]
  public abstract class Transaction {
    [DataMember] public UID m_PlayerId;
    [DataMember] public int m_TransactionId;
  }

  [DataContract]
  public sealed class ChoosePlayTransaction : Transaction {
    public ChoosePlayTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class ChooseDrawTransaction : Transaction {
    public ChooseDrawTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class MulliganTransaction : Transaction {
    public MulliganTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class AcceptStartingHandTransaction : Transaction {
    public AcceptStartingHandTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class PlayResourceTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;

    public PlayResourceTransaction(UID player, int transactionId,
                                   SessionCardId cardId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = cardId;
    }
  }

  [DataContract]
  public sealed class PlayTroopTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;
    [DataMember] private List<AbilityActivationData> m_AbilityDataList;
    [DataMember] private bool m_PlayingForFree;

    public PlayTroopTransaction(UID player, int transactionId,
                                SessionCardId cardId, bool playingForFree) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = cardId;
      m_AbilityDataList = new List<AbilityActivationData>();
      m_PlayingForFree = playingForFree;
    }
  }

  [DataContract]
  public sealed class PassPriorityTransaction : Transaction {
    [DataMember] private ETurnPhases m_TurnPhase;

    public PassPriorityTransaction(UID player, int transactionId,
                                  ETurnPhases turnPhase) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_TurnPhase = turnPhase;
    }
  }

  [DataContract]
  public sealed class ActivateAbilityTransaction : Transaction {
    [DataMember] private AbilityActivationData m_AbilityActivationData;

    public ActivateAbilityTransaction(UID player, int transactionId,
                                      AbilityActivationData activation) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_AbilityActivationData = activation;
    }
  }

  [DataContract]
  public sealed class SetTurnPhasesTransaction : Transaction {
    [DataMember] private List<ETurnPhases> m_SelfTurnPhases;
    [DataMember] private List<ETurnPhases> m_OpponentTurnPhases;

    public SetTurnPhasesTransaction(UID player, int transactionId,
                                   List<ETurnPhases> selfPhases,
                                   List<ETurnPhases> opponentPhases) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SelfTurnPhases = selfPhases;
      m_OpponentTurnPhases = opponentPhases;
    }
  }

  [DataContract]
  public sealed class DiscardTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;
    public DiscardTransaction(UID player, int transactionId, SessionCardId card) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = card;
    }
  }

  [DataContract]
  public sealed class PlayArtifactTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;
    [DataMember] private List<AbilityActivationData> m_AbilityDataList;
    [DataMember] private bool m_PlayingForFree;
    public PlayArtifactTransaction(UID player, int transactionId,
        SessionCardId card, List<AbilityActivationData> abilities, bool free) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = card;
      m_AbilityDataList = abilities;
      m_PlayingForFree = free;
    }
  }

  [DataContract]
  public sealed class PlaySpellTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;
    [DataMember] private List<AbilityActivationData> m_AbilityDataList;
    [DataMember] private bool m_PlayingForFree;
    public PlaySpellTransaction(UID player, int transactionId,
        SessionCardId card, List<AbilityActivationData> abilities, bool free) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = card;
      m_AbilityDataList = abilities;
      m_PlayingForFree = free;
    }
  }

  [DataContract]
  public sealed class PlayChampionTransaction : Transaction {
    [DataMember] private SessionCardId m_SessionCardId;
    public PlayChampionTransaction(UID player, int transactionId,
                                   SessionCardId card) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardId = card;
    }
  }

  [DataContract]
  public sealed class SetAbilityActivationDataTransaction : Transaction {
    [DataMember] private long m_AbilityInstanceId;
    [DataMember] private AbilityActivationData m_AbilityActivationData;
    public SetAbilityActivationDataTransaction(UID player, int transactionId,
        long abilityInstanceId, AbilityActivationData activation) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_AbilityInstanceId = abilityInstanceId;
      m_AbilityActivationData = activation;
    }
  }

  [DataContract]
  public sealed class ActivateTriggeredAbiliesTransaction : Transaction {
    [DataMember] private List<AbilityActivationData> m_AbilityActivationData;
    public ActivateTriggeredAbiliesTransaction(UID player, int transactionId,
        List<AbilityActivationData> activations) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_AbilityActivationData = activations;
    }
  }

  [DataContract]
  public sealed class CommitTroopsToAttackTransaction : Transaction {
    [DataMember] private List<AttackDeclaration> m_Attacks;
    public CommitTroopsToAttackTransaction(UID player, int transactionId,
        List<AttackDeclaration> attacks) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_Attacks = attacks;
    }

    [DataContract]
    public sealed class AttackDeclaration {
      [DataMember] public SessionCardId DefendingCardId;
      [DataMember] public List<SessionCardId> AttackingCardIds;
      public AttackDeclaration(SessionCardId defender, List<SessionCardId> attackers) {
        DefendingCardId = defender;
        AttackingCardIds = attackers;
      }
    }
  }

  [DataContract]
  public sealed class CommitTroopsToDefenseTransaction : Transaction {
    [DataMember] public List<DefenseDeclaration> m_DefenseDeclarations;
    public CommitTroopsToDefenseTransaction(UID player, int transactionId,
        List<DefenseDeclaration> declarations) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_DefenseDeclarations = declarations;
    }

    [DataContract]
    public sealed class DefenseDeclaration {
      [DataMember] public SessionCardId AttackerId;
      [DataMember] public List<SessionCardId> DefendingCardIds;
      public DefenseDeclaration(SessionCardId attacker, List<SessionCardId> defenders) {
        AttackerId = attacker;
        DefendingCardIds = defenders;
      }
    }
  }

  [DataContract]
  public sealed class AssignDamageOrderTransaction : Transaction {
    [DataMember] private List<DamageAssignment> m_AssignedDamageOrder;
    public AssignDamageOrderTransaction(UID player, int transactionId,
        List<DamageAssignment> assignments) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_AssignedDamageOrder = assignments;
    }

    [DataContract]
    public sealed class DamageAssignment {
      [DataMember] public CombatId CombatId;
      [DataMember] public List<SessionCardId> CardIds;
      public DamageAssignment(CombatId combat, List<SessionCardId> cards) {
        CombatId = combat;
        CardIds = cards;
      }
    }
  }

  [DataContract]
  public sealed class SetAutoPassTransaction : Transaction {
    [DataMember] private EPassingState m_PassingState;
    [DataMember] private bool m_AsActive;
    public SetAutoPassTransaction(UID player, int transactionId,
        EPassingState passingState, bool asActive) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_PassingState = passingState;
      m_AsActive = asActive;
    }
  }

  [DataContract]
  public sealed class CancelAutoPassTransaction : Transaction {
    public CancelAutoPassTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class RequestPrioritySyncTransaction : Transaction {
    public RequestPrioritySyncTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class QuitGameTransaction : Transaction {
    [DataMember] private bool m_WasBugged;
    [DataMember] private bool m_QuitEntireSeries;
    public QuitGameTransaction(UID player, int transactionId, bool wasBugged,
                               bool quitEntireSeries) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_WasBugged = wasBugged;
      m_QuitEntireSeries = quitEntireSeries;
    }
  }

  [DataContract]
  public sealed class EncounterModDialogTransaction : Transaction {
    [DataMember] public ResourceId ConversationId;
    [DataMember] public UID PlayerId;
    public EncounterModDialogTransaction(UID player, int transactionId,
                                         ResourceId conversationId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      PlayerId = player;
      ConversationId = conversationId;
    }
  }

  [DataContract]
  public sealed class ReadyCardTransaction : Transaction {
    [DataMember] public List<SessionCardId> m_SessionCardIds;
    public ReadyCardTransaction(UID player, int transactionId,
        List<SessionCardId> cards) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_SessionCardIds = cards;
    }
  }

  [DataContract]
  public sealed class RequestPlayerOptionsTransaction : Transaction {
    public RequestPlayerOptionsTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class TipWindowClosed : Transaction {
    public TipWindowClosed(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
    }
  }

  [DataContract]
  public sealed class SendGameStateChecksumTransaction : Transaction {
    [DataMember] private SessionChecksumData m_ChecksumData;
    public SendGameStateChecksumTransaction(UID player, int transactionId,
                                             SessionChecksumData checksum) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      m_ChecksumData = checksum;
    }
  }

  [DataContract]
  public sealed class DebugCheatTransaction : Transaction {
    public enum EDebugAction { AddCard }
    [DataMember] public EDebugAction DebugAction;
    [DataMember] public ResourceId CardTemplateId;
    [DataMember] public ECardShards ThresholdColor;
    [DataMember] public int Count;
    [DataMember] public SessionCardId SessionCardId;
    [DataMember] public ResourceId IventoryItemId;
    [DataMember] public UID DestPlayer;
    [DataMember] public int EquipmentSlot;
    [DataMember] public EAICommand AICommand;
    [DataMember] public int defaultGems;
    [DataMember] public string Key;
    [DataMember] public object Value;
    public DebugCheatTransaction(UID player, int transactionId) {
      m_PlayerId = player;
      m_TransactionId = transactionId;
      DebugAction = EDebugAction.AddCard;
      ThresholdColor = ECardShards.Unknown;
      DestPlayer = new UID(0);
      AICommand = EAICommand.Reset;
      Key = "fixture";
      Value = (object)"fixture";
    }
  }
}

namespace Game.Shared.Mechanics.Abilities {
  [DataContract]
  public sealed class AbilityActivationData {
    [DataMember] public SessionCardId SourceCardId;
    [DataMember] public ResourceId AbilityTemplateId;
    [DataMember] public long AbilityInstanceId;
    [DataMember] public Dictionary<int, AbilityTargetInstance> TargetMap;
    [DataMember] public Dictionary<int, int> OptionMap;
    [DataMember] public bool Disable;
    [DataMember] public bool OptedIn = true;
    [DataMember] public bool OptedInSet;
    [DataMember] public XCostData xCostData;
    [DataMember] public Dictionary<string, int> Variables;
    [DataMember] public int Index = -1;
  }

  [DataContract]
  public sealed class XCostData { }
}

namespace Game.Shared.Mechanics.Abilities.TargetTemplates {
  [DataContract]
  public sealed class AbilityTargetInstance {
    [DataMember] public ResourceId m_TargetTemplateId;
    [DataMember] public List<SessionCardId> m_SessionCardIds =
      new List<SessionCardId>();
    [DataMember] public List<UID> m_PlayerIds = new List<UID>();
    [DataMember] public bool IsLocked;
  }
}

namespace Game.Shared.Network.GameSession {
  [DataContract]
  public sealed class SessionSyncEventEventArgs {
    [DataMember] public UID RoutingPlayerId { get; set; }
    [DataMember] public SessionEventArgs SessionArgs { get; set; }
  }

  [DataContract]
  public sealed class PlayerTransactionRequestArgs {
    [DataMember] public UID PlayerId { get; set; }
    [DataMember] public Transaction Transaction { get; set; }
  }

  [DataContract]
  public sealed class GameStartedEventArgs {
    [DataMember] public UID RoutingPlayerId { get; set; }
    [DataMember] public bool Success { get; set; }
    [DataMember] public List<UID> TurnOrderPlayerIds { get; set; }
    [DataMember] public ulong SeedW { get; set; }
    [DataMember] public ulong SeedZ { get; set; }
  }
}

namespace Game.Shared.Network {
  [DataContract]
  public sealed class DataWrapper {
    [DataMember] public long RequestId { get; set; }
    [DataMember] public int DataType { get; set; }
    [DataMember] public byte[] Bytes { get; set; }
    [DataMember] public List<byte[]> BytesList { get; set; }
    [DataMember] public Guid RequestHandlerSessionId { get; set; }
    [DataMember] public byte Comp { get; set; }
  }
}

public static class ObjFmt3029Oracle {
  private static string Describe(Type type) {
    if (!type.IsGenericType)
      return type.FullName;
    Type[] arguments = type.GetGenericArguments();
    string result = type.Namespace + "." + type.Name + "#" +
      Describe(arguments[0]);
    for (int i = 1; i < arguments.Length; i++)
      result += "!" + Describe(arguments[i]);
    return result;
  }

  private static string Pick(Type expected, object value) {
    return Describe(value == null ? expected : value.GetType());
  }

  private static bool CheckMember(MemberInfo member) {
    return member.IsDefined(typeof(DataMemberAttribute), false);
  }

  private static byte[] Encode(object value, Type declaredType) {
    var encoder = ObjFmt.MakeEncoder(Pick, CheckMember);
    using (var stream = new MemoryStream()) {
      encoder.Encode(stream, null, value, null, declaredType);
      return stream.ToArray();
    }
  }

  private static void WriteTransactionWrapper(string path, long requestId,
                                               Guid handlerSessionId,
                                               UID player, Transaction tx) {
    var request = new PlayerTransactionRequestArgs {
      PlayerId = player, Transaction = tx
    };
    var wrapper = new DataWrapper {
      RequestId = requestId,
      DataType = 3029,
      Bytes = Encode(request, typeof(PlayerTransactionRequestArgs)),
      RequestHandlerSessionId = handlerSessionId,
      Comp = 0
    };
    File.WriteAllBytes(path, Encode(wrapper, typeof(DataWrapper)));
  }

  public static void Main(string[] args) {
    if (args.Length != 32)
      throw new ArgumentException(
        "Expected output paths for 3029, 3053, activation, non-empty 3055, " +
        "empty 3055, SetTurnPhases, four opening choice/mulligan 3029 " +
        "fixtures, resource and troop play 3029 fixtures, event data, and " +
        "19 additional live transaction families");

    ulong playerId = (1234UL << 8) | 244UL;
    var transaction = new PassPriorityTransaction(
      new UID(playerId), 23, ETurnPhases.FirstMainPhase);
    var request = new PlayerTransactionRequestArgs {
      PlayerId = new UID(playerId), Transaction = transaction
    };
    byte[] inner = Encode(request, typeof(PlayerTransactionRequestArgs));
    var wrapper = new DataWrapper {
      RequestId = 4,
      DataType = 3029,
      Bytes = inner,
      RequestHandlerSessionId = Guid.Parse(
        "2e590000-0000-0000-0000-000000000001"),
      Comp = 0
    };
    File.WriteAllBytes(args[0], Encode(wrapper, typeof(DataWrapper)));

    var gameStarted = new GameStartedEventArgs {
      RoutingPlayerId = new UID(playerId),
      Success = true,
      TurnOrderPlayerIds = new List<UID> {
        new UID(playerId), new UID((1000UL << 8) | 3UL)
      },
      SeedW = 0xABCDEF0102030405UL,
      SeedZ = 0x1234567890ABCDEFUL
    };
    File.WriteAllBytes(args[1],
      Encode(gameStarted, typeof(GameStartedEventArgs)));

    ulong sourceCardId = (101UL << 8) | 1UL;
    ulong targetCardId = (77UL << 8) | 1UL;
    var activation = new AbilityActivationData {
      SourceCardId = new SessionCardId(new UID(sourceCardId)),
      AbilityTemplateId = new ResourceId(Guid.Parse(
        "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd")),
      AbilityInstanceId = 555,
      TargetMap = new Dictionary<int, AbilityTargetInstance> {
        { 2, new AbilityTargetInstance {
          m_TargetTemplateId = new ResourceId(Guid.Parse(
            "5badd4e4-5af4-409d-9d1f-1b839b39c35e")),
          m_SessionCardIds = new List<SessionCardId> {
            new SessionCardId(new UID(targetCardId))
          }
        }}
      }
    };
    var activationTransaction = new ActivateAbilityTransaction(
      new UID(playerId), 42, activation);
    var activationRequest = new PlayerTransactionRequestArgs {
      PlayerId = new UID(playerId), Transaction = activationTransaction
    };
    byte[] activationInner = Encode(
      activationRequest, typeof(PlayerTransactionRequestArgs));
    var activationWrapper = new DataWrapper {
      RequestId = 5,
      DataType = 3029,
      Bytes = activationInner,
      RequestHandlerSessionId = Guid.Parse(
        "2e590000-0000-0000-0000-000000000002"),
      Comp = 0
    };
    File.WriteAllBytes(args[2],
      Encode(activationWrapper, typeof(DataWrapper)));

    var setTurnPhases = new SetTurnPhasesTransaction(
      new UID(playerId), 44,
      new List<ETurnPhases> {
        ETurnPhases.PickGoesFirst, ETurnPhases.Mulligan,
        ETurnPhases.FirstMainPhase
      },
      new List<ETurnPhases> {
        ETurnPhases.Ready, ETurnPhases.DeclareDefense
      });
    var setTurnPhasesRequest = new PlayerTransactionRequestArgs {
      PlayerId = new UID(playerId), Transaction = setTurnPhases
    };
    byte[] setTurnPhasesInner = Encode(
      setTurnPhasesRequest, typeof(PlayerTransactionRequestArgs));
    var setTurnPhasesWrapper = new DataWrapper {
      RequestId = 6,
      DataType = 3029,
      Bytes = setTurnPhasesInner,
      RequestHandlerSessionId = Guid.Parse(
        "2e590000-0000-0000-0000-000000000003"),
      Comp = 0
    };
    File.WriteAllBytes(args[5],
      Encode(setTurnPhasesWrapper, typeof(DataWrapper)));

    WriteTransactionWrapper(args[6], 7,
      Guid.Parse("2e590000-0000-0000-0000-000000000004"),
      new UID(playerId), new ChoosePlayTransaction(new UID(playerId), 45));
    WriteTransactionWrapper(args[7], 8,
      Guid.Parse("2e590000-0000-0000-0000-000000000005"),
      new UID(playerId), new ChooseDrawTransaction(new UID(playerId), 46));
    WriteTransactionWrapper(args[8], 9,
      Guid.Parse("2e590000-0000-0000-0000-000000000006"),
      new UID(playerId), new MulliganTransaction(new UID(playerId), 47));
    WriteTransactionWrapper(args[9], 10,
      Guid.Parse("2e590000-0000-0000-0000-000000000007"),
      new UID(playerId), new AcceptStartingHandTransaction(new UID(playerId), 48));

    WriteTransactionWrapper(args[10], 11,
      Guid.Parse("2e590000-0000-0000-0000-000000000008"),
      new UID(playerId), new PlayResourceTransaction(
        new UID(playerId), 49,
        new SessionCardId(new UID((201UL << 8) | 1UL))));
    WriteTransactionWrapper(args[11], 12,
      Guid.Parse("2e590000-0000-0000-0000-000000000009"),
      new UID(playerId), new PlayTroopTransaction(
        new UID(playerId), 50,
        new SessionCardId(new UID((202UL << 8) | 1UL)), false));

    var cardA = new SessionCardId(new UID((301UL << 8) | 1UL));
    var cardB = new SessionCardId(new UID((302UL << 8) | 1UL));
    var abilityList = new List<AbilityActivationData> { activation };
    Func<int, Guid> handlerGuid = id => Guid.Parse(
      String.Format("2e590000-0000-0000-0000-{0:x12}", id));
    WriteTransactionWrapper(args[13], 13, handlerGuid(13), new UID(playerId),
      new DiscardTransaction(new UID(playerId), 51, cardA));
    WriteTransactionWrapper(args[14], 14, handlerGuid(14), new UID(playerId),
      new PlayArtifactTransaction(new UID(playerId), 52, cardA,
                                  abilityList, false));
    WriteTransactionWrapper(args[15], 15, handlerGuid(15), new UID(playerId),
      new PlaySpellTransaction(new UID(playerId), 53, cardA,
                               abilityList, false));
    WriteTransactionWrapper(args[16], 16, handlerGuid(16), new UID(playerId),
      new PlayChampionTransaction(new UID(playerId), 54, cardA));
    WriteTransactionWrapper(args[17], 17, handlerGuid(17), new UID(playerId),
      new SetAbilityActivationDataTransaction(new UID(playerId), 55, 555,
                                              activation));
    WriteTransactionWrapper(args[18], 18, handlerGuid(18), new UID(playerId),
      new ActivateTriggeredAbiliesTransaction(new UID(playerId), 56,
                                              abilityList));
    WriteTransactionWrapper(args[19], 19, handlerGuid(19), new UID(playerId),
      new CommitTroopsToAttackTransaction(new UID(playerId), 57,
        new List<CommitTroopsToAttackTransaction.AttackDeclaration> {
          new CommitTroopsToAttackTransaction.AttackDeclaration(
            cardA, new List<SessionCardId> { cardB })
        }));
    WriteTransactionWrapper(args[20], 20, handlerGuid(20), new UID(playerId),
      new CommitTroopsToDefenseTransaction(new UID(playerId), 58,
        new List<CommitTroopsToDefenseTransaction.DefenseDeclaration> {
          new CommitTroopsToDefenseTransaction.DefenseDeclaration(
            cardA, new List<SessionCardId> { cardB })
        }));
    WriteTransactionWrapper(args[21], 21, handlerGuid(21), new UID(playerId),
      new AssignDamageOrderTransaction(new UID(playerId), 59,
        new List<AssignDamageOrderTransaction.DamageAssignment> {
          new AssignDamageOrderTransaction.DamageAssignment(
            new CombatId(new UID(playerId), 7),
            new List<SessionCardId> { cardA, cardB })
        }));
    WriteTransactionWrapper(args[22], 22, handlerGuid(22), new UID(playerId),
      new SetAutoPassTransaction(new UID(playerId), 60,
                                 EPassingState.EndPhase, true));
    WriteTransactionWrapper(args[23], 23, handlerGuid(23), new UID(playerId),
      new CancelAutoPassTransaction(new UID(playerId), 61));
    WriteTransactionWrapper(args[24], 24, handlerGuid(24), new UID(playerId),
      new RequestPrioritySyncTransaction(new UID(playerId), 62));
    WriteTransactionWrapper(args[25], 25, handlerGuid(25), new UID(playerId),
      new QuitGameTransaction(new UID(playerId), 63, false, true));
    WriteTransactionWrapper(args[26], 26, handlerGuid(26), new UID(playerId),
      new EncounterModDialogTransaction(new UID(playerId), 64,
        new ResourceId(Guid.Parse("4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd"))));
    WriteTransactionWrapper(args[27], 27, handlerGuid(27), new UID(playerId),
      new ReadyCardTransaction(new UID(playerId), 65,
                               new List<SessionCardId> { cardA }));
    WriteTransactionWrapper(args[28], 28, handlerGuid(28), new UID(playerId),
      new RequestPlayerOptionsTransaction(new UID(playerId), 66));
    WriteTransactionWrapper(args[29], 29, handlerGuid(29), new UID(playerId),
      new TipWindowClosed(new UID(playerId), 67));
    var checksum = new SessionChecksumData {
      SessionChecksum = 9876,
      CS = new List<byte[]>(),
      WantData = true
    };
    checksum.PlayerChecksums.Add(new UID(playerId), 321);
    WriteTransactionWrapper(args[30], 30, handlerGuid(30), new UID(playerId),
      new SendGameStateChecksumTransaction(new UID(playerId), 68, checksum));
    WriteTransactionWrapper(args[31], 31, handlerGuid(31), new UID(playerId),
      new DebugCheatTransaction(new UID(playerId), 69));

    var emptyPacket = new NetworkPacketSessionEventArgs {
      Class = 255,
      SessionId = new UID(42),
      PlayerId = new UID(playerId),
      EventIds = new List<int>(),
      EventData = new List<byte[]>()
    };
    var emptySync = new SessionSyncEventEventArgs {
      RoutingPlayerId = new UID(playerId),
      SessionArgs = emptyPacket
    };
    File.WriteAllBytes(args[4],
      Encode(emptySync, typeof(SessionSyncEventEventArgs)));

    var optionEventData = File.ReadAllBytes(args[12]);
    var packet = new NetworkPacketSessionEventArgs {
      Class = 255,
      SessionId = new UID(42),
      PlayerId = new UID(playerId),
      EventIds = new List<int> { 70 },
      EventData = new List<byte[]> { optionEventData }
    };
    var sync = new SessionSyncEventEventArgs {
      RoutingPlayerId = new UID(playerId),
      SessionArgs = packet
    };
    File.WriteAllBytes(args[3],
      Encode(sync, typeof(SessionSyncEventEventArgs)));
  }
}
