from __future__ import annotations

import unittest

from modules.combat import CombatHandler
from modules.tasks.treasure_map import (
    TreasureMapHandler,
    TreasureMapPolicy,
    TreasureMapStateMachine,
    TreasureObservation,
    TreasureState,
)


POINTS = {
    "treasure_map_icon": (120, 240),
    "quest_target_marker": (500, 360),
    "reward_confirm_button": (640, 520),
}


def detection(name: str):
    point = POINTS.get(name)
    if point is None:
        return {"confidence": 0.99}
    return {"confidence": 0.99, "center": point}


def observation(t: float, *visible: str, inventory: str | None = None,
                task_active: bool | None = None, resume: bool = False):
    return TreasureObservation(
        timestamp=t,
        detections={name: detection(name) for name in visible},
        inventory_signature=inventory,
        task_active=task_active,
        resume_requested=resume,
    )


def kind(intent) -> str | None:
    if intent is None:
        return None
    value = intent.kind
    return value.value if hasattr(value, "value") else str(value)


def quick_policy(**overrides) -> TreasureMapPolicy:
    values = {
        "stable_frames": 3,
        "max_retries": 3,
        "action_retry_seconds": 1.0,
        "ready_timeout": 10.0,
        "open_backpack_timeout": 7.0,
        "select_map_timeout": 3.0,
        "verify_committed_timeout": 5.0,
        "verification_probe_seconds": 2.0,
        "open_map_timeout": 7.0,
        "find_marker_timeout": 4.0,
        "navigate_timeout": 6.0,
        "dig_timeout": 3.0,
        "classify_timeout": 8.0,
        "combat_timeout": 20.0,
        "death_timeout": 60.0,
        "reconcile_timeout": 8.0,
        "reward_settle_seconds": 0.5,
    }
    values.update(overrides)
    return TreasureMapPolicy(**values)


class Driver:
    def __init__(self, machine: TreasureMapStateMachine):
        self.machine = machine
        self.t = 0.0
        self.intents = []

    def tick(self, *visible: str, inventory: str | None = None,
             task_active: bool | None = None, resume: bool = False,
             step: float = 0.1):
        intent = self.machine.tick(observation(
            self.t, *visible, inventory=inventory,
            task_active=task_active, resume=resume,
        ))
        if intent is not None:
            self.intents.append(intent)
            self.machine.acknowledge_action(
                intent, accepted=True, timestamp=self.t, reason="test_executor"
            )
        self.t += step
        return intent

    def stable(self, *visible: str, inventory: str | None = None,
               task_active: bool | None = None):
        result = None
        for _ in range(self.machine.policy.stable_frames):
            result = self.tick(*visible, inventory=inventory, task_active=task_active)
        return result

    def drive_to_verify(self):
        intent = self.stable("world_hud_anchor")
        self.assert_intent(intent, "key", key="i")
        self.stable("world_hud_anchor", "backpack_open", "treasure_map_icon",
                    inventory="stack:2")
        intent = self.tick("world_hud_anchor", "backpack_open", "treasure_map_icon",
                           inventory="stack:2")
        self.assert_intent(intent, "click")
        assert self.machine.state is TreasureState.VERIFY_COMMITTED

    def drive_to_classify(self):
        self.drive_to_verify()
        intent = self.stable("world_hud_anchor", "backpack_open", "treasure_map_icon",
                             inventory="stack:1")
        self.assert_intent(intent, "key", key="escape")
        assert self.machine.processed_maps == 1
        intent = self.stable("world_hud_anchor", inventory="stack:1")
        self.assert_intent(intent, "key", key="m")
        self.stable("world_hud_anchor", "map_panel_anchor", "quest_target_marker",
                    inventory="stack:1")
        intent = self.tick("world_hud_anchor", "map_panel_anchor", "quest_target_marker",
                           inventory="stack:1")
        self.assert_intent(intent, "click")
        assert self.machine.state is TreasureState.NAVIGATE
        self.stable("world_hud_anchor", "dig_interact_prompt", inventory="stack:1")
        intent = self.tick("world_hud_anchor", "dig_interact_prompt", inventory="stack:1")
        self.assert_intent(intent, "interact")
        assert self.machine.state is TreasureState.CLASSIFY

    def finish_death_reconciliation(
        self, *, active: bool, inventory_has_map: bool = True, inventory: str = "stack:1"
    ):
        inventory_signals = ("treasure_map_icon",) if inventory_has_map else ()
        intent = None
        for _ in range(self.machine.policy.stable_frames):
            emitted = self.tick(
                "world_hud_anchor", "death_return_scene_anchor", "backpack_open",
                *inventory_signals, inventory=inventory,
            )
            intent = emitted or intent
        self.assert_intent(intent, "key", key="escape")
        assert self.machine.state is TreasureState.DEATH_CLOSE_BACKPACK

        self.stable("world_hud_anchor", "death_return_scene_anchor", inventory=inventory)
        assert self.machine.state is TreasureState.DEATH_OPEN_TASK_PANEL
        intent = self.tick("world_hud_anchor", "death_return_scene_anchor", inventory=inventory)
        self.assert_intent(intent, "key", key="j")

        status_signal = "active_treasure_task" if active else "no_active_treasure_task"
        self.stable(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            status_signal, inventory=inventory,
        )
        intent = self.tick(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            status_signal, inventory=inventory,
        )
        self.assert_intent(intent, "key", key="j")
        assert self.machine.state is TreasureState.DEATH_CLOSE_TASK_PANEL

        self.stable("world_hud_anchor", "death_return_scene_anchor", inventory=inventory)
        return self.machine.state

    @staticmethod
    def assert_intent(intent, expected_kind: str, key: str | None = None):
        assert intent is not None
        assert kind(intent) == expected_kind
        if key is not None:
            assert intent.key == key


class TreasureMapStateMachineTests(unittest.TestCase):
    def make_driver(self, max_maps=1, **policy):
        machine = TreasureMapStateMachine(
            max_maps=max_maps,
            policy=quick_policy(**policy),
            clock=lambda: 0.0,
        )
        return Driver(machine)

    def test_happy_path_counts_confirmed_consumption_and_success(self):
        d = self.make_driver(max_maps=1)
        d.drive_to_classify()

        intent = d.stable("world_hud_anchor", "reward_dialog", "reward_confirm_button",
                          inventory="stack:1")
        self.assertEqual(kind(intent), "click")
        self.assertEqual(intent.target, POINTS["reward_confirm_button"])

        d.tick("world_hud_anchor", inventory="stack:1", step=0.2)
        d.tick("world_hud_anchor", inventory="stack:1", step=0.2)
        intent = d.tick("world_hud_anchor", inventory="stack:1", step=0.2)
        self.assertEqual(kind(intent), "stop")
        self.assertEqual(d.machine.state, TreasureState.COMPLETED)
        self.assertEqual(d.machine.processed_maps, 1)
        self.assertEqual(d.machine.success_count, 1)
        self.assertEqual(d.machine.death_count, 0)

    def test_success_signal_does_not_skip_visible_reward_modal(self):
        d = self.make_driver(max_maps=1)
        d.drive_to_classify()

        intent = d.stable(
            "world_hud_anchor", "dig_success", "reward_dialog", "reward_confirm_button",
            inventory="stack:1",
        )

        self.assertEqual(kind(intent), "click")
        self.assertEqual(intent.target, POINTS["reward_confirm_button"])
        self.assertEqual(d.machine.state, TreasureState.CLASSIFY)
        self.assertEqual(d.machine.success_count, 0)

    def test_map_is_not_counted_before_multi_frame_commit_evidence(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_verify()
        self.assertEqual(d.machine.processed_maps, 0)
        d.tick("world_hud_anchor", "map_consumed", inventory="stack:1")
        d.tick("world_hud_anchor", inventory="stack:2")
        self.assertEqual(d.machine.processed_maps, 0)
        d.stable("world_hud_anchor", "map_consumed", inventory="stack:1")
        self.assertEqual(d.machine.processed_maps, 1)
        self.assertTrue(d.machine.map_committed)

    def test_unconfirmed_consumption_is_probed_then_stops_without_second_use_click(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_verify()
        use_clicks = [i for i in d.intents if kind(i) == "click"]
        self.assertEqual(len(use_clicks), 1)

        d.t += d.machine.policy.verification_probe_seconds
        probe = d.tick("world_hud_anchor", inventory="stack:2")
        self.assertEqual(kind(probe), "key")
        self.assertEqual(probe.key, "m")

        d.t += d.machine.policy.verify_committed_timeout
        diagnostic = d.tick("world_hud_anchor", inventory="stack:2")
        self.assertEqual(kind(diagnostic), "diagnostic")
        self.assertEqual(d.machine.state, TreasureState.ERROR_STOP)
        use_clicks = [i for i in d.intents if kind(i) == "click"]
        self.assertEqual(len(use_clicks), 1, "an unverified map must never be clicked twice")

    def test_single_frame_critical_false_positive_suppresses_but_does_not_transition(self):
        d = self.make_driver()
        d.tick("world_hud_anchor")
        d.tick("world_hud_anchor")
        self.assertIsNone(d.tick("world_hud_anchor", "death_return_scene_anchor"))
        self.assertEqual(d.machine.state, TreasureState.READY)
        intent = d.tick("world_hud_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")
        self.assertEqual(d.machine.state, TreasureState.OPEN_BACKPACK)

    def test_combat_is_observation_only_and_never_emits_f9_or_combat_input(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_classify()
        for _ in range(3):
            self.assertIsNone(d.tick("combat_hud_anchor"))
        self.assertEqual(d.machine.state, TreasureState.COMBAT_WAIT)

        for _ in range(12):
            self.assertIsNone(d.tick("combat_hud_anchor", step=0.25))
        for intent in d.intents:
            self.assertNotEqual(getattr(intent, "key", None), "f9")
        emitted_during_combat = [
            action for action in d.intents
            if getattr(action, "metadata", {}).get("state") == TreasureState.COMBAT_WAIT.value
        ]
        self.assertEqual(emitted_during_combat, [])

        d.stable("world_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.READY)
        self.assertEqual(d.machine.processed_maps, 1)
        self.assertEqual(d.machine.success_count, 1)

    def test_death_after_dig_counts_processed_but_not_success_and_continues(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_classify()
        d.stable("combat_hud_anchor")
        self.assertEqual(d.machine.checkpoint, TreasureState.CLASSIFY)

        # Death during combat must preserve CLASSIFY rather than COMBAT_WAIT.
        d.stable("world_hud_anchor", "death_return_scene_anchor")
        self.assertIn(
            d.machine.state,
            {TreasureState.DEATH_SCENE_WAIT, TreasureState.DEATH_RECONCILE},
        )
        self.assertEqual(d.machine.checkpoint, TreasureState.CLASSIFY)
        intent = d.tick("world_hud_anchor", "death_return_scene_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")
        self.assertNotEqual(intent.key, "f9")

        state = d.finish_death_reconciliation(active=False)
        self.assertEqual(state, TreasureState.READY)
        self.assertEqual(d.machine.state, TreasureState.READY)
        self.assertEqual(d.machine.processed_maps, 1)
        self.assertEqual(d.machine.success_count, 0)
        self.assertEqual(d.machine.death_count, 1)
        self.assertFalse(d.machine.map_committed)
        # The fixed return-scene anchor may remain visible until navigation.
        # It belongs to the handled death and must not trigger an endless loop.
        intent = d.tick("world_hud_anchor", "death_return_scene_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")
        self.assertEqual(d.machine.state, TreasureState.OPEN_BACKPACK)

    def test_death_during_navigation_resumes_only_after_active_task_evidence(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_verify()
        d.stable("world_hud_anchor", "map_consumed", inventory="stack:1")
        d.machine.checkpoint = TreasureState.NAVIGATE
        d.machine._enter(TreasureState.NAVIGATE, d.t)  # simulate a recorded replay checkpoint

        d.stable("world_hud_anchor", "death_return_scene_anchor")
        intent = d.tick("world_hud_anchor", "death_return_scene_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")
        state = d.finish_death_reconciliation(active=True)
        self.assertEqual(state, TreasureState.OPEN_MAP)
        self.assertEqual(d.machine.state, TreasureState.OPEN_MAP)
        self.assertEqual(d.machine.death_count, 0)
        self.assertTrue(d.machine.map_committed)

    def test_explicit_active_task_overrides_post_dig_checkpoint(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_classify()
        d.stable("combat_hud_anchor")
        d.stable("world_hud_anchor", "death_return_scene_anchor")
        d.tick("world_hud_anchor", "death_return_scene_anchor")
        state = d.finish_death_reconciliation(active=True)
        self.assertEqual(state, TreasureState.OPEN_MAP)
        self.assertEqual(d.machine.state, TreasureState.OPEN_MAP)
        self.assertEqual(d.machine.death_count, 0)
        self.assertTrue(d.machine.map_committed)

    def test_accepted_but_unconfirmed_map_is_not_blindly_reused_after_death(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_verify()
        d.stable("world_hud_anchor", "death_return_scene_anchor", inventory="stack:2")
        intent = d.tick("world_hud_anchor", "death_return_scene_anchor", inventory="stack:2")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")

        d.stable("world_hud_anchor", "death_return_scene_anchor", "backpack_open",
                 "treasure_map_icon", inventory="stack:2")
        d.stable("world_hud_anchor", "death_return_scene_anchor", inventory="stack:2")
        d.tick("world_hud_anchor", "death_return_scene_anchor", inventory="stack:2")
        d.stable(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            "no_active_treasure_task", inventory="stack:2",
        )
        intent = d.tick(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            "no_active_treasure_task", inventory="stack:2",
        )
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(intent.metadata["reason"], "death_consumption_status_ambiguous")
        self.assertEqual(d.machine.processed_maps, 0)

    def test_map_not_yet_sent_is_reused_after_inactive_task_evidence(self):
        d = self.make_driver(max_maps=2)
        d.stable("world_hud_anchor")
        d.stable("world_hud_anchor", "backpack_open", "treasure_map_icon",
                 inventory="stack:2")
        self.assertEqual(d.machine.state, TreasureState.SELECT_MAP)
        self.assertFalse(d.machine._map_use_accepted)

        d.stable(
            "world_hud_anchor", "death_return_scene_anchor", "backpack_open",
            "treasure_map_icon", inventory="stack:2",
        )
        self.assertEqual(d.machine.state, TreasureState.DEATH_SCENE_WAIT)
        d.tick(
            "world_hud_anchor", "death_return_scene_anchor", "backpack_open",
            "treasure_map_icon", inventory="stack:2",
        )
        state = d.finish_death_reconciliation(active=False, inventory="stack:2")
        self.assertEqual(state, TreasureState.OPEN_BACKPACK)
        d.stable("world_hud_anchor", "death_return_scene_anchor", "backpack_open",
                 "treasure_map_icon", inventory="stack:2")
        intent = d.tick("world_hud_anchor", "death_return_scene_anchor", "backpack_open",
                        "treasure_map_icon", inventory="stack:2")
        self.assertEqual(kind(intent), "click")
        self.assertEqual(intent.target, POINTS["treasure_map_icon"])
        self.assertEqual(d.machine.state, TreasureState.VERIFY_COMMITTED)
        self.assertEqual(d.machine.processed_maps, 0)
        self.assertEqual(d.machine.death_count, 0)

    def test_death_timeout_stops_with_diagnostic_and_no_unknown_click(self):
        d = self.make_driver(max_maps=2)
        d.stable("death_return_scene_anchor")
        self.assertEqual(d.machine.state, TreasureState.DEATH_SCENE_WAIT)
        d.t += 60.0
        intent = d.tick("death_return_scene_anchor")
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(d.machine.state, TreasureState.ERROR_STOP)
        self.assertTrue(d.machine.stopped)
        self.assertIsNone(intent.target)
        self.assertTrue(intent.metadata["capture"])
        self.assertTrue(intent.metadata["stop"])
        self.assertEqual(intent.metadata["reason"], "death_scene_timeout")

    def test_death_timeout_still_runs_while_combat_anchor_lingers(self):
        d = self.make_driver(max_maps=2)
        d.stable("death_return_scene_anchor", "combat_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.DEATH_SCENE_WAIT)
        d.t += d.machine.policy.death_timeout
        intent = d.tick("death_return_scene_anchor", "combat_hud_anchor")
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(intent.metadata["reason"], "death_scene_timeout")

    def test_death_reconciliation_never_infers_missing_task_status(self):
        d = self.make_driver(max_maps=2)
        d.drive_to_classify()
        d.stable("world_hud_anchor", "death_return_scene_anchor")
        d.tick("world_hud_anchor", "death_return_scene_anchor")
        d.stable(
            "world_hud_anchor", "death_return_scene_anchor", "backpack_open",
            "treasure_map_icon", inventory="stack:1",
        )
        d.stable("world_hud_anchor", "death_return_scene_anchor", inventory="stack:1")
        d.tick("world_hud_anchor", "death_return_scene_anchor", inventory="stack:1")
        d.stable(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            inventory="stack:1",
        )
        self.assertEqual(d.machine.state, TreasureState.DEATH_TASK_CHECK)
        d.t += d.machine.policy.reconcile_timeout
        intent = d.tick(
            "world_hud_anchor", "death_return_scene_anchor", "task_panel_anchor",
            inventory="stack:1",
        )
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(intent.metadata["reason"], "death_task_status_timeout")

    def test_conflicting_task_status_stops_safely(self):
        d = self.make_driver(max_maps=2)
        d.machine.map_committed = True
        d.machine.processed_maps = 1
        d.machine._enter(TreasureState.DEATH_TASK_CHECK, d.t)
        intent = d.stable(
            "task_panel_anchor", "active_treasure_task", "no_active_treasure_task"
        )
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(intent.metadata["reason"], "death_task_status_conflict")

    def test_new_death_requires_return_scene_anchor_to_clear_between_epochs(self):
        d = self.make_driver(max_maps=3)
        d.machine._death_latched = True
        d.machine.map_committed = True
        d.machine.processed_maps = 1
        d.stable("combat_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.COMBAT_WAIT)
        d.stable("world_hud_anchor", "death_return_scene_anchor")
        self.assertIn(
            d.machine.state,
            {TreasureState.DEATH_SCENE_WAIT, TreasureState.DEATH_RECONCILE},
        )

    def test_persistent_unclickable_reward_modal_times_out(self):
        d = self.make_driver(max_maps=1)
        d.drive_to_classify()
        d.stable("world_hud_anchor", "reward_dialog", inventory="stack:1")
        d.t += d.machine.policy.classify_timeout
        intent = d.tick("world_hud_anchor", "reward_dialog", inventory="stack:1")
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(intent.metadata["reason"], "dig_result_timeout")

    def test_reward_button_jitter_does_not_reset_retry_budget(self):
        d = self.make_driver(max_maps=1)
        d.drive_to_classify()
        for _ in range(2):
            d.tick("world_hud_anchor", "reward_dialog", "reward_confirm_button")
        last = None
        for attempt in range(d.machine.policy.max_retries + 1):
            point = (640 + attempt * 3, 520)
            obs = TreasureObservation(
                timestamp=d.t,
                detections={
                    "world_hud_anchor": detection("world_hud_anchor"),
                    "reward_dialog": detection("reward_dialog"),
                    "reward_confirm_button": {"confidence": 0.99, "center": point},
                },
            )
            last = d.machine.tick(obs)
            if last is not None:
                d.intents.append(last)
                d.machine.acknowledge_action(last, accepted=True, timestamp=d.t)
            d.t += d.machine.policy.action_retry_seconds
        self.assertEqual(kind(last), "diagnostic")
        reward_clicks = [
            intent for intent in d.intents
            if getattr(intent, "metadata", {}).get("action_id") == "confirm_treasure_reward"
            and kind(intent) == "click"
        ]
        self.assertEqual(len(reward_clicks), d.machine.policy.max_retries)

    def test_action_rejection_prevents_classify_transition(self):
        d = self.make_driver(max_maps=1)
        d.machine._enter(TreasureState.DIG, d.t)
        intent = None
        for _ in range(d.machine.policy.stable_frames):
            intent = d.machine.tick(observation(d.t, "dig_interact_prompt"))
            d.t += 0.1
        self.assertEqual(kind(intent), "interact")
        self.assertEqual(d.machine.state, TreasureState.DIG_ACTION_PENDING)
        d.machine.acknowledge_action(
            intent, accepted=False, timestamp=d.t, reason="backend_error"
        )
        self.assertEqual(d.machine.state, TreasureState.ERROR_STOP)

    def test_external_pause_freezes_state_timeout_clock(self):
        d = self.make_driver(max_maps=1, open_backpack_timeout=1.0)
        d.stable("world_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.OPEN_BACKPACK)
        d.machine.suspend(d.t)
        d.t += 30.0
        d.machine.resume_suspended(d.t)
        d.stable("world_hud_anchor", "backpack_open", "treasure_map_icon")
        self.assertEqual(d.machine.state, TreasureState.SELECT_MAP)
        self.assertIsNone(d.machine.last_error)

    def test_unconfirmed_captcha_suppresses_death_recovery_input(self):
        d = self.make_driver(max_maps=2)
        d.stable("world_hud_anchor", "death_return_scene_anchor")
        self.assertEqual(d.machine.state, TreasureState.DEATH_SCENE_WAIT)
        # Even before captcha reaches the transition threshold, no backpack
        # key may be emitted under the overlay.
        self.assertIsNone(d.tick("world_hud_anchor", "death_return_scene_anchor",
                                 "captcha_dialog"))
        self.assertEqual(d.machine.state, TreasureState.DEATH_SCENE_WAIT)

    def test_captcha_requires_human_resume_and_disconnect_preempts_reward(self):
        d = self.make_driver(max_maps=2)
        d.tick("world_hud_anchor")
        d.tick("world_hud_anchor")
        d.stable("world_hud_anchor", "captcha_dialog")
        self.assertEqual(d.machine.state, TreasureState.CAPTCHA_PAUSE)
        self.assertFalse(d.machine.running)

        d.stable("world_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.CAPTCHA_PAUSE)
        d.machine.resume()
        d.tick("world_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.READY)

        # A stable disconnect wins even if a clickable reward is present.
        d.machine.map_committed = True
        d.machine._enter(TreasureState.CLASSIFY, d.t)
        d.stable("disconnect_dialog", "reward_dialog", "reward_confirm_button")
        self.assertEqual(d.machine.state, TreasureState.DISCONNECT_STOP)
        self.assertEqual(kind(d.intents[-1]), "stop")
        self.assertIsNone(d.intents[-1].target)

    def test_captcha_resume_preserves_preexisting_reward_context(self):
        d = self.make_driver(max_maps=1)
        d.drive_to_classify()
        d.stable("world_hud_anchor", "reward_dialog", inventory="stack:1")
        self.assertTrue(d.machine._reward_seen)
        d.stable("world_hud_anchor", "captcha_dialog", inventory="stack:1")
        self.assertEqual(d.machine.state, TreasureState.CAPTCHA_PAUSE)
        d.machine.resume()
        d.stable("world_hud_anchor", inventory="stack:1")
        self.assertEqual(d.machine.state, TreasureState.CLASSIFY)
        intent = d.tick("world_hud_anchor", inventory="stack:1", step=0.6)
        if intent is None:
            intent = d.tick("world_hud_anchor", inventory="stack:1", step=0.6)
        self.assertEqual(kind(intent), "stop")
        self.assertEqual(d.machine.success_count, 1)

    def test_retries_are_finite_and_end_in_diagnostic_state(self):
        d = self.make_driver(open_backpack_timeout=20.0)
        d.stable("world_hud_anchor")
        self.assertEqual(d.machine.state, TreasureState.OPEN_BACKPACK)
        # Attempts at entry, +1s, +2s; the next due tick stops.
        for _ in range(3):
            d.t += 1.0
            intent = d.tick("world_hud_anchor")
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(d.machine.state, TreasureState.ERROR_STOP)
        keys = [i.key for i in d.intents if kind(i) == "key"]
        self.assertEqual(keys, ["i", "i", "i"])

    def test_mapping_observation_and_reported_consecutive_are_supported(self):
        machine = TreasureMapStateMachine(max_maps=1, policy=quick_policy(), clock=lambda: 0.0)
        payload = {
            "timestamp": 0.0,
            "detections": {
                "world_hud_anchor": {"confidence": 0.9, "consecutive": 3},
            },
        }
        self.assertIsNone(machine.tick(payload))
        payload["timestamp"] = 0.1
        self.assertIsNone(machine.tick(payload))
        payload["timestamp"] = 0.2
        intent = machine.tick(payload)
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")

    def test_first_replay_timestamp_rebases_timeout_clock(self):
        machine = TreasureMapStateMachine(
            max_maps=1,
            policy=quick_policy(ready_timeout=5.0),
            clock=lambda: 1_000_000.0,
        )
        # A replay beginning at zero must not instantly time out merely because
        # the constructor used a wall-clock timestamp.
        self.assertIsNone(machine.tick(observation(0.0)))
        self.assertEqual(machine.state, TreasureState.READY)
        intent = machine.tick(observation(5.1))
        self.assertEqual(kind(intent), "diagnostic")
        self.assertEqual(machine.last_error, "world_or_ui_not_ready")

    def test_ready_closes_stably_known_task_panel_before_opening_backpack(self):
        d = self.make_driver(max_maps=1)
        intent = d.stable("world_hud_anchor", "task_panel_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "j")
        self.assertEqual(intent.postcondition, "task_panel_closed")
        self.assertEqual(d.machine.state, TreasureState.READY)
        intent = d.stable("world_hud_anchor")
        self.assertEqual(kind(intent), "key")
        self.assertEqual(intent.key, "i")

    def test_legacy_blocking_run_is_explicitly_disabled(self):
        handler = TreasureMapHandler()
        with self.assertRaisesRegex(RuntimeError, "tick"):
            handler.run(max_maps=1)

    def test_legacy_combat_handler_is_passive_and_non_blocking(self):
        class Window:
            in_combat = False

        class Windows:
            windows = [Window()]

        class Screen:
            active = True

            def find_template(self, name, window_index=0):
                return (10, 10) if self.active and name == "combat_enemy_area" else None

        class RejectInput:
            def __getattr__(self, name):
                raise AssertionError(f"combat attempted input method {name}")

        screen = Screen()
        handler = CombatHandler(Windows(), RejectInput(), screen)
        self.assertFalse(handler.fight(handler.wg))
        self.assertTrue(handler.wg.windows[0].in_combat)
        screen.active = False
        self.assertTrue(handler.fight(handler.wg))
        self.assertFalse(handler.wg.windows[0].in_combat)
        with self.assertRaises(RuntimeError):
            handler.use_skill_on_account(0, 1)
        with self.assertRaises(RuntimeError):
            handler.use_potion(0)
        with self.assertRaises(RuntimeError):
            handler.switch_target()

    def test_policy_rejects_single_frame_confirmation(self):
        with self.assertRaises(ValueError):
            TreasureMapPolicy(stable_frames=1)


if __name__ == "__main__":
    unittest.main()
