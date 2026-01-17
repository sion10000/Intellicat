# Intellicat
A way to autonomously entertain cats while the owner is not home.

# Dual Raspberry Pi Cat Enrichment Toy (YOLO + Bluetooth + PCA9685 Servos)

This project runs a **two-box / two-Raspberry-Pi** cat enrichment toy:

- **Pi 1 (MAIN)** runs a timed/looped play session.
- When a cat gets **very close** (distance score > 8 for 10 seconds), MAIN stops and **hands off** to:
- **Pi 2 (SECONDARY)** which runs the same session logic.
- When BOTH complete successfully, **Pi 1 dispenses a treat**.

Communication between Pis is done via **Bluetooth RFCOMM** using **Option A**:
- A systemd service on each Pi ensures `/dev/rfcomm0` exists after boot.
- The Python script only reads/writes messages via `/dev/rfcomm0`.

Servo control is handled via a **PCA9685** board on each Pi.

---

## Features (high level)
- Two Pi’s “take turns” to keep the cat engaged
- YOLO cat detection
- “Close proximity” detection by bounding-box size (distance score)
- PCA9685 servo control (4 servos per Pi)
- Treat dispense only after both Pi sessions succeed
- Limit of N cycles per hour (default 4)
- Manual start + manual treat + live servo speed control (via terminal commands)

---

## How it works (simple logic)
1) MAIN waits for the top of the hour (or manual start)
2) MAIN starts moving
3) If no cat after 30s → stop and wait for next hour  
4) If cat detected → continue for up to 2 minutes  
5) If cat gets close (>8/10 for 10s) → MAIN stops and pings SECONDARY  
6) SECONDARY starts moving and repeats the same rules  
7) When SECONDARY succeeds → it pings MAIN  
8) MAIN dispenses treat  
9) Repeat until 4 successes in the hour

See: [`docs/flowchart.txt`](docs/flowchart.txt)

---

## Hardware needed
Per box (2 boxes total):
- 1× Raspberry Pi 3B or 3B+
- 1× Camera (USB camera; script defaults to `usb0`)
- 1× PCA9685 16-channel servo driver
- 4× SG90 micro servos (or similar)
- 1× External 5V power supply for servos (recommended: **5V 3A–5A**)
- Jumper wires, breadboard or terminal blocks as needed

**Important:** Do NOT power servos from the Pi 5V pin when using multiple servos. Use an external 5V supply and share GND.

---

## Servo roles and angle limits (your calibrated values)
Each Pi has 4 servos:

- **Servo 1 (Candy dispenser)**
  - Range: **0° to 180°**
  - Treat action: 0 → 180 → 0

- **Servo 2 (In-out movement)**
  - Range: **45° to 160°**
  - Rest: **45°**
  - Random movement: between 45° and 160°

- **Servo 3 (Deployment + side-to-side)**
  - Overall range: **75° to 130°**
  - Rest: **130°**
  - Deploy: 130 → 100
  - Side-to-side: random between 75° and 100

- **Servo 4 (Door)**
  - Range: **50° to 130°**
  - Closed: **50°**
  - Open: **130°**

---

## Wiring (PCA9685 ↔ Pi + Servos)
Read: [`docs/wiring_pca9685.md`](docs/wiring_pca9685.md)

Quick summary:
- Pi SDA (GPIO2, pin 3) → PCA SDA
- Pi SCL (GPIO3, pin 5) → PCA SCL
- Pi 3.3V → PCA VCC
- Pi GND → PCA GND
- External 5V supply → PCA V+ (servo power rail)
- External GND → PCA GND (common ground with Pi)

---

## Software prerequisites
- Raspberry Pi OS (Bookworm or Bullseye)
- Python 3.9+
- Bluetooth enabled
- I2C enabled

Enable I2C:
```bash
sudo raspi-config
# Interface Options -> I2C -> Enable
sudo reboot
