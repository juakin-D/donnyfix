from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
from datetime import datetime, timedelta
import sqlite3
import hashlib
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DATABASE       = os.path.join(os.path.dirname(__file__), 'bookings.db')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change-this-password')

# PhoneHub Ghana bank details — replace with your real account
BANK_DETAILS = {
    'bank_name':    'GCB Bank Ghana',
    'account_name': 'PhoneHub Ghana Ltd.',
    'account_no':   '1234567890',
    'branch':       'Osu Branch, Accra',
    'sort_code':    '030100',
    'swift':        'GHCBGHAC',
}

# Service fee % applied to installment total
INSTALLMENT_FEE_PERCENT = 4.0

# Plan config: months -> deposit %, label, min device price
PLAN_CONFIG = {
    3:  {'deposit_pct': 40, 'label': '3 Months',  'min_price': 500},
    6:  {'deposit_pct': 30, 'label': '6 Months',  'min_price': 1500},
    12: {'deposit_pct': 20, 'label': '12 Months', 'min_price': 3000},
}


# ─── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL,
        device TEXT NOT NULL, service TEXT NOT NULL, date TEXT NOT NULL,
        notes TEXT, customer_id INTEGER, status TEXT DEFAULT 'Pending',
        FOREIGN KEY (customer_id) REFERENCES customers(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, device_brand TEXT, device_model TEXT,
        membership_tier TEXT DEFAULT 'Standard',
        membership_start TEXT, membership_expiry TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS installment_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        device_name TEXT NOT NULL,
        device_price REAL NOT NULL,
        service_fee REAL NOT NULL DEFAULT 0,
        total_payable REAL NOT NULL,
        deposit_amount REAL NOT NULL,
        balance_remaining REAL NOT NULL,
        monthly_amount REAL NOT NULL,
        plan_months INTEGER NOT NULL,
        payments_made INTEGER DEFAULT 0,
        next_due_date TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        momo_number TEXT, momo_network TEXT,
        bank_name TEXT, bank_reference TEXT,
        status TEXT DEFAULT 'Active',
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        paid_on TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        reference TEXT,
        recorded_by TEXT DEFAULT 'admin',
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (plan_id) REFERENCES installment_plans(id))''')

    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def membership_status(expiry_str):
    if not expiry_str:
        return 'Inactive'
    expiry = datetime.strptime(expiry_str, '%Y-%m-%d')
    today  = datetime.today()
    if expiry < today:
        return 'Expired'
    elif expiry <= today + timedelta(days=30):
        return 'Expiring Soon'
    return 'Active'


def add_one_month(date_str):
    """Add one calendar month to a YYYY-MM-DD string."""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    # Handle month overflow (e.g. Jan 31 -> Feb 28)
    month = d.month + 1
    year  = d.year + (1 if month > 12 else 0)
    month = month if month <= 12 else 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day).strftime('%Y-%m-%d')


def next_due_date():
    return add_one_month(datetime.today().strftime('%Y-%m-%d'))


def calculate_plan(device_price, months):
    cfg         = PLAN_CONFIG[months]
    service_fee = round(device_price * INSTALLMENT_FEE_PERCENT / 100, 2)
    total       = round(device_price + service_fee, 2)
    deposit     = round(total * cfg['deposit_pct'] / 100, 2)
    balance     = round(total - deposit, 2)
    monthly     = round(balance / months, 2)
    return {
        'service_fee': service_fee, 'total': total,
        'deposit': deposit, 'balance': balance, 'monthly': monthly,
        'deposit_pct': cfg['deposit_pct'], 'months': months,
    }


def fmt_ghs(amount):
    try:
        return f"GH\u20B5{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "GH\u20B50.00"


@app.context_processor
def inject_helpers():
    return dict(membership_status=membership_status,
                fmt_ghs=fmt_ghs, PLAN_CONFIG=PLAN_CONFIG)


# ─── AUTH DECORATORS ──────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*a, **kw)
    return w


def customer_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get('customer_id'):
            flash('Please log in to continue.', 'error')
            return redirect(url_for('customer_login'))
        return f(*a, **kw)
    return w


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/booking', methods=['GET', 'POST'])
def booking():
    if request.method == 'POST':
        name    = request.form['name']
        phone   = request.form['phone']
        email   = request.form['email']
        device  = request.form['device']
        service = request.form['service']
        date    = request.form['date']
        notes   = request.form.get('notes', '')
        cid     = session.get('customer_id')
        conn    = get_db()
        conn.execute(
            'INSERT INTO bookings (name,phone,email,device,service,date,notes,customer_id) VALUES (?,?,?,?,?,?,?,?)',
            (name, phone, email, device, service, date, notes, cid))
        conn.commit(); conn.close()
        return render_template('confirmation.html',
            name=name, phone=phone, email=email,
            device=device, service=service, date=date, notes=notes)
    return render_template('booking.html')


# ─── CUSTOMER AUTH ────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('customer_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name  = request.form['name'].strip()
        phone = request.form['phone'].strip()
        email = request.form['email'].strip().lower()
        pw    = request.form['password']
        db    = request.form.get('device_brand', '').strip()
        dm    = request.form.get('device_model', '').strip()
        if len(pw) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return render_template('register.html')
        start  = datetime.today().strftime('%Y-%m-%d')
        expiry = (datetime.today() + timedelta(days=365)).strftime('%Y-%m-%d')
        conn = get_db()
        if conn.execute('SELECT id FROM customers WHERE email=?', (email,)).fetchone():
            flash('An account with that email already exists.', 'error')
            conn.close()
            return render_template('register.html')
        conn.execute(
            "INSERT INTO customers (name,phone,email,password_hash,device_brand,device_model,membership_tier,membership_start,membership_expiry) VALUES (?,?,?,?,?,?,'Standard',?,?)",
            (name, phone, email, hash_password(pw), db, dm, start, expiry))
        conn.commit()
        customer = conn.execute('SELECT * FROM customers WHERE email=?', (email,)).fetchone()
        conn.close()
        session['customer_id']   = customer['id']
        session['customer_name'] = customer['name']
        flash(f'Welcome, {name}! Your 12-month membership is now active.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def customer_login():
    if session.get('customer_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        pw    = request.form['password']
        conn  = get_db()
        c     = conn.execute('SELECT * FROM customers WHERE email=? AND password_hash=?',
                             (email, hash_password(pw))).fetchone()
        conn.close()
        if c:
            session['customer_id']   = c['id']
            session['customer_name'] = c['name']
            flash(f'Welcome back, {c["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('customer_login.html')


@app.route('/logout')
def customer_logout():
    session.pop('customer_id', None)
    session.pop('customer_name', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


# ─── CUSTOMER DASHBOARD ───────────────────────────────────────────────────────

@app.route('/dashboard')
@customer_required
def dashboard():
    conn     = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=?', (session['customer_id'],)).fetchone()
    bookings = conn.execute(
        'SELECT * FROM bookings WHERE customer_id=? ORDER BY date DESC',
        (session['customer_id'],)).fetchall()
    plans = conn.execute(
        'SELECT * FROM installment_plans WHERE customer_id=? ORDER BY created_at DESC',
        (session['customer_id'],)).fetchall()
    conn.close()
    status = membership_status(customer['membership_expiry'])
    return render_template('dashboard.html',
                           customer=customer, bookings=bookings,
                           plans=plans, status=status,
                           now=datetime.today().strftime('%Y-%m-%d'))


# ══════════════════════════════════════════════════════════════════════════════
# INSTALLMENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/installment/apply', methods=['GET', 'POST'])
@customer_required
def installment_apply():
    if request.method == 'POST':
        device_name    = request.form['device_name'].strip()
        device_price   = float(request.form['device_price'])
        plan_months    = int(request.form['plan_months'])
        payment_method = request.form['payment_method']
        notes          = request.form.get('notes', '').strip()
        momo_number    = request.form.get('momo_number', '').strip()
        momo_network   = request.form.get('momo_network', '').strip()
        bank_name      = request.form.get('bank_name', '').strip()
        bank_reference = request.form.get('bank_reference', '').strip()

        if plan_months not in PLAN_CONFIG:
            flash('Invalid plan selected.', 'error')
            return redirect(url_for('installment_apply'))

        cfg = PLAN_CONFIG[plan_months]
        if device_price < cfg['min_price']:
            flash(f'Minimum price for {cfg["label"]} plan is {fmt_ghs(cfg["min_price"])}.', 'error')
            return redirect(url_for('installment_apply'))

        p = calculate_plan(device_price, plan_months)
        conn = get_db()
        conn.execute(
            '''INSERT INTO installment_plans
               (customer_id,device_name,device_price,service_fee,total_payable,
                deposit_amount,balance_remaining,monthly_amount,plan_months,
                next_due_date,payment_method,momo_number,momo_network,
                bank_name,bank_reference,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (session['customer_id'], device_name, device_price,
             p['service_fee'], p['total'], p['deposit'], p['balance'],
             p['monthly'], plan_months, next_due_date(),
             payment_method, momo_number or None, momo_network or None,
             bank_name or None, bank_reference or None, notes or None))
        conn.commit()
        plan_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        flash(f'Plan created! Deposit of {fmt_ghs(p["deposit"])} is due now.', 'success')
        return redirect(url_for('installment_detail', plan_id=plan_id))

    # GET — live preview from query string
    preview = None
    try:
        qp = float(request.args.get('price', 0))
        qm = int(request.args.get('months', 3))
        if qp > 0 and qm in PLAN_CONFIG:
            preview = calculate_plan(qp, qm)
            preview['device_price'] = qp
    except (ValueError, TypeError):
        pass

    return render_template('installment_apply.html',
                           preview=preview,
                           bank_details=BANK_DETAILS,
                           plan_config=PLAN_CONFIG)


@app.route('/installment/<int:plan_id>')
@customer_required
def installment_detail(plan_id):
    conn = get_db()
    plan = conn.execute(
        'SELECT * FROM installment_plans WHERE id=? AND customer_id=?',
        (plan_id, session['customer_id'])).fetchone()
    if not plan:
        conn.close()
        flash('Plan not found.', 'error')
        return redirect(url_for('dashboard'))
    payments   = conn.execute(
        'SELECT * FROM payments WHERE plan_id=? ORDER BY paid_on DESC', (plan_id,)).fetchall()
    conn.close()
    paid_total = sum(p['amount'] for p in payments)
    progress   = round((paid_total / plan['total_payable']) * 100) if plan['total_payable'] else 0
    return render_template('installment_detail.html',
                           plan=plan, payments=payments,
                           paid_total=paid_total, progress=progress,
                           bank_details=BANK_DETAILS)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin'))
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session['admin_username']  = u
            flash('Login successful.', 'success')
            return redirect(url_for('admin'))
        flash('Invalid username or password.', 'error')
    return render_template('admin_login.html')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin():
    search  = request.args.get('search', '').strip()
    service = request.args.get('service', '').strip()
    conn    = get_db()
    q = 'SELECT * FROM bookings WHERE 1=1'
    params = []
    if search:
        q += ' AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)'
        params += [f'%{search}%'] * 3
    if service:
        q += ' AND service=?'; params.append(service)
    q += ' ORDER BY date DESC'
    bookings = conn.execute(q, params).fetchall()
    conn.close()
    return render_template('admin.html', bookings=bookings, search=search, service=service)


@app.route('/admin/delete/<int:booking_id>', methods=['POST'])
@admin_required
def delete_booking(booking_id):
    conn = get_db()
    conn.execute('DELETE FROM bookings WHERE id=?', (booking_id,))
    conn.commit(); conn.close()
    flash('Booking deleted.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/members')
@admin_required
def admin_members():
    search = request.args.get('search', '').strip()
    tier   = request.args.get('tier', '').strip()
    conn   = get_db()
    q = 'SELECT * FROM customers WHERE 1=1'
    params = []
    if search:
        q += ' AND (name LIKE ? OR email LIKE ? OR phone LIKE ?)'
        params += [f'%{search}%'] * 3
    if tier:
        q += ' AND membership_tier=?'; params.append(tier)
    q += ' ORDER BY created_at DESC'
    customers = conn.execute(q, params).fetchall()
    conn.close()
    members = [{
        'id': c['id'], 'name': c['name'], 'phone': c['phone'], 'email': c['email'],
        'device_brand': c['device_brand'], 'device_model': c['device_model'],
        'tier': c['membership_tier'], 'expiry': c['membership_expiry'],
        'status': membership_status(c['membership_expiry']), 'created_at': c['created_at'],
    } for c in customers]
    return render_template('admin_members.html', members=members, search=search, tier=tier)


@app.route('/admin/members/delete/<int:customer_id>', methods=['POST'])
@admin_required
def delete_member(customer_id):
    conn = get_db()
    conn.execute('DELETE FROM customers WHERE id=?', (customer_id,))
    conn.commit(); conn.close()
    flash('Member deleted.', 'success')
    return redirect(url_for('admin_members'))


@app.route('/admin/installments')
@admin_required
def admin_installments():
    status_filter = request.args.get('status', '').strip()
    search        = request.args.get('search', '').strip()
    conn          = get_db()
    q = '''SELECT ip.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email
           FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id WHERE 1=1'''
    params = []
    if status_filter:
        q += ' AND ip.status=?'; params.append(status_filter)
    if search:
        q += ' AND (c.name LIKE ? OR c.email LIKE ? OR ip.device_name LIKE ?)'
        params += [f'%{search}%'] * 3
    q += ' ORDER BY ip.created_at DESC'
    plans  = conn.execute(q, params).fetchall()
    today  = datetime.today().strftime('%Y-%m-%d')
    annotated = []
    for p in plans:
        paid = conn.execute(
            'SELECT COALESCE(SUM(amount),0) FROM payments WHERE plan_id=?', (p['id'],)
        ).fetchone()[0]
        overdue = (p['status'] == 'Active' and p['next_due_date'] < today)
        annotated.append({**dict(p), 'paid_total': paid, 'overdue': overdue})

    total_outstanding = sum(p['balance_remaining'] for p in plans if p['status'] == 'Active')
    active_count      = sum(1 for p in plans if p['status'] == 'Active')
    overdue_count     = sum(1 for p in annotated if p['overdue'])
    completed_count   = sum(1 for p in plans if p['status'] == 'Completed')
    conn.close()

    return render_template('admin_installments.html',
                           plans=annotated,
                           total_outstanding=total_outstanding,
                           active_count=active_count,
                           overdue_count=overdue_count,
                           completed_count=completed_count,
                           status_filter=status_filter,
                           search=search,
                           bank_details=BANK_DETAILS)


@app.route('/admin/installments/<int:plan_id>/record-payment', methods=['POST'])
@admin_required
def record_payment(plan_id):
    amount    = float(request.form['amount'])
    method    = request.form['payment_method']
    reference = request.form.get('reference', '').strip()
    notes     = request.form.get('notes', '').strip()
    paid_on   = request.form.get('paid_on', datetime.today().strftime('%Y-%m-%d'))

    conn = get_db()
    plan = conn.execute('SELECT * FROM installment_plans WHERE id=?', (plan_id,)).fetchone()
    if not plan:
        conn.close()
        flash('Plan not found.', 'error')
        return redirect(url_for('admin_installments'))

    conn.execute(
        'INSERT INTO payments (plan_id,amount,paid_on,payment_method,reference,notes) VALUES (?,?,?,?,?,?)',
        (plan_id, amount, paid_on, method, reference or None, notes or None))

    new_balance       = round(max(plan['balance_remaining'] - amount, 0), 2)
    new_payments_made = plan['payments_made'] + 1
    new_next_due      = next_due_date()
    new_status        = 'Completed' if new_balance <= 0.01 else plan['status']

    conn.execute(
        'UPDATE installment_plans SET balance_remaining=?,payments_made=?,next_due_date=?,status=? WHERE id=?',
        (new_balance, new_payments_made, new_next_due, new_status, plan_id))
    conn.commit(); conn.close()

    if new_status == 'Completed':
        flash(f'Plan #{plan_id} is now FULLY PAID and marked Completed!', 'success')
    else:
        flash(f'Payment of {fmt_ghs(amount)} recorded for plan #{plan_id}.', 'success')
    return redirect(url_for('admin_installments'))


@app.route('/admin/installments/<int:plan_id>/update-status', methods=['POST'])
@admin_required
def update_plan_status(plan_id):
    new_status = request.form['status']
    conn = get_db()
    conn.execute('UPDATE installment_plans SET status=? WHERE id=?', (new_status, plan_id))
    conn.commit(); conn.close()
    flash(f'Plan #{plan_id} updated to {new_status}.', 'success')
    return redirect(url_for('admin_installments'))


# ─── RUN ──────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    app.run(debug=True)