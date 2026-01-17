# Intellicat — Dual Raspberry Pi Cat Enrichment Toy (YOLO + Bluetooth RFCOMM + PCA9685)

Intellicat is a **two-box / two-Raspberry-Pi** cat enrichment system. Two Raspberry Pi “boxes” take turns moving a toy for a cat and dispense a treat after both boxes succeed.

- **Pi 1 = MAIN box**
- **Pi 2 = SECONDARY box**

Each box has:
- a camera for cat detection (default: **USB camera `usb0`**)
- a **PCA9685** servo driver (I2C)
- **4 servos** controlling a door, stick deployment, stick motion, and a candy dispenser

The boxes communicate over **Bluetooth RFCOMM**. The Python script reads/writes messages via **`/dev/rfcomm0`**, which is created automatically after reboot by systemd services (**Option A**). You run `Intellicat.py` manually after boot; Bluetooth should already be connected.

This README is a **single complete document** that explains:
- what Intellicat does
- the full logic flow
- servo roles, safe angles, and movement order
- PCA9685 wiring and power
- Bluetooth setup that survives reboot
- software install steps
- YOLO model setup reference (guide used)
- how to run Intellicat
- runtime commands (manual start / speed / treat)
- troubleshooting and safety notes

---

## 1) What Intellicat does

Intellicat runs play sessions where a moving stick attracts a cat. The cat can “win” by coming close to the camera.

### Core behavior
- MAIN starts the movement session on the hour (or manually).
- MAIN checks for cats while moving.
- If **no cat** is detected within **30 seconds** → stop movement → wait for next hour.
- If a cat **is detected** → keep movement running.
- If the cat **never gets close** → session ends after **2 minutes** → wait for next hour.
- If the cat gets **very close**:
  - defined as **distance score > 8 for 10 seconds**
  - MAIN stops and sends a ping to SECONDARY to start.
- SECONDARY runs the same detection + timing rules.
- When SECONDARY succeeds, it pings MAIN.
- MAIN dispenses a treat **only after both** boxes succeed.
- The system stops after **4 successful cycles per hour** (configurable).

---

## 2) High-level logic (simple flow)

[MAIN] Top of hour?
No -> wait
Yes -> start moving + detect

Cat detected?
No -> after 30s stop -> wait next hour
Yes -> keep moving

Cat close (>8 score for 10s)?
No -> after 2min stop -> wait next hour
Yes -> stop -> send PI1_DONE -> [SECONDARY starts]

[SECONDARY] runs the same rules:
if close cat -> stop -> send PI2_DONE

[MAIN] receives PI2_DONE -> dispense treat -> cycle +1
if cycles>=4 -> stop until next hour

yaml
Copy code

---

## 3) “Close cat” definition (distance score)

Intellicat estimates distance by the size of the detected cat bounding box:

- It uses the **largest cat bounding box area** as a ratio of frame area.
- That ratio is converted to a **score from 1 to 10**.
- **Close** means: `distance_score > 8` continuously for **10 seconds**.

---

## 4) Hardware list (per box)

You need **two identical boxes**.

### Per box
- Raspberry Pi 3B / 3B+
- USB camera (assumed `usb0`)
- PCA9685 16-channel servo board
- 4× SG90 servos (or similar)
- External **5V power supply** for servos (recommended: **5V 3A–5A** per box depending on load)
- Wires / connectors / mechanical build parts

**Important:** Do NOT power multiple servos from the Pi 5V pin. Use an external 5V supply for the PCA9685 **V+** rail and share grounds.

---

## 5) Servo roles + safe angles (calibrated)

Each Pi has 4 servos:

### Servo 1 — Candy dispenser
- Range: **0° to 180°**
- Treat action: **0 → 180 → 0**

### Servo 2 — In/out movement
- Range: **45° to 160°**
- Rest: **45°**
- Random movement uses **45..160**

### Servo 3 — Deployment + side-to-side
- Total safe range: **75° to 130°**
- Rest: **130°**
- Deployment: **130 → 100**
- Side-to-side movement: **75..100**

### Servo 4 — Door
- Safe range: **50° to 130°**
- Closed: **50°**
- Open: **130°**

---

## 6) Servo movement sequences (order matters)

Intellicat keeps a strict order.

### Start movement (when a session begins)
1) **Door opens** (Servo 4: 50 → 130)  
2) **Stick deploy** (Servo 3: 130 → 100)  
3) **Random movement loop**
   - Servo 2 random target between **45..160**
   - Servo 3 random target between **75..100**
   - repeats until the session stops

### Stop movement (when session ends)
1) Servo 2 returns to rest: **→ 45**  
2) Servo 3 returns to rest: **→ 130**  
3) Door closes: Servo 4 **→ 50**

### Dispense treat (MAIN only)
1) Door opens (Servo 4: 50 → 130)  
2) Candy servo (Servo 1: 0 → 180 → 0)  
3) Door closes (Servo 4: 130 → 50)

---

## 7) PCA9685 wiring (per box)

### 7.1 Enable I2C on Raspberry Pi (required)
bash
sudo raspi-config
# Interface Options -> I2C -> Enable
sudo reboot
7.2 Pi ↔ PCA9685 (logic/I2C)
Raspberry Pi Pin	Signal	PCA9685 Pin
Pin 1	3.3V	VCC
Pin 3	SDA (GPIO2)	SDA
Pin 5	SCL (GPIO3)	SCL
Pin 6	GND	GND

Important: PCA9685 VCC is logic voltage. Use 3.3V from the Pi for VCC (not 5V).

### 7.3 Servo power (external 5V!) — do not skip this
Do not power multiple servos from the Pi’s 5V pin.

Connect external servo PSU to PCA9685:

External PSU	PCA9685
+5V	V+
GND	GND

### 7.4 Common ground (mandatory)
You MUST share ground:

Pi GND ↔ PCA9685 GND ↔ External PSU GND

### 7.5 Plug servos into PCA9685 channels
Each channel has: GND / V+ / Signal

SG90 wires:

Brown/Black = GND

Red = +5V

Orange/Yellow = Signal

### 7.6 Default PCA channel mapping (can be overridden)
Servo 1 → PCA channel 0

Servo 2 → PCA channel 1

Servo 3 → PCA channel 2

Servo 4 → PCA channel 3

Optional: Verify PCA9685 address
bash
Copy code
sudo apt-get install -y i2c-tools
i2cdetect -y 1
You usually see 0x40 unless you changed the board address jumpers.

---

## 8) Bluetooth setup (Option A: auto /dev/rfcomm0 after reboot)
Intellicat does not run rfcomm listen/connect inside Python.
Instead, you set up systemd services so RFCOMM connects automatically after reboot.

Goal after reboot
Pi 1 has /dev/rfcomm0 (server listening)

Pi 2 has /dev/rfcomm0 (client connected)

Intellicat.py can start manually anytime and immediately communicate

### 8.1 Install Bluetooth tools (both Pis)
bash
Copy code
sudo apt-get update
sudo apt-get install -y bluetooth bluez rfkill
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
### 8.2 Find each Pi’s Bluetooth MAC
On each Pi:

bash
Copy code
bluetoothctl show
Look for:
Controller XX:XX:XX:XX:XX:XX

Write down:

Pi 1 MAC

Pi 2 MAC

### 8.3 Pair + trust (recommended one-time)
Do on BOTH Pis (swap MACs accordingly):

bash
Copy code
bluetoothctl
Inside:

text
Copy code
power on
agent NoInputNoOutput
default-agent
discoverable on
pairable on
scan on
pair <OTHER_PI_MAC>
trust <OTHER_PI_MAC>
connect <OTHER_PI_MAC>
scan off
quit
Verify:

bash
Copy code
bluetoothctl info <OTHER_PI_MAC>
You want:

Paired: yes

Trusted: yes

---

## 9) systemd services (create /dev/rfcomm0 after boot)
### 9.1 Pi 1 (MAIN): rfcomm-server.service
Create:

bash
Copy code
sudo nano /etc/systemd/system/rfcomm-server.service
Paste:

ini
Copy code
[Unit]
Description=RFCOMM server (listen on ch 3)
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c "/usr/bin/pkill rfcomm || true"
ExecStartPre=/bin/bash -c "/usr/bin/rfcomm release all || true"
ExecStart=/usr/bin/rfcomm listen 0 3
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
Enable:

bash
Copy code
sudo systemctl daemon-reload
sudo systemctl enable --now rfcomm-server.service
### 9.2 Pi 2 (SECONDARY): rfcomm-client.service
Create:

bash
Copy code
sudo nano /etc/systemd/system/rfcomm-client.service
Paste (replace <PI1_MAC> with Pi 1 controller MAC):

ini
Copy code
[Unit]
Description=RFCOMM client (connect to MAIN on ch 3)
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c "/usr/bin/pkill rfcomm || true"
ExecStartPre=/bin/bash -c "/usr/bin/rfcomm release all || true"
ExecStart=/usr/bin/rfcomm connect 0 <PI1_MAC> 3
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
Enable:

bash
Copy code
sudo systemctl daemon-reload
sudo systemctl enable --now rfcomm-client.service
### 9.3 Verify after reboot
Reboot both:

bash
Copy code
sudo reboot
Check:

bash
Copy code
ls -l /dev/rfcomm0
Check service status:

bash
Copy code
systemctl status rfcomm-server.service --no-pager
systemctl status rfcomm-client.service --no-pager
9.4 Quick data test (optional)
Pi 1:

bash
Copy code
echo "hello from pi1" | sudo tee /dev/rfcomm0
Pi 2:

bash
Copy code
sudo cat /dev/rfcomm0

---

## 10) Software installation (Python + dependencies)
Important note (externally-managed environment)
Use a venv to install Python packages (recommended on Raspberry Pi OS).

### 10.1 Base packages (both Pis)
bash
Copy code
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-serial i2c-tools
OpenCV options:

Either use apt:

bash
Copy code
sudo apt-get install -y python3-opencv
Or use pip (inside venv):

bash
Copy code
pip install opencv-python
### 10.2 Create + activate venv
In your Intellicat project folder:

bash
Copy code
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
### 10.3 Install Python dependencies (inside venv)
bash
Copy code
pip install ultralytics numpy pyserial adafruit-blinka adafruit-circuitpython-pca9685 adafruit-circuitpython-motor
Optional (only needed if your script uses USB keyboard hotkeys without a terminal):

bash
Copy code
pip install evdev

---

## 11) YOLO model setup (reference used)
Intellicat uses a YOLO detection model. For setting up YOLO on Raspberry Pi, I used this guide:

https://www.ejtech.io/learn/yolo-on-raspberry-pi

Intellicat defaults to the model path/name:

yolo11n_ncnn_model

Make sure that yolo11n_ncnn_model exists on each Pi in the directory where you run the script (or update the script’s model path accordingly).

---

## 12) Running Intellicat (manual after boot)
Intellicat must run on both Pis.

### 12.1 Pi 1 (MAIN)
bash
Copy code
cd /path/to/your/project
source venv/bin/activate
python3 Intellicat.py --role=main --enable-manual-start --no-gui
### 12.2 Pi 2 (SECONDARY)
bash
Copy code
cd /path/to/your/project
source venv/bin/activate
python3 Intellicat.py --role=secondary --no-gui
Notes:

--enable-manual-start enables terminal commands.

--no-gui runs without an OpenCV video window (best for SSH).

If you run without --no-gui, press q in the OpenCV window to quit.

---

## 13) Manual controls (while running)
These controls work only if you started with --enable-manual-start.

Terminal commands (MAIN recommended)
Start a session now:

text
Copy code
manual start hour
Dispense treat manually (MAIN only, only when idle):

text
Copy code
treat
Speed control:

text
Copy code
speed 2
speed 0.5
faster
slower
speed?
help

---

## 14) Script defaults (unless overridden)
Intellicat defaults:

Model: yolo11n_ncnn_model

Source: usb0

Resolution: 640x480

Start hour: 9

Max cycles per hour: 4

Peer timeout: 180 seconds

Inference interval: 5 seconds

No-cat stop: 30 seconds

Cat-but-not-close stop: 120 seconds (2 minutes)

Servo angle limits and movement order are as described earlier in this README.

---

## 15) Permissions: /dev/rfcomm0
If you get permission errors opening /dev/rfcomm0, add your user to dialout:

bash
Copy code
sudo usermod -aG dialout $USER
sudo reboot

---

## 16) How to quit
In terminal: Ctrl + C

In GUI mode: press q in the OpenCV window

---

## 17) Troubleshooting
/dev/rfcomm0 missing after reboot
Check services:

bash
Copy code
systemctl status rfcomm-server.service --no-pager
systemctl status rfcomm-client.service --no-pager
Restart:

bash
Copy code
sudo systemctl restart rfcomm-server.service
sudo systemctl restart rfcomm-client.service
“No default controller available”
Restart Bluetooth:

bash
Copy code
sudo systemctl restart bluetooth
sudo systemctl restart hciuart
bluetoothctl show
“Address already in use”
Stop old rfcomm processes:

bash
Copy code
sudo pkill rfcomm
sudo rfcomm release all
Then restart services.

PCA9685 not detected
bash
Copy code
i2cdetect -y 1
If you don’t see 0x40:

enable I2C again

check SDA/SCL wiring

check PCA VCC=3.3V and GND

Servo glitches / Pi rebooting
This is almost always power:

use external 5V supply with enough current

share ground

reduce mechanical load

keep power wires short/thick

## 18) Safety notes
Moving parts can pinch. Keep paws/whiskers safe.

Secure wires so pets can’t chew them.

Always power servos correctly (external 5V recommended).

---

## 19) License / disclaimer
Use at your own risk. This is a hobby project.
Recommended license: MIT.

makefile
Copy code
::contentReference[oaicite:0]{index=0}
