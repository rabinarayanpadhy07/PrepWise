"""
Basic (rule-based, non-AI) resume features: keyword matching between a resume
and a job description, and tailoring that only reorders skill lists. Used for
users without a plan and whenever the AI request fails. Output has the same
shape as the AI's, and the UI labels it as not AI-generated.
"""

import re

from apps.ai_services.basic_text import evidence_line, find_skills, importance, role_family
from apps.interviews.basic import basic_questions
from apps.interviews.rubric import COMPETENCIES

MAX_SKILLS = 40
QUESTION_COUNT = 10
IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}

FAMILY_LABELS = {
    "engineering": "engineering",
    "data": "data",
    "product": "product",
    "design": "design",
    "marketing": "marketing",
    "sales": "sales",
    "finance": "finance",
    "people": "HR and people",
    "operations": "operations",
    "support": "customer support",
    "general": "most",
}

SUGGESTIONS = {
    "high": (
        "The job description stresses {skill}. If you have used it, add a specific example "
        "with a result to your resume; if not, prepare an honest answer on how you would "
        "get up to speed."
    ),
    "medium": (
        "The job description mentions {skill}. If you have relevant experience, name it in "
        "a bullet or your skills section so it is easy to find."
    ),
    "low": (
        "{skill} is listed as a nice-to-have. Mention it if you have it, but focus on the "
        "core requirements first."
    ),
}

NUMBER = re.compile(r"\d")
WORDS = re.compile(r"\w+")


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _matched(job_skills, resume_skills, resume_text):
    return [
        {
            "skill": skill,
            "evidence": _clip(evidence_line(skill, resume_text), 600)
            or "Mentioned in your resume.",
        }
        for skill in job_skills
        if skill in resume_skills
    ][:MAX_SKILLS]


def _missing(job_skills, resume_skills, job_text):
    items = []
    for skill in job_skills:
        if skill in resume_skills:
            continue
        level = importance(skill, job_text)
        items.append(
            {
                "skill": skill,
                "importance": level,
                "suggestion": SUGGESTIONS[level].format(skill=skill),
            }
        )
    items.sort(key=lambda item: IMPORTANCE_ORDER[item["importance"]])  # stable: keeps JD order
    return items[:MAX_SKILLS]


def _summary(job_profile, job_skills, matched, missing):
    role = job_profile.title
    if not job_skills:
        return (
            f"No skills from PrepWise's built-in list were found in the job description for "
            f"{role}, so this keyword comparison can't measure the match. Compare the "
            f"requirements with your resume yourself, or add a fuller job description."
        )
    share = round(100 * len(matched) / len(job_skills))
    text = (
        f"Keyword match for {role}: your resume mentions {len(matched)} of the "
        f"{len(job_skills)} skills found in the job description ({share}%)."
    )
    high = [item["skill"] for item in missing if item["importance"] == "high"]
    if high:
        text += f" Most important gaps: {', '.join(high[:5])}."
    elif missing:
        text += " No high-priority skills are missing."
    else:
        text += " Every skill found in the job description appears in your resume."
    return text + " This is a keyword comparison, so it can't judge the depth of your experience."


def _recommendations(job_profile, resume_text, matched, missing):
    recs = []
    high = [item["skill"] for item in missing if item["importance"] == "high"]
    if high:
        recs.append(
            f"Add evidence for these high-priority skills if you have them: {', '.join(high[:6])}."
        )
    other = [item["skill"] for item in missing if item["importance"] != "high"]
    if other:
        recs.append(f"Consider mentioning these if they apply to you: {', '.join(other[:6])}.")
    if matched:
        top = ", ".join(item["skill"] for item in matched[:3])
        recs.append(
            f"Prepare a short story (situation, task, action, result) for each of your "
            f"strongest matching skills: {top}."
        )
    lines = [line.strip() for line in resume_text.splitlines() if len(line.split()) >= 6]
    with_numbers = sum(1 for line in lines if NUMBER.search(line))
    if lines and with_numbers / len(lines) < 0.3:
        recs.append(
            "Few of your bullet points include numbers. Where you can, add measurable results "
            "(time saved, growth, scale, quality)."
        )
    word_count = len(WORDS.findall(resume_text))
    if word_count < 250:
        recs.append(
            "Your resume is quite short. Add detail on your responsibilities and results in "
            "recent roles or projects."
        )
    elif word_count > 1_100:
        recs.append(
            "Your resume is long. Trim older or less relevant points so the most relevant "
            "experience stands out."
        )
    title_words = [word for word in WORDS.findall(job_profile.title.lower()) if len(word) > 2]
    if title_words and not all(word in resume_text.lower() for word in title_words):
        recs.append(
            f"Use the words of the target role, {job_profile.title}, in your summary where "
            "they honestly describe your experience."
        )
    if job_profile.company:
        recs.append(
            f"Research {job_profile.company}: its products, customers and recent news, and "
            "prepare two or three questions to ask the interviewer."
        )
    recs.append("Practise your answers aloud with a mock interview before the real one.")
    return [_clip(rec, 600) for rec in recs[:15]]


def _questions(job_profile, resume, job_skills):
    family = role_family(job_profile.title, job_profile.description)
    generated = basic_questions(
        role_title=job_profile.title, job_profile=job_profile, resume=resume, count=QUESTION_COUNT
    )["questions"]
    items = []
    for index, question in enumerate(generated):
        skill = next(
            (skill for skill in job_skills if skill.lower() in question["text"].lower()), None
        )
        if skill:
            why = f"The job description mentions {skill}."
        elif index == 0:
            why = "Most interviews open with this."
        else:
            why = f"A common question in {FAMILY_LABELS.get(family, 'most')} interviews."
        items.append(
            {
                "question": _clip(question["text"], 600),
                "focus": COMPETENCIES[question["competency"]]["label"],
                "reason": _clip(f"{why} A strong answer covers: {question['guidance']}", 600),
            }
        )
    return items


def basic_analysis(resume, job_profile):
    """Resume-versus-job analysis by keyword matching, in the AI output format."""
    resume_text = resume.extracted_text or ""
    job_text = job_profile.description or ""
    job_skills = find_skills(job_text)
    resume_skills = set(find_skills(resume_text))
    matched = _matched(job_skills, resume_skills, resume_text)
    missing = _missing(job_skills, resume_skills, job_text)
    return {
        "summary": _clip(_summary(job_profile, job_skills, matched, missing), 3_000),
        "matched_skills": matched,
        "missing_skills": missing,
        "recommendations": _recommendations(job_profile, resume_text, matched, missing),
        "questions": _questions(job_profile, resume, job_skills),
    }


# ---------------------------------------------------------------------------
# Tailoring: reorder comma-separated lists so the job's skills come first
# ---------------------------------------------------------------------------


def _relevant(item, job_skills, job_text):
    if set(find_skills(item)) & job_skills:
        return True
    needle = item.strip().lower()
    return bool(needle) and bool(
        re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", job_text)
    )


def _reorder(value, job_skills, job_text):
    """(new value, moved items) with job-relevant items first; order otherwise kept."""
    items = [item.strip() for item in value.split(",") if item.strip()]
    relevant = [item for item in items if _relevant(item, job_skills, job_text)]
    rest = [item for item in items if item not in relevant]
    reordered = relevant + rest
    if reordered == items:
        return value, []
    return ", ".join(reordered), relevant


def basic_tailoring(fields, job_profile):
    """
    Changes in the AI tailoring format for (path, label, value) `fields`. Only
    skill and tech lists are touched, and only reordered: nothing is added or
    reworded, so every change stays truthful.
    """
    job_text = (job_profile.description or "").lower()
    job_skills = set(find_skills(job_profile.description))
    changes = []
    for path, _label, value in fields:
        if not (path.startswith("skills.") or path.endswith(".tech")):
            continue
        after, moved = _reorder(value, job_skills, job_text)
        if moved:
            changes.append(
                {
                    "path": path,
                    "after": after,
                    "reason": _clip(
                        f"Puts {', '.join(moved[:4])} first because the job description "
                        "asks for them.",
                        400,
                    ),
                }
            )
    return {"changes": changes}
