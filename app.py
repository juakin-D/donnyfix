from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timedelta, timezone
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import smtplib
import os
import re
import logging
import requests as http_req
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1-hour token validity

csrf    = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
)

ADMIN_SESSION_TIMEOUT = timedelta(minutes=int(os.environ.get('ADMIN_TIMEOUT_MINUTES', 30)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

DATABASE              = os.path.join(os.path.dirname(__file__), 'bookings.db')
ADMIN_USERNAME        = os.environ.get('ADMIN_USERNAME', 'admin')
_admin_pw_raw         = os.environ.get('ADMIN_PASSWORD', 'change-this-password')
ADMIN_PASSWORD_HASH   = generate_password_hash(_admin_pw_raw)
del _admin_pw_raw

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
    return generate_password_hash(p)


def verify_password(stored, supplied):
    return check_password_hash(stored, supplied)


_GH_PHONE_RE = re.compile(r'^(?:\+233|0)[2-9]\d{8}$')

def valid_gh_phone(phone):
    return bool(_GH_PHONE_RE.match(phone.strip().replace(' ', '').replace('-', '')))


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


# ─── EMAIL ────────────────────────────────────────────────────────────────────

MAIL_HOST = os.environ.get('MAIL_HOST', 'smtp.gmail.com')
MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
MAIL_USER = os.environ.get('MAIL_USER', '')
MAIL_PASS = os.environ.get('MAIL_PASS', '')
MAIL_FROM = os.environ.get('MAIL_FROM', 'noreply@phonehubghana.com')


def send_email(to, subject, html_body):
    if not MAIL_USER or not MAIL_PASS:
        logger.warning('send_email skipped — MAIL_USER/MAIL_PASS not configured')
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'PhoneHub Ghana <{MAIL_FROM}>'
        msg['To']      = to
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT) as s:
            s.starttls()
            s.login(MAIL_USER, MAIL_PASS)
            s.sendmail(MAIL_FROM, to, msg.as_string())
        logger.info('Email sent to %s — %s', to, subject)
        return True
    except Exception as exc:
        logger.error('Email to %s failed: %s', to, exc)
        return False


# ─── SMS (Africa's Talking) ───────────────────────────────────────────────────

AT_API_KEY   = os.environ.get('AT_API_KEY', '')
AT_USERNAME  = os.environ.get('AT_USERNAME', 'sandbox')
AT_SENDER_ID = os.environ.get('AT_SENDER_ID', 'PhoneHub')


def _normalize_gh_phone(phone):
    p = phone.strip().replace(' ', '').replace('-', '')
    if p.startswith('0'):
        return '+233' + p[1:]
    if not p.startswith('+'):
        return '+233' + p
    return p


def send_sms(phone, message):
    if not AT_API_KEY:
        logger.warning('send_sms skipped — AT_API_KEY not configured')
        return False
    normalized = _normalize_gh_phone(phone)
    try:
        resp = http_req.post(
            'https://api.africastalking.com/version1/messaging',
            headers={'apiKey': AT_API_KEY, 'Accept': 'application/json'},
            data={'username': AT_USERNAME, 'to': normalized,
                  'message': message, 'from': AT_SENDER_ID},
            timeout=10,
        )
        if resp.status_code == 201:
            logger.info('SMS sent to %s', normalized)
            return True
        logger.error('SMS to %s failed — HTTP %s: %s', normalized, resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.error('SMS to %s failed: %s', normalized, exc)
        return False


# ─── PDF RECEIPTS ─────────────────────────────────────────────────────────────

_C_GREEN = colors.HexColor('#006B3F')
_C_GOLD  = colors.HexColor('#FCD116')
_C_DARK  = colors.HexColor('#111008')
_C_GRAY  = colors.HexColor('#4A4740')
_C_LGRAY = colors.HexColor('#E8E4DC')
_C_BG    = colors.HexColor('#F7F5F0')


def _pdf_header(styles):
    return [
        Paragraph('PhoneHub Ghana',
                  ParagraphStyle('ph', parent=styles['Normal'], fontSize=20,
                                 fontName='Helvetica-Bold', textColor=_C_GREEN)),
        Paragraph('Osu Oxford Street, Accra · +233 (0) 302 000 000 · hello@phonehubghana.com',
                  ParagraphStyle('phs', parent=styles['Normal'], fontSize=8, textColor=_C_GRAY)),
        Spacer(1, 3*mm),
        HRFlowable(width='100%', thickness=2, color=_C_GOLD, spaceAfter=8),
    ]


def _kv_table(rows, col_w=(45*mm, 115*mm)):
    t = Table(rows, colWidths=list(col_w))
    t.setStyle(TableStyle([
        ('FONTNAME',     (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE',     (0, 0), (-1, -1), 10),
        ('TEXTCOLOR',    (0, 0), (0, -1), _C_GRAY),
        ('TEXTCOLOR',    (1, 0), (1, -1), _C_DARK),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ('TOPPADDING',   (0, 0), (-1, -1), 2),
    ]))
    return t


def _section_label(text, styles):
    return Paragraph(text, ParagraphStyle('sl', parent=styles['Normal'],
        fontSize=8, fontName='Helvetica-Bold', textColor=_C_GREEN,
        textTransform='uppercase', spaceBefore=6, spaceAfter=4))


def generate_booking_receipt_pdf(booking):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    story  = _pdf_header(styles)

    story.append(Paragraph(f'Booking Receipt — BK-{booking["id"]:05d}',
                           ParagraphStyle('title', parent=styles['Normal'],
                               fontSize=16, fontName='Helvetica-Bold',
                               textColor=_C_DARK, spaceAfter=6)))
    story.append(_kv_table([
        ['Issued',       datetime.today().strftime('%d %B %Y')],
        ['Booking Date', booking['date']],
        ['Status',       booking['status'] or 'Pending'],
    ]))
    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=_C_LGRAY, spaceAfter=4))

    story.append(_section_label('Customer', styles))
    story.append(_kv_table([
        ['Name',  booking['name']],
        ['Phone', booking['phone']],
        ['Email', booking['email']],
    ]))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=_C_LGRAY, spaceAfter=4))

    story.append(_section_label('Service Details', styles))
    story.append(_kv_table([
        ['Device',  booking['device']],
        ['Service', booking['service']],
        ['Notes',   booking['notes'] or '—'],
    ]))
    story.append(Spacer(1, 12*mm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=_C_GOLD, spaceAfter=6))
    story.append(Paragraph('Thank you for choosing PhoneHub Ghana. Please keep this receipt.',
                           ParagraphStyle('ft', parent=styles['Normal'],
                               fontSize=8, textColor=_C_GRAY, alignment=TA_CENTER)))
    doc.build(story)
    buf.seek(0)
    return buf


def generate_payment_receipt_pdf(plan, payment, customer_name):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    story  = _pdf_header(styles)

    story.append(Paragraph(f'Payment Receipt — PAY-{payment["id"]:05d}',
                           ParagraphStyle('title', parent=styles['Normal'],
                               fontSize=16, fontName='Helvetica-Bold',
                               textColor=_C_DARK, spaceAfter=6)))
    story.append(_kv_table([
        ['Plan #',      f'IP-{plan["id"]:04d}'],
        ['Customer',    customer_name],
        ['Date Paid',   payment['paid_on']],
        ['Issued',      datetime.today().strftime('%d %B %Y')],
    ]))
    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=_C_LGRAY, spaceAfter=4))

    story.append(_section_label('Installment Plan', styles))
    story.append(_kv_table([
        ['Device',            plan['device_name']],
        ['Total Payable',     fmt_ghs(plan['total_payable'])],
        ['Plan Duration',     f'{plan["plan_months"]} months'],
        ['Payments Made',     f'{plan["payments_made"]} of {plan["plan_months"]}'],
        ['Balance Remaining', fmt_ghs(plan['balance_remaining'])],
    ]))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=_C_LGRAY, spaceAfter=4))

    # Highlighted amount box
    story.append(_section_label('Payment', styles))
    amt_table = Table([['Amount Paid', fmt_ghs(payment['amount'])]], colWidths=[45*mm, 115*mm])
    amt_table.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (0, 0),  10),
        ('FONTSIZE',      (1, 0), (1, 0),  16),
        ('TEXTCOLOR',     (0, 0), (0, 0),  _C_GRAY),
        ('TEXTCOLOR',     (1, 0), (1, 0),  _C_GREEN),
        ('BACKGROUND',    (0, 0), (-1, -1), _C_BG),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 10),
        ('BOX',           (0, 0), (-1, -1), 0.5, _C_LGRAY),
    ]))
    story.append(amt_table)
    story.append(Spacer(1, 4*mm))
    story.append(_kv_table([
        ['Method',    payment['payment_method']],
        ['Reference', payment['reference'] or '—'],
        ['Notes',     payment['notes'] or '—'],
    ]))
    story.append(Spacer(1, 12*mm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=_C_GOLD, spaceAfter=6))
    story.append(Paragraph('This receipt confirms your installment payment. Thank you for being a PhoneHub member.',
                           ParagraphStyle('ft', parent=styles['Normal'],
                               fontSize=8, textColor=_C_GRAY, alignment=TA_CENTER)))
    doc.build(story)
    buf.seek(0)
    return buf


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
        last = session.get('admin_last_activity')
        if last:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            if elapsed > ADMIN_SESSION_TIMEOUT:
                session.clear()
                flash('Your session expired. Please log in again.', 'error')
                return redirect(url_for('admin_login'))
        session['admin_last_activity'] = datetime.now(timezone.utc).isoformat()
        return f(*a, **kw)
    return w


CUSTOMER_SESSION_TIMEOUT = timedelta(minutes=int(os.environ.get('CUSTOMER_TIMEOUT_MINUTES', 60)))

def customer_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get('customer_id'):
            flash('Please log in to continue.', 'error')
            return redirect(url_for('customer_login'))
        last = session.get('customer_last_activity')
        if last:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            if elapsed > CUSTOMER_SESSION_TIMEOUT:
                session.clear()
                flash('Your session expired. Please log in again.', 'error')
                return redirect(url_for('customer_login'))
        session['customer_last_activity'] = datetime.now(timezone.utc).isoformat()
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
        name    = request.form.get('name', '').strip()
        phone   = request.form.get('phone', '').strip()
        email   = request.form.get('email', '').strip().lower()
        device  = request.form.get('device', '').strip()
        service = request.form.get('service', '').strip()
        date    = request.form.get('date', '').strip()
        notes   = request.form.get('notes', '').strip()
        cid     = session.get('customer_id')

        errors = []
        if not name or len(name) > 100:
            errors.append('Please enter your full name (max 100 characters).')
        if not valid_gh_phone(phone):
            errors.append('Enter a valid Ghanaian phone number (e.g. 024 000 0000).')
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            errors.append('Enter a valid email address.')
        if not device or len(device) > 100:
            errors.append('Please enter your device (max 100 characters).')
        if not service:
            errors.append('Please select a service.')
        try:
            bdate = datetime.strptime(date, '%Y-%m-%d')
            today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
            if bdate < today:
                errors.append('Booking date cannot be in the past.')
            if (bdate - today).days > 365:
                errors.append('Booking date cannot be more than a year away.')
        except ValueError:
            errors.append('Invalid date.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('booking.html')

        conn = get_db()
        conn.execute(
            'INSERT INTO bookings (name,phone,email,device,service,date,notes,customer_id) VALUES (?,?,?,?,?,?,?,?)',
            (name, phone, email, device, service, date, notes, cid))
        conn.commit()
        booking_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        send_email(email, 'Booking Confirmed — PhoneHub Ghana', f"""
        <p>Hi {name},</p>
        <p>Your repair booking is confirmed.</p>
        <ul>
          <li><b>Device:</b> {device}</li>
          <li><b>Service:</b> {service}</li>
          <li><b>Date:</b> {date}</li>
        </ul>
        <p>We'll see you at our Osu Oxford Street location. Call us on +233 (0) 302 000 000 with any questions.</p>
        <p>— PhoneHub Ghana Team</p>
        """)
        return render_template('confirmation.html',
            name=name, phone=phone, email=email,
            device=device, service=service, date=date, notes=notes,
            booking_id=booking_id)
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
        if not name or len(name) > 100:
            flash('Please enter your full name (max 100 characters).', 'error')
            return render_template('register.html')
        if not valid_gh_phone(phone):
            flash('Enter a valid Ghanaian phone number (e.g. 024 000 0000).', 'error')
            return render_template('register.html')
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
        send_email(email, 'Welcome to PhoneHub Ghana!', f"""
        <p>Hi {name},</p>
        <p>Your PhoneHub Ghana account is live and your <b>12-month Standard Membership</b> starts today.</p>
        <p>You can now book repairs, apply for installment plans, and track your device history from your dashboard.</p>
        <p>Membership expires: <b>{expiry}</b></p>
        <p>— PhoneHub Ghana Team</p>
        """)
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
        c = conn.execute('SELECT * FROM customers WHERE email=?', (email,)).fetchone()
        conn.close()
        if c and verify_password(c['password_hash'], pw):
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

        if device_price <= 0 or device_price > 100_000:
            flash('Device price must be between GH₵1 and GH₵100,000.', 'error')
            return redirect(url_for('installment_apply'))

        cfg = PLAN_CONFIG[plan_months]
        if device_price < cfg['min_price']:
            flash(f'Minimum price for {cfg["label"]} plan is {fmt_ghs(cfg["min_price"])}.', 'error')
            return redirect(url_for('installment_apply'))

        if payment_method == 'MoMo' and not momo_number:
            flash('MoMo number is required when paying by Mobile Money.', 'error')
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
@limiter.limit('5 per minute', methods=['POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin'))
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        if u == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, p):
            session['admin_logged_in']    = True
            session['admin_username']     = u
            session['admin_last_activity'] = datetime.now(timezone.utc).isoformat()
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
    logger.warning('Admin %s deleted booking #%d', session.get('admin_username'), booking_id)
    flash('Booking deleted.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/bookings/<int:booking_id>/status', methods=['POST'])
@admin_required
def update_booking_status(booking_id):
    new_status = request.form.get('status', '')
    if new_status not in ('Pending', 'In Progress', 'Complete', 'Cancelled'):
        flash('Invalid status.', 'error')
        return redirect(url_for('admin'))
    conn = get_db()
    booking = conn.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
    conn.execute('UPDATE bookings SET status=? WHERE id=?', (new_status, booking_id))
    conn.commit(); conn.close()
    if booking and new_status == 'Complete':
        send_email(booking['email'], 'Your repair is ready — PhoneHub Ghana', f"""
        <p>Hi {booking['name']},</p>
        <p>Great news — your <b>{booking['device']}</b> ({booking['service']}) is complete and ready for collection.</p>
        <p>Visit us at Osu Oxford Street or call +233 (0) 302 000 000 to arrange pickup.</p>
        <p>— PhoneHub Ghana Team</p>
        """)
    flash(f'Booking #{booking_id} marked as {new_status}.', 'success')
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
    logger.warning('Admin %s deleted member #%d', session.get('admin_username'), customer_id)
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
    paid_map = {row[0]: row[1] for row in conn.execute(
        'SELECT plan_id, COALESCE(SUM(amount),0) FROM payments GROUP BY plan_id'
    ).fetchall()}
    annotated = []
    for p in plans:
        paid    = paid_map.get(p['id'], 0)
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
    try:
        amount = float(request.form['amount'])
    except (ValueError, KeyError):
        flash('Invalid payment amount.', 'error')
        return redirect(url_for('admin_installments'))

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

    if amount <= 0:
        conn.close()
        flash('Payment amount must be greater than zero.', 'error')
        return redirect(url_for('admin_installments'))
    if amount > plan['balance_remaining'] + 0.01:
        conn.close()
        flash(f'Amount exceeds remaining balance of {fmt_ghs(plan["balance_remaining"])}. '
              f'Use the exact balance to close the plan.', 'error')
        return redirect(url_for('admin_installments'))

    conn.execute(
        'INSERT INTO payments (plan_id,amount,paid_on,payment_method,reference,notes) VALUES (?,?,?,?,?,?)',
        (plan_id, amount, paid_on, method, reference or None, notes or None))

    new_balance       = round(max(plan['balance_remaining'] - amount, 0), 2)
    new_payments_made = plan['payments_made'] + 1
    new_next_due      = add_one_month(plan['next_due_date'])
    new_status        = 'Completed' if new_balance <= 0.01 else plan['status']

    conn.execute(
        'UPDATE installment_plans SET balance_remaining=?,payments_made=?,next_due_date=?,status=? WHERE id=?',
        (new_balance, new_payments_made, new_next_due, new_status, plan_id))
    payment_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    logger.info('Admin %s recorded payment of %s for plan #%d (new balance: %s)',
                session.get('admin_username'), fmt_ghs(amount), plan_id, fmt_ghs(new_balance))
    conn.close()

    # SMS confirmation to customer
    _name_parts = plan['customer_name'].split() if 'customer_name' in plan.keys() else []
    first = _name_parts[0] if _name_parts else 'Customer'
    if new_status == 'Completed':
        send_sms(plan['customer_phone'] if 'customer_phone' in plan.keys() else '',
                 f"Hi {first}, your PhoneHub Ghana installment for {plan['device_name']} "
                 f"is now FULLY PAID! Thank you. Call 0302000000 for your receipt.")
        flash(f'Plan #{plan_id} fully paid — marked Completed. Receipt: /receipt/payment/{payment_id}', 'success')
    else:
        send_sms(plan['customer_phone'] if 'customer_phone' in plan.keys() else '',
                 f"Hi {first}, payment of {fmt_ghs(amount)} received for your {plan['device_name']} plan. "
                 f"Balance: {fmt_ghs(new_balance)}. Next due: {new_next_due}. -PhoneHub Ghana")
        flash(f'Payment of {fmt_ghs(amount)} recorded for plan #{plan_id}.', 'success')
    return redirect(url_for('admin_installments', last_payment=payment_id))


@app.route('/admin/installments/<int:plan_id>/update-status', methods=['POST'])
@admin_required
def update_plan_status(plan_id):
    new_status = request.form.get('status', '')
    if new_status not in ('Active', 'Paused', 'Completed', 'Defaulted'):
        flash('Invalid status value.', 'error')
        return redirect(url_for('admin_installments'))
    conn = get_db()
    conn.execute('UPDATE installment_plans SET status=? WHERE id=?', (new_status, plan_id))
    conn.commit(); conn.close()
    logger.info('Admin %s set plan #%d status to %s', session.get('admin_username'), plan_id, new_status)
    flash(f'Plan #{plan_id} updated to {new_status}.', 'success')
    return redirect(url_for('admin_installments'))


# ─── PDF RECEIPT ROUTES ───────────────────────────────────────────────────────

@app.route('/receipt/booking/<int:booking_id>')
def booking_receipt(booking_id):
    conn    = get_db()
    booking = conn.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
    conn.close()
    if not booking:
        return render_template('404.html'), 404
    buf  = generate_booking_receipt_pdf(dict(booking))
    resp = make_response(buf.read())
    resp.headers['Content-Type']        = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=phonehub-booking-{booking_id:05d}.pdf'
    return resp


@app.route('/receipt/payment/<int:payment_id>')
@admin_required
def payment_receipt(payment_id):
    conn    = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
    if not payment:
        conn.close()
        flash('Payment not found.', 'error')
        return redirect(url_for('admin_installments'))
    plan = conn.execute(
        '''SELECT ip.*, c.name as customer_name
           FROM installment_plans ip
           JOIN customers c ON c.id = ip.customer_id
           WHERE ip.id=?''',
        (payment['plan_id'],)).fetchone()
    conn.close()
    if not plan:
        flash('Plan not found.', 'error')
        return redirect(url_for('admin_installments'))
    buf  = generate_payment_receipt_pdf(dict(plan), dict(payment), plan['customer_name'])
    resp = make_response(buf.read())
    resp.headers['Content-Type']        = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=phonehub-payment-{payment_id:05d}.pdf'
    return resp


@app.route('/receipt/payment/plan/<int:plan_id>/latest')
@admin_required
def latest_payment_receipt(plan_id):
    conn    = get_db()
    payment = conn.execute(
        'SELECT * FROM payments WHERE plan_id=? ORDER BY created_at DESC LIMIT 1', (plan_id,)).fetchone()
    if not payment:
        conn.close()
        flash('No payments recorded for this plan yet.', 'error')
        return redirect(url_for('admin_installments'))
    plan = conn.execute(
        '''SELECT ip.*, c.name as customer_name
           FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id
           WHERE ip.id=?''', (plan_id,)).fetchone()
    conn.close()
    buf  = generate_payment_receipt_pdf(dict(plan), dict(payment), plan['customer_name'])
    resp = make_response(buf.read())
    resp.headers['Content-Type']        = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=phonehub-plan-{plan_id}-receipt.pdf'
    return resp


# ─── SMS REMINDERS ────────────────────────────────────────────────────────────

@app.route('/admin/installments/send-reminders', methods=['POST'])
@admin_required
def send_payment_reminders():
    days    = int(request.form.get('days', 3))
    today   = datetime.today()
    cutoff  = (today + timedelta(days=days)).strftime('%Y-%m-%d')
    today_s = today.strftime('%Y-%m-%d')

    conn  = get_db()
    plans = conn.execute(
        '''SELECT ip.*, c.name as customer_name, c.phone as customer_phone
           FROM installment_plans ip
           JOIN customers c ON c.id = ip.customer_id
           WHERE ip.status = 'Active' AND ip.next_due_date <= ?
           ORDER BY ip.next_due_date''',
        (cutoff,)).fetchall()
    conn.close()

    sent = skipped = 0
    for p in plans:
        overdue = p['next_due_date'] < today_s
        first   = (p['customer_name'].split() or ['Customer'])[0]
        if overdue:
            msg = (f"Hi {first}, your PhoneHub Ghana installment of "
                   f"{fmt_ghs(p['monthly_amount'])} for {p['device_name']} "
                   f"was DUE {p['next_due_date']}. Please pay now via "
                   f"{p['payment_method']} & call 0302000000. "
                   f"Balance: {fmt_ghs(p['balance_remaining'])}.")
        else:
            msg = (f"Hi {first}, your PhoneHub Ghana installment of "
                   f"{fmt_ghs(p['monthly_amount'])} for {p['device_name']} "
                   f"is due {p['next_due_date']}. Pay via "
                   f"{p['payment_method']}. Balance: {fmt_ghs(p['balance_remaining'])}. "
                   f"Questions? Call 0302000000.")
        if send_sms(p['customer_phone'], msg):
            sent += 1
        else:
            skipped += 1

    total = len(plans)
    if not AT_API_KEY:
        flash(f'SMS not configured — set AT_API_KEY env var. Would have sent {total} reminder(s).', 'error')
    else:
        flash(f'Sent {sent} SMS reminder(s). {skipped} failed (check AT_API_KEY / phone numbers).', 'success')
    return redirect(url_for('admin_installments'))


# ─── ERROR HANDLERS ───────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(_e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(_e):
    return render_template('500.html'), 500


@app.errorhandler(CSRFError)
def csrf_error(_e):
    flash('Your form session expired. Please try again.', 'error')
    return redirect(request.referrer or url_for('home'))


@app.errorhandler(429)
def too_many_requests(_e):
    flash('Too many login attempts. Please wait a minute and try again.', 'error')
    return render_template('admin_login.html'), 429


# ─── RUN ──────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    app.run(debug=True)