# Deployment (Raspbian Bookworm)

This document describes deploying `redrat_ir_controller` on Raspbian Bookworm using system Python 3.11 and a project-local virtual environment, with a systemd service unit.

Prerequisites
- A Raspberry Pi running Raspbian Bookworm (64-bit) with systemd.
- `python3.11` and `python3.11-venv` installed (package names may vary).

Quick install steps

1. Install system Python 3.11 (apt example):

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-distutils build-essential
```

2. In the project directory, create a venv and install dependencies:

```bash
cd /home/scott/redrat_ir_controller
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -r requirements.txt
```

3. Verify `tflite-runtime` and `numpy` imported correctly:

```bash
.venv/bin/python -c "import numpy; import tflite_runtime; print('numpy', numpy.__version__)"
```

If the `tflite_runtime` import fails due to missing wheel for your platform, you will need a platform-specific wheel. Check the `openwakeword` docs or the TensorFlow Lite repository for supported wheels, or build from source (advanced).

4. (Optional) Pin installed packages for reproducible deploys:

```bash
pip freeze > requirements.lock
```

5. Install and enable the systemd unit (uses `/home/scott/redrat_ir_controller/.venv` and `scott` user):

```bash
sudo cp deploy/redrat.service /etc/systemd/system/redrat.service
sudo systemctl daemon-reload
sudo systemctl enable --now redrat.service
sudo journalctl -u redrat.service -f
```

Notes
- The unit calls the venv Python directly; no activation is required in the unit file.
- For production, consider creating a dedicated `redrat` service user and placing the project under `/opt/redrat`.
- If you prefer to run without a venv, install dependencies system-wide using Python 3.11, but this is less reproducible.
