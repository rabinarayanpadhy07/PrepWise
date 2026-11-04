"""
Basic (rule-based, non-AI) interview content: questions from a built-in bank
chosen for the role, and feedback from simple checks on the answer text.
Used for users without a plan and whenever the AI request fails. Output has
the same shape as the AI's, and the UI labels it as not AI-generated.
"""

import random
import re

from apps.ai_services.basic_text import find_skills, keywords, role_family

# ---------------------------------------------------------------------------
# Questions: (text, competency, guidance). "{role}" is filled in.
# ---------------------------------------------------------------------------

OPENING = (
    "Tell me about yourself and why you're interested in this {role} role.",
    "communication",
    "A short, relevant summary of background and strengths, linked to this role, and a clear "
    "reason for wanting it. Two minutes at most.",
)

GENERAL = [
    (
        "Tell me about a project you're proud of. What was your personal contribution?",
        "evidence",
        "A specific project, the candidate's own actions (not just the team's), and a concrete, "
        "ideally measurable result.",
    ),
    (
        "Describe a time you had to deliver under a tight deadline. What did you do?",
        "structure",
        "Situation, task, actions and result in order; how work was prioritised; what was "
        "delivered and what was learned.",
    ),
    (
        "Tell me about a mistake you made at work or in a project, and what you learned from it.",
        "structure",
        "Honest ownership of the mistake, what was done to fix it, and a specific change in "
        "behaviour afterwards.",
    ),
    (
        "Describe a disagreement with a colleague and how you resolved it.",
        "communication",
        "Listening to the other view, keeping it professional, focusing on the shared goal, "
        "and the outcome of the conversation.",
    ),
    (
        "How do you prioritise when several urgent tasks land at the same time?",
        "problem_solving",
        "A clear method (impact, urgency, dependencies), communicating trade-offs to "
        "stakeholders, and a real example.",
    ),
    (
        "Tell me about a time you had to learn something new quickly to get a job done.",
        "problem_solving",
        "How the candidate approached learning, the resources used, how quickly they became "
        "effective and the result.",
    ),
    (
        "Describe a time you went beyond what was asked of you.",
        "evidence",
        "A specific example, why it mattered, the extra effort taken and the measurable impact.",
    ),
    (
        "What would you aim to achieve in your first three months as a {role}?",
        "role_knowledge",
        "Realistic goals: learning the product and team, early wins, and how success would be "
        "measured.",
    ),
]

ROLE_QUESTIONS = {
    "engineering": [
        (
            "Walk me through how you would design a system for a feature you've built recently. "
            "What trade-offs did you make?",
            "problem_solving",
            "Requirements, components, data flow, scaling and failure cases, and why the chosen "
            "trade-offs were right.",
        ),
        (
            "How do you make sure the code you ship is reliable and maintainable?",
            "role_knowledge",
            "Testing at the right levels, code review, readable design, monitoring, and examples.",
        ),
        (
            "Tell me about the hardest bug you've debugged. How did you find the cause?",
            "problem_solving",
            "A systematic approach: reproducing, narrowing down, using tools and logs, fixing the "
            "root cause and preventing it from recurring.",
        ),
        (
            "Describe a time you improved the performance of an application or service.",
            "evidence",
            "How the problem was measured, what was changed, and the before/after numbers.",
        ),
        (
            "How would you explain a technical decision to a non-technical stakeholder?",
            "communication",
            "Plain language, focus on impact and risk, and checking understanding.",
        ),
        (
            "How do you decide when to refactor code versus ship a new feature?",
            "problem_solving",
            "Weighing user value, risk, deadlines and technical debt, with an example.",
        ),
    ],
    "data": [
        (
            "Walk me through an analysis you did from question to recommendation.",
            "structure",
            "The business question, data sources, cleaning, method, findings and the decision it "
            "supported.",
        ),
        (
            "How do you check that your data is accurate before you rely on it?",
            "role_knowledge",
            "Validation checks, handling missing or duplicate data, sanity checks against known "
            "numbers.",
        ),
        (
            "Tell me about a time your analysis changed a decision.",
            "evidence",
            "The decision, the insight, how it was communicated, and the measurable outcome.",
        ),
        (
            "How would you design an experiment to test a new feature?",
            "problem_solving",
            "A clear hypothesis, metric, control group, sample size, duration and how to read "
            "the result.",
        ),
        (
            "How do you present complex findings to a non-technical audience?",
            "communication",
            "Leading with the answer, simple visuals, and the business impact.",
        ),
        (
            "Which model or method would you choose for a prediction problem, and why?",
            "role_knowledge",
            "Matching the method to the data and goal, baselines, evaluation metrics and "
            "trade-offs.",
        ),
    ],
    "product": [
        (
            "How do you decide what to build next?",
            "problem_solving",
            "Using user problems, data and business goals, a prioritisation framework and "
            "stakeholder input.",
        ),
        (
            "Tell me about a product or feature you launched. How did you measure success?",
            "evidence",
            "Goals and metrics defined up front, the launch, results and what was learned.",
        ),
        (
            "Describe a time you had to say no to a stakeholder.",
            "communication",
            "Understanding their need, explaining the trade-off with data, and offering an "
            "alternative.",
        ),
        (
            "How do you work with engineering and design to ship a feature?",
            "role_knowledge",
            "Clear problem framing, shared requirements, regular check-ins and handling scope "
            "changes.",
        ),
        (
            "How would you improve a product you use every day?",
            "problem_solving",
            "Identifying a real user problem, possible solutions, how to validate them and how to "
            "measure impact.",
        ),
    ],
    "design": [
        (
            "Walk me through your design process on a recent project.",
            "structure",
            "Research, problem definition, exploration, testing with users, iteration and outcome.",
        ),
        (
            "Tell me about a time user research changed your design.",
            "evidence",
            "What was learned, how the design changed and the measurable effect.",
        ),
        (
            "How do you handle feedback you disagree with?",
            "communication",
            "Listening, understanding the reason, using evidence, and finding the best outcome for "
            "users.",
        ),
        (
            "How do you make your designs accessible?",
            "role_knowledge",
            "Contrast, keyboard and screen-reader support, clear language and testing.",
        ),
        (
            "How do you balance user needs with business goals and technical limits?",
            "problem_solving",
            "Understanding each constraint, trade-offs and an example.",
        ),
    ],
    "marketing": [
        (
            "Tell me about a campaign you ran. What were the results?",
            "evidence",
            "Goal, audience, channels, budget, results against targets and lessons learned.",
        ),
        (
            "How do you decide which channels to invest in?",
            "problem_solving",
            "Audience, cost per acquisition, testing, and measuring return on spend.",
        ),
        (
            "Which metrics do you track to judge whether marketing is working?",
            "role_knowledge",
            "Funnel metrics tied to business outcomes, attribution and how decisions follow.",
        ),
        (
            "How would you launch a new product with a small budget?",
            "problem_solving",
            "Clear positioning, targeted channels, low-cost experiments and measuring results.",
        ),
        (
            "Describe a time a campaign didn't go as planned.",
            "structure",
            "What happened, how it was noticed, the changes made and the outcome.",
        ),
    ],
    "sales": [
        (
            "Walk me through how you qualify a new lead.",
            "role_knowledge",
            "A clear framework (need, budget, authority, timing) and how it saves time.",
        ),
        (
            "Tell me about the biggest deal you've closed. How did you win it?",
            "evidence",
            "The customer's need, the process, handling objections, and the deal value.",
        ),
        (
            "How do you handle a customer who says your price is too high?",
            "communication",
            "Understanding the concern, focusing on value, and negotiating without discounting "
            "too early.",
        ),
        (
            "How do you manage your pipeline to hit your targets?",
            "problem_solving",
            "Tracking stages, forecasting, prioritising and the results achieved.",
        ),
        (
            "Tell me about a deal you lost and what you learned.",
            "structure",
            "Honest reflection, the reasons, and what was done differently afterwards.",
        ),
    ],
    "finance": [
        (
            "Walk me through a financial model or analysis you built.",
            "structure",
            "Purpose, inputs and assumptions, structure, checks, and how it was used.",
        ),
        (
            "How do you make sure your numbers are accurate?",
            "role_knowledge",
            "Reconciliations, checks, reviews and controls.",
        ),
        (
            "Tell me about a time your analysis helped save money or improve results.",
            "evidence",
            "The problem, the analysis, the recommendation and the measurable impact.",
        ),
        (
            "How would you explain a variance against budget to a manager?",
            "communication",
            "Clear drivers, one-off versus ongoing causes, and recommended actions.",
        ),
        (
            "How do you prioritise during month-end or a tight reporting deadline?",
            "problem_solving",
            "Planning, dependencies, automation, and communicating risks early.",
        ),
    ],
    "people": [
        (
            "How do you find and attract strong candidates for a hard-to-fill role?",
            "role_knowledge",
            "Sourcing channels, employer branding, a good candidate experience "
            "and measuring results.",
        ),
        (
            "Tell me about a difficult employee situation you handled.",
            "structure",
            "Listening, fairness, following policy, clear communication and the outcome.",
        ),
        (
            "How do you make sure hiring decisions are fair?",
            "problem_solving",
            "Structured interviews, clear criteria, diverse panels and reviewing data.",
        ),
        (
            "Describe an improvement you made to an HR process.",
            "evidence",
            "The problem, the change and the measurable improvement.",
        ),
        (
            "How do you communicate a change that employees may not like?",
            "communication",
            "Explaining why, being honest, listening to concerns and following up.",
        ),
    ],
    "operations": [
        (
            "Tell me about a process you improved. How did you measure the result?",
            "evidence",
            "The problem, data used, the change, and before/after numbers.",
        ),
        (
            "How do you handle an unexpected disruption, such as a supplier failing?",
            "problem_solving",
            "Quick assessment, contingency options, communication and prevention.",
        ),
        (
            "How do you plan and track a project with many moving parts?",
            "role_knowledge",
            "Clear scope, milestones, owners, tracking tools and managing risks.",
        ),
        (
            "Describe a time you had to get people from different teams to work together.",
            "communication",
            "Shared goals, clear roles, regular updates and resolving conflicts.",
        ),
        (
            "Which metrics would you use to judge whether operations are running well?",
            "role_knowledge",
            "Relevant KPIs (cost, time, quality, service) and how they drive action.",
        ),
    ],
    "support": [
        (
            "Tell me about a time you turned an unhappy customer into a satisfied one.",
            "evidence",
            "Listening, empathy, solving the problem, following up, and the outcome.",
        ),
        (
            "How do you handle a question you don't know the answer to?",
            "problem_solving",
            "Being honest, finding the answer, escalating when needed, and following up.",
        ),
        (
            "How do you prioritise a queue of urgent requests?",
            "problem_solving",
            "Impact and urgency, service levels, and keeping customers informed.",
        ),
        (
            "How would you explain a technical problem to a non-technical customer?",
            "communication",
            "Simple language, checking understanding and clear next steps.",
        ),
        (
            "What would you do if you noticed the same problem reported by many customers?",
            "role_knowledge",
            "Spotting the pattern, escalating with evidence, and helping fix the root cause.",
        ),
    ],
}

SKILL_QUESTION = (
    "This role involves {skill}. Describe how you have used {skill} in a real project, "
    "including a challenge you faced.",
    "role_knowledge",
    "A specific project using {skill}, what the candidate did themselves, a challenge and how "
    "it was solved, and the result.",
)
RESUME_SKILL_QUESTION = (
    "Your resume mentions {skill}. Walk me through the most complex thing you've done with it.",
    "role_knowledge",
    "Depth of hands-on experience with {skill}: the problem, decisions made, trade-offs and "
    "the outcome.",
)


def basic_questions(*, role_title, job_profile, resume, count):
    """Interview questions for the role from the built-in bank, in the AI output format."""
    role = (role_title or "this").strip()
    job_text = job_profile.description if job_profile is not None else ""
    resume_text = resume.extracted_text if resume is not None else ""
    family = role_family(role_title, job_text)
    rng = random.Random()  # noqa: S311 - question variety, not security

    job_skills = find_skills(job_text)
    resume_skills = set(find_skills(resume_text))
    skill_items = []
    for skill in job_skills[:6]:
        template = RESUME_SKILL_QUESTION if skill in resume_skills else SKILL_QUESTION
        skill_items.append(tuple(part.replace("{skill}", skill) for part in template))
    if not job_skills and resume_skills:
        for skill in sorted(resume_skills)[:3]:
            skill_items.append(
                tuple(part.replace("{skill}", skill) for part in RESUME_SKILL_QUESTION)
            )

    specific = list(ROLE_QUESTIONS.get(family, []))
    general = list(GENERAL)
    rng.shuffle(specific)
    rng.shuffle(general)
    rng.shuffle(skill_items)

    # Warm-up first, then alternate role-specific, skill and behavioural questions.
    picked = [OPENING]
    pools = [specific, skill_items[:3], general]
    while len(picked) < count and any(pools):
        for pool in pools:
            if pool and len(picked) < count:
                picked.append(pool.pop())
    leftovers = specific + skill_items + general
    while len(picked) < count and leftovers:
        picked.append(leftovers.pop())
    while len(picked) < count:  # more questions requested than the bank holds
        picked.append(GENERAL[len(picked) % len(GENERAL)])

    return {
        "questions": [
            {"text": text.replace("{role}", role), "competency": competency, "guidance": guidance}
            for text, competency, guidance in picked[:count]
        ]
    }


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

SITUATION = re.compile(
    r"\b(when i was|at my (previous|last|current)|in my (previous|last|current) (role|job)|"
    r"situation|context|background|we had|there was|our team)\b",
    re.IGNORECASE,
)
TASK = re.compile(
    r"\b(my (goal|task|job|role|responsibility) was|i was (asked|responsible)|needed to|"
    r"had to|the goal was|the challenge was|objective)\b",
    re.IGNORECASE,
)
ACTION = re.compile(
    r"\bi (decided|built|led|created|designed|implemented|wrote|organi[sz]ed|analy[sz]ed|"
    r"worked|set up|introduced|proposed|spoke|talked|met|planned|tested|fixed|changed|"
    r"started|managed|coordinated|developed|launched|automated|reviewed|trained)\b",
    re.IGNORECASE,
)
RESULT = re.compile(
    r"\b(as a result|result(ed)?|outcome|which led to|led to|so that|reduced|increased|"
    r"improved|saved|grew|cut|delivered|achieved|launched|won)\b",
    re.IGNORECASE,
)
REASONING = re.compile(
    r"\b(because|so that|trade-?offs?|option|alternative|instead|decided|approach|considered|"
    r"compared|prioriti[sz]\w*|why|risk|root cause|first|then|finally)\b",
    re.IGNORECASE,
)
CONNECTORS = re.compile(r"\b(first(ly)?|then|next|after that|finally|in the end)\b", re.IGNORECASE)
NUMBERS = re.compile(r"\d|\b(percent|per cent|half|double|twice|thousand|million)\b", re.IGNORECASE)
FILLER = re.compile(r"\b(um+|uh+|erm|like,|you know|sort of|kind of)\b", re.IGNORECASE)


def _clamp(value):
    return max(1, min(5, int(round(value))))


def basic_evaluation(*, session, question, answer):
    """Feedback on one answer from simple text checks, in the AI output format."""
    text = answer.text or ""
    words = text.split()
    count = len(words)
    sentences = max(1, len([part for part in re.split(r"[.!?]+", text) if part.strip()]))
    avg_sentence = count / sentences

    question_words = keywords(f"{question.text} {question.guidance}")
    relevance = len(question_words & keywords(text)) / max(1, len(question_words))
    target_skills = set(find_skills(f"{question.text} {question.guidance} {session.role_title}"))
    if session.job_profile is not None:
        target_skills |= set(find_skills(session.job_profile.description))
    skills_mentioned = set(find_skills(text)) & target_skills

    star = {
        "situation": bool(SITUATION.search(text)),
        "task": bool(TASK.search(text)),
        "action": bool(ACTION.search(text)),
        "result": bool(RESULT.search(text)),
    }
    has_numbers = bool(NUMBERS.search(text))
    reasoning = len(REASONING.findall(text))
    fillers = len(FILLER.findall(text))

    if count < 8:
        scores = dict.fromkeys(
            ("role_knowledge", "problem_solving", "communication", "structure", "evidence"), 1
        )
    else:
        length_score = 1 if count < 25 else 2 if count < 50 else 4 if count <= 350 else 3
        communication = (
            length_score + (1 if relevance >= 0.25 else 0) - (1 if avg_sentence > 35 else 0)
        )
        communication -= 1 if fillers > max(3, count // 40) else 0
        scores = {
            "communication": _clamp(communication),
            "structure": _clamp(1 + sum(star.values()) + (0.5 if CONNECTORS.search(text) else 0)),
            "evidence": _clamp(
                1
                + (1.5 if has_numbers else 0)
                + (1 if star["action"] else 0)
                + (1 if star["result"] else 0)
                + (0.5 if count >= 60 else 0)
            ),
            "role_knowledge": _clamp(
                1
                + min(2, len(skills_mentioned))
                + (2 if relevance >= 0.35 else 1 if relevance >= 0.15 else 0)
            ),
            "problem_solving": _clamp(1 + min(3, reasoning / 2) + (1 if star["action"] else 0)),
        }
    overall = _clamp(sum(scores.values()) / len(scores))

    rationales = {
        "communication": (
            f"{count} words in about {sentences} sentence(s); "
            + (
                "it stays close to the question."
                if relevance >= 0.25
                else "it uses few of the question's key terms."
            )
        ),
        "structure": "Parts of a STAR answer found: "
        + (", ".join(part for part, found in star.items() if found) or "none")
        + ".",
        "evidence": (
            (
                "It includes numbers or measurable results. "
                if has_numbers
                else "No numbers or measurable results. "
            )
            + (
                "It describes what you did. "
                if star["action"]
                else "Your own actions aren't clear. "
            )
        ).strip(),
        "role_knowledge": (
            f"Mentions relevant skills: {', '.join(sorted(skills_mentioned))}."
            if skills_mentioned
            else "No role-specific skills or tools from the question or job description are mentioned."  # noqa: E501
        ),
        "problem_solving": (
            "It explains reasoning or trade-offs."
            if reasoning >= 2
            else "There is little explanation of why you chose your approach."
        ),
    }

    strengths = []
    if scores["structure"] >= 4:
        strengths.append("Your answer follows a clear situation, action and result structure.")
    if has_numbers:
        strengths.append("You back up your answer with numbers or measurable results.")
    if star["action"]:
        strengths.append('You describe your own actions using "I", not just the team\'s.')
    if skills_mentioned:
        strengths.append(f"You mention relevant skills: {', '.join(sorted(skills_mentioned))}.")
    if 50 <= count <= 350 and avg_sentence <= 35:
        strengths.append("The answer is a reasonable length and easy to follow.")

    improvements = []
    if count < 50:
        improvements.append(
            {
                "area": "Answer length",
                "explanation": f"At {count} words, the answer is too short to show your experience.",  # noqa: E501
                "suggestion": "Aim for about one to two minutes: describe the situation, what you did and the result.",  # noqa: E501
            }
        )
    elif count > 350:
        improvements.append(
            {
                "area": "Concision",
                "explanation": f"At {count} words, the answer may lose the interviewer's attention.",  # noqa: E501
                "suggestion": "Cut background detail and lead with the most important point.",
            }
        )
    missing_star = [part for part, found in star.items() if not found]
    if missing_star:
        improvements.append(
            {
                "area": "Structure",
                "explanation": f"The answer doesn't clearly cover: {', '.join(missing_star)}.",
                "suggestion": "Use the STAR method: Situation, Task, Action, Result, in that order.",  # noqa: E501
            }
        )
    if not has_numbers:
        improvements.append(
            {
                "area": "Evidence",
                "explanation": "Interviewers remember concrete results.",
                "suggestion": "Add a number: time saved, percentage improved, users affected, money saved.",  # noqa: E501
            }
        )
    if relevance < 0.15:
        improvements.append(
            {
                "area": "Relevance",
                "explanation": "The answer uses few of the question's key terms, so it may not address it directly.",  # noqa: E501
                "suggestion": "Restate the question in your first sentence and answer it directly.",
            }
        )
    if not improvements:
        improvements.append(
            {
                "area": "Depth",
                "explanation": "This is a solid answer by these basic checks.",
                "suggestion": "Practise adding a short reflection: what you learned or would do differently.",  # noqa: E501
            }
        )

    summary = (
        f"Basic feedback from automatic checks (not AI-generated). Your {count}-word answer "
        f"scores about {overall} out of 5 on length, structure, evidence and relevance. "
        "These checks can't judge the quality of your ideas the way an AI or a person can."
    )
    return {
        "overall_score": overall,
        "summary": summary,
        "strengths": strengths[:10],
        "improvements": improvements[:10],
        "competency_scores": [
            {"competency": code, "score": scores[code], "rationale": rationales[code]}
            for code in (
                "role_knowledge",
                "problem_solving",
                "communication",
                "structure",
                "evidence",
            )
        ],
    }
