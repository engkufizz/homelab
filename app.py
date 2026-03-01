# app.py
import sqlite3
import time
import threading
import subprocess
import psutil
import speedtest
from ping3 import ping
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_FILE = "homelab.db"

# Targets
WEB_TARGETS = {"Google": "google.com", "Facebook": "facebook.com", "YouTube": "youtube.com"}
DNS_TARGETS = {"GoogleDNS": "8.8.8.8", "CloudflareDNS": "1.1.1.1"}

def get_db():
    """Creates a new database connection for the current thread."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialises the database tables for historical PM data."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS system_pm (timestamp INTEGER, cpu REAL, ram REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS network_pm (timestamp INTEGER, dl_mbps REAL, ul_mbps REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS latency_pm (timestamp INTEGER, target TEXT, ping_ms REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS speedtest_pm (timestamp INTEGER, dl REAL, ul REAL, ping REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS routing_pm (timestamp INTEGER, target TEXT, report TEXT)''')
    conn.commit()
    conn.close()

# --- Background Workers for Data Collection ---

def worker_system():
    while True:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        conn = get_db()
        conn.execute("INSERT INTO system_pm VALUES (?, ?, ?)", (int(time.time()), cpu, ram))
        conn.commit()
        conn.close()
        time.sleep(10)

def worker_network():
    last_io = psutil.net_io_counters()
    last_time = time.time()
    while True:
        time.sleep(5)
        current_io = psutil.net_io_counters()
        current_time = time.time()
        time_diff = current_time - last_time
        
        dl_mbps = ((current_io.bytes_recv - last_io.bytes_recv) * 8) / (time_diff * 1_000_000)
        ul_mbps = ((current_io.bytes_sent - last_io.bytes_sent) * 8) / (time_diff * 1_000_000)
        
        conn = get_db()
        conn.execute("INSERT INTO network_pm VALUES (?, ?, ?)", (int(current_time), round(dl_mbps, 2), round(ul_mbps, 2)))
        conn.commit()
        conn.close()
        
        last_io = current_io
        last_time = current_time

def worker_latency():
    while True:
        ts = int(time.time())
        conn = get_db()
        for name, host in {**WEB_TARGETS, **DNS_TARGETS}.items():
            try:
                delay = ping(host, unit='ms')
                val = round(delay, 2) if delay else 0
                conn.execute("INSERT INTO latency_pm VALUES (?, ?, ?)", (ts, name, val))
            except:
                pass
        conn.commit()
        conn.close()
        time.sleep(10)

def worker_speedtest():
    while True:
        try:
            st = speedtest.Speedtest()
            st.get_best_server()
            dl = round(st.download() / 1_000_000, 2)
            ul = round(st.upload() / 1_000_000, 2)
            p = round(st.results.ping, 2)
            conn = get_db()
            conn.execute("INSERT INTO speedtest_pm VALUES (?, ?, ?, ?)", (int(time.time()), dl, ul, p))
            conn.commit()
            conn.close()
        except:
            pass
        time.sleep(3600) # Every 1 hour

def worker_routing():
    while True:
        ts = int(time.time())
        conn = get_db()
        for name, host in WEB_TARGETS.items():
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                result = subprocess.check_output(['tracert', '-d', '-h', '10', host], startupinfo=startupinfo, timeout=30).decode('utf-8', errors='ignore')
                # Keep only the latest report per target to save space
                conn.execute("DELETE FROM routing_pm WHERE target=?", (name,))
                conn.execute("INSERT INTO routing_pm VALUES (?, ?, ?)", (ts, name, result))
            except:
                pass
        conn.commit()
        conn.close()
        time.sleep(300) # Every 5 mins

# Start everything
init_db()
threading.Thread(target=worker_system, daemon=True).start()
threading.Thread(target=worker_network, daemon=True).start()
threading.Thread(target=worker_latency, daemon=True).start()
threading.Thread(target=worker_speedtest, daemon=True).start()
threading.Thread(target=worker_routing, daemon=True).start()

# --- API Endpoints ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/current')
def api_current():
    """Fetches the absolute latest data for the top dashboard cards."""
    conn = get_db()
    c = conn.cursor()
    
    # System
    sys_row = c.execute("SELECT * FROM system_pm ORDER BY timestamp DESC LIMIT 1").fetchone()
    disk = psutil.disk_usage('C:\\')
    
    # Network
    net_row = c.execute("SELECT * FROM network_pm ORDER BY timestamp DESC LIMIT 1").fetchone()
    
    # Speedtest
    st_row = c.execute("SELECT * FROM speedtest_pm ORDER BY timestamp DESC LIMIT 1").fetchone()
    
    # Routing
    routes = {row['target']: row['report'] for row in c.execute("SELECT * FROM routing_pm").fetchall()}
    
    conn.close()
    
    return jsonify({
        "system": {
            "cpu": sys_row['cpu'] if sys_row else 0,
            "ram": sys_row['ram'] if sys_row else 0,
            "disk_pct": disk.percent,
            "disk_used": round(disk.used / (1024**3), 2),
            "disk_total": round(disk.total / (1024**3), 2)
        },
        "network": {
            "dl": net_row['dl_mbps'] if net_row else 0,
            "ul": net_row['ul_mbps'] if net_row else 0
        },
        "speedtest": {
            "dl": st_row['dl'] if st_row else 0,
            "ul": st_row['ul'] if st_row else 0,
            "ping": st_row['ping'] if st_row else 0,
            "time": time.strftime("%H:%M", time.localtime(st_row['timestamp'])) if st_row else "N/A"
        },
        "routes": routes
    })

@app.route('/api/history')
def api_history():
    """Aggregates historical data into 12 buckets for MermaidJS charts."""
    hours = int(request.args.get('hours', 1))
    now = int(time.time())
    start_time = now - (hours * 3600)
    bucket_size = (hours * 3600) // 12 # Divide timeframe into 12 points
    
    conn = get_db()
    c = conn.cursor()
    
    # Helper to group data by time buckets
    def get_trend(table, col, target=None):
        query = f"""
            SELECT (timestamp / {bucket_size}) * {bucket_size} as bucket, 
                   AVG({col}) as avg_val 
            FROM {table} 
            WHERE timestamp >= {start_time}
        """
        if target:
            query += f" AND target = '{target}'"
        query += " GROUP BY bucket ORDER BY bucket ASC"
        
        rows = c.execute(query).fetchall()
        
        # Format for Mermaid: X-axis (time strings), Y-axis (values)
        labels = []
        values = []
        for r in rows:
            labels.append(time.strftime("%H:%M", time.localtime(r['bucket'])))
            values.append(round(r['avg_val'], 1))
            
        # Ensure we don't return empty arrays which break Mermaid
        if not labels:
            return ["No Data"], [0]
        return labels, values

    cpu_labels, cpu_vals = get_trend("system_pm", "cpu")
    ram_labels, ram_vals = get_trend("system_pm", "ram")
    dl_labels, dl_vals = get_trend("network_pm", "dl_mbps")
    ping_labels, ping_vals = get_trend("latency_pm", "ping_ms", "Google") # Example trend for Google

    conn.close()
    
    return jsonify({
        "cpu": {"x": cpu_labels, "y": cpu_vals},
        "ram": {"x": ram_labels, "y": ram_vals},
        "throughput": {"x": dl_labels, "y": dl_vals},
        "latency": {"x": ping_labels, "y": ping_vals}
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
