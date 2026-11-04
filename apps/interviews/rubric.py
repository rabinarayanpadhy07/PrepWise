"""
The answer-evaluation rubric.

Five competencies are scored on every answer so progress can be compared over
time. Each is interpreted for the specific role (title and job description)
and for the question's own guidance, which is what makes the rubric role-specific.
"""

RUBRIC_VERSION = "2026-10-v1"

COMPETENCIES = {
    "role_knowledge": {
        "label": "Role knowledge",
        "description": "Knowledge and judgement expected of someone in this role.",
        "anchors": {
            1: "Inaccurate or missing role-relevant knowledge.",
            3: "Generally correct but shallow or generic for this role.",
            5: "Accurate, specific and clearly at the level the role requires.",
        },
    },
    "problem_solving": {
        "label": "Problem solving",
        "description": "How the candidate reasons about the problem, trade-offs and decisions.",
        "anchors": {
            1: "No clear approach or reasoning.",
            3: "A reasonable approach with limited discussion of trade-offs.",
            5: "A clear approach that weighs alternatives and justifies decisions.",
        },
    },
    "communication": {
        "label": "Communication",
        "description": "Clarity, concision and suitability for the interviewer.",
        "anchors": {
            1: "Hard to follow or does not address the question.",
            3: "Understandable, with some rambling or vagueness.",
            5: "Clear, concise and directly answers the question.",
        },
    },
    "structure": {
        "label": "Structure",
        "description": "A logical order, such as situation, task, action and result.",
        "anchors": {
            1: "No discernible structure.",
            3: "Partly structured; some parts missing or out of order.",
            5: "Well organised from start to finish.",
        },
    },
    "evidence": {
        "label": "Evidence and impact",
        "description": (
            "Concrete examples, the candidate's own contribution and measurable results."
        ),
        "anchors": {
            1: "No examples or results.",
            3: "An example with unclear personal contribution or outcome.",
            5: "Specific examples with clear personal contribution and results.",
        },
    },
}

COMPETENCY_CHOICES = [(code, spec["label"]) for code, spec in COMPETENCIES.items()]
COMPETENCY_CODES = list(COMPETENCIES)


def competency_label(code):
    return COMPETENCIES.get(code, {}).get("label", code)


def rubric_text(role_title):
    lines = [
        f"Score each criterion from 1 to 5, interpreting it for the role of {role_title} "
        "as described in the job description. Use 2 and 4 for answers between the anchors.",
    ]
    for code, spec in COMPETENCIES.items():
        anchors = "; ".join(f"{score} = {text}" for score, text in spec["anchors"].items())
        lines.append(f"- {code} ({spec['label']}): {spec['description']} Anchors: {anchors}")
    return "\n".join(lines)
