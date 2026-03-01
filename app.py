# app.py
import time
import threading
import subprocess
import psutil
import speedtest
from ping3 import ping
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Global state dictionaries to cache data
sys_data = {"cpu_history": [0]*12}
net_data = {"dl_throughput": 0, "ul_throughput": 0}
latency_data = {}
route_data = {}
speedtest_data = {"download": 0, "upload": 0, "ping": 0, "timestamp": "Initialising..."}

# Targets
WEB_TARGETS = {"Google": "google.com", "Facebook": "facebook.com", "YouTube": "youtube.com"}
DNS_TARGETS = {"Google DNS": "8.8.8.8", "Cloudflare DNS": "1.1.1.1"}

def monitor_throughput():
    """Calculates real-time network throughput in Mbps."""
    last_io = psutil.net_io_counters()
    last_time = time.time()
    while True:
        time.sleep(2)
        current_io = psutil.net_io_counters()
        current_time = time.time()
        
        time_diff = current_time - last_time
        dl_mbps = ((current_io.bytes_recv - last_io.bytes_recv) * 8) / (time_diff * 1_000_000)
        ul_mbps = ((current_io.bytes_sent - last_io.bytes_sent) * 8) / (time_diff * 1_000_000)
        
        net_data["dl_throughput"] = round(dl_mbps, 2)
        net_data["ul_throughput"] = round(ul_mbps, 2)
        
        last_io = current_io
        last_time = current_time

def run_speedtest():
    """Runs a speedtest every 1 hour."""
    while True:
        try:
            st = speedtest.Speedtest()
            st.get_best_server()
            dl = round(st.download() / 1_000_000, 2)
            ul = round(st.upload() / 1_000_000, 2)
            p = round(st.results.ping, 2)
            speedtest_data.update({
                "download": dl, "upload": ul, "ping": p, 
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            speedtest_data["timestamp"] = f"Error: {str(e)}"
        time.sleep(3600) # Wait 1 hour

def monitor_latency():
    """Checks latency every 10 seconds."""
    while True:
        for name, host in {**WEB_TARGETS, **DNS_TARGETS}.items():
            try:
                delay = ping(host, unit='ms')
                latency_data[name] = round(delay, 2) if delay else "Timeout"
            except Exception:
                latency_data[name] = "Error (Run as Admin?)"
        time.sleep(10)

def monitor_routing():
    """Runs a Windows tracert every 2 minutes."""
    while True:
        for name, host in WEB_TARGETS.items():
            try:
                # Windows native tracert: -d (no DNS resolution for speed), -h 10 (max 10 hops)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW # Hides console window popup
                
                result = subprocess.check_output(
                    ['tracert', '-d', '-h', '10', host], 
                    startupinfo=startupinfo, 
                    timeout=30
                ).decode('utf-8', errors='ignore')
                route_data[name] = result
            except Exception:
                route_data[name] = "Routing check failed or timed out."
        time.sleep(120)

# Start background threads
threading.Thread(target=monitor_throughput, daemon=True).start()
threading.Thread(target=run_speedtest, daemon=True).start()
threading.Thread(target=monitor_latency, daemon=True).start()
threading.Thread(target=monitor_routing, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/metrics')
def metrics():
    """API endpoint to serve all metrics to the frontend."""
    # CPU
    cpu_pct = psutil.cpu_percent(interval=None)
    sys_data["cpu_history"].pop(0)
    sys_data["cpu_history"].append(cpu_pct)
    
    # RAM
    ram = psutil.virtual_memory()
    
    # Storage (C: Drive)
    disk = psutil.disk_usage('C:\\')
    
    # Temperature (Windows does not expose this natively via psutil without 3rd party drivers)
    core_temp = "N/A (Not supported natively on Windows)"

    return jsonify({
        "system": {
            "cpu": cpu_pct,
            "cpu_history": sys_data["cpu_history"],
            "ram_pct": ram.percent,
            "ram_used": round(ram.used / (1024**3), 2),
            "ram_total": round(ram.total / (1024**3), 2),
            "disk_pct": disk.percent,
            "disk_used": round(disk.used / (1024**3), 2),
            "disk_total": round(disk.total / (1024**3), 2),
            "temperature": core_temp
        },
        "network": net_data,
        "latency": latency_data,
        "route": route_data,
        "speedtest": speedtest_data
    })

if __name__ == '__main__':
    # Run on all interfaces, port 5000
    app.run(host='0.0.0.0', port=5000, threaded=True)
