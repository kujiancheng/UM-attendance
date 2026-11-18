"""
Cloud Runner: Runs both Auto-Attend and Telegram QR Relay Bot concurrently,
with a lightweight built-in HTTP health-check server for cloud platforms (Koyeb/Render).
"""
import subprocess
import sys
import time
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"UM Auto-Attend Cloud Service is running 24/7.")

    def log_message(self, format, *args):
        pass  # Silence HTTP server logs

def start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# Start health check server in background thread for Koyeb health checks
threading.Thread(target=start_health_server, daemon=True).start()

print("=" * 60)
print("  UM Auto-Attend Cloud Suite: Starting 24/7 Service")
print("=" * 60)

# Start auto_attend.py
p1 = subprocess.Popen([sys.executable, "auto_attend.py"])

# Start qr_relay_bot.py
p2 = subprocess.Popen([sys.executable, "qr_relay_bot.py"])

try:
    while True:
        # If either process exits, restart it
        if p1.poll() is not None:
            print("Restarting auto_attend.py...")
            p1 = subprocess.Popen([sys.executable, "auto_attend.py"])
        if p2.poll() is not None:
            print("Restarting qr_relay_bot.py...")
            p2 = subprocess.Popen([sys.executable, "qr_relay_bot.py"])
        time.sleep(10)
except KeyboardInterrupt:
    p1.terminate()
    p2.terminate()
    print("Stopped.")

