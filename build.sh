#!/usr/bin/env bash
# Exit immediately if a command exits with a non-zero status
set -o errexit

echo "==> Upgrading pip..."
pip install --upgrade pip

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Collecting static files..."
python manage.py collectstatic --no-input

echo "==> Checking database configuration..."
if [ -n "$DATABASE_URL" ] || [ -n "$DB_HOST" ]; then
    echo "==> Database credentials detected. Running migrations..."
    if python manage.py migrate; then
        echo "==> Migrations applied successfully!"
    else
        echo "==> [ERROR] Database migration failed!"
        echo "==> If using Supabase, ensure you use the Connection Pooler (aws-0-<region>.pooler.supabase.com:5432) which supports IPv4, not the direct IPv6-only host (db.<ref>.supabase.co)."
        echo "==> If your password contains special characters (like commas or @), configure DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT individually."
        exit 1
    fi
else
    echo "==> [NOTICE] Neither DATABASE_URL nor DB_HOST is configured in Render."
    echo "==> Skipping build-time migrations. Please configure DATABASE_URL in Render Dashboard > Environment."
fi

echo "==> Build completed successfully!"
