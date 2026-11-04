# Deploying PrepWise Backend on Render

This guide outlines the configuration and steps to deploy the PrepWise Django backend to [Render](https://render.com) as a Web Service.

---

## 1. What Has Been Prepared

1. **Vercel Cleanup**: All previous Vercel artifacts (`vercel.json`, `.vercelignore`, and `[tool.vercel]` config in `pyproject.toml`) have been completely removed.
2. **Gunicorn Added**: `gunicorn==26.2.0` added to both `requirements.txt` and `pyproject.toml`.
3. **Optimized Build Script (`build.sh`)**: Runs dependency installs, static collection (`collectstatic`), and database migrations (`migrate`) with error-exit safety.
4. **Render Gunicorn Configuration (`gunicorn.conf.py`)**: Automatically reads `$PORT` provided by Render, binds to `0.0.0.0:$PORT`, configures multi-threaded workers (`gthread`), sets timeouts to 120s for AI workloads, and pipes logs to stdout/stderr.
5. **Python Version Pin (`.python-version`)**: Explicitly sets `3.12.8`.
6. **Host & CSRF Handling**: Automatically recognizes Render's dynamic hostname (`RENDER_EXTERNAL_HOSTNAME`) and `.onrender.com` in `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
7. **Blueprint (`render.yaml`)**: An Infrastructure-as-Code blueprint ready for 1-click Render Blueprint deploys.

---

## 2. Deploying to Render

You can deploy using either **Option A (Render Blueprint - Recommended)** or **Option B (Manual Web Service)**.

### Option A: Deploy via Blueprint (Fastest)

1. Push your latest git commit to GitHub (`git push origin main`).
2. Log in to [Render Dashboard](https://dashboard.render.com).
3. Click **New +** > **Blueprint**.
4. Select your `PrepWise` backend repository.
5. Render will automatically detect `render.yaml`.
6. Fill in the prompted secret variables (see Section 3 below) and click **Apply**.

---

### Option B: Deploy Manually as a Web Service

1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** > **Web Service**.
3. Connect your `PrepWise` repository (`https://github.com/rabinarayanpadhy07/PrepWise.git`).
4. Configure the service settings:
   - **Name**: `prepwise-backend` (or your preferred name)
   - **Region**: Oregon (or your preferred region, preferably close to your database)
   - **Branch**: `main`
   - **Root Directory**: Leave blank (if repo root contains `manage.py`)
   - **Runtime**: `Python 3`
   - **Build Command**: `./build.sh`
   - **Start Command**: `gunicorn config.wsgi:application`
   - **Plan**: Free (or Starter)
5. Expand **Advanced** and set:
   - **Health Check Path**: `/api/v1/health/`
   - **Auto-Deploy**: `Yes`
6. Under **Environment Variables**, add the required variables from Section 3.
7. Click **Create Web Service**.

---

## 3. Required Environment Variables

Configure these in the Render Dashboard (**Environment** tab of your Web Service):

| Key | Suggested / Required Value | Description |
| :--- | :--- | :--- |
| `PYTHON_VERSION` | `3.12.8` | Ensures Python 3.12 runtime. |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | Enables production settings with security checks. |
| `DJANGO_DEBUG` | `false` | Must be `false` in production. |
| `DJANGO_READ_ENV_FILE` | `false` | Instructs Django to read directly from environment variables. |
| `DJANGO_SECRET_KEY` | *(50+ char random string)* | Secure secret key for cryptographic signing. |
| `DJANGO_ALLOWED_HOSTS` | `.onrender.com` | Accepts `.onrender.com` and any custom domains (comma-separated). |
| `DATABASE_URL` | *(Postgres connection string)* | E.g. Supabase / Neon / Render Postgres URI. |
| `DB_SSLMODE` | `require` | Enforces SSL connection to the PostgreSQL database. |
| `CORS_ALLOWED_ORIGINS` | `https://your-frontend.vercel.app` | Comma-separated `https://` URLs of your frontend. |
| `CSRF_TRUSTED_ORIGINS` | `https://your-frontend.vercel.app` | Comma-separated `https://` URLs of your frontend. |
| `AUTH_COOKIE_SECURE` | `true` | Enforces HTTPS-only cookies for JWT refresh tokens. |
| `AUTH_COOKIE_SAMESITE` | `None` | Set to `None` when frontend and backend run on different domains. |
| `CORS_ALLOW_CREDENTIALS`| `true` | Allows cookies to be sent cross-domain. |
| `AI_KEY_ENCRYPTION_KEYS`| `20261103:<base64-key>` | Key for encrypting stored AI keys in the admin panel. |

### Optional / Integration Variables

| Key | Description |
| :--- | :--- |
| `PAYMENT_PROVIDER` | `razorpay` (or leave empty if payments are disabled) |
| `PAYMENT_KEY_ID` | Your Razorpay API Key ID (`rzp_test_...` or `rzp_live_...`) |
| `PAYMENT_KEY_SECRET` | Your Razorpay Key Secret |
| `PAYMENT_WEBHOOK_SECRET`| Your Razorpay Webhook Secret |
| `GOOGLE_OAUTH_CLIENT_ID`| Google Cloud OAuth Client ID (if using Google sign-in) |
| `API_DOCS_ENABLED` | Set to `true` if you want Swagger UI at `/api/v1/docs/` |

---

## 4. Verification

Once deployed:
1. Check the Render deployment logs:
   - You should see:
     ```
     ==> Upgrading pip...
     ==> Installing Python dependencies...
     ==> Collecting static files...
     ==> Running database migrations...
     ==> Build completed successfully!
     ==> Starting service with 'gunicorn config.wsgi:application'
     ```
2. Test the health check endpoint:
   ```bash
   curl -i https://<your-service>.onrender.com/api/v1/health/
   ```
   Expected response:
   ```json
   HTTP/1.1 200 OK
   Content-Type: application/json

   {"status":"ok"}
   ```
3. Test ready check (checks database connectivity):
   ```bash
   curl -i https://<your-service>.onrender.com/api/v1/health/ready/
   ```
   Expected response:
   ```json
   HTTP/1.1 200 OK
   {"status":"ready","checks":{"database":"ok"}}
   ```
