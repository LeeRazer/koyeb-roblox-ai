from fastapi import FastAPI
from pydantic import BaseModel
from typing import Dict, List, Any, Optional
import time

app = FastAPI(title="Dungeon Racing Game - Global AI Director Server", version="2.0.0")

# Global trained state across all Roblox game servers
global_ai_config = {
    "version": 1,
    "last_updated": time.time(),
    "director_modifiers": {
        "horde_multiplier": 1.0,        # Multiplier for HordeMax in AIDirector
        "special_cap_bonus": 0,         # Extra specials for AIDirector (0, +1)
        "stress_accumulation_rate": 1.0, # Multiplier for playerStress in AIDirector
        "interval_speed": 1.0           # Multiplier for spawn interval timings
    },
    "zombie_tactics": {
        "Zombitt": {"flank_aggression": 0.5, "speed_buff": 1.0},
        "Infectus": {"attack_range_pref": 1.0},
        "GiggleBiT": {"charge_delay": 0.8},
        "ZombittCarcelero": {"grab_cooldown": 12.0, "aggro_range": 40.0},
        "ZombiePolice": {"shield_block_chance": 0.4}
    }
}

class RoomTelemetry(BaseModel):
    server_id: str
    difficulty: str  # "Easy", "Normal", "Hard", "Nightmare", "Endless"
    room_id: Optional[str] = "Unknown"
    players_count: int
    completion_time_seconds: float
    total_player_deaths: int
    total_incapacitations: int
    max_intensity_reached: float
    top_killer_zombie: Optional[str] = "None"

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Dungeon Racing Game AI Director",
        "version": global_ai_config["version"]
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/v1/dungeon-ai-config")
def get_dungeon_ai_config():
    """Endpoint consumed by AIDirector in Roblox Studio to adjust pacing dynamically"""
    return global_ai_config

@app.post("/api/v1/report-dungeon-telemetry")
def report_dungeon_telemetry(telemetry: RoomTelemetry):
    """Endpoint called by AIDirector at the end of each dungeon room/run"""
    global global_ai_config
    
    # Adaptive AI Pacing Logic:
    # If players clear room too fast with 0 deaths -> increase difficulty modifiers slightly
    if telemetry.completion_time_seconds < 45.0 and telemetry.total_player_deaths == 0:
        global_ai_config["director_modifiers"]["horde_multiplier"] = min(
            1.35, global_ai_config["director_modifiers"]["horde_multiplier"] + 0.02
        )
        global_ai_config["director_modifiers"]["stress_accumulation_rate"] = min(
            1.25, global_ai_config["director_modifiers"]["stress_accumulation_rate"] + 0.02
        )
    # If room wiped players out (high incapacitations/deaths) -> relax slightly globally
    elif telemetry.total_player_deaths >= telemetry.players_count:
        global_ai_config["director_modifiers"]["horde_multiplier"] = max(
            0.8, global_ai_config["director_modifiers"]["horde_multiplier"] - 0.03
        )
        global_ai_config["director_modifiers"]["stress_accumulation_rate"] = max(
            0.8, global_ai_config["director_modifiers"]["stress_accumulation_rate"] - 0.03
        )

    # Adapt specific zombie tactics based on top killer
    if telemetry.top_killer_zombie in global_ai_config["zombie_tactics"]:
        z_type = telemetry.top_killer_zombie
        if z_type == "ZombiePolice":
            global_ai_config["zombie_tactics"]["ZombiePolice"]["shield_block_chance"] = min(
                0.8, global_ai_config["zombie_tactics"]["ZombiePolice"]["shield_block_chance"] + 0.01
            )

    global_ai_config["version"] += 1
    global_ai_config["last_updated"] = time.time()

    return {
        "status": "success",
        "new_version": global_ai_config["version"],
        "message": "Global AI Director updated successfully"
    }
