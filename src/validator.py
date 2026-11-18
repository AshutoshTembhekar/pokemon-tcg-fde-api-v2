import re
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, ValidationError, model_validator


class ValidatedCard(BaseModel):
    card_id: str
    local_id: str
    name: str
    category: str
    set_id: str
    set_name: str
    rarity: str
    mechanic_tier: str
    hp: int | None = None
    primary_type: str = "Colorless"
    types: list[str] = Field(default_factory=list)
    primary_attack_name: str = "None"
    max_attack_damage: int = 0
    primary_attack_energy_cost: int = 0
    damage_per_hp_ratio: float = 0.0
    damage_per_energy_ratio: float = 0.0
    market_price_usd: float | None = None
    price_source: str = "unavailable"
    price_per_hp_usd: float | None = None


def _classify_mechanic_tier(name: str, suffix: str | None = None) -> str:
    """Classify card into rulebox mechanic tiers (ex, VMAX, VSTAR, V, GX, EX, Standard)."""
    token = f"{name} {suffix or ''}".strip()
    if re.search(r"\bVMAX\b", token):
        return "VMAX"
    if re.search(r"\bVSTAR\b", token):
        return "VSTAR"
    if re.search(r"\bGX\b", token):
        return "GX"
    if re.search(r"\bEX\b", token):
        return "EX (Classic)"
    if re.search(r"(?:\bex\b|-ex$)", token):
        return "ex"
    if re.search(r"\bV\b", token):
        return "V"
    return "Standard"


def _extract_market_price(pricing: dict[str, Any] | None) -> tuple[float | None, str]:
    """Extract best available secondary market price in USD from TCGplayer or Cardmarket."""
    if not isinstance(pricing, dict):
        return None, "unavailable"

    tcgplayer = pricing.get("tcgplayer")
    if isinstance(tcgplayer, dict):
        for variant in (
            "holofoil",
            "normal",
            "reverse-holofoil",
            "1st-edition-holofoil",
            "unlimited-holofoil",
        ):
            variant_data = tcgplayer.get(variant)
            if isinstance(variant_data, dict):
                for price_key in ("marketPrice", "midPrice", "directLowPrice", "lowPrice"):
                    val = variant_data.get(price_key)
                    if isinstance(val, (int, float)) and val > 0:
                        return round(float(val), 2), f"tcgplayer:{variant}"

    cardmarket = pricing.get("cardmarket")
    if isinstance(cardmarket, dict):
        for cm_key in ("trend", "avg30", "avg7", "avg", "low"):
            val = cardmarket.get(cm_key)
            if isinstance(val, (int, float)) and val > 0:
                return round(float(val) * 1.08, 2), f"cardmarket:{cm_key}"

    return None, "unavailable"


def _parse_combat_metrics(attacks: list[dict[str, Any]] | None) -> tuple[str, int, int]:
    """Return (top_attack_name, max_damage, energy_cost_for_top_attack)."""
    if not attacks:
        return "None", 0, 0

    best_name = "None"
    best_dmg = 0
    best_cost = 0

    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw_dmg = str(atk.get("damage") or "0")
        digits = re.findall(r"\d+", raw_dmg)
        dmg_val = int(digits[0]) if digits else 0
        cost_list = atk.get("cost") or []
        cost_len = len(cost_list) if isinstance(cost_list, list) else 0

        if dmg_val > best_dmg or (dmg_val == best_dmg and best_name == "None"):
            best_dmg = dmg_val
            best_name = str(atk.get("name") or "Unknown")
            best_cost = cost_len

    return best_name, best_dmg, best_cost


class RawCardInput(BaseModel):
    id: str
    localId: str
    name: str
    category: str
    rarity: str | None = None
    suffix: str | None = None
    hp: int | None = None
    types: list[str] | None = None
    attacks: list[dict[str, Any]] | None = None
    pricing: dict[str, Any] | None = None
    set: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_domain_rules(self) -> "RawCardInput":
        if not self.rarity or not str(self.rarity).strip():
            raise ValueError("Missing required rarity classification")

        if self.category == "Pokemon":
            if self.hp is None or self.hp <= 0:
                raise ValueError(f"Pokemon card '{self.name}' must have positive integer HP")
            if not self.types or len(self.types) == 0:
                raise ValueError(f"Pokemon card '{self.name}' missing elemental typing")
        return self

    def to_validated(
        self, fallback_set_id: str = "unknown", fallback_set_name: str = "Unknown Set"
    ) -> ValidatedCard:
        top_atk_name, max_dmg, atk_cost = _parse_combat_metrics(self.attacks)
        types_clean = self.types if self.types else ["Colorless"]
        hp_val = self.hp if (self.hp and self.hp > 0) else None

        dmg_hp_ratio = round(max_dmg / hp_val, 4) if hp_val else 0.0
        dmg_energy_ratio = round(max_dmg / atk_cost, 2) if atk_cost > 0 else float(max_dmg)

        market_usd, price_src = _extract_market_price(self.pricing)
        price_hp_ratio = round(market_usd / hp_val, 4) if (market_usd and hp_val) else None
        tier = _classify_mechanic_tier(self.name, self.suffix)

        return ValidatedCard(
            card_id=self.id,
            local_id=self.localId,
            name=self.name,
            category=self.category,
            set_id=str(self.set.get("id") or fallback_set_id),
            set_name=str(self.set.get("name") or fallback_set_name),
            rarity=str(self.rarity),
            mechanic_tier=tier,
            hp=hp_val,
            primary_type=types_clean[0],
            types=types_clean,
            primary_attack_name=top_atk_name,
            max_attack_damage=max_dmg,
            primary_attack_energy_cost=atk_cost,
            damage_per_hp_ratio=dmg_hp_ratio,
            damage_per_energy_ratio=dmg_energy_ratio,
            market_price_usd=market_usd,
            price_source=price_src,
            price_per_hp_usd=price_hp_ratio,
        )


def validate_and_quarantine(
    raw_payload: dict[str, Any] | list[dict[str, Any]],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Accept either a connector dict {'set_id', 'set_name', 'cards': [...]} or raw list of cards."""
    if isinstance(raw_payload, dict):
        raw_cards = raw_payload.get("cards", [])
        set_id = str(raw_payload.get("set_id", "unknown"))
        set_name = str(raw_payload.get("set_name", "Unknown Set"))
    else:
        raw_cards = raw_payload
        set_id = "unknown"
        set_name = "Unknown Set"

    clean_records: list[dict[str, Any]] = []
    quarantined_records: list[dict[str, Any]] = []

    for payload in raw_cards:
        try:
            parsed = RawCardInput.model_validate(payload)
            clean_records.append(parsed.to_validated(set_id, set_name).model_dump())
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            quarantined_records.append(
                {
                    "card_id": payload.get("id", "unknown"),
                    "name": payload.get("name", "unknown"),
                    "error": str(exc),
                    "raw_payload": payload,
                }
            )

    df_clean = pd.DataFrame(clean_records) if clean_records else pd.DataFrame()
    return df_clean, quarantined_records