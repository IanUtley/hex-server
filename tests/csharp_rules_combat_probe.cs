using System;
using System.Collections.Generic;

namespace Game.Shared.Mechanics
{
	public enum ECombatFlags { None = 0, AttackersDeclared = 1, BlockersDeclared = 2, DamageAssigned = 4, AttackBlocked = 8, FirstStrikeResolved = 16, Resolved = 32 }
	public enum ECombatPhase { None = 0, Standard = 1, FirstStrike = 2 }
	public enum ECardTypes { Unknown = 0, Troop = 2 }
	public enum ECardCollections { Warzone = 1 }
	public struct SessionCardId
	{
		public int Value;
		public SessionCardId(int value) { Value = value; }
		public static bool operator ==(SessionCardId left, SessionCardId right) { return left.Value == right.Value; }
		public static bool operator !=(SessionCardId left, SessionCardId right) { return left.Value != right.Value; }
		public override bool Equals(object value) { return value is SessionCardId && ((SessionCardId)value).Value == Value; }
		public override int GetHashCode() { return Value; }
	}
	public struct CombatId { public int Value; }
	public class Player { }
	public class CardCollection { public ECardCollections CollectionType = ECardCollections.Warzone; }
	public class Card
	{
		public SessionCardId m_SessionCardId;
		public ECardTypes CurrentType = ECardTypes.Troop;
		public CardCollection GetCardCollection() { return new CardCollection(); }
		public bool CaresAboutCombatPhase(ECombatPhase phase) { return true; }
	}
	public static class LogBase { public static void Error(string logger, string message, params object[] args) {} }

	public static class CombatProbe
	{
		public static void Main()
		{
			Card defender = new Card { m_SessionCardId = new SessionCardId(99) };
			Card attacker = new Card { m_SessionCardId = new SessionCardId(10) };
			Card first = new Card { m_SessionCardId = new SessionCardId(20) };
			Card second = new Card { m_SessionCardId = new SessionCardId(30) };
			Combat combat = new Combat(new Player(), defender, new CombatId { Value = 1 });
			combat.DeclareAttacker(attacker);
			combat.DeclareBlockers(new Card[] { first, second });
			bool valid = combat.AssignDamageOrder(new SessionCardId[] { new SessionCardId(30), new SessionCardId(20) });
			Console.WriteLine(((int)combat.Flags).ToString() + "," + valid + "," +
				combat.Blockers[0].m_SessionCardId.Value + "," + combat.Blockers[1].m_SessionCardId.Value);
		}
	}
}
