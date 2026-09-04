# -*- coding: utf-8 -*-
"""Passive battle-state observer.

The game client owns automatic combat. This compatibility handler can inspect
combat and resource bars, but every method that previously sent a combat key is
disabled and ``fight`` never blocks.
"""

import logging
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class CombatHandler:
    """Read-only compatibility facade for legacy mode loops."""

    def __init__(self, window_group, input_sim, screen_mgr):
        self.wg = window_group
        self.input = input_sim
        self.screen = screen_mgr

    def in_combat(self) -> bool:
        """检测是否处于战斗状态"""
        for i in range(len(self.wg.windows)):
            # New calibrated profiles use combat_hud_anchor. Keep the old
            # name as a read-only compatibility fallback.
            if (self.screen.find_template("combat_hud_anchor", i)
                    or self.screen.find_template("combat_enemy_area", i)):
                self.wg.windows[i].in_combat = True
                return True
        for win in self.wg.windows:
            win.in_combat = False
        return False

    def get_enemy_count(self, window_index: int = 0) -> int:
        """估算敌方数量"""
        pos = self.screen.find_template("combat_enemy_area", window_index)
        return 1 if pos else 0

    def switch_target(self):
        """Disabled: combat is owned by the game's automatic battle mode."""
        raise RuntimeError("Combat input is disabled; the game handles automatic battle")

    def check_hp_low(self, window_index: int = 0) -> Optional[float]:
        """检测窗口HP百分比

        通过检测左上角红色HP条的像素占比来计算真实百分比
        返回 HP百分比 (0-100)，None表示无法检测
        """
        # HP条通常在角色头像左上方，截取该区域
        win = self.wg.windows[window_index]
        # 截取顶部区域（HP条典型位置）
        bar_x, bar_y = win.x + 20, win.y + 30
        bar_w, bar_h = 150, 20

        region = self.screen.capture_region(window_index, bar_x - win.x, bar_y - win.y, bar_w, bar_h)

        # 红色HP条颜色范围
        # Captures are OpenCV BGR, so red is the final channel.
        lower = np.array([20, 20, 150], dtype=np.uint8)
        upper = np.array([100, 100, 255], dtype=np.uint8)
        mask = cv2.inRange(region, lower, upper)

        # 计算有颜色的像素占比
        total_pixels = bar_w * bar_h
        colored_pixels = cv2.countNonZero(mask)

        if colored_pixels < 10:
            return None  # 没有检测到HP条

        hp_percent = (colored_pixels / total_pixels) * 100
        return round(hp_percent, 1)

    def check_mp_low(self, window_index: int = 0) -> Optional[float]:
        """检测窗口MP百分比

        通过检测蓝色MP条的像素占比来计算真实百分比
        """
        win = self.wg.windows[window_index]
        # MP条通常在HP条下方
        bar_x, bar_y = win.x + 20, win.y + 55
        bar_w, bar_h = 150, 20

        region = self.screen.capture_region(window_index, bar_x - win.x, bar_y - win.y, bar_w, bar_h)

        # 蓝色MP条颜色范围
        # Captures are OpenCV BGR, so blue is the first channel.
        lower = np.array([150, 30, 20], dtype=np.uint8)
        upper = np.array([255, 100, 100], dtype=np.uint8)
        mask = cv2.inRange(region, lower, upper)

        total_pixels = bar_w * bar_h
        colored_pixels = cv2.countNonZero(mask)

        if colored_pixels < 10:
            return None

        mp_percent = (colored_pixels / total_pixels) * 100
        return round(mp_percent, 1)

    def use_skill_on_account(self, account_index: int, skill_slot: int):
        """Disabled to guarantee that the assistant sends no combat keys."""
        raise RuntimeError("Combat skill input is disabled; the game handles automatic battle")

    def use_potion(self, account_index: int, potion_type: str = "hp"):
        """Disabled to guarantee that the assistant sends no combat keys."""
        raise RuntimeError("Combat potion input is disabled; the game handles automatic battle")

    def fight(self, window_group):
        """Observe one combat frame without blocking or issuing input.

        The return value is ``True`` only when combat is no longer detected.
        It is retained for legacy callers; the treasure workflow uses its own
        COMBAT_WAIT state and does not call this method.
        """
        active = self.in_combat()
        if active:
            logger.debug("战斗中：由游戏内置自动战斗处理")
            return False
        self._handle_combat_end()
        return True

    def _check_combat_end(self) -> bool:
        """检测战斗是否结束"""
        for i in range(len(self.wg.windows)):
            result = self.screen.find_template("combat_end_dialog", i)
            if result:
                return True
        return False

    def _handle_combat_end(self):
        """Clear passive flags only; result dialogs belong to task states."""
        for win in self.wg.windows:
            win.in_combat = False
