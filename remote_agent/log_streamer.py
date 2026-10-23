"""
Remote Machine Log Streamer Agent
Runs start_server.bat (or python app.py) on the remote machine, prints output
to the local console AND streams every line live over HTTP to your local Live Portal.

Zero external dependencies - uses standard library Python 3 only.
"""
import configparser
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

# Load config or environment defaults
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "agent_config.ini")

def load_settings():
    settings = {
        "portal_url": os.environ.get("PORTAL_URL", "http://localhost:8088/api/ingest"),
        "command": os.environ.get("COMMAND", "start_server.bat"),
        "batch_size": int(os.environ.get("BATCH_SIZE", "5")),
        "batch_interval_sec": float(os.environ.get("BATCH_INTERVAL", "0.2")),
    }
    if os.path.exists(CONFIG_FILE):
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        if "agent" in cfg:
            for k in ("portal_url", "command", "batch_size", "batch_interval_sec"):
                if k in cfg["agent"]:
                    val = cfg["agent"][k]
                    if k in ("batch_size",):
                        settings[k] = int(val)
                    elif k in ("batch_interval_sec",):
                        settings[k] = float(val)
                    else:
                        settings[k] = val
    return settings

SETTINGS = load_settings()
PORTAL_URL = SETTINGS["portal_url"]
COMMAND = SETTINGS["command"]
HOSTNAME = socket.gethostname()

log_queue = queue.Queue(maxsize=10000)
stop_event = threading.Event()

def classify_level(line):
    upper = line.upper()
    if any(k in upper for k in ("ERROR", "EXCEPTION", "TRACEBACK", "FAILED", "CRITICAL")):
        return "ERROR"
    if any(k in upper for k in ("WARN", "WARNING", "REJECTED")):
        return "WARN"
    if any(k in upper for k in ("TWILIO", "CALL_SID", "CALL-", "CALL_")):
        return "TWILIO"
    if any(k in upper for k in ("AUDIT", "TKT-", "INC0", "ASSIGNMENT")):
        return "AUDIT"
    return "INFO"

def send_payload(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        PORTAL_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Agent-Host": HOSTNAME,
            "User-Agent": "FLM-Remote-Log-Streamer/1.0"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        return resp.status == 200

def log_sender_worker():
    """Background worker that batches and sends log lines to the local portal with automatic retry."""
    buffer = []
    last_send = time.time()
    last_heartbeat = time.time()

    while not stop_event.is_set() or not log_queue.empty():
        try:
            item = log_queue.get(timeout=0.2)
            buffer.append(item)
        except queue.Empty:
            pass

        now = time.time()
        # Flush if buffer reaches batch size or time elapsed
        if buffer and (len(buffer) >= SETTINGS["batch_size"] or (now - last_send) >= SETTINGS["batch_interval_sec"]):
            payload = {
                "host": HOSTNAME,
                "logs": buffer
            }
            try:
                send_payload(payload)
                buffer.clear()
                last_send = now
                last_heartbeat = now
            except Exception as e:
                # Connection failed, keep buffer (capped at 5000)
                if len(buffer) > 5000:
                    buffer = buffer[-2000:]
                time.sleep(1.0)

        # Heartbeat if idle for 10 seconds
        if not buffer and (now - last_heartbeat) >= 10.0:
            heartbeat = {
                "host": HOSTNAME,
                "logs": [{
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "level": "INFO",
                    "source": "heartbeat",
                    "message": f"Agent heartbeat ok ({HOSTNAME})"
                }]
            }
            try:
                send_payload(heartbeat)
                last_heartbeat = now
            except Exception:
                pass

def stream_command():
    print("=" * 64)
    print(f"  ★ REMOTE MACHINE LOG STREAMER")
    print("=" * 64)
    print(f"  Target Portal Endpoint : {PORTAL_URL}")
    print(f"  Local Hostname         : {HOSTNAME}")
    print(f"  Command to Monitor     : {COMMAND}")
    print("=" * 64)
    print("  Starting process and streaming live output...\n", flush=True)

    # Start background sender
    sender_thread = threading.Thread(target=log_sender_worker, daemon=True)
    sender_thread.start()

    # Determine command invocation
    # If on Windows and command ends in .bat, use cmd.exe /c
    is_windows = sys.platform.startswith("win")
    if is_windows and COMMAND.lower().endswith(".bat"):
        cmd_args = ["cmd.exe", "/c", COMMAND]
    else:
        cmd_args = COMMAND if isinstance(COMMAND, list) else COMMAND.split()

    try:
        proc = subprocess.Popen(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
    except Exception as e:
        print(f"Failed to start command {COMMAND}: {e}")
        return

    try:
        for line in iter(proc.stdout.readline, ''):
            if not line and proc.poll() is not None:
                break
            line_str = line.rstrip('\r\n')
            if not line_str:
                continue

            # Print to remote console
            print(line_str, flush=True)

            # Queue for streaming
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "level": classify_level(line_str),
                "source": os.path.basename(COMMAND),
                "message": line_str,
            }
            try:
                log_queue.put_nowait(entry)
            except queue.Full:
                pass

        proc.stdout.close()
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping process...")
        proc.terminate()
    finally:
        stop_event.set()
        sender_thread.join(timeout=2.0)
        print("Log streamer terminated.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        PORTAL_URL = sys.argv[1]
    if len(sys.argv) > 2:
        COMMAND = sys.argv[2]
    stream_command()
