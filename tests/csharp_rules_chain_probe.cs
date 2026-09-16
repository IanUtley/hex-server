using System;
using Game.Shared.Mechanics.Abilities;
using Game.Shared.Resources;

namespace Game.Shared.Resources
{
	public struct ResourceId
	{
		public int Value;
		public ResourceId(int value) { Value = value; }
		public static bool operator ==(ResourceId left, ResourceId right) { return left.Value == right.Value; }
		public static bool operator !=(ResourceId left, ResourceId right) { return left.Value != right.Value; }
		public override bool Equals(object value) { return value is ResourceId && ((ResourceId)value).Value == Value; }
		public override int GetHashCode() { return Value; }
	}
	public static class BuiltInResources { public static ResourceId PlayCardAbilityTemplateId = new ResourceId(1); }
}

namespace Game.Shared.Mechanics.Abilities
{
	public class Card { }
	public class AbilityInstance
	{
		public long m_AbilityInstanceId;
		public long m_ParentAbilityInstanceId;
		public ResourceId AbilityTemplateId;
		public Card SourceCard;
	}
	public class AbilityManager
	{
		private System.Collections.Generic.Dictionary<long, AbilityInstance> items = new System.Collections.Generic.Dictionary<long, AbilityInstance>();
		public void Add(AbilityInstance ability) { items[ability.m_AbilityInstanceId] = ability; }
		public AbilityInstance GetAbilityInstance(long id) { AbilityInstance item; items.TryGetValue(id, out item); return item; }
		public void RemoveChainAbility(long id) { }
		public void RemoveAbility(long id) { items.Remove(id); }
		public void RemoveChainAbilitiesForPlayCard(AbilityInstance ability) { }
	}
}

namespace Game.Shared.Mechanics
{
	public class Session { public AbilityManager AbilityManager = new AbilityManager(); public int m_SessionId; }
	public static class LogBase { public static string GetClassLogger() { return "probe"; } }
	public static class Log { public static void Trace(string logger, object session, string text, params object[] values) { } }
	public static class ChainProbe
	{
		public static void Main()
		{
			Session session = new Session();
			AbilityInstance first = new AbilityInstance { m_AbilityInstanceId = 7 };
			AbilityInstance second = new AbilityInstance { m_AbilityInstanceId = 9 };
			session.AbilityManager.Add(first); session.AbilityManager.Add(second);
			Chain chain = new Chain(session);
			chain.PushAbility(first); chain.PushAbility(second);
			bool wrongPop = chain.PopAbility(7) == null;
			bool rightPop = chain.PopAbility(9) == second;
			Console.WriteLine(chain.Count + "," + chain.PeekAbility().m_AbilityInstanceId + "," + wrongPop + "," + rightPop);
		}
	}
}
