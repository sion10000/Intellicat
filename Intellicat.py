#!/usr/bin/env python3
import os
import sys
import glob
import time
import datetime
import random
import threading
import queue
from collections import deque
import argparse

import cv2
import numpy as np
from ultralytics import YOLO

try:
    import serial  # pyserial
except Exception:
    serial = None

# PCA9685 libs
try:
    import board
    import busio
    from adafruit_pca9685 import PCA9685
    from adafruit_motor import servo as adafruit_servo
except Exception:
    board = busio = PCA9685 = adafruit_servo = None


# -----------------------------
# HARD-CODED DEFAULTS (you can still override via CLI if you want)
# -----------------------------
DEFAULT_MODEL = "yolo11n_ncnn_model"
DEFAULT_SOURCE = "usb0"
DEFAULT_RESOLUTION = "640x480"


# -----------------------------
# Args
# -----------------------------
parser = argparse.ArgumentParser()

# no longer required; defaults are hard-coded above
parser.add_argument('--model', default=DEFAULT_MODEL, help='YOLO model (default: yolo11n_ncnn_model)')
parser.add_argument('--source', default=DEFAULT_SOURCE, help='Source (default: usb0)')
parser.add_argument('--resolution', default=DEFAULT_RESOLUTION, help='Resolution (default: 640x480)')

parser.add_argument('--thresh', type=float, default=0.5)
parser.add_argument('--record', action='store_true')
parser.add_argument('--no-gui', action='store_true', help='Disable cv2.imshow')

parser.add_argument('--role', choices=['main', 'secondary'], required=True)

# Kept for compatibility (not used in Option A)
parser.add_argument('--server-mac', default=None, help='(Unused in Option A)')

# Still used for logs only
parser.add_argument('--bt-channel', type=int, default=3, help='RFCOMM channel (default 3)')

parser.add_argument('--start-hour', type=int, default=9)
parser.add_argument('--max-cycles-per-hour', type=int, default=4)
parser.add_argument('--peer-timeout', type=int, default=180)

parser.add_argument('--enable-manual-start', action='store_true')

# PCA9685 settings
parser.add_argument('--pca-address', default="0x40", help='PCA9685 I2C address (default 0x40)')
parser.add_argument('--pca-frequency', type=int, default=50, help='PWM frequency (Hz), typically 50 for servos')

# Servo channel mapping on PCA9685
parser.add_argument('--servo1-ch', type=int, default=0, help='PCA channel for servo1 (candy)')
parser.add_argument('--servo2-ch', type=int, default=1, help='PCA channel for servo2 (in-out)')
parser.add_argument('--servo3-ch', type=int, default=2, help='PCA channel for servo3 (deploy + side-to-side)')
parser.add_argument('--servo4-ch', type=int, default=3, help='PCA channel for servo4 (door)')

# Servo pulse calibration
parser.add_argument('--servo-min-us', type=int, default=500)
parser.add_argument('--servo-max-us', type=int, default=2400)

# Base motion timing (speed is adjustable live via terminal)
parser.add_argument('--step-deg', type=float, default=1.0)
parser.add_argument('--step-delay', type=float, default=0.04)
parser.add_argument('--random-move-duration', type=float, default=2.0)
parser.add_argument('--random-move-min-s', type=float, default=1.5)
parser.add_argument('--random-move-max-s', type=float, default=2.8)

parser.add_argument('--dry-run-servos', action='store_true', help='No hardware output, just log movements')

args = parser.parse_args()

ROLE = args.role
BT_CH = args.bt_channel


# -----------------------------
# Logging
# -----------------------------
def log(msg: str):
    print(f"[{ROLE.upper()}] {msg}", flush=True)


# -----------------------------
# Runtime speed control (live adjustable)
# -----------------------------
class SpeedControl:
    """
    speed_mult:
      - 1.0 = normal
      - 2.0 = 2x faster (durations/pauses reduced)
      - 0.5 = slower (durations/pauses increased)
    """
    def __init__(self):
        self._lock = threading.Lock()

        # >>> DEFAULT SERVO SPEED MULTIPLIER <<<
        # You asked: "make the speed of the servo's 15"
        self.speed_mult = 15.0

        # base values (these stay the same; multiplier changes effective speed)
        self.base_step_deg = float(args.step_deg)
        self.base_step_delay = float(args.step_delay)
        self.base_random_move_duration = float(args.random_move_duration)
        self.base_random_min_wait = float(args.random_move_min_s)
        self.base_random_max_wait = float(args.random_move_max_s)

        # base sequence durations
        self.base_door_open = 3.0
        self.base_deploy = 2.0
        self.base_s2_home = 2.5
        self.base_s3_home = 2.0
        self.base_door_close = 3.0

        self.base_treat_door_open = 3.0
        self.base_treat_s1_out = 3.0
        self.base_treat_s1_back = 2.5
        self.base_treat_door_close = 3.0

    def set_speed(self, mult: float):
        if mult <= 0:
            return
        with self._lock:
            self.speed_mult = float(mult)

    def get_speed(self) -> float:
        with self._lock:
            return float(self.speed_mult)

    def faster(self):
        with self._lock:
            self.speed_mult *= 1.25

    def slower(self):
        with self._lock:
            self.speed_mult /= 1.25
            if self.speed_mult < 0.05:
                self.speed_mult = 0.05

    def effective_step_deg(self) -> float:
        with self._lock:
            return max(0.2, float(self.base_step_deg))

    def effective_step_delay(self) -> float:
        with self._lock:
            # faster => smaller delay
            return max(0.001, float(self.base_step_delay) / float(self.speed_mult))

    def effective_duration(self, base_seconds: float) -> float:
        with self._lock:
            # faster => smaller duration
            return max(0.0, float(base_seconds) / float(self.speed_mult))

    def effective_random_move_duration(self) -> float:
        with self._lock:
            return max(0.0, float(self.base_random_move_duration) / float(self.speed_mult))

    def effective_random_wait(self) -> float:
        with self._lock:
            w = random.uniform(float(self.base_random_min_wait), float(self.base_random_max_wait))
            return max(0.0, w / float(self.speed_mult))

    def info_line(self) -> str:
        with self._lock:
            return (f"speed={self.speed_mult:.2f} | "
                    f"step_deg={self.base_step_deg} step_delay={self.base_step_delay} | "
                    f"rnd_dur={self.base_random_move_duration} rnd_wait={self.base_random_min_wait}-{self.base_random_max_wait}")


speed = SpeedControl()


# -----------------------------
# Bluetooth via /dev/rfcomm0 (Option A)
# -----------------------------
if serial is None:
    log("ERROR: pyserial not installed. Install: sudo apt-get install -y python3-serial")
    sys.exit(1)

message_queue = queue.Queue()
bt_write_lock = threading.Lock()

RFCOMM_DEV = "/dev/rfcomm0"
bt_ser = None  # serial.Serial object


def bt_open_serial_loop():
    global bt_ser
    last_wait_log = 0.0

    while True:
        try:
            if not os.path.exists(RFCOMM_DEV):
                now = time.time()
                if now - last_wait_log > 5.0:
                    log("Waiting for /dev/rfcomm0 (Bluetooth rfcomm not connected yet)...")
                    last_wait_log = now
                time.sleep(0.5)
                continue

            bt_ser = serial.Serial(RFCOMM_DEV, baudrate=115200, timeout=1)
            log("BT /dev/rfcomm0 opened")

            buf = b""
            while True:
                data = bt_ser.read(256)
                if not data:
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = line.decode("utf-8", errors="ignore").strip()
                    if msg:
                        log(f"BT recv: {msg}")
                        message_queue.put(msg)

        except Exception as e:
            log(f"BT serial error: {e}")
        finally:
            try:
                if bt_ser:
                    bt_ser.close()
            except Exception:
                pass
            bt_ser = None
            time.sleep(1)


def send_ping(message: str):
    global bt_ser
    if bt_ser is None:
        log(f"BT not connected, cannot send '{message}'")
        return
    try:
        with bt_write_lock:
            bt_ser.write((message + "\n").encode("utf-8"))
            bt_ser.flush()
        log(f"BT sent: {message}")
    except Exception as e:
        log(f"BT send error: {e}")


threading.Thread(target=bt_open_serial_loop, daemon=True).start()


# -----------------------------
# Manual commands
# - stdin works only when you run in a terminal
# - USB keyboard hotkeys work headless at boot (systemd)
# -----------------------------
def print_help():
    log("Commands (terminal only):")
    log('  manual start hour   -> start a session on MAIN now')
    log('  treat               -> dispense candy (only when IDLE, MAIN only)')
    log('  speed <x>           -> set speed multiplier (e.g. speed 2 = faster, speed 0.5 = slower)')
    log('  faster / slower     -> adjust speed by 1.25x')
    log('  speed?              -> show current speed settings')
    log('  help                -> show this help')
    log("Hotkeys (USB keyboard, headless):")
    log('  A                   -> manual start hour')
    log('  B                   -> treat')


def stdin_listener():
    """
    NOTE: When running as a systemd service at boot, stdin usually has no attached terminal.
    This is still useful when you run the script manually over SSH in a terminal.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        cmd = line.strip()
        low = cmd.lower()

        if low in ("manual start hour", "manual", "start"):
            message_queue.put("MANUAL_START")
        elif low in ("treat", "dispense", "candy"):
            message_queue.put("MANUAL_TREAT")
        elif low.startswith("speed "):
            parts = low.split()
            if len(parts) == 2:
                try:
                    val = float(parts[1])
                    speed.set_speed(val)
                    log("Speed updated: " + speed.info_line())
                except Exception:
                    log("Invalid speed value. Example: speed 2")
            else:
                log("Usage: speed <number>")
        elif low == "faster":
            speed.faster()
            log("Speed updated: " + speed.info_line())
        elif low == "slower":
            speed.slower()
            log("Speed updated: " + speed.info_line())
        elif low in ("speed?", "speed", "status"):
            log("Speed: " + speed.info_line())
        elif low in ("help", "?"):
            print_help()
        else:
            log('Unknown command. Type "help".')


def _find_keyboard_device_path():
    """
    Prefer stable symlinks.
    """
    candidates = []
    candidates += sorted(glob.glob("/dev/input/by-id/*kbd*"))
    candidates += sorted(glob.glob("/dev/input/by-path/*kbd*"))
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def keyboard_hotkey_listener():
    """
    Headless keyboard hotkeys:
      A -> MANUAL_START
      B -> MANUAL_TREAT

    Reads directly from /dev/input/* so it works even when launched by systemd at boot.
    """
    try:
        from evdev import InputDevice, list_devices, ecodes, categorize
    except Exception as e:
        log(f"Hotkeys disabled: install evdev in your venv (pip install evdev). Error: {e}")
        return

    while True:
        dev_path = _find_keyboard_device_path()

        # Fallback: scan /dev/input/event* and pick something that looks like a keyboard
        if dev_path is None:
            try:
                for p in list_devices():
                    dev = InputDevice(p)
                    caps = dev.capabilities()
                    if ecodes.EV_KEY not in caps:
                        continue
                    keys = set(caps.get(ecodes.EV_KEY, []))
                    # basic keyboard heuristic
                    if ecodes.KEY_A in keys and ecodes.KEY_B in keys and ecodes.KEY_ENTER in keys:
                        dev_path = p
                        break
            except Exception:
                dev_path = None

        if dev_path is None:
            log("Waiting for USB keyboard...")
            time.sleep(1)
            continue

        try:
            dev = InputDevice(dev_path)
            log(f"Keyboard hotkeys enabled on: {dev_path} ({dev.name})")

            # Try to grab the device so keypresses don't also type somewhere else.
            # If permissions don't allow it, it will still often read fine.
            try:
                dev.grab()
            except Exception as e:
                log(f"Keyboard grab failed (OK): {e}")

            last_ts = {}

            for event in dev.read_loop():
                if event.type != ecodes.EV_KEY:
                    continue

                key = categorize(event)
                if key.keystate != 1:  # 1 = key down
                    continue

                code = key.keycode
                # Sometimes this is a list like ['KEY_LEFTSHIFT', 'KEY_A']
                if isinstance(code, list):
                    code = code[-1] if code else None
                if not code:
                    continue

                now = time.time()
                if code in last_ts and (now - last_ts[code]) < 0.25:
                    continue
                last_ts[code] = now

                if code == "KEY_A":
                    log("Hotkey A -> MANUAL_START")
                    message_queue.put("MANUAL_START")
                elif code == "KEY_B":
                    log("Hotkey B -> MANUAL_TREAT")
                    message_queue.put("MANUAL_TREAT")

        except OSError as e:
            log(f"Keyboard device error/disconnect ({dev_path}): {e} (retrying)")
            time.sleep(1)
            continue
        except Exception as e:
            log(f"Keyboard hotkey error ({dev_path}): {e} (retrying)")
            time.sleep(1)
            continue
        finally:
            try:
                dev.ungrab()
            except Exception:
                pass


if args.enable_manual_start:
    threading.Thread(target=stdin_listener, daemon=True).start()
    threading.Thread(target=keyboard_hotkey_listener, daemon=True).start()
    log('Manual control enabled: press A (manual start) / B (treat).  (Speed default is 15.)')


# -----------------------------
# Servo limits (your calibrated angles)
# -----------------------------
# Servo 1 (candy): 0..180, rest 0
# Servo 2 (in-out): 45..160, rest 45
# Servo 3 (deploy + side-to-side): 75..130, rest 130
#   deploy: 130 -> 100
#   side-to-side: 75..100
# Servo 4 (door): 50..130, closed 50, open 130

SERVO_LIMITS = {
    1: (0.0, 180.0),
    2: (45.0, 160.0),
    3: (75.0, 130.0),
    4: (50.0, 130.0),
}

SERVO_REST = {
    1: 0.0,
    2: 45.0,
    3: 130.0,
    4: 50.0,
}

DOOR_CLOSED = 50.0
DOOR_OPEN = 130.0

S3_REST = 130.0
S3_DEPLOY = 100.0
S3_SIDE_MIN = 75.0
S3_SIDE_MAX = 100.0

S2_REST = 45.0
S2_MIN = 45.0
S2_MAX = 160.0


def clamp_for_servo(sid: int, a: float) -> float:
    mn, mx = SERVO_LIMITS[sid]
    return float(max(mn, min(mx, float(a))))


# -----------------------------
# Servos via PCA9685 (quiet logging: only "what is moving when")
# -----------------------------
class ServoRig:
    """
    Servo IDs:
      1 = candy
      2 = in-out
      3 = deploy + side-to-side
      4 = door
    """
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.current_angles = dict(SERVO_REST)

        self.ch_map = {
            1: args.servo1_ch,
            2: args.servo2_ch,
            3: args.servo3_ch,
            4: args.servo4_ch,
        }
        self.servos = {}

        if self.dry_run:
            return

        if PCA9685 is None:
            raise RuntimeError(
                "PCA9685 libraries missing. Install in venv:\n"
                "pip install adafruit-circuitpython-pca9685 adafruit-circuitpython-motor"
            )

        addr = int(args.pca_address, 16) if isinstance(args.pca_address, str) else int(args.pca_address)
        i2c = busio.I2C(board.SCL, board.SDA)
        self.pca = PCA9685(i2c, address=addr)
        self.pca.frequency = int(args.pca_frequency)

        for sid, ch in self.ch_map.items():
            self.servos[sid] = adafruit_servo.Servo(
                self.pca.channels[int(ch)],
                min_pulse=args.servo_min_us,
                max_pulse=args.servo_max_us,
                actuation_range=180
            )

        # go to rest at startup
        for sid in (1, 2, 3, 4):
            self.set_angle(sid, SERVO_REST[sid])

    def set_angle(self, sid: int, angle: float):
        angle = clamp_for_servo(sid, angle)
        self.current_angles[sid] = angle
        if self.dry_run:
            return
        self.servos[sid].angle = angle

    def move_smooth(self, sid: int, target: float, duration: float, label: str):
        start = self.current_angles[sid]
        target = clamp_for_servo(sid, target)

        # log only once per action
        log(f"{label} (servo{sid}) {start:.1f}° -> {target:.1f}°")

        if duration <= 0:
            self.set_angle(sid, target)
            return

        # number of steps based on base step_deg, but sleep is dynamic so you can change speed live
        step_deg = max(0.2, speed.effective_step_deg())
        delta = target - start
        steps = max(1, int(abs(delta) / step_deg))

        # Duration also scales with speed multiplier
        eff_duration = speed.effective_duration(duration)
        per_step = max(speed.effective_step_delay(), eff_duration / steps)

        for i in range(1, steps + 1):
            a = start + (delta * i / steps)
            self.set_angle(sid, a)
            # per-step delay updates live (if user changes speed)
            time.sleep(speed.effective_step_delay() if speed.effective_step_delay() > per_step else per_step)

    def cleanup(self):
        if self.dry_run:
            return
        try:
            self.pca.deinit()
        except Exception:
            pass


rig = ServoRig(dry_run=args.dry_run_servos)

movement_thread = None
movement_stop_event = threading.Event()
movement_lock = threading.Lock()


# -----------------------------
# Movement / Treat sequences (same order as before)
# -----------------------------
def movement_sequence_start():
    """
    Start movement (same order, your angle limits):
      1) door open (servo4): 50 -> 130
      2) deploy (servo3): 130 -> 100
      3) random:
          servo2: 45..160
          servo3: 75..100
    """
    global movement_thread
    with movement_lock:
        movement_stop_event.clear()

        rig.move_smooth(4, DOOR_OPEN, duration=speed.base_door_open, label="Door opening")
        rig.move_smooth(3, S3_DEPLOY, duration=speed.base_deploy, label="Stick deploying")

        def worker():
            log("Random movement started (servo2 in-out + servo3 side-to-side)")
            while not movement_stop_event.is_set():
                a2 = random.uniform(S2_MIN, S2_MAX)
                a3 = random.uniform(S3_SIDE_MIN, S3_SIDE_MAX)

                rig.move_smooth(2, a2, duration=speed.base_random_move_duration, label="In-out move")
                rig.move_smooth(3, a3, duration=speed.base_random_move_duration, label="Side-to-side move")

                wait_s = speed.effective_random_wait()
                t0 = time.time()
                while (time.time() - t0) < wait_s and not movement_stop_event.is_set():
                    time.sleep(0.05)
            log("Random movement stopped")

        movement_thread = threading.Thread(target=worker, daemon=True)
        movement_thread.start()


def movement_sequence_stop():
    """
    Stop movement (same order to REST):
      1) servo2 -> 45
      2) servo3 -> 130
      3) servo4 -> 50
    """
    global movement_thread
    with movement_lock:
        movement_stop_event.set()
        if movement_thread is not None:
            movement_thread.join(timeout=3.0)
            movement_thread = None

        rig.move_smooth(2, S2_REST, duration=speed.base_s2_home, label="In-out returning to rest")
        rig.move_smooth(3, S3_REST, duration=speed.base_s3_home, label="Stick returning to rest")
        rig.move_smooth(4, DOOR_CLOSED, duration=speed.base_door_close, label="Door closing")


def dispense_treat_sequence():
    """
    Dispense candy (same order):
      1) door open (servo4): 50 -> 130
      2) candy (servo1): 0 -> 180 -> 0
      3) door close (servo4): 130 -> 50
    """
    log("Treat dispensing started")
    rig.move_smooth(4, DOOR_OPEN, duration=speed.base_treat_door_open, label="Door opening for treat")
    rig.move_smooth(1, 180.0, duration=speed.base_treat_s1_out, label="Candy servo dispensing")
    rig.move_smooth(1, 0.0, duration=speed.base_treat_s1_back, label="Candy servo resetting")
    rig.move_smooth(4, DOOR_CLOSED, duration=speed.base_treat_door_close, label="Door closing after treat")
    log("Treat dispensing finished")


# -----------------------------
# YOLO setup
# -----------------------------
if not os.path.exists(args.model):
    log(f"ERROR: model path not found: {args.model}")
    sys.exit(1)

model = YOLO(args.model, task='detect')
labels = model.names

cat_idx = None
label_items = labels.items() if isinstance(labels, dict) else enumerate(labels)
for idx, name in label_items:
    if str(name).lower() == "cat":
        cat_idx = int(idx)
        break
if cat_idx is None:
    log("ERROR: 'cat' class not found in model.")
    sys.exit(1)

log(f"Using cat class index: {cat_idx}")


# -----------------------------
# Source setup
# -----------------------------
img_source = args.source
img_ext = ['.jpg', '.jpeg', '.png', '.bmp', '.JPG', '.JPEG', '.PNG', '.BMP']
vid_ext = ['.avi', '.mov', '.mp4', '.mkv', '.wmv']

if os.path.isdir(img_source):
    source_type = 'folder'
elif os.path.isfile(img_source):
    _, ext = os.path.splitext(img_source)
    if ext in img_ext:
        source_type = 'image'
    elif ext in vid_ext:
        source_type = 'video'
    else:
        log(f"Unsupported file extension: {ext}")
        sys.exit(1)
elif 'usb' in img_source:
    source_type = 'usb'
    usb_idx = int(img_source[3:])
elif 'picamera' in img_source:
    source_type = 'picamera'
else:
    log(f"Invalid source: {img_source}")
    sys.exit(1)

resW, resH = map(int, args.resolution.split('x'))

recorder = None
if args.record:
    recorder = cv2.VideoWriter('demo1.avi', cv2.VideoWriter_fourcc(*'MJPG'), 30, (resW, resH))

cap = None
imgs_list = []
if source_type == 'image':
    imgs_list = [img_source]
elif source_type == 'folder':
    imgs_list = sorted([f for f in glob.glob(img_source + "/*") if os.path.splitext(f)[1] in img_ext])
elif source_type in ['video', 'usb']:
    cap_arg = img_source if source_type == 'video' else usb_idx
    cap = cv2.VideoCapture(cap_arg)
    cap.set(3, resW)
    cap.set(4, resH)
elif source_type == 'picamera':
    from picamera2 import Picamera2
    cap = Picamera2()
    cap.configure(cap.create_video_configuration(main={"format": 'RGB888', "size": (resW, resH)}))
    cap.start()


# -----------------------------
# Detection + session state
# -----------------------------
frame_rate_buffer = deque(maxlen=200)
infer_interval = 5.0
last_infer_time = 0.0

last_object_count = 0
last_distance_score = None

MIN_RATIO = 0.01
MAX_RATIO = 0.25


def ratio_to_score(r):
    if r <= MIN_RATIO:
        return 1
    if r >= MAX_RATIO:
        return 10
    return int(round(1 + (r - MIN_RATIO) / (MAX_RATIO - MIN_RATIO) * 9))


session_active = False
movement_on = False
session_start_ts = None
cat_seen = False
close_start_ts = None
local_close_complete = False

waiting_for_peer = False
peer_wait_start_ts = None

current_hour = datetime.datetime.now().hour
cycles_this_hour = 0

if ROLE == 'main':
    now_init = datetime.datetime.now()
    first = now_init.replace(hour=args.start_hour, minute=0, second=0, microsecond=0)
    if now_init <= first:
        next_session_time = first
    else:
        next_session_time = now_init.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
else:
    next_session_time = None


def start_session():
    global session_active, movement_on, session_start_ts, cat_seen, close_start_ts, local_close_complete
    session_active = True
    movement_on = True
    session_start_ts = time.time()
    cat_seen = False
    close_start_ts = None
    local_close_complete = False
    log("SESSION start")
    movement_sequence_start()


def stop_session():
    global session_active, movement_on
    if movement_on:
        movement_sequence_stop()
    movement_on = False
    session_active = False
    log("SESSION stop")


def schedule_next_hour():
    global next_session_time
    now_dt = datetime.datetime.now()
    next_session_time = now_dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    log(f"Next session scheduled at {next_session_time.strftime('%H:%M:%S')}")


def handle_close_logic():
    global close_start_ts, local_close_complete
    if last_distance_score is not None and last_distance_score > 8:
        if close_start_ts is None:
            close_start_ts = time.time()
        elif time.time() - close_start_ts >= 10.0:
            local_close_complete = True
    else:
        close_start_ts = None


def is_idle_now() -> bool:
    return (not session_active) and (not waiting_for_peer) and (not movement_on)


log(
    "Starting (Option A, PCA9685). Defaults: "
    f"model={args.model}, source={args.source}, resolution={args.resolution}. "
    f"Speed: {speed.info_line()}"
)

try:
    while True:
        t_start = time.perf_counter()
        now_ts = time.time()
        now_dt = datetime.datetime.now()

        # Hour change resets quota
        if now_dt.hour != current_hour:
            current_hour = now_dt.hour
            cycles_this_hour = 0
            if ROLE == 'main':
                next_session_time = now_dt.replace(minute=0, second=0, microsecond=0)
            log("New hour -> cycles reset")

        # Handle messages
        while True:
            try:
                msg = message_queue.get_nowait()
            except queue.Empty:
                break

            if msg == "MANUAL_START":
                if ROLE == "main" and (not session_active) and (not waiting_for_peer) and cycles_this_hour < args.max_cycles_per_hour:
                    next_session_time = now_dt
                    log("Manual start: scheduled immediately")
                else:
                    log("Manual start ignored")

            elif msg == "MANUAL_TREAT":
                if ROLE != "main":
                    log("Manual treat ignored (only MAIN dispenses)")
                else:
                    if not is_idle_now():
                        log("Manual treat blocked: system is not IDLE")
                    else:
                        dispense_treat_sequence()

            elif msg == "PI1_DONE" and ROLE == "secondary":
                if not session_active:
                    log("Received PI1_DONE -> starting secondary session")
                    start_session()

            elif msg == "PI2_DONE" and ROLE == "main":
                if waiting_for_peer:
                    waiting_for_peer = False
                    peer_wait_start_ts = None
                    log("Received PI2_DONE -> full cycle success on both Pis")
                    dispense_treat_sequence()
                    cycles_this_hour += 1
                    log(f"Cycle count: {cycles_this_hour}/{args.max_cycles_per_hour}")

                    if cycles_this_hour >= args.max_cycles_per_hour:
                        log("Max cycles reached -> pause until next hour")
                        next_session_time = None
                    else:
                        next_session_time = now_dt

        # MAIN starts session on schedule/manual
        if ROLE == "main" and (not session_active) and (not waiting_for_peer):
            if next_session_time is not None and now_dt >= next_session_time and cycles_this_hour < args.max_cycles_per_hour:
                log("Starting main session")
                start_session()

        # MAIN waiting for secondary timeout
        if ROLE == "main" and waiting_for_peer and peer_wait_start_ts is not None:
            if now_ts - peer_wait_start_ts > args.peer_timeout:
                log("Timeout waiting for secondary -> schedule next hour")
                waiting_for_peer = False
                peer_wait_start_ts = None
                schedule_next_hour()

        # Read frame
        frame = None
        if source_type in ['image', 'folder']:
            if not imgs_list:
                log("No images found.")
                break
            frame = cv2.imread(imgs_list[0])
            if frame is None:
                continue
        elif source_type in ['video', 'usb']:
            ret, frame = cap.read()
            if not ret or frame is None:
                log("End/camera error.")
                break
        elif source_type == 'picamera':
            frame = cap.capture_array()
            if frame is None:
                log("Picamera error.")
                break

        frame = cv2.resize(frame, (resW, resH))

        # Inference only if session active (every 5s)
        run_infer = session_active and (now_ts - last_infer_time >= infer_interval)
        if run_infer:
            last_infer_time = now_ts
            results = model(frame, verbose=False, classes=[cat_idx])
            dets = results[0].boxes

            h, w = frame.shape[:2]
            frame_area = h * w
            object_count = 0
            best_ratio = 0.0

            for det in dets:
                conf = float(det.conf.item())
                if conf < args.thresh:
                    continue
                xyxy = det.xyxy.cpu().numpy().squeeze()
                xmin, ymin, xmax, ymax = xyxy.astype(int)

                object_count += 1
                ratio = ((xmax - xmin) * (ymax - ymin)) / frame_area
                best_ratio = max(best_ratio, ratio)

            last_object_count = object_count
            if object_count > 0:
                last_distance_score = ratio_to_score(best_ratio)
                log(f"cat detected {last_distance_score}")
            else:
                last_distance_score = None

        # Session rules
        if session_active:
            if last_distance_score is not None:
                cat_seen = True

            handle_close_logic()

            if (not cat_seen) and (time.time() - session_start_ts >= 30.0):
                log("No cat in 30s -> stop")
                stop_session()
                if ROLE == "main":
                    schedule_next_hour()

            elif cat_seen and (not local_close_complete) and (time.time() - session_start_ts >= 120.0):
                log("Cat but not close in 2min -> stop")
                stop_session()
                if ROLE == "main":
                    schedule_next_hour()

            elif local_close_complete:
                log("Close cat 10s -> local success")
                stop_session()
                local_close_complete = False
                cat_seen = False

                if ROLE == "main":
                    waiting_for_peer = True
                    peer_wait_start_ts = time.time()
                    send_ping("PI1_DONE")
                else:
                    send_ping("PI2_DONE")

        # Output
        fps = 1.0 / max((time.perf_counter() - t_start), 1e-6)
        frame_rate_buffer.append(fps)
        avg_fps = float(np.mean(frame_rate_buffer)) if frame_rate_buffer else 0.0

        if recorder is not None:
            recorder.write(frame)

        if not args.no_gui:
            cv2.putText(frame, f'FPS: {avg_fps:0.2f}', (10, 20), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 255), 2)
            cv2.putText(frame, f'Cats: {last_object_count}', (10, 40), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 255), 2)
            if last_distance_score is not None:
                cv2.putText(frame, f'Distance: {last_distance_score}/10', (10, 60), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 255), 2)
            state = "WAITING_PEER" if (ROLE == "main" and waiting_for_peer) else ("ACTIVE" if session_active else "IDLE")
            cv2.putText(frame, f'State: {state}', (10, 80), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 255), 2)

            cv2.imshow("YOLO detection results", frame)
            key = cv2.waitKey(5) & 0xFF
            if key == ord('q'):
                log("Quit requested")
                break
        else:
            time.sleep(0.001)

except KeyboardInterrupt:
    log("KeyboardInterrupt -> exit")

finally:
    try:
        if session_active:
            stop_session()
    except Exception:
        pass

    if source_type in ['video', 'usb'] and cap is not None:
        cap.release()
    elif source_type == 'picamera' and cap is not None:
        try:
            cap.stop()
        except Exception:
            pass

    if recorder is not None:
        recorder.release()

    if not args.no_gui:
        cv2.destroyAllWindows()

    rig.cleanup()
    log("Clean exit")
