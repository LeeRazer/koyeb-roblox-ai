from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_DIRECTOR_MODIFIERS: dict[str, float | int] = {
    "horde_multiplier": 1.0,
    "special_cap_bonus": 0,
    "stress_accumulation_rate": 1.0,
    "interval_speed": 1.0,
}


# The action names intentionally match the states emitted by the Roblox Brains.
# Values are priors in the normalized [-1, 1] reward domain. They remain inert on
# the Roblox side until enough real samples have been collected.
DEFAULT_ZOMBIES: dict[str, dict[str, Any]] = {
    "Zombitt": {
        "tactics": {"approach_lateral": 0.62, "orbit_bias": 0.20, "aggression": 0.5},
        "q_values": {"Attack": 0.20, "Chase": 0.35},
    },
    "GiggleBiT": {
        "tactics": {"intercept_prediction": 0.85, "reference_speed": 22.0, "never_retreat": True},
        "q_values": {"Attack": 0.30, "Chase": 0.45},
    },
    "Zombie Police": {
        "tactics": {"shield_block_chance": 0.45, "orbit_bias": 0.18, "push_freq": 0.4},
        "q_values": {"Attack": 0.25, "Chase": 0.30},
    },
    "ZombittCarcelero": {
        "tactics": {"chain_grab_stamina_wait": 0.3, "tactical_range": 32.0},
        "q_values": {"AttackGrab": 0.35, "AttackMelee": 0.15, "Chase": 0.20},
    },
    "Infectus": {
        "tactics": {"preferred_min": 6.0, "preferred_max": 11.0, "retreat_lateral": 0.65},
        "q_values": {"AttackVomit": 0.35, "AttackMelee": 0.10, "Chase": 0.20},
    },
    "Gross": {
        "tactics": {"reference_speed": 8.0, "aoe_slam_dist": 14.0},
        "q_values": {"AttackClose": 0.30, "Scream": 0.25, "Chase": 0.20},
    },
    "ZombittRecolector": {
        "tactics": {"preferred_min": 3.8, "approach_lateral": 0.62},
        "q_values": {"RageAttack": 0.30, "Recolectar": 0.15, "Chase": 0.20},
    },
    "ZombittResonante": {
        "tactics": {"preferred_min": 20.0, "tactical_range": 45.0},
        "q_values": {"AttackSpit": 0.35, "AttackMelee": 0.10, "Chase": 0.25},
    },
    "ZombittCarmesi": {
        "tactics": {"preferred_min": 17.0, "tactical_range": 38.0},
        "q_values": {"MortisSkill": 0.35, "AttackMelee": 0.10, "Chase": 0.25},
    },
    "JoRvboT": {
        "tactics": {"tactical_range": 48.0, "preferred_min": 20.0},
        "q_values": {"Attack": 0.35, "Retreat": 0.20, "Chase": 0.15},
    },
}


ENEMY_TYPES = frozenset(DEFAULT_ZOMBIES)


# v3 clients used tactical labels instead of actual Brain states. They remain
# accepted during migration but are normalized before training.
ACTION_ALIASES: dict[str, dict[str, str]] = {
    "Zombitt": {"zigzag_step": "Chase", "straight_charge": "Chase"},
    "GiggleBiT": {"vector_intercept": "Chase", "flank_ambush": "Chase"},
    "Zombie Police": {"align_shield": "Chase", "shield_bash": "Attack"},
    "ZombittCarcelero": {
        "stamina_drain_grab": "AttackGrab",
        "instant_grab": "AttackGrab",
    },
    "Infectus": {"spit_kiting": "AttackVomit", "close_range_spit": "AttackVomit"},
    "Gross": {"heavy_slam": "AttackClose", "body_block": "Chase"},
    "ZombittRecolector": {"scavenge_flank": "Recolectar", "direct_melee": "RageAttack"},
    "ZombittResonante": {"long_range_wave": "AttackSpit", "reposition": "Chase"},
    "ZombittCarmesi": {"crimson_burst": "MortisSkill", "strafe_shoot": "Chase"},
    "JoRvboT": {"snipe_beam": "Attack", "tactical_retreat": "Retreat"},
}


def normalize_action(enemy_type: str, action_name: str) -> str | None:
    if action_name in DEFAULT_ZOMBIES.get(enemy_type, {}).get("q_values", {}):
        return action_name
    return ACTION_ALIASES.get(enemy_type, {}).get(action_name)


def default_zombies() -> dict[str, dict[str, Any]]:
    return deepcopy(DEFAULT_ZOMBIES)

