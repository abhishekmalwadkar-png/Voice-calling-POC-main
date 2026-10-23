"""
Live Log Portal Server - Central log receiver and live web dashboard.
Receives live streaming logs from remote machines and displays them in real-time
via Server-Sent Events (SSE) and an interactive web interface.

Standard library only (zero external pip packages).
"""
import collections
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Max log lines held in memory for immediate playback upon web client connection
MAX_BUFFER_SIZE = 5000

# Global shared log store
_log_buffer = collections.deque(maxlen=MAX_BUFFER_SIZE)
_buffer_lock = threading.Lock()
_subscribers = []
_subscribers_lock = threading.Lock()
_remote_agent_info = {
    "status": "offline",
    "hostname": "Unknown",
    "ip": "Unknown",
    "last_seen": 0,
    "last_seen_iso": "Never",
    "total_received": 0,
    "error_count": 0,
    "warning_count": 0,
}

def get_local_ips():
    """Discover local network IPs for convenient remote agent configuration."""
    ips = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        # Fallback via UDP socket trick
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                ips.append(ip)
        except Exception:
            pass
    return ips or ["127.0.0.1"]

def broadcast_log(entry):
    """Deliver a log entry to all connected browser SSE clients."""
    with _buffer_lock:
        _log_buffer.append(entry)
        _remote_agent_info["total_received"] += 1
        level = entry.get("level", "INFO").upper()
        if "ERR" in level or "FAIL" in level:
            _remote_agent_info["error_count"] += 1
        elif "WARN" in level:
            _remote_agent_info["warning_count"] += 1

    msg = f"data: {json.dumps(entry)}\n\n".encode("utf-8")
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            if d in _subscribers:
                _subscribers.remove(d)

class PortalHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Silence default HTTP server console noise
        pass

    def _send(self, code, body, content_type="text/html; charset=utf-8", extra_headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Agent-Host")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, payload):
        self._send(code, json.dumps(payload), "application/json; charset=utf-8")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Agent-Host")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path == "/":
            index_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    content = f.read()
                return self._send(200, content, "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, f"Error loading template: {e}")

        if path == "/static/style.css":
            css_path = os.path.join(os.path.dirname(__file__), "static", "style.css")
            try:
                with open(css_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/css; charset=utf-8")
            except Exception:
                return self._send(404, "Not Found")

        if path == "/static/app.js":
            js_path = os.path.join(os.path.dirname(__file__), "static", "app.js")
            try:
                with open(js_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "application/javascript; charset=utf-8")
            except Exception:
                return self._send(404, "Not Found")

        if path == "/api/status":
            now = time.time()
            is_active = (now - _remote_agent_info["last_seen"]) < 15
            status = "online" if is_active else "offline"
            return self._json(200, {
                **_remote_agent_info,
                "status": status,
                "local_ips": get_local_ips(),
                "buffer_count": len(_log_buffer),
            })

        if path == "/api/logs":
            with _buffer_lock:
                logs = list(_log_buffer)
            return self._json(200, {"logs": logs, "count": len(logs)})

        if path == "/api/stream":
            # Server-Sent Events (SSE) live stream to browser
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            import queue
            client_queue = queue.Queue(maxsize=1000)
            with _subscribers_lock:
                _subscribers.append(client_queue)

            try:
                # Send recent buffer initial burst (up to last 200 lines)
                with _buffer_lock:
                    initial_batch = list(_log_buffer)[-200:]
                if initial_batch:
                    batch_msg = f"event: batch\ndata: {json.dumps(initial_batch)}\n\n".encode("utf-8")
                    self.wfile.write(batch_msg)
                    self.wfile.flush()

                # Stream live events
                while True:
                    try:
                        msg = client_queue.get(timeout=15.0)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        # Keep-alive heartbeat comment
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _subscribers_lock:
                    if client_queue in _subscribers:
                        _subscribers.remove(client_queue)
            return

        self._json(404, {"error": "Not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path == "/api/ingest":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._json(400, {"error": "Invalid JSON"})

            # Update remote agent heartbeat
            remote_ip = self.client_address[0]
            agent_host = self.headers.get("X-Agent-Host") or data.get("host") or remote_ip
            now = time.time()
            iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

            _remote_agent_info["status"] = "online"
            _remote_agent_info["hostname"] = agent_host
            _remote_agent_info["ip"] = remote_ip
            _remote_agent_info["last_seen"] = now
            _remote_agent_info["last_seen_iso"] = iso_now

            # Accept single log item or batch
            items = data.get("logs") if isinstance(data.get("logs"), list) else [data]
            for item in items:
                if not isinstance(item, dict) or not item.get("message"):
                    continue
                entry = {
                    "id": f"{int(now * 1000)}-{len(_log_buffer)}",
                    "timestamp": item.get("timestamp") or iso_now,
                    "level": item.get("level") or "INFO",
                    "source": item.get("source") or "start_server.bat",
                    "host": agent_host,
                    "message": str(item.get("message")),
                    "tag": item.get("tag") or "server",
                }
                broadcast_log(entry)

            return self._json(200, {"status": "ok", "ingested": len(items)})

        if path == "/api/clear":
            with _buffer_lock:
                _log_buffer.clear()
                _remote_agent_info["total_received"] = 0
                _remote_agent_info["error_count"] = 0
                _remote_agent_info["warning_count"] = 0
            # Broadcast clear event to browsers
            msg = b"event: clear\ndata: {}\n\n"
            with _subscribers_lock:
                for q in _subscribers:
                    try:
                        q.put_nowait(msg)
                    except Exception:
                        pass
            return self._json(200, {"status": "cleared"})

        self._json(404, {"error": "Not found"})

def run_server(port=8088, use_tunnel=False):
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, PortalHandler)
    httpd.daemon_threads = True

    local_ips = get_local_ips()
    primary_ip = local_ips[0] if local_ips else "localhost"

    print("=" * 68)
    print("  ★ FLM LIVE LOG PORTAL (REMOTE MONITOR)")
    print("=" * 68)
    print(f"  Local Browser Dashboard : http://localhost:{port}")
    print(f"  LAN Portal Address      : http://{primary_ip}:{port}")
    print(f"  Remote Ingest Endpoint  : http://{primary_ip}:{port}/api/ingest")
    print("-" * 68)

    tunnel_url = None
    if use_tunnel:
        cloudflared_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cloudflared.exe"))
        if os.path.exists(cloudflared_path):
            print("  Starting Cloudflare Tunnel for internet/cross-network access...", flush=True)
            try:
                proc = subprocess.Popen([cloudflared_path, "tunnel", "--url", f"http://localhost:{port}"],
                                        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
                deadline = time.time() + 30
                while time.time() < deadline:
                    line = proc.stderr.readline() or ""
                    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if m:
                        tunnel_url = m.group(0)
                        threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()
                        print(f"  Public Tunnel URL       : {tunnel_url}")
                        print(f"  Remote Ingest (Public)  : {tunnel_url}/api/ingest")
                        break
            except Exception as e:
                print(f"  Could not start tunnel: {e}")
        else:
            print("  cloudflared.exe not found; running in local/LAN mode.")

    print("=" * 68)
    print("  Ready! Logs from the remote machine will stream live into your browser.")
    print("  Press Ctrl+C in this window to stop the portal.\n", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down portal server...")
        httpd.server_close()

if __name__ == "__main__":
    port = 8088
    use_tunnel = False
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg == "--tunnel":
            use_tunnel = True
    run_server(port, use_tunnel)
