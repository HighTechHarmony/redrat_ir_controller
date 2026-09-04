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
| `voice/sensitivity.py`     | Runtime wake-word threshold override with auto-revert timer          |
| `voice/store.py`           | YAML-backed voice-command store; signals STT rebuild on change       |
| `api/server.py`            | Flask REST API and single-page web control panel                     |

---

## Hardware Requirements

- **Raspberry Pi** (any model with USB); tested on Raspberry Pi 4 running
  Raspberry Pi OS Bookworm (64-bit).
- **RedRat3 or RedRat3-II** USB IR transceiver (VID `0x112A`, PID `0x0001` /
  `0x0005`).
- **reSpeaker 2-Mics Pi HAT** (Seeed) — on-board stereo microphone + speaker
  HAT. The WM8960 codec appears as ALSA card 2 and `sounddevice` index 0.
  Requires the `seeed-voicecard` kernel driver (see Software Prerequisites below).
- **USB microphone** (alternative) — supported but not the primary setup;
  set `voice.alsa_device` to the appropriate ALSA hardware address. Logitech
  HD Pro Webcam C920 (`hw:4,0`) was used in early development.

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
  espeak-ng \            # REST text-to-speech output
  unzip curl            # for the model download script
```

### reSpeaker HAT (seeed-voicecard)

If you're using the reSpeaker 2-Mics Pi HAT, the HAT requires the Seeed
voice-card driver. Follow the installation instructions in the repository:
https://github.com/respeaker/seeed-voicecard

Example (follow upstream README for full, up-to-date instructions):

```bash
git clone https://github.com/respeaker/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
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
  # ALSA input device for the microphone — run `arecord -L` or
  # `python -m sounddevice` to list.  Use `"hw:2,0"` for the
  # reSpeaker 2-mic HAT, `"default"` for the system default.
  alsa_device: "hw:2,0"

  # Output device for beep/acknowledgement tones.  This is a
  # sounddevice device *index* (not an ALSA device name).
  # Run `python -m sounddevice` to list available output devices.
  # For the reSpeaker HAT (WM8960 codec) this is typically 0.
  speaker_device: 0

  # ALSA output device for REST text-to-speech via espeak-ng.
  # Unlike speaker_device, this is an ALSA device name, not a sounddevice index.
  # Run `aplay -L` to list available devices.
  tts_device: "default"
  tts_timeout_s: 30

  # openWakeWord model name (built-in) or path to a custom .onnx/.tflite file.
  wake_word_model: "hey_jarvis_v0.1"

  # Detection confidence threshold (0–1). Lower = more sensitive.
  # The "quiet mode" feature temporarily raises this to 0.99.
  wake_word_threshold: 0.8

  # Path to the extracted Vosk model directory.
  vosk_model_path: "models/vosk-model-small-en-us-0.15"

  # Seconds to wait for a command after the wake word.
  command_timeout_s: 5

  # Minimum rapidfuzz score (0–100) to accept a voice command match.
  command_match_threshold: 70

  # When true (default), you can define short keyword-style phrases like
  # "projector on" — rapidfuzz token_set_ratio will match transcripts like
  # "turn on the projector" or "turn the projector on" naturally.
  # Set false to use full-sentence phrases for stricter matching.
  fuzzy_match_voice_commands: true

  # Play a short beep when the wake word fires (requires ALSA playback support).
  beep_on_wake: true
  beep_freq_hz: 800
  beep_duration_s: 0.5

  # Debug/logging options (set false in normal operation to avoid CPU load).
  debug_wake: false
  wake_log_every: 100
  debug_audio: false
  audio_log_every: 100
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

GET    /api/voice/status                STT pipeline + sensitivity state
POST   /api/voice/speak                 speak text synchronously via espeak-ng (body: {"text": "Hello"})
GET    /api/voice/sensitivity           current sensitivity (threshold, remaining)
POST   /api/voice/sensitivity/suppress   enter quiet mode (body: {"seconds": 3600})
POST   /api/voice/sensitivity/cancel     exit quiet mode immediately
GET    /api/voice/commands              list voice command mappings
POST   /api/voice/commands              add a new voice command
PUT    /api/voice/commands/<id>         update a voice command
DELETE /api/voice/commands/<id>         delete a voice command
GET    /api/voice/commands/export       download voice_commands.yaml
POST   /api/voice/commands/import       upload and replace voice_commands.yaml

GET    /api/devices                     enumerate LIRC devices
GET    /api/device/diagnostics          run device self-check
```

To speak text through the configured `voice.tts_device`:

```bash
curl -X POST http://<host>:5000/api/voice/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello from the RedRat controller"}'
```

Adding or updating a voice command via the API (or the web UI) takes effect
immediately — the STT engine rebuilds its Vosk vocabulary without a restart.

---

## Voice Pipeline

### Wake word sensitivity ("quiet mode")

When the room is busy (guests, movie night, etc.), you can temporarily raise the
wake word threshold so casual conversation won't trigger false wakes:

- Say **"Hey Jarvis, quiet mode"** — the threshold rises from 0.8 to 0.99 for
  one hour (configurable 1/2/4h via the web UI), then auto-reverts. A descending
  two-tone beep confirms it.
- Say **"Hey Jarvis, normal mode"** — reverts immediately. An ascending
  two-tone beep confirms it.
- The web panel (`http://redrat:5000`) shows live countdown and Resume/Quiet
  buttons.
- The API endpoints are `POST /api/voice/sensitivity/suppress` and
  `POST /api/voice/sensitivity/cancel`.

### Pipeline flow

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
`voice.speaker_device` to the sounddevice output index (run
`python -m sounddevice` to list).

### Built-in voice commands

These system macros are always available; they don't appear in the voice-command
YAML file but show up in the macro list and can be triggered by voice or API.

| Phrase        | Macro               | Action                                     |
| ------------- | ------------------- | ------------------------------------------ |
| "quiet mode"  | `__quiet_mode__`    | Suppress wake-word (0.8→0.99) for 1 hour    |
| "normal mode" | `__normal_mode__`   | Resume normal wake-word sensitivity         |

System macros can be run via `POST /api/macros/run` with `{"name": "__quiet_mode__"}`
and appear in the web UI macro dropdown.

### Diagnostic audio scripts

The repo includes helper scripts for testing the audio hardware:

```bash
.venv/bin/python scripts/test_audio.py            # verify mic capture stream
.venv/bin/python scripts/test_playback.py --list   # list sounddevice output devices
.venv/bin/python scripts/test_playback.py --device 0  # play a test beep
.venv/bin/python scripts/test_wake_detect.py       # live wake-word score display
                                               #   (stop the service first!)
```

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

### Microphone silent — capture volume is 0%

A very common cause of "the service runs but no voice commands register" is the
**hardware capture (mic) volume being set to 0%**. The stream opens fine and
logs `Audio stream running`, but the recorded audio is silence, so the wake word
can never trigger. If you've seen this once, you'll likely hit it again after
plugging in a new mic or after a reboot — check this first.

1. Find your input card and inspect its capture controls:

   ```bash
   arecord -l                     # find your card number, e.g. card 3
   amixer -c 3 contents           # list every control, including capture volumes
   amixer -c 3 get Mic            # typical control name for USB mics
   ```

   You're looking for a capture control at `0%` / `0.00dB` (or muted `[off]`).

2. Raise it:

   ```bash
   amixer -c 3 sset Mic 100%      # control name varies: Mic / Capture / etc.
   ```

3. **Persist across reboots** — ALSA mixer settings are reset on boot unless
   saved:

   ```bash
   sudo alsactl store             # writes /var/lib/alsa/asound.state
   ```

4. Verify the mic now picks up real signal:

   ```bash
   arecord -D hw:3,0 -f S16_LE -r 44100 -c 1 -d 3 /tmp/mic_test.wav
   ```

   Then check its level (RMS well above the noise floor, spiking when you
   speak). A dead/zero-gain mic sits around −70 dBFS (just ADC noise); a quiet
   room is typically −40 to −45 dBFS; speech should peak well above that.

### Wake word never triggers

> First check the mic capture volume — if it's at 0% the audio is silent and the
> wake word can never fire. See [Microphone silent — capture volume is 0%](#microphone-silent--capture-volume-is-0)
> above.

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

### No beep on startup (or beep sounds wrong)

The startup beep at boot can fail if the codec's default sample rate doesn't
match the rate the input stream locked to. This is normal — the beep has a
fallback that tries 48 kHz, 44.1 kHz, then 16 kHz, and the beep will play on
the first supported rate. If you don't hear it:

1. Test the speaker hardware directly:
   ```bash
   aplay -D plughw:2,0 beep.wav   # hardware test (reSpeaker HAT = card 2)
   ```
2. If `aplay` works but the beep still doesn't, check the journal for
   `Startup beep failed` warnings.
3. Verify `speaker_device` is the correct sounddevice index:
   ```bash
   .venv/bin/python -m sounddevice | head -5
   ```

### No audio / wake word after a power outage

After a power outage, the I2S bus on the reSpeaker HAT can have sync errors:
```
bcm2835-i2s fe203000.i2s: I2S SYNC error!
```

This causes both the beep and microphone to fail silently on the first boot
after the outage. **A simple service restart recovers it:**

```bash
sudo systemctl restart redrat.service
```

To diagnose whether it's the same issue, check `dmesg | grep "I2S SYNC"` and
the journal for wake-word detection (see Wake word never triggers above).

### Wake word too sensitive (false triggers from room noise)

Use the **quiet mode** feature to temporarily raise the threshold:

- Web panel: click "Quiet Mode" with a 1/2/4 hour duration.
- Voice: say "Hey Jarvis, quiet mode".
- API: `POST /api/voice/sensitivity/suppress` with `{"seconds": 3600}`.

The web panel shows a live countdown; say "Hey Jarvis, normal mode" or click
"Resume Now" to cancel early.

### No wake word detection — check mic with test script

```bash
sudo systemctl stop redrat.service
.venv/bin/python scripts/test_wake_detect.py   # live scores; say "hey Jarvis"
sudo systemctl start redrat.service
```

Scores should spike above 0.5 when you say the wake word. If they stay at 0:
the mic isn't providing audio — try the capture-volume checks above, test with
`arecord -D hw:2,0 -c 2` to confirm the hardware works, and look for `I2C SYNC`
errors in `dmesg`.

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
