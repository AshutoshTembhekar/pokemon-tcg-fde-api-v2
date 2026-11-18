import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from src.connector import fetch_set_cards
from src.validator import validate_and_quarantine

API_KEY_EXPECTED = os.getenv("FDE_API_KEY", "fde-tcg-dev-key-2026")
PROCESSED_DIR = Path("data/processed")
QUARANTINE_FILE = PROCESSED_DIR / "quarantine_log.json"

app = FastAPI(
    title="Pokémon TCG Market Valuation & Combat Analytics API (v2.0)",
    description="TCGdex ingestion, Pydantic v2 quarantine, TCGplayer/Cardmarket pricing, and EX/VMAX combat leaderboards.",
    version="2.0.0",
)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if not x_api_key or x_api_key != API_KEY_EXPECTED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return x_api_key


def _load_set_dataframe(set_id: str) -> pd.DataFrame:
    parquet_path = PROCESSED_DIR / f"{set_id}.parquet"
    if not parquet_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Set '{set_id}' not found in Parquet store. Run POST /v1/sync/set/{set_id} first.",
        )
    return pd.read_parquet(parquet_path)


@app.get("/health", tags=["Operations"])
def health_check() -> dict[str, str]:
    return {"status": "healthy", "service": "pokemon-tcg-market-analytics-v2", "version": "2.0.0"}


@app.post("/v1/sync/set/{set_id}", dependencies=[Depends(verify_api_key)], tags=["Ingestion"])
async def sync_set(set_id: str, limit: int = Query(default=60, ge=1, le=250)) -> dict[str, Any]:
    try:
        raw_payload = await fetch_set_cards(set_id=set_id, max_cards=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    df_clean, quarantine_log = validate_and_quarantine(raw_payload)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = PROCESSED_DIR / f"{set_id}.parquet"

    if not df_clean.empty:
        df_clean.to_parquet(parquet_path, index=False)

    QUARANTINE_FILE.write_text(json.dumps(quarantine_log, indent=2), encoding="utf-8")

    priced_count = int(df_clean["market_price_usd"].notnull().sum()) if not df_clean.empty else 0
    rulebox_count = int((df_clean["mechanic_tier"] != "Standard").sum()) if not df_clean.empty else 0

    return {
        "set_id": set_id,
        "set_name": raw_payload.get("set_name", set_id),
        "ingested_clean_count": len(df_clean),
        "priced_cards_count": priced_count,
        "rulebox_cards_count": rulebox_count,
        "quarantined_count": len(quarantine_log),
        "parquet_artifact": str(parquet_path),
    }


@app.get("/v1/cards", dependencies=[Depends(verify_api_key)], tags=["Analytics"])
def query_cards(
    set_id: str = Query(default="sv03"),
    primary_type: str | None = Query(default=None),
    mechanic_tier: str | None = Query(default=None, description="Filter by ex, VMAX, VSTAR, V, GX, Standard"),
    min_hp: int | None = Query(default=None, ge=1),
    min_price_usd: float | None = Query(default=None, ge=0.0),
    limit: int = Query(default=25, ge=1, le=200),
) -> dict[str, Any]:
    df = _load_set_dataframe(set_id)

    if primary_type:
        df = df[df["primary_type"].str.lower() == primary_type.lower()]
    if mechanic_tier:
        df = df[df["mechanic_tier"].str.lower() == mechanic_tier.lower()]
    if min_hp is not None:
        df = df[df["hp"].fillna(0) >= min_hp]
    if min_price_usd is not None:
        df = df[df["market_price_usd"].fillna(0.0) >= min_price_usd]

    records = df.head(limit).where(pd.notnull(df), None).to_dict(orient="records")
    return {"set_id": set_id, "matched_count": len(records), "cards": records}


@app.get("/v1/sets/{set_id}/analytics", dependencies=[Depends(verify_api_key)], tags=["Analytics"])
def set_summary_analytics(set_id: str) -> dict[str, Any]:
    df = _load_set_dataframe(set_id)
    pokemon_df = df[df["category"] == "Pokemon"].copy()

    rarity_dist = df["rarity"].value_counts().to_dict()
    tier_dist = df["mechanic_tier"].value_counts().to_dict()
    mean_hp_by_type = (
        pokemon_df.groupby("primary_type")["hp"].mean().round(2).to_dict()
        if not pokemon_df.empty
        else {}
    )

    top_damage_eff = (
        pokemon_df.sort_values(by=["damage_per_hp_ratio", "max_attack_damage"], ascending=False)
        .head(5)[
            [
                "card_id",
                "name",
                "mechanic_tier",
                "hp",
                "primary_attack_name",
                "max_attack_damage",
                "damage_per_hp_ratio",
            ]
        ]
        .to_dict(orient="records")
        if not pokemon_df.empty
        else []
    )

    return {
        "set_id": set_id,
        "total_clean_cards": len(df),
        "rarity_distribution": rarity_dist,
        "mechanic_tier_distribution": tier_dist,
        "mean_hp_by_type": mean_hp_by_type,
        "top_damage_efficiency_cards": top_damage_eff,
    }


@app.get("/v1/sets/{set_id}/market-leaders", dependencies=[Depends(verify_api_key)], tags=["Leaderboards"])
def set_market_leaders(set_id: str, top_n: int = Query(default=10, ge=1, le=50)) -> dict[str, Any]:
    df = _load_set_dataframe(set_id)
    priced_df = df[df["market_price_usd"].notnull()].copy()

    if priced_df.empty:
        return {
            "set_id": set_id,
            "priced_card_count": 0,
            "message": "No secondary market pricing records available for this set.",
            "most_valuable_cards": [],
            "avg_price_by_mechanic_tier": {},
        }

    most_valuable = (
        priced_df.sort_values(by="market_price_usd", ascending=False)
        .head(top_n)[
            [
                "card_id",
                "name",
                "rarity",
                "mechanic_tier",
                "hp",
                "market_price_usd",
                "price_source",
                "price_per_hp_usd",
            ]
        ]
        .where(pd.notnull(priced_df), None)
        .to_dict(orient="records")
    )

    avg_price_by_tier = (
        priced_df.groupby("mechanic_tier")["market_price_usd"]
        .agg(card_count="count", avg_price_usd="mean", max_price_usd="max")
        .round(2)
        .to_dict(orient="index")
    )

    return {
        "set_id": set_id,
        "priced_card_count": len(priced_df),
        "total_set_market_value_usd": round(float(priced_df["market_price_usd"].sum()), 2),
        "avg_price_by_mechanic_tier": avg_price_by_tier,
        "most_valuable_cards": most_valuable,
    }


@app.get("/v1/sets/{set_id}/combat-leaders", dependencies=[Depends(verify_api_key)], tags=["Leaderboards"])
def set_combat_leaders(set_id: str, top_n: int = Query(default=10, ge=1, le=50)) -> dict[str, Any]:
    df = _load_set_dataframe(set_id)
    pokemon_df = df[df["category"] == "Pokemon"].copy()

    if pokemon_df.empty:
        return {"set_id": set_id, "top_heavy_hitters": [], "avg_damage_by_type": {}}

    heavy_hitters = (
        pokemon_df.sort_values(by=["max_attack_damage", "damage_per_energy_ratio"], ascending=False)
        .head(top_n)[
            [
                "card_id",
                "name",
                "mechanic_tier",
                "primary_type",
                "hp",
                "primary_attack_name",
                "max_attack_damage",
                "primary_attack_energy_cost",
                "damage_per_energy_ratio",
            ]
        ]
        .to_dict(orient="records")
    )

    avg_dmg_by_type = (
        pokemon_df.groupby("primary_type")["max_attack_damage"].mean().round(2).to_dict()
    )

    return {
        "set_id": set_id,
        "top_heavy_hitters": heavy_hitters,
        "avg_damage_by_type": avg_dmg_by_type,
    }


@app.get("/v1/quarantine", dependencies=[Depends(verify_api_key)], tags=["Data Quality"])
def get_quarantined_records() -> dict[str, Any]:
    if not QUARANTINE_FILE.exists():
        return {"quarantined_count": 0, "records": []}
    records = json.loads(QUARANTINE_FILE.read_text(encoding="utf-8"))
    return {"quarantined_count": len(records), "records": records}