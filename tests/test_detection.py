from __future__ import annotations

import cv2
import numpy as np

from core.detection import TemplateDetector, TemplateManifest
from core.frame_source import CapturedFrame
from core.screen import ScreenManager


def _pattern(height: int = 9, width: int = 13) -> np.ndarray:
    rng = np.random.default_rng(814)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def test_manifest_accepts_runtime_aliases_and_normalized_coordinate_space():
    manifest = TemplateManifest.from_dict(
        {
            "coordinate_space": "normalized_client",
            "templates": {
                "anchor": {
                    "file": "anchor.png",
                    "roi": [0.25, 0.1, 0.5, 0.8],
                    "stable_frames": 3,
                }
            },
        }
    )

    rule = manifest.get("anchor")
    assert rule.path == "anchor.png"
    assert rule.roi_mode == "normalized"
    assert rule.consecutive == 3
    assert manifest.validate(required=["anchor"]) == []


def test_detector_returns_template_center_with_unscaled_roi_offset():
    template = _pattern()
    screen = np.zeros((100, 140, 3), dtype=np.uint8)
    x, y = 83, 37
    height, width = template.shape[:2]
    screen[y : y + height, x : x + width] = template
    manifest = TemplateManifest.from_dict(
        {
            "coordinate_space": "normalized_client",
            "templates": {
                "anchor": {
                    "file": "ignored.png",
                    "threshold": 0.99,
                    "roi": [0.4, 0.2, 0.55, 0.7],
                    "scales": [1.0],
                    "stable_frames": 1,
                }
            },
        }
    )
    detector = TemplateDetector(manifest, templates={"anchor": template})

    result = detector.detect(CapturedFrame(screen, timestamp=123.25), "anchor")

    assert result is not None
    assert result.box == (x, y, width, height)
    assert result.center == (x + width // 2, y + height // 2)
    assert result.roi == (56, 20, 77, 70)
    assert result.timestamp == 123.25
    assert result.confidence >= 0.99


def test_reference_size_scales_roi_and_template_in_same_coordinate_space():
    template = _pattern(8, 10)
    scaled = cv2.resize(template, (20, 16), interpolation=cv2.INTER_CUBIC)
    screen = np.zeros((200, 200, 3), dtype=np.uint8)
    x, y = 72, 86
    screen[y : y + 16, x : x + 20] = scaled
    manifest = TemplateManifest.from_dict(
        {
            "templates": {
                "anchor": {
                    "path": "ignored.png",
                    "threshold": 0.99,
                    "roi": [20, 30, 40, 40],
                    "reference_size": [100, 100],
                    "scales": [1.0],
                }
            }
        }
    )

    result = TemplateDetector(manifest, templates={"anchor": template}).detect(screen, "anchor")

    assert result is not None
    assert result.roi == (40, 60, 80, 80)
    assert result.box == (x, y, 20, 16)
    assert result.center == (82, 94)
    assert result.scale == 2.0


def test_consecutive_rule_suppresses_single_frame_and_resets_after_miss():
    template = _pattern()
    screen = np.zeros((60, 80, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    screen[20 : 20 + height, 30 : 30 + width] = template
    manifest = TemplateManifest.from_dict(
        {
            "templates": {
                "anchor": {
                    "path": "ignored.png",
                    "threshold": 0.99,
                    "consecutive": 2,
                }
            }
        }
    )
    detector = TemplateDetector(manifest, templates={"anchor": template})

    assert detector.detect(screen, "anchor") is None
    confirmed = detector.detect(screen, "anchor")
    assert confirmed is not None
    assert confirmed.consecutive == 2

    assert detector.detect(np.zeros_like(screen), "anchor") is None
    assert detector.detect(screen, "anchor") is None


def test_color_detection_explicitly_converts_rgb_and_hsv_to_bgr_capture():
    screen = np.zeros((50, 80, 3), dtype=np.uint8)
    screen[10:20, 30:50] = (0, 0, 255)  # red in OpenCV BGR

    class FakeWindowGroup:
        def capture(self, _window_index):
            return screen

    manager = ScreenManager(FakeWindowGroup())

    assert manager.find_color_in_window(0, (255, 0, 0), 0, "RGB") == (40, 15)
    assert manager.find_color_in_window(0, (0, 255, 255), 0, "HSV") == (40, 15)
