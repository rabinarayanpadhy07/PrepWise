"""
Resume maker: structured resume content, AI import from an uploaded resume,
and AI tailoring to a job description.

The resume's layout is a fixed LaTeX template rendered by the frontend. The
backend only stores and edits *content* (ResumeContentSerializer), so neither
the user nor the AI ever writes LaTeX. Tailoring returns a list of field-level
changes ({path, before, after, reason}) for the user to review; nothing is
applied or saved until the user accepts it.
"""

import json
import logging
import re

from rest_framework import serializers

from apps.ai_services.errors import AIInvalidResponse
from apps.ai_services.prompts import UNTRUSTED_CONTENT_RULE, object_schema, string_list, tagged
from apps.ai_services.service import run_json_task
from apps.resumes.basic import basic_tailoring

logger = logging.getLogger("prepwise.resumes")

IMPORT_FEATURE = "resume_import"
TAILOR_FEATURE = "resume_tailoring"
MAX_TOKENS = 12_000
MAX_CHANGES = 25

LINK_KINDS = ["linkedin", "github", "website", "other"]

# ---------------------------------------------------------------------------
# Content format
# ---------------------------------------------------------------------------


def _text(max_length, required=False):
    if required:
        return serializers.CharField(max_length=max_length)
    return serializers.CharField(
        max_length=max_length, required=False, allow_blank=True, default=""
    )


def _bullets():
    return serializers.ListField(
        child=serializers.CharField(max_length=600), max_length=12, required=False, default=list
    )


class LinkSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=LINK_KINDS)
    url = serializers.URLField(max_length=300)


class EducationSerializer(serializers.Serializer):
    institution = _text(200, required=True)
    dates = _text(80)
    degree = _text(200)
    score = _text(80)


class SkillGroupSerializer(serializers.Serializer):
    category = _text(100, required=True)
    items = _text(800, required=True)


class ExperienceSerializer(serializers.Serializer):
    company = _text(150, required=True)
    dates = _text(80)
    role = _text(150)
    location = _text(100)
    bullets = _bullets()


class ProjectSerializer(serializers.Serializer):
    name = _text(150, required=True)
    url = serializers.URLField(max_length=300, required=False, allow_blank=True, default="")
    tech = _text(300)
    bullets = _bullets()


class CertificationSerializer(serializers.Serializer):
    name = _text(200, required=True)
    url = serializers.URLField(max_length=300, required=False, allow_blank=True, default="")
    issuer = _text(150)


class ResumeContentSerializer(serializers.Serializer):
    """Everything that varies between resumes. The template supplies the rest."""

    name = _text(120)
    phone = _text(40)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True, default="")
    links = serializers.ListField(
        child=LinkSerializer(), max_length=6, required=False, default=list
    )
    summary = _text(2_500)
    education = serializers.ListField(
        child=EducationSerializer(), max_length=8, required=False, default=list
    )
    skills = serializers.ListField(
        child=SkillGroupSerializer(), max_length=15, required=False, default=list
    )
    experience = serializers.ListField(
        child=ExperienceSerializer(), max_length=12, required=False, default=list
    )
    projects = serializers.ListField(
        child=ProjectSerializer(), max_length=12, required=False, default=list
    )
    certifications = serializers.ListField(
        child=CertificationSerializer(), max_length=20, required=False, default=list
    )


def clean_content(data):
    """Validate content and return it with every field present (defaults filled in)."""
    serializer = ResumeContentSerializer(data=data or {})
    serializer.is_valid(raise_exception=True)
    return json.loads(json.dumps(serializer.validated_data))


# ---------------------------------------------------------------------------
# AI import: uploaded resume text -> structured content
# ---------------------------------------------------------------------------

_STR = {"type": "string"}

CONTENT_SCHEMA = object_schema(
    {
        "name": _STR,
        "phone": _STR,
        "email": _STR,
        "links": string_list(
            object_schema({"kind": {"type": "string", "enum": LINK_KINDS}, "url": _STR})
        ),
        "summary": _STR,
        "education": string_list(
            object_schema({"institution": _STR, "dates": _STR, "degree": _STR, "score": _STR})
        ),
        "skills": string_list(object_schema({"category": _STR, "items": _STR})),
        "experience": string_list(
            object_schema(
                {
                    "company": _STR,
                    "dates": _STR,
                    "role": _STR,
                    "location": _STR,
                    "bullets": string_list(),
                }
            )
        ),
        "projects": string_list(
            object_schema({"name": _STR, "url": _STR, "tech": _STR, "bullets": string_list()})
        ),
        "certifications": string_list(object_schema({"name": _STR, "url": _STR, "issuer": _STR})),
    }
)

IMPORT_SYSTEM_PROMPT = f"""You convert the text of a resume into structured fields.

Rules:
- Copy the candidate's information faithfully. Do not invent, embellish or
  summarise away any employer, date, title, skill, metric or achievement.
- Use an empty string or an empty list when the resume doesn't contain something.
- Keep each bullet point as written, fixing only broken line wraps and spacing.
- skills: group them as the resume does; "items" is a comma-separated list.
- links: only full URLs (add https:// if the resume omits it). kind is linkedin,
  github, website (a personal site or portfolio) or other.
- dates: keep the resume's own wording, e.g. "Mar 2026 -- Apr 2026".
- {UNTRUSTED_CONTENT_RULE}"""


class _ImportedContentSerializer(serializers.Serializer):
    """Passes the model's JSON through whole; import_resume_content validates it."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        return data


def _drop_bad_urls(content):
    # The model sometimes returns a bare domain or an empty link; drop what isn't a URL.
    url_field = serializers.URLField()

    def ok(value):
        try:
            url_field.run_validation(value)
            return True
        except serializers.ValidationError:
            return False

    content["links"] = [link for link in content.get("links", []) if ok(link.get("url", ""))]
    for key in ("projects", "certifications"):
        for item in content.get(key, []):
            if item.get("url") and not ok(item["url"]):
                item["url"] = ""
    if content.get("email") and "@" not in content["email"]:
        content["email"] = ""
    return content


def import_resume_content(user, resume, options=None):
    """Structured content extracted from an uploaded resume's text, validated."""
    result = run_json_task(
        user,
        feature=IMPORT_FEATURE,
        system=IMPORT_SYSTEM_PROMPT,
        prompt="\n\n".join(
            [tagged("resume", resume.extracted_text), "Return the resume as structured fields."]
        ),
        schema=CONTENT_SCHEMA,
        max_output_tokens=MAX_TOKENS,
        serializer_class=_ImportedContentSerializer,
        options=options,
    )
    content = _drop_incomplete(_drop_bad_urls(_truncate(dict(result.data))))
    try:
        return clean_content(content)
    except serializers.ValidationError as exc:
        logger.warning("Imported resume content failed validation: %s", sorted(exc.detail))
        raise AIInvalidResponse() from exc


# Entries without their identifying field are dropped rather than failing the import.
_REQUIRED = {
    "education": ("institution",),
    "skills": ("category", "items"),
    "experience": ("company",),
    "projects": ("name",),
    "certifications": ("name",),
}


def _drop_incomplete(content):
    for key, required in _REQUIRED.items():
        items = content.get(key)
        if isinstance(items, list):
            content[key] = [
                item
                for item in items
                if isinstance(item, dict) and all(str(item.get(f, "")).strip() for f in required)
            ]
    return content


def _truncate(content):
    """Clip over-long values from the model to the content limits instead of failing."""
    limits = {
        "name": 120, "phone": 40, "summary": 2_500,
        "institution": 200, "dates": 80, "degree": 200, "score": 80,
        "category": 100, "items": 800, "company": 150, "role": 150, "location": 100,
        "tech": 300, "issuer": 150,
    }  # fmt: skip
    list_limits = {
        "links": 6, "education": 8, "skills": 15, "experience": 12,
        "projects": 12, "certifications": 20,
    }  # fmt: skip

    def clip(obj):
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if isinstance(value, str) and key in limits:
                    out[key] = value[: limits[key]]
                elif key == "bullets" and isinstance(value, list):
                    out[key] = [str(b)[:600] for b in value if str(b).strip()][:12]
                elif isinstance(value, list):
                    out[key] = [clip(v) for v in value][: list_limits.get(key, len(value))]
                else:
                    out[key] = value
            return out
        return obj

    return clip(content)


# ---------------------------------------------------------------------------
# AI tailoring: content + job description -> reviewed field changes
# ---------------------------------------------------------------------------

_PATH = re.compile(
    r"^(?:summary"
    r"|skills\.(?P<skill>\d+)\.items"
    r"|experience\.(?P<exp>\d+)\.bullets\.(?P<exp_bullet>\d+)"
    r"|projects\.(?P<proj>\d+)\.bullets\.(?P<proj_bullet>\d+)"
    r"|projects\.(?P<proj_tech>\d+)\.tech)$"
)

TAILOR_SYSTEM_PROMPT = f"""You are an expert resume editor. You tailor a candidate's
existing resume to one job description by editing a few fields.

You receive the editable fields as "path: value" lines. Return only the fields
you change, each with its path, the full new value and a one-sentence reason
that names the job requirement it addresses.

Rules:
- Truthfulness comes first. Never add an employer, title, date, technology,
  tool, certification, metric or achievement that isn't already in the resume.
  You may reword, reorder, tighten, and use the job description's terminology
  for things the resume already shows.
- skills.N.items: a comma-separated list. You may reorder it to put the
  job's most relevant skills first. Only list skills that already appear
  somewhere in the resume.
- Bullets: start with a strong past-tense verb, keep one line or two at most
  (about 30 words), keep any numbers exactly as written.
- summary: 2 to 4 sentences aimed at this role, using only facts from the resume.
- Change only fields that clearly improve the match; between 3 and 15 changes.
- Use only paths from the list you were given. Plain text only, no LaTeX or markdown.
- {UNTRUSTED_CONTENT_RULE}"""

TAILOR_SCHEMA = object_schema(
    {"changes": string_list(object_schema({"path": _STR, "after": _STR, "reason": _STR}))}
)


class _TailorChange(serializers.Serializer):
    path = serializers.CharField(max_length=60)
    after = serializers.CharField(max_length=2_500, allow_blank=True)
    reason = serializers.CharField(max_length=400, allow_blank=True)


class TailorOutputSerializer(serializers.Serializer):
    changes = serializers.ListField(child=_TailorChange(), max_length=40)


def editable_fields(content):
    """[(path, label, value)] for every field tailoring may change, in display order."""
    fields = []
    if content.get("summary"):
        fields.append(("summary", "Summary", content["summary"]))
    for i, group in enumerate(content.get("skills", [])):
        fields.append((f"skills.{i}.items", f"Skills · {group['category']}", group["items"]))
    for i, job in enumerate(content.get("experience", [])):
        for j, bullet in enumerate(job.get("bullets", [])):
            fields.append(
                (
                    f"experience.{i}.bullets.{j}",
                    f"Experience · {job['company']} · point {j + 1}",
                    bullet,
                )
            )
    for i, project in enumerate(content.get("projects", [])):
        if project.get("tech"):
            fields.append(
                (f"projects.{i}.tech", f"Projects · {project['name']} · stack", project["tech"])
            )
        for j, bullet in enumerate(project.get("bullets", [])):
            fields.append(
                (
                    f"projects.{i}.bullets.{j}",
                    f"Projects · {project['name']} · point {j + 1}",
                    bullet,
                )
            )
    return fields


def _content_text(content):
    """All resume text values (not field names), lowercased, joined by newlines."""
    values = []

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            values.append(value.lower())

    walk(content)
    return "\n".join(values)


def _skills_are_supported(after, content_text):
    """True if every comma-separated skill already appears in the resume as a whole term."""
    items = [item.strip().lower() for item in after.split(",") if item.strip()]
    return bool(items) and all(
        re.search(rf"(?<![a-z0-9]){re.escape(item)}(?![a-z0-9])", content_text) for item in items
    )


def validate_changes(content, proposed):
    """
    Keep only changes to real, editable fields whose value actually changes.
    Skill lists may only contain skills already present in the resume.
    Returns [{path, label, before, after, reason}] in resume order.
    """
    fields = {path: (label, value) for path, label, value in editable_fields(content)}
    order = {path: index for index, path in enumerate(fields)}
    text = _content_text(content)
    seen, changes = set(), []
    for change in proposed:
        path = change["path"].strip()
        after = " ".join(change["after"].split())
        if path in seen or path not in fields or not _PATH.match(path):
            continue
        label, before = fields[path]
        if not after or after == before:
            continue
        if path.startswith("skills.") and not _skills_are_supported(after, text):
            logger.info("Dropped a tailoring change that added unsupported skills (%s)", path)
            continue
        seen.add(path)
        changes.append(
            {
                "path": path,
                "label": label,
                "before": before,
                "after": after,
                "reason": change["reason"].strip(),
            }
        )
    changes.sort(key=lambda item: order[item["path"]])
    return changes[:MAX_CHANGES]


def tailor_content(user, content, job_profile, options=None):
    """
    (source, changes): proposed field changes that tailor `content` to
    `job_profile`. Nothing is saved. Without a plan, or if the AI request fails,
    the basic tailoring (skill lists reordered only) is used and source is "basic".
    """
    fields = editable_fields(content)
    if not fields:
        return "", []
    company = f" at {job_profile.company}" if job_profile.company else ""
    lines = "\n".join(f"{path}: {value}" for path, _label, value in fields)
    result = run_json_task(
        user,
        feature=TAILOR_FEATURE,
        system=TAILOR_SYSTEM_PROMPT,
        prompt="\n\n".join(
            [
                f"Target role: {job_profile.title}{company}",
                tagged("job_description", job_profile.description),
                tagged("resume_fields", lines),
                "Return the changes that tailor this resume to the job.",
            ]
        ),
        schema=TAILOR_SCHEMA,
        max_output_tokens=MAX_TOKENS,
        serializer_class=TailorOutputSerializer,
        options=options,
        basic=lambda: basic_tailoring(fields, job_profile),
    )
    changes = validate_changes(content, result.data["changes"])
    logger.info(
        "Tailoring (%s) proposed %s changes (%s kept)",
        result.source,
        len(result.data["changes"]),
        len(changes),
    )
    return result.source, changes


def apply_changes(content, changes):
    """Apply accepted {path, after} changes to a copy of `content` (used by tests and the API)."""
    updated = json.loads(json.dumps(content))
    for change in changes:
        match = _PATH.match(change["path"])
        if not match:
            continue
        groups = match.groupdict()
        try:
            if change["path"] == "summary":
                updated["summary"] = change["after"]
            elif groups["skill"] is not None:
                updated["skills"][int(groups["skill"])]["items"] = change["after"]
            elif groups["exp"] is not None:
                updated["experience"][int(groups["exp"])]["bullets"][int(groups["exp_bullet"])] = (
                    change["after"]
                )
            elif groups["proj"] is not None:
                updated["projects"][int(groups["proj"])]["bullets"][int(groups["proj_bullet"])] = (
                    change["after"]
                )
            elif groups["proj_tech"] is not None:
                updated["projects"][int(groups["proj_tech"])]["tech"] = change["after"]
        except (IndexError, KeyError):
            continue
    return updated
