<div align="center">

# 👁️ GazeFocus

### Look at a screen. Start typing. Focus is already there.

**Head‑pose driven focus switching for multi‑monitor setups — no clicking, ever.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)](#-platform-support)
[![MediaPipe](https://img.shields.io/badge/MediaPipe-FaceMesh-FF6F00.svg)](https://mediapipe.dev/)
[![OpenCV](https://img.shields.io/badge/OpenCV-4.8%2B-5C3EE8.svg)](https://opencv.org/)
[![Privacy: on‑device](https://img.shields.io/badge/privacy-100%25%20on--device-brightgreen.svg)](#-privacy-by-design)

</div>

---

## ✨ What is GazeFocus?

GazeFocus is a tiny desktop app that watches **where your head is pointing** and moves keyboard focus to the monitor you're facing — **before your first keystroke**.

You look at your second screen, start typing, and your words land where your eyes are. No `Alt+Tab`. No mouse click. No "oops, wrong window".

It's the interaction pattern you always wanted from a multi‑monitor setup, built from scratch with **Python + MediaPipe + OpenCV**, running **100% on your machine**.

---

## 🎬 The problem it solves

You're writing code on monitor 1. You glance at monitor 2 to check a log. You start typing…
❌ Without GazeFocus ✅ With GazeFocus
───────────────────── ─────────────────────
Terminal [git commit] Terminal [git commit]
Editor (your text) Editor (your text)
↑ ↑
lands in the lands where
wrong window you're looking

text

Dozens of times a day. GazeFocus makes it zero.

---

## 🚀 Features at a glance

| | |
|---|---|
| 🎯 **Head‑pose tracking** | MediaPipe FaceMesh + `solvePnP` → yaw/pitch in degrees |
| 🖥️ **N monitors** | Per‑monitor calibration saved to JSON |
| 🌊 **One Euro Filter** | Smooth when still, fast when you turn — no jitter, no lag |
| 🛡️ **Outlier rejection** | Sudden >25° jumps are discarded (glances, reflections, phones) |
| 🧲 **Hysteresis** | Easy to stay on a monitor, harder to leave by accident |
| 🖱️ **Cursor follows gaze** | Moves once per monitor change, yields to keyboard *and* mouse |
| ⌨️ **Respects typing** | Never steals focus mid‑sentence |
| 🖱️ **Respects mouse** | Pauses on movement, clicks **and scroll** |
| ⏱️ **Busy hysteresis** | Enters `BUSY` instantly, leaves after a calm window (0.8 s) |
| 🔁 **Change‑only mode** | Acts *only* when you switch monitors — no re‑focusing the same screen |
| 🎹 **Global hotkeys** | `Ctrl+Alt+G` pause/resume · `Ctrl+Alt+Q` quit |
| 🕶️ **Headless mode** | Run in the background with no preview window |
| 🔒 **Local only** | Camera frames stay in memory — never saved, never uploaded |

---

## 🧠 How it works (the 60‑second version)
┌──────────────┐ ┌───────────────┐ ┌────────────────┐
│ Webcam │───▶│ FaceMesh + │───▶│ One Euro │
│ 640×480 │ │ solvePnP │ │ Filter │
└──────────────┘ └───────────────┘ └────────────────┘
│
▼
┌──────────────┐ ┌───────────────┐ ┌────────────────┐
│ Focus + │◀───│ Hysteresis │◀───│ Match to │
│ Cursor │ │ + Busy gate │ │ calibration │
└──────────────┘ └───────────────┘ └────────────────┘

text

1. **Capture** — webcam at 640×480, 30 fps.
2. **Pose** — 6 facial landmarks → `cv2.solvePnP` → **yaw** (left/right) and **pitch** (up/down) in degrees.
3. **Filter** — One Euro Filter smooths the signal. Slow movements are heavily filtered; fast turns pass through instantly.
4. **Reject outliers** — any frame that jumps more than 25° from the last good pose is thrown away.
5. **Match** — nearest calibrated monitor within `yaw_pitch_threshold`. If two monitors are ambiguous, do nothing.
6. **Act** — only when:
   - you're facing a **different** monitor than the one you last acted on,
   - you haven't typed or moved the mouse in the last `typing_block` / `mouse_block` seconds,
   - and the calm window `busy_release` has elapsed.

Then it either moves the cursor to the monitor center (once) or focuses the last active window on that monitor (Windows).

---

## 📦 Installation

### Requirements

- **Python 3.10 – 3.12** (MediaPipe `0.10.14` has no wheels for 3.13 yet)
- A webcam (built‑in or external)
- **Windows 10/11** for focus switching · head tracking alone works on macOS/Linux

### Steps

bash
git clone https://github.com/AMtek27/GazeFocus-.git
cd gazefocus
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
If pip installs a newer MediaPipe without mp.solutions, pin it:
pip install "mediapipe==0.10.14"

🛠️ Usage
1️⃣ Calibrate (once per setup)
bash
python gazefocus.py --calibrate
Face the center of a monitor — turn your head, not just your eyes.

Wait until spread is below 2.0 in the preview.

Press the monitor number (1…N).

Repeat for each monitor.

Press s to save, q to cancel.

Calibration is stored in calibration.json next to the script.

Sanity check: open calibration.json. pitch should be between −30 and +30 for every monitor. If you see values near ±170, the calibration is corrupt — delete the file and recalibrate.

2️⃣ Run
bash
# With preview window
python gazefocus.py

# Background / headless (no preview)
python gazefocus.py --headless
3️⃣ Hotkeys
Shortcut	Action
Ctrl+Alt+G	Pause / resume
Ctrl+Alt+Q	Quit (clean shutdown)
q	Quit (preview window)
Ctrl+C	Quit (console)
X	Quit (preview window)
4️⃣ CLI options
text
--calibrate     Run calibration and save to calibration.json
--headless      Run in background without preview window
--camera N      Camera index as integer, e.g. 0 (default), 1, 2
Need to find your camera index?

bash
python -c "import cv2; [print(i, cv2.VideoCapture(i).isOpened()) for i in range(5)]"
The True ones are your available cameras.

🎛️ Tuning
All parameters live in Config at the top of gazefocus.py.

Focus & timing
Parameter	Default	What it does
debounce	0.15	Seconds you must face a monitor before focus switches
typing_block	0.30	Cooldown after keyboard activity
mouse_block	0.35	Cooldown after mouse activity
switch_cooldown	0.25	Minimum time between two focus switches
busy_release	0.80	Seconds of calm after last input before leaving BUSY
Matching
Parameter	Default	What it does
yaw_pitch_threshold	22.0	Max angular distance to count as facing a monitor
ambiguous_margin	0.5	If two monitors are this close, do nothing
stay_multiplier	1.6	Hysteresis: staying is 1.6× more forgiving than entering
Smoothing & noise
Parameter	Default	What it does
oe_min_cutoff	1.2	Raise to filter more when still
oe_beta	0.04	Raise for faster response on quick turns
oe_d_cutoff	1.0	Derivative filter cutoff
outlier_jump_deg	25.0	Frames that jump more than this are discarded
none_grace	0.20	Frames of None tolerated before resetting the candidate
max_no_face_frames	15	Frames without a face before the filter resets
Cursor
Parameter	Default	What it does
cursor_debounce	0.08	Delay before moving pointer once per monitor change
cursor_enabled	True	Set to False to never touch the pointer
move_pointer	True	Same as above (both must be True for cursor to move)
Recommended presets
python
# More relaxed (fewer accidental switches)
debounce = 0.25
busy_release = 1.20
yaw_pitch_threshold = 26.0

# More responsive (but still stable)
debounce = 0.10
busy_release = 0.50
oe_beta = 0.08
🔒 Privacy by design
🧠 On‑device only — all inference runs locally with MediaPipe and OpenCV.

🚫 No frames stored — images live in RAM for a single frame and are discarded immediately.

📡 No network — no account, no analytics, no telemetry, no update pings.

📄 calibration.json contains only six numbers per monitor (yaw/pitch). No images, no personal data.

Verify it yourself: grep for requests, urllib, socket, http — there are none.

🪟 Platform support
Feature	Windows 10/11	macOS	Linux
Head tracking	✅	✅	✅
Cursor movement	✅	⚠️ (needs pyobjc port)	⚠️ (needs X11 port)
Focus switching	✅	❌	❌
Focus switching relies on Win32 APIs (SetForegroundWindow, GetWindowRect). macOS and Linux ports are on the roadmap.

🧪 Verification checklist
After installation, run through this in order:

□ python gazefocus.py --calibrate → preview shows pitch between −20 and +20 facing forward
□ calibration.json exists and has pitch values between −30 and +30
□ python gazefocus.py → overlay shows READY
□ Type for 2 seconds → overlay shows BUSY the whole time
□ Stop typing → BUSY for ~0.8 s → then READY
□ Look at monitor 2 → cursor jumps there once (if move_pointer=True)
□ Keep facing monitor 2 → nothing happens again (no re‑focus)
□ Look back at monitor 1 → acts once
□ Move mouse → BUSY, cursor does not snap back
□ Ctrl+Alt+G → PAUSED
□ Ctrl+Alt+Q → clean exit, no lingering process
🧯 Troubleshooting
Symptom	Fix
AttributeError: module 'mediapipe' has no attribute 'solutions'	pip install "mediapipe==0.10.14"
calibration.json has pitch ≈ ±170	Delete it, recalibrate. If it persists, check front lighting.
Cursor snaps back to center while working	Set cursor_enabled=False or move_pointer=False
Focus never changes	Click a window on each monitor once to register it. Check overlay for NO-WIN.
Overlay stuck on BUSY	Raise busy_release is already 0.8 s — if it's too high, lower it. Or you're still typing.
Jittery pose	Increase oe_min_cutoff (e.g. 1.8) or yaw_pitch_threshold (e.g. 25)
Laggy on fast turns	Increase oe_beta (e.g. 0.08)
Wrong monitor chosen	Recalibrate facing each monitor's center with your head, not just your eyes
--camera N errors	N means an integer — use --camera 0, --camera 1, etc.
🗺️ Roadmap
□ macOS focus switching via Accessibility API (pyobjc)
□ Linux focus switching via X11 / Wayland portals
□ Split‑pane focus for VS Code, iTerm2, Windows Terminal
□ System tray icon with live status
□ Autostart on login (Windows Task Scheduler / launchd / systemd)
□ PyInstaller one‑file .exe
□ Adaptive calibration from clicks
🤝 Contributing
Issues and pull requests are welcome.

🐛 Found a bug? Open an issue with your OS, Python version, and the overlay output.

💡 Have an idea? Open an issue describing the use case before sending a big PR.

🧪 Testing on macOS/Linux? Especially welcome — the Win32 paths are the only ones currently exercised.

📄 License
MIT — see LICENSE.

Use it, fork it, ship it, sell it. Just keep the copyright notice.

<div align="center">