# RedRat IR Controller

A Raspberry Pi service that combines IR signal learning/transmission via a
[RedRat3](https://www.redrat.co.uk/products/redrat3/) USB transceiver with a
local voice-command pipeline — wake word detection followed by offline
speech-to-text — and a web control panel.

---

## Table of Contents

1. [Background](#background)
2. [Architecture](#architecture)
3. [Hardware Requirements](#hardware-requirements)
4. [Software Prerequisites](#software-prerequisites)
5. [Installation](#installation)
6. [Configuration](#configuration)
7. [Running Manually](#running-manually)
8. [systemd Service](#systemd-service)
9. [Web UI & API](#web-ui--api)
10. [Voice Pipeline](#voice-pipeline)
11. [Troubleshooting](#troubleshooting)

---

## Background

The RedRat3 is a USB IR transceiver that can both learn and replay arbitrary IR
signals. It has an in-kernel Linux driver (`redrat3`, part of `rc-core`) that
exposes the device as a standard LIRC chardev at `/dev/lirc0`.

This project uses the **kernel LIRC driver**. The kernel driver:

- is automatically loaded on device plug-in by udev,
- exposes the standard LIRC `MODE2` pulse/space interface so no vendor-level USB
  protocol handling is required in userspace,
- allows the device to be used by `ir-keytable`, `lircd`, or any other LIRC
  tool alongside this service.

The voice pipeline adds hands-free control: say the wake word ("Hey Jarvis"),
then say a registered phrase ("turn on the projector"), and the matching IR
macro fires — all offline, with no cloud dependency.

---

## Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │                  main.py                        │
                    │                                                 │
  USB RedRat3 ──────┤  LircDevice  (/dev/lirc0 via kernel redrat3)  │
                    │      │                                          │
                    │  SignalStore  (config/ir_codes.yaml)           │
                    │  MacroExecutor (config/macros.yaml)            │
                    │                                                 │
  Microphone ───────┤  AudioCapture  (sounddevice / ALSA)           │
                    │      │                                          │
                    │  WakeWordDetector  (openWakeWord / TFLite)     │
                    │      │  wake_event                              │
                    │  SpeechRecognizer  (Vosk, offline STT)        │
                    │      │  transcript                              │
                    │  CommandMatcher    (rapidfuzz)                 │
                    │      │  macro name                              │
                    │  MacroExecutor ──────────────────► LircDevice  │
                    │                                                 │
                    │  Flask API + Web UI  (port 5000)               │
                    └───────────────────────────────────────────────┘
```

### Key modules

| Module                     | Purpose                                                              |
| -------------------------- | -------------------------------------------------------------------- |
| `redrat/lirc_device.py`    | LIRC chardev driver — send/learn via `/dev/lircX`                    |
| `redrat/protocol.py`       | `IrData` dataclass; encode/decode helpers shared by both backends    |
| `redrat/store.py`          | YAML-backed IR signal store (`ir_codes.yaml`)                        |
| `macros/executor.py`       | Ordered macro runner with configurable inter-step delays             |
| `voice/audio.py`           | Continuous ALSA capture; transparent 44100→16 kHz resampling         |
| `voice/wake_word.py`       | openWakeWord background thread; fires `wake_event` on detection      |
| `voice/stt.py`             | Vosk offline STT; restricted vocabulary; rebuilds on command changes |
| `voice/command_matcher.py` | rapidfuzz `token_set_ratio` phrase matching                          |
| `voice/store.py`           | YAML-backed voice-command store; signals STT rebuild on change       |
| `api/server.py`            | Flask REST API and single-page web control panel                     |

---

## Hardware Requirements

- **Raspberry Pi** (any model with USB); tested on Raspberry Pi 4 running
  Raspberry Pi OS Bookworm (64-bit).
- **RedRat3 or RedRat3-II** USB IR transceiver (VID `0x112A`, PID `0x0001` /
  `0x0005`).
- **USB microphone** (or USB webcam with mic) for voice commands. The Logitech
  HD Pro Webcam C920 (`hw:4,0`) has been used in development.

---

## Software Prerequisites

### Python 3.11

Raspberry Pi OS Bookworm ships Python 3.11 as the system Python on 64-bit
images; confirm with:

```bash
python3 --version   # expect 3.11.x
```

If it is not available (e.g. on older images or 32-bit), **use pyenv** to
install it without disturbing the system Python:

```bash
# 1. Install pyenv build dependencies
sudo apt update
sudo apt install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

# 2. Install pyenv
curl https://pyenv.run | bash

# 3. Add pyenv to your shell (add to ~/.bashrc or ~/.profile, then reload)
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"   # optional but convenient

# 4. Install Python 3.11
pyenv install 3.11.15          # matches .python-version in the repo

# 5. Set it as the local version inside the project directory
cd /home/scott/redrat_ir_controller
pyenv local 3.11.15            # writes .python-version; already committed
```

### System packages

```bash
sudo apt update
sudo apt install -y \
  libportaudio2 \       # sounddevice / PortAudio runtime
  portaudio19-dev \     # PortAudio headers (needed to build sounddevice wheel)
  libasound2-dev \      # ALSA headers
  unzip curl            # for the model download script
```

### Kernel driver

The `redrat3` kernel module ships with the mainline kernel and loads
automatically when the device is plugged in. Verify it is present:

```bash
lsmod | grep redrat3        # should show redrat3
ls -l /dev/lirc*            # should show /dev/lirc0 (or lirc1, etc.)
```

If not loaded:

```bash
sudo modprobe redrat3
```

To load it automatically at boot:

```bash
echo redrat3 | sudo tee /etc/modules-load.d/redrat3.conf
```

#### udev permissions

The service runs as a non-root user and needs read/write access to `/dev/lircX`.
The repo ships a udev rule for this:

```bash
sudo cp 99-redrat.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# Add your service user to the video group (which owns /dev/lirc* on Bookworm):
sudo usermod -aG video scott
# Log out and back in, or reboot, for the group change to take effect.
```

> **Note:** On some Bookworm installations `/dev/lirc0` is owned by group
> `video`; on others it is `dialout` or `plugdev`. Check with
> `ls -l /dev/lirc0` and add the service user to the appropriate group.

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-org/redrat_ir_controller.git
cd redrat_ir_controller

# 2. Create a virtual environment using Python 3.11
#    (pyenv local 3.11.15 must already be in effect, or use the full path)
python -m venv .venv
source .venv/bin/activate

# 3. Upgrade pip and install dependencies
pip install -U pip setuptools wheel
pip install -r requirements.txt

# 4. Download speech / wake-word models (~200 MB total)
bash scripts/download_models.sh

# 5. Copy and edit the config
cp config/config_example.yaml config/config.yaml
# Edit config/config.yaml — see Configuration section below
```

---

## Configuration

All runtime settings live in `config/config.yaml`. An annotated example:

```yaml
redrat:
  lirc_path: "/dev/lirc0" # path to the LIRC chardev

flask:
  host: "0.0.0.0"
  port: 5000
  debug: false

storage:
  ir_codes: "config/ir_codes.yaml"
  macros: "config/macros.yaml"
  voice_commands: "config/voice_commands.yaml"

voice:
  # ALSA input device for the microphone — run `arecord -L` or `python -m sounddevice` to list.
  # "default" uses the system default input; "hw:1,0" pins a specific card.
  alsa_device: "default"

  # ALSA output device for beep/acknowledgement tones — run `aplay -L` to list.
  # Defaults to the system default output device.
  speaker_device: "default"

  # openWakeWord model name (built-in) or path to a custom .onnx/.tflite file.
  wake_word_model: "hey_jarvis_v0.1"

  # Detection confidence threshold (0–1). Lower = more sensitive.
  wake_word_threshold: 0.3

  # Path to the extracted Vosk model directory.
  vosk_model_path: "models/vosk-model-small-en-us-0.15"

  # Seconds to wait for a command after the wake word.
  command_timeout_s: 5

  # Minimum rapidfuzz score (0–100) to accept a voice command match.
  command_match_threshold: 70

  # Play a short beep when the wake word fires (requires ALSA playback support).
  beep_on_wake: true
  beep_freq_hz: 800
  beep_duration_s: 0.15
```

### IR codes, macros, and voice commands

The three YAML data files are managed by the web UI and REST API at runtime.
You can also seed them from the example files:

```bash
cp config/ir_codes_example.yaml    config/ir_codes.yaml
cp config/macros_example.yaml      config/macros.yaml
cp config/voice_commands_example.yaml config/voice_commands.yaml
```

**Macro step format** (`macros.yaml`):

```yaml
home_theater_on:
  - signal: projector_power # send IR signal; delay_ms before sending
    delay_ms: 8000
  - signal: receiver_power
    delay_ms: 2000
  - signal: receiver_input_hdmi1
```

The special signal name `__delay_1s__` inserts a one-second pause without
sending IR (useful when a longer gap is needed between steps).

---

## Running Manually

```bash
source .venv/bin/activate
python main.py                      # INFO logging
python main.py --log-level DEBUG    # verbose logging
```

Or use the convenience wrapper:

```bash
bash scripts/start.sh
```

The web UI is available at `http://<pi-hostname>:5000/` once the service starts.

---

## systemd Service

A systemd unit is provided at `deploy/redrat.service`. It must be edited to
match your username and install path before use.

### 1. Edit the unit file

```bash
nano deploy/redrat.service
```

Key fields to update:

```ini
[Service]
User=scott                                         # ← your username
WorkingDirectory=/home/scott/redrat_ir_controller  # ← absolute path to repo
ExecStart=/home/scott/redrat_ir_controller/.venv/bin/python \
          /home/scott/redrat_ir_controller/main.py
```

If you installed Python via **pyenv**, the venv was created with the pyenv-
managed Python, so the `.venv/bin/python` path is self-contained and the unit
does **not** need to know about pyenv at all — the venv embeds the correct
interpreter. No `ExecStartPre`, `Environment=PYENV_ROOT`, or shell activation
is necessary.

### 2. Install and enable

```bash
sudo cp deploy/redrat.service /etc/systemd/system/redrat.service
sudo systemctl daemon-reload
sudo systemctl enable --now redrat.service
```

### 3. Check status and logs

```bash
sudo systemctl status redrat.service
sudo journalctl -u redrat.service -f        # live log tail
sudo journalctl -u redrat.service -n 100    # last 100 lines
```

### 4. Restart after config changes

```bash
sudo systemctl restart redrat.service
```

### Complete unit file reference

```ini
[Unit]
Description=RedRat IR Controller Service
After=network.target

[Service]
Type=simple
User=scott
WorkingDirectory=/home/scott/redrat_ir_controller
ExecStart=/home/scott/redrat_ir_controller/.venv/bin/python \
          /home/scott/redrat_ir_controller/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

---

## Web UI & API

Once running, open `http://<host>:5000/` in a browser.

### Control panel panels

| Panel              | Function                                                         |
| ------------------ | ---------------------------------------------------------------- |
| **Learn Signal**   | Arm the IR receiver and capture a signal from a remote           |
| **Signals**        | Send, burst-send, or delete learned signals                      |
| **Macro Builder**  | Assemble ordered signal steps with delays; save as a named macro |
| **Macros**         | Run, delete, or load a macro into the builder                    |
| **Voice Commands** | Add, edit, and delete wake-word → macro phrase mappings          |

### REST API summary

All endpoints are under `/api/`.

```
GET    /api/signals                     list learned signal names
POST   /api/signals/learn               learn a new signal from remote
POST   /api/signals/send                transmit a signal once
POST   /api/signals/send-burst          transmit repeatedly for N seconds
DELETE /api/signals/<name>              delete a signal
GET    /api/signals/export              download ir_codes.yaml
POST   /api/signals/import              upload and replace ir_codes.yaml

GET    /api/macros                      list all macros
POST   /api/macros                      create/update a macro (auto-creates voice command)
POST   /api/macros/run                  run a macro (async)
DELETE /api/macros/<name>               delete a macro
GET    /api/macros/export               download macros.yaml
POST   /api/macros/import               upload and replace macros.yaml

GET    /api/voice/status                current STT pipeline state
GET    /api/voice/commands              list voice command mappings
POST   /api/voice/commands              add a new voice command
PUT    /api/voice/commands/<id>         update a voice command
DELETE /api/voice/commands/<id>         delete a voice command
GET    /api/voice/commands/export       download voice_commands.yaml
POST   /api/voice/commands/import       upload and replace voice_commands.yaml

GET    /api/devices                     enumerate LIRC devices
GET    /api/device/diagnostics          run device self-check
```

Adding or updating a voice command via the API (or the web UI) takes effect
immediately — the STT engine rebuilds its Vosk vocabulary without a restart.

---

## Voice Pipeline

```
Microphone
   │  16 kHz int16 mono frames (80 ms / 1280 samples)
   ▼
AudioCapture          — sounddevice; auto-resamples if device rejects 16 kHz
   │
   ├──► WakeWordDetector  — openWakeWord (hey_jarvis_v0.1 TFLite model)
   │        │  50% overlapping windows to prevent boundary misses
   │        │  wake_event set when score ≥ threshold
   │
   └──► SpeechRecognizer  — Vosk offline ASR, vocabulary restricted to
            │               registered phrases + "[unk]"
            ▼
        CommandMatcher   — rapidfuzz token_set_ratio ≥ threshold
            │
            ▼
        MacroExecutor    — runs the mapped IR macro
```

### Wake word

The default model is `hey_jarvis_v0.1`. Built-in models are downloaded by
`scripts/download_models.sh`. Custom models (`.onnx` or `.tflite`) can be
referenced by file path in `config.yaml → voice.wake_word_model`.

### Speech recognition

Vosk runs fully offline. The small English model
(`vosk-model-small-en-us-0.15`, ~50 MB) is used by default. Larger, more
accurate models can be downloaded from [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)
and pointed to via `vosk_model_path`.

The recognizer restricts its vocabulary to the currently registered voice-command
phrases. This dramatically reduces false activations. The vocabulary is rebuilt
automatically whenever a command is added or changed via the API.

### Audio device selection

Run either of these to list available input devices and their indices:

```bash
arecord -L
source .venv/bin/activate && python -m sounddevice
```

Set `voice.alsa_device` in `config.yaml` accordingly, e.g. `"hw:2,0"`,
`"plughw:2,0"`, or `"default"`. To use a separate speaker for beeps, set
`voice.speaker_device` to the output device name (e.g. `"hw:0,0"` or `"default"`).

---

## Troubleshooting

### `/dev/lirc0` not found

```bash
lsmod | grep redrat3   # check module is loaded
dmesg | grep -i redrat # check for USB enumeration errors
sudo modprobe redrat3
```

### Permission denied on `/dev/lirc0`

```bash
ls -l /dev/lirc0             # check owner/group
groups                       # check your user's groups
sudo usermod -aG video scott # add user to the owning group
# then log out and back in
```

### Wake word never triggers

1. Confirm the microphone is capturing audio:
   ```bash
   arecord -D default -f S16_LE -r 16000 -d 3 test.wav && aplay test.wav
   ```
2. Enable debug logging (`debug_wake: true`, `wake_log_every: 1`) and watch for
   `rms=` values — should spike above `0.05` when you speak.
3. Score the recording offline:
   ```bash
   source .venv/bin/activate
   python - <<'EOF'
   import wave, numpy as np
   from openwakeword.model import Model
   model = Model(wakeword_models=["hey_jarvis_v0.1"])
   with wave.open("test.wav", "rb") as wf:
       raw = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
   chunk_size = 1280
   for i in range(0, len(raw), chunk_size):
       chunk = np.pad(raw[i:i+chunk_size], (0, max(0, chunk_size-len(raw[i:i+chunk_size]))))
       preds = model.predict(chunk)
       score = preds.get("hey_jarvis_v0.1", 0)
       if score > 0.1:
           print(f"chunk {i//chunk_size}: score={score:.3f}")
   EOF
   ```

### Voice command not matched

- Check logs for `Transcription:` lines — the STT output will show what Vosk
  heard.
- Lower `command_match_threshold` (default 70) if phrases are close but not
  matching.
- Ensure the phrase is registered: `GET /api/voice/commands`.

### systemd service exits immediately

```bash
sudo journalctl -u redrat.service -n 50
```

Common causes:

- Wrong `WorkingDirectory` or `ExecStart` path.
- `/dev/lirc0` not present at service start time — add
  `After=dev-lirc0.device` to the `[Unit]` section.
- User not in the `video` (or equivalent) group.
- Python package import error — test manually first:
  ```bash
  sudo -u scott /home/scott/redrat_ir_controller/.venv/bin/python \
       /home/scott/redrat_ir_controller/main.py
  ```
