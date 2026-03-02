import time
import threading
import subprocess
import platform
import psutil
import speedtest
import sqlite3
import re
import random
import ssl
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request

# FIX: Tell Python to ignore strict SSL certificate verification errors
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context

app = Flask(__name__)

# Global state for real-time dashboard
data_store = {
    "system": {"cpu": 0, "ram": 0, "storage": 0, "temp": "N/A"},
    "network_io": {"download_bps": 0, "upload_bps": 0},
    "speedtest_status": "idle", 
    "manual_speedtest": {"dl": 0, "ul": 0, "ping": 0, "time": "Never"},
    "latency": {
        "Google": {"ping": 0, "loss": 0},
        "Facebook": {"ping": 0, "loss": 0},
        "YouTube": {"ping": 0, "loss": 0},
        "Cloudflare DNS": {"ping": 0, "loss": 0},
        "Google DNS": {"ping": 0, "loss": 0}
    },
    "mtr": {"Google": "Initialising route trace...", "Facebook": "Initialising route trace...", "YouTube": "Initialising route trace..."}
}

TARGETS = {
    "Google": "google.com",
    "Facebook": "facebook.com",
    "YouTube": "youtube.com",
    "Cloudflare DNS": "1.1.1.1",
    "Google DNS": "8.8.8.8"
}

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect('homelab.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS speedtest (ts DATETIME DEFAULT CURRENT_TIMESTAMP, dl_mbps REAL, ul_mbps REAL, ping_ms REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS manual_speedtest (ts TEXT, dl_mbps REAL, ul_mbps REAL, ping_ms REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS latency (ts DATETIME DEFAULT CURRENT_TIMESTAMP, target TEXT, ping_ms REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS throughput (ts DATETIME DEFAULT CURRENT_TIMESTAMP, dl_mbps REAL, ul_mbps REAL)''')
    
    try:
        c.execute("ALTER TABLE latency ADD COLUMN loss_pct REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

def log_to_db(query, params=()):
    try:
        conn = sqlite3.connect('homelab.db')
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def load_last_manual_speedtest():
    try:
        conn = sqlite3.connect('homelab.db')
        c = conn.cursor()
        c.execute("SELECT ts, dl_mbps, ul_mbps, ping_ms FROM manual_speedtest ORDER BY ROWID DESC LIMIT 1")
        row = c.fetchone()
        if row:
            data_store["manual_speedtest"]["time"] = row[0]
            data_store["manual_speedtest"]["dl"] = row[1]
            data_store["manual_speedtest"]["ul"] = row[2]
            data_store["manual_speedtest"]["ping"] = row[3]
        conn.close()
    except Exception as e:
        print(f"Failed to load last manual speedtest: {e}")

# --- Network Tools ---
def get_ping_and_loss(host):
    is_win = platform.system().lower() == 'windows'
    param_count = '-n' if is_win else '-c'
    param_timeout = '-w' if is_win else '-W'
    timeout_val = '2000' if is_win else '2'
    
    command = ['ping', param_count, '4', param_timeout, timeout_val, host]
    
    try:
        output = subprocess.check_output(command, stderr=subprocess.STDOUT, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        output = e.output
    except Exception:
        return 0.0, 100.0

    loss_match = re.search(r'(\d+)%\s*(packet\s*)?loss', output)
    loss_pct = float(loss_match.group(1)) if loss_match else 100.0
    
    ping_ms = 0.0
    if loss_pct < 100.0:
        if is_win:
            ping_match = re.search(r'Average\s*=\s*(\d+)ms', output)
            if ping_match: ping_ms = float(ping_match.group(1))
        else:
            ping_match = re.search(r'= [\d\.]+/([\d\.]+)/[\d\.]+/', output)
            if ping_match: ping_ms = float(ping_match.group(1))
            
    return ping_ms, loss_pct

def get_mtr(host):
    is_win = platform.system().lower() == 'windows'
    try:
        if is_win:
            output = subprocess.check_output(['tracert', '-d', '-h', '15', '-w', '500', host], stderr=subprocess.STDOUT, universal_newlines=True)
            lines = output.split('\n')
            cleaned_lines = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith("Tracing route") and not line.startswith("over a maximum"):
                    cleaned_lines.append(line)
            return '\n'.join(cleaned_lines)
        else:
            output = subprocess.check_output(['mtr', '-c', '1', '-r', '-w', host], stderr=subprocess.STDOUT, universal_newlines=True)
            return output
    except subprocess.CalledProcessError as e:
        return f"Routing Error:\n{e.output}"
    except Exception as e:
        return f"Routing Error: {str(e)}"

def perform_speedtest(is_manual=False):
    if data_store["speedtest_status"] == "running":
        return False
        
    data_store["speedtest_status"] = "running"
    try:
        st = speedtest.Speedtest(secure=True)
        st.get_best_server()
        dl_mbps = round(st.download() / 1_000_000, 2)
        ul_mbps = round(st.upload() / 1_000_000, 2)
        ping_ms = round(st.results.ping, 2)
        
        if is_manual:
            current_time = datetime.now().strftime("%H:%M:%S")
            data_store["manual_speedtest"]["dl"] = dl_mbps
            data_store["manual_speedtest"]["ul"] = ul_mbps
            data_store["manual_speedtest"]["ping"] = ping_ms
            data_store["manual_speedtest"]["time"] = current_time
            log_to_db("INSERT INTO manual_speedtest (ts, dl_mbps, ul_mbps, ping_ms) VALUES (?, ?, ?, ?)", 
                      (current_time, dl_mbps, ul_mbps, ping_ms))
        else:
            log_to_db("INSERT INTO speedtest (dl_mbps, ul_mbps, ping_ms) VALUES (?, ?, ?)", (dl_mbps, ul_mbps, ping_ms))
            
        data_store["speedtest_status"] = "idle"
        return True
    except Exception as e:
        print(f"Speedtest failed: {e}")
        data_store["speedtest_status"] = "error"
        time.sleep(5) 
        data_store["speedtest_status"] = "idle"
        return False

# --- Background Threads ---
def background_system_stats():
    last_net = psutil.net_io_counters()
    last_time = time.time()
    db_log_timer = time.time()
    
    dl_accum = 0
    ul_accum = 0
    ticks = 0
    
    while True:
        data_store["system"]["cpu"] = psutil.cpu_percent(interval=None)
        data_store["system"]["ram"] = psutil.virtual_memory().percent
        data_store["system"]["storage"] = psutil.disk_usage('/').percent
        
        try:
            temps = psutil.sensors_temperatures()
            if temps and 'coretemp' in temps:
                data_store["system"]["temp"] = round(temps['coretemp'][0].current, 1)
        except: pass

        current_net = psutil.net_io_counters()
        current_time = time.time()
        time_diff = current_time - last_time
        
        if time_diff > 0:
            dl_bps = (current_net.bytes_recv - last_net.bytes_recv) / time_diff
            ul_bps = (current_net.bytes_sent - last_net.bytes_sent) / time_diff
            data_store["network_io"]["download_bps"] = dl_bps
            data_store["network_io"]["upload_bps"] = ul_bps
            
            dl_accum += (dl_bps * 8) / 1_000_000
            ul_accum += (ul_bps * 8) / 1_000_000
            ticks += 1
            
        last_net = current_net
        last_time = current_time
        
        if current_time - db_log_timer >= 60:
            if ticks > 0:
                log_to_db("INSERT INTO throughput (dl_mbps, ul_mbps) VALUES (?, ?)", (dl_accum/ticks, ul_accum/ticks))
            dl_accum, ul_accum, ticks = 0, 0, 0
            db_log_timer = current_time

        time.sleep(1)

def background_speedtest():
    while True:
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        sleep_seconds = (next_hour - now).total_seconds()
        
        print(f"Next automated PM speedtest scheduled for {next_hour.strftime('%H:%M:%S')} (in {int(sleep_seconds)} seconds)")
        time.sleep(sleep_seconds)
        
        # Add Jitter: Wait a random amount of time between 5 and 45 seconds to bypass Speedtest.net rate limits
        jitter = random.randint(5, 45)
        print(f"Applying {jitter}s random delay (jitter) to avoid Speedtest.net blocks...")
        time.sleep(jitter)
        
        for attempt in range(3):
            print(f"Running automated PM speedtest (Attempt {attempt + 1}/3)...")
            if perform_speedtest(is_manual=False):
                print("Automated PM speedtest successful and logged.")
                break
            else:
                print("Automated PM speedtest failed. Retrying in 60 seconds...")
                time.sleep(60)

def background_latency_mtr():
    while True:
        for name, host in TARGETS.items():
            ping_ms, loss_pct = get_ping_and_loss(host)
            data_store["latency"][name]["ping"] = ping_ms
            data_store["latency"][name]["loss"] = loss_pct
            log_to_db("INSERT INTO latency (target, ping_ms, loss_pct) VALUES (?, ?, ?)", (name, ping_ms, loss_pct))
            
        for name in ["Google", "Facebook", "YouTube"]:
            data_store["mtr"][name] = get_mtr(TARGETS[name])
            
        time.sleep(60)

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def api_data():
    return jsonify(data_store)

@app.route('/api/speedtest/run', methods=['POST'])
def api_run_speedtest():
    if data_store["speedtest_status"] == "idle":
        threading.Thread(target=perform_speedtest, args=(True,), daemon=True).start()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route('/api/history')
def api_history():
    metric = request.args.get('metric', 'speedtest')
    hours = int(request.args.get('hours', 24))
    
    conn = sqlite3.connect('homelab.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    time_limit = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    result = {"labels": [], "data1": [], "data2": []}
    
    if metric == 'speedtest':
        c.execute("SELECT ts as bucket, dl_mbps as dl, ul_mbps as ul FROM speedtest WHERE ts >= ? ORDER BY ts ASC", (time_limit,))
        for row in c.fetchall():
            result["labels"].append(row["bucket"])
            result["data1"].append(round(row["dl"], 2))
            result["data2"].append(round(row["ul"], 2))
            
    elif metric == 'throughput':
        grouping = "'%Y-%m-%d %H:00:00'" if hours > 2 else "'%Y-%m-%d %H:%M:00'"
        c.execute(f"SELECT strftime({grouping}, ts) as bucket, AVG(dl_mbps) as dl, AVG(ul_mbps) as ul FROM throughput WHERE ts >= ? GROUP BY bucket ORDER BY bucket ASC", (time_limit,))
        for row in c.fetchall():
            result["labels"].append(row["bucket"])
            result["data1"].append(round(row["dl"], 2))
            result["data2"].append(round(row["ul"], 2))
            
    elif metric == 'latency':
        grouping = "'%Y-%m-%d %H:00:00'" if hours > 2 else "'%Y-%m-%d %H:%M:00'"
        c.execute(f"SELECT strftime({grouping}, ts) as bucket, AVG(ping_ms) as ping FROM latency WHERE target='Google' AND ts >= ? GROUP BY bucket ORDER BY bucket ASC", (time_limit,))
        for row in c.fetchall():
            result["labels"].append(row["bucket"])
            result["data1"].append(round(row["ping"], 2))
            
    elif metric == 'loss':
        grouping = "'%Y-%m-%d %H:00:00'" if hours > 2 else "'%Y-%m-%d %H:%M:00'"
        c.execute(f"SELECT strftime({grouping}, ts) as bucket, AVG(loss_pct) as loss FROM latency WHERE target='Google' AND ts >= ? GROUP BY bucket ORDER BY bucket ASC", (time_limit,))
        for row in c.fetchall():
            result["labels"].append(row["bucket"])
            result["data1"].append(round(row["loss"], 2))

    conn.close()
    return jsonify(result)

if __name__ == '__main__':
    init_db()
    load_last_manual_speedtest()
    threading.Thread(target=background_system_stats, daemon=True).start()
    threading.Thread(target=background_speedtest, daemon=True).start()
    threading.Thread(target=background_latency_mtr, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
