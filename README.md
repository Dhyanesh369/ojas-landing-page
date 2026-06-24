# OJAS Parenthood

India's first clinical pre-conception programme platform. This application features a high-fidelity frontend, robust SQLite-backed CRM, automated email notifications, and comprehensive visitor analytics tracking designed for a fake-door validation launch.

## 🚀 Production Deployment Instructions

### 1. Server Requirements
- Python 3.10+
- A VPS (e.g., DigitalOcean, AWS EC2, Linode)
- Nginx (for Reverse Proxy & SSL)

### 2. Installation
Clone the repository to your production server:
```bash
git clone <your-repository-url>
cd ojas
```

Install the required python packages (a virtual environment is recommended):
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Variables (Critical for Security)
Do **NOT** hardcode credentials. Copy the example configuration and fill in your actual credentials.
```bash
cp .env.example .env
nano .env
```
Ensure your `SMTP_PASS` uses an App Password if you are using Gmail.

### 4. Running the Application
To run the server continuously in the background using `systemd`, create a service file:
```bash
sudo nano /etc/systemd/system/ojas.service
```
Paste the following:
```ini
[Unit]
Description=OJAS Python Server
After=network.target

[Service]
User=root
WorkingDirectory=/path/to/ojas
ExecStart=/path/to/ojas/venv/bin/python server.py
EnvironmentFile=/path/to/ojas/.env
Restart=always

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl enable ojas
sudo systemctl start ojas
```

### 5. Setting up Nginx & SSL (Recommended)
You should not expose Port 8000 directly. Use Nginx to proxy traffic and handle HTTPS:

```nginx
server {
    listen 80;
    server_name ojas.com www.ojas.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Then use Certbot to instantly generate an SSL certificate:
```bash
sudo certbot --nginx -d ojas.com -d www.ojas.com
```

## 🛡️ Security Features Active
- **Rate Limiting:** The backend automatically throttles IPs exceeding 5 submissions per minute.
- **SQL Injection Prevention:** All endpoints strictly utilize parameterized SQLite `?` queries.
- **Data Integrity:** `PRAGMA journal_mode=WAL` is enabled to support high-concurrency DB writes safely.
- **Form Protection:** Native server-side validation rejects duplicate emails and invalid formats.

## 📊 Monitoring
- Application errors and rate limits are logged to `app.log`.
- Admin endpoints securely log CRM access events.
- To view logs in real-time, run `tail -f app.log`.
