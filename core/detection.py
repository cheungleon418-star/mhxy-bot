"""Manifest-driven OpenCV template detection with temporal confirmation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Any, Iterable, Mapping, Optional, Tuple, Union

import cv2
import numpy as np

from .frame_source import CapturedFrame


Box = Tuple[int, int, int, int]


@dataclass(frozen=True)
class DetectionResult:
    name: str
    confidence: float
    center: Tuple[int, int]
    box: Box
    roi: Box
    timestamp: float
    scale: float = 1.0
    consecutive: int = 1


@dataclass(frozen=True)
class TemplateRule:
    """Detection policy for one template.

    Pixel ROIs are interpreted in ``reference_size`` coordinates when that is
    supplied.  Normalized ROIs use values from zero to one.  ``scales`` resize
    the template; with a reference size they are multipliers around the current
    frame-to-reference scale.
    """

    name: str
    path: str
    threshold: float = 0.85
    roi: Optional[Tuple[float, float, float, float]] = None
    roi_mode: str = "pixels"
    reference_size: Optional[Tuple[int, int]] = None
    scales: Tuple[float, ...] = (1.0,)
    consecutive: int = 1
    max_drift: int = 12
    calibrated: bool = False
    grayscale: bool = False

    @classmethod
    def from_dict(
        cls,
        name: str,
        value: Mapping[str, Any],
        *,
        default_roi_mode: str = "pixels",
    ) -> "TemplateRule":
        roi = value.get("roi")
        reference_size = value.get("reference_size")
        scales_value = value.get("scales", (1.0,))
        if isinstance(scales_value, (int, float)):
            scales_value = (float(scales_value),)
        return cls(
            name=name,
            path=str(value.get("path", value.get("file", f"{name}.png"))),
            threshold=float(value.get("threshold", 0.85)),
            roi=tuple(float(part) for part in roi) if roi is not None else None,
            roi_mode=str(value.get("roi_mode", default_roi_mode)),
            reference_size=(
                tuple(int(part) for part in reference_size)
                if reference_size is not None
                else None
            ),
            scales=tuple(float(part) for part in scales_value),
            consecutive=int(value.get("consecutive", value.get("stable_frames", 1))),
            max_drift=int(value.get("max_drift", 12)),
            calibrated=bool(value.get("calibrated", False)),
            grayscale=bool(value.get("grayscale", False)),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("template name is empty")
        if not self.path:
            errors.append(f"{self.name}: path is empty")
        if not 0.0 <= self.threshold <= 1.0:
            errors.append(f"{self.name}: threshold must be between 0 and 1")
        if self.roi is not None and len(self.roi) != 4:
            errors.append(f"{self.name}: roi must have four values")
        if self.roi_mode not in {"pixels", "normalized"}:
            errors.append(f"{self.name}: roi_mode must be pixels or normalized")
        if self.roi is not None and self.roi_mode == "normalized":
            x, y, width, height = self.roi
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                errors.append(f"{self.name}: normalized roi must fit within 0..1")
        if self.reference_size is not None and (
            len(self.reference_size) != 2 or min(self.reference_size) <= 0
        ):
            errors.append(f"{self.name}: reference_size must be two positive values")
        if not self.scales or any(scale <= 0 for scale in self.scales):
            errors.append(f"{self.name}: scales must contain positive values")
        if self.consecutive < 1:
            errors.append(f"{self.name}: consecutive must be at least 1")
        if self.max_drift < 0:
            errors.append(f"{self.name}: max_drift cannot be negative")
        return errors


class TemplateManifest:
    """Validated collection of named template rules."""

    def __init__(self, rules: Mapping[str, TemplateRule], base_dir: Union[str, Path] = "."):
        self.rules = dict(rules)
        self.base_dir = Path(base_dir).expanduser().resolve()

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        base_dir: Union[str, Path] = ".",
    ) -> "TemplateManifest":
        raw_templates = value.get("templates", value)
        if not isinstance(raw_templates, Mapping):
            raise ValueError("Template manifest 'templates' must be an object")
        coordinate_space = str(value.get("coordinate_space", "pixels"))
        default_roi_mode = "normalized" if coordinate_space == "normalized_client" else "pixels"
        rules: dict[str, TemplateRule] = {}
        for name, raw_rule in raw_templates.items():
            if not isinstance(raw_rule, Mapping):
                raise ValueError(f"Template rule must be an object: {name}")
            rules[str(name)] = TemplateRule.from_dict(
                str(name), raw_rule, default_roi_mode=default_roi_mode
            )
        manifest = cls(rules, base_dir=base_dir)
        errors = manifest.validate()
        if errors:
            raise ValueError("Invalid template manifest: " + "; ".join(errors))
        return manifest

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "TemplateManifest":
        source = Path(path).expanduser().resolve()
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid template manifest JSON {source}: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ValueError("Template manifest root must be an object")
        return cls.from_dict(value, base_dir=source.parent)

    load = from_file

    def get(self, name: str) -> TemplateRule:
        try:
            return self.rules[name]
        except KeyError as exc:
            raise KeyError(f"Unknown template: {name}") from exc

    def template_path(self, name: str) -> Path:
        path = Path(self.get(name).path).expanduser()
        return path.resolve() if path.is_absolute() else (self.base_dir / path).resolve()

    def validate(
        self,
        required: Optional[Iterable[str]] = None,
        *,
        require_calibrated: bool = False,
        require_files: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        for rule in self.rules.values():
            errors.extend(rule.validation_errors())
            if require_calibrated and not rule.calibrated:
                errors.append(f"{rule.name}: not calibrated")
            if require_files and not self.template_path(rule.name).is_file():
                errors.append(f"{rule.name}: template file is missing")
        for name in required or ():
            if name not in self.rules:
                errors.append(f"{name}: required template is missing")
        return errors


class TemplateDetector:
    """Match templates within their configured ROI and temporal policy."""

    def __init__(
        self,
        manifest: TemplateManifest,
        *,
        templates: Optional[Mapping[str, np.ndarray]] = None,
    ) -> None:
        self.manifest = manifest
        self._templates = {name: image.copy() for name, image in (templates or {}).items()}
        self._streaks: dict[str, int] = {}
        self._last_centers: dict[str, Tuple[int, int]] = {}

    def reset(self, name: Optional[str] = None) -> None:
        if name is None:
            self._streaks.clear()
            self._last_centers.clear()
        else:
            self._streaks.pop(name, None)
            self._last_centers.pop(name, None)

    def _template(self, name: str) -> np.ndarray:
        if name not in self._templates:
            path = self.manifest.template_path(name)
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"Template image is missing or invalid: {path}")
            self._templates[name] = image
        return self._templates[name]

    @staticmethod
    def _actual_roi(rule: TemplateRule, frame_size: Tuple[int, int]) -> Box:
        frame_width, frame_height = frame_size
        if rule.roi is None:
            return 0, 0, frame_width, frame_height

        x, y, width, height = rule.roi
        if rule.roi_mode == "normalized":
            x *= frame_width
            width *= frame_width
            y *= frame_height
            height *= frame_height
        elif rule.reference_size is not None:
            ref_width, ref_height = rule.reference_size
            x *= frame_width / ref_width
            width *= frame_width / ref_width
            y *= frame_height / ref_height
            height *= frame_height / ref_height

        left = max(0, min(frame_width, int(round(x))))
        top = max(0, min(frame_height, int(round(y))))
        right = max(left, min(frame_width, int(round(x + width))))
        bottom = max(top, min(frame_height, int(round(y + height))))
        return left, top, right - left, bottom - top

    @staticmethod
    def _base_scale(rule: TemplateRule, frame_size: Tuple[int, int]) -> float:
        if rule.reference_size is None:
            return 1.0
        frame_width, frame_height = frame_size
        ref_width, ref_height = rule.reference_size
        # UI scale is expected to be uniform; the smaller ratio avoids growing
        # a template beyond letterboxed content.
        return min(frame_width / ref_width, frame_height / ref_height)

    def detect(
        self,
        frame: Union[CapturedFrame, np.ndarray],
        name: str,
        *,
        require_consecutive: bool = True,
    ) -> Optional[DetectionResult]:
        rule = self.manifest.get(name)
        if isinstance(frame, CapturedFrame):
            image = frame.image
            timestamp = frame.timestamp
        else:
            image = frame
            timestamp = time.monotonic()
        frame_height, frame_width = image.shape[:2]
        roi = self._actual_roi(rule, (frame_width, frame_height))
        roi_x, roi_y, roi_width, roi_height = roi
        if roi_width <= 0 or roi_height <= 0:
            self.reset(name)
            return None
        search = image[roi_y : roi_y + roi_height, roi_x : roi_x + roi_width]
        original_template = self._template(name)

        best: Optional[DetectionResult] = None
        base_scale = self._base_scale(rule, (frame_width, frame_height))
        for relative_scale in rule.scales:
            scale = base_scale * relative_scale
            template_width = max(1, int(round(original_template.shape[1] * scale)))
            template_height = max(1, int(round(original_template.shape[0] * scale)))
            if template_width > roi_width or template_height > roi_height:
                continue
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            template = (
                original_template
                if template_width == original_template.shape[1]
                and template_height == original_template.shape[0]
                else cv2.resize(
                    original_template,
                    (template_width, template_height),
                    interpolation=interpolation,
                )
            )
            match_search = search
            if rule.grayscale:
                match_search = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
                template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            scores = cv2.matchTemplate(match_search, template, cv2.TM_CCOEFF_NORMED)
            _, confidence, _, location = cv2.minMaxLoc(scores)
            if not math.isfinite(confidence):
                continue
            x = roi_x + int(location[0])
            y = roi_y + int(location[1])
            candidate = DetectionResult(
                name=name,
                confidence=float(confidence),
                center=(x + template_width // 2, y + template_height // 2),
                box=(x, y, template_width, template_height),
                roi=roi,
                timestamp=timestamp,
                scale=scale,
                consecutive=1,
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate

        if best is None or best.confidence < rule.threshold:
            self.reset(name)
            return None

        previous = self._last_centers.get(name)
        if previous is not None and math.dist(previous, best.center) <= rule.max_drift:
            streak = self._streaks.get(name, 0) + 1
        else:
            streak = 1
        self._streaks[name] = streak
        self._last_centers[name] = best.center
        result = DetectionResult(
            name=best.name,
            confidence=best.confidence,
            center=best.center,
            box=best.box,
            roi=best.roi,
            timestamp=best.timestamp,
            scale=best.scale,
            consecutive=streak,
        )
        if require_consecutive and streak < rule.consecutive:
            return None
        return result

    def detect_many(
        self,
        frame: Union[CapturedFrame, np.ndarray],
        names: Optional[Iterable[str]] = None,
        *,
        require_consecutive: bool = True,
    ) -> dict[str, DetectionResult]:
        found: dict[str, DetectionResult] = {}
        for name in names or self.manifest.rules:
            result = self.detect(frame, name, require_consecutive=require_consecutive)
            if result is not None:
                found[name] = result
        return found


__all__ = [
    "DetectionResult",
    "TemplateDetector",
    "TemplateManifest",
    "TemplateRule",
]
