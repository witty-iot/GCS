"""
flight/camera_feed.py
-----------------------
Local hardware controller for a persistent camera feed, following the
same pattern as SprayController in safety.py: built once in main.py from
this Jetson's own drone_config.yaml ("hardware.camera"), handed into
every action plugin's drone_context under the "camera" key, never part
of mission data.

Unlike SprayController (an idle on/off relay), this one runs a
continuous background loop for the whole flight — the camera feed is
live from shortly after takeoff until landing, not just while a
window_clean-style action is executing. A live preview window is always
showing raw frames; an action plugin can call enable_detection() to
switch that preview into "draw boxes + accumulate detections" mode for
as long as it needs, then disable_detection() to get back how many
qualifying detections were seen and return the feed to plain preview.

No camera hardware exists yet on a real Jetson — "source" in
hardware.camera is whatever OpenCV VideoCapture index/path is available
(0 = a laptop's built-in webcam for SITL/dev work). Swapping to a real
drone camera later is a one-line config change, not a code change.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 0.2


class CameraFeed:
    def __init__(self, source: int = 0, window_title: str = "Drone Camera Feed") -> None:
        self.source = source
        self._window_title = window_title
        self._cap = None
        self._task: Optional[asyncio.Task] = None

        # Detection-mode state, toggled by enable_detection()/disable_detection()
        self._detecting = False
        self._model = None
        self._model_path: Optional[str] = None
        self._trigger_classes: List[str] = []
        self._confidence_threshold: float = 0.4
        self._max_qualifying = 0

    def start(self, drone_id: str) -> None:
        """
        Open the camera and start the continuous preview loop. Safe to
        call even if no camera is available — logs a warning and
        no-ops, same graceful-degradation pattern as SprayController
        when GPIO isn't present.
        """
        try:
            import cv2
        except ImportError:
            log.warning("[%s] CameraFeed: opencv-python not installed, feed disabled.", drone_id)
            return

        self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            log.warning(
                "[%s] CameraFeed: could not open camera source %s, feed disabled.",
                drone_id, self.source,
            )
            self._cap = None
            return

        log.info("[%s] CameraFeed: live preview started (source=%s).", drone_id, self.source)
        self._task = asyncio.create_task(self._loop(drone_id), name=f"camera_feed_{drone_id}")

    async def _loop(self, drone_id: str) -> None:
        import cv2

        try:
            while True:
                ok, frame = self._cap.read()
                if not ok:
                    await asyncio.sleep(LOOP_INTERVAL_SEC)
                    continue

                if self._detecting and self._model is not None:
                    results = self._model.predict(frame, conf=self._confidence_threshold, verbose=False)[0]
                    names = results.names
                    qualifying = sum(
                        1
                        for cls_id, conf in zip(results.boxes.cls.tolist(), results.boxes.conf.tolist())
                        if names[int(cls_id)] in self._trigger_classes and conf >= self._confidence_threshold
                    )
                    self._max_qualifying = max(self._max_qualifying, qualifying)
                    frame = results.plot()

                cv2.imshow(self._window_title, frame)
                cv2.waitKey(1)
                await asyncio.sleep(LOOP_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass

    def enable_detection(self, model_path: str, trigger_classes: List[str], confidence_threshold: float) -> None:
        """
        Switch the live feed into detection mode: draws boxes on every
        frame and accumulates the running max count of qualifying
        detections, until disable_detection() is called.
        """
        if self._cap is None:
            return  # no camera available; window_clean plugin handles logging this

        if self._model is None or self._model_path != model_path:
            from ultralytics import YOLO
            self._model = YOLO(model_path)
            self._model_path = model_path
            log.info("CameraFeed: loaded model from %s", model_path)

        self._trigger_classes = trigger_classes
        self._confidence_threshold = confidence_threshold
        self._max_qualifying = 0
        self._detecting = True

    def disable_detection(self) -> int:
        """Turn detection mode off, return the max qualifying-detection count observed."""
        self._detecting = False
        return self._max_qualifying

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._cap is not None:
            self._cap.release()
        try:
            import cv2
            cv2.destroyWindow(self._window_title)
        except Exception:
            pass
