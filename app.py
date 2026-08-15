import os
import sqlite3
import uuid
import webbrowser
from threading import Timer
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify
import qrcode
from io import BytesIO
import socket

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
# Ensure absolute pathing for DB so the app works regardless of CWD or WSGI runner
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'parking.db')

_db_initialized = False

def init_db():
    global _db_initialized
    db = sqlite3.connect(DATABASE)
    try:
        cursor = db.cursor()
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
        
        # Check if scan_time column exists for database migration
        cursor.execute("PRAGMA table_info(slots)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'scan_time' not in columns:
            cursor.execute("ALTER TABLE slots ADD COLUMN scan_time TEXT")

        # Ensure all 100 slots (S1 to S100) exist in database without overwriting existing data
        cursor.execute("SELECT id FROM slots")
        existing_ids = set(row[0] for row in cursor.fetchall())
        
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
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# Automatically initialize database and tables when app is loaded (essential for Gunicorn/Render)
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

@app.route('/qr/<session_id>')
def qr(session_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM slots WHERE session_id = ?', (session_id,))
    slot = cursor.fetchone()
    
    if not slot:
        return "Invalid Session", 404
        
    cursor.execute("SELECT COUNT(*) FROM slots WHERE status = 'vacant'")
    vacant_count = cursor.fetchone()[0]
    
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else 5000
    scan_url = f"http://{local_ip}:{port}/scan/{session_id}"
    return render_template('qr.html', slot=slot, vacant_count=vacant_count, scan_url=scan_url)

@app.route('/qr_image/<session_id>')
def qr_image(session_id):
    # Endpoint to generate actual QR code image
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else 5000
    scan_url = f"http://{local_ip}:{port}/scan/{session_id}"
    
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
    cursor.execute('SELECT * FROM slots WHERE session_id = ?', (session_id,))
    slot = cursor.fetchone()
    
    if not slot:
        return "Invalid or Expired Session", 404
        
    already_scanned = False
    if slot['status'] == 'reserved':
        scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("UPDATE slots SET status = 'occupied', scan_time = ? WHERE session_id = ?", (scan_time, session_id))
        db.commit()
        cursor.execute('SELECT * FROM slots WHERE session_id = ?', (session_id,))
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
    
    # Future Integration:
    # Replace simulated barrier animation with Raspberry Pi GPIO motor control.
    
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
