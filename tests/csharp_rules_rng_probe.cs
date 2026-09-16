using System;

public static class RulesRngProbe
{
	public static void Main(string[] args)
	{
		ulong z = UInt64.Parse(args[0]);
		ulong w = UInt64.Parse(args[1]);
		int count = Int32.Parse(args[2]);
		MultiplyWithCarryRng rng = new MultiplyWithCarryRng(z, w);
		for (int i = 0; i < count; i++)
		{
			Console.WriteLine(rng.Next());
		}
	}
}
