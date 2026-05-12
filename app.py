from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, g, jsonify
from flask_wtf.csrf import CSRFProtect, CSRFError
from markupsafe import escape as _he
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timedelta, timezone
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
import smtplib
import os
import re
import secrets
import logging
from decimal import Decimal, ROUND_HALF_UP
import requests as http_req
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
_secret_key = os.environ.get('SECRET_KEY', '')
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    _SECRET_KEY_MISSING = True
else:
    _SECRET_KEY_MISSING = False
app.secret_key = _secret_key
del _secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1-hour token validity

csrf    = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
)

ADMIN_SESSION_TIMEOUT = timedelta(minutes=int(os.environ.get('ADMIN_TIMEOUT_MINUTES', 30)))
ADMIN_PAGE_SIZE       = 50

BOOKING_SERVICES = frozenset({
    'Screen Replacement', 'Battery Replacement', 'Charging Port Repair',
    'Water Damage Treatment', 'Software Restore', 'Data Backup & Transfer',
    'Full Diagnostics', 'Camera / Speaker Repair',
})

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)
if _SECRET_KEY_MISSING:
    logger.warning('SECRET_KEY env var not set — a random key was generated. Sessions will be lost on restart. Set SECRET_KEY in production!')

DATABASE_URL          = os.environ.get('DATABASE_URL')
ADMIN_USERNAME  = os.environ.get('ADMIN_USERNAME', 'admin')
_admin_pw_raw   = os.environ.get('ADMIN_PASSWORD', '')
if not _admin_pw_raw:
    _admin_pw_raw = secrets.token_urlsafe(24)
    logger.critical('ADMIN_PASSWORD env var not set — a random admin password was generated for this session. Set ADMIN_PASSWORD in production!')
ADMIN_PASSWORD_HASH = generate_password_hash(_admin_pw_raw)
del _admin_pw_raw

BANK_DETAILS = {
    'bank_name':    os.environ.get('BANK_NAME',    'GCB Bank Ghana'),
    'account_name': os.environ.get('BANK_ACCT_NAME','DonnyPhonehub Gh Ltd.'),
    'account_no':   os.environ.get('BANK_ACCT_NO', ''),
    'branch':       os.environ.get('BANK_BRANCH',  ''),
    'sort_code':    os.environ.get('BANK_SORT',    ''),
    'swift':        os.environ.get('BANK_SWIFT',   ''),
    'momo_number':  os.environ.get('MOMO_NUMBER',  '0554509428'),
    'momo_name':    os.environ.get('MOMO_NAME',    'DonnyPhonehub Gh Ltd.'),
}

# ─── CLOUDINARY IMAGE STORAGE ────────────────────────────────────────────────
# Sign up free at cloudinary.com
# Free tier: 25GB storage, 25GB bandwidth/month
#
# After signing up go to:
# Dashboard → Account Details
# Copy: Cloud Name, API Key, API Secret
#
# Set these environment variables on Render:
# CLOUDINARY_CLOUD_NAME = your-cloud-name
# CLOUDINARY_API_KEY    = your-api-key
# CLOUDINARY_API_SECRET = your-api-secret
# CLOUDINARY_FOLDER     = phonehub-ghana/inventory
#
# Images are auto-optimised to max 800x800px
# and served via Cloudinary's global CDN.
# ─────────────────────────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key    = os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret = os.environ.get('CLOUDINARY_API_SECRET', ''),
    secure     = True
)
CLOUDINARY_FOLDER = os.environ.get('CLOUDINARY_FOLDER', 'phonehub-ghana/inventory')

# Plan config: months -> deposit %, fee %, label, min device price
PLAN_CONFIG = {
    2:  {'deposit_pct': 40, 'fee_pct': 2.5, 'label': '2 Months', 'min_price': 500},
    3:  {'deposit_pct': 40, 'fee_pct': 5,   'label': '3 Months', 'min_price': 500},
    6:  {'deposit_pct': 60, 'fee_pct': 10,  'label': '6 Months', 'min_price': 1500},
}

RESERVATION_DEPOSIT_PCT = int(os.environ.get('RESERVATION_DEPOSIT_PCT', 20))

ROLE_PERMISSIONS = {
    'owner': {
        'view_bookings':      True,
        'edit_bookings':      True,
        'delete_bookings':    True,
        'view_members':       True,
        'edit_members':       True,
        'delete_members':     True,
        'view_installments':  True,
        'edit_installments':  True,
        'view_inventory':     True,
        'edit_inventory':     True,
        'delete_inventory':   True,
        'view_revenue':       True,
        'manage_staff':       True,
        'send_reminders':     True,
        'extend_membership':  True,
        'record_payment':     True,
    },
    'manager': {
        'view_bookings':      True,
        'edit_bookings':      True,
        'delete_bookings':    False,
        'view_members':       True,
        'edit_members':       True,
        'delete_members':     False,
        'view_installments':  True,
        'edit_installments':  True,
        'view_inventory':     True,
        'edit_inventory':     True,
        'delete_inventory':   False,
        'view_revenue':       True,
        'manage_staff':       False,
        'send_reminders':     True,
        'extend_membership':  True,
        'record_payment':     True,
    },
    'technician': {
        'view_bookings':      True,
        'edit_bookings':      True,
        'delete_bookings':    False,
        'view_members':       False,
        'edit_members':       False,
        'delete_members':     False,
        'view_installments':  False,
        'edit_installments':  False,
        'view_inventory':     True,
        'edit_inventory':     False,
        'delete_inventory':   False,
        'view_revenue':       False,
        'manage_staff':       False,
        'send_reminders':     False,
        'extend_membership':  False,
        'record_payment':     False,
    },
    'sales': {
        'view_bookings':      True,
        'edit_bookings':      False,
        'delete_bookings':    False,
        'view_members':       True,
        'edit_members':       False,
        'delete_members':     False,
        'view_installments':  True,
        'edit_installments':  False,
        'view_inventory':     True,
        'edit_inventory':     False,
        'delete_inventory':   False,
        'view_revenue':       False,
        'manage_staff':       False,
        'send_reminders':     False,
        'extend_membership':  False,
        'record_payment':     False,
    },
}


def has_permission(permission):
    """Check if current session user has a permission."""
    if session.get('admin_is_master'):
        return True
    role = session.get('admin_role', '')
    return ROLE_PERMISSIONS.get(role, {}).get(permission, False)


def log_activity(action, category, target_type=None, target_id=None, details=None):
    try:
        user_name = session.get('admin_username', 'unknown')
        user_role = session.get('admin_role', 'unknown')
        staff_id  = session.get('admin_staff_id')
        ip        = request.remote_addr
        conn = get_db()
        conn.execute(
            """INSERT INTO activity_log
               (user_name, user_role, staff_id, action,
                category, target_type, target_id, details, ip_address)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_name, user_role, staff_id, action,
             category, target_type, target_id, details, ip)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error('Activity log failed: %s', exc)


# ─── DATABASE ─────────────────────────────────────────────────────────────────

_db_pool = None
_last_reservation_expiry: datetime | None = None

def _get_pool():
    global _db_pool
    if _db_pool is None and DATABASE_URL:
        _db_pool = pg_pool.ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=DATABASE_URL
        )
    return _db_pool


class _PgConn:
    """Thin wrapper so callers use conn.execute() / conn.commit() / conn.close()."""
    def __init__(self, conn, pool=None):
        self._conn = conn
        self._pool = pool

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._pool:
            self._pool.putconn(self._conn)
        else:
            self._conn.close()


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            'DATABASE_URL environment variable is not set.')
    import time
    last_exc = None
    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                DATABASE_URL,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
            )
            return _PgConn(conn)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            logger.warning(
                'DB connect attempt %d failed: %s',
                attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


def init_db():
    if not DATABASE_URL:
        logger.error('DATABASE_URL not set — skipping init_db()')
        return
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS customers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, device_brand TEXT, device_model TEXT,
        membership_tier TEXT DEFAULT 'Standard',
        membership_start TEXT, membership_expiry TEXT,
        email_verified INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW())''')

    conn.execute('''CREATE TABLE IF NOT EXISTS bookings (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL,
        device TEXT NOT NULL, service TEXT NOT NULL, date TEXT NOT NULL,
        notes TEXT, customer_id INTEGER, status TEXT DEFAULT 'Pending',
        FOREIGN KEY (customer_id) REFERENCES customers(id))''')

    for _col_sql in [
        "ALTER TABLE bookings ADD COLUMN assigned_to INTEGER REFERENCES staff(id)",
        "ALTER TABLE bookings ADD COLUMN assigned_at TIMESTAMP",
        "ALTER TABLE bookings ADD COLUMN priority TEXT DEFAULT 'Normal'",
        "ALTER TABLE bookings ADD COLUMN estimated_duration TEXT",
        "ALTER TABLE bookings ADD COLUMN internal_notes TEXT",
    ]:
        try:
            conn.execute(_col_sql)
            conn.commit()
        except Exception:
            conn.rollback()

    conn.execute('''CREATE TABLE IF NOT EXISTS installment_plans (
        id SERIAL PRIMARY KEY,
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
        created_at TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY (customer_id) REFERENCES customers(id))''')

    conn.execute('''CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        plan_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        paid_on TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        reference TEXT,
        recorded_by TEXT DEFAULT 'admin',
        notes TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY (plan_id) REFERENCES installment_plans(id))''')

    conn.execute('''CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS email_verification_tokens (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0)''')

    conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
        id            SERIAL PRIMARY KEY,
        brand         TEXT NOT NULL,
        model         TEXT NOT NULL,
        imei          TEXT,
        condition     TEXT DEFAULT 'New',
        cost_price    REAL NOT NULL,
        selling_price REAL NOT NULL,
        status        TEXT DEFAULT 'In Stock',
        color         TEXT,
        storage       TEXT,
        notes         TEXT,
        added_by      TEXT DEFAULT 'admin',
        sold_to       INTEGER,
        plan_id       INTEGER,
        created_at    TIMESTAMP DEFAULT NOW(),
        updated_at    TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY (sold_to) REFERENCES customers(id),
        FOREIGN KEY (plan_id) REFERENCES installment_plans(id)
    )""")

    # Safe migration — add image columns to inventory if they don't exist yet
    for col in ('image1_url', 'image2_url', 'image1_public_id', 'image2_public_id'):
        try:
            conn.execute(f'ALTER TABLE inventory ADD COLUMN {col} TEXT')
            conn.commit()
        except Exception:
            conn.rollback()

    conn.execute("""CREATE TABLE IF NOT EXISTS staff (
        id            SERIAL PRIMARY KEY,
        name          TEXT NOT NULL,
        email         TEXT NOT NULL UNIQUE,
        phone         TEXT,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'technician',
        is_active     INTEGER DEFAULT 1,
        created_by    TEXT DEFAULT 'owner',
        last_login    TIMESTAMP,
        created_at    TIMESTAMP DEFAULT NOW()
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS reservations (
        id              SERIAL PRIMARY KEY,
        item_id         INTEGER NOT NULL,
        customer_id     INTEGER,
        customer_name   TEXT NOT NULL,
        customer_phone  TEXT NOT NULL,
        customer_email  TEXT NOT NULL,
        deposit_amount  REAL NOT NULL,
        payment_method  TEXT NOT NULL,
        momo_number     TEXT,
        momo_network    TEXT,
        bank_reference  TEXT,
        status          TEXT DEFAULT 'Pending',
        expires_at      TIMESTAMP NOT NULL,
        confirmed_by    TEXT,
        notes           TEXT,
        created_at      TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY (item_id)      REFERENCES inventory(id),
        FOREIGN KEY (customer_id)  REFERENCES customers(id)
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS device_enquiries (
        id              SERIAL PRIMARY KEY,
        customer_id     INTEGER,
        customer_name   TEXT NOT NULL,
        customer_phone  TEXT NOT NULL,
        customer_email  TEXT NOT NULL,
        device_type     TEXT NOT NULL,
        budget          TEXT,
        message         TEXT NOT NULL,
        status          TEXT DEFAULT 'New',
        created_at      TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )""")
    # Migration: add reply columns if they don't exist yet
    for col, defn in [
        ('response_message', 'TEXT'),
        ('replied_at',       'TIMESTAMP'),
        ('replied_by',       'TEXT'),
    ]:
        try:
            conn.execute(f'ALTER TABLE device_enquiries ADD COLUMN {col} {defn}')
        except Exception:
            conn.rollback()

    conn.execute("""
CREATE TABLE IF NOT EXISTS activity_log (
    id          SERIAL PRIMARY KEY,
    user_name   TEXT NOT NULL,
    user_role   TEXT NOT NULL,
    staff_id    INTEGER,
    action      TEXT NOT NULL,
    category    TEXT NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    details     TEXT,
    ip_address  TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
)
""")

    conn.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    id                   SERIAL PRIMARY KEY,
    plan_id              INTEGER NOT NULL,
    customer_id          INTEGER NOT NULL,
    amount               REAL NOT NULL,
    payment_method       TEXT NOT NULL,
    reference            TEXT,
    momo_number          TEXT,
    momo_network         TEXT,
    screenshot_url       TEXT,
    screenshot_public_id TEXT,
    notes                TEXT,
    status               TEXT DEFAULT 'Pending',
    reviewed_by          TEXT,
    review_notes         TEXT,
    created_at           TIMESTAMP DEFAULT NOW(),
    reviewed_at          TIMESTAMP,
    FOREIGN KEY (plan_id)     REFERENCES installment_plans(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
)
""")

    conn.execute("""
CREATE TABLE IF NOT EXISTS pending_deposits (
    id                   SERIAL PRIMARY KEY,
    reservation_id       INTEGER NOT NULL,
    customer_id          INTEGER,
    customer_name        TEXT NOT NULL,
    customer_phone       TEXT NOT NULL,
    amount               REAL NOT NULL,
    payment_method       TEXT NOT NULL,
    reference            TEXT,
    momo_number          TEXT,
    momo_network         TEXT,
    screenshot_url       TEXT,
    screenshot_public_id TEXT,
    notes                TEXT,
    status               TEXT DEFAULT 'Pending',
    reviewed_by          TEXT,
    review_notes         TEXT,
    created_at           TIMESTAMP DEFAULT NOW(),
    reviewed_at          TIMESTAMP,
    FOREIGN KEY (reservation_id) REFERENCES reservations(id),
    FOREIGN KEY (customer_id)    REFERENCES customers(id)
)
""")

    conn.commit()
    conn.close()


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def hash_password(p):
    return generate_password_hash(p)


def verify_password(stored, supplied):
    return check_password_hash(stored, supplied)


def validate_password(pw):
    """Return an error string or None if the password meets policy."""
    if len(pw) < 8:
        return 'Password must be at least 8 characters.'
    if not re.search(r'[A-Za-z]', pw):
        return 'Password must contain at least one letter.'
    if not re.search(r'[0-9!@#$%^&*()\-_=+\[\]{}|;:\'",.<>?/`~]', pw):
        return 'Password must contain at least one number or special character.'
    return None


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
    month = d.month + 1
    year  = d.year + (1 if month > 12 else 0)
    month = month if month <= 12 else 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day).strftime('%Y-%m-%d')


def next_due_date():
    return add_one_month(datetime.today().strftime('%Y-%m-%d'))


def _d(value):
    """Convert to Decimal rounded to 2dp."""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def calculate_plan(device_price, months):
    cfg         = PLAN_CONFIG[months]
    price       = _d(device_price)
    service_fee = _d(price * Decimal(str(cfg['fee_pct'])) / 100)
    total       = _d(price + service_fee)
    deposit     = _d(total * Decimal(cfg['deposit_pct']) / 100)
    balance     = _d(total - deposit)
    monthly     = _d(balance / Decimal(months))
    return {
        'service_fee': float(service_fee), 'total': float(total),
        'deposit': float(deposit), 'balance': float(balance), 'monthly': float(monthly),
        'deposit_pct': cfg['deposit_pct'], 'fee_pct': cfg['fee_pct'], 'months': months,
    }


def fmt_ghs(amount):
    try:
        return f"GH\u20B5{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "GH\u20B50.00"


# ─── IMAGE UPLOAD HELPERS ─────────────────────────────────────────────────────

ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
MAX_IMAGE_SIZE_MB = 5


def allowed_image(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTENSIONS


def upload_image_to_cloudinary(file, item_id, slot_number):
    if not file or not file.filename:
        return None
    if not allowed_image(file.filename):
        logger.warning('Rejected image upload — invalid extension: %s', file.filename)
        return None
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        logger.warning('Rejected image — too large: %d bytes', file_size)
        return None
    if not cloudinary.config().cloud_name:
        logger.warning('Cloudinary not configured — skipping upload')
        return None
    try:
        public_id = f'{CLOUDINARY_FOLDER}/item-{item_id}-img{slot_number}'
        result = cloudinary.uploader.upload(
            file,
            public_id      = public_id,
            overwrite      = True,
            resource_type  = 'image',
            transformation = [
                {'width': 800, 'height': 800,
                 'crop': 'limit', 'quality': 'auto',
                 'fetch_format': 'auto'}
            ]
        )
        logger.info('Image uploaded to Cloudinary: %s', result['secure_url'])
        return {'url': result['secure_url'], 'public_id': result['public_id']}
    except Exception as exc:
        logger.error('Cloudinary upload failed for item %d slot %d: %s', item_id, slot_number, exc)
        return None


def delete_image_from_cloudinary(public_id):
    if not public_id:
        return
    if not cloudinary.config().cloud_name:
        return
    try:
        cloudinary.uploader.destroy(public_id)
        logger.info('Deleted Cloudinary image: %s', public_id)
    except Exception as exc:
        logger.error('Failed to delete Cloudinary image %s: %s', public_id, exc)


# ─── EMAIL ────────────────────────────────────────────────────────────────────

MAIL_HOST = os.environ.get('MAIL_HOST', 'smtp.gmail.com')
MAIL_PORT = int(os.environ.get('MAIL_PORT', 465))
MAIL_USER = os.environ.get('MAIL_USER', '')
MAIL_PASS = os.environ.get('MAIL_PASS', '')
MAIL_FROM = os.environ.get('MAIL_FROM', 'noreply@phonehubghana.com')


def send_email(to, subject, html_body):
    if not MAIL_USER or not MAIL_PASS:
        logger.warning(
            'send_email skipped — MAIL_USER/MAIL_PASS '
            'not configured')
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'DonnyPhonehub Gh <{MAIL_FROM}>'
        msg['To']      = to
        msg.attach(MIMEText(html_body, 'html'))
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        with smtplib.SMTP_SSL(MAIL_HOST, 465,
                              timeout=10,
                              context=_ctx) as s:
            s.login(MAIL_USER, MAIL_PASS)
            s.sendmail(MAIL_FROM, to, msg.as_string())
        logger.info('Email sent to %s — %s', to, subject)
        return True
    except Exception as exc:
        logger.error(
            'Email to %s failed: %s', to, exc)
        return False


# ─── SMS (Arkesel) ────────────────────────────────────────────────────────────

ARKESEL_API_KEY  = os.environ.get('ARKESEL_API_KEY', '')
ARKESEL_SENDER   = os.environ.get('ARKESEL_SENDER_ID', 'DonnyPhonehub')


def _normalize_gh_phone(phone):
    p = phone.strip().replace(' ', '').replace('-', '')
    if p.startswith('0'):
        return '+233' + p[1:]
    if not p.startswith('+'):
        return '+233' + p
    return p


def send_sms(phone, message):
    if not ARKESEL_API_KEY:
        logger.warning('send_sms skipped — ARKESEL_API_KEY not configured')
        return False
    if not phone or not valid_gh_phone(phone):
        logger.warning('send_sms skipped — invalid phone number: %r', phone)
        return False
    normalized = _normalize_gh_phone(phone)
    try:
        resp = http_req.get(
            'https://sms.arkesel.com/sms/api',
            params={
                'action':  'send-sms',
                'api_key': ARKESEL_API_KEY,
                'to':      normalized,
                'from':    ARKESEL_SENDER,
                'sms':     message,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get('code') == 'ok':
            logger.info('SMS sent to %s via Arkesel', normalized)
            return True
        logger.error('SMS to %s failed — Arkesel: %s', normalized, data)
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
        Paragraph('DonnyPhonehub Gh',
                  ParagraphStyle('ph', parent=styles['Normal'], fontSize=20,
                                 fontName='Helvetica-Bold', textColor=_C_GREEN)),
        Paragraph('Tamale, Northern Region, Ghana · 0541057500 · hello@phonehubghana.com',
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
    story.append(Paragraph('Thank you for choosing DonnyPhonehub Gh. Please keep this receipt.',
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
    story.append(Paragraph('This receipt confirms your installment payment. Thank you for being a DonnyPhonehub Gh member.',
                           ParagraphStyle('ft', parent=styles['Normal'],
                               fontSize=8, textColor=_C_GRAY, alignment=TA_CENTER)))
    doc.build(story)
    buf.seek(0)
    return buf


def _safe_redirect(fallback):
    """Redirect to request.referrer only if it is same-origin and same-scheme."""
    ref = request.referrer
    if ref:
        from urllib.parse import urlparse
        ref_p  = urlparse(ref)
        own_p  = urlparse(request.host_url)
        if ref_p.netloc == own_p.netloc and ref_p.scheme == own_p.scheme:
            return redirect(ref)
    return redirect(fallback)


@app.before_request
def _generate_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


@app.after_request
def set_security_headers(response):
    nonce = getattr(g, 'csp_nonce', '')
    response.headers['Content-Security-Policy'] = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
        f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        f"font-src 'self' https://fonts.gstatic.com; "
        f"img-src 'self' data: https://res.cloudinary.com; "
        f"connect-src 'self'; "
        f"form-action 'self'; "
        f"base-uri 'self'; "
        f"upgrade-insecure-requests;"
    )
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options']    = 'nosniff'
    response.headers['X-Frame-Options']           = 'SAMEORIGIN'
    response.headers['Referrer-Policy']           = 'strict-origin-when-cross-origin'
    return response


def _pending_payment_count():
    try:
        conn = get_db()
        count = conn.execute(
            """SELECT
               (SELECT COUNT(*) FROM pending_payments  WHERE status='Pending') +
               (SELECT COUNT(*) FROM pending_deposits  WHERE status='Pending')
               AS total"""
        ).fetchone()['total']
        conn.close()
        return int(count or 0)
    except Exception:
        return 0


@app.context_processor
def inject_helpers():
    pending_count = 0
    if session.get('admin_logged_in'):
        try:
            pending_count = _pending_payment_count()
        except Exception:
            pass
    return dict(membership_status=membership_status,
                fmt_ghs=fmt_ghs, PLAN_CONFIG=PLAN_CONFIG,
                has_permission=has_permission,
                csp_nonce=getattr(g, 'csp_nonce', ''),
                pending_payment_count=pending_count)


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

@app.route('/health')
@limiter.limit('30 per minute')
def health():
    return {'status': 'ok', 'service': 'DonnyPhonehub Gh', 'build': '267a97d'}


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


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
        cur = conn.execute(
            'INSERT INTO bookings (name,phone,email,device,service,date,notes,customer_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, phone, email, device, service, date, notes, cid))
        booking_id = cur.fetchone()['id']
        conn.commit()
        conn.close()
        ids = session.get('guest_booking_ids', []) + [booking_id]
        session['guest_booking_ids'] = ids[-10:]
        try:
            send_email(email, 'Booking Confirmed — DonnyPhonehub Gh', f"""
        <p>Hi {_he(name)},</p>
        <p>Your repair booking is confirmed.</p>
        <ul>
          <li><b>Device:</b> {_he(device)}</li>
          <li><b>Service:</b> {_he(service)}</li>
          <li><b>Date:</b> {_he(date)}</li>
        </ul>
        <p>We'll see you at our Tamale, Northern Region location. Call us on 0541057500 with any questions.</p>
        <p>— DonnyPhonehub Gh Team</p>
        """)
        except Exception as _email_exc:
            logger.error('Email notification failed: %s', _email_exc)
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
        name  = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')
        db    = request.form.get('device_brand', '').strip()
        dm    = request.form.get('device_model', '').strip()
        if not name or len(name) > 100:
            flash('Please enter your full name (max 100 characters).', 'error')
            return render_template('register.html')
        if not valid_gh_phone(phone):
            flash('Enter a valid Ghanaian phone number (e.g. 024 000 0000).', 'error')
            return render_template('register.html')
        pw_err = validate_password(pw)
        if pw_err:
            flash(pw_err, 'error')
            return render_template('register.html')
        start  = datetime.today().strftime('%Y-%m-%d')
        expiry = (datetime.today() + timedelta(days=365)).strftime('%Y-%m-%d')
        conn = get_db()
        if conn.execute('SELECT id FROM customers WHERE email=%s', (email,)).fetchone():
            flash('An account with that email already exists.', 'error')
            conn.close()
            return render_template('register.html')
        conn.execute(
            "INSERT INTO customers (name,phone,email,password_hash,device_brand,device_model,membership_tier,membership_start,membership_expiry,email_verified) VALUES (%s,%s,%s,%s,%s,%s,'Standard',%s,%s,0)",
            (name, phone, email, hash_password(pw), db, dm, start, expiry))
        conn.commit()
        customer = conn.execute('SELECT * FROM customers WHERE email=%s', (email,)).fetchone()
        v_token  = secrets.token_urlsafe(32)
        v_expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        conn.execute(
            'INSERT INTO email_verification_tokens (customer_id,token,expires_at) VALUES (%s,%s,%s)',
            (customer['id'], v_token, v_expiry))
        conn.commit()
        conn.close()
        session.clear()
        session['customer_id']   = customer['id']
        session['customer_name'] = customer['name']
        verify_url = url_for('verify_email', token=v_token, _external=True)
        try:
            send_email(email, 'Verify your email — DonnyPhonehub Gh', f"""
        <p>Hi {_he(name)},</p>
        <p>Your DonnyPhonehub Gh account is live! Please verify your email to unlock all features.</p>
        <p><a href="{verify_url}" style="background:#006B3F;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Verify My Email</a></p>
        <p style="margin-top:12px;font-size:13px;color:#666">Link expires in 24 hours. If you didn't create an account, ignore this email.</p>
        <p>— DonnyPhonehub Gh Team</p>
        """)
            flash(
                f'Welcome, {name}! Check your email '
                f'to verify your account.',
                'success')
        except Exception as _email_exc:
            logger.error(
                'Verification email failed for %s: %s',
                email, _email_exc)
            flash(
                f'Welcome, {name}! Your account is active. '
                f'You can verify your email from your dashboard.',
                'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def customer_login():
    if session.get('customer_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')
        conn  = get_db()
        c = conn.execute('SELECT * FROM customers WHERE email=%s', (email,)).fetchone()
        conn.close()
        if c and verify_password(c['password_hash'], pw):
            session.clear()
            session['customer_id']   = c['id']
            session['customer_name'] = c['name']
            flash(f'Welcome back, {c["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('customer_login.html')


@app.route('/logout', methods=['GET', 'POST'])
@csrf.exempt
def customer_logout():
    try:
        session.clear()
        flash('You have been logged out.', 'success')
    except Exception:
        pass
    return redirect(url_for('home'))


# ─── CUSTOMER DASHBOARD ───────────────────────────────────────────────────────

@app.route('/dashboard')
@customer_required
def dashboard():
    conn     = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=%s', (session['customer_id'],)).fetchone()
    if not customer:
        conn.close()
        session.clear()
        flash('Your account no longer exists. Please register or contact us.', 'error')
        return redirect(url_for('customer_login'))
    bookings = conn.execute(
        'SELECT * FROM bookings WHERE customer_id=%s ORDER BY date DESC',
        (session['customer_id'],)).fetchall()
    plans = conn.execute(
        'SELECT * FROM installment_plans WHERE customer_id=%s ORDER BY created_at DESC',
        (session['customer_id'],)).fetchall()
    conn.execute(
        "UPDATE reservations SET status='Expired' WHERE status='Pending' AND expires_at < NOW()"
    )
    conn.commit()
    reservations = conn.execute(
        """SELECT r.*, i.brand, i.model, i.color, i.storage, i.selling_price
           FROM reservations r
           JOIN inventory i ON i.id = r.item_id
           WHERE r.customer_id=%s AND r.status IN ('Pending','Confirmed')
           ORDER BY r.created_at DESC
           LIMIT 5""",
        (session['customer_id'],)
    ).fetchall()
    conn.close()
    status = membership_status(customer['membership_expiry'])
    installment_eligible = status in ('Active', 'Expiring Soon')
    return render_template('dashboard.html',
                           customer=customer, bookings=bookings,
                           plans=plans, status=status,
                           installment_eligible=installment_eligible,
                           reservations=reservations,
                           now=datetime.today().strftime('%Y-%m-%d'))


@app.route('/account/edit', methods=['GET', 'POST'])
@customer_required
def account_edit():
    conn     = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=%s', (session['customer_id'],)).fetchone()
    if not customer:
        conn.close()
        session.clear()
        flash('Your account no longer exists.', 'error')
        return redirect(url_for('customer_login'))
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        db    = request.form.get('device_brand', '').strip()[:100]
        dm    = request.form.get('device_model', '').strip()[:100]
        if not name or len(name) > 100:
            conn.close()
            flash('Please enter your full name (max 100 characters).', 'error')
            return render_template('account_edit.html', customer=customer)
        if not valid_gh_phone(phone):
            conn.close()
            flash('Enter a valid Ghanaian phone number (e.g. 024 000 0000).', 'error')
            return render_template('account_edit.html', customer=customer)
        conn.execute(
            'UPDATE customers SET name=%s, phone=%s, device_brand=%s, device_model=%s WHERE id=%s',
            (name, phone, db, dm, session['customer_id']))
        conn.commit()
        conn.close()
        session['customer_name'] = name
        flash('Your profile has been updated.', 'success')
        return redirect(url_for('dashboard'))
    conn.close()
    return render_template('account_edit.html', customer=customer)


# ══════════════════════════════════════════════════════════════════════════════
# INSTALLMENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/installment/apply', methods=['GET', 'POST'])
@customer_required
def installment_apply():
    conn     = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=%s', (session['customer_id'],)).fetchone()
    conn.close()
    if not customer['email_verified']:
        flash('You must verify your email address before applying for an installment plan. Check your inbox for the verification link.', 'error')
        return redirect(url_for('dashboard'))
    mem_status = membership_status(customer['membership_expiry'])
    if mem_status not in ('Active', 'Expiring Soon'):
        flash('Your membership has expired or is inactive. Please renew to apply for an installment plan.', 'error')
        return redirect(url_for('dashboard'))

    # Block if member already has an active plan
    conn2 = get_db()
    blocking_plan = conn2.execute(
        "SELECT id, status FROM installment_plans WHERE customer_id=%s AND status IN ('Active','Defaulted') LIMIT 1",
        (session['customer_id'],)).fetchone()
    conn2.close()
    if blocking_plan:
        if blocking_plan['status'] == 'Defaulted':
            flash('Your previous installment plan was defaulted. You are not eligible to apply for a new plan. Please contact us at 0541057500 to resolve this.', 'error')
            return redirect(url_for('installment_detail', plan_id=blocking_plan['id']))
        flash('You already have an active installment plan. Please complete your current plan before applying for a new one.', 'error')
        return redirect(url_for('installment_detail', plan_id=blocking_plan['id']))

    if request.method == 'POST':
        try:
            device_name    = request.form.get('device_name', '').strip()
            device_price   = float(request.form.get('device_price', 0))
            plan_months    = int(request.form.get('plan_months', 0))
            payment_method = request.form.get('payment_method', '').strip()
        except (ValueError, TypeError):
            flash('Invalid form data. Please try again.', 'error')
            return redirect(url_for('installment_apply'))
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

        if payment_method not in ('MoMo', 'Bank'):
            flash('Invalid payment method selected.', 'error')
            return redirect(url_for('installment_apply'))

        if payment_method == 'MoMo' and not momo_number:
            flash('MoMo number is required when paying by Mobile Money.', 'error')
            return redirect(url_for('installment_apply'))

        p = calculate_plan(device_price, plan_months)
        conn = get_db()
        cur = conn.execute(
            '''INSERT INTO installment_plans
               (customer_id,device_name,device_price,service_fee,total_payable,
                deposit_amount,balance_remaining,monthly_amount,plan_months,
                next_due_date,payment_method,momo_number,momo_network,
                bank_name,bank_reference,notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id''',
            (session['customer_id'], device_name, device_price,
             p['service_fee'], p['total'], p['deposit'], p['balance'],
             p['monthly'], plan_months, next_due_date(),
             payment_method, momo_number or None, momo_network or None,
             bank_name or None, bank_reference or None, notes or None))
        plan_id = cur.fetchone()['id']
        conn.commit()
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
        'SELECT * FROM installment_plans WHERE id=%s AND customer_id=%s',
        (plan_id, session['customer_id'])).fetchone()
    if not plan:
        conn.close()
        flash('Plan not found.', 'error')
        return redirect(url_for('dashboard'))
    payments_raw = conn.execute(
        'SELECT * FROM payments WHERE plan_id=%s ORDER BY paid_on DESC', (plan_id,)).fetchall()
    conn.close()

    # Convert to plain dicts so Jinja2 never handles raw psycopg2 row objects
    plan_dict = dict(plan)
    if plan_dict.get('created_at') and hasattr(plan_dict['created_at'], 'strftime'):
        plan_dict['created_at'] = plan_dict['created_at'].strftime('%Y-%m-%d')
    payments = [dict(p) for p in payments_raw]

    paid_total = sum(p['amount'] for p in payments)
    progress   = round((paid_total / plan_dict['total_payable']) * 100) if plan_dict['total_payable'] else 0
    return render_template('installment_detail.html',
                           plan=plan_dict, payments=payments,
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

        # Check master admin first (env var — never in DB)
        if u == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, p):
            session['admin_logged_in']     = True
            session['admin_username']      = u
            session['admin_role']          = 'owner'
            session['admin_is_master']     = True
            session['admin_last_activity'] = datetime.now(timezone.utc).isoformat()
            log_activity('Logged in', 'auth', details=f'Login as {session.get("admin_role")}')
            flash('Welcome back.', 'success')
            return redirect(url_for('admin'))

        # Check staff accounts in database
        conn = get_db()
        staff = conn.execute(
            'SELECT * FROM staff WHERE email=%s AND is_active=1',
            (u,)).fetchone()
        conn.close()

        if staff and check_password_hash(staff['password_hash'], p):
            conn2 = get_db()
            conn2.execute(
                'UPDATE staff SET last_login=NOW() WHERE id=%s',
                (staff['id'],))
            conn2.commit()
            conn2.close()
            session['admin_logged_in']     = True
            session['admin_username']      = staff['name']
            session['admin_role']          = staff['role']
            session['admin_staff_id']      = staff['id']
            session['admin_is_master']     = False
            session['admin_last_activity'] = datetime.now(timezone.utc).isoformat()
            log_activity('Logged in', 'auth', details=f'Login as {session.get("admin_role")}')
            flash(f'Welcome, {staff["name"]}.', 'success')
            if staff['role'] == 'technician':
                return redirect(url_for('admin_my_jobs'))
            return redirect(url_for('admin'))

        flash('Invalid credentials.', 'error')
    return render_template('admin_login.html')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    log_activity('Logged out', 'auth')
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin():
    today = datetime.today().strftime('%Y-%m-%d')
    now   = datetime.now()
    conn  = get_db()
    try:
        bookings_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE date=%s", (today,)
        ).fetchone()['cnt']
        bookings_pending = conn.execute(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE status='Pending'"
        ).fetchone()['cnt']
        members_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customers WHERE DATE(created_at)=CURRENT_DATE"
        ).fetchone()['cnt']
        members_active = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customers WHERE membership_expiry >= %s", (today,)
        ).fetchone()['cnt']
        payments_today = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE paid_on=%s", (today,)
        ).fetchone()['total']
        payments_month = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments "
            "WHERE DATE_TRUNC('month',paid_on::date)=DATE_TRUNC('month',CURRENT_DATE)"
        ).fetchone()['total']
        active_plans = conn.execute(
            "SELECT COUNT(*) AS cnt FROM installment_plans WHERE status='Active'"
        ).fetchone()['cnt']
        overdue_plans = conn.execute(
            "SELECT COUNT(*) AS cnt FROM installment_plans "
            "WHERE status='Active' AND next_due_date < %s", (today,)
        ).fetchone()['cnt']
        outstanding = conn.execute(
            "SELECT COALESCE(SUM(balance_remaining),0) AS total "
            "FROM installment_plans WHERE status='Active'"
        ).fetchone()['total']
        inventory_stock = conn.execute(
            "SELECT COUNT(*) AS cnt FROM inventory WHERE status='In Stock'"
        ).fetchone()['cnt']
        inventory_reserved = conn.execute(
            "SELECT COUNT(*) AS cnt FROM inventory WHERE status='Reserved'"
        ).fetchone()['cnt']
        pending_reservations = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE status='Pending'"
        ).fetchone()['cnt']
        new_enquiries = conn.execute(
            "SELECT COUNT(*) AS cnt FROM device_enquiries WHERE status='New'"
        ).fetchone()['cnt']
        expiring_soon = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customers "
            "WHERE membership_expiry::date BETWEEN "
            "CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'"
        ).fetchone()['cnt']
        recent_pending = conn.execute(
            "SELECT id, name, device, service, date FROM bookings "
            "WHERE status='Pending' ORDER BY date LIMIT 5"
        ).fetchall()
        overdue_list = conn.execute(
            "SELECT ip.id, ip.device_name, ip.next_due_date, ip.monthly_amount, "
            "ip.balance_remaining, c.name AS customer_name, c.phone AS customer_phone "
            "FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id "
            "WHERE ip.status='Active' AND ip.next_due_date < %s "
            "ORDER BY ip.next_due_date LIMIT 5", (today,)
        ).fetchall()
        recent_payments = conn.execute(
            "SELECT p.amount, p.paid_on, p.payment_method, ip.device_name, "
            "c.name AS customer_name "
            "FROM payments p "
            "JOIN installment_plans ip ON ip.id=p.plan_id "
            "JOIN customers c ON c.id=ip.customer_id "
            "ORDER BY p.created_at DESC LIMIT 5"
        ).fetchall()
        recent_members = conn.execute(
            "SELECT name, phone, email, created_at "
            "FROM customers ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        daily_payments = conn.execute(
            "SELECT paid_on, COALESCE(SUM(amount),0) AS total FROM payments "
            "WHERE paid_on::date >= CURRENT_DATE - INTERVAL '7 days' "
            "GROUP BY paid_on ORDER BY paid_on"
        ).fetchall()
        daily_bookings = conn.execute(
            "SELECT date, COUNT(*) AS cnt FROM bookings "
            "WHERE date::date >= CURRENT_DATE - INTERVAL '7 days' "
            "GROUP BY date ORDER BY date"
        ).fetchall()
        activity_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM activity_log "
            "WHERE DATE(created_at) = CURRENT_DATE"
        ).fetchone()['cnt']
    finally:
        conn.close()
    return render_template('admin_dashboard.html',
        bookings_today=bookings_today,
        bookings_pending=bookings_pending,
        members_today=members_today,
        members_active=members_active,
        payments_today=float(payments_today),
        payments_month=float(payments_month),
        active_plans=active_plans,
        overdue_plans=overdue_plans,
        outstanding=float(outstanding),
        inventory_stock=inventory_stock,
        inventory_reserved=inventory_reserved,
        pending_reservations=pending_reservations,
        new_enquiries=new_enquiries,
        expiring_soon=expiring_soon,
        recent_pending=recent_pending,
        overdue_list=overdue_list,
        recent_payments=recent_payments,
        recent_members=recent_members,
        daily_payments=[{'paid_on': str(r['paid_on']), 'total': float(r['total'])} for r in daily_payments],
        daily_bookings=[{'date': str(r['date']), 'cnt': r['cnt']} for r in daily_bookings],
        today=today,
        now=now,
        activity_today=activity_today,
    )


@app.route('/admin/bookings')
@admin_required
def admin_bookings():
    search    = request.args.get('search', '').strip()
    service   = request.args.get('service', '').strip()
    status    = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to', '').strip()
    if service not in BOOKING_SERVICES:
        service = ''
    if status not in ('Pending', 'In Progress', 'Complete', 'Cancelled'):
        status = ''
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    assigned_filter = request.args.get('assigned', '').strip()
    conn    = get_db()
    conditions, params = [], []
    if search:
        conditions.append('(b.name ILIKE %s OR b.email ILIKE %s OR b.phone ILIKE %s)')
        params += [f'%{search}%'] * 3
    if service:
        conditions.append('b.service=%s'); params.append(service)
    if status:
        conditions.append('b.status=%s'); params.append(status)
    if date_from:
        conditions.append('b.date >= %s'); params.append(date_from)
    if date_to:
        conditions.append('b.date <= %s'); params.append(date_to)
    if assigned_filter == 'unassigned':
        conditions.append('b.assigned_to IS NULL')
    elif assigned_filter == 'assigned':
        conditions.append('b.assigned_to IS NOT NULL')
    elif assigned_filter and assigned_filter.isdigit():
        conditions.append('b.assigned_to=%s'); params.append(int(assigned_filter))
    base_from = 'FROM bookings b LEFT JOIN staff s ON s.id = b.assigned_to'
    where     = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    total    = conn.execute(f'SELECT COUNT(*) AS cnt {base_from} {where}', params).fetchone()['cnt']
    bookings = conn.execute(
        f'SELECT b.*, s.name AS technician_name {base_from} {where} ORDER BY b.date DESC LIMIT %s OFFSET %s',
        params + [ADMIN_PAGE_SIZE, (page - 1) * ADMIN_PAGE_SIZE]
    ).fetchall()
    technicians = conn.execute(
        "SELECT id, name, role FROM staff WHERE is_active=1 AND role IN ('technician','manager','owner') ORDER BY name"
    ).fetchall()
    conn.close()
    total_pages = max(1, -(-total // ADMIN_PAGE_SIZE))
    return render_template('admin.html', bookings=bookings, search=search, service=service,
                           status=status, date_from=date_from, date_to=date_to,
                           page=page, total_pages=total_pages, total=total,
                           technicians=technicians, assigned_filter=assigned_filter)


@app.route('/admin/my-jobs')
@admin_required
def admin_my_jobs():
    staff_id  = session.get('admin_staff_id')
    is_master = session.get('admin_is_master')
    conn = get_db()
    order_clause = """ORDER BY
        CASE b.priority WHEN 'Urgent' THEN 0 WHEN 'Normal' THEN 1 WHEN 'Low' THEN 2 ELSE 3 END,
        b.date"""
    if is_master:
        bookings = conn.execute(
            f"""SELECT b.*, s.name AS technician_name
                FROM bookings b LEFT JOIN staff s ON s.id = b.assigned_to
                WHERE b.assigned_to IS NOT NULL AND b.status IN ('Pending', 'In Progress')
                {order_clause}"""
        ).fetchall()
        completed_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE assigned_to IS NOT NULL AND status='Complete' AND date=CURRENT_DATE::text"
        ).fetchone()['cnt']
    elif staff_id:
        bookings = conn.execute(
            f"""SELECT b.*, s.name AS technician_name
                FROM bookings b LEFT JOIN staff s ON s.id = b.assigned_to
                WHERE b.assigned_to=%s AND b.status IN ('Pending', 'In Progress')
                {order_clause}""",
            (staff_id,)
        ).fetchall()
        completed_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE assigned_to=%s AND status='Complete' AND date=CURRENT_DATE::text",
            (staff_id,)
        ).fetchone()['cnt']
    else:
        bookings = []
        completed_today = 0
    conn.close()
    today_str       = datetime.today().strftime('%Y-%m-%d')
    total_assigned  = len(bookings)
    urgent_count    = sum(1 for b in bookings if (b.get('priority') or 'Normal') == 'Urgent')
    today_jobs      = sum(1 for b in bookings if (b.get('date') or '') == today_str)
    return render_template('admin_my_jobs.html',
                           bookings=bookings,
                           total_assigned=total_assigned,
                           urgent_count=urgent_count,
                           today_jobs=today_jobs,
                           completed_today=completed_today,
                           is_master=is_master)


@app.route('/admin/bookings/<int:booking_id>/assign', methods=['POST'])
@admin_required
def assign_booking(booking_id):
    if not has_permission('edit_bookings'):
        flash('You do not have permission.', 'error')
        return redirect(url_for('admin'))
    staff_id_raw        = request.form.get('staff_id', '').strip()
    priority            = request.form.get('priority', 'Normal').strip()
    estimated_duration  = request.form.get('estimated_duration', '').strip()
    internal_notes      = request.form.get('internal_notes', '').strip()
    if priority not in ('Low', 'Normal', 'Urgent'):
        priority = 'Normal'
    conn = get_db()
    booking = conn.execute('SELECT * FROM bookings WHERE id=%s', (booking_id,)).fetchone()
    if not booking:
        conn.close()
        flash('Booking not found.', 'error')
        return redirect(url_for('admin_bookings'))
    assigned_staff = None
    if staff_id_raw:
        try:
            staff_id = int(staff_id_raw)
        except ValueError:
            conn.close()
            flash('Invalid staff member.', 'error')
            return redirect(url_for('admin_bookings'))
        assigned_staff = conn.execute(
            'SELECT id, name, phone, role FROM staff WHERE id=%s AND is_active=1', (staff_id,)
        ).fetchone()
        if not assigned_staff:
            conn.close()
            flash('Staff member not found or inactive.', 'error')
            return redirect(url_for('admin_bookings'))
        conn.execute(
            """UPDATE bookings SET assigned_to=%s, assigned_at=NOW(), priority=%s,
               estimated_duration=%s, internal_notes=%s WHERE id=%s""",
            (staff_id, priority, estimated_duration or None, internal_notes or None, booking_id)
        )
    else:
        conn.execute(
            """UPDATE bookings SET assigned_to=NULL, assigned_at=NULL, priority=%s,
               estimated_duration=%s, internal_notes=%s WHERE id=%s""",
            (priority, estimated_duration or None, internal_notes or None, booking_id)
        )
    conn.commit()
    try:
        if assigned_staff:
            log_activity(f'Assigned booking to {assigned_staff["name"]}', 'booking',
                         target_type='booking', target_id=booking_id,
                         details=f'Booking #{booking_id} → {assigned_staff["name"]} ({assigned_staff["role"]}). Priority: {priority}')
            if assigned_staff.get('phone'):
                send_sms(assigned_staff['phone'],
                         f'New job assigned: {booking["device"]} — {booking["service"]}. '
                         f'Customer: {booking["name"]} ({booking["phone"]}). '
                         f'Date: {booking["date"]}. Priority: {priority}. '
                         f'Check your dashboard. -DonnyPhonehub Gh')
        else:
            log_activity('Unassigned booking', 'booking', target_type='booking', target_id=booking_id,
                         details=f'Booking #{booking_id} unassigned')
    except Exception:
        pass
    conn.close()
    if assigned_staff:
        flash(f'Booking #{booking_id} assigned to {assigned_staff["name"]}.', 'success')
    else:
        flash(f'Booking #{booking_id} unassigned.', 'success')
    if request.form.get('next') == 'my_jobs':
        return redirect(url_for('admin_my_jobs'))
    return redirect(url_for('admin_bookings'))


@app.route('/admin/bookings/bulk-assign', methods=['POST'])
@admin_required
def bulk_assign_bookings():
    if not has_permission('edit_bookings'):
        flash('No permission.', 'error')
        return redirect(url_for('admin'))
    staff_id_raw    = request.form.get('staff_id', '').strip()
    booking_ids_raw = request.form.getlist('booking_ids')
    priority        = request.form.get('priority', 'Normal').strip()
    if not staff_id_raw or not booking_ids_raw:
        flash('Select a technician and at least one booking.', 'error')
        return redirect(url_for('admin_bookings'))
    try:
        staff_id    = int(staff_id_raw)
        booking_ids = [int(b) for b in booking_ids_raw]
    except ValueError:
        flash('Invalid selection.', 'error')
        return redirect(url_for('admin_bookings'))
    conn = get_db()
    staff_member = conn.execute(
        'SELECT name FROM staff WHERE id=%s AND is_active=1', (staff_id,)
    ).fetchone()
    if not staff_member:
        conn.close()
        flash('Staff member not found.', 'error')
        return redirect(url_for('admin_bookings'))
    count = 0
    for bid in booking_ids:
        try:
            conn.execute(
                'UPDATE bookings SET assigned_to=%s, assigned_at=NOW(), priority=%s WHERE id=%s',
                (staff_id, priority, bid)
            )
            count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    try:
        log_activity(f'Bulk assigned {count} bookings', 'booking',
                     details=f'{count} bookings → {staff_member["name"]}. Priority: {priority}')
    except Exception:
        pass
    flash(f'{count} booking(s) assigned to {staff_member["name"]}.', 'success')
    return redirect(url_for('admin_bookings'))


@app.route('/admin/bookings/export')
@admin_required
def admin_bookings_export():
    import csv, io
    if not has_permission('view_bookings'):
        flash('You do not have permission.', 'error')
        return redirect(url_for('admin'))
    search    = request.args.get('search', '').strip()
    service   = request.args.get('service', '').strip()
    status    = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to', '').strip()
    conn = get_db()
    q = ('SELECT id, name, phone, email, device, service, date, '
         'notes, status, customer_id FROM bookings WHERE 1=1')
    params = []
    if search:
        q += ' AND (name ILIKE %s OR email ILIKE %s OR phone ILIKE %s)'
        params += [f'%{search}%'] * 3
    if service:
        q += ' AND service=%s'; params.append(service)
    if status:
        q += ' AND status=%s'; params.append(status)
    if date_from:
        q += ' AND date >= %s'; params.append(date_from)
    if date_to:
        q += ' AND date <= %s'; params.append(date_to)
    q += ' ORDER BY date DESC'
    bookings = conn.execute(q, params).fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Booking ID', 'Customer Name', 'Phone', 'Email',
                     'Device', 'Service', 'Date', 'Status', 'Notes', 'Member ID'])
    for b in bookings:
        writer.writerow([
            b['id'], b['name'], b['phone'], b['email'],
            b['device'], b['service'], b['date'],
            b['status'], b['notes'] or '',
            b['customer_id'] or 'Guest',
        ])
    log_activity('Exported bookings CSV', 'booking',
                 details=f'{len(bookings)} bookings exported'
                         f'{" (filtered)" if any([search, service, status, date_from, date_to]) else ""}')
    today = datetime.today().strftime('%Y-%m-%d')
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=donnyphonehub-bookings-{today}.csv'
    return resp


@app.route('/admin/bookings/export/summary')
@admin_required
def admin_bookings_export_summary():
    import csv, io
    if not has_permission('view_bookings'):
        flash('No permission.', 'error')
        return redirect(url_for('admin'))
    conn = get_db()
    monthly = conn.execute("""
        SELECT TO_CHAR(DATE_TRUNC('month', date::date), 'Mon YYYY') AS month,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status='Complete')    AS completed,
               COUNT(*) FILTER (WHERE status='Pending')     AS pending,
               COUNT(*) FILTER (WHERE status='In Progress') AS in_progress,
               COUNT(*) FILTER (WHERE status='Cancelled')   AS cancelled
        FROM bookings
        WHERE date::date >= CURRENT_DATE - INTERVAL '12 months'
        GROUP BY DATE_TRUNC('month', date::date)
        ORDER BY DATE_TRUNC('month', date::date)
    """).fetchall()
    services = conn.execute("""
        SELECT service, COUNT(*) AS count FROM bookings
        GROUP BY service ORDER BY count DESC
    """).fetchall()
    top_customers = conn.execute("""
        SELECT c.name, c.phone, COUNT(*) AS bookings,
               COUNT(*) FILTER (WHERE b.status='Complete') AS completed
        FROM bookings b JOIN customers c ON c.id=b.customer_id
        WHERE b.customer_id IS NOT NULL
        GROUP BY c.name, c.phone ORDER BY bookings DESC LIMIT 20
    """).fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['=== MONTHLY BOOKING SUMMARY (Last 12 Months) ==='])
    writer.writerow(['Month', 'Total', 'Completed', 'Pending', 'In Progress', 'Cancelled'])
    for m in monthly:
        writer.writerow([m['month'], m['total'], m['completed'],
                         m['pending'], m['in_progress'], m['cancelled']])
    writer.writerow([])
    writer.writerow(['=== SERVICE POPULARITY ==='])
    writer.writerow(['Service', 'Total Bookings'])
    for s in services:
        writer.writerow([s['service'], s['count']])
    writer.writerow([])
    writer.writerow(['=== TOP CUSTOMERS BY BOOKINGS ==='])
    writer.writerow(['Customer', 'Phone', 'Total Bookings', 'Completed'])
    for t in top_customers:
        writer.writerow([t['name'], t['phone'], t['bookings'], t['completed']])
    today = datetime.today().strftime('%Y-%m-%d')
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=donnyphonehub-bookings-summary-{today}.csv'
    return resp


@app.route('/admin/delete/<int:booking_id>', methods=['POST'])
@admin_required
def delete_booking(booking_id):
    if not has_permission('delete_bookings'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin_bookings'))
    conn = get_db()
    conn.execute('DELETE FROM bookings WHERE id=%s', (booking_id,))
    conn.commit(); conn.close()
    logger.warning('Admin %s deleted booking #%d', session.get('admin_username'), booking_id)
    log_activity('Deleted booking', 'booking', target_type='booking', target_id=booking_id,
                 details=f'Booking #{booking_id} deleted')
    flash('Booking deleted.', 'success')
    return redirect(url_for('admin_bookings'))


@app.route('/admin/bookings/<int:booking_id>/status', methods=['POST'])
@admin_required
def update_booking_status(booking_id):
    if not has_permission('edit_bookings'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin_bookings'))
    new_status = request.form.get('status', '')
    if new_status not in ('Pending', 'In Progress', 'Complete', 'Cancelled'):
        flash('Invalid status.', 'error')
        return redirect(url_for('admin_bookings'))
    conn = get_db()
    booking = conn.execute('SELECT * FROM bookings WHERE id=%s', (booking_id,)).fetchone()
    conn.execute('UPDATE bookings SET status=%s WHERE id=%s', (new_status, booking_id))
    conn.commit(); conn.close()
    if booking and new_status == 'Complete':
        try:
            send_email(booking['email'], 'Your repair is ready — DonnyPhonehub Gh', f"""
        <p>Hi {_he(booking['name'])},</p>
        <p>Great news — your <b>{_he(booking['device'])}</b> ({_he(booking['service'])}) is complete and ready for collection.</p>
        <p>Visit us at Tamale, Northern Region or call 0541057500 to arrange pickup.</p>
        <p>— DonnyPhonehub Gh Team</p>
        """)
        except Exception as _email_exc:
            logger.error('Email notification failed: %s', _email_exc)
    log_activity(f'Changed booking status to {new_status}', 'booking',
                 target_type='booking', target_id=booking_id,
                 details=f'Booking #{booking_id}: status → {new_status}')
    flash(f'Booking #{booking_id} marked as {new_status}.', 'success')
    return redirect(url_for('admin_bookings'))


@app.route('/admin/members')
@admin_required
def admin_members():
    if not has_permission('view_members'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    search = request.args.get('search', '').strip()
    tier   = request.args.get('tier', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    conn   = get_db()
    q = 'SELECT * FROM customers WHERE 1=1'
    params = []
    if search:
        q += ' AND (name LIKE %s OR email LIKE %s OR phone LIKE %s)'
        params += [f'%{search}%'] * 3
    if tier:
        q += ' AND membership_tier=%s'; params.append(tier)
    total    = conn.execute(q.replace('SELECT *', 'SELECT COUNT(*) AS cnt'), params).fetchone()['cnt']
    q       += ' ORDER BY created_at DESC LIMIT %s OFFSET %s'
    customers = conn.execute(q, params + [ADMIN_PAGE_SIZE, (page - 1) * ADMIN_PAGE_SIZE]).fetchall()
    # Fetch latest non-completed plan per customer (single query)
    cust_ids = [c['id'] for c in customers]
    plans_map = {}
    if cust_ids:
        try:
            placeholders = ','.join(['%s'] * len(cust_ids))
            plans_rows = conn.execute(
                f'''SELECT DISTINCT ON (customer_id) customer_id, id, status, device_name
                    FROM installment_plans
                    WHERE customer_id IN ({placeholders})
                      AND status IN ('Active','Paused','Defaulted')
                    ORDER BY customer_id, created_at DESC''',
                cust_ids).fetchall()
            for p in plans_rows:
                plans_map[p['customer_id']] = {'plan_id': p['id'], 'plan_status': p['status'], 'plan_device': p['device_name']}
        except Exception as exc:
            logger.error('admin_members plan lookup failed: %s', exc)
    conn.close()
    members = [{
        'id': c['id'], 'name': c['name'], 'phone': c['phone'], 'email': c['email'],
        'device_brand': c['device_brand'], 'device_model': c['device_model'],
        'tier': c['membership_tier'], 'expiry': c['membership_expiry'],
        'status': membership_status(c['membership_expiry']),
        'created_at': c['created_at'].strftime('%Y-%m-%d') if c['created_at'] else None,
        **plans_map.get(c['id'], {'plan_id': None, 'plan_status': None, 'plan_device': None}),
    } for c in customers]
    status_filter = request.args.get('status', '').strip()
    if status_filter:
        members = [m for m in members if m['status'] == status_filter]
        total = len(members)
    total_pages = max(1, -(-total // ADMIN_PAGE_SIZE))
    return render_template('admin_members.html', members=members, search=search, tier=tier,
                           status_filter=status_filter, page=page, total_pages=total_pages, total=total)


@app.route('/admin/members/export')
@admin_required
def admin_members_export():
    import csv, io
    if not has_permission('view_members'):
        flash('You do not have permission.', 'error')
        return redirect(url_for('admin'))
    search        = request.args.get('search', '').strip()
    tier          = request.args.get('tier', '').strip()
    status_filter = request.args.get('status', '').strip()
    conn = get_db()
    q = ('SELECT id, name, phone, email, device_brand, device_model, '
         'membership_tier, membership_start, membership_expiry, '
         'email_verified, created_at FROM customers WHERE 1=1')
    params = []
    if search:
        q += ' AND (name ILIKE %s OR email ILIKE %s OR phone ILIKE %s)'
        params += [f'%{search}%'] * 3
    if tier:
        q += ' AND membership_tier=%s'; params.append(tier)
    q += ' ORDER BY created_at DESC'
    customers = conn.execute(q, params).fetchall()
    plan_map = {}
    try:
        plans = conn.execute(
            """SELECT customer_id, COUNT(*) AS plan_count,
               SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) AS active_plans,
               COALESCE(SUM(total_payable), 0) AS total_value,
               COALESCE(SUM(balance_remaining), 0) AS total_balance
               FROM installment_plans GROUP BY customer_id"""
        ).fetchall()
        for p in plans:
            plan_map[p['customer_id']] = p
    except Exception:
        pass
    payment_map = {}
    try:
        payments = conn.execute(
            """SELECT ip.customer_id, COALESCE(SUM(p.amount), 0) AS total_paid
               FROM payments p JOIN installment_plans ip ON ip.id=p.plan_id
               GROUP BY ip.customer_id"""
        ).fetchall()
        for p in payments:
            payment_map[p['customer_id']] = float(p['total_paid'])
    except Exception:
        pass
    booking_map = {}
    try:
        bcounts = conn.execute(
            """SELECT customer_id, COUNT(*) AS cnt FROM bookings
               WHERE customer_id IS NOT NULL GROUP BY customer_id"""
        ).fetchall()
        for b in bcounts:
            booking_map[b['customer_id']] = b['cnt']
    except Exception:
        pass
    conn.close()
    filtered = []
    for c in customers:
        c_status = membership_status(c['membership_expiry'])
        if status_filter and c_status != status_filter:
            continue
        filtered.append((c, c_status))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Member ID', 'Name', 'Phone', 'Email',
                     'Device Brand', 'Device Model', 'Membership Tier',
                     'Membership Status', 'Start Date', 'Expiry Date',
                     'Email Verified', 'Registered', 'Total Bookings',
                     'Installment Plans', 'Active Plans',
                     'Total Plan Value', 'Total Paid', 'Outstanding Balance'])
    for c, c_status in filtered:
        cid   = c['id']
        pinfo = plan_map.get(cid, {})
        writer.writerow([
            c['id'], c['name'], c['phone'], c['email'],
            c['device_brand'] or '', c['device_model'] or '',
            c['membership_tier'], c_status,
            c['membership_start'] or '', c['membership_expiry'] or '',
            'Yes' if c['email_verified'] else 'No',
            str(c['created_at'])[:10] if c['created_at'] else '',
            booking_map.get(cid, 0),
            pinfo.get('plan_count', 0),
            pinfo.get('active_plans', 0),
            round(float(pinfo.get('total_value', 0)), 2),
            round(payment_map.get(cid, 0), 2),
            round(float(pinfo.get('total_balance', 0)), 2),
        ])
    log_activity('Exported members CSV', 'member',
                 details=f'{len(filtered)} members exported'
                         f'{" (filtered)" if any([search, tier, status_filter]) else ""}')
    today = datetime.today().strftime('%Y-%m-%d')
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=donnyphonehub-members-{today}.csv'
    return resp


@app.route('/admin/members/<int:customer_id>')
@admin_required
def admin_member_detail(customer_id):
    if not has_permission('view_members'):
        flash('You do not have permission.', 'error')
        return redirect(url_for('admin'))
    conn = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=%s', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Member not found.', 'error')
        return redirect(url_for('admin_members'))
    mem_status = membership_status(customer['membership_expiry'])
    bookings = conn.execute(
        'SELECT * FROM bookings WHERE customer_id=%s ORDER BY date DESC',
        (customer_id,)
    ).fetchall()
    booking_stats = conn.execute(
        """SELECT
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE status='Complete') AS completed,
           COUNT(*) FILTER (WHERE status='Pending') AS pending,
           COUNT(*) FILTER (WHERE status='In Progress') AS in_progress,
           COUNT(*) FILTER (WHERE status='Cancelled') AS cancelled
           FROM bookings WHERE customer_id=%s""",
        (customer_id,)
    ).fetchone()
    plans = conn.execute(
        'SELECT * FROM installment_plans WHERE customer_id=%s ORDER BY created_at DESC',
        (customer_id,)
    ).fetchall()
    payments = conn.execute(
        """SELECT p.*, ip.device_name, ip.id AS plan_id
           FROM payments p
           JOIN installment_plans ip ON ip.id=p.plan_id
           WHERE ip.customer_id=%s
           ORDER BY p.created_at DESC""",
        (customer_id,)
    ).fetchall()
    payment_stats = conn.execute(
        """SELECT
           COALESCE(SUM(p.amount), 0) AS total_paid,
           COUNT(p.id) AS payment_count
           FROM payments p
           JOIN installment_plans ip ON ip.id=p.plan_id
           WHERE ip.customer_id=%s""",
        (customer_id,)
    ).fetchone()
    reservations = conn.execute(
        """SELECT r.*, i.brand, i.model, i.color, i.storage, i.selling_price
           FROM reservations r
           JOIN inventory i ON i.id=r.item_id
           WHERE r.customer_id=%s
           ORDER BY r.created_at DESC""",
        (customer_id,)
    ).fetchall()
    devices_owned = conn.execute(
        """SELECT brand, model, color, storage, selling_price, updated_at
           FROM inventory
           WHERE sold_to=%s
           ORDER BY updated_at DESC""",
        (customer_id,)
    ).fetchall()
    try:
        activity = conn.execute(
            """SELECT * FROM activity_log
               WHERE target_type='customer'
               AND target_id=%s
               ORDER BY created_at DESC
               LIMIT 20""",
            (customer_id,)
        ).fetchall()
    except Exception:
        activity = []
    conn.close()
    total_spent = float(payment_stats['total_paid'])
    days_as_member = 0
    if customer['membership_start']:
        try:
            start = datetime.strptime(customer['membership_start'], '%Y-%m-%d')
            days_as_member = (datetime.today() - start).days
        except ValueError:
            pass
    return render_template('admin_member_detail.html',
        customer=customer,
        mem_status=mem_status,
        bookings=bookings,
        booking_stats=booking_stats,
        plans=plans,
        payments=payments,
        payment_stats=payment_stats,
        total_spent=total_spent,
        reservations=reservations,
        devices_owned=devices_owned,
        activity=activity,
        days_as_member=days_as_member,
    )


@app.route('/admin/members/delete/<int:customer_id>', methods=['POST'])
@admin_required
def delete_member(customer_id):
    if not has_permission('delete_members'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    conn = get_db()
    customer = conn.execute('SELECT id, name, email FROM customers WHERE id=%s', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Member not found.', 'error')
        return redirect(url_for('admin_members'))
    try:
        # Delete in FK-safe order: payments → plans → bookings → inventory ref → tokens → customer
        conn.execute(
            'DELETE FROM payments WHERE plan_id IN '
            '(SELECT id FROM installment_plans WHERE customer_id=%s)',
            (customer_id,))
        conn.execute('DELETE FROM installment_plans WHERE customer_id=%s', (customer_id,))
        conn.execute('DELETE FROM bookings WHERE customer_id=%s', (customer_id,))
        conn.execute('UPDATE inventory SET sold_to=NULL WHERE sold_to=%s', (customer_id,))
        conn.execute('DELETE FROM email_verification_tokens WHERE customer_id=%s', (customer_id,))
        conn.execute('DELETE FROM customers WHERE id=%s', (customer_id,))
        conn.commit()
        logger.warning('Admin %s deleted member #%d (%s)', session.get('admin_username'), customer_id, customer['email'])
        log_activity('Deleted member', 'member', target_type='customer', target_id=customer_id,
                     details=f'Deleted member "{customer["name"]}" ({customer["email"]}) and all records')
        flash(f'Member "{customer["name"]}" and all related records deleted.', 'success')
    except Exception as exc:
        conn.rollback()
        logger.error('delete_member #%d failed: %s', customer_id, exc)
        flash('Could not delete member — please try again.', 'error')
    finally:
        conn.close()
    return redirect(url_for('admin_members'))


@app.route('/admin/installments')
@admin_required
def admin_installments():
    if not has_permission('view_installments'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    status_filter = request.args.get('status', '').strip()
    search        = request.args.get('search', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    conn          = get_db()
    q = '''SELECT ip.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email
           FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id WHERE 1=1'''
    params = []
    if status_filter:
        q += ' AND ip.status=%s'; params.append(status_filter)
    if search:
        q += ' AND (c.name LIKE %s OR c.email LIKE %s OR ip.device_name LIKE %s)'
        params += [f'%{search}%'] * 3
    count_q           = q.replace('SELECT ip.*, c.name as customer_name, c.phone as customer_phone, c.email as customer_email', 'SELECT COUNT(*) AS cnt')
    total             = conn.execute(count_q, params).fetchone()['cnt']
    today             = datetime.today().strftime('%Y-%m-%d')
    stats_q           = q + ' ORDER BY ip.created_at DESC'
    all_plans         = conn.execute(stats_q, params).fetchall()
    total_outstanding = sum(p['balance_remaining'] for p in all_plans if p['status'] == 'Active')
    active_count      = sum(1 for p in all_plans if p['status'] == 'Active')
    completed_count   = sum(1 for p in all_plans if p['status'] == 'Completed')

    q      += ' ORDER BY ip.created_at DESC LIMIT %s OFFSET %s'
    plans   = conn.execute(q, params + [ADMIN_PAGE_SIZE, (page - 1) * ADMIN_PAGE_SIZE]).fetchall()
    paid_map = {row['plan_id']: row['total_paid'] for row in conn.execute(
        'SELECT plan_id, COALESCE(SUM(amount),0) AS total_paid FROM payments GROUP BY plan_id'
    ).fetchall()}
    annotated = []
    for p in plans:
        paid    = paid_map.get(p['id'], 0)
        overdue = (p['status'] == 'Active' and p['next_due_date'] < today)
        annotated.append({**dict(p), 'paid_total': paid, 'overdue': overdue})

    overdue_count = sum(1 for p in annotated if p['overdue'])
    conn.close()
    total_pages = max(1, -(-total // ADMIN_PAGE_SIZE))

    return render_template('admin_installments.html',
                           plans=annotated,
                           total_outstanding=total_outstanding,
                           active_count=active_count,
                           overdue_count=overdue_count,
                           completed_count=completed_count,
                           status_filter=status_filter,
                           search=search,
                           bank_details=BANK_DETAILS,
                           page=page, total_pages=total_pages, total=total)


@app.route('/admin/installments/<int:plan_id>/record-payment', methods=['POST'])
@admin_required
def record_payment(plan_id):
    if not has_permission('record_payment'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    try:
        amount = float(request.form['amount'])
    except (ValueError, KeyError):
        flash('Invalid payment amount.', 'error')
        return redirect(url_for('admin_installments'))

    method    = request.form.get('payment_method', '').strip()
    if method not in ('Cash', 'MTN MoMo', 'Vodafone Cash', 'AirtelTigo Money', 'Bank Transfer', 'Bank Deposit'):
        flash('Invalid payment method.', 'error')
        return redirect(url_for('admin_installments'))
    reference = request.form.get('reference', '').strip()
    notes     = request.form.get('notes', '').strip()
    _paid_on_raw = request.form.get('paid_on', '').strip()
    try:
        paid_on = datetime.strptime(_paid_on_raw, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        paid_on = datetime.today().strftime('%Y-%m-%d')

    conn = get_db()
    plan = conn.execute(
        '''SELECT ip.*, c.name AS customer_name, c.phone AS customer_phone
           FROM installment_plans ip
           JOIN customers c ON c.id = ip.customer_id
           WHERE ip.id=%s''',
        (plan_id,)).fetchone()
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

    # Duplicate guard — same plan + date + amount within last 60 seconds
    duplicate = conn.execute(
        '''SELECT id FROM payments
           WHERE plan_id=%s AND paid_on=%s AND amount=%s
             AND created_at >= NOW() - INTERVAL '60 seconds' ''',
        (plan_id, paid_on, amount)).fetchone()
    if duplicate:
        conn.close()
        flash('Duplicate payment detected — this payment was already recorded moments ago.', 'error')
        return redirect(url_for('admin_installments'))

    try:
        cur = conn.execute(
            'INSERT INTO payments (plan_id,amount,paid_on,payment_method,reference,notes) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
            (plan_id, amount, paid_on, method, reference or None, notes or None))
        payment_id = cur.fetchone()['id']

        new_balance       = float(_d(max(_d(plan['balance_remaining']) - _d(amount), Decimal('0'))))
        new_payments_made = plan['payments_made'] + 1
        new_next_due      = add_one_month(plan['next_due_date'])
        new_status        = 'Completed' if new_balance <= 0.01 else plan['status']

        conn.execute(
            'UPDATE installment_plans SET balance_remaining=%s,payments_made=%s,next_due_date=%s,status=%s WHERE id=%s',
            (new_balance, new_payments_made, new_next_due, new_status, plan_id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        logger.error('record_payment plan #%d failed: %s', plan_id, exc)
        flash('Payment could not be recorded — please try again.', 'error')
        return redirect(url_for('admin_installments'))

    logger.info('Admin %s recorded payment for plan #%d', session.get('admin_username'), plan_id)
    conn.close()
    log_activity('Recorded payment', 'installment', target_type='payment', target_id=payment_id,
                 details=f'Plan #{plan_id}: {fmt_ghs(amount)} via {method}. Balance: {fmt_ghs(new_balance)}')

    customer_name  = plan.get('customer_name') or ''
    customer_phone = plan.get('customer_phone') or ''
    first = customer_name.split()[0] if customer_name else 'Customer'
    if new_status == 'Completed':
        send_sms(customer_phone,
                 f"Hi {first}, your DonnyPhonehub Gh installment for {plan['device_name']} "
                 f"is now FULLY PAID! Thank you. Call 0541057500 for your receipt.")
        flash(f'Plan #{plan_id} fully paid — marked Completed. Receipt: /receipt/payment/{payment_id}', 'success')
    else:
        send_sms(customer_phone,
                 f"Hi {first}, payment of {fmt_ghs(amount)} received for your {plan['device_name']} plan. "
                 f"Balance: {fmt_ghs(new_balance)}. Next due: {new_next_due}. -DonnyPhonehub Gh")
        flash(f'Payment of {fmt_ghs(amount)} recorded for plan #{plan_id}.', 'success')
    return redirect(url_for('admin_installments', last_payment=payment_id))


@app.route('/admin/installments/<int:plan_id>/update-status', methods=['POST'])
@admin_required
def update_plan_status(plan_id):
    if not has_permission('edit_installments'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    new_status = request.form.get('status', '')
    if new_status not in ('Active', 'Paused', 'Completed', 'Defaulted'):
        flash('Invalid status value.', 'error')
        return redirect(url_for('admin_installments'))
    conn = get_db()
    conn.execute('UPDATE installment_plans SET status=%s WHERE id=%s', (new_status, plan_id))
    conn.commit(); conn.close()
    logger.info('Admin %s set plan #%d status to %s', session.get('admin_username'), plan_id, new_status)
    log_activity(f'Changed plan status to {new_status}', 'installment',
                 target_type='plan', target_id=plan_id,
                 details=f'Plan #{plan_id}: status → {new_status}')
    flash(f'Plan #{plan_id} updated to {new_status}.', 'success')
    back = request.form.get('next', 'admin_installments')
    if back == 'admin_members':
        return redirect(url_for('admin_members'))
    return redirect(url_for('admin_installments'))


# ─── PDF RECEIPT ROUTES ───────────────────────────────────────────────────────

@app.route('/receipt/booking/<int:booking_id>')
def booking_receipt(booking_id):
    conn    = get_db()
    booking = conn.execute('SELECT * FROM bookings WHERE id=%s', (booking_id,)).fetchone()
    conn.close()
    if not booking:
        return render_template('404.html'), 404
    is_admin  = session.get('admin_logged_in')
    is_owner  = (session.get('customer_id') and
                 booking['customer_id'] == session['customer_id'])
    is_guest  = booking_id in session.get('guest_booking_ids', [])
    if not is_admin and not is_owner and not is_guest:
        flash('Please log in to download your receipt.', 'error')
        return redirect(url_for('customer_login'))
    buf  = generate_booking_receipt_pdf(dict(booking))
    resp = make_response(buf.read())
    resp.headers['Content-Type']        = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=phonehub-booking-{booking_id:05d}.pdf'
    return resp


@app.route('/receipt/payment/<int:payment_id>')
@admin_required
def payment_receipt(payment_id):
    if not has_permission('view_installments'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    conn    = get_db()
    payment = conn.execute('SELECT * FROM payments WHERE id=%s', (payment_id,)).fetchone()
    if not payment:
        conn.close()
        flash('Payment not found.', 'error')
        return redirect(url_for('admin_installments'))
    plan = conn.execute(
        '''SELECT ip.*, c.name as customer_name
           FROM installment_plans ip
           JOIN customers c ON c.id = ip.customer_id
           WHERE ip.id=%s''',
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
        'SELECT * FROM payments WHERE plan_id=%s ORDER BY created_at DESC LIMIT 1', (plan_id,)).fetchone()
    if not payment:
        conn.close()
        flash('No payments recorded for this plan yet.', 'error')
        return redirect(url_for('admin_installments'))
    plan = conn.execute(
        '''SELECT ip.*, c.name as customer_name
           FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id
           WHERE ip.id=%s''', (plan_id,)).fetchone()
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
    if not has_permission('send_reminders'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    days    = int(request.form.get('days', 3))
    today   = datetime.today()
    cutoff  = (today + timedelta(days=days)).strftime('%Y-%m-%d')
    today_s = today.strftime('%Y-%m-%d')

    conn  = get_db()
    plans = conn.execute(
        '''SELECT ip.*, c.name as customer_name, c.phone as customer_phone
           FROM installment_plans ip
           JOIN customers c ON c.id = ip.customer_id
           WHERE ip.status = 'Active' AND ip.next_due_date <= %s
           ORDER BY ip.next_due_date''',
        (cutoff,)).fetchall()
    conn.close()

    sent = skipped = 0
    for p in plans:
        overdue = p['next_due_date'] < today_s
        first   = (p['customer_name'].split() or ['Customer'])[0]
        if overdue:
            msg = (f"Hi {first}, your DonnyPhonehub Gh installment of "
                   f"{fmt_ghs(p['monthly_amount'])} for {p['device_name']} "
                   f"was DUE {p['next_due_date']}. Please pay now via "
                   f"{p['payment_method']} & call 0541057500. "
                   f"Balance: {fmt_ghs(p['balance_remaining'])}.")
        else:
            msg = (f"Hi {first}, your DonnyPhonehub Gh installment of "
                   f"{fmt_ghs(p['monthly_amount'])} for {p['device_name']} "
                   f"is due {p['next_due_date']}. Pay via "
                   f"{p['payment_method']}. Balance: {fmt_ghs(p['balance_remaining'])}. "
                   f"Questions? Call 0541057500.")
        if send_sms(p['customer_phone'], msg):
            sent += 1
        else:
            skipped += 1

    total = len(plans)
    log_activity('Sent payment reminders', 'installment',
                 details=f'Sent {sent} SMS, {skipped} failed, {total} total plans targeted')
    if not ARKESEL_API_KEY:
        flash(f'SMS not configured — set ARKESEL_API_KEY env var. Would have sent {total} reminder(s).', 'error')
    else:
        flash(f'Sent {sent} SMS reminder(s). {skipped} failed (check ARKESEL_API_KEY / phone numbers).', 'success')
    return redirect(url_for('admin_installments'))


# ─── EMAIL VERIFICATION ───────────────────────────────────────────────────────

@app.route('/verify-email/<token>')
def verify_email(token):
    conn = get_db()
    row  = conn.execute(
        'SELECT * FROM email_verification_tokens WHERE token=%s AND used=0', (token,)).fetchone()
    if not row:
        conn.close()
        flash('Verification link is invalid or already used.', 'error')
        return redirect(url_for('dashboard'))
    expires = datetime.fromisoformat(row['expires_at']).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        conn.close()
        flash('Verification link has expired. Request a new one from your dashboard.', 'error')
        return redirect(url_for('dashboard'))
    conn.execute('UPDATE customers SET email_verified=1 WHERE id=%s', (row['customer_id'],))
    conn.execute('UPDATE email_verification_tokens SET used=1 WHERE id=%s', (row['id'],))
    conn.commit(); conn.close()
    flash('Email verified! Your account is fully active.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/resend-verification', methods=['POST'])
@customer_required
@limiter.limit('3 per hour')
def resend_verification():
    conn = None
    try:
        conn = get_db()
        customer = conn.execute('SELECT * FROM customers WHERE id=%s', (session['customer_id'],)).fetchone()
        if not customer:
            flash('Account not found. Please log in again.', 'error')
            return redirect(url_for('customer_logout'))
        if customer['email_verified']:
            flash('Your email is already verified.', 'success')
            return redirect(url_for('dashboard'))
        conn.execute('UPDATE email_verification_tokens SET used=1 WHERE customer_id=%s', (customer['id'],))
        v_token  = secrets.token_urlsafe(32)
        v_expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        conn.execute(
            'INSERT INTO email_verification_tokens (customer_id,token,expires_at) VALUES (%s,%s,%s)',
            (customer['id'], v_token, v_expiry))
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        flash('Something went wrong. Please try again later.', 'error')
        return redirect(url_for('dashboard'))
    finally:
        if conn:
            conn.close()
    verify_url = url_for('verify_email', token=v_token, _external=True)
    try:
        send_email(customer['email'], 'Verify your email — DonnyPhonehub Gh', f"""
    <p>Hi {_he(customer['name'])},</p>
    <p>Click below to verify your email address:</p>
    <p><a href="{verify_url}" style="background:#006B3F;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Verify My Email</a></p>
    <p style="font-size:13px;color:#666;margin-top:12px">Link expires in 24 hours.</p>
    <p>— DonnyPhonehub Gh Team</p>
    """)
        flash(
            'Verification email sent — check your inbox.',
            'success')
    except Exception as _email_exc:
        logger.error(
            'Resend verification failed for customer '
            '#%d: %s',
            session['customer_id'], _email_exc)
        flash(
            'Could not send email right now. '
            'Please try again in a few minutes.',
            'error')
    return redirect(url_for('dashboard'))


# ─── PASSWORD RESET ───────────────────────────────────────────────────────────

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per hour', methods=['POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn  = get_db()
        customer = conn.execute('SELECT * FROM customers WHERE email=%s', (email,)).fetchone()
        if customer:
            conn.execute('UPDATE password_reset_tokens SET used=1 WHERE email=%s', (email,))
            token   = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            conn.execute(
                'INSERT INTO password_reset_tokens (email,token,expires_at) VALUES (%s,%s,%s)',
                (email, token, expires))
            conn.commit()
            reset_url = url_for('reset_password', token=token, _external=True)
            try:
                send_email(email, 'Reset your password — DonnyPhonehub Gh', f"""
            <p>Hi {_he(customer['name'])},</p>
            <p>We received a request to reset your DonnyPhonehub Gh password.</p>
            <p><a href="{reset_url}" style="background:#006B3F;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Reset Password</a></p>
            <p style="font-size:13px;color:#666;margin-top:12px">This link expires in 30 minutes. If you didn't request this, ignore the email.</p>
            <p>— DonnyPhonehub Gh Team</p>
            """)
            except Exception as _email_exc:
                logger.error(
                    'Password reset email failed for '
                    '%s: %s', email, _email_exc)
        conn.close()
        flash('If an account with that email exists, a reset link has been sent.', 'success')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db()
    row  = conn.execute(
        'SELECT * FROM password_reset_tokens WHERE token=%s AND used=0', (token,)).fetchone()
    if not row:
        conn.close()
        flash('Reset link is invalid or already used.', 'error')
        return redirect(url_for('forgot_password'))
    expires = datetime.fromisoformat(row['expires_at']).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        conn.close()
        flash('Reset link has expired. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        pw  = request.form.get('password', '')
        pw2 = request.form.get('password2', '')
        pw_err = validate_password(pw)
        if pw_err:
            conn.close()
            flash(pw_err, 'error')
            return render_template('reset_password.html', token=token)
        if pw != pw2:
            conn.close()
            flash('Passwords do not match.', 'error')
            return render_template('reset_password.html', token=token)
        conn.execute('UPDATE customers SET password_hash=%s WHERE email=%s',
                     (generate_password_hash(pw), row['email']))
        conn.execute('UPDATE password_reset_tokens SET used=1 WHERE id=%s', (row['id'],))
        conn.commit(); conn.close()
        flash('Password updated. Please log in.', 'success')
        return redirect(url_for('customer_login'))
    conn.close()
    return render_template('reset_password.html', token=token)


# ─── ADMIN MEMBERSHIP EXTENSION ───────────────────────────────────────────────

@app.route('/admin/members/<int:customer_id>/update-membership', methods=['POST'])
@admin_required
def update_membership(customer_id):
    if not has_permission('edit_members'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    tier   = request.form.get('tier', '').strip()
    expiry = request.form.get('expiry', '').strip()
    if tier not in ('Standard', 'Silver', 'Gold', 'Premium'):
        flash('Invalid membership tier.', 'error')
        return redirect(url_for('admin_members'))
    try:
        datetime.strptime(expiry, '%Y-%m-%d')
    except ValueError:
        flash('Invalid expiry date.', 'error')
        return redirect(url_for('admin_members'))
    conn = get_db()
    customer = conn.execute('SELECT id FROM customers WHERE id=%s', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Member not found.', 'error')
        return redirect(url_for('admin_members'))
    conn.execute('UPDATE customers SET membership_tier=%s, membership_expiry=%s WHERE id=%s',
                 (tier, expiry, customer_id))
    conn.commit(); conn.close()
    logger.info('Admin %s updated membership for customer #%d: tier=%s expiry=%s',
                session.get('admin_username'), customer_id, tier, expiry)
    log_activity('Updated membership', 'member', target_type='customer', target_id=customer_id,
                 details=f'Tier → {tier}, Expiry → {expiry}')
    flash(f'Membership updated — {tier}, expires {expiry}.', 'success')
    return redirect(url_for('admin_member_detail', customer_id=customer_id))


@app.route('/admin/members/<int:customer_id>/extend', methods=['POST'])
@admin_required
def extend_membership(customer_id):
    if not has_permission('extend_membership'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    try:
        months = int(request.form.get('months', 0))
    except ValueError:
        months = 0
    if months not in (1, 3, 6, 12):
        flash('Invalid extension period.', 'error')
        return redirect(url_for('admin_members'))
    conn     = get_db()
    customer = conn.execute('SELECT * FROM customers WHERE id=%s', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Member not found.', 'error')
        return redirect(url_for('admin_members'))
    current_expiry = customer['membership_expiry']
    try:
        base = max(datetime.strptime(current_expiry, '%Y-%m-%d'), datetime.today())
    except (ValueError, TypeError):
        base = datetime.today()
    new_expiry = (base + timedelta(days=30 * months)).strftime('%Y-%m-%d')
    conn.execute('UPDATE customers SET membership_expiry=%s WHERE id=%s', (new_expiry, customer_id))
    conn.commit(); conn.close()
    logger.info('Admin %s extended membership for customer #%d by %d months (new expiry: %s)',
                session.get('admin_username'), customer_id, months, new_expiry)
    log_activity('Extended membership', 'member', target_type='customer', target_id=customer_id,
                 details=f'Extended by {months} month(s). New expiry: {new_expiry}')
    flash(f'Membership extended by {months} month(s). New expiry: {new_expiry}.', 'success')
    return redirect(url_for('admin_member_detail', customer_id=customer_id))


@app.route('/admin/members/<int:customer_id>/message', methods=['POST'])
@admin_required
def admin_member_message(customer_id):
    import html as _html
    if not has_permission('edit_members'):
        flash('You do not have permission to message members.', 'error')
        return redirect(url_for('admin_members'))

    subject = request.form.get('subject', '').strip()
    body    = request.form.get('body', '').strip()
    if not subject or not body:
        flash('Subject and message are required.', 'error')
        return redirect(url_for('admin_members'))

    conn = get_db()
    try:
        customer = conn.execute(
            'SELECT name, email FROM customers WHERE id=%s', (customer_id,)
        ).fetchone()
    finally:
        conn.close()

    if not customer:
        flash('Customer not found.', 'error')
        return redirect(url_for('admin_members'))

    customer_email = (customer['email'] or '').strip()
    if not customer_email:
        flash(f'No email on record for {customer["name"]}.', 'warning')
        return redirect(url_for('admin_members'))

    safe_name = _html.escape(customer['name'] or '')
    safe_body = _html.escape(body).replace('\n', '<br>')

    html_body = f"""
    <div style="font-family:'DM Sans',Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E8E4DC;">
      <div style="background:#006B3F;padding:28px 32px;">
        <div style="font-family:'Syne',Arial,sans-serif;font-size:20px;font-weight:800;color:#fff;">
          Donny<span style="color:#FCD116;">Phonehub</span> Gh
        </div>
      </div>
      <div style="padding:32px;">
        <p style="color:#4A4740;font-size:15px;line-height:1.6;margin:0 0 20px;">
          Hi {safe_name},
        </p>
        <div style="font-size:15px;color:#111008;line-height:1.7;">
          {safe_body}
        </div>
        <p style="font-size:14px;color:#4A4740;line-height:1.6;margin-top:28px;">
          Questions? Call us on
          <a href="tel:+233541057500" style="color:#006B3F;font-weight:600;">0541 057 500</a>
          or reply to this email.
        </p>
      </div>
      <div style="background:#F7F5F0;padding:20px 32px;text-align:center;font-size:13px;color:#8C8880;">
        &copy; 2026 DonnyPhonehub Gh Ltd. &mdash; Tamale, Northern Region, Ghana
      </div>
    </div>
    """
    sent = send_email(customer_email, subject, html_body)
    if sent:
        logger.info('Admin %s messaged member #%d (%s)', session.get('admin_username'), customer_id, customer_email)
        flash(f'Message sent to {customer["name"]} ({customer_email}).', 'success')
    else:
        flash('Could not send email — check MAIL_USER / MAIL_PASS settings.', 'warning')

    return redirect(url_for('admin_members'))


# ─── INVENTORY ROUTES ─────────────────────────────────────────────────────────

INVENTORY_CONDITIONS = ('New', 'Certified Pre-Owned', 'Good', 'Fair')
INVENTORY_STATUSES   = ('In Stock', 'Reserved', 'Sold', 'Under Repair', 'Returned')


@app.route('/admin/inventory')
@admin_required
def admin_inventory():
    search           = request.args.get('search', '').strip()
    status_filter    = request.args.get('status', '').strip()
    condition_filter = request.args.get('condition', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db()

    # Stats
    stats = conn.execute("""
        SELECT
          COUNT(*) FILTER (WHERE status='In Stock')                         AS total_stock,
          COUNT(*) FILTER (WHERE status='Reserved')                         AS total_reserved,
          COUNT(*) FILTER (WHERE status='Sold')                             AS total_sold,
          COALESCE(SUM(cost_price)    FILTER (WHERE status IN ('In Stock','Reserved')), 0) AS stock_value,
          COALESCE(SUM(selling_price) FILTER (WHERE status IN ('In Stock','Reserved')), 0) AS potential_revenue,
          COALESCE(SUM(selling_price - cost_price) FILTER (
              WHERE status='Sold'
              AND DATE_TRUNC('month', updated_at) = DATE_TRUNC('month', NOW())
          ), 0) AS monthly_profit
        FROM inventory
    """).fetchone()

    # Main query
    q      = 'SELECT i.*, c.name AS customer_name FROM inventory i LEFT JOIN customers c ON c.id=i.sold_to WHERE 1=1'
    params = []
    if search:
        q += ' AND (i.brand ILIKE %s OR i.model ILIKE %s OR i.imei ILIKE %s)'
        params += [f'%{search}%'] * 3
    if status_filter:
        q += ' AND i.status=%s'; params.append(status_filter)
    if condition_filter:
        q += ' AND i.condition=%s'; params.append(condition_filter)

    total      = conn.execute(q.replace('SELECT i.*, c.name AS customer_name', 'SELECT COUNT(*) AS cnt'), params).fetchone()['cnt']
    q         += ' ORDER BY i.created_at DESC LIMIT %s OFFSET %s'
    items      = conn.execute(q, params + [ADMIN_PAGE_SIZE, (page - 1) * ADMIN_PAGE_SIZE]).fetchall()
    # Active plans for reserve modal
    active_plans = conn.execute(
        "SELECT id, device_name FROM installment_plans WHERE status='Active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    total_pages = max(1, -(-total // ADMIN_PAGE_SIZE))
    return render_template('admin_inventory.html',
        inventory=items, total_stock=stats['total_stock'],
        total_reserved=stats['total_reserved'], total_sold=stats['total_sold'],
        stock_value=stats['stock_value'], potential_revenue=stats['potential_revenue'],
        monthly_profit=stats['monthly_profit'],
        search=search, status_filter=status_filter, condition_filter=condition_filter,
        page=page, total_pages=total_pages, total=total,
        active_plans=active_plans,
        conditions=INVENTORY_CONDITIONS, statuses=INVENTORY_STATUSES)


@app.route('/admin/inventory/add', methods=['POST'])
@admin_required
def admin_inventory_add():
    brand    = request.form.get('brand', '').strip()[:100]
    model    = request.form.get('model', '').strip()[:100]
    imei     = request.form.get('imei', '').strip()[:20] or None
    cond     = request.form.get('condition', '').strip()
    color    = request.form.get('color', '').strip() or None
    storage  = request.form.get('storage', '').strip() or None
    notes    = request.form.get('notes', '').strip() or None

    if not brand or not model:
        flash('Brand and model are required.', 'error')
        return redirect(url_for('admin_inventory'))
    if cond not in INVENTORY_CONDITIONS:
        flash('Invalid condition value.', 'error')
        return redirect(url_for('admin_inventory'))
    try:
        cost_price    = float(request.form.get('cost_price', 0))
        selling_price = float(request.form.get('selling_price', 0))
    except ValueError:
        flash('Prices must be valid numbers.', 'error')
        return redirect(url_for('admin_inventory'))
    if cost_price <= 0 or selling_price <= 0:
        flash('Prices must be greater than zero.', 'error')
        return redirect(url_for('admin_inventory'))
    if selling_price < cost_price:
        flash('Selling price must be at least equal to cost price.', 'error')
        return redirect(url_for('admin_inventory'))

    conn = get_db()
    if imei:
        if conn.execute('SELECT id FROM inventory WHERE imei=%s', (imei,)).fetchone():
            conn.close()
            flash(f'IMEI {imei} already exists in inventory.', 'error')
            return redirect(url_for('admin_inventory'))
    cur = conn.execute(
        '''INSERT INTO inventory (brand,model,imei,condition,cost_price,selling_price,color,storage,notes,added_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (brand, model, imei, cond, cost_price, selling_price, color, storage, notes, session.get('admin_username', 'admin')))
    item_id = cur.fetchone()['id']
    conn.commit()

    image1_file = request.files.get('image1')
    image2_file = request.files.get('image2')
    img1_result = upload_image_to_cloudinary(image1_file, item_id, 1) if image1_file and image1_file.filename else None
    img2_result = upload_image_to_cloudinary(image2_file, item_id, 2) if image2_file and image2_file.filename else None

    if img1_result or img2_result:
        conn.execute(
            '''UPDATE inventory SET image1_url=%s, image1_public_id=%s,
               image2_url=%s, image2_public_id=%s WHERE id=%s''',
            (
                img1_result['url'] if img1_result else None,
                img1_result['public_id'] if img1_result else None,
                img2_result['url'] if img2_result else None,
                img2_result['public_id'] if img2_result else None,
                item_id
            )
        )
        conn.commit()

    conn.close()
    logger.info('Admin %s added inventory: %s %s', session.get('admin_username'), brand, model)
    log_activity('Added device to inventory', 'inventory', target_type='inventory', target_id=item_id,
                 details=f'{brand} {model} — cost {fmt_ghs(cost_price)}, sell {fmt_ghs(selling_price)}')
    count = sum(1 for r in [img1_result, img2_result] if r)
    if count:
        flash(f'Device added with {count} photo(s) uploaded.', 'success')
    elif (image1_file and image1_file.filename) or (image2_file and image2_file.filename):
        flash('Device added but photo upload failed. You can add photos by editing the device.', 'success')
    else:
        flash('Device added to inventory.', 'success')
    return redirect(url_for('admin_inventory'))


@app.route('/admin/inventory/<int:item_id>/edit', methods=['POST'])
@admin_required
def admin_inventory_edit(item_id):
    brand    = request.form.get('brand', '').strip()[:100]
    model    = request.form.get('model', '').strip()[:100]
    imei     = request.form.get('imei', '').strip()[:20] or None
    cond     = request.form.get('condition', '').strip()
    status   = request.form.get('status', '').strip()
    color    = request.form.get('color', '').strip() or None
    storage  = request.form.get('storage', '').strip() or None
    notes    = request.form.get('notes', '').strip() or None

    if not brand or not model:
        flash('Brand and model are required.', 'error')
        return redirect(url_for('admin_inventory'))
    if cond not in INVENTORY_CONDITIONS:
        flash('Invalid condition value.', 'error')
        return redirect(url_for('admin_inventory'))
    if status not in INVENTORY_STATUSES:
        flash('Invalid status value.', 'error')
        return redirect(url_for('admin_inventory'))
    try:
        cost_price    = float(request.form.get('cost_price', 0))
        selling_price = float(request.form.get('selling_price', 0))
    except ValueError:
        flash('Prices must be valid numbers.', 'error')
        return redirect(url_for('admin_inventory'))
    if cost_price <= 0 or selling_price <= 0:
        flash('Prices must be greater than zero.', 'error')
        return redirect(url_for('admin_inventory'))
    if selling_price < cost_price:
        flash('Selling price must be at least equal to cost price.', 'error')
        return redirect(url_for('admin_inventory'))

    conn = get_db()
    current = conn.execute('SELECT * FROM inventory WHERE id=%s', (item_id,)).fetchone()
    if not current:
        conn.close()
        flash('Device not found.', 'error')
        return redirect(url_for('admin_inventory'))
    if imei:
        dup = conn.execute('SELECT id FROM inventory WHERE imei=%s AND id!=%s', (imei, item_id)).fetchone()
        if dup:
            conn.close()
            flash(f'IMEI {imei} already exists on another device.', 'error')
            return redirect(url_for('admin_inventory'))
    conn.execute(
        'UPDATE inventory SET brand=%s,model=%s,imei=%s,condition=%s,cost_price=%s,selling_price=%s,color=%s,storage=%s,notes=%s,status=%s,updated_at=NOW() WHERE id=%s',
        (brand, model, imei, cond, cost_price, selling_price, color, storage, notes, status, item_id))
    conn.commit()

    image1_file   = request.files.get('image1')
    image2_file   = request.files.get('image2')
    delete_image1 = request.form.get('delete_image1') == '1'
    delete_image2 = request.form.get('delete_image2') == '1'

    img1_url       = current.get('image1_url')
    img1_public_id = current.get('image1_public_id')
    img2_url       = current.get('image2_url')
    img2_public_id = current.get('image2_public_id')

    if delete_image1 or (image1_file and image1_file.filename):
        delete_image_from_cloudinary(current.get('image1_public_id'))
        img1_url = img1_public_id = None

    if delete_image2 or (image2_file and image2_file.filename):
        delete_image_from_cloudinary(current.get('image2_public_id'))
        img2_url = img2_public_id = None

    if image1_file and image1_file.filename:
        result = upload_image_to_cloudinary(image1_file, item_id, 1)
        if result:
            img1_url = result['url']
            img1_public_id = result['public_id']

    if image2_file and image2_file.filename:
        result = upload_image_to_cloudinary(image2_file, item_id, 2)
        if result:
            img2_url = result['url']
            img2_public_id = result['public_id']

    conn.execute(
        '''UPDATE inventory SET image1_url=%s, image1_public_id=%s,
           image2_url=%s, image2_public_id=%s WHERE id=%s''',
        (img1_url, img1_public_id, img2_url, img2_public_id, item_id)
    )
    conn.commit()
    conn.close()
    log_activity('Edited inventory device', 'inventory', target_type='inventory', target_id=item_id,
                 details=f'{brand} {model} — status: {status}')
    flash('Device updated.', 'success')
    return redirect(url_for('admin_inventory'))


@app.route('/admin/inventory/<int:item_id>/sell', methods=['POST'])
@admin_required
def admin_inventory_sell(item_id):
    customer_id = request.form.get('customer_id', '').strip()
    if not customer_id:
        flash('Customer is required.', 'error')
        return redirect(url_for('admin_inventory'))
    try:
        customer_id = int(customer_id)
    except ValueError:
        flash('Invalid customer.', 'error')
        return redirect(url_for('admin_inventory'))

    conn = get_db()
    item = conn.execute('SELECT id, status FROM inventory WHERE id=%s', (item_id,)).fetchone()
    if not item:
        conn.close()
        flash('Device not found.', 'error')
        return redirect(url_for('admin_inventory'))
    if item['status'] not in ('In Stock', 'Reserved'):
        conn.close()
        flash('Only In Stock or Reserved devices can be marked as sold.', 'error')
        return redirect(url_for('admin_inventory'))
    customer = conn.execute('SELECT id FROM customers WHERE id=%s', (customer_id,)).fetchone()
    if not customer:
        conn.close()
        flash('Customer not found.', 'error')
        return redirect(url_for('admin_inventory'))
    conn.execute('UPDATE inventory SET status=%s,sold_to=%s,updated_at=NOW() WHERE id=%s',
                 ('Sold', customer_id, item_id))
    conn.commit(); conn.close()
    log_activity('Marked device as sold', 'inventory', target_type='inventory', target_id=item_id,
                 details=f'Sold to customer #{customer_id}')
    flash('Device marked as sold.', 'success')
    return redirect(url_for('admin_inventory'))


@app.route('/admin/inventory/<int:item_id>/reserve', methods=['POST'])
@admin_required
def admin_inventory_reserve(item_id):
    plan_id = request.form.get('plan_id', '').strip()
    if not plan_id:
        flash('Installment plan is required.', 'error')
        return redirect(url_for('admin_inventory'))
    try:
        plan_id = int(plan_id)
    except ValueError:
        flash('Invalid plan.', 'error')
        return redirect(url_for('admin_inventory'))

    conn = get_db()
    item = conn.execute('SELECT id FROM inventory WHERE id=%s', (item_id,)).fetchone()
    if not item:
        conn.close()
        flash('Device not found.', 'error')
        return redirect(url_for('admin_inventory'))
    plan = conn.execute('SELECT id FROM installment_plans WHERE id=%s', (plan_id,)).fetchone()
    if not plan:
        conn.close()
        flash('Plan not found.', 'error')
        return redirect(url_for('admin_inventory'))
    conn.execute('UPDATE inventory SET status=%s,plan_id=%s,updated_at=NOW() WHERE id=%s',
                 ('Reserved', plan_id, item_id))
    conn.commit(); conn.close()
    log_activity('Reserved device for plan', 'inventory', target_type='inventory', target_id=item_id,
                 details=f'Reserved for plan #{plan_id}')
    flash(f'Device reserved for plan #{plan_id}.', 'success')
    return redirect(url_for('admin_inventory'))


@app.route('/admin/inventory/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_inventory_delete(item_id):
    conn = get_db()
    item = conn.execute('SELECT * FROM inventory WHERE id=%s', (item_id,)).fetchone()
    if not item:
        conn.close()
        flash('Device not found.', 'error')
        return redirect(url_for('admin_inventory'))
    if item['status'] not in ('In Stock', 'Returned'):
        conn.close()
        flash('Cannot delete a sold or reserved device.', 'error')
        return redirect(url_for('admin_inventory'))
    delete_image_from_cloudinary(item.get('image1_public_id'))
    delete_image_from_cloudinary(item.get('image2_public_id'))
    conn.execute('DELETE FROM inventory WHERE id=%s', (item_id,))
    conn.commit(); conn.close()
    log_activity('Deleted device from inventory', 'inventory', target_type='inventory', target_id=item_id,
                 details=f'{item["brand"]} {item["model"]}')
    flash('Device removed from inventory.', 'success')
    return redirect(url_for('admin_inventory'))


@app.route('/admin/inventory/export')
@admin_required
def admin_inventory_export():
    import csv, io
    conn  = get_db()
    items = conn.execute(
        'SELECT id,brand,model,imei,condition,color,storage,cost_price,selling_price,status,notes,created_at,updated_at FROM inventory ORDER BY created_at DESC'
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['ID','Brand','Model','IMEI','Condition','Color','Storage','Cost Price','Selling Price','Profit','Status','Notes','Added','Updated'])
    for it in items:
        writer.writerow([
            it['id'], it['brand'], it['model'], it['imei'] or '',
            it['condition'], it['color'] or '', it['storage'] or '',
            it['cost_price'], it['selling_price'],
            round(it['selling_price'] - it['cost_price'], 2),
            it['status'], it['notes'] or '',
            str(it['created_at'])[:10] if it['created_at'] else '',
            str(it['updated_at'])[:10] if it['updated_at'] else '',
        ])

    today    = datetime.today().strftime('%Y-%m-%d')
    response = make_response(buf.getvalue())
    response.headers['Content-Type']        = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename=phonehub-inventory-{today}.csv'
    return response


# ─── REVENUE ROUTES ───────────────────────────────────────────────────────────

@app.route('/admin/revenue')
@admin_required
def admin_revenue():
    conn = get_db()
    try:
        this_month_collections = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments "
            "WHERE DATE_TRUNC('month',paid_on::date)=DATE_TRUNC('month',CURRENT_DATE)"
        ).fetchone()['total'])

        this_month_repairs = int(conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE status='Complete' "
            "AND DATE_TRUNC('month',date::date)=DATE_TRUNC('month',CURRENT_DATE)"
        ).fetchone()['count'])

        this_month_members = int(conn.execute(
            "SELECT COUNT(*) AS count FROM customers "
            "WHERE DATE_TRUNC('month',created_at)=DATE_TRUNC('month',NOW())"
        ).fetchone()['count'])

        outstanding_balance = float(conn.execute(
            "SELECT COALESCE(SUM(balance_remaining),0) AS total "
            "FROM installment_plans WHERE status='Active'"
        ).fetchone()['total'])

        overdue_count = int(conn.execute(
            "SELECT COUNT(*) AS count FROM installment_plans "
            "WHERE status='Active' AND next_due_date<CURRENT_DATE::text"
        ).fetchone()['count'])

        active_plans = int(conn.execute(
            "SELECT COUNT(*) AS count FROM installment_plans WHERE status='Active'"
        ).fetchone()['count'])

        device_profit = float(conn.execute(
            "SELECT COALESCE(SUM(selling_price-cost_price),0) AS profit FROM inventory "
            "WHERE status='Sold' AND DATE_TRUNC('month',updated_at)=DATE_TRUNC('month',NOW())"
        ).fetchone()['profit'])

        monthly_collections = [
            {'month': r['month'], 'total': float(r['total'])}
            for r in conn.execute(
                "SELECT TO_CHAR(DATE_TRUNC('month',paid_on::date),'Mon YYYY') AS month,"
                "COALESCE(SUM(amount),0) AS total FROM payments "
                "WHERE paid_on::date>=CURRENT_DATE-INTERVAL '6 months' "
                "GROUP BY DATE_TRUNC('month',paid_on::date) "
                "ORDER BY DATE_TRUNC('month',paid_on::date)"
            ).fetchall()
        ]

        monthly_repairs = [
            {'month': r['month'], 'count': int(r['count'])}
            for r in conn.execute(
                "SELECT TO_CHAR(DATE_TRUNC('month',date::date),'Mon YYYY') AS month,"
                "COUNT(*) AS count FROM bookings WHERE status='Complete' "
                "AND date::date>=CURRENT_DATE-INTERVAL '6 months' "
                "GROUP BY DATE_TRUNC('month',date::date) "
                "ORDER BY DATE_TRUNC('month',date::date)"
            ).fetchall()
        ]

        monthly_members = [
            {'month': r['month'], 'count': int(r['count'])}
            for r in conn.execute(
                "SELECT TO_CHAR(DATE_TRUNC('month',created_at),'Mon YYYY') AS month,"
                "COUNT(*) AS count FROM customers "
                "WHERE created_at>=NOW()-INTERVAL '6 months' "
                "GROUP BY DATE_TRUNC('month',created_at) "
                "ORDER BY DATE_TRUNC('month',created_at)"
            ).fetchall()
        ]

        recent_payments = [dict(r) for r in conn.execute(
            "SELECT p.id,p.amount,p.paid_on,p.payment_method,p.reference,p.created_at,"
            "ip.device_name,c.name AS customer_name "
            "FROM payments p "
            "JOIN installment_plans ip ON ip.id=p.plan_id "
            "JOIN customers c ON c.id=ip.customer_id "
            "ORDER BY p.created_at DESC LIMIT 10"
        ).fetchall()]

        top_services = [dict(r) for r in conn.execute(
            "SELECT service,COUNT(*) AS count FROM bookings "
            "GROUP BY service ORDER BY count DESC LIMIT 5"
        ).fetchall()]

        expiring_members = [dict(r) for r in conn.execute(
            "SELECT name,phone,membership_expiry FROM customers "
            "WHERE membership_expiry::date BETWEEN CURRENT_DATE "
            "AND CURRENT_DATE+INTERVAL '30 days' "
            "ORDER BY membership_expiry LIMIT 10"
        ).fetchall()]

    except Exception as exc:
        logger.error('admin_revenue query failed: %s', exc)
        conn.close()
        flash('Error loading revenue data.', 'error')
        return redirect(url_for('admin'))

    conn.close()
    return render_template('admin_revenue.html',
        this_month_collections=this_month_collections,
        this_month_repairs=this_month_repairs,
        this_month_members=this_month_members,
        outstanding_balance=outstanding_balance,
        overdue_count=overdue_count,
        active_plans=active_plans,
        device_profit=device_profit,
        monthly_collections=monthly_collections,
        monthly_repairs=monthly_repairs,
        monthly_members=monthly_members,
        recent_payments=recent_payments,
        top_services=top_services,
        expiring_members=expiring_members,
        current_month=datetime.today().strftime('%B %Y'),
    )


@app.route('/admin/revenue/export')
@admin_required
def admin_revenue_export():
    import csv, io
    conn = get_db()

    monthly_summary = conn.execute("""
        SELECT
            months.month_start,
            COALESCE(p.collections, 0)   AS collections,
            COALESCE(b.repairs, 0)       AS repairs,
            COALESCE(c.new_members, 0)   AS new_members,
            COALESCE(i.device_profit, 0) AS device_profit
        FROM (
            SELECT generate_series(
                DATE_TRUNC('month', CURRENT_DATE - INTERVAL '11 months'),
                DATE_TRUNC('month', CURRENT_DATE),
                INTERVAL '1 month'
            ) AS month_start
        ) months
        LEFT JOIN (
            SELECT DATE_TRUNC('month', paid_on::date) AS m, SUM(amount) AS collections
            FROM payments GROUP BY m
        ) p ON p.m = months.month_start
        LEFT JOIN (
            SELECT DATE_TRUNC('month', date::date) AS m, COUNT(*) AS repairs
            FROM bookings WHERE status='Complete' GROUP BY m
        ) b ON b.m = months.month_start
        LEFT JOIN (
            SELECT DATE_TRUNC('month', created_at) AS m, COUNT(*) AS new_members
            FROM customers GROUP BY m
        ) c ON c.m = months.month_start
        LEFT JOIN (
            SELECT DATE_TRUNC('month', updated_at) AS m,
                   SUM(selling_price - cost_price) AS device_profit
            FROM inventory WHERE status='Sold' GROUP BY m
        ) i ON i.m = months.month_start
        ORDER BY months.month_start
    """).fetchall()

    all_payments = conn.execute(
        "SELECT p.id,p.plan_id,c.name AS customer_name,ip.device_name,"
        "p.amount,p.payment_method,p.reference,p.paid_on "
        "FROM payments p "
        "JOIN installment_plans ip ON ip.id=p.plan_id "
        "JOIN customers c ON c.id=ip.customer_id "
        "ORDER BY p.paid_on DESC"
    ).fetchall()

    all_bookings = conn.execute(
        "SELECT id,name,device,service,date FROM bookings "
        "WHERE status='Complete' ORDER BY date DESC"
    ).fetchall()

    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(['=== SECTION 1: Monthly Summary (Last 12 Months) ==='])
    writer.writerow(['Month', 'Installment Collections', 'Completed Repairs',
                     'New Members', 'Device Sales Profit'])
    for row in monthly_summary:
        ms = row['month_start']
        label = ms.strftime('%B %Y') if hasattr(ms, 'strftime') else str(ms)[:7]
        writer.writerow([
            label,
            round(float(row['collections']), 2),
            int(row['repairs']),
            int(row['new_members']),
            round(float(row['device_profit']), 2),
        ])

    writer.writerow([])
    writer.writerow(['=== SECTION 2: All Payments ==='])
    writer.writerow(['Payment ID', 'Plan ID', 'Customer', 'Device',
                     'Amount', 'Method', 'Reference', 'Date'])
    for p in all_payments:
        writer.writerow([
            p['id'], p['plan_id'], p['customer_name'], p['device_name'],
            round(float(p['amount']), 2), p['payment_method'],
            p['reference'] or '', p['paid_on'],
        ])

    writer.writerow([])
    writer.writerow(['=== SECTION 3: All Completed Bookings ==='])
    writer.writerow(['Booking ID', 'Customer', 'Device', 'Service', 'Date'])
    for b in all_bookings:
        writer.writerow([b['id'], b['name'], b['device'], b['service'], b['date']])

    today = datetime.today().strftime('%Y-%m-%d')
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = (
        f'attachment; filename=phonehub-revenue-report-{today}.csv')
    return response


# ─── STAFF MANAGEMENT ROUTES ─────────────────────────────────────────────────

@app.route('/admin/staff')
@admin_required
def admin_staff():
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    conn = get_db()
    staff_list = conn.execute('SELECT * FROM staff ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('admin_staff.html',
                           staff_list=staff_list,
                           current_staff_id=session.get('admin_staff_id'))


@app.route('/admin/staff/add', methods=['POST'])
@admin_required
def admin_staff_add():
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))

    name     = request.form.get('name', '').strip()
    email    = request.form.get('email', '').strip().lower()
    phone    = request.form.get('phone', '').strip() or None
    role     = request.form.get('role', '').strip()
    password = request.form.get('password', '')
    confirm  = request.form.get('confirm_password', '')

    valid_roles = ['manager', 'technician', 'sales']
    if session.get('admin_is_master'):
        valid_roles.append('owner')

    errors = []
    if not name or len(name) > 100:
        errors.append('Name is required (max 100 characters).')
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        errors.append('Enter a valid email address.')
    if phone and not valid_gh_phone(phone):
        errors.append('Enter a valid Ghanaian phone number.')
    if role not in valid_roles:
        errors.append('Invalid role selected.')
    if role == 'owner' and not session.get('admin_is_master'):
        errors.append('Only the master admin can create owner accounts.')
    if len(password) < 8:
        errors.append('Password must be at least 8 characters.')
    if password != confirm:
        errors.append('Passwords do not match.')

    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('admin_staff'))

    conn = get_db()
    if conn.execute('SELECT id FROM staff WHERE email=%s', (email,)).fetchone():
        conn.close()
        flash('An account with that email already exists.', 'error')
        return redirect(url_for('admin_staff'))
    conn.execute(
        'INSERT INTO staff (name, email, phone, password_hash, role, created_by) VALUES (%s, %s, %s, %s, %s, %s)',
        (name, email, phone, generate_password_hash(password), role, session.get('admin_username')))
    conn.commit()
    conn.close()
    logger.info('Admin %s created staff account for %s (%s)', session.get('admin_username'), name, role)
    log_activity('Created staff account', 'staff', target_type='staff',
                 details=f'{name} ({email}) — role: {role}')
    flash(f'Staff account created for {name}.', 'success')
    return redirect(url_for('admin_staff'))


@app.route('/admin/staff/<int:staff_id>/edit', methods=['POST'])
@admin_required
def admin_staff_edit(staff_id):
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))

    name      = request.form.get('name', '').strip()
    phone     = request.form.get('phone', '').strip() or None
    role      = request.form.get('role', '').strip()
    is_active = 1 if request.form.get('is_active') else 0

    current_staff_id = session.get('admin_staff_id')
    if current_staff_id and current_staff_id == staff_id:
        flash('You cannot edit your own role.', 'error')
        return redirect(url_for('admin_staff'))

    valid_roles = ['manager', 'technician', 'sales']
    if session.get('admin_is_master'):
        valid_roles.append('owner')

    if role not in valid_roles:
        flash('Invalid role.', 'error')
        return redirect(url_for('admin_staff'))
    if role == 'owner' and not session.get('admin_is_master'):
        flash('Only the master admin can assign owner role.', 'error')
        return redirect(url_for('admin_staff'))
    if not name or len(name) > 100:
        flash('Name is required.', 'error')
        return redirect(url_for('admin_staff'))
    if phone and not valid_gh_phone(phone):
        flash('Enter a valid Ghanaian phone number.', 'error')
        return redirect(url_for('admin_staff'))

    conn = get_db()
    conn.execute(
        'UPDATE staff SET name=%s, phone=%s, role=%s, is_active=%s WHERE id=%s',
        (name, phone, role, is_active, staff_id))
    conn.commit()
    conn.close()
    logger.info('Admin %s edited staff #%d', session.get('admin_username'), staff_id)
    log_activity('Edited staff account', 'staff', target_type='staff', target_id=staff_id,
                 details=f'Role → {role}, Active → {is_active}')
    flash('Staff account updated.', 'success')
    return redirect(url_for('admin_staff'))


@app.route('/admin/staff/<int:staff_id>/reset-password', methods=['POST'])
@admin_required
def admin_staff_reset_password(staff_id):
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    if not (session.get('admin_is_master') or session.get('admin_role') == 'owner'):
        flash('Only owner accounts can reset staff passwords.', 'error')
        return redirect(url_for('admin_staff'))

    new_password = request.form.get('new_password', '')
    confirm      = request.form.get('confirm_password', '')

    if len(new_password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return redirect(url_for('admin_staff'))
    if new_password != confirm:
        flash('Passwords do not match.', 'error')
        return redirect(url_for('admin_staff'))

    conn = get_db()
    staff = conn.execute('SELECT name FROM staff WHERE id=%s', (staff_id,)).fetchone()
    if not staff:
        conn.close()
        flash('Staff member not found.', 'error')
        return redirect(url_for('admin_staff'))
    conn.execute('UPDATE staff SET password_hash=%s WHERE id=%s',
                 (generate_password_hash(new_password), staff_id))
    conn.commit()
    conn.close()
    logger.info('Admin %s reset password for staff #%d (%s)', session.get('admin_username'), staff_id, staff['name'])
    log_activity('Reset staff password', 'staff', target_type='staff', target_id=staff_id,
                 details=f'Password reset for {staff["name"]}')
    flash(f'Password reset for {staff["name"]}.', 'success')
    return redirect(url_for('admin_staff'))


@app.route('/admin/staff/<int:staff_id>/deactivate', methods=['POST'])
@admin_required
def admin_staff_deactivate(staff_id):
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))

    current_staff_id = session.get('admin_staff_id')
    if current_staff_id and current_staff_id == staff_id:
        flash('You cannot deactivate your own account.', 'error')
        return redirect(url_for('admin_staff'))

    conn = get_db()
    staff = conn.execute('SELECT name FROM staff WHERE id=%s', (staff_id,)).fetchone()
    if not staff:
        conn.close()
        flash('Staff member not found.', 'error')
        return redirect(url_for('admin_staff'))
    conn.execute('UPDATE staff SET is_active=0 WHERE id=%s', (staff_id,))
    conn.commit()
    conn.close()
    logger.info('Admin %s deactivated staff #%d (%s)', session.get('admin_username'), staff_id, staff['name'])
    log_activity('Deactivated staff account', 'staff', target_type='staff', target_id=staff_id,
                 details=f'Deactivated {staff["name"]}')
    flash(f"{staff['name']}'s account has been deactivated.", 'success')
    return redirect(url_for('admin_staff'))


@app.route('/admin/staff/<int:staff_id>/delete', methods=['POST'])
@admin_required
def admin_staff_delete(staff_id):
    if not has_permission('manage_staff'):
        flash('You do not have permission to do that.', 'error')
        return redirect(url_for('admin'))
    if not session.get('admin_is_master'):
        flash('Only the master admin can permanently delete staff accounts.', 'error')
        return redirect(url_for('admin_staff'))

    current_staff_id = session.get('admin_staff_id')
    if current_staff_id and current_staff_id == staff_id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('admin_staff'))

    conn = get_db()
    staff = conn.execute('SELECT name FROM staff WHERE id=%s', (staff_id,)).fetchone()
    if not staff:
        conn.close()
        flash('Staff member not found.', 'error')
        return redirect(url_for('admin_staff'))
    conn.execute('DELETE FROM staff WHERE id=%s', (staff_id,))
    conn.commit()
    conn.close()
    logger.info('Master admin permanently deleted staff #%d (%s)', staff_id, staff['name'])
    log_activity('Permanently deleted staff', 'staff', target_type='staff', target_id=staff_id,
                 details=f'Deleted {staff["name"]}')
    flash(f"Staff account for {staff['name']} permanently deleted.", 'success')
    return redirect(url_for('admin_staff'))


# ─── ONLINE SHOP — PUBLIC ROUTES ─────────────────────────────────────────────

@app.route('/shop')
@limiter.limit('120 per minute')
def shop():
    global _last_reservation_expiry
    conn = get_db()
    now = datetime.now()
    if _last_reservation_expiry is None or (now - _last_reservation_expiry).seconds >= 60:
        conn.execute(
            "UPDATE reservations SET status='Expired' WHERE status='Pending' AND expires_at < NOW()"
        )
        conn.commit()
        _last_reservation_expiry = now
    items = conn.execute(
        "SELECT * FROM inventory WHERE status='In Stock' ORDER BY brand, model"
    ).fetchall()
    brands = sorted({i['brand'] for i in items})
    conn.close()
    return render_template('shop.html', items=items, brands=brands,
                           RESERVATION_DEPOSIT_PCT=RESERVATION_DEPOSIT_PCT)


@app.route('/shop/<int:item_id>')
def shop_detail(item_id):
    conn = get_db()
    conn.execute(
        "UPDATE reservations SET status='Expired' WHERE status='Pending' AND expires_at < NOW()"
    )
    conn.commit()
    item = conn.execute('SELECT * FROM inventory WHERE id=%s', (item_id,)).fetchone()
    if not item:
        conn.close()
        return render_template('404.html'), 404
    similar = conn.execute(
        "SELECT * FROM inventory WHERE brand=%s AND id!=%s AND status='In Stock' LIMIT 4",
        (item['brand'], item_id)
    ).fetchall()
    conn.close()
    deposit = round(item['selling_price'] * RESERVATION_DEPOSIT_PCT / 100, 2)
    plans = {}
    for months in PLAN_CONFIG:
        if item['selling_price'] >= PLAN_CONFIG[months]['min_price']:
            plans[months] = calculate_plan(item['selling_price'], months)
    return render_template('shop_detail.html', item=item, similar=similar,
                           deposit=deposit, plans=plans,
                           RESERVATION_DEPOSIT_PCT=RESERVATION_DEPOSIT_PCT,
                           BANK_DETAILS=BANK_DETAILS)


@app.route('/shop/<int:item_id>/reserve', methods=['POST'])
def shop_reserve(item_id):
    name       = request.form.get('name', '').strip()
    phone      = request.form.get('phone', '').strip()
    email      = request.form.get('email', '').strip()
    method     = request.form.get('payment_method', '').strip()
    momo_num   = request.form.get('momo_number', '').strip()
    momo_net   = request.form.get('momo_network', '').strip()
    bank_ref   = request.form.get('bank_reference', '').strip()

    if not all([name, phone, email, method]):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('shop_detail', item_id=item_id))

    conn = get_db()
    try:
        # Lock the row to prevent concurrent reservations
        item = conn.execute(
            "SELECT * FROM inventory WHERE id=%s AND status='In Stock' FOR UPDATE",
            (item_id,)
        ).fetchone()
        if not item:
            conn.rollback()
            conn.close()
            flash('Sorry, this device is no longer available.', 'error')
            return redirect(url_for('shop'))

        deposit = round(item['selling_price'] * RESERVATION_DEPOSIT_PCT / 100, 2)
        expires = datetime.now() + timedelta(hours=48)
        customer_id = session.get('customer_id')

        conn.execute(
            """INSERT INTO reservations
               (item_id, customer_id, customer_name, customer_phone, customer_email,
                deposit_amount, payment_method, momo_number, momo_network,
                bank_reference, status, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending',%s)""",
            (item_id, customer_id, name, phone, email,
             deposit, method, momo_num or None, momo_net or None,
             bank_ref or None, expires)
        )
        conn.execute(
            "UPDATE inventory SET status='Reserved', updated_at=NOW() WHERE id=%s",
            (item_id,)
        )
        conn.commit()
        logger.info('Reservation created for item #%d by %s', item_id, email)
        flash(
            f'Your reservation is confirmed! Please pay a deposit of {fmt_ghs(deposit)} '
            f'within 48 hours to hold this device.', 'success'
        )
    except Exception as exc:
        conn.rollback()
        logger.error('Reservation failed for item #%d: %s', item_id, exc)
        flash('An error occurred. Please try again.', 'error')
    finally:
        conn.close()

    if session.get('customer_id'):
        return redirect(url_for('shop_reservations'))
    return redirect(url_for('shop'))


@app.route('/shop/reservations')
@customer_required
def shop_reservations():
    conn = get_db()
    conn.execute(
        "UPDATE reservations SET status='Expired' WHERE status='Pending' AND expires_at < NOW()"
    )
    conn.commit()
    reservations = conn.execute(
        """SELECT r.*, i.brand, i.model, i.color, i.storage, i.selling_price
           FROM reservations r
           JOIN inventory i ON i.id = r.item_id
           WHERE r.customer_id=%s
           ORDER BY r.created_at DESC""",
        (session['customer_id'],)
    ).fetchall()
    conn.close()
    now = datetime.now()
    return render_template('shop_reservations.html', reservations=reservations, now=now)


@app.route('/shop/reservations/<int:res_id>/cancel', methods=['POST'])
@customer_required
def shop_reservation_cancel(res_id):
    conn = get_db()
    res = conn.execute(
        "SELECT * FROM reservations WHERE id=%s AND customer_id=%s",
        (res_id, session['customer_id'])
    ).fetchone()
    if not res or res['status'] not in ('Pending',):
        conn.close()
        flash('Reservation not found or cannot be cancelled.', 'error')
        return redirect(url_for('shop_reservations'))
    conn.execute("UPDATE reservations SET status='Cancelled' WHERE id=%s", (res_id,))
    conn.execute(
        "UPDATE inventory SET status='In Stock', updated_at=NOW() WHERE id=%s",
        (res['item_id'],)
    )
    conn.commit()
    conn.close()
    flash('Reservation cancelled. The device is now available again.', 'success')
    return redirect(url_for('shop_reservations'))


@app.route('/installment/<int:plan_id>/notify-payment', methods=['POST'])
@customer_required
def notify_payment(plan_id):
    conn = get_db()
    plan = conn.execute(
        'SELECT * FROM installment_plans WHERE id=%s AND customer_id=%s',
        (plan_id, session['customer_id'])
    ).fetchone()
    if not plan:
        conn.close()
        flash('Plan not found.', 'error')
        return redirect(url_for('dashboard'))
    if plan['status'] != 'Active':
        conn.close()
        flash('This plan is not active.', 'error')
        return redirect(url_for('installment_detail', plan_id=plan_id))
    try:
        amount = float(request.form.get('amount', 0))
    except (ValueError, TypeError):
        conn.close()
        flash('Invalid payment amount.', 'error')
        return redirect(url_for('installment_detail', plan_id=plan_id))
    method       = request.form.get('payment_method', '').strip()
    reference    = request.form.get('reference', '').strip()
    momo_number  = request.form.get('momo_number', '').strip()
    momo_network = request.form.get('momo_network', '').strip()
    notes        = request.form.get('notes', '').strip()
    errors = []
    if amount <= 0:
        errors.append('Payment amount must be greater than zero.')
    if amount > plan['balance_remaining'] + 0.01:
        errors.append(f'Amount exceeds remaining balance of {fmt_ghs(plan["balance_remaining"])}.')
    if method not in ('MTN MoMo', 'Vodafone Cash', 'AirtelTigo Money', 'Bank Transfer', 'Bank Deposit'):
        errors.append('Invalid payment method.')
    if any(w in method for w in ('MoMo', 'Cash', 'Money')) and not momo_number:
        errors.append('MoMo number is required.')
    if not reference:
        errors.append('Transaction reference is required for verification.')
    if errors:
        conn.close()
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('installment_detail', plan_id=plan_id))
    duplicate = conn.execute(
        """SELECT id FROM pending_payments
           WHERE plan_id=%s AND amount=%s AND status='Pending'
           AND created_at >= NOW() - INTERVAL '1 hour'""",
        (plan_id, amount)
    ).fetchone()
    if duplicate:
        conn.close()
        flash('You already submitted a payment notification for this amount. Please wait for admin to verify.', 'error')
        return redirect(url_for('installment_detail', plan_id=plan_id))
    screenshot_url = screenshot_public_id = None
    screenshot_file = request.files.get('screenshot')
    if screenshot_file and screenshot_file.filename:
        try:
            result = upload_image_to_cloudinary(screenshot_file, f'payment-{plan_id}', 1)
            if result:
                screenshot_url       = result['url']
                screenshot_public_id = result['public_id']
        except Exception as exc:
            logger.error('Screenshot upload failed: %s', exc)
    conn.execute(
        """INSERT INTO pending_payments
           (plan_id, customer_id, amount, payment_method, reference,
            momo_number, momo_network, screenshot_url, screenshot_public_id, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (plan_id, session['customer_id'], amount, method,
         reference or None, momo_number or None, momo_network or None,
         screenshot_url, screenshot_public_id, notes or None)
    )
    conn.commit()
    conn.close()
    try:
        customer_name = session.get('customer_name', 'Customer')
        send_sms('0541057500',
                 f'New payment notification: {customer_name} claims {fmt_ghs(amount)} paid for '
                 f'plan #{plan_id} via {method}. Ref: {reference}. Verify in admin panel. -DonnyPhonehub Gh')
    except Exception:
        pass
    flash('Payment notification submitted! We will verify and confirm within 24 hours.', 'success')
    return redirect(url_for('installment_detail', plan_id=plan_id))


@app.route('/shop/reservations/<int:res_id>/notify-deposit', methods=['POST'])
@customer_required
def notify_deposit(res_id):
    conn = get_db()
    res = conn.execute(
        """SELECT r.*, i.selling_price, i.brand, i.model
           FROM reservations r JOIN inventory i ON i.id=r.item_id
           WHERE r.id=%s AND r.customer_id=%s AND r.status='Pending'""",
        (res_id, session['customer_id'])
    ).fetchone()
    if not res:
        conn.close()
        flash('Reservation not found.', 'error')
        return redirect(url_for('shop_reservations'))
    method       = request.form.get('payment_method', '').strip()
    reference    = request.form.get('reference', '').strip()
    momo_number  = request.form.get('momo_number', '').strip()
    momo_network = request.form.get('momo_network', '').strip()
    notes        = request.form.get('notes', '').strip()
    if not method or not reference:
        conn.close()
        flash('Payment method and reference are required.', 'error')
        return redirect(url_for('shop_reservations'))
    dup = conn.execute(
        "SELECT id FROM pending_deposits WHERE reservation_id=%s AND status='Pending'",
        (res_id,)
    ).fetchone()
    if dup:
        conn.close()
        flash('You already submitted a deposit notification. Please wait for verification.', 'error')
        return redirect(url_for('shop_reservations'))
    screenshot_url = screenshot_public_id = None
    screenshot_file = request.files.get('screenshot')
    if screenshot_file and screenshot_file.filename:
        try:
            result = upload_image_to_cloudinary(screenshot_file, f'deposit-{res_id}', 1)
            if result:
                screenshot_url       = result['url']
                screenshot_public_id = result['public_id']
        except Exception:
            pass
    conn.execute(
        """INSERT INTO pending_deposits
           (reservation_id, customer_id, customer_name, customer_phone,
            amount, payment_method, reference, momo_number, momo_network,
            screenshot_url, screenshot_public_id, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (res_id, session['customer_id'], res['customer_name'], res['customer_phone'],
         res['deposit_amount'], method, reference or None,
         momo_number or None, momo_network or None,
         screenshot_url, screenshot_public_id, notes or None)
    )
    conn.commit()
    conn.close()
    flash('Deposit notification submitted! We will verify within 24 hours.', 'success')
    return redirect(url_for('shop_reservations'))


@app.route('/shop/enquire', methods=['POST'])
def shop_enquire():
    name        = request.form.get('name', '').strip()
    phone       = request.form.get('phone', '').strip()
    email       = request.form.get('email', '').strip()
    device_type = request.form.get('device_type', '').strip()
    budget      = request.form.get('budget', '').strip()
    message     = request.form.get('message', '').strip()

    if not all([name, phone, email, device_type, message]):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('shop') + '#enquire')

    customer_id = session.get('customer_id')
    conn = get_db()
    conn.execute(
        """INSERT INTO device_enquiries
           (customer_id, customer_name, customer_phone, customer_email,
            device_type, budget, message)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (customer_id, name, phone, email, device_type, budget or None, message)
    )
    conn.commit()
    conn.close()
    logger.info('Device enquiry submitted by %s (%s)', name, email)
    flash("Thanks for your enquiry! We'll reach out within 24 hours.", 'success')
    return redirect(url_for('shop'))


# ─── ONLINE SHOP — ADMIN ROUTES ───────────────────────────────────────────────

@app.route('/admin/shop')
@admin_required
def admin_shop():
    if not has_permission('view_inventory'):
        flash('You do not have permission to view the shop.', 'error')
        return redirect(url_for('admin'))

    conn = get_db()
    # Auto-expire pending reservations
    conn.execute(
        "UPDATE reservations SET status='Expired' WHERE status='Pending' AND expires_at < NOW()"
    )
    conn.commit()

    reservations = conn.execute(
        """SELECT r.*, i.brand, i.model, i.color, i.storage, i.selling_price
           FROM reservations r
           JOIN inventory i ON i.id = r.item_id
           ORDER BY r.created_at DESC
           LIMIT 200"""
    ).fetchall()

    enquiries = conn.execute(
        "SELECT * FROM device_enquiries ORDER BY created_at DESC LIMIT 200"
    ).fetchall()

    stats = conn.execute(
        """SELECT
           COUNT(*) FILTER (WHERE status='Pending')   AS pending,
           COUNT(*) FILTER (WHERE status='Confirmed') AS confirmed,
           COUNT(*) FILTER (WHERE status='Completed') AS completed,
           COUNT(*) FILTER (WHERE status='Cancelled') AS cancelled,
           COUNT(*) FILTER (WHERE status='Expired')   AS expired,
           COALESCE(SUM(deposit_amount) FILTER (WHERE status='Completed'), 0) AS total_deposits
           FROM reservations"""
    ).fetchone()

    enq_new = conn.execute(
        "SELECT COUNT(*) AS cnt FROM device_enquiries WHERE status='New'"
    ).fetchone()['cnt']

    conn.close()
    now = datetime.now()
    return render_template('admin_shop.html',
                           reservations=reservations, enquiries=enquiries,
                           stats=stats, enq_new=enq_new, now=now,
                           RESERVATION_DEPOSIT_PCT=RESERVATION_DEPOSIT_PCT)


@app.route('/admin/shop/reservations/<int:res_id>/confirm', methods=['POST'])
@admin_required
def admin_shop_confirm(res_id):
    if not has_permission('edit_inventory'):
        flash('You do not have permission to confirm reservations.', 'error')
        return redirect(url_for('admin_shop'))
    conn = get_db()
    res = conn.execute('SELECT * FROM reservations WHERE id=%s', (res_id,)).fetchone()
    if not res:
        conn.close()
        flash('Reservation not found.', 'error')
        return redirect(url_for('admin_shop'))
    admin_name = session.get('admin_name', session.get('admin_username', 'admin'))
    conn.execute(
        "UPDATE reservations SET status='Confirmed', confirmed_by=%s WHERE id=%s",
        (admin_name, res_id)
    )
    conn.commit()
    conn.close()
    log_activity('Confirmed reservation', 'shop', target_type='reservation', target_id=res_id,
                 details=f'Deposit confirmed for reservation #{res_id}')
    flash('Reservation confirmed.', 'success')
    return redirect(url_for('admin_shop'))


@app.route('/admin/shop/reservations/<int:res_id>/cancel', methods=['POST'])
@admin_required
def admin_shop_cancel(res_id):
    if not has_permission('edit_inventory'):
        flash('You do not have permission to cancel reservations.', 'error')
        return redirect(url_for('admin_shop'))
    conn = get_db()
    res = conn.execute('SELECT * FROM reservations WHERE id=%s', (res_id,)).fetchone()
    if not res:
        conn.close()
        flash('Reservation not found.', 'error')
        return redirect(url_for('admin_shop'))
    conn.execute("UPDATE reservations SET status='Cancelled' WHERE id=%s", (res_id,))
    conn.execute(
        "UPDATE inventory SET status='In Stock', updated_at=NOW() WHERE id=%s",
        (res['item_id'],)
    )
    conn.commit()
    conn.close()
    log_activity('Cancelled reservation', 'shop', target_type='reservation', target_id=res_id,
                 details=f'Reservation #{res_id} cancelled')
    flash('Reservation cancelled. Device returned to stock.', 'success')
    return redirect(url_for('admin_shop'))


@app.route('/admin/shop/reservations/<int:res_id>/complete', methods=['POST'])
@admin_required
def admin_shop_complete(res_id):
    if not has_permission('edit_inventory'):
        flash('You do not have permission to complete reservations.', 'error')
        return redirect(url_for('admin_shop'))
    conn = get_db()
    res = conn.execute('SELECT * FROM reservations WHERE id=%s', (res_id,)).fetchone()
    if not res:
        conn.close()
        flash('Reservation not found.', 'error')
        return redirect(url_for('admin_shop'))
    conn.execute("UPDATE reservations SET status='Completed' WHERE id=%s", (res_id,))
    conn.execute(
        "UPDATE inventory SET status='Sold', updated_at=NOW() WHERE id=%s",
        (res['item_id'],)
    )
    conn.commit()
    conn.close()
    log_activity('Completed sale', 'shop', target_type='reservation', target_id=res_id,
                 details=f'Reservation #{res_id} → sale completed')
    flash('Sale completed. Device marked as sold.', 'success')
    return redirect(url_for('admin_shop'))


@app.route('/admin/shop/enquiries/<int:enq_id>/reply', methods=['POST'])
@admin_required
def admin_shop_enquiry_reply(enq_id):
    import html as _html
    if not has_permission('edit_inventory'):
        flash('You do not have permission to reply to enquiries.', 'error')
        return redirect(url_for('admin_shop'))

    response_message = request.form.get('response_message', '').strip()
    if not response_message:
        flash('Reply message cannot be empty.', 'error')
        return redirect(url_for('admin_shop') + '#tab-enquiries')

    conn = get_db()
    try:
        enq = conn.execute('SELECT * FROM device_enquiries WHERE id=%s', (enq_id,)).fetchone()
        if not enq:
            flash('Enquiry not found.', 'error')
            return redirect(url_for('admin_shop'))

        admin_name = session.get('admin_username', 'Admin')
        conn.execute(
            """UPDATE device_enquiries
               SET status='Replied', response_message=%s, replied_at=NOW(), replied_by=%s
               WHERE id=%s""",
            (response_message, admin_name, enq_id)
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error('Enquiry reply DB error for #%d: %s', enq_id, exc)
        flash(f'Could not save reply — database error: {exc}', 'error')
        return redirect(url_for('admin_shop') + '#tab-enquiries')
    finally:
        conn.close()

    # Email the customer
    customer_email = enq.get('customer_email', '').strip()
    if not customer_email:
        flash('Reply saved but no customer email address on record.', 'warning')
        return redirect(url_for('admin_shop') + '#tab-enquiries')

    safe_name    = _html.escape(enq['customer_name'] or '')
    safe_device  = _html.escape(enq['device_type'] or '')
    safe_message = _html.escape(enq['message'] or '').replace('\n', '<br>')
    safe_reply   = _html.escape(response_message).replace('\n', '<br>')

    html_body = f"""
    <div style="font-family:'DM Sans',Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E8E4DC;">
      <div style="background:#006B3F;padding:28px 32px;">
        <div style="font-family:'Syne',Arial,sans-serif;font-size:20px;font-weight:800;color:#fff;">
          Donny<span style="color:#FCD116;">Phonehub</span> Gh
        </div>
      </div>
      <div style="padding:32px;">
        <h2 style="font-family:'Syne',Arial,sans-serif;font-size:20px;font-weight:700;color:#111008;margin:0 0 8px;">
          We've replied to your enquiry
        </h2>
        <p style="color:#4A4740;font-size:15px;line-height:1.6;margin:0 0 24px;">
          Hi {safe_name}, here's our response to your device enquiry.
        </p>

        <div style="background:#F7F5F0;border-radius:10px;padding:18px 20px;margin-bottom:24px;">
          <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#8C8880;margin-bottom:8px;">Your Enquiry</div>
          <div style="font-size:14px;color:#4A4740;font-weight:600;margin-bottom:4px;">Device: {safe_device}</div>
          <div style="font-size:14px;color:#4A4740;line-height:1.6;">{safe_message}</div>
        </div>

        <div style="background:#D1FAE5;border-left:4px solid #006B3F;border-radius:0 10px 10px 0;padding:18px 20px;margin-bottom:28px;">
          <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#065F46;margin-bottom:8px;">Our Response</div>
          <div style="font-size:15px;color:#065F46;line-height:1.65;">{safe_reply}</div>
        </div>

        <p style="font-size:14px;color:#4A4740;line-height:1.6;">
          Have more questions? Call us on <a href="tel:+233541057500" style="color:#006B3F;font-weight:600;">0541 057 500</a>
          or reply to this email.
        </p>
      </div>
      <div style="background:#F7F5F0;padding:20px 32px;text-align:center;font-size:13px;color:#8C8880;">
        &copy; 2026 DonnyPhonehub Gh Ltd. &mdash; Tamale, Northern Region, Ghana
      </div>
    </div>
    """
    sent = send_email(customer_email, f"Re: Your Device Enquiry — DonnyPhonehub Gh", html_body)
    if sent:
        flash(f"Reply sent to {enq['customer_name']} ({customer_email}).", 'success')
    else:
        flash('Reply saved but email could not be sent — check MAIL_USER / MAIL_PASS settings.', 'warning')

    return redirect(url_for('admin_shop') + '#tab-enquiries')


@app.route('/admin/shop/enquiries/<int:enq_id>/delete', methods=['POST'])
@admin_required
def admin_shop_enquiry_delete(enq_id):
    if not has_permission('delete_inventory'):
        flash('You do not have permission to delete enquiries.', 'error')
        return redirect(url_for('admin_shop'))
    conn = get_db()
    conn.execute('DELETE FROM device_enquiries WHERE id=%s', (enq_id,))
    conn.commit()
    conn.close()
    log_activity('Deleted enquiry', 'shop', target_type='enquiry', target_id=enq_id,
                 details=f'Enquiry #{enq_id} deleted')
    flash('Enquiry deleted.', 'success')
    return redirect(url_for('admin_shop'))


# ─── ACTIVITY LOG ────────────────────────────────────────────────────────────

@app.route('/admin/activity')
@admin_required
def admin_activity():
    if not has_permission('manage_staff'):
        flash('You do not have permission to view the activity log.', 'error')
        return redirect(url_for('admin'))

    category  = request.args.get('category', '').strip()
    user      = request.args.get('user', '').strip()
    search    = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conn = get_db()
    q      = "SELECT * FROM activity_log WHERE 1=1"
    params = []
    if category:
        q += " AND category=%s"; params.append(category)
    if user:
        q += " AND user_name=%s"; params.append(user)
    if search:
        q += " AND (details ILIKE %s OR action ILIKE %s)"
        params += [f'%{search}%', f'%{search}%']
    if date_from:
        q += " AND DATE(created_at) >= %s"; params.append(date_from)
    if date_to:
        q += " AND DATE(created_at) <= %s"; params.append(date_to)

    total     = conn.execute(q.replace("SELECT *", "SELECT COUNT(*) AS cnt"), params).fetchone()['cnt']
    logs      = conn.execute(q + " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                             params + [ADMIN_PAGE_SIZE, (page - 1) * ADMIN_PAGE_SIZE]).fetchall()
    users      = conn.execute("SELECT DISTINCT user_name FROM activity_log ORDER BY user_name").fetchall()
    categories = conn.execute("SELECT DISTINCT category FROM activity_log ORDER BY category").fetchall()
    today_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM activity_log WHERE DATE(created_at) = CURRENT_DATE"
    ).fetchone()['cnt']
    conn.close()

    total_pages = max(1, -(-total // ADMIN_PAGE_SIZE))
    return render_template('admin_activity.html',
        logs=logs, total=total,
        page=page, total_pages=total_pages,
        users=[u['user_name'] for u in users],
        categories=[c['category'] for c in categories],
        selected_category=category,
        selected_user=user,
        search=search,
        date_from=date_from,
        date_to=date_to,
        today_count=today_count,
    )


@app.route('/admin/activity/export')
@admin_required
def admin_activity_export():
    import csv, io
    if not has_permission('manage_staff'):
        flash('No permission.', 'error')
        return redirect(url_for('admin'))

    category  = request.args.get('category', '').strip()
    user      = request.args.get('user', '').strip()
    search    = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to', '').strip()

    conn   = get_db()
    q      = "SELECT * FROM activity_log WHERE 1=1"
    params = []
    if category:
        q += " AND category=%s"; params.append(category)
    if user:
        q += " AND user_name=%s"; params.append(user)
    if search:
        q += " AND (details ILIKE %s OR action ILIKE %s)"
        params += [f'%{search}%', f'%{search}%']
    if date_from:
        q += " AND DATE(created_at) >= %s"; params.append(date_from)
    if date_to:
        q += " AND DATE(created_at) <= %s"; params.append(date_to)

    logs = conn.execute(q + " ORDER BY created_at DESC", params).fetchall()
    conn.close()

    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['ID', 'Timestamp', 'User', 'Role', 'Action', 'Category',
                     'Target Type', 'Target ID', 'Details', 'IP Address'])
    for log in logs:
        writer.writerow([
            log['id'],
            log['created_at'].strftime('%Y-%m-%d %H:%M:%S') if log['created_at'] else '',
            log['user_name'], log['user_role'], log['action'], log['category'],
            log['target_type'] or '', log['target_id'] or '',
            log['details'] or '', log['ip_address'] or '',
        ])

    today_str = datetime.today().strftime('%Y-%m-%d')
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type']        = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=phonehub-activity-log-{today_str}.csv'
    return resp


# ─── ADMIN PAYMENT VERIFICATION QUEUE ────────────────────────────────────────

@app.route('/admin/payments')
@admin_required
def admin_payments():
    conn = get_db()
    pending_pp = conn.execute(
        """SELECT pp.*, c.name AS customer_name, c.phone AS customer_phone,
                  ip.device_name, ip.monthly_amount, ip.balance_remaining
           FROM pending_payments pp
           JOIN customers c ON c.id = pp.customer_id
           JOIN installment_plans ip ON ip.id = pp.plan_id
           WHERE pp.status = 'Pending'
           ORDER BY pp.created_at DESC"""
    ).fetchall()
    pending_pd = conn.execute(
        """SELECT pd.*, i.brand, i.model, i.selling_price
           FROM pending_deposits pd
           JOIN reservations r ON r.id = pd.reservation_id
           JOIN inventory i ON i.id = r.inventory_id
           WHERE pd.status = 'Pending'
           ORDER BY pd.created_at DESC"""
    ).fetchall()
    stats = conn.execute(
        """SELECT
           (SELECT COUNT(*) FROM pending_payments WHERE status='Pending') AS pp_count,
           (SELECT COUNT(*) FROM pending_deposits WHERE status='Pending') AS pd_count,
           (SELECT COALESCE(SUM(amount),0) FROM pending_payments WHERE status='Pending') AS pp_total,
           (SELECT COALESCE(SUM(amount),0) FROM pending_deposits WHERE status='Pending') AS pd_total,
           (SELECT COUNT(*) FROM pending_payments
            WHERE status != 'Pending' AND DATE(reviewed_at) = CURRENT_DATE) +
           (SELECT COUNT(*) FROM pending_deposits
            WHERE status != 'Pending' AND DATE(reviewed_at) = CURRENT_DATE) AS reviewed_today"""
    ).fetchone()
    conn.close()
    return render_template('admin_payments.html',
                           pending_pp=pending_pp, pending_pd=pending_pd, stats=stats)


@app.route('/admin/payments/<int:pp_id>/approve', methods=['POST'])
@admin_required
def approve_pending_payment(pp_id):
    if not has_permission('record_payment'):
        flash('Permission denied.', 'error')
        return redirect(url_for('admin_payments'))
    conn = get_db()
    pp = conn.execute(
        'SELECT * FROM pending_payments WHERE id=%s AND status=%s', (pp_id, 'Pending')
    ).fetchone()
    if not pp:
        flash('Payment record not found or already reviewed.', 'error')
        conn.close()
        return redirect(url_for('admin_payments'))
    plan = conn.execute(
        'SELECT ip.*, c.name AS customer_name, c.phone AS customer_phone '
        'FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id WHERE ip.id=%s',
        (pp['plan_id'],)
    ).fetchone()
    if not plan:
        flash('Installment plan not found.', 'error')
        conn.close()
        return redirect(url_for('admin_payments'))
    amount = _d(pp['amount'])
    new_balance = float(max(_d(plan['balance_remaining']) - amount, Decimal('0')))
    new_payments_made = plan['payments_made'] + 1
    new_next_due = add_one_month(plan['next_due_date'])
    new_status = 'Completed' if new_balance <= 0.01 else plan['status']
    today_str = datetime.today().strftime('%Y-%m-%d')
    try:
        cur = conn.execute(
            'INSERT INTO payments (plan_id,amount,paid_on,payment_method,reference,recorded_by,notes) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (pp['plan_id'], float(amount), today_str,
             pp['payment_method'], pp['reference'] or None,
             session.get('admin_username','admin'),
             f'Via payment notification #{pp_id}')
        )
        payment_id = cur.fetchone()['id']
        conn.execute(
            'UPDATE installment_plans SET balance_remaining=%s,payments_made=%s,next_due_date=%s,status=%s WHERE id=%s',
            (new_balance, new_payments_made, new_next_due, new_status, pp['plan_id'])
        )
        conn.execute(
            "UPDATE pending_payments SET status='Approved',reviewed_by=%s,reviewed_at=NOW() WHERE id=%s",
            (session.get('admin_username','Admin'), pp_id)
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        logger.error('approve_pending_payment #%d failed: %s', pp_id, exc)
        flash('Could not approve payment — please try again.', 'error')
        return redirect(url_for('admin_payments'))
    log_activity('Approved payment notification', 'installment',
                 target_type='installment_plan', target_id=pp['plan_id'],
                 details=f'Payment #{pp_id}: {fmt_ghs(float(amount))} for plan #{pp["plan_id"]}')
    send_sms(plan['customer_phone'],
             f"Hi {plan['customer_name'].split()[0]}, your payment of {fmt_ghs(float(amount))} "
             f"has been confirmed! Balance: {fmt_ghs(new_balance)}. "
             f"{'Plan fully paid!' if new_status == 'Completed' else 'Next due: ' + new_next_due} "
             f"— DonnyPhonehub Gh")
    conn.close()
    flash(f'Payment approved and applied to plan #{pp["plan_id"]}. Balance: {fmt_ghs(new_balance)}.', 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/payments/<int:pp_id>/reject', methods=['POST'])
@admin_required
def reject_pending_payment(pp_id):
    if not has_permission('record_payment'):
        flash('Permission denied.', 'error')
        return redirect(url_for('admin_payments'))
    conn = get_db()
    pp = conn.execute(
        'SELECT pp.*, c.name AS customer_name, c.phone AS customer_phone '
        'FROM pending_payments pp JOIN customers c ON c.id=pp.customer_id '
        'WHERE pp.id=%s AND pp.status=%s', (pp_id, 'Pending')
    ).fetchone()
    if not pp:
        flash('Payment record not found or already reviewed.', 'error')
        conn.close()
        return redirect(url_for('admin_payments'))
    review_notes = request.form.get('review_notes', '').strip()
    conn.execute(
        "UPDATE pending_payments SET status='Rejected',reviewed_by=%s,review_notes=%s,reviewed_at=NOW() WHERE id=%s",
        (session.get('admin_username','Admin'), review_notes or None, pp_id)
    )
    conn.commit()
    reason_txt = f' Reason: {review_notes}' if review_notes else ''
    send_sms(pp['customer_phone'],
             f"Hi {pp['customer_name'].split()[0]}, your payment of {fmt_ghs(pp['amount'])} "
             f"could not be verified.{reason_txt} Please call 0541057500. — DonnyPhonehub Gh")
    log_activity('Rejected payment notification', 'installment',
                 target_type='installment_plan', target_id=pp['plan_id'],
                 details=f'Payment #{pp_id} rejected. Notes: {review_notes}')
    conn.close()
    flash('Payment rejected. Customer has been notified via SMS.', 'warning')
    return redirect(url_for('admin_payments'))


@app.route('/admin/deposits/<int:pd_id>/approve', methods=['POST'])
@admin_required
def approve_pending_deposit(pd_id):
    if not has_permission('record_payment'):
        flash('Permission denied.', 'error')
        return redirect(url_for('admin_payments'))
    conn = get_db()
    pd_row = conn.execute(
        'SELECT * FROM pending_deposits WHERE id=%s AND status=%s', (pd_id, 'Pending')
    ).fetchone()
    if not pd_row:
        flash('Deposit record not found or already reviewed.', 'error')
        conn.close()
        return redirect(url_for('admin_payments'))
    conn.execute(
        "UPDATE reservations SET status='Confirmed',deposit_amount=%s WHERE id=%s",
        (float(_d(pd_row['amount'])), pd_row['reservation_id'])
    )
    conn.execute(
        "UPDATE pending_deposits SET status='Approved',reviewed_by=%s,reviewed_at=NOW() WHERE id=%s",
        (session.get('admin_username','Admin'), pd_id)
    )
    conn.commit()
    if pd_row['customer_id']:
        cust = conn.execute(
            'SELECT name, phone FROM customers WHERE id=%s', (pd_row['customer_id'],)
        ).fetchone()
        if cust:
            send_sms(cust['phone'],
                     f"Hi {cust['name'].split()[0]}, your deposit of {fmt_ghs(pd_row['amount'])} "
                     f"has been confirmed! Your reservation is now Confirmed. "
                     f"Contact us to arrange pickup. — DonnyPhonehub Gh")
    elif pd_row['customer_phone']:
        send_sms(pd_row['customer_phone'],
                 f"Hi {pd_row['customer_name'].split()[0]}, your deposit of {fmt_ghs(pd_row['amount'])} "
                 f"has been confirmed! Contact us to arrange pickup. — DonnyPhonehub Gh")
    log_activity('Approved deposit notification', 'shop',
                 target_type='reservation', target_id=pd_row['reservation_id'],
                 details=f'Deposit #{pd_id}: {fmt_ghs(pd_row["amount"])} for reservation #{pd_row["reservation_id"]}')
    conn.close()
    flash('Deposit confirmed. Reservation is now Confirmed.', 'success')
    return redirect(url_for('admin_payments'))


@app.route('/admin/deposits/<int:pd_id>/reject', methods=['POST'])
@admin_required
def reject_pending_deposit(pd_id):
    if not has_permission('record_payment'):
        flash('Permission denied.', 'error')
        return redirect(url_for('admin_payments'))
    conn = get_db()
    pd_row = conn.execute(
        'SELECT * FROM pending_deposits WHERE id=%s AND status=%s', (pd_id, 'Pending')
    ).fetchone()
    if not pd_row:
        flash('Deposit record not found or already reviewed.', 'error')
        conn.close()
        return redirect(url_for('admin_payments'))
    review_notes = request.form.get('review_notes', '').strip()
    conn.execute(
        "UPDATE pending_deposits SET status='Rejected',reviewed_by=%s,review_notes=%s,reviewed_at=NOW() WHERE id=%s",
        (session.get('admin_username','Admin'), review_notes or None, pd_id)
    )
    conn.commit()
    reason_txt = f' Reason: {review_notes}' if review_notes else ''
    phone = pd_row['customer_phone']
    name  = pd_row['customer_name']
    if pd_row['customer_id']:
        cust = conn.execute(
            'SELECT name, phone FROM customers WHERE id=%s', (pd_row['customer_id'],)
        ).fetchone()
        if cust:
            phone = cust['phone']
            name  = cust['name']
    if phone:
        send_sms(phone,
                 f"Hi {name.split()[0]}, your deposit of {fmt_ghs(pd_row['amount'])} "
                 f"could not be verified.{reason_txt} Please call 0541057500. — DonnyPhonehub Gh")
    log_activity('Rejected deposit notification', 'shop',
                 target_type='reservation', target_id=pd_row['reservation_id'],
                 details=f'Deposit #{pd_id} rejected. Notes: {review_notes}')
    conn.close()
    flash('Deposit rejected. Customer has been notified.', 'warning')
    return redirect(url_for('admin_payments'))


# ─── GLOBAL SEARCH ────────────────────────────────────────────────────────────

@app.route('/admin/search')
@admin_required
def admin_search():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return render_template('admin_search.html',
            query=q, results=None, total_results=0,
            error='Enter at least 2 characters to search.')
    search_term = f'%{q}%'
    conn = get_db()
    results = {}
    if has_permission('view_members'):
        rows = conn.execute(
            """SELECT id, name, phone, email, membership_tier, membership_expiry
               FROM customers
               WHERE name ILIKE %s OR phone ILIKE %s OR email ILIKE %s
               ORDER BY name LIMIT 10""",
            (search_term, search_term, search_term)
        ).fetchall()
        results['customers'] = [dict(r) for r in rows]
    if has_permission('view_bookings'):
        rows = conn.execute(
            """SELECT id, name, phone, email, device, service, date, status
               FROM bookings
               WHERE name ILIKE %s OR phone ILIKE %s OR email ILIKE %s
               OR device ILIKE %s OR service ILIKE %s OR CAST(id AS TEXT) = %s
               ORDER BY date DESC LIMIT 10""",
            (search_term, search_term, search_term, search_term, search_term, q)
        ).fetchall()
        results['bookings'] = [dict(r) for r in rows]
    if has_permission('view_installments'):
        rows = conn.execute(
            """SELECT ip.id, ip.device_name, ip.total_payable, ip.balance_remaining,
               ip.status, ip.plan_months, c.name AS customer_name, c.phone AS customer_phone
               FROM installment_plans ip
               JOIN customers c ON c.id=ip.customer_id
               WHERE c.name ILIKE %s OR c.phone ILIKE %s OR ip.device_name ILIKE %s
               OR CAST(ip.id AS TEXT) = %s
               ORDER BY ip.created_at DESC LIMIT 10""",
            (search_term, search_term, search_term, q)
        ).fetchall()
        results['plans'] = [dict(r) for r in rows]
    if has_permission('view_inventory'):
        rows = conn.execute(
            """SELECT id, brand, model, imei, condition, selling_price, status, color, storage
               FROM inventory
               WHERE brand ILIKE %s OR model ILIKE %s OR imei ILIKE %s
               OR color ILIKE %s OR CAST(id AS TEXT) = %s
               ORDER BY created_at DESC LIMIT 10""",
            (search_term, search_term, search_term, search_term, q)
        ).fetchall()
        results['inventory'] = [dict(r) for r in rows]
        rows = conn.execute(
            """SELECT r.id, r.customer_name, r.customer_phone, r.customer_email,
               r.status, r.deposit_amount, r.created_at, i.brand, i.model
               FROM reservations r JOIN inventory i ON i.id=r.item_id
               WHERE r.customer_name ILIKE %s OR r.customer_phone ILIKE %s
               OR r.customer_email ILIKE %s OR i.brand ILIKE %s OR i.model ILIKE %s
               OR CAST(r.id AS TEXT) = %s
               ORDER BY r.created_at DESC LIMIT 10""",
            (search_term, search_term, search_term, search_term, search_term, q)
        ).fetchall()
        results['reservations'] = [dict(r) for r in rows]
    if has_permission('manage_staff'):
        rows = conn.execute(
            """SELECT id, name, email, phone, role, is_active FROM staff
               WHERE name ILIKE %s OR email ILIKE %s OR phone ILIKE %s
               ORDER BY name LIMIT 5""",
            (search_term, search_term, search_term)
        ).fetchall()
        results['staff'] = [dict(r) for r in rows]
        try:
            rows = conn.execute(
                """SELECT id, customer_name, customer_phone, device_type, message, status, created_at
                   FROM device_enquiries
                   WHERE customer_name ILIKE %s OR customer_phone ILIKE %s OR device_type ILIKE %s
                   ORDER BY created_at DESC LIMIT 5""",
                (search_term, search_term, search_term)
            ).fetchall()
            results['enquiries'] = [dict(r) for r in rows]
        except Exception:
            results['enquiries'] = []
    conn.close()
    total_results = sum(len(v) for v in results.values())
    return render_template('admin_search.html',
        query=q, results=results, total_results=total_results, error=None)


@app.route('/admin/search/json')
@admin_required
def admin_search_json():
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    search_term = f'%{q}%'
    conn = get_db()
    suggestions = []
    if has_permission('view_members'):
        rows = conn.execute(
            """SELECT id, name, phone, email FROM customers
               WHERE name ILIKE %s OR phone ILIKE %s OR email ILIKE %s LIMIT 3""",
            (search_term, search_term, search_term)
        ).fetchall()
        for c in rows:
            suggestions.append({
                'type': 'customer', 'icon': '👤',
                'title': c['name'],
                'subtitle': f"{c['phone']} · {c['email']}",
                'url': f"/admin/members/{c['id']}"
            })
    if has_permission('view_bookings'):
        rows = conn.execute(
            """SELECT id, name, device, service, status FROM bookings
               WHERE name ILIKE %s OR device ILIKE %s OR CAST(id AS TEXT) = %s LIMIT 2""",
            (search_term, search_term, q)
        ).fetchall()
        for b in rows:
            suggestions.append({
                'type': 'booking', 'icon': '📋',
                'title': f"Booking #{b['id']} — {b['name']}",
                'subtitle': f"{b['device']} · {b['service']} · {b['status']}",
                'url': '/admin/bookings'
            })
    if has_permission('view_inventory'):
        rows = conn.execute(
            """SELECT id, brand, model, status, selling_price FROM inventory
               WHERE brand ILIKE %s OR model ILIKE %s OR imei ILIKE %s LIMIT 2""",
            (search_term, search_term, search_term)
        ).fetchall()
        for i in rows:
            suggestions.append({
                'type': 'inventory', 'icon': '📱',
                'title': f"{i['brand']} {i['model']}",
                'subtitle': f"GH₵{i['selling_price']:,.2f} · {i['status']}",
                'url': f"/shop/{i['id']}"
            })
    if has_permission('view_installments'):
        rows = conn.execute(
            """SELECT ip.id, ip.device_name, ip.status, c.name AS customer_name
               FROM installment_plans ip JOIN customers c ON c.id=ip.customer_id
               WHERE c.name ILIKE %s OR ip.device_name ILIKE %s OR CAST(ip.id AS TEXT) = %s
               LIMIT 1""",
            (search_term, search_term, q)
        ).fetchall()
        for p in rows:
            suggestions.append({
                'type': 'plan', 'icon': '💳',
                'title': f"Plan #{p['id']} — {p['customer_name']}",
                'subtitle': f"{p['device_name']} · {p['status']}",
                'url': '/admin/installments'
            })
    conn.close()
    return jsonify({'results': suggestions[:8]})


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
    return _safe_redirect(url_for('home'))


@app.errorhandler(429)
def too_many_requests(_e):
    path = request.path
    if path.startswith('/admin'):
        flash('Too many attempts. Please wait a minute and try again.', 'error')
        return render_template('admin_login.html'), 429
    flash('Too many attempts. Please wait a while and try again.', 'error')
    return _safe_redirect(url_for('home'))


# ─── RUN ──────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    app.run(debug=True)
