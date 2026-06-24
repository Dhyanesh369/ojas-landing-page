import http.server
import socketserver
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import uuid
import os
import hashlib
import urllib.parse
import threading
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import logging
import html

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
IP_RATE_LIMIT = {}

PORT = 8000
def get_db_connection():
    # Vercel Supabase inserts custom parameters like ?supa=... which crashes psycopg2.
    # We prioritize the NON_POOLING url which is a standard connection string.
    postgres_url = os.environ.get('POSTGRES_URL_NON_POOLING') or os.environ.get('POSTGRES_URL') or os.environ.get('DATABASE_URL')
    
    if postgres_url:
        # Strip unsupported query parameters to prevent "INVALID URI QUERY PARAMETER" errors
        if '?' in postgres_url:
            base_url, query = postgres_url.split('?', 1)
            # Only keep standard psycopg2 parameters like sslmode
            safe_params = [p for p in query.split('&') if p.startswith('sslmode=')]
            postgres_url = base_url + ('?' + '&'.join(safe_params) if safe_params else '')
            
        return psycopg2.connect(postgres_url, cursor_factory=RealDictCursor)
        
    raise Exception('POSTGRES_URL environment variable is missing!')


SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', 'dummy@example.com')
SMTP_PASS = os.environ.get('SMTP_PASS', 'dummy_password')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'k.r.dhyan9894@gmail.com')

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            full_name TEXT,
            email TEXT UNIQUE NOT NULL,
            whatsapp_number TEXT,
            form_source TEXT,
            landing_page_url TEXT,
            utm_source TEXT,
            utm_medium TEXT,
            utm_campaign TEXT,
            utm_content TEXT,
            utm_term TEXT,
            referrer_url TEXT,
            ip_address TEXT,
            country TEXT,
            state TEXT,
            city TEXT,
            device_type TEXT,
            browser TEXT,
            operating_system TEXT,
            lead_status TEXT DEFAULT 'New',
            lead_score INTEGER DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            priority TEXT DEFAULT 'Normal',
            is_archived INTEGER DEFAULT 0,
            survey_planning_time TEXT,
            survey_currently_trying TEXT,
            survey_specialist TEXT,
            survey_challenge TEXT,
            survey_early_access TEXT
        )
    ''')
    
    # Add columns gracefully in case db already exists
    c.execute('ALTER TABLE leads ADD COLUMN IF NOT EXISTS survey_planning_time TEXT')
    c.execute('ALTER TABLE leads ADD COLUMN IF NOT EXISTS survey_currently_trying TEXT')
    c.execute('ALTER TABLE leads ADD COLUMN IF NOT EXISTS survey_specialist TEXT')
    c.execute('ALTER TABLE leads ADD COLUMN IF NOT EXISTS survey_challenge TEXT')
    c.execute('ALTER TABLE leads ADD COLUMN IF NOT EXISTS survey_early_access TEXT')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS visitor_analytics (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_data TEXT,
            ip_address TEXT,
            device_type TEXT,
            browser TEXT,
            operating_system TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS lead_events (
            id SERIAL PRIMARY KEY,
            lead_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            description TEXT,
            admin_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS submission_logs (
            id SERIAL PRIMARY KEY,
            email TEXT,
            ip_address TEXT,
            status TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS email_queue (
            id SERIAL PRIMARY KEY,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'Pending',
            retries INTEGER DEFAULT 0,
            error_log TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token TEXT PRIMARY KEY,
            admin_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )
    ''')
    
    c.execute('SELECT id FROM admins WHERE email = %s', ('k.r.dhyan9894@gmail.com',))
    if not c.fetchone():
        c.execute('INSERT INTO admins (id, email, password_hash) VALUES (%s, %s, %s)',
                  (str(uuid.uuid4()), 'k.r.dhyan9894@gmail.com', hash_password('dhyan369')))
                  
    c.execute('CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(lead_status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_email_status ON email_queue(status, next_retry_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_analytics_session ON visitor_analytics(session_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_analytics_type ON visitor_analytics(event_type)')
    
    conn.commit()
    conn.close()

def parse_user_agent(ua_string):
    if not ua_string: return 'Unknown', 'Unknown', 'Unknown'
    ua = ua_string.lower()
    device = 'Mobile' if 'mobile' in ua or 'android' in ua or 'iphone' in ua else 'Desktop'
    browser = 'Unknown'
    if 'chrome' in ua and 'edg' not in ua: browser = 'Chrome'
    elif 'safari' in ua and 'chrome' not in ua: browser = 'Safari'
    elif 'firefox' in ua: browser = 'Firefox'
    elif 'edg' in ua: browser = 'Edge'
    os_name = 'Unknown'
    if 'windows' in ua: os_name = 'Windows'
    elif 'mac os' in ua: os_name = 'MacOS'
    elif 'linux' in ua: os_name = 'Linux'
    elif 'android' in ua: os_name = 'Android'
    elif 'iphone' in ua or 'ipad' in ua: os_name = 'iOS'
    return device, browser, os_name

def log_attempt(email, ip_address, status, error_message=''):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT INTO submission_logs (email, ip_address, status, error_message) VALUES (%s, %s, %s, %s)',
                  (email, ip_address, status, error_message))
        conn.commit()
        conn.close()
    except Exception: pass

def email_worker():
    while True:
        try:
            conn = get_db_connection()
            c = conn.cursor()
            
            c.execute('''
                SELECT * FROM email_queue 
                WHERE status != 'Sent' AND retries < 5 AND next_retry_at <= %s
            ''', (datetime.now().isoformat(),))
            
            emails = c.fetchall()
            
            for task in emails:
                task_id = task['id']
                try:
                    msg = MIMEMultipart()
                    msg['From'] = SMTP_USER
                    msg['To'] = task['to_email']
                    msg['Subject'] = task['subject']
                    msg.attach(MIMEText(task['body'], 'html'))
                    
                    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
                    server.starttls()
                    server.login(SMTP_USER, SMTP_PASS)
                    server.send_message(msg)
                    server.quit()
                    
                    c.execute("UPDATE email_queue SET status = 'Sent', error_log = 'Success' WHERE id = %s", (task_id,))
                except Exception as e:
                    next_retry = datetime.now() + timedelta(minutes=2 ** task['retries'])
                    c.execute('''
                        UPDATE email_queue 
                        SET status = 'Failed', retries = retries + 1, error_log = %s, next_retry_at = ?
                        WHERE id = %s
                    ''', (str(e), next_retry.isoformat(), task_id))
            conn.commit()
            conn.close()
        except Exception as e:
            pass
        time.sleep(10)

class handler(http.server.BaseHTTPRequestHandler):

    def get_session_admin(self):
        auth_header = self.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '): return None
        token = auth_header.split(' ')[1]
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT admin_id, expires_at FROM admin_sessions WHERE token = %s', (token,))
        row = c.fetchone()
        conn.close()
        if not row: return None
        if datetime.now() > datetime.fromisoformat(row[1]): return None
        return row[0]

    def send_json(self, status, data):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        if path.startswith('/api/admin/'):
            admin_id = self.get_session_admin()
            if not admin_id: return self.send_json(401, {'error': 'Unauthorized'})
            
            conn = get_db_connection()
            c = conn.cursor()
            
            if path == '/api/admin/dashboard':
                # Real Analytics
                c.execute('SELECT COUNT(DISTINCT session_id) as count FROM visitor_analytics')
                total_visitors = c.fetchone()['count']
                
                c.execute('SELECT COUNT(*) as count FROM leads WHERE is_archived = 0')
                total_leads = c.fetchone()['count']
                
                conv_rate = round((total_leads / total_visitors) * 100, 1) if total_visitors > 0 else 0
                
                c.execute("SELECT AVG(CAST(event_data AS INTEGER)) as avg_time FROM visitor_analytics WHERE event_type = 'Time on Page'")
                avg_time_row = c.fetchone()
                avg_time = round(avg_time_row['avg_time'] or 0)
                
                c.execute("SELECT COUNT(*) as bounces FROM visitor_analytics WHERE event_type = 'Bounce'")
                bounces = c.fetchone()['bounces']
                bounce_rate = round((bounces / total_visitors) * 100, 1) if total_visitors > 0 else 0
                
                c.execute("SELECT event_data, COUNT(*) as c FROM visitor_analytics WHERE event_type = 'CTA Button Click' GROUP BY event_data ORDER BY c DESC LIMIT 1")
                top_cta_row = c.fetchone()
                top_cta = top_cta_row['event_data'] if top_cta_row else 'None'
                
                c.execute("SELECT form_source, COUNT(*) as c FROM leads GROUP BY form_source ORDER BY c DESC LIMIT 1")
                top_form_row = c.fetchone()
                top_form = top_form_row['form_source'] if top_form_row else 'None'
                
                c.execute("SELECT form_source, COUNT(*) as count FROM leads WHERE is_archived = 0 GROUP BY form_source")
                sources = {row['form_source']: row['count'] for row in c.fetchall()}
                
                c.execute("SELECT lead_status, COUNT(*) as count FROM leads WHERE is_archived = 0 GROUP BY lead_status")
                statuses = {row['lead_status']: row['count'] for row in c.fetchall()}
                
                # Daily Trend (Last 7 days)
                c.execute('''
                    SELECT DATE(created_at) as date, COUNT(*) as count 
                    FROM leads WHERE is_archived = 0 
                    GROUP BY DATE(created_at) ORDER BY date DESC LIMIT 7
                ''')
                daily_trend = {row['date']: row['count'] for row in reversed(c.fetchall())}
                
                conn.close()
                return self.send_json(200, {
                    'total_visitors': total_visitors,
                    'total_leads': total_leads,
                    'conversion_rate': conv_rate,
                    'avg_time_seconds': avg_time,
                    'bounce_rate': bounce_rate,
                    'most_clicked_cta': top_cta,
                    'most_successful_form': top_form,
                    'sources': sources,
                    'statuses': statuses,
                    'daily_trend': daily_trend
                })
                
            elif path == '/api/admin/leads':
                qs = urllib.parse.parse_qs(parsed.query)
                search = qs.get('search', [''])[0]
                status_filter = qs.get('status', [''])[0]
                show_archived = qs.get('archived', ['0'])[0] == '1'
                
                query = "SELECT * FROM leads WHERE is_archived = %s"
                params = [1 if show_archived else 0]
                if search:
                    query += " AND (full_name ILIKE %s OR email ILIKE %s)"
                    params.extend([f"%{search}%", f"%{search}%"])
                if status_filter:
                    query += " AND lead_status = %s"
                    params.append(status_filter)
                
                query += " ORDER BY created_at DESC"
                c.execute(query, params)
                leads = [dict(row) for row in c.fetchall()]
                conn.close()
                return self.send_json(200, {'leads': leads})
                
            elif path.startswith('/api/admin/leads/') and not path.endswith('/events'):
                lead_id = path.split('/')[-1]
                c.execute('SELECT * FROM leads WHERE id = %s', (lead_id,))
                lead = c.fetchone()
                if not lead:
                    conn.close()
                    return self.send_json(404, {'error': 'Not found'})
                
                c.execute('INSERT INTO lead_events (lead_id, event_type, description, admin_id) VALUES (%s, %s, %s, %s)',
                          (lead_id, 'Admin Viewed', 'Admin viewed lead profile', admin_id))
                conn.commit()
                
                c.execute('SELECT * FROM lead_events WHERE lead_id = %s ORDER BY created_at DESC', (lead_id,))
                events = [dict(row) for row in c.fetchall()]
                conn.close()
                
                lead_dict = dict(lead)
                lead_dict['timeline'] = events
                return self.send_json(200, lead_dict)
                
            elif path == '/api/admin/export':
                qs = urllib.parse.parse_qs(parsed.query)
                start_date = qs.get('start_date', [''])[0]
                end_date = qs.get('end_date', [''])[0]
                campaign = qs.get('campaign', [''])[0]
                status = qs.get('status', [''])[0]
                source = qs.get('source', [''])[0]
                export_format = qs.get('format', ['csv'])[0]
                
                query = "SELECT * FROM leads WHERE 1=1"
                params = []
                
                if start_date:
                    query += " AND date(created_at) >= %s"
                    params.append(start_date)
                if end_date:
                    query += " AND date(created_at) <= %s"
                    params.append(end_date)
                if campaign:
                    query += " AND utm_campaign ILIKE %s"
                    params.append(f"%{campaign}%")
                if status:
                    query += " AND lead_status = %s"
                    params.append(status)
                if source:
                    query += " AND form_source = %s"
                    params.append(source)
                    
                query += " ORDER BY created_at DESC"
                c.execute(query, params)
                leads = c.fetchall()
                conn.close()
                
                if export_format == 'xlsx':
                    import openpyxl
                    from io import BytesIO
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    ws.title = "Leads"
                    if leads:
                        headers = list(leads[0].keys())
                        ws.append(headers)
                        for row in leads:
                            # Convert to strings to avoid excel errors on None
                            ws.append([str(row[k]) if row[k] is not None else '' for k in headers])
                    
                    stream = BytesIO()
                    wb.save(stream)
                    data = stream.getvalue()
                    
                    self.send_response(200)
                    self.send_header('Content-type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    self.send_header('Content-Disposition', 'attachment; filename="ojas_leads_export.xlsx"')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    import io
                    import csv
                    stream = io.StringIO()
                    writer = csv.writer(stream)
                    if leads:
                        headers = list(leads[0].keys())
                        writer.writerow(headers)
                        for row in leads:
                            writer.writerow([str(row[k]) if row[k] is not None else '' for k in headers])
                    
                    data = stream.getvalue().encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-type', 'text/csv')
                    self.send_header('Content-Disposition', 'attachment; filename="ojas_leads_export.csv"')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                return
                
            conn.close()
            return self.send_json(404, {'error': 'Not found'})
            
        return self.send_json(404, {'error': 'Not found'})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b''
        try: data = json.loads(post_data.decode('utf-8')) if post_data else {}
        except: data = {}
            
        if path == '/api/analytics':
            session_id = data.get('session_id')
            events = data.get('events', [])
            if not session_id or not events:
                return self.send_json(400, {'error': 'Missing data'})
                
            ip_address = self.headers.get('x-forwarded-for', '127.0.0.1').split(',')[0].strip()
            ua_string = self.headers.get('User-Agent', '')
            device_type, browser, operating_system = parse_user_agent(ua_string)
            
            conn = get_db_connection()
            c = conn.cursor()
            c.executemany('''
                INSERT INTO visitor_analytics 
                (session_id, event_type, event_data, ip_address, device_type, browser, operating_system) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', [(session_id, ev.get('type', 'Unknown'), ev.get('data', ''), ip_address, device_type, browser, operating_system) for ev in events])
            conn.commit()
            conn.close()
            return self.send_json(200, {'success': True})
            
        elif path == '/api/admin/login':
            email = data.get('email')
            password = data.get('password')
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('SELECT id FROM admins WHERE email = %s AND password_hash = %s', (email, hash_password(password)))
                row = c.fetchone()
                if row:
                    token = str(uuid.uuid4())
                    expires = datetime.now() + timedelta(days=1)
                    c.execute('INSERT INTO admin_sessions (token, admin_id, expires_at) VALUES (%s, %s, %s)',
                              (token, row['id'] if isinstance(row, dict) else row[0], expires.isoformat()))
                    conn.commit()
                    conn.close()
                    return self.send_json(200, {'token': token})
                else:
                    conn.close()
                    return self.send_json(401, {'error': 'Invalid credentials'})
            except Exception as e:
                return self.send_json(500, {'error': f'Database error: {str(e)}'})
                
        elif path == '/api/admin/logout':
            auth_header = self.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('DELETE FROM admin_sessions WHERE token = %s', (token,))
                conn.commit()
                conn.close()
            return self.send_json(200, {'success': True})
            
        elif path.startswith('/api/admin/leads/') and path.endswith('/events'):
            admin_id = self.get_session_admin()
            if not admin_id: return self.send_json(401, {'error': 'Unauthorized'})
            
            lead_id = path.split('/')[4]
            event_type = data.get('event_type')
            desc = data.get('description')
            
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('INSERT INTO lead_events (lead_id, event_type, description, admin_id) VALUES (%s, %s, %s, %s)',
                      (lead_id, event_type, desc, admin_id))
            conn.commit()
            conn.close()
            return self.send_json(200, {'success': True})
            
        elif path == '/api/leads':
            ip_address = self.headers.get('x-forwarded-for', '127.0.0.1').split(',')[0].strip()
            
            # Rate limiting: max 5 requests per minute per IP
            now = time.time()
            if ip_address in IP_RATE_LIMIT:
                requests = [t for t in IP_RATE_LIMIT[ip_address] if now - t < 60]
                if len(requests) >= 5:
                    logging.warning(f"Rate limit exceeded for IP: {ip_address}")
                    return self.send_json(429, {'error': 'Too many requests. Please try again later.'})
                requests.append(now)
                IP_RATE_LIMIT[ip_address] = requests
            else:
                IP_RATE_LIMIT[ip_address] = [now]
                
            ua_string = self.headers.get('User-Agent', '')
            device_type, browser, operating_system = parse_user_agent(ua_string)
            
            try:
                email = data.get('email', '').strip()
                full_name = data.get('name', '').strip()
                whatsapp_number = data.get('phone', '').strip()
                form_source = data.get('source', 'Unknown').strip()
                
                if not email:
                    log_attempt(email, ip_address, 'Error', 'Email is required')
                    return self.send_json(400, {'error': 'Email is required'})
                
                conn = get_db_connection()
                c = conn.cursor()
                
                c.execute('SELECT id FROM leads WHERE email = %s', (email,))
                if c.fetchone():
                    log_attempt(email, ip_address, 'Duplicate', 'This email is already registered')
                    conn.close()
                    return self.send_json(400, {'error': 'This email is already registered.'})
                
                lead_id = str(uuid.uuid4())
                c.execute('''
                    INSERT INTO leads (
                        id, full_name, email, whatsapp_number, form_source,
                        landing_page_url, utm_source, utm_medium, utm_campaign, utm_content, utm_term, referrer_url,
                        ip_address, device_type, browser, operating_system
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    lead_id, full_name, email, whatsapp_number, form_source,
                    data.get('landing_page_url', ''), data.get('utm_source', ''), data.get('utm_medium', ''),
                    data.get('utm_campaign', ''), data.get('utm_content', ''), data.get('utm_term', ''),
                    data.get('referrer_url', ''), ip_address, device_type, browser, operating_system
                ))
                
                c.execute('INSERT INTO lead_events (lead_id, event_type, description) VALUES (%s, %s, %s)',
                          (lead_id, 'Lead Created', f"Lead submitted via {form_source}"))
                
                # --- SYNCHRONOUS EMAIL SENDING FOR VERCEL ---
                visitor_subject = "You're on the OJAS Parenthood waitlist."
                visitor_body = f"<h2>Hi {html.escape(full_name) if full_name else 'there'},</h2><p>Thank you for joining the OJAS Parenthood waitlist.</p><p>We are thrilled to help you on your journey. We will personally contact you before our official launch to secure your founding spot.</p><br><p>Best regards,<br>The OJAS Team</p>"
                
                admin_subject = f"New Lead: {full_name or email} ({form_source})"
                admin_body = f"<h2>New Lead Submitted</h2><p><b>Name:</b> {html.escape(full_name) if full_name else 'N/A'}</p><p><b>Email:</b> {html.escape(email)}</p><p><b>Phone:</b> {html.escape(whatsapp_number) if whatsapp_number else 'N/A'}</p><p><b>Source:</b> {html.escape(form_source)}</p><p><b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p><p><br><a href='http://localhost:8000/admin?lead_id={lead_id}' style='display:inline-block;padding:10px 15px;background:#1B3A2D;color:#fff;text-decoration:none;border-radius:4px;'>View Lead in CRM</a><br></p><hr><h3>Marketing Attribution</h3><p><b>Landing Page:</b> {html.escape(data.get('landing_page_url', 'N/A') or 'N/A')}</p><p><b>Referrer:</b> {html.escape(data.get('referrer_url', 'N/A') or 'N/A')}</p><p><b>UTM Source:</b> {html.escape(data.get('utm_source', 'N/A') or 'N/A')}</p><p><b>UTM Campaign:</b> {html.escape(data.get('utm_campaign', 'N/A') or 'N/A')}</p>"
                
                try:
                    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
                    server.starttls()
                    server.login(SMTP_USER, SMTP_PASS)
                    
                    # Visitor Email
                    msg = MIMEMultipart()
                    msg['From'] = SMTP_USER
                    msg['To'] = email
                    msg['Subject'] = visitor_subject
                    msg.attach(MIMEText(visitor_body, 'html'))
                    server.send_message(msg)
                    
                    # Admin Email
                    msg2 = MIMEMultipart()
                    msg2['From'] = SMTP_USER
                    msg2['To'] = ADMIN_EMAIL
                    msg2['Subject'] = admin_subject
                    msg2.attach(MIMEText(admin_body, 'html'))
                    server.send_message(msg2)
                    
                    server.quit()
                except Exception as e:
                    logging.error(f"Sync Email Failed on Vercel: {str(e)}")
                
                conn.commit()
                conn.close()
                
                log_attempt(email, ip_address, 'Success')
                return self.send_json(200, {'success': True, 'lead_id': lead_id, 'message': 'Lead saved successfully.'})
                
            except Exception as e:
                return self.send_json(500, {'error': 'Server error: ' + str(e)})

        elif path.startswith('/api/leads/') and path.endswith('/survey'):
            lead_id = path.split('/')[3]
            try:
                conn = get_db_connection()
                c = conn.cursor()
                
                c.execute('''
                    UPDATE leads SET
                        survey_planning_time = %s,
                        survey_currently_trying = %s,
                        survey_specialist = %s,
                        survey_challenge = %s,
                        survey_early_access = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (
                    data.get('planning_time'), data.get('currently_trying'), 
                    data.get('specialist'), data.get('challenge'), 
                    data.get('early_access'), lead_id
                ))
                
                c.execute('INSERT INTO lead_events (lead_id, event_type, description) VALUES (%s, %s, %s)',
                          (lead_id, 'Survey Completed', "Visitor completed the optional qualification survey."))
                          
                conn.commit()
                conn.close()
                return self.send_json(200, {'success': True})
            except Exception as e:
                return self.send_json(500, {'error': 'Server error: ' + str(e)})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/admin/leads/'):
            admin_id = self.get_session_admin()
            if not admin_id: return self.send_json(401, {'error': 'Unauthorized'})
            
            lead_id = path.split('/')[-1]
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            
            conn = get_db_connection()
            c = conn.cursor()
            
            c.execute('SELECT * FROM leads WHERE id = %s', (lead_id,))
            old_lead = dict(c.fetchone())
            
            status = data.get('lead_status', old_lead['lead_status'])
            priority = data.get('priority', old_lead['priority'])
            is_archived = data.get('is_archived', old_lead['is_archived'])
            
            c.execute('UPDATE leads SET lead_status = %s, priority = %s, is_archived = %s, updated_at = %s WHERE id = %s',
                      (status, priority, is_archived, datetime.now().isoformat(), lead_id))
            
            if status != old_lead['lead_status']:
                c.execute('INSERT INTO lead_events (lead_id, event_type, description, admin_id) VALUES (%s, %s, %s, %s)',
                          (lead_id, 'Status Updated', f"Status changed from {old_lead['lead_status']} to {status}", admin_id))
            if priority != old_lead['priority']:
                c.execute('INSERT INTO lead_events (lead_id, event_type, description, admin_id) VALUES (%s, %s, %s, %s)',
                          (lead_id, 'Priority Updated', f"Priority changed from {old_lead['priority']} to {priority}", admin_id))
            if is_archived != old_lead['is_archived']:
                desc = "Lead Archived" if is_archived == 1 else "Lead Restored"
                c.execute('INSERT INTO lead_events (lead_id, event_type, description, admin_id) VALUES (%s, %s, %s, %s)',
                          (lead_id, 'Archive Status', desc, admin_id))
            
            conn.commit()
            conn.close()
            return self.send_json(200, {'success': True})
            
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/admin/leads/'):
            admin_id = self.get_session_admin()
            if not admin_id: return self.send_json(401, {'error': 'Unauthorized'})
            
            lead_id = path.split('/')[-1]
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('DELETE FROM leads WHERE id = %s', (lead_id,))
            c.execute('DELETE FROM lead_events WHERE lead_id = %s', (lead_id,))
            conn.commit()
            conn.close()
            return self.send_json(200, {'success': True})

# Initialize DB safely on module load for Vercel
try:
    init_db()
except Exception as e:
    logging.error(f"Failed to initialize database on startup: {str(e)}")
