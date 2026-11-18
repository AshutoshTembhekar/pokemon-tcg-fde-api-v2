from fastapi.testclient import TestClient

from src.api import API_KEY_EXPECTED, app
from src.validator import validate_and_quarantine

client = TestClient(app)


def test_health_endpoint() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_unauthorized_request_blocked() -> None:
    resp = client.get("/v1/quarantine")
    assert resp.status_code == 401


def test_validator_quarantines_corrupt_pokemon_cards() -> None:
    sample_payload = {
        "set_id": "sv03",
        "set_name": "Obsidian Flames",
        "cards": [
            {
                "id": "sv03-125",
                "localId": "125",
                "name": "Charizard ex",
                "category": "Pokemon",
                "rarity": "Double Rare",
                "suffix": "ex",
                "hp": 330,
                "types": ["Darkness"],
                "attacks": [{"name": "Burning Darkness", "cost": ["Fire", "Fire"], "damage": "180+"}],
                "pricing": {"tcgplayer": {"holofoil": {"marketPrice": 42.50}}},
                "set": {"id": "sv03", "name": "Obsidian Flames"},
            },
            {
                "id": "sv03-999",
                "localId": "999",
                "name": "Corrupt Missing HP Pokemon",
                "category": "Pokemon",
                "rarity": "Common",
                "hp": None,
                "types": ["Fire"],
                "set": {"id": "sv03", "name": "Obsidian Flames"},
            },
        ],
    }

    df_clean, quarantined = validate_and_quarantine(sample_payload)
    assert len(df_clean) == 1
    assert df_clean.iloc[0]["mechanic_tier"] == "ex"
    assert df_clean.iloc[0]["market_price_usd"] == 42.50
    assert df_clean.iloc[0]["max_attack_damage"] == 180
    assert df_clean.iloc[0]["damage_per_energy_ratio"] == 90.0
    assert len(quarantined) == 1
    assert quarantined[0]["card_id"] == "sv03-999"


def test_authorized_quarantine_endpoint() -> None:
    resp = client.get("/v1/quarantine", headers={"X-API-Key": API_KEY_EXPECTED})
    assert resp.status_code == 200
    assert "quarantined_count" in resp.json()