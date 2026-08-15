import os
import sqlite3
import uuid
import webbrowser
from threading import Timer
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify, g, has_app_context
import qrcode
from io import BytesIO
import socket
from dotenv import load_dotenv

load_dotenv()

try:
    import psycopg
    from psycopg.rows import dict_row
    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

app = Flask(__name__)
app.secret_key = 'smart_parking_super_secret'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'parking.db')

def get_db_url():
    url = os.environ.get('DATABASE_URL')
    if url:
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        return url
    return None

def is_postgres():
    return get_db_url() is not None

def adapt_sql(sql):
    if is_postgres():
        return sql.replace('?', '%s')
    else:
        return sql.replace('%s', '?')

class DBCursorWrapper:
    def __init__(self, cursor, is_pg=False):
        self.cursor = cursor
        self.is_pg = is_pg

    def execute(self, sql, params=()):
        adapted_sql = adapt_sql(sql)
        return self.cursor.execute(adapted_sql, params)

    def executemany(self, sql, params_seq):
        adapted_sql = adapt_sql(sql)
        return self.cursor.executemany(adapted_sql, params_seq)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

class DBWrapper:
    def __init__(self, conn, is_pg=False):
        self.conn = conn
        self.is_pg = is_pg

    def cursor(self):
        return DBCursorWrapper(self.conn.cursor(), self.is_pg)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

_db_initialized = False

def init_db():
    global _db_initialized
    db_url = get_db_url()
    
    if db_url:
        if not HAS_PSYCOPG:
            raise RuntimeError("psycopg package is required to connect to PostgreSQL (DATABASE_URL is set).")
        conn = psycopg.connect(db_url, row_factory=dict_row)
        is_pg = True
    else:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        is_pg = False

    db = DBWrapper(conn, is_pg=is_pg)
    try:
        cursor = db.cursor()
        if is_pg:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS slots (
                    id VARCHAR(20) PRIMARY KEY,
                    status VARCHAR(20) NOT NULL,
                    car_number VARCHAR(50),
                    phone_number VARCHAR(50),
                    entry_time VARCHAR(50),
                    exit_time VARCHAR(50),
                    session_id VARCHAR(100),
                    scan_time VARCHAR(50)
                )
            ''')
            cursor.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'slots' AND column_name = 'scan_time'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE slots ADD COLUMN scan_time VARCHAR(50)")
        else:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS slots (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    car_number TEXT,
                    phone_number TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    session_id TEXT,
                    scan_time TEXT
                )
            ''')
            cursor.execute("PRAGMA table_info(slots)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'scan_time' not in columns:
                cursor.execute("ALTER TABLE slots ADD COLUMN scan_time TEXT")

        cursor.execute("SELECT id FROM slots")
        existing_ids = set(row['id'] for row in cursor.fetchall())
        
        missing_slots = [(f'S{i}', 'vacant') for i in range(1, 101) if f'S{i}' not in existing_ids]
        if missing_slots:
            cursor.executemany(
                'INSERT INTO slots (id, status) VALUES (?, ?)',
                missing_slots
            )
        db.commit()
        _db_initialized = True
    finally:
        db.close()

def get_db():
    if not _db_initialized:
        init_db()
    
    if has_app_context():
        if 'db' not in g:
            db_url = get_db_url()
            if db_url:
                if not HAS_PSYCOPG:
                    raise RuntimeError("psycopg package is required to connect to PostgreSQL.")
                conn = psycopg.connect(db_url, row_factory=dict_row)
                g.db = DBWrapper(conn, is_pg=True)
            else:
                conn = sqlite3.connect(DATABASE)
                conn.row_factory = sqlite3.Row
                g.db = DBWrapper(conn, is_pg=False)
        return g.db
    else:
        db_url = get_db_url()
        if db_url:
            if not HAS_PSYCOPG:
                raise RuntimeError("psycopg package is required to connect to PostgreSQL.")
            conn = psycopg.connect(db_url, row_factory=dict_row)
            return DBWrapper(conn, is_pg=True)
        else:
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            return DBWrapper(conn, is_pg=False)

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        if error:
            try:
                db.rollback()
            except Exception:
                pass
        else:
            try:
                db.commit()
            except Exception:
                pass
        db.close()

# Automatically initialize database on module load
init_db()

@app.before_request
def require_login():
    allowed_routes = ['login', 'static', 'qr', 'scan', 'gate_entry', 'qr_image', 'api_dashboard_status']
    if request.endpoint not in allowed_routes and 'admin_logged_in' not in session:
        return redirect(url_for('login'))

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/admin', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'admin' and password == 'admin123':
            session['admin_logged_in'] = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    
    if session.get('admin_logged_in'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM slots ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)')
    slots = cursor.fetchall()
    
    total = len(slots)
    vacant = sum(1 for s in slots if s['status'] == 'vacant')
    reserved = sum(1 for s in slots if s['status'] == 'reserved')
    occupied = sum(1 for s in slots if s['status'] == 'occupied')
    
    return render_template('dashboard.html', slots=slots, total=total, vacant=vacant, reserved=reserved, occupied=occupied)

@app.route('/api/dashboard-status')
def api_dashboard_status():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, status, car_number, session_id, entry_time FROM slots ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)')
    slots = cursor.fetchall()
    
    total = len(slots)
    vacant = sum(1 for s in slots if s['status'] == 'vacant')
    reserved = sum(1 for s in slots if s['status'] == 'reserved')
    occupied = sum(1 for s in slots if s['status'] == 'occupied')
    
    slots_data = [
        {
            'id': s['id'],
            'status': s['status'],
            'car_number': s['car_number'] or '',
            'session_id': s['session_id'] or '',
            'entry_time': s['entry_time'] or ''
        }
        for s in slots
    ]
    
    return jsonify({
        'total': total,
        'vacant': vacant,
        'reserved': reserved,
        'occupied': occupied,
        'slots': slots_data
    })

@app.route('/entry', methods=['POST'])
def entry():
    car_number = request.form.get('car_number')
    phone_number = request.form.get('phone_number')
    if not car_number or not phone_number:
        return redirect(url_for('dashboard'))
        
    db = get_db()
    cursor = db.cursor()
    
    # Check for vacant slot
    cursor.execute("SELECT id FROM slots WHERE status = 'vacant' ORDER BY CAST(SUBSTR(id, 2) AS INTEGER) LIMIT 1")
    slot = cursor.fetchone()
    
    if not slot:
        return "Parking Full", 400
        
    slot_id = slot['id']
    session_id = str(uuid.uuid4())
    entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        UPDATE slots 
        SET status = 'reserved', car_number = ?, phone_number = ?, entry_time = ?, session_id = ?
        WHERE id = ?
    ''', (car_number, phone_number, entry_time, session_id, slot_id))
    db.commit()
    
    return redirect(url_for('qr', session_id=session_id))

def get_base_url():
    # 1. Explicit environment variable override (e.g. APP_BASE_URL, RENDER_EXTERNAL_URL, VERCEL_URL)
    env_url = os.environ.get('APP_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL')
    if env_url:
        if not env_url.startswith('http://') and not env_url.startswith('https://'):
            env_url = f"https://{env_url}"
        return env_url.rstrip('/')

    vercel_url = os.environ.get('VERCEL_URL')
    if vercel_url:
        if not vercel_url.startswith('http://') and not vercel_url.startswith('https://'):
            vercel_url = f"https://{vercel_url}"
        return vercel_url.rstrip('/')

    # 2. Flask request host inspection (supports X-Forwarded-Host and X-Forwarded-Proto)
    if request:
        host = request.host.split(':')[0]
        is_local_host = (
            host in ('localhost', '127.0.0.1') or
            host.startswith('192.168.') or
            host.startswith('10.') or
            host.startswith('172.')
        )
        if os.environ.get('VERCEL') or os.environ.get('RENDER') or 'vercel.app' in request.host or 'onrender.com' in request.host or not is_local_host:
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            forwarded_host = request.headers.get('X-Forwarded-Host', request.host)
            return f"{scheme}://{forwarded_host}".rstrip('/')

    # 3. Fallback for local development network access (mobile on local Wi-Fi)
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if (request and ':' in request.host) else 5000
    return f"http://{local_ip}:{port}"

@app.route('/qr/<session_id>')
def qr(session_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM slots WHERE session_id = ? OR id = ? OR id = ?', (session_id, session_id, f"S{session_id}"))
    slot = cursor.fetchone()
    
    if not slot:
        return "Invalid Session", 404
        
    cursor.execute("SELECT COUNT(*) FROM slots WHERE status = 'vacant'")
    row = cursor.fetchone()
    vacant_count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    base_url = get_base_url()
    scan_url = f"{base_url}/scan/{session_id}"
    return render_template('qr.html', slot=slot, vacant_count=vacant_count, scan_url=scan_url)

@app.route('/qr_image/<session_id>')
def qr_image(session_id):
    # Endpoint to generate actual QR code image
    base_url = get_base_url()
    scan_url = f"{base_url}/scan/{session_id}"
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(scan_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route('/scan/<session_id>')
def scan(session_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM slots WHERE session_id = ? OR id = ? OR id = ?', (session_id, session_id, f"S{session_id}"))
    slot = cursor.fetchone()
    
    if not slot:
        return "Invalid or Expired Session", 404
        
    already_scanned = False
    if slot['status'] == 'reserved':
        scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE slots SET status = 'occupied', scan_time = ? WHERE id = ?", (scan_time, slot['id']))
        db.commit()
        cursor.execute('SELECT * FROM slots WHERE id = ?', (slot['id'],))
        slot = cursor.fetchone()
    elif slot['status'] == 'occupied':
        already_scanned = True
        
    return render_template('scan.html', slot=slot, already_scanned=already_scanned)

@app.route('/gate/entry')
def gate_entry():
    return render_template('gate_entry.html')

@app.route('/exit_search', methods=['POST'])
def exit_search():
    car_number = request.form.get('car_number')
    if not car_number:
        return redirect(url_for('dashboard'))
        
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM slots WHERE car_number = ? AND status = 'occupied'", (car_number,))
    slot = cursor.fetchone()
    
    if not slot:
        return render_template('dashboard.html', error="Car not found or not currently occupied.", **get_dashboard_stats(db))
        
    entry_time = datetime.strptime(slot['entry_time'], '%Y-%m-%d %H:%M:%S')
    duration = datetime.now() - entry_time
    hours = max(1, int(duration.total_seconds() / 3600) + 1)
    amount = hours * 50 # 50 units per hour
    
    return render_template('exit.html', slot=slot, duration=duration, amount=amount)

@app.route('/process_exit', methods=['POST'])
def process_exit():
    slot_id = request.form.get('slot_id')
    if not slot_id:
        return redirect(url_for('dashboard'))
        
    db = get_db()
    cursor = db.cursor()
    
    cursor.execute('''
        UPDATE slots 
        SET status = 'vacant', car_number = NULL, phone_number = NULL, entry_time = NULL, exit_time = NULL, session_id = NULL, scan_time = NULL
        WHERE id = ?
    ''', (slot_id,))
    db.commit()
    
    return redirect(url_for('gate_exit'))

@app.route('/gate/exit')
def gate_exit():
    return render_template('gate_exit.html')

def get_dashboard_stats(db):
    cursor = db.cursor()
    cursor.execute('SELECT * FROM slots ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)')
    slots = cursor.fetchall()
    
    total = len(slots)
    vacant = sum(1 for s in slots if s['status'] == 'vacant')
    reserved = sum(1 for s in slots if s['status'] == 'reserved')
    occupied = sum(1 for s in slots if s['status'] == 'occupied')
    
    return {'slots': slots, 'total': total, 'vacant': vacant, 'reserved': reserved, 'occupied': occupied}

if __name__ == '__main__':
    init_db()
    # Automatically open the browser to the application
    local_url = f"http://{get_local_ip()}:5000/"
    Timer(1.5, lambda: webbrowser.open(local_url)).start()
    # Disable reloader since we call Timer, to prevent dual-opening edge cases
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
