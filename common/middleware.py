import re
import uuid

from common.logging import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller-supplied ID only if it is short and harmless to log.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIDMiddleware:
    """Attaches a request ID to the request, the log context, and the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id

        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)

        response[REQUEST_ID_HEADER] = request_id
        return response
