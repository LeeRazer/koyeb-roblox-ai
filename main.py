from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, List, Any, Optional
import sqlite3
import time
import json
import os

app = FastAPI(title="Roblox Zombie AI Q-Learning Brain", version="3.0.0")

DB_PATH = "zombie_ai_brain.db"

# 1. Initialize SQLite Database for Permanent Multi-Game Persistence
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS zombie_weights (
            enemy_type TEXT PRIMARY KEY,
            tactics_json TEXT,
            q_values_json TEXT,
            total_battles INTEGER DEFAULT 0,
            last_updated REAL
        )
    """)
    conn.commit()

    # Initial default Q-values & tactics for the 10 Zombie Types
    default_zombies = {
        "Zombitt": {
            "tactics": {"approach_lateral": 0.62, "orbit_bias": 0.20, "aggression": 0.5},
            "q_values": {"zigzag_step": 10.0, "straight_charge": 5.0}
        },
        "GiggleBiT": {
            "tactics": {"intercept_prediction": 0.85, "reference_speed": 22.0, "never_retreat": True},
            "q_values": {"vector_intercept": 15.0, "flank_ambush": 12.0}
        },
        "Zombie Police": {
            "tactics": {"shield_block_chance": 0.45, "orbit_bias": 0.18, "push_freq": 0.4},
            "q_values": {"align_shield": 12.0, "shield_bash": 8.0}
        },
        "ZombittCarcelero": {
            "tactics": {"chain_grab_stamina_wait": 0.3, "tactical_range": 32.0},
            "q_values": {"stamina_drain_grab": 14.0, "instant_grab": 6.0}
        },
        "Infectus": {
            "tactics": {"preferred_min": 6.0, "preferred_max": 11.0, "retreat_lateral": 0.65},
            "q_values": {"spit_kiting": 11.0, "close_range_spit": 4.0}
        },
        "Gross": {
            "tactics": {"reference_speed": 8.0, "aoe_slam_dist": 14.0},
            "q_values": {"heavy_slam": 10.0, "body_block": 7.0}
        },
        "ZombittRecolector": {
            "tactics": {"preferred_min": 3.8, "approach_lateral": 0.62},
            "q_values": {"scavenge_flank": 8.0, "direct_melee": 5.0}
        },
        "ZombittResonante": {
            "tactics": {"preferred_min": 20.0, "tactical_range": 45.0},
            "q_values": {"long_range_wave": 12.0, "reposition": 9.0}
        },
        "ZombittCarmesi": {
            "tactics": {"preferred_min": 17.0, "tactical_range": 38.0},
            "q_values": {"crimson_burst": 11.0, "strafe_shoot": 8.0}
        },
        "JoRvboT": {
            "tactics": {"tactical_range": 48.0, "preferred_min": 20.0},
            "q_values": {"snipe_beam": 13.0, "tactical_retreat": 10.0}
        }
    }

    for enemy_name, data in default_zombies.items():
        cursor.execute("SELECT enemy_type FROM zombie_weights WHERE enemy_type = ?", (enemy_name,))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO zombie_weights (enemy_type, tactics_json, q_values_json, total_battles, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (
                enemy_name,
                json.dumps(data["tactics"]),
                json.dumps(data["q_values"]),
                0,
                time.time()
            ))
    conn.commit()
    conn.close()

init_db()

# Models
class BattleActionResult(BaseModel):
    enemy_type: str
    action_name: str         # e.g., "zigzag_step", "stamina_drain_grab", "vector_intercept"
    reward: float            # Positive reward (e.g. +10 for hit/damage) or Negative penalty (-5 for quick death)
    player_dashed: bool = False
    player_health_lost: float = 0.0

@app.get("/")
def root():
    return {"status": "online", "service": "Roblox Q-Learning Zombie AI", "db": "SQLite persistent"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/v1/zombie-brain-weights")
def get_zombie_brain_weights():
    """Returns the trained weights & tactics for all 10 Zombie types"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT enemy_type, tactics_json, q_values_json, total_battles FROM zombie_weights")
    rows = cursor.fetchall()
    conn.close()

    result = {}
    for row in rows:
        enemy_type, tactics_json, q_values_json, battles = row
        result[enemy_type] = {
            "tactics": json.loads(tactics_json),
            "q_values": json.loads(q_values_json),
            "total_battles": battles
        }
    return {"version": int(time.time()), "zombies": result}

@app.post("/api/v1/report-zombie-action")
def report_zombie_action(action: BattleActionResult):
    """Q-Learning update endpoint called asynchronously from Roblox"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT tactics_json, q_values_json, total_battles FROM zombie_weights WHERE enemy_type = ?", (action.enemy_type,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Enemy type not registered")
        
    tactics = json.loads(row[0])
    q_values = json.loads(row[1])
    battles = row[2] + 1
    
    # Q-Learning Bellman Update Rule: Q(s,a) = Q(s,a) + alpha * (reward - Q(s,a))
    alpha = 0.15 # Learning rate
    current_q = q_values.get(action.action_name, 5.0)
    new_q = current_q + alpha * (action.reward - current_q)
    q_values[action.action_name] = round(new_q, 3)

    # Dynamic adaptation of physical tactics based on learned Q-values:
    if action.enemy_type == "Zombitt":
        if q_values.get("zigzag_step", 0) > q_values.get("straight_charge", 0):
            tactics["approach_lateral"] = min(0.88, tactics.get("approach_lateral", 0.62) + 0.01)
    elif action.enemy_type == "ZombittCarcelero":
        if action.player_dashed and action.reward > 0:
            tactics["chain_grab_stamina_wait"] = min(0.90, tactics.get("chain_grab_stamina_wait", 0.3) + 0.02)
    elif action.enemy_type == "Zombie Police":
        if action.reward > 0:
            tactics["shield_block_chance"] = min(0.85, tactics.get("shield_block_chance", 0.45) + 0.01)
    elif action.enemy_type == "GiggleBiT":
        if action.reward > 0:
            tactics["intercept_prediction"] = min(0.95, tactics.get("intercept_prediction", 0.85) + 0.01)

    cursor.execute("""
        UPDATE zombie_weights 
        SET tactics_json = ?, q_values_json = ?, total_battles = ?, last_updated = ?
        WHERE enemy_type = ?
    """, (json.dumps(tactics), json.dumps(q_values), battles, time.time(), action.enemy_type))
    
    conn.commit()
    conn.close()
    
    return {"status": "success", "updated_q": new_q}

@app.get("/api/v1/backup-db")
def backup_db():
    """Allows downloading the SQLite database file directly to PC"""
    if os.path.exists(DB_PATH):
        return FileResponse(DB_PATH, filename="zombie_ai_brain.db", media_type="application/x-sqlite3")
    raise HTTPException(status_code=404, detail="Database file not found")
