import sqlite3
from datetime import datetime, timedelta

DB_NAME = "vpn_database.db"

def get_conn():
    # Добавлен timeout для предотвращения ошибки "database is locked"
    return sqlite3.connect(DB_NAME, timeout=10)

def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS servers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ip TEXT UNIQUE,
                        port INTEGER DEFAULT 2222,
                        active INTEGER DEFAULT 1,
                        name TEXT DEFAULT 'VPN Server',
                        max_users INTEGER DEFAULT 50,
                        price_1d INTEGER,
                        price_7d INTEGER,
                        price_30d INTEGER
                    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_id INTEGER,
                        server_id INTEGER,
                        username TEXT,
                        expire_date TIMESTAMP,
                        has_used_trial INTEGER DEFAULT 0,
                        active INTEGER DEFAULT 1,
                        is_trial INTEGER DEFAULT 0,
                        notified_3h INTEGER DEFAULT 0,
                        last_expired TIMESTAMP,
                        notified_1d INTEGER DEFAULT 0,
                        notified_3d INTEGER DEFAULT 0,
                        UNIQUE(tg_id, server_id),
                        FOREIGN KEY(server_id) REFERENCES servers(id)
                    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )''')

    default_settings = [
        ('trial_hours', '24'),
        ('price_1d', '50'),
        ('price_7d', '200'),
        ('price_30d', '500'),
        ('admin_contact', '@your_telegram_username'),
        ('manual_payment_details', 'Реквизиты пока не заданы администратором.'),
        ('site_url', 'https://amneziawg.fun'),
        # Автооплата: активный провайдер (off/aipay/platega). По умолчанию выключена.
        ('payment_provider', 'off'),
        # ID способа оплаты Platega (в примерах их доков: 2 = СБП QR). Уточняется у менеджера.
        ('platega_payment_method', '2'),
        # Отправка писем (регистрация по почте): auto | smtp | resend | off
        ('mail_mode', 'auto'),
        ('mail_smtp_host', ''),
        ('mail_smtp_port', '587'),
        ('mail_smtp_user', ''),
        ('mail_smtp_password', ''),
        ('mail_smtp_tls', 'starttls'),
        ('mail_from', ''),
        ('mail_resend_key', '')
    ]
    cursor.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", default_settings)
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_id INTEGER,
                        server_id INTEGER,
                        period TEXT,
                        amount INTEGER,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
                    
    cursor.execute('''CREATE TABLE IF NOT EXISTS manual_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_id INTEGER,
                        server_id INTEGER,
                        period TEXT,
                        amount INTEGER,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')

    # Запросы на получение реквизитов ручной оплаты с САЙТА (аналог диалога "Запрос реквизитов"
    # в боте) - реквизиты не показываются пользователю сразу, а только после одобрения админом.
    cursor.execute('''CREATE TABLE IF NOT EXISTS detail_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_id INTEGER,
                        server_id INTEGER,
                        period TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
                    
    cursor.execute('''CREATE TABLE IF NOT EXISTS free_trials (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ip TEXT UNIQUE,
                        server_id INTEGER,
                        peer_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
                        tg_id INTEGER PRIMARY KEY,
                        full_name TEXT,
                        username TEXT,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        accepted_tos INTEGER DEFAULT 0
                    )''')
                    
    # Безопасное добавление колонки, если таблица была создана ранее без нее
    try:
        cursor.execute("ALTER TABLE user_profiles ADD COLUMN accepted_tos INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Колонка для привязки заказа к платежу AiPay (безопасная миграция для старых баз)
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN aipay_uid TEXT")
    except sqlite3.OperationalError:
        pass

    # Колонка для хранения текста конфигурации, чтобы отдавать скачивание с сайта
    # в любой момент без повторного обращения по SSH к VPN-серверу
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN config_text TEXT")
    except sqlite3.OperationalError:
        pass

    # === УЧЕТНЫЕ ЗАПИСИ ПО ПОЧТЕ (регистрация/вход на сайте без Telegram) ===
    # tg_id заполняется при привязке (через бота, виджет входа или ссылку-привязку);
    # одна почта может иметь tg_id, один tg может иметь несколько почт - это не запрещаем.
    cursor.execute('''CREATE TABLE IF NOT EXISTS email_accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT,
                        tg_id INTEGER,
                        verified INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')

    # Одноразовые токены из писем: verify (подтверждение), reset (сброс пароля),
    # link (привязка почты к tg аккаунту)
    cursor.execute('''CREATE TABLE IF NOT EXISTS email_tokens (
                        token TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        tg_id INTEGER,
                        expires_at TIMESTAMP NOT NULL,
                        used INTEGER DEFAULT 0
                    )''')

    # Одноразовые токены входа в кабинет сайта ЧЕРЕЗ БОТА (deep-link /start web_login):
    # бот выдает ссылку на {site_url}/auth/bot?token=..., сайт по ней авторизует tg_id.
    # Нужно потому, что виджет входа Telegram нередко блокируют по IP/в браузерах.
    cursor.execute('''CREATE TABLE IF NOT EXISTS tg_login_tokens (
                        token TEXT PRIMARY KEY,
                        tg_id INTEGER NOT NULL,
                        expires_at TIMESTAMP NOT NULL,
                        used INTEGER DEFAULT 0
                    )''')

    # Администраторы ВЕБ-АДМИНКИ на сайте (главный админ из config.ADMIN_ID сидится отдельно)
    cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
                        tg_id INTEGER PRIMARY KEY,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        added_by INTEGER
                    )''')

    conn.commit()
    conn.close()

# --- Настройки бота ---
def get_setting(key):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def update_setting(key, value):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# --- Сервера ---
def add_server(ip, name, port=2222):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, active FROM servers WHERE ip=?", (ip,))
        row = cursor.fetchone()
        
        if row:
            if row[1] == 0:
                cursor.execute("UPDATE servers SET name=?, port=?, active=1 WHERE ip=?", (name, port, ip))
                conn.commit()
                conn.close()
                return True
            else:
                conn.close()
                return False 
        else:
            cursor.execute("INSERT INTO servers (ip, name, port) VALUES (?, ?, ?)", (ip, name, port))
            conn.commit()
            conn.close()
            return True
    except sqlite3.IntegrityError:
        return False

def rename_server(server_id, new_name):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET name=? WHERE id=?", (new_name, server_id))
    conn.commit()
    conn.close()

def set_server_limit(server_id, limit):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET max_users=? WHERE id=?", (limit, server_id))
    conn.commit()
    conn.close()

def delete_server(server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET active=0 WHERE id=?", (server_id,))
    conn.commit()
    conn.close()

def get_active_servers():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, ip, port, name, max_users FROM servers WHERE active=1")
    servers = cursor.fetchall()
    conn.close()
    return servers

def get_server_by_id(server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT ip, port, name, max_users, price_1d, price_7d, price_30d FROM servers WHERE id=?", (server_id,))
    server = cursor.fetchone()
    conn.close()
    return server

def update_server_price(server_id, period, price):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE servers SET {period}=? WHERE id=?", (price, server_id))
    conn.commit()
    conn.close()

# --- Пользователи ---
def count_paid_users_on_server(server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE server_id=? AND active=1 AND is_trial=0", (server_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_user_sub(tg_id, server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, server_id, username, expire_date, has_used_trial, active, is_trial, notified_3h, last_expired FROM users WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    user = cursor.fetchone()
    conn.close()
    return user

def get_user_subs(tg_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, server_id, username, expire_date, has_used_trial, active, is_trial, notified_3h, last_expired FROM users WHERE tg_id=?", (tg_id,))
    subs = cursor.fetchall()
    conn.close()
    return subs

def get_all_users():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, server_id, username, expire_date, active FROM users")
    users = cursor.fetchall()
    conn.close()
    return users

def get_unique_user_ids():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT tg_id FROM users")
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

def add_or_update_user(tg_id, server_id, username, days=0, hours=0, is_trial=False, config_text=None):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date, has_used_trial FROM users WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    row = cursor.fetchone()
    
    now = datetime.now()
    base_date = now
    
    if row and row[0]:
        try:
            db_date = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S.%f") if "." in row[0] else datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if db_date > now:
                base_date = db_date
        except ValueError:
            pass
            
    if days: new_expire = base_date + timedelta(days=days)
    elif hours: new_expire = base_date + timedelta(hours=hours)
    else: new_expire = base_date
        
    trial_flag_historic = 1 if is_trial else (1 if (row and row[1] == 1) else 0)
    current_trial_flag = 1 if is_trial else 0

    # config_text передается только при первой выдаче (после add_user.sh). При продлении
    # (config_text=None) сохраняем ранее записанный конфиг через подзапрос, чтобы INSERT OR REPLACE
    # его не затер - точно так же, как это уже сделано для last_expired.
    if config_text is not None:
        cursor.execute('''INSERT OR REPLACE INTO users (tg_id, server_id, username, expire_date, has_used_trial, active, is_trial, notified_3h, notified_1d, notified_3d, last_expired, config_text) 
                          VALUES (?, ?, ?, ?, ?, 1, ?, 0, 0, 0, (SELECT last_expired FROM users WHERE tg_id=? AND server_id=?), ?)''', 
                          (tg_id, server_id, username, new_expire, trial_flag_historic, current_trial_flag, tg_id, server_id, config_text))
    else:
        cursor.execute('''INSERT OR REPLACE INTO users (tg_id, server_id, username, expire_date, has_used_trial, active, is_trial, notified_3h, notified_1d, notified_3d, last_expired, config_text) 
                          VALUES (?, ?, ?, ?, ?, 1, ?, 0, 0, 0, 
                          (SELECT last_expired FROM users WHERE tg_id=? AND server_id=?), 
                          (SELECT config_text FROM users WHERE tg_id=? AND server_id=?))''', 
                          (tg_id, server_id, username, new_expire, trial_flag_historic, current_trial_flag, tg_id, server_id, tg_id, server_id))
    conn.commit()
    conn.close()

def get_user_config(tg_id, server_id):
    """Возвращает сохраненный текст конфигурации (для скачивания с сайта в любое время)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT config_text FROM users WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_config(tg_id, server_id, config_text):
    """Точечно перезаписывает config_text, не трогая срок действия/статус подписки.
    Используется для разовой досрочной перевыдачи конфига пользователям, оформившимся
    до появления сохранения конфигов в базе (у них config_text изначально NULL)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET config_text=? WHERE tg_id=? AND server_id=?", (config_text, tg_id, server_id))
    conn.commit()
    conn.close()


# --- УЧЕТНЫЕ ЗАПИСИ ПО ПОЧТЕ (email_accounts / email_tokens) ---
def create_email_account(email, password_hash=None, tg_id=None, verified=0):
    """Создает почтовую учетку. Возвращает id или None, если email уже занят."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO email_accounts (email, password_hash, tg_id, verified) VALUES (?, ?, ?, ?)",
            (email, password_hash, tg_id, verified)
        )
        acc_id = cursor.lastrowid
        conn.commit()
        return acc_id
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def get_email_account(email):
    """Ряд (id, email, password_hash, tg_id, verified) или None."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, password_hash, tg_id, verified FROM email_accounts WHERE email=?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_email_account_by_tg(tg_id):
    """Ряд (id, email, password_hash, tg_id, verified) первой привязанной к tg почты или None."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, password_hash, tg_id, verified FROM email_accounts WHERE tg_id=? LIMIT 1", (tg_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def set_email_password(email, password_hash):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE email_accounts SET password_hash=? WHERE email=?", (password_hash, email))
    conn.commit()
    conn.close()

def mark_email_verified(email):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE email_accounts SET verified=1 WHERE email=?", (email,))
    conn.commit()
    conn.close()

def bind_email_to_tg(email, tg_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE email_accounts SET tg_id=? WHERE email=?", (tg_id, email))
    conn.commit()
    conn.close()

def create_email_token(email, kind, tg_id, ttl_seconds):
    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO email_tokens (token, email, kind, tg_id, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, email, kind, tg_id, expires)
    )
    conn.commit()
    conn.close()
    return token

def _fresh_token_row(cursor, token, kind):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        "SELECT email, tg_id FROM email_tokens WHERE token=? AND kind=? AND used=0 AND expires_at > ?",
        (token, kind, now)
    )
    return cursor.fetchone()

def peek_email_token(token, kind):
    """(email, tg_id) если токен жив, иначе None. Токен НЕ расходуется (для предпросмотра форм)."""
    conn = get_conn()
    cursor = conn.cursor()
    row = _fresh_token_row(cursor, token, kind)
    conn.close()
    return row

def consume_email_token(token, kind):
    """(email, tg_id) если токен был жив - и он помечен использованным; иначе None."""
    conn = get_conn()
    cursor = conn.cursor()
    row = _fresh_token_row(cursor, token, kind)
    if row:
        cursor.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
        conn.commit()
    conn.close()
    return row


# --- ОДНОРАЗОВЫЕ ТОКЕНЫ ВХОДА НА САЙТ ЧЕРЕЗ БОТА (tg_login_tokens) ---
def create_tg_login_token(tg_id, ttl_seconds=600):
    """Ссылка на вход в кабинет, выданная ботом. Живет 10 минут, одноразовая."""
    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tg_login_tokens (token, tg_id, expires_at) VALUES (?, ?, ?)",
        (token, tg_id, expires)
    )
    conn.commit()
    conn.close()
    return token

def consume_tg_login_token(token):
    """tg_id если токен был жив (и сразу расходуется); иначе None."""
    if not token:
        return None
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT tg_id FROM tg_login_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now)
    )
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE tg_login_tokens SET used=1 WHERE token=?", (token,))
        conn.commit()
    conn.close()
    return row[0] if row else None


# --- АДМИНИСТРАТОРЫ ВЕБ-АДМИНКИ (таблица admins) ---
def list_admins():
    """Ряды (tg_id, added_at, added_by)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, added_at, added_by FROM admins ORDER BY added_at")
    rows = cursor.fetchall()
    conn.close()
    return rows

def is_admin_user(tg_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,))
    ok = cursor.fetchone() is not None
    conn.close()
    return ok

def add_admin(tg_id, added_by=None):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (tg_id, added_by) VALUES (?, ?)", (tg_id, added_by))
    conn.commit()
    conn.close()

def remove_admin(tg_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM admins WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()


# --- СВОДКИ ДЛЯ ВЕБ-АДМИНКИ ---
def get_all_profiles():
    """Все профили: (tg_id, full_name, username, last_active, accepted_tos, всего подписок, активных)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''SELECT p.tg_id, p.full_name, p.username, p.last_active, p.accepted_tos,
                        (SELECT COUNT(*) FROM users u WHERE u.tg_id = p.tg_id),
                        (SELECT COUNT(*) FROM users u WHERE u.tg_id = p.tg_id AND u.active = 1)
                      FROM user_profiles p ORDER BY p.last_active DESC''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_pending_detail_requests():
    """Заявки на реквизиты со статусом pending: (id, tg_id, server_id, period, created_at)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, tg_id, server_id, period, created_at FROM detail_requests WHERE status='pending' ORDER BY created_at"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_pending_manual_orders():
    """Заказы по чеку со статусом pending: (id, tg_id, server_id, period, amount, created_at)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(manual_orders)")
    cols = [c[1] for c in cursor.fetchall()]
    conn.close()
    created = "created_at" if "created_at" in cols else "id"
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT id, tg_id, server_id, period, amount, {created} FROM manual_orders WHERE status='pending' ORDER BY {created}"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_settings():
    """Все настройки: список (key, value) в алфавитном порядке."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings ORDER BY key")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_paid_revenue():
    """(кол-во оплаченных заказов, сумма руб.) по таблицам orders и manual_orders."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), IFNULL(SUM(amount),0) FROM orders WHERE status='paid'")
    c1, s1 = cursor.fetchone()
    try:
        cursor.execute("SELECT COUNT(*), IFNULL(SUM(amount),0) FROM manual_orders WHERE status='paid'")
        c2, s2 = cursor.fetchone()
    except sqlite3.OperationalError:
        c2, s2 = (0, 0)
    conn.close()
    return int(c1 or 0) + int(c2 or 0), int(s1 or 0) + int(s2 or 0)

def count_active_subs():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE active=1")
    n = cursor.fetchone()[0]
    conn.close()
    return int(n or 0)

def get_all_servers_full():
    """Все серверы (и неактивные): (id, ip, port, active, name, max_users, price_1d, price_7d, price_30d)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, ip, port, active, name, max_users, price_1d, price_7d, price_30d FROM servers ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return rows

def set_server_active(server_id, active):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE servers SET active=? WHERE id=?", (1 if active else 0, server_id))
    conn.commit()
    conn.close()


def get_expired_users():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, server_id, username FROM users WHERE expire_date < ? AND active=1", (datetime.now(),))
    expired = cursor.fetchall()
    conn.close()
    return expired

def get_expiring_soon_users():
    conn = get_conn()
    cursor = conn.cursor()
    time_limit = datetime.now() + timedelta(hours=3)
    cursor.execute("SELECT tg_id, server_id, username FROM users WHERE expire_date < ? AND expire_date > ? AND active=1 AND notified_3h=0", (time_limit, datetime.now()))
    users = cursor.fetchall()
    conn.close()
    return users

def get_expiring_soon_users_1d():
    conn = get_conn()
    cursor = conn.cursor()
    time_limit = datetime.now() + timedelta(days=1)
    time_floor = datetime.now() + timedelta(hours=3)
    cursor.execute("SELECT tg_id, server_id, username FROM users WHERE expire_date < ? AND expire_date > ? AND active=1 AND notified_1d=0", (time_limit, time_floor))
    users = cursor.fetchall()
    conn.close()
    return users

def get_expiring_soon_users_3d():
    conn = get_conn()
    cursor = conn.cursor()
    time_limit = datetime.now() + timedelta(days=3)
    time_floor = datetime.now() + timedelta(days=1)
    cursor.execute("SELECT tg_id, server_id, username FROM users WHERE expire_date < ? AND expire_date > ? AND active=1 AND notified_3d=0", (time_limit, time_floor))
    users = cursor.fetchall()
    conn.close()
    return users

def mark_notified_3h(tg_id, server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET notified_3h=1 WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    conn.commit()
    conn.close()

def mark_notified_1d(tg_id, server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET notified_1d=1 WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    conn.commit()
    conn.close()

def mark_notified_3d(tg_id, server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET notified_3d=1 WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    conn.commit()
    conn.close()

def deactivate_user(tg_id, server_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET active=0, last_expired=? WHERE tg_id=? AND server_id=?", (datetime.now(), tg_id, server_id))
    conn.commit()
    conn.close()

def delete_user_completely(tg_id, server_id=None):
    conn = get_conn()
    cursor = conn.cursor()
    if server_id is not None:
        cursor.execute("DELETE FROM users WHERE tg_id=? AND server_id=?", (tg_id, server_id))
    else:
        cursor.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

# --- Платежи ---
def create_order(tg_id, server_id, period, amount):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO orders (tg_id, server_id, period, amount, status) VALUES (?, ?, ?, ?, 'pending')", 
                   (tg_id, server_id, period, amount))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_order(order_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, server_id, period, amount, status FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    conn.close()
    return order

def complete_order(order_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()

def create_manual_order(tg_id, server_id, period, amount):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO manual_orders (tg_id, server_id, period, amount, status) VALUES (?, ?, ?, ?, 'pending')", 
                   (tg_id, server_id, period, amount))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_manual_order(order_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, server_id, period, amount, status FROM manual_orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    conn.close()
    return order

def update_manual_order_status(order_id, status):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE manual_orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()

# --- Запросы на реквизиты ручной оплаты с сайта (гейт одобрения, как в боте) ---
def create_detail_request(tg_id, server_id, period):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO detail_requests (tg_id, server_id, period, status) VALUES (?, ?, ?, 'pending')",
                   (tg_id, server_id, period))
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return request_id

def get_detail_request(request_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, server_id, period, status FROM detail_requests WHERE id=?", (request_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def set_detail_request_status(request_id, status):
    """Возвращает True, только если статус был изменен именно сейчас (запрос был 'pending')."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE detail_requests SET status=? WHERE id=? AND status='pending'", (status, request_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

# --- Автоматическая оплата AiPay ---
def create_aipay_order(tg_id, server_id, period, amount, aipay_uid):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO orders (tg_id, server_id, period, amount, status, aipay_uid) VALUES (?, ?, ?, ?, 'pending', ?)",
        (tg_id, server_id, period, amount, aipay_uid)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_order_by_uid(aipay_uid):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, server_id, period, amount, status FROM orders WHERE aipay_uid=?", (aipay_uid,))
    order = cursor.fetchone()
    conn.close()
    return order

def complete_order_by_uid(aipay_uid):
    """Помечает заказ оплаченным, только если он еще был в статусе pending.
    Возвращает True, если статус был изменен именно сейчас (защита от повторной обработки вебхука)."""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status='paid' WHERE aipay_uid=? AND status='pending'", (aipay_uid,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def fail_order_by_uid(aipay_uid, status_name="failed"):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status=? WHERE aipay_uid=? AND status='pending'", (status_name, aipay_uid))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

# --- Веб-триалы ---
def add_free_trial(ip, server_id, peer_id, expires_at):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO free_trials (ip, server_id, peer_id, expires_at) VALUES (?, ?, ?, ?)", (ip, server_id, peer_id, expires_at))
    conn.commit()
    conn.close()
    
def get_recent_trial_by_ip(ip, time_limit):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT created_at FROM free_trials WHERE ip = ? AND created_at > ?", (ip, time_limit))
    row = cursor.fetchone()
    conn.close()
    return row

def get_expired_trials():
    conn = get_conn()
    cursor = conn.cursor()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("SELECT id, server_id, peer_id FROM free_trials WHERE expires_at < ?", (now_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_free_trial(trial_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM free_trials WHERE id=?", (trial_id,))
    conn.commit()
    conn.close()
    
def get_inactive_users_30d():
    conn = get_conn()
    cursor = conn.cursor()
    time_limit = datetime.now() - timedelta(days=30)
    cursor.execute("SELECT tg_id, server_id, username FROM users WHERE active=0 AND last_expired < ?", (time_limit,))
    users = cursor.fetchall()
    conn.close()
    return users    
    
def update_user_profile(tg_id, full_name, username):
    conn = get_conn() # ОШИБКА ИСПРАВЛЕНА ЗДЕСЬ
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_profiles (tg_id, full_name, username, last_active)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(tg_id) DO UPDATE SET
        full_name=excluded.full_name,
        username=excluded.username,
        last_active=CURRENT_TIMESTAMP
    ''', (tg_id, full_name, username))
    conn.commit()
    conn.close()

def get_user_profile(tg_id):
    conn = get_conn() # ОШИБКА ИСПРАВЛЕНА ЗДЕСЬ
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, username, last_active, accepted_tos FROM user_profiles WHERE tg_id=?", (tg_id,))
    res = cursor.fetchone()
    conn.close()
    return res

def accept_tos(tg_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE user_profiles SET accepted_tos=1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()