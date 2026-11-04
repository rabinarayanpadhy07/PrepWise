import os

from django.core.wsgi import get_wsgi_application

# Servers default to production settings; set DJANGO_SETTINGS_MODULE to override.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_wsgi_application()
app = application

