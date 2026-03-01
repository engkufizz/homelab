import threading
import time
import psutil
import subprocess
import json
import re
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Global data store
data_store = {
    "system": {"cpu": 0, "ram": 0, "disk": 0},
    "throughput": {"bytes_recv_rate": 0, "bytes_sent_rate": 0},
    "speedtest": {"download": 0, "upload": 0, "ping": 0},
    "latency": {},
    "dns_latency": {},
    "mtr": {}
}

TARGETS = ["google.com", "facebook.com", "youtube.com", "instagram.com", "linkedin.com"]
DNS_TARGETS = {"Google": "8.8.8.8", "Cloudflare": "1.1.1.1", "Quad9": "9.9.9.9"}

def run_cmd(cmd):
    """Executes a shell command and returns the output."""
    try:
        # creationflags=0x08000000 hides the console window on Windows
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        return result.stdout
    except Exception:
        return ""

def parse_windows_ping(output):
    """Parses Windows ping output for latency and loss."""
    latency, loss = 0.0, 100.0
    time_match = re.search(r"Average = (\d+)ms", output)
    if time_match:
        latency = float(time_match.group(1))
    
    loss_match = re.search(r"\((\d+)% loss\)", output)
    if loss_match:
        loss = float(loss_match.group(1))
        
    return latency, loss

def monitor_throughput():
    """Calculates network throughput per second."""
    last_io = psutil.net_io_counters()
    while True:
        time.sleep(2)
        current_io = psutil.net_io_counters()
        
        # Calculate bytes per second, then convert to Megabits per second (Mbps)
        recv_rate = ((current_io.bytes_recv - last_io.bytes_recv) * 8) / 2 / 1_000_000
        sent_rate = ((current_io.bytes_sent - last_io.bytes_sent) * 8) / 2 / 1_000_000
        
        data_store["throughput"]["bytes_recv_rate"] = round(recv_rate, 2)
        data_store["throughput"]["bytes_sent_rate"] = round(sent_rate, 2)
        last_io = current_io

def monitor_latency():
    """Pings targets continuously."""
    while True:
        for target in TARGETS:
            out = run_cmd(['ping', '-n', '2', '-w', '1000', target])
            lat, loss = parse_windows_ping(out)
            data_store["latency"][target] = {"latency": lat, "loss": loss}
            
        for name, ip in DNS_TARGETS.items():
            out = run_cmd(['ping', '-n', '2', '-w', '1000', ip])
            lat, loss = parse_windows_ping(out)
            data_store["dns_latency"][name] = {"latency": lat, "loss": loss}
        time.sleep(5)

def monitor_mtr():
    """Simulates MTR using tracert (Windows)."""
    while True:
        for target in TARGETS:
            # -d (do not resolve addresses), -h 8 (max 8 hops to save time)
            out = run_cmd(['tracert', '-d', '-h', '8', '-w', '500', target])
            hops = []
            for line in out.split('\n'):
                parts = line.split()
                if len(parts) >= 4 and parts[0].isdigit():
                    hop_num = parts[0]
                    # Extract ms if available, otherwise 0
                    ms = [int(p) for p in parts if p.isdigit() and p != hop_num]
                    avg_ms = sum(ms) / len(ms) if ms else 0
                    ip = parts[-1] if not parts[-1].endswith('ms') else "Unknown"
                    hops.append({"hop": hop_num, "ip": ip, "latency": avg_ms})
            data_store["mtr"][target] = hops
        time.sleep(60) # Run every minute

def run_speedtest():
    """Runs speedtest-cli every hour."""
    while True:
        try:
            out = run_cmd(['speedtest-cli', '--json'])
            if out:
                data = json.loads(out)
                data_store["speedtest"] = {
                    "download": round(data['download'] / 1_000_000, 2),
                    "upload": round(data['upload'] / 1_000_000, 2),
                    "ping": round(data['ping'], 2)
                }
        except Exception as e:
            print("Speedtest error:", e)
        time.sleep(3600)

def monitor_system():
    """Monitors CPU, RAM, and Disk."""
    while True:
        data_store["system"]["cpu"] = psutil.cpu_percent(interval=1)
        data_store["system"]["ram"] = psutil.virtual_memory().percent
        data_store["system"]["disk"] = psutil.disk_usage('/').percent
        time.sleep(2)

# Start background threads
threads = [
    threading.Thread(target=monitor_throughput, daemon=True),
    threading.Thread(target=monitor_latency, daemon=True),
    threading.Thread(target=monitor_mtr, daemon=True),
    threading.Thread(target=run_speedtest, daemon=True),
    threading.Thread(target=monitor_system, daemon=True)
]
for t in threads:
    t.start()

@app.route('/api/data')
def get_data():
    return jsonify(data_store)

if __name__ == '__main__':
    # Run on port 5000
    app.run(host='0.0.0.0', port=5000)
