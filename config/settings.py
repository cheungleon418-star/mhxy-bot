# -*- coding: utf-8 -*-
"""Legacy constants used by the original modules.

New code should load :mod:`config.runtime`. These values remain so the old
modules import cleanly while the first single-window treasure-map flow is
migrated. Paths point to private runtime data, never to repository assets.
"""

from __future__ import annotations

from .runtime import DEFAULT_PROFILE, REQUIRED_TEMPLATES, runtime_paths


_PATHS = runtime_paths()
TEMPLATES_DIR = str(_PATHS.profile_templates(DEFAULT_PROFILE))

# A compatibility fallback only. Live execution binds the actual HWND client
# rectangle; it must not rely on these coordinates.
ACCOUNT_WINDOWS = [
    {"name": "单窗口", "x": 0, "y": 0, "width": 1280, "height": 960},
]
SCREEN_RESOLUTION = (1280, 960)

MATCH_THRESHOLD = 0.90
RETRY_COUNT = 2
RECOGNIZE_INTERVAL = 0.2
AVG_SAMPLES = 3
SCREEN_SCALE = 1.0

_LEGACY_TEMPLATE_NAMES = (
    "quest_npc_flag",
    "quest_accept_btn",
    "quest_submit_btn",
    "dialog_continue",
    "dialog_confirm",
    "dialog_close",
    "combat_enemy_area",
    "combat_end_dialog",
    "death_dialog",
    "fly_flag_icon",
    "fly_scroll_icon",
    "warehouse_npc",
    "friend_icon",
    "login_button",
    "login_screen",
)
TEMPLATES = {
    name: str(_PATHS.profile_templates(DEFAULT_PROFILE) / f"{name}.png")
    for name in (*REQUIRED_TEMPLATES, *_LEGACY_TEMPLATE_NAMES)
}

# F9 is "屏蔽其他玩家" in this game. It is deliberately not exposed as an
# automatic-combat key. The supported flow lets the game handle combat and
# sends no skill, potion, target or combat-toggle input.
KEYS = {
    "map_key": "m",
    "quest_key": "j",
    "backpack_key": "i",
    "interact": "space",
    "confirm": "return",
    "cancel": "escape",
    "hide_other_players": "f9",
    # Retained for imports in unverified modes; treasure_map never uses them.
    "skill_1": "f1",
    "skill_2": "f2",
    "skill_3": "f3",
    "skill_4": "f4",
    "skill_5": "f5",
    "skill_6": "f6",
    "skill_7": "f7",
    "skill_8": "f8",
    "hp_potion": "f6",
    "mp_potion": "f7",
    "switch_target": "tab",
}

COMMON = {
    "click_delay": 200,
    "key_delay": 150,
    "action_delay": 500,
    "post_action_wait": 800,
    "random_delay_min": 100,
    "random_delay_max": 400,
    "esc_count": 2,
}

NAV = {
    "open_map_delay": 800,
    "auto_path_wait": 5000,
    "path_check_interval": 1000,
    "interact_delay": 600,
    "approach_wait": 2000,
    "dialog_wait": 400,
    "max_dialog_steps": 15,
    "map_width": 545,
    "map_height": 276,
    "max_map_x": 548,
    "max_map_y": 547,
    "base_click_x": 400,
    "base_click_y": 300,
    "scale_factor": 1.0,
    "unblock_max_retries": 3,
    "unblock_ctrl_trigger": True,
    "unblock_wait_moving": 3.0,
    "new_district_mode": False,
    "new_district_delay_mult": 1.5,
    "new_district_retry_mult": 2,
    "fly_destinations": {
        "长安城": {"x": 100, "y": 200},
        "建邺城": {"x": 200, "y": 100},
        "傲来国": {"x": 300, "y": 150},
        "长寿村": {"x": 150, "y": 300},
        "大唐国境": {"x": 280, "y": 40},
        "大唐境外": {"x": 100, "y": 50},
        "西梁女国": {"x": 400, "y": 100},
        "宝象国": {"x": 350, "y": 200},
    },
}

COMBAT = {
    "input_mode": "game_builtin_auto",
    "send_combat_inputs": False,
    "combat_start_wait": 1500,
    "combat_end_wait": 2000,
    "combat_timeout": 120,
    "skill_interval": 300,
    "switch_account_delay": 200,
    "skill_rotation": [],
    "target_mode": "none",
    "hp_potion_threshold": 0,
    "mp_potion_threshold": 0,
    "potion_cooldown": 800,
    "death_auto_respawn": False,
    "death_scene_timeout": 60,
    "return_city_delay": 3000,
}

LOOP = {
    "interval": 0.2,
    "mode": "treasure_map",
    "max_hours": 0,
    "auto_exit": True,
    "logging": True,
    "log_file": str(_PATHS.logs / "mhxy_bot.log"),
    "hotkey_exit": "f11",
    "hotkey_pause": "f12",
    "stuck_detection": True,
    "stuck_timeout": 60,
    "after_daily": "stop",
    "wait_time": 0,
}
