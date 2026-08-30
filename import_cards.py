"""Import Hex TCG card templates from JSON files into SQLite database."""
import json, os, sqlite3

SETS_DIR = "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/Data/Sets"
DB_PATH = os.path.join(os.path.dirname(__file__), "hconnect.db")

db = sqlite3.connect(DB_PATH)

db.execute("""
CREATE TABLE IF NOT EXISTS card_templates (
    guid TEXT PRIMARY KEY,
    set_guid TEXT,
    name TEXT,
    rarity TEXT,
    cost INTEGER DEFAULT 0,
    attack INTEGER DEFAULT 0,
    defense INTEGER DEFAULT 0,
    card_type TEXT DEFAULT '',
    socket_count INTEGER DEFAULT 0
)
""")
db.execute("DELETE FROM card_templates")

count = 0
for f in os.listdir(SETS_DIR):
    if not f.endswith(".card"):
        continue
    try:
        with open(os.path.join(SETS_DIR, f)) as fp:
            c = json.load(fp)
            guid = f[:-5]
            set_guid = c.get("m_SetId", {}).get("m_Guid", "")
            name = c.get("m_Name", "?")
            rarity = c.get("m_CardRarity", "Common")
            cost = c.get("m_ResourceCost", 0) or 0
            attack = c.get("m_BaseAttackValue", 0) or 0
            defense = c.get("m_BaseDefenseValue", 0) or 0
            card_type = c.get("m_CardType", "") or ""
            socket_count = c.get("m_SocketCount", 0) or 0
            db.execute(
                "INSERT INTO card_templates (guid, set_guid, name, rarity, cost, attack, defense, card_type, socket_count) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (guid, set_guid, name, rarity, cost, attack, defense, card_type, socket_count))
            count += 1
    except Exception as e:
        print(f"  SKIP {f}: {e}")

db.commit()
db.close()
print(f"Imported {count} cards into {DB_PATH}")
