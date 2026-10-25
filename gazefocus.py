"""
GazeFocus — head-pose driven focus switcher.

Look at a monitor and start typing: focus is already there. No clicking.

Windows: full focus switching via Win32.
macOS/Linux: head tracking works; focus switching is Windows-only for now.

Usage:
    python gazefocus.py                # calibrate if needed, then run (preview window)
    python gazefocus.py --calibrate    # recalibrate
    python gazefocus.py --headless     # run in background, no preview window

Hotkeys:
    Ctrl+Alt+G  pause/resume
    Ctrl+Alt+Q  quit
    q           quit (preview window)
    Ctrl+C      quit (console)

License: MIT
"""

import argparse
import cv2
import mediapipe as mp
import numpy as np
import json
import os
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

try:
    import win32gui, win32con, win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

from screeninfo import get_monitors
from pynput import keyboard, mouse


# ============================================================
# Config
# ============================================================
@dataclass
class Config:
    # Focus (conservative)
    debounce: float = 0.15
    typing_block: float = 0.30
    mouse_block: float = 0.35
    switch_cooldown: float = 0.25

    # Busy hysteresis (enter fast, leave slow)
    busy_release: float = 1.2

    # Matching
    yaw_pitch_threshold: float = 22.0
    ambiguous_margin: float = 0.5
    stay_multiplier: float = 1.6

    # Cursor (moves once per monitor change, yields to keyboard AND mouse)
    cursor_debounce: float = 0.08
    cursor_enabled: bool = True
    move_pointer: bool = True

    # One Euro filter
    oe_min_cutoff: float = 1.2
    oe_beta: float = 0.04
    oe_d_cutoff: float = 1.0

    # Anti-interference
    outlier_jump_deg: float = 25.0
    none_grace: float = 0.20
    max_no_face_frames: int = 15

    camera_index: int = 0
    calibration_file: str = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "calibration.json"
    )

    headless: bool = False


CFG = Config()


# ============================================================
# Head pose
# ============================================================
MODEL_POINTS = np.array([
    (0.0,    0.0,    0.0),
    (0.0,  -63.6,  -12.5),
    (-43.3, 32.7,  -26.0),
    (43.3,  32.7,  -26.0),
    (-28.9, -28.9, -24.1),
    (28.9,  -28.9, -24.1),
], dtype=np.float64)
MP_INDICES = [1, 152, 33, 263, 61, 291]


def head_pose_yaw_pitch(face_landmarks, w: int, h: int) -> Optional[Tuple[float, float]]:
    """Return (pitch, yaw) in degrees with pitch normalized to [-90, 90]."""
    try:
        image_points = np.array(
            [(face_landmarks[i].x * w, face_landmarks[i].y * h) for i in MP_INDICES],
            dtype=np.float64,
        )
        f = float(w)
        cam = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], dtype=np.float64)
        dist = np.zeros((4, 1))

        best = None
        for flag in (cv2.SOLVEPNP_ITERATIVE, cv2.SOLVEPNP_EPNP):
            ok, rvec, _ = cv2.solvePnP(
                MODEL_POINTS, image_points, cam, dist, flags=flag
            )
            if not ok:
                continue
            rmat, _ = cv2.Rodrigues(rvec)
            sy = np.sqrt(rmat[0, 0] ** 2 + rmat[1, 0] ** 2)
            if sy < 1e-6:
                continue
            pitch = float(np.degrees(np.arctan2(-rmat[1, 2], rmat[1, 1])))
            yaw = float(np.degrees(np.arctan2(-rmat[2, 0], sy)))

            if pitch > 90:
                pitch = 180 - pitch
            elif pitch < -90:
                pitch = -180 - pitch

            score = abs(pitch) + abs(yaw)
            if best is None or score < best[0]:
                best = (score, pitch, yaw)

        if best is None:
            return None
        return best[1], best[2]
    except Exception:
        return None


# ============================================================
# One Euro Filter
# ============================================================
class LowPass:
    def __init__(self, alpha: float):
        self.alpha = alpha
        self.y: Optional[float] = None

    def filter(self, x: float) -> float:
        if self.y is None:
            self.y = x
        else:
            self.y = self.alpha * x + (1 - self.alpha) * self.y
        return self.y


class OneEuroFilter:
    def __init__(self, min_cutoff=1.2, beta=0.04, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x_filt = LowPass(1.0)
        self.dx_filt = LowPass(1.0)
        self.last_x: Optional[float] = None
        self.last_t: Optional[float] = None

    @staticmethod
    def alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * np.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float) -> float:
        dt = (1.0 / 30.0) if self.last_t is None else max(t - self.last_t, 1e-4)
        dx = 0.0 if self.last_x is None else (x - self.last_x) / dt

        self.dx_filt.alpha = self.alpha(self.d_cutoff, dt)
        dx_hat = self.dx_filt.filter(dx)

        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        self.x_filt.alpha = self.alpha(cutoff, dt)
        x_hat = self.x_filt.filter(x)

        self.last_x = x
        self.last_t = t
        return x_hat

    def reset(self):
        self.x_filt.y = None
        self.dx_filt.y = None
        self.last_x = None
        self.last_t = None


# ============================================================
# Camera / MediaPipe
# ============================================================
def open_camera(idx: int) -> cv2.VideoCapture:
    if sys.platform == "win32":
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(idx)
    else:
        cap = cv2.VideoCapture(idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)
    return cap


def make_face_mesh():
    return mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1,
        refine_landmarks=False,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.6,
    )


# ============================================================
# Calibration
# ============================================================
def do_calibration(cfg: Config) -> Optional[dict]:
    monitors = get_monitors()
    print("Detected monitors:")
    for i, m in enumerate(monitors):
        print(f"  [{i}] {(m.name or 'monitor')}  {m.width}x{m.height} @ ({m.x},{m.y})")
    print("\nFace the CENTER of a monitor (turn your head, not just your eyes)")
    print("and press its number. Wait for spread < 2.0 before pressing.")
    print("'s' saves and exits, 'q' cancels.\n")

    fm = make_face_mesh()
    cap = open_camera(cfg.camera_index)
    calib: Dict[int, Dict[str, float]] = {}
    recent: deque = deque(maxlen=45)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            res = fm.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            if res.multi_face_landmarks:
                pose = head_pose_yaw_pitch(res.multi_face_landmarks[0].landmark, w, h)
                if pose is not None:
                    recent.append(pose)
                    pitch, yaw = pose
                    cv2.putText(frame, f"yaw {yaw:+.1f}  pitch {pitch:+.1f}",
                                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (0, 255, 0), 2)
                    if len(recent) >= 15:
                        tail = list(recent)[-15:]
                        sy = float(np.std([t[1] for t in tail]))
                        sp = float(np.std([t[0] for t in tail]))
                        color = (0, 255, 0) if max(sy, sp) < 2.0 else (0, 165, 255)
                        cv2.putText(frame, f"spread y={sy:.1f} p={sp:.1f}",
                                    (10, 60), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.6, color, 2)

            cv2.putText(frame, "1..N=monitor  s=save  q=quit",
                        (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (200, 200, 200), 1)
            cv2.imshow("GazeFocus Calibration", frame)

            k = cv2.waitKey(1) & 0xFF
            if k == ord('q'):
                return None
            if k == ord('s'):
                break
            if ord('1') <= k <= ord('9'):
                idx = k - ord('1')
                if idx >= len(monitors):
                    print(f"Monitor {idx} does not exist"); continue
                tail = list(recent)[-15:]
                if len(tail) < 10:
                    print("Too few samples. Move closer and wait."); continue
                sy = float(np.std([t[1] for t in tail]))
                sp = float(np.std([t[0] for t in tail]))
                if max(sy, sp) > 3.0:
                    print(f"Unstable pose (spread y={sy:.1f} p={sp:.1f}). "
                          f"Improve lighting or wait.")
                    continue
                y = float(np.median([t[1] for t in tail]))
                p = float(np.median([t[0] for t in tail]))
                calib[idx] = {"yaw": y, "pitch": p}
                print(f"Monitor {idx}: yaw {y:+.1f}  pitch {p:+.1f}")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        fm.close()

    if not calib:
        return None

    out = {"monitors": [
        {"index": i, "yaw": calib[i]["yaw"], "pitch": calib[i]["pitch"],
         "name": (monitors[i].name if i < len(monitors) else f"monitor{i}")}
        for i in sorted(calib)
    ]}
    with open(cfg.calibration_file, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved to {cfg.calibration_file}")
    return out


# ============================================================
# Focus manager (Windows)
# ============================================================
class FocusManager:
    def __init__(self, monitors):
        self.monitors = monitors
        self.last_window_per_monitor: Dict[int, int] = {}
        self.last_active_hwnd = None
        self._stop = False
        if HAS_WIN32:
            threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self._stop = True

    def monitor_of_hwnd(self, hwnd: int) -> Optional[int]:
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return None
        for i, m in enumerate(self.monitors):
            if m.x <= l < m.x + m.width and m.y <= t < m.y + m.height:
                return i
        return None

    def foreground_monitor(self) -> Optional[int]:
        if not HAS_WIN32:
            return None
        return self.monitor_of_hwnd(win32gui.GetForegroundWindow())

    def _poll(self):
        while not self._stop:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and hwnd != self.last_active_hwnd:
                    self.last_active_hwnd = hwnd
                    mi = self.monitor_of_hwnd(hwnd)
                    if mi is not None:
                        self.last_window_per_monitor[mi] = hwnd
            except Exception:
                pass
            time.sleep(0.08)

    def focus_monitor(self, mi: int, fallback_click: bool = False) -> bool:
        if not HAS_WIN32:
            return False
        hwnd = self.last_window_per_monitor.get(mi)
        if hwnd and win32gui.IsWindow(hwnd):
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32gui.SetForegroundWindow(hwnd)
                return True
            except Exception:
                pass
        if fallback_click and mi < len(self.monitors):
            m = self.monitors[mi]
            cx, cy = m.x + m.width // 2, m.y + m.height // 2
            old = win32api.GetCursorPos()
            win32api.SetCursorPos((cx, cy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            win32api.SetCursorPos(old)
            return True
        return False


# ============================================================
# Matching with hysteresis
# ============================================================
def match_target(yaw: float, pitch: float, cms: List[dict],
                 cfg: Config, current: Optional[int] = None) -> Optional[int]:
    if not cms:
        return None

    dists = sorted(
        (float(np.hypot(yaw - c["yaw"], pitch - c["pitch"])), c["index"])
        for c in cms
    )
    d_min, i_min = dists[0]

    enter_thr = cfg.yaw_pitch_threshold
    stay_thr = cfg.yaw_pitch_threshold * cfg.stay_multiplier

    if current is not None:
        cur = next((c for c in cms if c["index"] == current), None)
        if cur is not None:
            d_cur = float(np.hypot(yaw - cur["yaw"], pitch - cur["pitch"]))
            if d_cur <= stay_thr and (d_min > enter_thr or i_min == current):
                return current

    if d_min > enter_thr:
        return None
    if len(dists) > 1 and dists[1][0] - d_min < cfg.ambiguous_margin:
        return None
    return i_min


# ============================================================
# Runtime
# ============================================================
def run(cfg: Config):
    with open(cfg.calibration_file) as f:
        cms = json.load(f)["monitors"]
    if not cms:
        print("Empty calibration.")
        return
    print("Calibration loaded:")
    for c in cms:
        print(f"  monitor {c['index']}: yaw {c['yaw']:+.1f}  pitch {c['pitch']:+.1f}")
    if len(cms) >= 2:
        ys = [c["yaw"] for c in cms]
        print(f"  yaw separation: {max(ys) - min(ys):.1f}° (recommended >20°)")

    monitors = get_monitors()
    focus = FocusManager(monitors)

    # Shared activity timestamps
    last_key = [0.0]
    last_mouse = [0.0]
    paused = [False]
    should_exit = [False]

    keyboard.Listener(on_press=lambda k: last_key.__setitem__(0, time.time())).start()

    mouse.Listener(
        on_move=lambda x, y: last_mouse.__setitem__(0, time.time()),
        on_click=lambda x, y, b, p: last_mouse.__setitem__(0, time.time()) if p else None,
        on_scroll=lambda x, y, dx, dy: last_mouse.__setitem__(0, time.time()),
    ).start()

    def hotkey_listener():
        combo = set()
        if sys.platform == "win32":
            need_pause = {keyboard.Key.ctrl, keyboard.Key.alt,
                          keyboard.KeyCode.from_char('g')}
            need_quit = {keyboard.Key.ctrl, keyboard.Key.alt,
                         keyboard.KeyCode.from_char('q')}
        else:
            need_pause = {keyboard.Key.cmd, keyboard.Key.alt,
                          keyboard.KeyCode.from_char('g')}
            need_quit = {keyboard.Key.cmd, keyboard.Key.alt,
                         keyboard.KeyCode.from_char('q')}

        def on_press(k):
            combo.add(k)
            if need_pause.issubset(combo):
                paused[0] = not paused[0]
                print("PAUSED" if paused[0] else "RUNNING")
            if need_quit.issubset(combo):
                should_exit[0] = True
                print("QUIT requested")

        def on_release(k):
            combo.discard(k)

        with keyboard.Listener(on_press=on_press, on_release=on_release) as lst:
            lst.join()

    threading.Thread(target=hotkey_listener, daemon=True).start()

    fm = make_face_mesh()
    cap = open_camera(cfg.camera_index)

    f_yaw = OneEuroFilter(cfg.oe_min_cutoff, cfg.oe_beta, cfg.oe_d_cutoff)
    f_pitch = OneEuroFilter(cfg.oe_min_cutoff, cfg.oe_beta, cfg.oe_d_cutoff)

    prev_candidate: Optional[int] = None
    stable_since = time.time()
    last_switch = 0.0

    # Only act when the facing monitor changes
    acted_monitor: Optional[int] = None

    # Busy hysteresis
    busy_until = 0.0

    # Cursor state
    cursor_monitor: Optional[int] = None
    cursor_stable_since = time.time()
    cursor_moved = False

    last_good_pose: Optional[Tuple[float, float]] = None
    no_face_frames = 0
    rejected_jumps = 0

    if cfg.headless:
        print("Running headless. Ctrl+Alt+G pause/resume, Ctrl+Alt+Q quit.\n")
    else:
        print("Running. Ctrl+Alt+G pause/resume, Ctrl+Alt+Q quit, 'q' quit.\n")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            res = fm.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            now = time.time()

            if should_exit[0]:
                break

            # ---- BUSY hysteresis ----
            typing_active = (now - last_key[0]) < cfg.typing_block
            mouse_active = (now - last_mouse[0]) < cfg.mouse_block
            if typing_active or mouse_active:
                busy_until = max(busy_until, now + cfg.busy_release)
            user_busy = now < busy_until

            raw_candidate: Optional[int] = None
            status_lines = []

            if res.multi_face_landmarks:
                no_face_frames = 0
                pose = head_pose_yaw_pitch(
                    res.multi_face_landmarks[0].landmark, w, h
                )
                if pose is not None:
                    pitch_raw, yaw_raw = pose

                    if last_good_pose is not None:
                        dy = abs(yaw_raw - last_good_pose[1])
                        dp = abs(pitch_raw - last_good_pose[0])
                        if dy > cfg.outlier_jump_deg or dp > cfg.outlier_jump_deg:
                            rejected_jumps += 1
                            status_lines.append(f"OUTLIER y={dy:.1f} p={dp:.1f}")
                            pose = None

                    if pose is not None:
                        last_good_pose = (pitch_raw, yaw_raw)
                        yaw_s = f_yaw(yaw_raw, now)
                        pitch_s = f_pitch(pitch_raw, now)
                        raw_candidate = match_target(
                            yaw_s, pitch_s, cms, cfg, current=prev_candidate
                        )
                        status_lines.append(
                            f"yaw {yaw_s:+.1f}  pitch {pitch_s:+.1f} -> {raw_candidate}"
                        )
            else:
                no_face_frames += 1
                if no_face_frames >= cfg.max_no_face_frames:
                    f_yaw.reset()
                    f_pitch.reset()
                    last_good_pose = None
                    raw_candidate = None

            # Grace for None
            if raw_candidate != prev_candidate:
                if raw_candidate is not None:
                    prev_candidate = raw_candidate
                    stable_since = now
                else:
                    if now - stable_since > cfg.none_grace:
                        prev_candidate = None
                        stable_since = now

            # ---- Only act when the facing monitor CHANGES ----
            monitor_changed = (
                prev_candidate is not None
                and prev_candidate != acted_monitor
            )

            # Reset cursor intent while busy
            if user_busy:
                cursor_moved = False
                cursor_stable_since = now

            # ---- Cursor: only on monitor change, once, yields to user ----
            if (monitor_changed
                    and cfg.cursor_enabled and cfg.move_pointer
                    and HAS_WIN32 and not paused[0]
                    and not user_busy
                    and prev_candidate is not None):

                if prev_candidate != cursor_monitor:
                    cursor_monitor = prev_candidate
                    cursor_stable_since = now
                    cursor_moved = False
                else:
                    if (not cursor_moved
                            and now - cursor_stable_since >= cfg.cursor_debounce
                            and prev_candidate < len(monitors)):
                        m = monitors[prev_candidate]
                        win32api.SetCursorPos(
                            (m.x + m.width // 2, m.y + m.height // 2)
                        )
                        cursor_moved = True

            # ---- Focus: only on monitor change, conservative ----
            if (monitor_changed
                    and prev_candidate is not None
                    and not paused[0]
                    and not user_busy):
                fg_mi = focus.foreground_monitor()
                if fg_mi != prev_candidate:
                    if (now - stable_since >= cfg.debounce
                            and now - last_switch >= cfg.switch_cooldown):
                        ok_sw = focus.focus_monitor(prev_candidate)
                        last_switch = now
                        if ok_sw:
                            acted_monitor = prev_candidate
                        status_lines.append(
                            f"SWITCH -> {prev_candidate} "
                            f"{'OK' if ok_sw else 'NO-WIN'}"
                        )

            # If cursor moved, mark this monitor as acted for cursor too
            if cursor_moved and acted_monitor != cursor_monitor:
                # Do not override focus acted_monitor; cursor is independent.
                pass

            if cfg.headless:
                if status_lines and (
                    "SWITCH" in " ".join(status_lines)
                    or int(now * 10) % 10 == 0
                ):
                    msg = " | ".join(status_lines)
                    state = "PAUSED" if paused[0] else "RUNNING"
                    busy = "BUSY" if user_busy else "READY"
                    print(f"[{state}/{busy}] {msg}")
            else:
                y = 30
                for line in status_lines:
                    cv2.putText(frame, line, (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
                    y += 25
                gate = "BUSY" if user_busy else "READY"
                gate_col = (0, 165, 255) if user_busy else (0, 255, 0)
                cv2.putText(frame, gate, (w - 200, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, gate_col, 2)
                cv2.putText(frame,
                            f"rej: jump={rejected_jumps} noface={no_face_frames}",
                            (10, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (180, 180, 180), 1)
                cv2.putText(frame,
                            "PAUSED" if paused[0] else "RUNNING",
                            (w - 130, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (0, 0, 255) if paused[0] else (0, 255, 0), 2)

                cv2.imshow("GazeFocus", frame)
                try:
                    if cv2.getWindowProperty("GazeFocus", cv2.WND_PROP_VISIBLE) < 1:
                        break
                except cv2.error:
                    break
                if (cv2.waitKey(1) & 0xFF) == ord('q'):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        if not cfg.headless:
            cv2.destroyAllWindows()
        fm.close()
        focus.stop()
        print("Stopped.")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="GazeFocus — head-pose driven focus switcher."
    )
    parser.add_argument("--calibrate", action="store_true",
                        help="Run calibration and save to calibration.json")
    parser.add_argument("--headless", action="store_true",
                        help="Run in background without preview window")
    parser.add_argument("--camera", type=int, default=CFG.camera_index,
                        help="Camera index as integer, e.g. 0 (default), 1, 2")
    args = parser.parse_args()

    CFG.camera_index = args.camera
    CFG.headless = args.headless

    need_cal = args.calibrate or not os.path.exists(CFG.calibration_file)
    if need_cal:
        do_calibration(CFG)
    if os.path.exists(CFG.calibration_file):
        run(CFG)
    else:
        print("No calibration. Run: python gazefocus.py --calibrate")


if __name__ == "__main__":
    main()
