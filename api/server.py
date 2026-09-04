"""
Flask web API and basic control panel for the RedRat IR controller.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import yaml
from flask import Blueprint, Flask, Response, jsonify, request

from macros.executor import MacroExecutor, MacroNotFoundError, VIRTUAL_DELAY_1S, VIRTUAL_DELAY_10S
from redrat.lirc_device import LircDevice, LircError

from redrat.store import SignalNotFoundError, SignalStore
from voice.store import VoiceCommandNotFoundError, VoiceCommandStore
from voice.tts import TextToSpeech, TtsPlaybackError, TtsTimeoutError, TtsUnavailableError

log = logging.getLogger(__name__)

# Module-level singletons injected by main.py via create_app()
_device: object | None = None
_signal_store: SignalStore | None = None
_macro_executor: MacroExecutor | None = None
_voice_store: VoiceCommandStore | None = None
_voice_status: dict = {"state": "unavailable"}
_sensitivity_mgr: object | None = None
_tts: TextToSpeech | None = None

_bp = Blueprint("api", __name__, url_prefix="/api")


def _err(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _ok(data=None, status: int = 200):
    return jsonify(data if data is not None else {"ok": True}), status


def create_app(
    device: LircDevice,
    signal_store: SignalStore,
    macro_executor: MacroExecutor,
    voice_store: VoiceCommandStore,
    voice_status: dict,
    sensitivity_mgr=None,
    tts: TextToSpeech | None = None,
) -> Flask:
    """Create and configure the Flask application."""
    global _device, _signal_store, _macro_executor, _voice_store, _voice_status, _sensitivity_mgr, _tts
    _device = device
    _signal_store = signal_store
    _macro_executor = macro_executor
    _voice_store = voice_store
    _voice_status = voice_status
    _sensitivity_mgr = sensitivity_mgr
    _tts = tts

    app = Flask(__name__)
    app.register_blueprint(_bp)

    @app.route("/", methods=["GET"])
    def home():
        return _home_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    return app


@_bp.route("", methods=["GET"])
@_bp.route("/", methods=["GET"])
def api_index():
    return _ok(
        {
            "service": "redrat-ir-controller",
            "routes": {
                "devices": ["GET /api/devices", "GET /api/device/diagnostics"],
                "documentation": ["GET /api/docs"],
                "signals": [
                    "GET /api/signals",
                    "GET /api/signals/learn",
                    "POST /api/signals/learn",
                    "POST /api/signals/send",
                    "POST /api/signals/send-burst",
                    "DELETE /api/signals/<name>",
                    "GET /api/signals/export",
                    "POST /api/signals/import",
                ],
                "macros": [
                    "GET /api/macros",
                    "POST /api/macros",
                    "POST /api/macros/run",
                    "DELETE /api/macros/<name>",
                    "GET /api/macros/export",
                    "POST /api/macros/import",
                ],
                "voice": [
                    "GET /api/voice/status",
                  "POST /api/voice/speak",
                    "GET /api/voice/sensitivity",
                    "POST /api/voice/sensitivity/suppress",
                    "POST /api/voice/sensitivity/cancel",
                    "GET /api/voice/commands",
                    "POST /api/voice/commands",
                    "PUT /api/voice/commands/<id>",
                    "DELETE /api/voice/commands/<id>",
                    "GET /api/voice/commands/export",
                    "POST /api/voice/commands/import",
                ],
            },
        }
    )


@_bp.route("/devices", methods=["GET"])
def list_devices():
    try:
        devices = LircDevice.enumerate()
        return _ok([d.info() for d in devices])
    except Exception as exc:
        log.exception("Error enumerating LIRC devices")
        return _err(str(exc), 500)


@_bp.route("/device/diagnostics", methods=["GET"])
def device_diagnostics():
    try:
        result = _device.diagnostics()
        status = 200 if result.get("ok") else 502
        return _ok(result, status)
    except Exception as exc:
        log.exception("Error running RedRat diagnostics")
        return _err(str(exc), 500)


@_bp.route("/docs", methods=["GET"])
def api_docs():
    return _api_docs_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@_bp.route("/signals", methods=["GET"])
def list_signals():
    return _ok(_signal_store.list_names())


def _get_learning_device() -> tuple[LircDevice, bool]:
    """Return a device that supports MODE2 receive and whether the caller must close it."""
    try:
        if isinstance(_device, LircDevice) and _device.info().get("can_receive"):
            return _device, False
    except Exception:
        pass

    for device in LircDevice.enumerate():
        try:
            if device.info().get("can_receive"):
                return device, True
        except Exception:
            pass
        device.close()

    raise LircError(
        "No LIRC device with MODE2 receive support was found. "
      "This RedRat3 may not be responding; try unplugging and replugging "
      "the device, then retry learning."
    )


@_bp.route("/signals/learn", methods=["GET", "POST"])
def learn_signal():
    if request.method == "GET":
        return _learn_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _err("'name' is required")
    timeout_s = float(body.get("timeout_s", 10.0))
    if not (1.0 <= timeout_s <= 60.0):
        return _err("'timeout_s' must be between 1 and 60")

    learn_device = None
    should_close = False
    try:
        learn_device, should_close = _get_learning_device()
        ir = learn_device.learn(timeout_s=timeout_s)
    except LircError as exc:
        return _err(str(exc), 504 if "timeout" in str(exc).lower() else 501)
    finally:
        if learn_device is not None and should_close:
            learn_device.close()

    try:
        _signal_store.save_signal(name, ir)
    except ValueError as exc:
        return _err(str(exc))

    return _ok(
        {"name": name, "carrier_hz": ir.carrier_hz, "timings_count": len(ir.timings_us)},
        201,
    )


@_bp.route("/signals/send", methods=["POST"])
def send_signal():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _err("'name' is required")

    try:
        ir = _signal_store.get(name)
    except SignalNotFoundError:
        return _err(f"Signal {name!r} not found", 404)

    try:
        _device.send(ir)
    except LircError as exc:
        return _err(str(exc), 502)

    return _ok({"sent": name})


@_bp.route("/signals/send-burst", methods=["POST"])
def send_signal_burst():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _err("'name' is required")

    duration_s = float(body.get("duration_s", 4.0))
    interval_ms = int(body.get("interval_ms", 120))
    if duration_s <= 0 or duration_s > 30:
        return _err("'duration_s' must be > 0 and <= 30")
    if interval_ms < 50 or interval_ms > 5000:
        return _err("'interval_ms' must be between 50 and 5000")

    try:
        ir = _signal_store.get(name)
    except SignalNotFoundError:
        return _err(f"Signal {name!r} not found", 404)

    deadline = time.monotonic() + duration_s
    sent_count = 0
    while time.monotonic() < deadline:
      try:
        _device.send(ir)
      except LircError as exc:
        return _err(f"Burst stopped after {sent_count} sends: {exc}", 502)
      sent_count += 1
      time.sleep(interval_ms / 1000.0)

    return _ok(
        {
            "name": name,
            "duration_s": duration_s,
            "interval_ms": interval_ms,
            "sent_count": sent_count,
        }
    )


@_bp.route("/signals/<name>", methods=["DELETE"])
def delete_signal(name: str):
    try:
        _signal_store.delete(name)
    except SignalNotFoundError:
        return _err(f"Signal {name!r} not found", 404)
    return _ok()


@_bp.route("/signals/export", methods=["GET"])
def export_signals():
    path = _signal_store._path
    try:
        content = path.read_bytes()
    except OSError as exc:
        return _err(f"Could not read signal store: {exc}", 500)
    return Response(
        content,
        mimetype="application/x-yaml",
        headers={"Content-Disposition": f"attachment; filename=ir_codes.yaml"},
    )


@_bp.route("/signals/import", methods=["POST"])
def import_signals():
    if "file" not in request.files:
        return _err("'file' field is required")
    uploaded = request.files["file"]
    try:
        data = yaml.safe_load(uploaded.stream)
    except yaml.YAMLError as exc:
        return _err(f"Invalid YAML: {exc}")
    if not isinstance(data, dict):
        return _err("IR codes file must be a YAML mapping (signal_name: ...)")
    path = _signal_store._path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return _err(f"Could not write signal store: {exc}", 500)
    _signal_store.reload()
    return _ok({"imported": len(data)})


@_bp.route("/macros", methods=["GET"])
def list_macros():
    return _ok(_macro_executor.list_macros())


@_bp.route("/macros", methods=["POST"])
def save_macro():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    steps = body.get("steps")
    if not name:
        return _err("'name' is required")
    if not isinstance(steps, list):
        return _err("'steps' must be an array")

    try:
        _macro_executor.save_macro(name, steps)
    except ValueError as exc:
        return _err(str(exc), 400)

    # Auto-create a voice command when a brand-new macro is saved.
    voice_command_created = None
    if _voice_store is not None:
        already_mapped = any(
            c.get("macro") == name for c in _voice_store.list_commands()
        )
        if not already_mapped:
            phrase = name.replace("_", " ").replace("-", " ").strip()
            try:
                voice_command_created = _voice_store.add(phrase=phrase, macro=name)
            except Exception as exc:
                log.warning("Could not auto-create voice command for macro %r: %s", name, exc)

    return _ok({"saved": name, "voice_command_created": voice_command_created}, 201)


@_bp.route("/macros/<name>", methods=["DELETE"])
def delete_macro(name: str):
    try:
        _macro_executor.delete_macro(name)
    except MacroNotFoundError:
        return _err(f"Macro {name!r} not found", 404)
    except ValueError as exc:
        return _err(str(exc), 400)
    return _ok()


@_bp.route("/macros/export", methods=["GET"])
def export_macros():
    path = _macro_executor._path
    try:
        content = path.read_bytes()
    except OSError as exc:
        return _err(f"Could not read macro store: {exc}", 500)
    return Response(
        content,
        mimetype="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=macros.yaml"},
    )


@_bp.route("/macros/import", methods=["POST"])
def import_macros():
    if "file" not in request.files:
        return _err("'file' field is required")
    uploaded = request.files["file"]
    try:
        data = yaml.safe_load(uploaded.stream)
    except yaml.YAMLError as exc:
        return _err(f"Invalid YAML: {exc}")
    if not isinstance(data, dict):
        return _err("Macros file must be a YAML mapping (macro_name: [steps])")
    path = _macro_executor._path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return _err(f"Could not write macro store: {exc}", 500)
    _macro_executor.reload()
    return _ok({"imported": len(data)})


@_bp.route("/macros/run", methods=["POST"])
def run_macro():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _err("'name' is required")

    if name not in _macro_executor.macro_names():
        return _err(f"Macro {name!r} not found", 404)

    def _run():
        try:
            _macro_executor.run(name)
        except Exception as exc:
            log.error("Error running macro %r: %s", name, exc)

    threading.Thread(target=_run, daemon=True, name=f"macro-{name}").start()
    return _ok({"running": name}, 202)


@_bp.route("/voice/status", methods=["GET"])
def voice_status():
    data = dict(_voice_status)
    if _sensitivity_mgr is not None:
        data["sensitivity"] = _sensitivity_mgr.status
    return _ok(data)

@_bp.route("/voice/speak", methods=["POST"])
def speak_text():
    if _tts is None:
        return _err("Text-to-speech is unavailable", 503)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Request body must be a JSON object")
    text = body.get("text")
    if not isinstance(text, str):
        return _err("'text' must be a string")
    text = text.strip()
    if not text:
        return _err("'text' must not be empty")
    if len(text) > 500:
        return _err("'text' must be 500 characters or fewer")

    try:
        _tts.speak(text)
    except TtsUnavailableError as exc:
        log.error("Text-to-speech unavailable: %s", exc)
        return _err(str(exc), 503)
    except (TtsPlaybackError, TtsTimeoutError) as exc:
        log.error("Text-to-speech playback failed: %s", exc)
        return _err(str(exc), 502)

    return _ok({"spoken": True, "text": text})


@_bp.route("/voice/sensitivity", methods=["GET"])
def get_sensitivity():
    if _sensitivity_mgr is None:
        return _err("Sensitivity manager unavailable", 503)
    return _ok(_sensitivity_mgr.status)


@_bp.route("/voice/sensitivity/suppress", methods=["POST"])
def suppress_sensitivity():
    if _sensitivity_mgr is None:
        return _err("Sensitivity manager unavailable", 503)
    body = request.get_json(silent=True) or {}
    seconds = int(body.get("seconds", 0) or 0)
    if seconds <= 0:
        seconds = _sensitivity_mgr.status.get("default_suppress_s", 3600)
    _sensitivity_mgr.suppress(seconds)
    return _ok(_sensitivity_mgr.status)


@_bp.route("/voice/sensitivity/cancel", methods=["POST"])
def cancel_sensitivity():
    if _sensitivity_mgr is None:
        return _err("Sensitivity manager unavailable", 503)
    _sensitivity_mgr.cancel()
    return _ok(_sensitivity_mgr.status)


@_bp.route("/voice/commands", methods=["GET"])
def list_voice_commands():
    return _ok(_voice_store.list_commands())


@_bp.route("/voice/commands", methods=["POST"])
def add_voice_command():
    body = request.get_json(silent=True) or {}
    phrase = body.get("phrase", "").strip()
    macro = body.get("macro", "").strip()
    if not phrase:
        return _err("'phrase' is required")
    if not macro:
        return _err("'macro' is required")

    try:
        entry = _voice_store.add(phrase=phrase, macro=macro)
    except ValueError as exc:
        return _err(str(exc))

    return _ok(entry, 201)


@_bp.route("/voice/commands/<command_id>", methods=["PUT"])
def update_voice_command(command_id: str):
    body = request.get_json(silent=True) or {}
    phrase = body.get("phrase")
    macro = body.get("macro")
    if phrase is not None:
        phrase = phrase.strip() or None
    if macro is not None:
        macro = macro.strip() or None

    if phrase is None and macro is None:
        return _err("At least one of 'phrase' or 'macro' must be provided")

    try:
        entry = _voice_store.update(command_id, phrase=phrase, macro=macro)
    except VoiceCommandNotFoundError:
        return _err(f"Voice command {command_id!r} not found", 404)
    except ValueError as exc:
        return _err(str(exc))

    return _ok(entry)


@_bp.route("/voice/commands/<command_id>", methods=["DELETE"])
def delete_voice_command(command_id: str):
    try:
        _voice_store.delete(command_id)
    except VoiceCommandNotFoundError:
        return _err(f"Voice command {command_id!r} not found", 404)
    return _ok()


@_bp.route("/voice/commands/export", methods=["GET"])
def export_voice_commands():
    path = _voice_store._path
    try:
        content = path.read_bytes()
    except OSError as exc:
        return _err(f"Could not read voice command store: {exc}", 500)
    return Response(
        content,
        mimetype="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=voice_commands.yaml"},
    )


@_bp.route("/voice/commands/import", methods=["POST"])
def import_voice_commands():
    if "file" not in request.files:
        return _err("'file' field is required")
    uploaded = request.files["file"]
    try:
        data = yaml.safe_load(uploaded.stream)
    except yaml.YAMLError as exc:
        return _err(f"Invalid YAML: {exc}")
    if not isinstance(data, list):
        return _err("Voice commands file must be a YAML sequence (list of entries)")
    path = _voice_store._path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return _err(f"Could not write voice command store: {exc}", 500)
    _voice_store.reload()
    return _ok({"imported": len(data)})


def _home_html() -> str:
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>RedRat Control Panel</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; margin: 1.2rem; color: #222; }}
      h1 {{ margin: 0 0 0.75rem 0; }}
      .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 0.9rem; }}
      .panel {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.8rem; background: #fff; }}
      .row {{ display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin: 0.4rem 0; }}
      label {{ min-width: 90px; }}
      input[type=text], input[type=number], select {{ padding: 0.4rem 0.5rem; min-width: 180px; }}
      button {{ padding: 0.42rem 0.75rem; cursor: pointer; }}
      ul {{ margin: 0.4rem 0 0.4rem 1.2rem; }}
      .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
      .hint {{ color: #666; font-size: 0.92rem; }}
      #toast {{ margin-top: 0.6rem; color: #0a5; }}
      table.vc-table {{ width:100%; border-collapse:collapse; font-size:0.93rem; margin-top:0.5rem; }}
      table.vc-table th {{ text-align:left; border-bottom:2px solid #ddd; padding:0.25rem 0.4rem; }}
      table.vc-table td {{ border-bottom:1px solid #eee; padding:0.3rem 0.4rem; vertical-align:middle; }}
      table.vc-table tr:hover td {{ background:#f9f9f9; }}
      .vc-edit-row {{ background:#fffbe6 !important; }}
      .sortable {{ cursor:pointer; user-select:none; }}
      .sortable:hover {{ background:#f0f0f0; }}
      .sort-indicator {{ font-size:0.75rem; margin-left:0.2rem; color:#888; }}
    </style>
  </head>
  <body>
    <h1>RedRat Control Panel</h1>
    <p class="hint">Learn signals, send/test IR, create macros (with virtual 1s and 10s delay steps), and run playback.</p>

    <div class="grid">
      <div class="panel">
        <h2>Learn Signal</h2>
        <div class="row"><label>Name</label><input id="learn-name" type="text" value="new_signal"/></div>
        <div class="row"><label>Timeout (s)</label><input id="learn-timeout" type="number" min="1" max="60" value="10"/></div>
        <div class="row"><button id="btn-learn">Learn</button></div>
        <div id="learn-result" class="hint"></div>
      </div>

      <div class="panel">
        <h2>Signals</h2>
        <div class="row"><button id="btn-refresh-signals">Refresh</button></div>
        <div class="row"><label>Signal</label><select id="signal-select"></select></div>
        <div class="row">
          <button id="btn-send-once">Send Once</button>
          <button id="btn-send-burst">Send Burst 4s</button>
          <button id="btn-delete-signal">Delete</button>
        </div>
        <div id="signals-result" class="hint"></div>
      </div>

      <div class="panel">
        <h2>Macro Builder</h2>
        <div class="row"><label>Macro Name</label><input id="macro-name" type="text" value="new_macro"/></div>
        <div class="row"><label>Signal Step</label><select id="macro-signal-select"></select><button id="btn-add-step">Add Step</button></div>
        <div class="row">
          <button id="btn-add-delay">Add 1s Delay Step</button>
          <button id="btn-add-delay10">Add 10s Delay Step</button>
          <span class="hint mono">{VIRTUAL_DELAY_1S} {VIRTUAL_DELAY_10S}</span>
        </div>
        <ul id="macro-steps"></ul>
        <div class="row"><button id="btn-save-macro">Save Macro</button></div>
        <div id="macro-build-result" class="hint"></div>
      </div>

      <div class="panel">
        <h2>Macros</h2>
        <div class="row"><button id="btn-refresh-macros">Refresh</button></div>
        <div class="row"><label>Macro</label><select id="macro-select"></select></div>
        <div class="row"><button id="btn-run-macro">Run</button><button id="btn-delete-macro">Delete</button><button id="btn-load-macro">Load Into Builder</button></div>
        <div id="macros-result" class="hint"></div>
      </div>

      <div class="panel">
        <h2>Wake Word Sensitivity</h2>
        <p class="hint">Quiet mode raises the wake-word threshold so room chatter won't trigger it.</p>
        <div class="row"><span id="sens-state" style="font-weight:bold;">Loading...</span></div>
        <div class="row">
          <label>Duration</label>
          <select id="sens-duration">
            <option value="3600">1 hour</option>
            <option value="7200">2 hours</option>
            <option value="14400">4 hours</option>
          </select>
        </div>
        <div class="row">
          <button id="btn-sens-quiet">Quiet Mode</button>
          <button id="btn-sens-resume" style="display:none;">Resume Now</button>
        </div>
        <div id="sens-result" class="hint"></div>
      </div>
    </div>

    <div class="panel" style="margin-top:0.9rem;">
      <h2>Voice Commands</h2>
      <p class="hint">Map a spoken phrase to a macro. Say the wake word, then the phrase.</p>
      <div class="row">
        <label>Phrase</label>
        <input id="vc-phrase" type="text" placeholder="e.g. turn on the lights" style="min-width:220px;" />
        <label style="min-width:50px;">Macro</label>
        <select id="vc-macro" style="min-width:180px;"></select>
        <button id="btn-vc-save">Add</button>
        <button id="btn-vc-cancel" style="display:none;">Cancel</button>
      </div>
      <div id="vc-result" class="hint"></div>
      <table class="vc-table" id="vc-table">
        <thead><tr><th data-sort="phrase" class="sortable">Phrase <span class="sort-indicator"></span></th><th data-sort="macro" class="sortable">Macro <span class="sort-indicator"></span></th><th style="width:120px;"></th></tr></thead>
        <tbody id="vc-tbody"></tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:0.9rem;">
      <h2>Export / Import</h2>
      <p class="hint">Download a YAML file to back up your data, or upload a previously saved file to restore it. Import <strong>replaces</strong> the current data.</p>
      <table style="width:100%;border-collapse:collapse;font-size:0.93rem;">
        <thead><tr><th style="text-align:left;padding:0.3rem 0.5rem;border-bottom:2px solid #ddd;">Dataset</th><th style="text-align:left;padding:0.3rem 0.5rem;border-bottom:2px solid #ddd;">Export</th><th style="text-align:left;padding:0.3rem 0.5rem;border-bottom:2px solid #ddd;">Import</th></tr></thead>
        <tbody>
          <tr>
            <td style="padding:0.35rem 0.5rem;">IR Signals</td>
            <td style="padding:0.35rem 0.5rem;"><a href="/api/signals/export" download="ir_codes.yaml"><button type="button">Download</button></a></td>
            <td style="padding:0.35rem 0.5rem;"><input type="file" id="imp-signals-file" accept=".yaml,.yml" style="max-width:200px;"/><button type="button" id="btn-imp-signals">Upload</button> <span id="imp-signals-result" class="hint"></span></td>
          </tr>
          <tr>
            <td style="padding:0.35rem 0.5rem;">Macros</td>
            <td style="padding:0.35rem 0.5rem;"><a href="/api/macros/export" download="macros.yaml"><button type="button">Download</button></a></td>
            <td style="padding:0.35rem 0.5rem;"><input type="file" id="imp-macros-file" accept=".yaml,.yml" style="max-width:200px;"/><button type="button" id="btn-imp-macros">Upload</button> <span id="imp-macros-result" class="hint"></span></td>
          </tr>
          <tr>
            <td style="padding:0.35rem 0.5rem;">Voice Commands</td>
            <td style="padding:0.35rem 0.5rem;"><a href="/api/voice/commands/export" download="voice_commands.yaml"><button type="button">Download</button></a></td>
            <td style="padding:0.35rem 0.5rem;"><input type="file" id="imp-vc-file" accept=".yaml,.yml" style="max-width:200px;"/><button type="button" id="btn-imp-vc">Upload</button> <span id="imp-vc-result" class="hint"></span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:0.9rem;">
      <h2>Docs</h2>
      <p><a href="/api/docs">Open API docs</a></p>
      <p id="toast"></p>
    </div>

    <script>
      const delayToken = {json.dumps(VIRTUAL_DELAY_1S)};
      const delay10Token = {json.dumps(VIRTUAL_DELAY_10S)};
      const macroSteps = [];
      let lastMacros = {{}};
      const $ = (id) => document.getElementById(id);

      function show(el, msg) {{ $(el).textContent = msg; }}

      async function api(path, options={{}}) {{
        const resp = await fetch(path, options);
        const data = await resp.json().catch(() => ({{}}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${{resp.status}}`);
        return data;
      }}

      function renderSteps() {{
        const ul = $("macro-steps");
        ul.innerHTML = "";
        macroSteps.forEach((s, idx) => {{
          const li = document.createElement("li");
          const txt = s.signal === delayToken ? "[delay 1s]" : (s.signal === delay10Token ? "[delay 10s]" : s.signal);
          li.textContent = txt + (s.delay_ms ? ` (+${{s.delay_ms}}ms)` : "");
          const b = document.createElement("button");
          b.textContent = "Remove";
          b.style.marginLeft = "8px";
          b.onclick = () => {{ macroSteps.splice(idx, 1); renderSteps(); }};
          li.appendChild(b);
          ul.appendChild(li);
        }});
      }}

      async function refreshSignals() {{
        const names = await api('/api/signals');
        const selects = [$("signal-select"), $("macro-signal-select")];
        selects.forEach(sel => {{
          sel.innerHTML = "";
          names.forEach(n => {{
            const o = document.createElement('option');
            o.value = n; o.textContent = n;
            sel.appendChild(o);
          }});
        }});
      }}

      async function refreshMacros() {{
        lastMacros = await api('/api/macros');
        const sel = $("macro-select");
        sel.innerHTML = "";
        Object.keys(lastMacros).sort().forEach(n => {{
          const o = document.createElement('option');
          o.value = n; o.textContent = n;
          sel.appendChild(o);
        }});
        // Also refresh the voice-command macro dropdown
        refreshMacroDropdown();
      }}

      function refreshMacroDropdown() {{
        const sel = $("vc-macro");
        const prev = sel.value;
        sel.innerHTML = "";
        Object.keys(lastMacros).sort().forEach(n => {{
          const o = document.createElement('option');
          o.value = n; o.textContent = n;
          sel.appendChild(o);
        }});
        // Restore previous selection if still present
        if (prev && [...sel.options].some(o => o.value === prev)) {{
          sel.value = prev;
        }}
      }}

      $("btn-learn").onclick = async () => {{
        try {{
          show("learn-result", "Learning... press remote button now.");
          const data = await api('/api/signals/learn', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{ name: $("learn-name").value.trim(), timeout_s: Number($("learn-timeout").value || 10) }})
          }});
          show("learn-result", `Learned '${{data.name}}' (${{data.timings_count}} timings).`);
          await refreshSignals();
        }} catch (e) {{ show("learn-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-send-once").onclick = async () => {{
        try {{
          const name = $("signal-select").value;
          await api('/api/signals/send', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{name}})}});
          show("signals-result", `Sent '${{name}}'.`);
        }} catch (e) {{ show("signals-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-send-burst").onclick = async () => {{
        try {{
          const name = $("signal-select").value;
          const data = await api('/api/signals/send-burst', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{name, duration_s:4, interval_ms:120}})}});
          show("signals-result", `Burst complete: ${{data.sent_count}} sends.`);
        }} catch (e) {{ show("signals-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-delete-signal").onclick = async () => {{
        try {{
          const name = $("signal-select").value;
          await api(`/api/signals/${{encodeURIComponent(name)}}`, {{method:'DELETE'}});
          show("signals-result", `Deleted '${{name}}'.`);
          await refreshSignals();
        }} catch (e) {{ show("signals-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-refresh-signals").onclick = async () => {{
        try {{ await refreshSignals(); show("signals-result", "Signals refreshed."); }}
        catch (e) {{ show("signals-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-add-step").onclick = () => {{
        const s = $("macro-signal-select").value;
        if (!s) return;
        macroSteps.push({{signal:s}});
        renderSteps();
      }};

      $("btn-add-delay").onclick = () => {{
        macroSteps.push({{signal:delayToken}});
        renderSteps();
      }};

      $("btn-add-delay10").onclick = () => {{
        macroSteps.push({{signal:delay10Token}});
        renderSteps();
      }};

      $("btn-save-macro").onclick = async () => {{
        try {{
          const name = $("macro-name").value.trim();
          if (!name) throw new Error("Macro name is required");
          if (!macroSteps.length) throw new Error("Add at least one step");
          await api('/api/macros', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{name, steps: macroSteps}})}});
          show("macro-build-result", `Saved macro '${{name}}'.`);
          await refreshMacros();
        }} catch (e) {{ show("macro-build-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-refresh-macros").onclick = async () => {{
        try {{ await refreshMacros(); show("macros-result", "Macros refreshed."); }}
        catch (e) {{ show("macros-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-run-macro").onclick = async () => {{
        try {{
          const name = $("macro-select").value;
          await api('/api/macros/run', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{name}})}});
          show("macros-result", `Running '${{name}}'.`);
        }} catch (e) {{ show("macros-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-delete-macro").onclick = async () => {{
        try {{
          const name = $("macro-select").value;
          await api(`/api/macros/${{encodeURIComponent(name)}}`, {{method:'DELETE'}});
          show("macros-result", `Deleted '${{name}}'.`);
          await refreshMacros();
        }} catch (e) {{ show("macros-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-load-macro").onclick = () => {{
        const name = $("macro-select").value;
        const steps = lastMacros[name] || [];
        $("macro-name").value = name;
        macroSteps.length = 0;
        steps.forEach(s => macroSteps.push({{...s}}));
        renderSteps();
        show("macro-build-result", `Loaded '${{name}}' into builder.`);
      }};

      // ── Voice Commands ────────────────────────────────────────────────
      let vcEditingId = null;
      let vcSortCol = null;
      let vcSortDir = 1; // 1 = asc, -1 = desc
      let vcData = [];

      function vcSort(col) {{
        if (vcSortCol === col) {{
          vcSortDir *= -1;
        }} else {{
          vcSortCol = col;
          vcSortDir = 1;
        }}
        // Update indicators
        document.querySelectorAll("#vc-table .sort-indicator").forEach(el => el.textContent = "");
        const th = document.querySelector(`#vc-table th[data-sort="${{col}}"]`);
        if (th) th.querySelector(".sort-indicator").textContent = vcSortDir === 1 ? "▲" : "▼";
        vcRenderTable();
      }}

      function vcRenderTable() {{
        const tbody = $("vc-tbody");
        tbody.innerHTML = "";
        let sorted = [...vcData];
        if (vcSortCol) {{
          sorted.sort((a, b) => {{
            const va = (a[vcSortCol] || "").toLowerCase();
            const vb = (b[vcSortCol] || "").toLowerCase();
            return va < vb ? -vcSortDir : va > vb ? vcSortDir : 0;
          }});
        }}
        sorted.forEach(cmd => {{
          const tr = document.createElement("tr");
          tr.dataset.id = cmd.id;
          tr.innerHTML = `
            <td class="mono">${{cmd.phrase}}</td>
            <td class="mono">${{cmd.macro}}</td>
            <td>
              <button onclick="vcStartEdit('${{cmd.id}}','${{cmd.phrase.replace(/'/g,"\\'")}}',${{JSON.stringify(cmd.macro).replace(/"/g,"&quot;")}})">Edit</button>
              <button onclick="vcDelete('${{cmd.id}}')">Delete</button>
            </td>`;
          tbody.appendChild(tr);
        }});
        // Re-apply edit row highlight
        if (vcEditingId) {{
          document.querySelectorAll("#vc-tbody tr").forEach(tr => {{
            tr.classList.toggle("vc-edit-row", tr.dataset.id === vcEditingId);
          }});
        }}
      }}

      async function refreshVoiceCommands() {{
        vcData = await api('/api/voice/commands');
        vcRenderTable();
      }}

      function vcStartEdit(id, phrase, macro) {{
        vcEditingId = id;
        $("vc-phrase").value = phrase;
        $("vc-macro").value = macro;
        $("btn-vc-save").textContent = "Update";
        $("btn-vc-cancel").style.display = "";
        // Highlight the row being edited
        document.querySelectorAll("#vc-tbody tr").forEach(tr => {{
          tr.classList.toggle("vc-edit-row", tr.dataset.id === id);
        }});
        $("vc-phrase").focus();
        // Also load the associated macro into the Macro Builder panel
        if (macro && lastMacros[macro]) {{
          $("macro-name").value = macro;
          macroSteps.length = 0;
          lastMacros[macro].forEach(s => macroSteps.push({{...s}}));
          renderSteps();
          show("macro-build-result", `Loaded '${{macro}}' into builder (from voice command edit).`);
        }}
      }}

      function vcCancelEdit() {{
        vcEditingId = null;
        $("vc-phrase").value = "";
        const msel = $("vc-macro");
        if (msel.options.length) msel.selectedIndex = 0;
        $("btn-vc-save").textContent = "Add";
        $("btn-vc-cancel").style.display = "none";
        document.querySelectorAll("#vc-tbody tr").forEach(tr => tr.classList.remove("vc-edit-row"));
      }}

      async function vcDelete(id) {{
        try {{
          await api(`/api/voice/commands/${{encodeURIComponent(id)}}`, {{method:'DELETE'}});
          if (vcEditingId === id) vcCancelEdit();
          await refreshVoiceCommands();
          show("vc-result", "Deleted.");
        }} catch(e) {{ show("vc-result", `Error: ${{e.message}}`); }}
      }}

      $("btn-vc-cancel").onclick = vcCancelEdit;

      $("btn-vc-save").onclick = async () => {{
        const phrase = $("vc-phrase").value.trim();
        const macro  = $("vc-macro").value.trim();
        if (!phrase) {{ show("vc-result", "Phrase is required."); return; }}
        if (!macro)  {{ show("vc-result", "Macro is required.");  return; }}
        try {{
          if (vcEditingId) {{
            await api(`/api/voice/commands/${{encodeURIComponent(vcEditingId)}}`, {{
              method:'PUT', headers:{{'Content-Type':'application/json'}},
              body: JSON.stringify({{phrase, macro}})
            }});
            show("vc-result", `Updated command.`);
            vcCancelEdit();
          }} else {{
            await api('/api/voice/commands', {{
              method:'POST', headers:{{'Content-Type':'application/json'}},
              body: JSON.stringify({{phrase, macro}})
            }});
            show("vc-result", `Added '${{phrase}}'.`);
            $("vc-phrase").value = "";
            $("vc-macro").value = "";
          }}
          await refreshVoiceCommands();
        }} catch(e) {{ show("vc-result", `Error: ${{e.message}}`); }}
      }};

      // ── Export / Import ───────────────────────────────────────────────
      async function importYaml(fileInputId, resultId, endpoint, refreshFn) {{
        const file = $(fileInputId).files[0];
        if (!file) {{ show(resultId, "Please select a file first."); return; }}
        const fd = new FormData();
        fd.append("file", file);
        try {{
          show(resultId, "Uploading...");
          const resp = await fetch(endpoint, {{ method: "POST", body: fd }});
          const data = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(data.error || `HTTP ${{resp.status}}`);
          show(resultId, `Imported ${{data.imported}} item(s). ✓`);
          $(fileInputId).value = "";
          if (refreshFn) await refreshFn();
        }} catch (e) {{ show(resultId, `Error: ${{e.message}}`); }}
      }}

      $("btn-imp-signals").onclick = () => importYaml("imp-signals-file", "imp-signals-result", "/api/signals/import", refreshSignals);
      $("btn-imp-macros").onclick  = () => importYaml("imp-macros-file",  "imp-macros-result",  "/api/macros/import",  refreshMacros);
      $("btn-imp-vc").onclick      = () => importYaml("imp-vc-file",      "imp-vc-result",      "/api/voice/commands/import", refreshVoiceCommands);

      // ── Wake Word Sensitivity ─────────────────────────────────────────
      async function refreshSensitivity() {{
        try {{
          const s = await api('/api/voice/sensitivity');
          const state = $("sens-state");
          if (s.state === "suppressed") {{
            const mins = Math.floor(s.remaining_s / 60);
            const secs = s.remaining_s % 60;
            state.textContent = `Suppressed — ${{mins}}m ${{secs}}s remaining`;
            state.style.color = "#a60";
            $("btn-sens-resume").style.display = "";
            $("btn-sens-quiet").style.display = "none";
          }} else {{
            state.textContent = `Normal (threshold ${{s.threshold}})`;
            state.style.color = "#0a5";
            $("btn-sens-resume").style.display = "none";
            $("btn-sens-quiet").style.display = "";
          }}
        }} catch (e) {{
          show("sens-state", `Error: ${{e.message}}`);
        }}
      }}

      $("btn-sens-quiet").onclick = async () => {{
        try {{
          const seconds = Number($("sens-duration").value || 3600);
          await api('/api/voice/sensitivity/suppress', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{seconds}})}});
          show("sens-result", `Quiet mode on for ${{seconds}}s.`);
          await refreshSensitivity();
        }} catch (e) {{ show("sens-result", `Error: ${{e.message}}`); }}
      }};

      $("btn-sens-resume").onclick = async () => {{
        try {{
          await api('/api/voice/sensitivity/cancel', {{method:'POST'}});
          show("sens-result", "Quiet mode off — back to normal.");
          await refreshSensitivity();
        }} catch (e) {{ show("sens-result", `Error: ${{e.message}}`); }}
      }};

      // ── Sortable column headers ───────────────────────────────────────
      document.querySelectorAll("#vc-table th[data-sort]").forEach(th => {{
        th.onclick = () => vcSort(th.dataset.sort);
      }});

      (async function init() {{
        try {{
          await refreshSignals();
          await refreshMacros();
          await refreshVoiceCommands();
          await refreshSensitivity();
          setInterval(refreshSensitivity, 5000);  // keep countdown live
          show("toast", "Panel ready.");
        }} catch (e) {{
          show("toast", `Startup error: ${{e.message}}`);
        }}
      }})();
    </script>
  </body>
</html>
"""


def _learn_html() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Learn IR Signal</title>
  </head>
  <body>
    <h1>Learn IR Signal</h1>
    <p>Use the main panel at <a href="/">/</a> for interactive control.</p>
  </body>
</html>
"""


def _api_docs_html() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>RedRat API Docs</title>
    <style>
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; margin: 2rem; color: #222; line-height: 1.45; }
      h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
      h3 { margin-top: 1.4rem; }
      code { background: #f4f4f4; padding: 0.1rem 0.35rem; border-radius: 4px; }
      pre { background: #f8f8f8; border: 1px solid #e5e5e5; padding: 0.75rem; border-radius: 8px; overflow-x: auto; }
      .route { display: flex; gap: 0.6rem; align-items: baseline; margin: 0.5rem 0; }
      .method { font-weight: bold; min-width: 3rem; color: #2563eb; }
      .path { font-family: monospace; }
      .desc { color: #555; margin-left: 1rem; }
      ul { margin: 0.2rem 0 0.8rem 0; }
    </style>
  </head>
  <body>
    <h1>RedRat API Docs</h1>
    <p>Base URL: <code>http://HOST:5000/api</code></p>
    <p>The web control panel is at <code>http://HOST:5000/</code>.</p>

    <h2>Signals</h2>
    <div class="route"><span class="method">GET</span><span class="path">/api/signals</span><span class="desc">List all learned signal names.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/signals/learn</span><span class="desc">Learn a new IR signal. Body: <code>{"name": "signal_name", "timeout_s": 10}</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/signals/send</span><span class="desc">Transmit a signal once. Body: <code>{"name": "signal_name"}</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/signals/send-burst</span><span class="desc">Repeatedly transmit. Body: <code>{"name": "...", "duration_s": 4, "interval_ms": 120}</code>.</span></div>
    <div class="route"><span class="method">DELETE</span><span class="path">/api/signals/&lt;name&gt;</span><span class="desc">Delete a signal.</span></div>
    <div class="route"><span class="method">GET</span><span class="path">/api/signals/export</span><span class="desc">Download <code>ir_codes.yaml</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/signals/import</span><span class="desc">Upload a YAML file to replace all signals.</span></div>

    <h2>Macros</h2>
    <div class="route"><span class="method">GET</span><span class="path">/api/macros</span><span class="desc">List all macros (real, system, and virtual).</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/macros</span><span class="desc">Create or update a macro. Auto-creates a voice command if none maps to it.<br/>
      Body: <code>{"name": "macro_name", "steps": [{"signal": "sig", "delay_ms": 500}]}</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/macros/run</span><span class="desc">Run a macro (async). Body: <code>{"name": "macro_name"}</code>. Works for IR macros and system macros.</span></div>
    <div class="route"><span class="method">DELETE</span><span class="path">/api/macros/&lt;name&gt;</span><span class="desc">Delete a macro. System macros cannot be deleted.</span></div>
    <div class="route"><span class="method">GET</span><span class="path">/api/macros/export</span><span class="desc">Download <code>macros.yaml</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/macros/import</span><span class="desc">Upload a YAML file to replace all macros.</span></div>

    <h3>Virtual Delay Tokens</h3>
    <p>Use these as <code>signal</code> values in macro steps to add pauses without IR:</p>
    <ul>
      <li><code>__delay_1s__</code> — 1 second pause</li>
      <li><code>__delay_10s__</code> — 10 second pause</li>
    </ul>
    <pre>{"name":"movie_on","steps":[{"signal":"projector_power"},{"signal":"__delay_1s__"},{"signal":"receiver_power"}]}</pre>

    <h3>System Macros</h3>
    <p>Code-backed macros that are always available (not stored in <code>macros.yaml</code>):</p>
    <ul>
      <li><code>__quiet_mode__</code> — suppress wake-word sensitivity for 1 hour</li>
      <li><code>__normal_mode__</code> — resume normal wake-word sensitivity</li>
    </ul>
    <p>Run them via <code>POST /api/macros/run</code> or map them to voice commands (built-in by default).</p>

    <h2>Voice Commands</h2>
    <div class="route"><span class="method">GET</span><span class="path">/api/voice/status</span><span class="desc">Returns STT pipeline state (<code>"idle"</code>, <code>"listening"</code>, <code>"recognizing"</code>) plus current sensitivity info.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/voice/speak</span><span class="desc">Speak text synchronously through espeak-ng. Body: <code>{"text": "Hello"}</code>.</span></div>
    <div class="route"><span class="method">GET</span><span class="path">/api/voice/commands</span><span class="desc">List all voice command mappings.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/voice/commands</span><span class="desc">Add a voice command. Body: <code>{"phrase": "turn on projector", "macro": "projector_on"}</code>.</span></div>
    <div class="route"><span class="method">PUT</span><span class="path">/api/voice/commands/&lt;id&gt;</span><span class="desc">Update a voice command. Body: <code>{"phrase": "new phrase"}</code> and/or <code>{"macro": "new_macro"}</code>.</span></div>
    <div class="route"><span class="method">DELETE</span><span class="path">/api/voice/commands/&lt;id&gt;</span><span class="desc">Delete a voice command.</span></div>
    <div class="route"><span class="method">GET</span><span class="path">/api/voice/commands/export</span><span class="desc">Download <code>voice_commands.yaml</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/voice/commands/import</span><span class="desc">Upload a YAML file to replace all voice commands.</span></div>

    <h2>Wake Word Sensitivity ("Quiet Mode")</h2>
    <div class="route"><span class="method">GET</span><span class="path">/api/voice/sensitivity</span><span class="desc">Returns <code>{"state": "normal"|"suppressed", "threshold": 0.8, "suppressed_threshold": 0.99, "remaining_s": null|14399, "default_suppress_s": 3600, "max_suppress_s": 14400}</code>.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/voice/sensitivity/suppress</span><span class="desc">Enter quiet mode. Body (optional): <code>{"seconds": 3600}</code>. Defaults to 1 hour; max 4 hours.</span></div>
    <div class="route"><span class="method">POST</span><span class="path">/api/voice/sensitivity/cancel</span><span class="desc">Cancel quiet mode immediately (revert to normal threshold).</span></div>

    <h2>Devices</h2>
    <div class="route"><span class="method">GET</span><span class="path">/api/devices</span><span class="desc">Enumerate LIRC devices on the system.</span></div>
    <div class="route"><span class="method">GET</span><span class="path">/api/device/diagnostics</span><span class="desc">Run a self-check on the active RedRat device.</span></div>
  </body>
</html>
"""
