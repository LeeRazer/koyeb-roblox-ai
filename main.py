from fastapi import FastAPI
from pydantic import BaseModel
from typing import Dict, List, Any
import time

app = FastAPI(title="Roblox AI Brain Server", version="1.0.0")

# Persistent state in memory (can be backed up to file/database)
ai_knowledge = {
    "version": 1,
    "last_updated": time.time(),
    "tactics": {
        "range_defense_level": 0.5,  # 0.0 to 1.0 (How much AI counters ranged attacks)
        "melee_parry_level": 0.3,    # 0.0 to 1.0 (How aggressive AI is in melee counter)
        "flank_preference": 0.4,     # 0.0 to 1.0 (How much AI prefers flanking)
        "retreat_threshold": 0.25    # Health % at which AI retreats
    },
    "danger_spots": [] # List of {x, y, z} player death spots
}

class TelemetryPayload(BaseModel):
    server_id: str
    ranged_attacks: int = 0
    melee_attacks: int = 0
    player_deaths: List[Dict[str, float]] = []
    boss_victories: int = 0
    player_victories: int = 0

@app.get("/")
def root():
    return {
        "message": "Roblox AI Brain Server is running live on Koyeb!",
        "version": ai_knowledge["version"]
    }

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/get-ai-weights")
def get_ai_weights():
    """Returns the current trained weights and tactics to Roblox servers"""
    return ai_knowledge

@app.post("/train-ai")
def train_ai(payload: TelemetryPayload):
    """Receives batch telemetry from Roblox and updates AI intelligence"""
    global ai_knowledge
    
    # 1. Adapt tactics based on player attack styles
    if payload.ranged_attacks > payload.melee_attacks:
        # Increase ranged defense counter
        ai_knowledge["tactics"]["range_defense_level"] = min(
            0.95, ai_knowledge["tactics"]["range_defense_level"] + 0.03
        )
    elif payload.melee_attacks > payload.ranged_attacks:
        # Increase melee parry/counter aggressiveness
        ai_knowledge["tactics"]["melee_parry_level"] = min(
            0.95, ai_knowledge["tactics"]["melee_parry_level"] + 0.03
        )
        
    # 2. Adapt based on win/loss ratios
    if payload.player_victories > payload.boss_victories:
        # Players are winning too easily -> AI becomes smarter at flanking
        ai_knowledge["tactics"]["flank_preference"] = min(
            0.9, ai_knowledge["tactics"]["flank_preference"] + 0.04
        )

    # 3. Store player death spots (keep max 100 spots to save memory)
    for spot in payload.player_deaths:
        ai_knowledge["danger_spots"].append(spot)
    if len(ai_knowledge["danger_spots"]) > 100:
        ai_knowledge["danger_spots"] = ai_knowledge["danger_spots"][-100:]

    ai_knowledge["version"] += 1
    ai_knowledge["last_updated"] = time.time()

    return {
        "status": "success",
        "new_version": ai_knowledge["version"],
        "message": "AI updated successfully"
    }
