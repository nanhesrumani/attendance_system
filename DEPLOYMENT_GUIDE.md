# Deploying the Attendance System — GitHub + AWS

## What I found in your project
- Flask app (factory pattern, `create_app()` in `app.py`, launched via `run.py`)
- MySQL database (`mysql-connector-python`), schema in `sql_structure_and_data.sql`
- Face recognition: `insightface` + `onnxruntime` + `torch` (CPU), anti-spoofing model in `recognition/anti_spoof/`
- RTSP camera streams (`recognition/stream_manager.py`) — the app connects **out** to camera URLs, it doesn't need a webcam attached to the server
- Google Sheets integration (service account JSON) and OpenAI API usage
- No `requirements.txt`, `.env`, `Dockerfile`, or `.gitignore` in the export — I've written all of these for you (attached)
- No model weight files (`.onnx`/`.pth`) in the anti-spoof folder — you'll need to get those onto the server separately (see Step 3)

**Recommended architecture:** Dockerized Flask app on a single EC2 instance, MySQL on RDS, GitHub Actions for CI/CD, nginx + Let's Encrypt for HTTPS. This is the efficient middle ground for this app — ECS/Fargate or Lambda add complexity you don't need for one app instance with stateful in-memory camera-stream threads; a plain EC2 host with Docker is simplest to reason about and cheapest to run, and you can move to ECS later without changing the app itself since it's already containerized.

---

## Step 0 — Files I've prepared for you
Attached to this message:
- `requirements.txt` — pinned to versions that work together (torch CPU build, insightface, onnxruntime, etc.)
- `Dockerfile` — production image, runs via `gunicorn`
- `.dockerignore`, `.gitignore`
- `.env.example` — every config var your `config.py` reads
- `docker-compose.yml` — runs the app container, mounts persistent volumes for `dataset/`, `uploads/`, `credentials.json`, and the anti-spoof model folder
- `nginx/attendance.conf` — reverse proxy config
- `.github/workflows/deploy.yml` — auto-deploy on push to `main`

Drop these into the root of `attendance_system - Copy (3)` (rename that folder to something like `attendance-system` without spaces first — spaces in paths cause friction in Docker/CI).

---

## Step 1 — Push to GitHub

```bash
cd attendance-system
git init
git add .
git commit -m "Initial commit"
gh repo create attendance-system --private --source=. --push
# or: create the repo on github.com, then
# git remote add origin git@github.com:<you>/attendance-system.git
# git branch -M main && git push -u origin main
```

Double-check `git status` before your first commit — the `.gitignore` excludes `.env`, `credentials.json`, `dataset/images/*`, `dataset/embeddings/*`, `uploads/*`, and any `.onnx`/`.pth` files. You do **not** want student photos, face embeddings, DB passwords, or the Google service-account key in a repo, private or not.

---

## Step 2 — Set up AWS: RDS (database)

1. **RDS Console → Create database**
   - Engine: MySQL 8.0
   - Templates: Free tier (to start) or Production
   - Instance: `db.t3.micro` (fine for a college-scale deployment; resize later if needed)
   - Set master username/password — save these, they go in `.env` as `DB_USER`/`DB_PASS`
   - Public access: **No** (the app connects from inside the same VPC — more on this below)
   - Note the endpoint hostname once it's created (`DB_HOST`)

2. **Load your schema** — easiest from a machine that can reach RDS (temporarily allow your IP in the RDS security group, or do this step from the EC2 instance once it's up):
   ```bash
   mysql -h <DB_HOST> -u admin -p < sql_structure_and_data.sql
   ```

3. Security group for RDS: allow inbound port `3306` **only** from the EC2 instance's security group (not `0.0.0.0/0`).

---

## Step 3 — Set up AWS: EC2 (app server)

1. **Launch instance**
   - AMI: Ubuntu 22.04 LTS
   - Type: `t3.medium` minimum (torch + insightface + opencv want ≥4GB RAM; go `t3.large` if you'll run several concurrent camera streams — `MAX_STREAM_THREADS` in your config controls this)
   - Storage: 20–30GB gp3
   - Security group: allow `22` (your IP only), `80`, `443` from anywhere
   - Same VPC as the RDS instance
   - Create/select a key pair for SSH

2. **SSH in and install Docker**
   ```bash
   ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
   sudo apt update && sudo apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx
   sudo usermod -aG docker $USER
   # log out and back in for the group change to apply
   ```

3. **Clone your repo**
   ```bash
   git clone https://github.com/<you>/attendance-system.git
   cd attendance-system
   cp .env.example .env
   nano .env    # fill in DB_HOST (the RDS endpoint), DB_PASS, SECRET_KEY, etc.
   ```

4. **Get the missing model files onto the server** — since they weren't in your export:
   - Anti-spoof weights (`recognition/anti_spoof_models/*.pth` or `.onnx`): copy them from wherever you currently run this app, e.g. `scp -i your-key.pem -r ./recognition/anti_spoof_models ubuntu@<EC2_IP>:~/attendance-system/recognition/`
   - `credentials.json` (Google service account): same approach — `scp` it up, never commit it
   - `insightface`'s own detection/recognition model (`buffalo_l`) downloads automatically on first run and gets cached — this needs outbound internet access from EC2, which it has by default

5. **Build and run**
   ```bash
   docker compose build
   docker compose up -d
   docker compose logs -f   # watch for startup errors
   ```
   The app should now be reachable at `http://127.0.0.1:5000` on the instance itself.

6. **nginx + HTTPS**
   ```bash
   sudo cp nginx/attendance.conf /etc/nginx/sites-available/attendance
   sudo ln -s /etc/nginx/sites-available/attendance /etc/nginx/sites-enabled/
   sudo rm /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```
   If you have a domain, point its A record at the EC2 Elastic IP, edit `server_name` in the nginx config to match, then:
   ```bash
   sudo certbot --nginx -d your-domain.com
   ```
   Without a domain you can run over plain HTTP against the IP for now, but get a domain before this goes in front of real users — camera streams and login sessions over plain HTTP are not something to expose publicly long-term.

7. **Elastic IP** — allocate one and associate it with the instance so the public IP doesn't change on reboot.

---

## Step 4 — CI/CD with GitHub Actions

The included `.github/workflows/deploy.yml` SSHes into EC2 and re-deploys on every push to `main`.

In your GitHub repo → **Settings → Secrets and variables → Actions**, add:
- `EC2_HOST` — your Elastic IP or domain
- `EC2_USER` — `ubuntu`
- `EC2_SSH_KEY` — the **private** key content of the key pair you used to launch the instance

From then on, `git push` to `main` rebuilds and restarts the container automatically. `.env`, `credentials.json`, and the anti-spoof model files stay on the server (they're gitignored) and aren't touched by deploys.

---

## Step 5 — Sanity checks after deploy
- Visit the site, log in with `admin@college.edu / admin123` (from `run.py`) and **change that password immediately**
- Confirm a camera/RTSP stream connects — this depends on the camera being reachable from the EC2 instance's network, not just the app; if cameras are on a college LAN, you'll need a VPN or port-forward/relay so EC2 can reach the RTSP URLs
- Check `docker compose logs -f` for any missing-model or DB-connection errors
- Set up RDS automated backups (on by default with a 7-day retention on most templates — verify in the console)

---

## Ongoing costs (rough, us-east-1, on-demand pricing)
- EC2 `t3.medium`: ~$30/mo (or ~$8-10/mo with a 1-year Savings Plan)
- RDS `db.t3.micro`: ~$13/mo
- EBS storage, Elastic IP, data transfer: a few dollars/mo
- Free tier covers a good chunk of this for the first 12 months on a new AWS account

## Later, if you need to scale
- Move the container to ECS/Fargate behind an Application Load Balancer once you need >1 instance or auto-scaling — no app changes needed, just repackage the same Docker image
- Move `dataset/images` and `uploads` to S3 instead of an EC2 volume if you outgrow single-instance storage or want multi-instance deployments
