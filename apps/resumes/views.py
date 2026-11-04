import os
import re
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Length
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.generics import (
    ListAPIView,
    ListCreateAPIView,
    RetrieveDestroyAPIView,
    RetrieveUpdateDestroyAPIView,
    get_object_or_404,
)
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.ai_services.serializers import AIOptionsSerializer
from apps.resumes.analysis import run_analysis
from apps.resumes.builder import clean_content, import_resume_content, tailor_content
from apps.resumes.extraction import ExtractionError, extract_resume
from apps.resumes.models import BuiltResume, JobProfile, Resume, ResumeAnalysis
from apps.resumes.serializers import (
    BuiltResumeImportSerializer,
    BuiltResumeListSerializer,
    BuiltResumeSerializer,
    JobProfileSerializer,
    ResumeAnalysisCreateSerializer,
    ResumeAnalysisSerializer,
    ResumeDetailSerializer,
    ResumeSerializer,
    ResumeUploadSerializer,
    TailorRequestSerializer,
    TailorResponseSerializer,
)
from apps.usage import idempotency

# A pending analysis older than this is treated as interrupted and may be retried.
STALE_PENDING_AFTER = timedelta(minutes=10)
# Multipart overhead allowed on top of the file itself.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_code = "file_too_large"
    default_detail = "The file is too large."


def _max_upload_message():
    megabytes = settings.RESUME_MAX_UPLOAD_BYTES / (1024 * 1024)
    return f"The file must be {megabytes:g} MB or smaller."


def _safe_filename(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:255] or "resume"


class OwnedQuerysetMixin:
    """Restricts every lookup to the requester's own rows; other IDs return 404."""

    model = None

    def get_queryset(self):
        return self.model.objects.filter(owner=self.request.user)


@extend_schema(tags=["resumes"])
class ResumeListCreateView(OwnedQuerysetMixin, ListCreateAPIView):
    model = Resume
    serializer_class = ResumeSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .defer("extracted_text")
            .annotate(text_length=Length("extracted_text"))
        )

    @extend_schema(
        request={"multipart/form-data": ResumeUploadSerializer},
        responses={201: ResumeDetailSerializer},
    )
    def post(self, request, *args, **kwargs):
        # Refuse oversized bodies before Django streams them to disk.
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
        if declared > settings.RESUME_MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES:
            raise PayloadTooLarge(_max_upload_message())

        upload_serializer = ResumeUploadSerializer(data=request.data)
        upload_serializer.is_valid(raise_exception=True)
        upload = upload_serializer.validated_data["file"]
        if upload.size > settings.RESUME_MAX_UPLOAD_BYTES:
            raise serializers.ValidationError({"file": [_max_upload_message()]})

        filename = _safe_filename(upload.name)
        try:
            extracted = extract_resume(filename, upload.read())
        except ExtractionError as exc:
            raise serializers.ValidationError({"file": [exc.message]}, code=exc.code) from exc

        resume = Resume.objects.create(
            owner=request.user,
            original_filename=filename,
            file_type=extracted.file_type,
            size_bytes=upload.size,
            sha256=extracted.sha256,
            page_count=extracted.page_count,
            extracted_text=extracted.text,
        )
        resume.text_length = len(resume.extracted_text)
        return Response(ResumeDetailSerializer(resume).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["resumes"])
class ResumeDetailView(OwnedQuerysetMixin, RetrieveDestroyAPIView):
    model = Resume
    serializer_class = ResumeDetailSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().annotate(text_length=Length("extracted_text"))


@extend_schema(tags=["job profiles"])
class JobProfileListCreateView(OwnedQuerysetMixin, ListCreateAPIView):
    model = JobProfile
    serializer_class = JobProfileSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


@extend_schema(tags=["job profiles"])
class JobProfileDetailView(OwnedQuerysetMixin, RetrieveUpdateDestroyAPIView):
    model = JobProfile
    serializer_class = JobProfileSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "delete", "options"]


class _AIThrottleOnPostMixin:
    throttle_scope = "ai"

    def get_throttles(self):
        if self.request.method == "POST":
            return [ScopedRateThrottle()]
        return super().get_throttles()


@extend_schema(tags=["resume analyses"])
class ResumeAnalysisListCreateView(_AIThrottleOnPostMixin, OwnedQuerysetMixin, ListAPIView):
    model = ResumeAnalysis
    serializer_class = ResumeAnalysisSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().select_related("resume", "job_profile")

    @extend_schema(
        request=ResumeAnalysisCreateSerializer,
        responses={200: ResumeAnalysisSerializer, 201: ResumeAnalysisSerializer},
    )
    def post(self, request, *args, **kwargs):
        serializer = ResumeAnalysisCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        record, existing_id = idempotency.claim(
            request.user, "resume_analysis", data.get("idempotency_key")
        )
        if existing_id:
            # A retried request: return the analysis it already created.
            analysis = get_object_or_404(self.get_queryset(), pk=existing_id)
            return Response(ResumeAnalysisSerializer(analysis).data)
        try:
            # Saved before the AI call so an interrupted or failed run is still recorded.
            analysis = ResumeAnalysis.objects.create(
                owner=request.user,
                resume=data["resume_id"],
                job_profile=data["job_profile_id"],
                started_at=timezone.now(),
            )
        except BaseException:
            idempotency.abandon(record)
            raise
        idempotency.complete(record, analysis.pk)

        run_analysis(analysis, AIOptionsSerializer.options_from(data))
        return Response(ResumeAnalysisSerializer(analysis).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["resume analyses"])
class ResumeAnalysisDetailView(OwnedQuerysetMixin, RetrieveDestroyAPIView):
    model = ResumeAnalysis
    serializer_class = ResumeAnalysisSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().select_related("resume", "job_profile")


class AnalysisNotRetryable(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "not_retryable"
    default_detail = "Only failed or interrupted analyses can be retried."


@extend_schema(tags=["resume analyses"])
class ResumeAnalysisRetryView(_AIThrottleOnPostMixin, APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=AIOptionsSerializer, responses={200: ResumeAnalysisSerializer})
    def post(self, request, pk, *args, **kwargs):
        analysis = get_object_or_404(ResumeAnalysis, pk=pk, owner=request.user)
        options = AIOptionsSerializer(data=request.data or {})
        options.is_valid(raise_exception=True)

        # Claim atomically so two concurrent retries can't both call the provider.
        now = timezone.now()
        stale_before = now - STALE_PENDING_AFTER
        retryable = Q(status=ResumeAnalysis.Status.FAILED) | Q(
            Q(started_at__lt=stale_before)
            | Q(started_at__isnull=True, created_at__lt=stale_before),
            status=ResumeAnalysis.Status.PENDING,
        )
        claimed = ResumeAnalysis.objects.filter(retryable, pk=analysis.pk).update(
            status=ResumeAnalysis.Status.PENDING, error_code="", started_at=now
        )
        if not claimed:
            raise AnalysisNotRetryable()

        analysis = ResumeAnalysis.objects.select_related("resume", "job_profile").get(
            pk=analysis.pk
        )
        run_analysis(analysis, AIOptionsSerializer.options_from(options.validated_data))
        return Response(ResumeAnalysisSerializer(analysis).data)


# ---------------------------------------------------------------------------
# Resume maker
# ---------------------------------------------------------------------------


@extend_schema(tags=["resume maker"])
class BuiltResumeListCreateView(OwnedQuerysetMixin, ListCreateAPIView):
    """Resumes made in the resume maker. POST creates one from content (or blank)."""

    model = BuiltResume
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().select_related("job_profile")

    def get_serializer_class(self):
        return BuiltResumeSerializer if self.request.method == "POST" else BuiltResumeListSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


@extend_schema(tags=["resume maker"])
class BuiltResumeDetailView(OwnedQuerysetMixin, RetrieveUpdateDestroyAPIView):
    model = BuiltResume
    serializer_class = BuiltResumeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return super().get_queryset().select_related("job_profile")


@extend_schema(tags=["resume maker"])
class BuiltResumeImportView(_AIThrottleOnPostMixin, APIView):
    """Creates a resume-maker resume from an uploaded resume, using AI to fill the fields."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=BuiltResumeImportSerializer, responses={201: BuiltResumeSerializer})
    def post(self, request, *args, **kwargs):
        serializer = BuiltResumeImportSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        source = data["resume_id"]
        content = import_resume_content(
            request.user, source, AIOptionsSerializer.options_from(data)
        )
        title = (data.get("title") or "").strip() or os.path.splitext(source.original_filename)[0]
        built = BuiltResume.objects.create(owner=request.user, title=title[:150], content=content)
        return Response(BuiltResumeSerializer(built).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["resume maker"])
class BuiltResumeTailorView(_AIThrottleOnPostMixin, APIView):
    """
    Suggests field-level changes that tailor the resume to a job description.
    Nothing is saved: the client applies the changes the user accepts.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=TailorRequestSerializer, responses={200: TailorResponseSerializer})
    def post(self, request, pk, *args, **kwargs):
        built = get_object_or_404(BuiltResume, pk=pk, owner=request.user)
        serializer = TailorRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        content = data.get("content") or clean_content(built.content)
        job_profile = data["job_profile_id"]
        source, changes = tailor_content(
            request.user, content, job_profile, AIOptionsSerializer.options_from(data)
        )
        return Response(
            TailorResponseSerializer(
                {"job_profile": job_profile, "source": source, "changes": changes}
            ).data
        )
