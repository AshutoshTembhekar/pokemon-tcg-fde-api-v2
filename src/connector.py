import json
from pathlib import Path

import httpx

BASE_URL = "https://api.tcgdex.net/v2/en"
RAW_DIR = Path("data/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)


async def fetch_set_cards(set_id: str, max_cards: int = 60) -> dict:
    """
    Fetches set metadata and detailed card payloads from TCGdex API.
    Caches raw response to data/raw/{set_id}.json for offline fallback.
    """
    raw_file = RAW_DIR / f"{set_id}.json"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            set_resp = await client.get(f"{BASE_URL}/sets/{set_id}")
            set_resp.raise_for_status()
            set_data = set_resp.json()

            card_summaries = set_data.get("cards", [])[:max_cards]
            detailed_cards = []

            for summary in card_summaries:
                card_id = summary.get("id")
                if not card_id:
                    continue
                card_resp = await client.get(f"{BASE_URL}/cards/{card_id}")
                if card_resp.status_code == 200:
                    detailed_cards.append(card_resp.json())

            payload = {
                "set_id": set_data.get("id", set_id),
                "set_name": set_data.get("name", "Unknown Set"),
                "release_date": set_data.get("releaseDate"),
                "cards": detailed_cards,
            }
            raw_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return payload

        except httpx.HTTPError as exc:
            if raw_file.exists():
                return json.loads(raw_file.read_text(encoding="utf-8"))
            raise RuntimeError(f"Failed to fetch set '{set_id}' and no local cache exists: {exc}") from exc