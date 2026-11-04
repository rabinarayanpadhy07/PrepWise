import json
import os
import sys
import traceback

# Servers default to production settings; set DJANGO_SETTINGS_MODULE to override.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

try:
    from django.core.wsgi import get_wsgi_application

    application = get_wsgi_application()
except Exception as startup_error:
    sys.stderr.write(f"DJANGO STARTUP ERROR: {startup_error}\n")
    traceback.print_exc()

    def application(environ, start_response):
        status = "500 Internal Server Error"
        payload = json.dumps(
            {
                "status": "error",
                "message": "Django failed to start",
                "detail": str(startup_error),
            }
        ).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
        ]
        start_response(status, headers)
        return [payload]


app = application
