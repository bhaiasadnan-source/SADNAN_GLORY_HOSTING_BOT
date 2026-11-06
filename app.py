from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import json
import os
import subprocess
import random
import string
import uuid
from datetime import datetime, timedelta
import sys
import shutil
import threading
import time
import zipfile
import psutil

app = Flask(__name__)
app.secret_key = 'sadnan-super-secret-key-2026'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# ============================================
# PATHS (Railway Volume Support)
# ============================================
DATA_DIR = '/app/data' if os.path.exists('/app/data') else '.'
os.makedirs(DATA_DIR, exist_ok=True)

BOTS_DIR = '/app/bots' if os.path.exists('/app/bots') else 'bots'
os.makedirs(BOTS_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, 'users.json')

CPU_HISTORY = {}
CRASH_COUNT = {}
NET_STATS = {}

# ============================================
# KEEP-ALIVE PING SYSTEM
# ============================================
PUBLIC_URL = os.environ.get('PUBLIC_URL', '').strip()
PING_INTERVAL = int(os.environ.get('PING_INTERVAL', 300))
PING_ENABLED = os.environ.get('PING_ENABLED', 'true').lower() == 'true'
APP_START_TIME = time.time()


# ============================================
# RATE LIMITER
# ============================================

class RateLimiter:
    def check_rate(self, server_id, limit_percent):
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        users = load_users()
        server = None
        for uname, udata in users.items():
            if udata.get('role') == 'admin': continue
            servers = udata.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    server = s
                    break
        if not server or server.get('status') != 'running':
            return False, 0
        pid = server.get('pid')
        if not pid: return False, 0
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=1)
            now = time.time()
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except: pass
        return False, 0


rate_limiter = RateLimiter()


def should_auto_restart(server_id):
    if server_id not in CRASH_COUNT:
        CRASH_COUNT[server_id] = {'count': 0, 'last_crash': time.time()}
    crash_info = CRASH_COUNT[server_id]
    if time.time() - crash_info['last_crash'] < 60:
        if crash_info['count'] >= 3:
            return False
    else:
        crash_info['count'] = 0
    crash_info['count'] += 1
    crash_info['last_crash'] = time.time()
    return True


# ============================================
# HELPERS
# ============================================

def generate_random_password(length=10):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))


def load_users():
    if not os.path.exists(USERS_FILE):
        default = {"jerin": {"password": "hubby", "role": "admin"}}
        save_users(default)
        return default
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        data = {}
    if 'jerin' not in data:
        data['jerin'] = {"password": "hubby", "role": "admin"}
        save_users(data)
    return data


def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def get_server_dir(server_id):
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)
    return server_dir


def check_server_valid(server_id):
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                expiry = s.get('expiry', '')
                if expiry:
                    try:
                        exp_date = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S.%f')
                        if datetime.now() > exp_date:
                            return False, "expired"
                    except: pass
                return True, s
    return False, "deleted"


def get_server_by_id(server_id):
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return s, uname
    return None, None


def create_default_files(server_dir):
    main_py = os.path.join(server_dir, 'main.py')
    if not os.path.exists(main_py):
        with open(main_py, 'w', encoding='utf-8') as f:
            f.write('# SADNAN HOSTING - Default Bot\nimport time\n\nprint("=" * 40)\nprint("Bot is running on SADNAN HOSTING")\nprint("Server is ready!")\nprint("=" * 40)\n\ncounter = 0\nwhile True:\n    counter += 1\n    print(f"[{time.strftime(\'%H:%M:%S\')}] Heartbeat #{counter} | Server active")\n    time.sleep(10)\n')
    req_file = os.path.join(server_dir, 'requirements.txt')
    if not os.path.exists(req_file):
        with open(req_file, 'w', encoding='utf-8') as f:
            f.write('# Add your pip packages here\n')


# ============================================
# BOT RUNNER
# ============================================

def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt', project_folder=None):
    server_dir = get_server_dir(server_id)
    if project_folder:
        work_dir = os.path.join(server_dir, project_folder)
    else:
        work_dir = server_dir
    main_path = os.path.join(work_dir, main_file)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable

    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except: pass

    if not os.path.exists(main_path):
        loc = project_folder if project_folder else "root"
        return None, f"ERROR: {main_file} not found in {loc}!"

    if os.path.exists(log_file):
        try: os.remove(log_file)
        except: open(log_file, 'w').close()

    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    server, _ = get_server_by_id(server_id)
    cpu_limit = server.get('cpu_limit', 80) if server else 80
    log(f"[{ts()}] Checking rate limit...")
    log(f"[{ts()}] Rate limit: {cpu_limit}%")
    if project_folder:
        log(f"[{ts()}] Project: {project_folder}")
    log("")

    if requirements_file and requirements_file.strip():
        req_path = os.path.join(work_dir, requirements_file.strip())
        log(f"[{ts()}] Run: pip install -r {requirements_file}")
        log("")
        if os.path.exists(req_path):
            with open(req_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            lines = [l.strip() for l in content.split('\n') if l.strip() and not l.strip().startswith('#')]
            if lines:
                try:
                    proc = subprocess.Popen(
                        [python_exe, '-m', 'pip', 'install', '-r', os.path.abspath(req_path), '--disable-pip-version-check'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, universal_newlines=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    for line in iter(proc.stdout.readline, ''):
                        if line.strip():
                            log(f"[{ts()}] {line.rstrip()}")
                    proc.wait()
                    log("")
                    if proc.returncode != 0:
                        log(f"[{ts()}] Some packages failed to install")
                    else:
                        log(f"[{ts()}] Requirements installation complete!")
                except Exception as e:
                    log(f"[{ts()}] pip error: {str(e)}")
            else:
                log(f"[{ts()}] {requirements_file} is empty, skipping...")
        else:
            log(f"[{ts()}] {requirements_file} not found, skipping...")
    else:
        log(f"[{ts()}] No requirements file set, skipping...")

    log("")
    log(f"[{ts()}] Run: python {main_file}")
    log(f"[{ts()}] Python {sys.version.split()[0]}")
    log("")

    try:
        main_path_abs = os.path.abspath(main_path)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        proc = subprocess.Popen(
            [python_exe, main_path_abs],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=work_dir,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1, env=env, universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        log(f"[{ts()}] Server marked as running")
        log(f"[{ts()}] PID: {proc.pid}")
        log("")

        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"[{datetime.now().strftime('%I:%M:%S %p')}] CPU Limit! {avg_cpu:.1f}% > {cpu_limit}%")
                    proc.terminate()
                    time.sleep(2)
                    if proc.poll() is None: proc.kill()
                    users = load_users()
                    for uname, udata in users.items():
                        if udata.get('role') == 'admin': continue
                        servers = udata.get('servers', [])
                        if not isinstance(servers, list): continue
                        for s in servers:
                            if isinstance(s, dict) and s.get('server_id') == server_id:
                                s['status'] = 'stopped'
                                s['pid'] = None
                                s['rate_limit_exceeded'] = True
                                s['stopped_by_user'] = False
                                save_users(users)
                                break
                    break

        threading.Thread(target=rate_monitor, daemon=True).start()

        def stream_output():
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    for line in iter(proc.stdout.readline, ''):
                        if line:
                            line = line.rstrip('\n\r')
                            if line:
                                f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {line}\n")
                                f.flush()
            except: pass

        threading.Thread(target=stream_output, daemon=True).start()
        return proc.pid, None
    except Exception as e:
        log(f"[{ts()}] Error: {str(e)}")
        return None, str(e)


def stop_bot_process(pid):
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except: return False


def monitor_bot(server_id, pid):
    while True:
        try:
            if sys.platform == 'win32':
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
                if str(pid) not in result.stdout:
                    break
            else:
                try: os.kill(pid, 0)
                except: break
        except: break
        time.sleep(5)

    server, _ = get_server_by_id(server_id)
    if not server: return
    if server.get('stopped_by_user'): return
    if server.get('rate_limit_exceeded'): return

    if should_auto_restart(server_id):
        time.sleep(3)
        new_pid, error = run_bot(server_id, server.get('main_file', 'main.py'),
                                 server.get('requirements_file', 'requirements.txt'),
                                 server.get('project_folder'))
        if new_pid:
            users = load_users()
            for uname, udata in users.items():
                if udata.get('role') == 'admin': continue
                servers = udata.get('servers', [])
                if not isinstance(servers, list): continue
                for s in servers:
                    if isinstance(s, dict) and s.get('server_id') == server_id:
                        s['status'] = 'running'
                        s['pid'] = new_pid
                        s['started_at'] = str(datetime.now())
                        s['rate_limit_exceeded'] = False
                        s['stopped_by_user'] = False
                        save_users(users)
                        break
            threading.Thread(target=monitor_bot, args=(server_id, new_pid), daemon=True).start()
    else:
        users = load_users()
        for uname, udata in users.items():
            if udata.get('role') == 'admin': continue
            servers = udata.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'stopped'
                    s['pid'] = None
                    save_users(users)
                    return


def get_process_stats(pid):
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.5)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {'cpu_percent': round(cpu, 1), 'ram_mb': round(ram, 1), 'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB"}
    except:
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}


def get_network_stats(psutil_pid):
    try:
        proc = psutil.Process(psutil_pid)
        io = proc.io_counters()
        if io:
            return format_bytes(io.read_bytes / 1024), format_bytes(io.write_bytes / 1024)
    except: pass
    return "0 KB", "0 KB"


def format_bytes(kb):
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024: return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


# ============================================
# SELF-PING WORKER
# ============================================

def self_ping_worker():
    if not PING_ENABLED:
        print("[PING] Self-ping disabled (PING_ENABLED=false)")
        return
    if not PUBLIC_URL:
        print("[PING] No PUBLIC_URL set - self-ping disabled")
        print("[PING] Set PUBLIC_URL env var like: https://your-app.up.railway.app")
        return

    url = PUBLIC_URL.rstrip('/')
    if not url.startswith('http'):
        url = 'https://' + url
    ping_url = f"{url}/ping"

    print(f"[PING] Self-ping started: {ping_url} every {PING_INTERVAL}s")
    time.sleep(30)

    failures = 0
    while True:
        try:
            import requests
            start = time.time()
            r = requests.get(ping_url, timeout=30)
            elapsed = (time.time() - start) * 1000
            if r.status_code == 200:
                failures = 0
                print(f"[PING] OK {r.status_code} - {elapsed:.0f}ms")
            else:
                failures += 1
                print(f"[PING] Bad {r.status_code} - {elapsed:.0f}ms")
        except Exception as e:
            failures += 1
            print(f"[PING] Error: {str(e)[:80]}")
            if failures >= 3:
                time.sleep(60)
                continue
        time.sleep(PING_INTERVAL)


# ============================================
# PING ENDPOINTS
# ============================================

@app.route('/ping')
@app.route('/health')
@app.route('/keepalive')
def ping():
    try:
        users = load_users()
        active = 0
        running = 0
        for uname, udata in users.items():
            if udata.get('role') == 'admin': continue
            for s in udata.get('servers', []):
                if isinstance(s, dict):
                    active += 1
                    if s.get('status') == 'running':
                        running += 1
        return jsonify({
            'status': 'alive',
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'uptime_seconds': int(time.time() - APP_START_TIME),
            'active_servers': active,
            'running_servers': running,
            'ping_enabled': PING_ENABLED,
            'next_ping_in': f"{PING_INTERVAL}s"
        }), 200
    except Exception as e:
        return jsonify({'status': 'alive', 'time': str(datetime.now()), 'error': str(e)}), 200


# ============================================
# PUBLIC API - SERVER CREATE
# ============================================

@app.route('/api/create', methods=['GET'])
def api_create_server():
    username = request.args.get('username', '').strip()
    password = request.args.get('password', '').strip()
    server_type = request.args.get('type', 'python').strip()
    ram = request.args.get('ram', '1GB').strip()
    disk = request.args.get('disk', '1GB').strip()
    cpu_limit = int(request.args.get('cpu', '30'))
    days = int(request.args.get('days', '3'))

    if not password: password = generate_random_password(10)
    if not username: username = f"SADNAN_CODEX{random.randint(10000, 99999)}"
    if len(username) < 3:
        return jsonify({'status': 'error', 'message': 'Username must be at least 3 characters!'}), 400
    if len(password) < 4:
        return jsonify({'status': 'error', 'message': 'Password must be at least 4 characters!'}), 400
    if cpu_limit < 10 or cpu_limit > 100:
        return jsonify({'status': 'error', 'message': 'CPU limit must be between 10 and 100!'}), 400
    if days < 1 or days > 365:
        return jsonify({'status': 'error', 'message': 'Days must be between 1 and 365!'}), 400

    users = load_users()
    if username in users:
        return jsonify({'status': 'error', 'message': f"Username '{username}' already exists!"}), 400

    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=days)
    create_default_files(get_server_dir(server_id))

    host = request.host
    is_local = host.startswith('localhost') or host.startswith('127.0.0.1') or host.startswith('192.168')
    scheme = 'http' if is_local else 'https'
    full_url = f"{scheme}://{host}/{server_id}/login"

    new_server = {
        'server_id': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': full_url,
        'type': server_type,
        'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': 'main.py',
        'requirements_file': 'requirements.txt',
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False,
        'project_folder': None,
    }

    users[username] = {'password': password, 'role': 'user', 'servers': [new_server]}
    save_users(users)

    return jsonify({
        'status': 'success',
        'message': 'Panel created successfully!',
        'username': username, 'password': password,
        'server_type': server_type, 'ram': ram, 'disk': disk,
        'cpu_limit': cpu_limit, 'validity': f'{days} days',
        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
        'full_url': full_url, 'server_id': server_id
    }), 200


# ============================================
# ROUTES
# ============================================

@app.route('/')
def index():
    return render_template('landing.html')


@app.route('/landing')
def landing():
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        ud = users.get(username)
        if ud and ud.get('role') == 'admin' and password == ud.get('password'):
            session['user'] = username
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error="Invalid credentials!")
    return render_template('login.html', error=None)


@app.route('/<server_id>/login', methods=['GET', 'POST'])
def server_login(server_id):
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)

    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        for uname, udata in users.items():
            if udata.get('role') == 'admin': continue
            servers = udata.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    if username == uname and password == udata.get('password'):
                        session['user'] = uname
                        session['role'] = 'user'
                        session['current_server_id'] = server_id
                        return redirect(url_for('server_home', server_id=server_id))
                    else:
                        return render_template('login.html', error="Invalid credentials!")
        return render_template('login.html', error="Invalid login!")
    return render_template('login.html', error=None)


@app.route('/<server_id>/home')
def server_home(server_id):
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    if session.get('current_server_id') != server_id:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))
    valid, result = check_server_valid(server_id)
    if not valid:
        session.clear()
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    return render_template('home.html', username=session['user'], current_server=result)


@app.route('/logout')
def logout():
    server_id = session.get('current_server_id')
    session.clear()
    if server_id:
        return redirect(url_for('server_login', server_id=server_id))
    return redirect(url_for('login'))


# ============================================
# ADMIN
# ============================================

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    users = load_users()
    user_list = []
    total_servers = 0
    total_running = 0
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): servers = []
        running = sum(1 for s in servers if isinstance(s, dict) and s.get('status') == 'running')
        total_servers += len(servers)
        total_running += running
        user_list.append({
            'username': uname, 'password': udata.get('password', ''),
            'servers': servers, 'server_count': len(servers), 'running_count': running
        })
    return render_template('admin.html', users=user_list, total_servers=total_servers, total_running=total_running)


@app.route('/admin/create_server', methods=['POST'])
def create_server():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')
    server_type = data.get('server_type', 'python')
    ram = data.get('ram', '512MB')
    disk = data.get('disk', '1GB')
    expiry_days = int(data.get('expiry_days', 30))
    cpu_limit = int(data.get('cpu_limit', 80))
    if not username or not password:
        return jsonify({'error': 'Required!'}), 400
    users = load_users()
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=expiry_days)
    create_default_files(get_server_dir(server_id))
    new_server = {
        'server_id': server_id, 'link': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': request.host_url.rstrip('/') + f"/{server_id}/home",
        'type': server_type, 'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()), 'expiry': str(expiry_date),
        'main_file': 'main.py', 'requirements_file': 'requirements.txt',
        'cpu_limit': cpu_limit, 'rate_limit_exceeded': False, 'stopped_by_user': False,
        'project_folder': None,
    }
    if username not in users:
        users[username] = {'password': password, 'role': 'user', 'servers': []}
    users[username]['servers'].append(new_server)
    save_users(users)
    return jsonify({'success': True, 'username': username, 'password': password,
                    'login_url': new_server['login_url'],
                    'hostname': new_server['full_link'], 'server_id': server_id})


@app.route('/admin/set_rate_limit/<server_id>', methods=['POST'])
def set_rate_limit(server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    cpu_limit = int(request.get_json().get('cpu_limit', 80))
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['cpu_limit'] = cpu_limit
                save_users(users)
                return jsonify({'success': True, 'cpu_limit': cpu_limit})
    return jsonify({'error': 'Not found'}), 404


@app.route('/admin/delete_server/<username>/<server_id>', methods=['POST'])
def delete_server(username, server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    users = load_users()
    if username in users:
        servers = users[username].get('servers', [])
        if not isinstance(servers, list): servers = []
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('pid'): stop_bot_process(s['pid'])
                try: shutil.rmtree(get_server_dir(server_id))
                except: pass
                break
        users[username]['servers'] = [s for s in servers if isinstance(s, dict) and s.get('server_id') != server_id]
        if len(users[username]['servers']) == 0:
            del users[username]
        save_users(users)
    return jsonify({'success': True})


# ============================================
# BOT API
# ============================================

@app.route('/api/run/<server_id>', methods=['POST'])
def api_run(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    if server.get('status') == 'running': return jsonify({'status': 'error', 'msg': 'Already running!'})
    project_folder = server.get('project_folder')
    if not project_folder:
        return jsonify({'status': 'error', 'msg': 'Please create a NEW PROJECT first and enter it!'})
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False
    pid, error = run_bot(server_id, server.get('main_file', 'main.py'),
                         server.get('requirements_file', 'requirements.txt'),
                         project_folder)
    if pid:
        users = load_users()
        for uname, udata in users.items():
            if udata.get('role') == 'admin': continue
            servers = udata.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'running'
                    s['pid'] = pid
                    s['started_at'] = str(datetime.now())
                    save_users(users)
                    break
        threading.Thread(target=monitor_bot, args=(server_id, pid), daemon=True).start()
        return jsonify({'status': 'success', 'msg': 'Started!'})
    return jsonify({'status': 'error', 'msg': error or 'Failed'})


@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    if server.get('pid'): stop_bot_process(server['pid'])
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['status'] = 'stopped'
                s['pid'] = None
                s['stopped_by_user'] = True
                save_users(users)
                break
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n[{datetime.now().strftime('%I:%M:%S %p')}] Server stopped by user\n")
    except: pass
    return jsonify({'status': 'success', 'msg': 'Stopped'})


@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f: logs = f.read()
        except: logs = ""
    else: logs = ""
    return jsonify({'logs': logs})


@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file):
            try: os.remove(log_file)
            except: open(log_file, 'w').close()
        return jsonify({'status': 'success', 'msg': 'Cleared'})
    except: return jsonify({'status': 'error'}), 500


@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json()
    cmd = data.get('cmd', '')
    server_id = data.get('server_id', '')
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                cwd=get_server_dir(server_id), timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        output = (result.stdout + result.stderr)[:2000]
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] $ {cmd}\n{output}\n")
        return jsonify({'status': 'success', 'output': output})
    except: return jsonify({'status': 'error', 'msg': 'Timeout'})


@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({'cpu': '0%', 'ram': '0 MB', 'uptime': '0h', 'status': 'unknown',
                        'cpu_limit': 80, 'net_in': '0 KB', 'net_out': '0 KB'})
    uptime, cpu, ram, net_in, net_out = "0h 0m", "0%", "0 MB", "0 KB", "0 KB"
    if server.get('status') == 'running' and server.get('pid'):
        stats = get_process_stats(server['pid'])
        cpu = f"{stats['cpu_percent']}%"
        ram = stats['ram_display']
        net_in, net_out = get_network_stats(server['pid'])
    if server.get('status') == 'running' and server.get('started_at'):
        try:
            start = datetime.strptime(server['started_at'], '%Y-%m-%d %H:%M:%S.%f')
            diff = datetime.now() - start
            if diff.days > 0: uptime = f"{diff.days}d {diff.seconds//3600}h"
            else:
                h, m, s = diff.seconds // 3600, (diff.seconds % 3600) // 60, diff.seconds % 60
                uptime = f"{h}h {m}m {s}s"
        except: pass
    return jsonify({'cpu': cpu, 'ram': ram, 'uptime': uptime, 'net_in': net_in, 'net_out': net_out,
                    'cpu_limit': server.get('cpu_limit', 80), 'status': server.get('status', 'stopped')})


@app.route('/api/change_password/<server_id>', methods=['POST'])
def api_change_password(server_id):
    if 'user' not in session: return jsonify({'error': 'Please login first!'}), 403
    data = request.get_json()
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    if not current_password or not new_password: return jsonify({'error': 'All fields are required!'})
    if len(new_password) < 4: return jsonify({'error': 'Password must be at least 4 characters!'})
    users = load_users()
    username = session.get('user')
    if username in users:
        if users[username].get('password') == current_password:
            users[username]['password'] = new_password
            save_users(users)
            return jsonify({'success': True, 'msg': 'Password changed!'})
        return jsonify({'error': 'Current password is incorrect!'})
    return jsonify({'error': 'User not found!'}), 404


@app.route('/api/set_project/<server_id>', methods=['POST'])
def api_set_project(server_id):
    if 'user' not in session:
        return jsonify({'error': 'Please login first!'}), 403
    data = request.get_json()
    project_folder = (data.get('project_folder') or '').strip()
    if project_folder:
        project_path = os.path.join(get_server_dir(server_id), project_folder)
        if not os.path.exists(project_path) or not os.path.isdir(project_path):
            return jsonify({'error': 'Project folder not found!'}), 404
        main_py = os.path.join(project_path, 'main.py')
        if not os.path.exists(main_py):
            return jsonify({'error': 'main.py not found in project! Upload a main.py file first.'}), 404
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['project_folder'] = project_folder if project_folder else None
                save_users(users)
                return jsonify({'success': True, 'project_folder': project_folder})
    return jsonify({'error': 'Server not found'}), 404


# ============================================
# GITHUB DEPLOY
# ============================================

@app.route('/api/github/deploy/<server_id>', methods=['POST'])
def api_github_deploy(server_id):
    data = request.get_json()
    repo_url = data.get('repo_url', '').strip()
    access_token = data.get('access_token', '').strip()
    is_private = data.get('is_private', False)
    if not repo_url:
        return jsonify({'status': 'error', 'msg': 'Repository URL is required!'}), 400
    server_dir = get_server_dir(server_id)
    log_file = os.path.join(server_dir, 'github_deploy.log')
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Starting GitHub deployment...\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Repository: {repo_url}\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Type: {'Private' if is_private else 'Public'}\n")
            f.write("─" * 40 + "\n")
    except: pass

    def deploy_thread():
        try:
            import requests
            def deploy_log(msg):
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {msg}\n")
                        f.flush()
                except: pass
            deploy_log("Preparing deployment...")
            clean_url = repo_url.replace('.git', '').rstrip('/')
            if 'github.com' not in clean_url:
                deploy_log("Error: Only GitHub URLs are supported!")
                return
            parts = clean_url.split('github.com/')[-1].split('/')
            if len(parts) < 2:
                deploy_log("Error: Invalid GitHub URL format!")
                return
            owner = parts[0]
            repo = parts[1]
            branch = 'main'
            if len(parts) > 3 and parts[2] == 'tree':
                branch = parts[3]
            deploy_log(f"Owner: {owner}")
            deploy_log(f"Repository: {repo}")
            deploy_log(f"Branch: {branch}")
            deploy_log("Downloading ZIP archive...")
            api_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
            headers = {'Accept': 'application/vnd.github.v3+json'}
            if is_private and access_token:
                headers['Authorization'] = f'token {access_token}'
                deploy_log("Using access token for authentication")
            response = requests.get(api_url, headers=headers, stream=True, timeout=60)
            if response.status_code == 200:
                deploy_log("Repository downloaded successfully!")
                deploy_log("Extracting files...")
                temp_zip = os.path.join(server_dir, '_github_temp.zip')
                with open(temp_zip, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                try:
                    with zipfile.ZipFile(temp_zip, 'r') as zf:
                        for member in zf.namelist():
                            relative_path = '/'.join(member.split('/')[1:])
                            if not relative_path: continue
                            target_path = os.path.join(server_dir, relative_path)
                            if member.endswith('/'):
                                os.makedirs(target_path, exist_ok=True)
                            else:
                                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                                with zf.open(member) as source, open(target_path, 'wb') as target:
                                    shutil.copyfileobj(source, target)
                                deploy_log(f"  {relative_path}")
                    deploy_log("")
                    deploy_log("Deployment completed successfully!")
                except Exception as e:
                    deploy_log(f"Extraction error: {str(e)}")
                finally:
                    try: os.remove(temp_zip)
                    except: pass
            elif response.status_code == 404:
                deploy_log("Error: Repository not found!")
            elif response.status_code == 401:
                deploy_log("Error: Authentication failed!")
            elif response.status_code == 403:
                deploy_log("Error: Rate limit exceeded!")
            else:
                deploy_log(f"Error: HTTP {response.status_code}")
        except Exception as e:
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Error: {str(e)}\n")
            except: pass

    threading.Thread(target=deploy_thread, daemon=True).start()
    return jsonify({'status': 'success', 'msg': 'Deployment started!'})


@app.route('/api/github/logs/<server_id>')
def api_github_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = f.read()
        except: logs = "> Ready for deployment..."
    else: logs = "> Ready for deployment..."
    return jsonify({'logs': logs})


@app.route('/api/github/clear_logs/<server_id>', methods=['POST'])
def api_github_clear_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    try:
        if os.path.exists(log_file): os.remove(log_file)
        return jsonify({'status': 'success'})
    except: return jsonify({'status': 'error'}), 500


# ============================================
# FILE API
# ============================================

@app.route('/api/files/<server_id>')
def api_files(server_id):
    folder = request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        server_dir = os.path.join(server_dir, folder)
        if not os.path.abspath(server_dir).startswith(os.path.abspath(get_server_dir(server_id))):
            return jsonify({'files': []})
    if not os.path.exists(server_dir): return jsonify({'files': []})
    files = []
    try:
        for item in os.listdir(server_dir):
            item_path = os.path.join(server_dir, item)
            files.append({'name': item, 'is_dir': os.path.isdir(item_path),
                          'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0,
                          'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M')})
    except: pass
    return jsonify({'files': files})


@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    filename = request.args.get('filename', '')
    filepath = os.path.join(get_server_dir(server_id), filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        with open(filepath, 'r', encoding='utf-8') as f: return jsonify({'content': f.read()})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    data = request.get_json()
    filepath = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f: f.write(data.get('content', ''))
    return jsonify({'success': True})


@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    data = request.get_json()
    filepath = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    if os.path.exists(filepath):
        if os.path.isdir(filepath): shutil.rmtree(filepath)
        else: os.remove(filepath)
    return jsonify({'success': True})


@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    folder = request.form.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        server_dir = os.path.join(server_dir, folder)
        os.makedirs(server_dir, exist_ok=True)
    file = request.files['file']
    file.save(os.path.join(server_dir, file.filename))
    return jsonify({'success': True})


@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    data = request.get_json()
    os.makedirs(os.path.join(get_server_dir(server_id), data.get('foldername', '')), exist_ok=True)
    return jsonify({'success': True})


@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    d = request.get_json()
    server_dir = get_server_dir(server_id)
    old_path = os.path.join(server_dir, d.get('old_name', ''))
    new_path = os.path.join(server_dir, d.get('new_name', ''))
    if os.path.exists(old_path):
        os.rename(old_path, new_path)
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/unzip/<server_id>', methods=['POST'])
def api_unzip(server_id):
    data = request.get_json()
    zip_path = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    if os.path.exists(zip_path) and zip_path.endswith('.zip'):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf: zf.extractall(os.path.dirname(zip_path))
            return jsonify({'status': 'success', 'msg': 'Extracted!'})
        except Exception as e: return jsonify({'status': 'error', 'msg': str(e)})
    return jsonify({'status': 'error', 'msg': 'Invalid zip'}), 400


@app.route('/api/get_startup/<server_id>')
def api_get_startup(server_id):
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({'main_file': 'main.py', 'requirements_file': 'requirements.txt', 'files': [], 'project_folder': None})
    project_folder = server.get('project_folder')
    files = []
    if project_folder:
        project_dir = os.path.join(get_server_dir(server_id), project_folder)
        if os.path.exists(project_dir) and os.path.isdir(project_dir):
            try:
                for item in sorted(os.listdir(project_dir)):
                    item_path = os.path.join(project_dir, item)
                    if os.path.isfile(item_path):
                        files.append({'name': item, 'size': os.path.getsize(item_path),
                                      'is_python': item.endswith('.py'),
                                      'is_txt': item.endswith('.txt'),
                                      'is_zip': item.endswith('.zip'),
                                      'is_json': item.endswith('.json'),
                                      'is_js': item.endswith(('.js', '.ts'))})
            except: pass
    return jsonify({'main_file': server.get('main_file', 'main.py'),
                    'requirements_file': server.get('requirements_file', 'requirements.txt'),
                    'files': files, 'project_folder': project_folder})


@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    d = request.get_json()
    users = load_users()
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if 'main_file' in d and d['main_file']:
                    s['main_file'] = d['main_file']
                if 'requirements_file' in d:
                    s['requirements_file'] = d['requirements_file']
                save_users(users)
                return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404


# ============================================
# AUTO-START PING THREAD (gunicorn compatible)
# ============================================

_ping_started = False

def start_ping_if_needed():
    global _ping_started
    if _ping_started: return
    _ping_started = True
    if PING_ENABLED and PUBLIC_URL:
        threading.Thread(target=self_ping_worker, daemon=True).start()
        print("[PING] Background thread started")


@app.before_request
def _ensure_ping_started():
    start_ping_if_needed()


# ============================================
# MAIN
# ============================================

if __name__ == '__main__':
    if PING_ENABLED and PUBLIC_URL:
        threading.Thread(target=self_ping_worker, daemon=True).start()

    port = int(os.environ.get('PORT', 5000))
    print("\n" + "=" * 50)
    print("SADNAN HOSTING - RAILWAY READY")
    print("=" * 50)
    print(f"Landing: http://localhost:{port}")
    print(f"Admin:   http://localhost:{port}/login")
    print(f"API:     http://localhost:{port}/api/create")
    print(f"Ping:    http://localhost:{port}/ping")
    print(f"Login:   jerin / hubby")
    print(f"Data:    {DATA_DIR}")
    print(f"Bots:    {BOTS_DIR}")
    if PUBLIC_URL:
        print(f"Self-Ping: {PUBLIC_URL}/ping every {PING_INTERVAL}s")
    else:
        print("Self-Ping: DISABLED (set PUBLIC_URL to enable)")
    print("=" * 50 + "\n")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)