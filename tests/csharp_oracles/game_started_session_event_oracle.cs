using System;
using System.IO;
using Game.Shared;

public static class GameStartedSessionEventOracle {
  private static void Require(bool condition, string message) {
    if (!condition) throw new InvalidDataException(message);
  }

  public static void Main(string[] args) {
    if (args.Length != 1)
      throw new ArgumentException("Expected one Python class-1 event fixture path");

    var gameStarted = SessionEventArgs.BuildArgs(
      1, File.ReadAllBytes(args[0])) as GameStartedSessionEventArgs;
    Require(gameStarted != null, "BuildArgs class-1 dispatch");
    Require(gameStarted.Class == 1, "class ID");
    Require(gameStarted.SessionId.m_UID64 == 42UL, "session UID");
    Require(gameStarted.TurnOrder.Count == 2, "turn-order count");
    Require(gameStarted.TurnOrder[0].m_UID64 == ((1234UL << 8) | 244UL),
            "first player UID");
    Require(gameStarted.TurnOrder[1].m_UID64 == ((1000UL << 8) | 3UL),
            "second player UID");
    Require(gameStarted.ChampionNames.Count == 2 &&
            gameStarted.ChampionNames[0] == "Necrotic" &&
            gameStarted.ChampionNames[1] == "Iddi", "champion names");
    Require(gameStarted.ChampionTemplateIds.Count == 2 &&
            gameStarted.ChampionTemplateIds[0].ToString() ==
              "1d462ffb-0744-4996-804c-ba61b2c5c2f1" &&
            gameStarted.ChampionTemplateIds[1].ToString() ==
              "f8f86969-2e47-4901-8c9e-7fbf8d859e22",
            "champion-template ResourceIds");
    Require(gameStarted.SleeveTemplateIds.Count == 2 &&
            gameStarted.SleeveTemplateIds[0].ToString() ==
              "11111111-2222-3333-4444-555555555555" &&
            gameStarted.SleeveTemplateIds[1].ToString() ==
              "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "sleeve-template ResourceIds");
    Require(gameStarted.BoardTemplateIds.Count == 0, "board-template list");
    Require(gameStarted.CoinTemplateIds.Count == 0, "coin-template list");
    Require(gameStarted.Divisions.Count == 2 &&
            gameStarted.Divisions[0] == 1 && gameStarted.Divisions[1] == 1,
            "division values");
    Require(gameStarted.SeedW == 0xABCDEF01UL, "SeedW");
    Require(gameStarted.SeedZ == 0x12345678UL, "SeedZ");
    Require((int)gameStarted.SeriesFormat == 0, "series format");
    Require(gameStarted.SeriesMaxGames == 0, "series max games");
    Require(gameStarted.SeriesPrevWinners.Count == 0, "series previous winners");

    Console.WriteLine(
      "PASS extracted C# GameStartedSessionEventArgs reader: class 1 fields");
  }
}
