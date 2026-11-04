from django.urls import include, re_path

# Every API route lives under /api/<version>/. Only versions listed in
# REST_FRAMEWORK["ALLOWED_VERSIONS"] are matched; anything else returns 404.
urlpatterns = [
    re_path(r"^api/(?P<version>v1)/", include("config.api_v1")),
]

handler400 = "common.views.bad_request"
handler403 = "common.views.permission_denied"
handler404 = "common.views.page_not_found"
handler500 = "common.views.server_error"
