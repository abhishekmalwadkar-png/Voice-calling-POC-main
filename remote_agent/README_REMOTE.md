# Remote Machine Log Streamer Guide

This folder allows your remote Windows machine to stream its console output (`start_server.bat`) in real-time directly into the **Live Log Portal** on your local machine.

---

## Quick Setup (3 Steps)

### Step 1: Start the Portal on your local machine
On your local machine, run:
```cmd
start_portal.bat
```
The browser will open at `http://localhost:8088`. Note your local IP displayed in the portal header or terminal.

---

### Step 2: Configure the Remote Agent
On the remote machine, open `remote_agent\agent_config.ini`:
```ini
[agent]
portal_url = http://<YOUR_LOCAL_IP>:8088/api/ingest
command = start_server.bat
```

> **Tip (Different Networks / Over Internet):**
> If your remote machine is in the cloud (AWS/Azure) or cannot reach your local IP directly, start your local portal with `--tunnel`:
> ```cmd
> python portal\portal_server.py --tunnel
> ```
> It will generate a public Cloudflare URL (e.g. `https://xxxx.trycloudflare.com/api/ingest`). Put that URL into `agent_config.ini`!

---

### Step 3: Run on Remote Machine
Double click:
```cmd
run_remote.bat
```
`start_server.bat` will start, display its logs normally on the remote machine, and stream everything live to your local portal!
