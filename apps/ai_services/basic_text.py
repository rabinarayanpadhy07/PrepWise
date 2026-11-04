"""
Text helpers for PrepWise's basic (rule-based, non-AI) features: a skills
vocabulary, keyword matching and role detection. Deterministic and offline;
nothing here calls a provider.
"""

import re

# Canonical skill name -> extra spellings. Matching is case-insensitive and
# whole-word, so "Go" doesn't match "good" and "Java" doesn't match "JavaScript".
SKILLS = {
    # Programming languages
    "Python": [],
    "Java": [],
    "JavaScript": ["JS", "ECMAScript"],
    "TypeScript": [],
    "C": [],
    "C++": ["cpp"],
    "C#": ["csharp"],
    "Go": ["Golang"],
    "Rust": [],
    "Ruby": [],
    "PHP": [],
    "Kotlin": [],
    "Swift": [],
    "Scala": [],
    "R": [],
    "MATLAB": [],
    "Dart": [],
    "Bash": ["shell scripting", "shell"],
    "SQL": [],
    # Web and mobile
    "HTML": ["HTML5"],
    "CSS": ["CSS3"],
    "React": ["React.js", "ReactJS"],
    "Next.js": ["NextJS"],
    "Angular": ["AngularJS"],
    "Vue": ["Vue.js", "VueJS"],
    "Svelte": [],
    "Node.js": ["Node", "NodeJS"],
    "Express": ["Express.js"],
    "Django": [],
    "Flask": [],
    "FastAPI": [],
    "Spring": ["Spring Boot"],
    "Ruby on Rails": ["Rails"],
    "Laravel": [],
    ".NET": ["dotnet", "ASP.NET"],
    "Tailwind CSS": ["Tailwind"],
    "Redux": [],
    "GraphQL": [],
    "REST APIs": ["REST", "RESTful", "REST API"],
    "gRPC": [],
    "Android": [],
    "iOS": [],
    "React Native": [],
    "Flutter": [],
    # Data and AI
    "PostgreSQL": ["Postgres"],
    "MySQL": [],
    "MongoDB": ["Mongo"],
    "Redis": [],
    "Elasticsearch": [],
    "Cassandra": [],
    "DynamoDB": [],
    "SQLite": [],
    "Oracle": [],
    "Snowflake": [],
    "BigQuery": [],
    "Spark": ["Apache Spark", "PySpark"],
    "Hadoop": [],
    "Kafka": ["Apache Kafka"],
    "Airflow": ["Apache Airflow"],
    "dbt": [],
    "ETL": ["ELT"],
    "Data modeling": ["data modelling"],
    "Data warehousing": ["data warehouse"],
    "Pandas": [],
    "NumPy": [],
    "scikit-learn": ["sklearn"],
    "TensorFlow": [],
    "PyTorch": [],
    "Machine learning": ["ML"],
    "Deep learning": [],
    "NLP": ["natural language processing"],
    "Computer vision": [],
    "LLMs": ["LLM", "large language models"],
    "Statistics": ["statistical analysis"],
    "A/B testing": ["AB testing", "experimentation"],
    "Data analysis": ["data analytics"],
    "Data visualization": ["data visualisation"],
    "Excel": ["Microsoft Excel", "spreadsheets"],
    "Tableau": [],
    "Power BI": ["PowerBI"],
    "Looker": [],
    # Cloud and DevOps
    "AWS": ["Amazon Web Services"],
    "Azure": ["Microsoft Azure"],
    "GCP": ["Google Cloud", "Google Cloud Platform"],
    "Docker": [],
    "Kubernetes": ["K8s"],
    "Terraform": [],
    "Ansible": [],
    "CI/CD": ["continuous integration", "continuous delivery", "continuous deployment"],
    "Jenkins": [],
    "GitHub Actions": [],
    "Git": [],
    "Linux": [],
    "Microservices": ["microservice"],
    "System design": ["distributed systems"],
    "Serverless": ["AWS Lambda", "Lambda"],
    "Monitoring": ["observability"],
    "Security": ["cybersecurity", "information security", "application security"],
    "Networking": ["TCP/IP"],
    # Engineering practice
    "Testing": ["unit testing", "test automation", "automated testing", "TDD"],
    "Selenium": [],
    "Cypress": [],
    "Playwright": [],
    "Agile": ["Scrum", "Kanban"],
    "Code review": ["code reviews"],
    "Algorithms": ["data structures"],
    "Object-oriented programming": ["OOP", "object oriented"],
    "Performance optimization": ["performance tuning", "performance optimisation"],
    # Product, design and business
    "Product management": ["product manager", "product owner"],
    "Roadmapping": ["roadmap", "roadmaps"],
    "User research": ["customer research", "user interviews"],
    "UX design": ["UX", "user experience"],
    "UI design": ["UI", "user interface design"],
    "Figma": [],
    "Sketch": [],
    "Adobe Creative Suite": ["Photoshop", "Illustrator", "InDesign"],
    "Prototyping": ["wireframing", "wireframes"],
    "Accessibility": ["WCAG", "a11y"],
    "Stakeholder management": ["stakeholders"],
    "Project management": ["project manager", "PMP"],
    "Jira": [],
    "Confluence": [],
    "SEO": ["search engine optimization", "search engine optimisation"],
    "SEM": ["Google Ads", "PPC", "paid search"],
    "Content marketing": ["content strategy"],
    "Social media": ["social media marketing"],
    "Email marketing": [],
    "Marketing analytics": ["Google Analytics"],
    "Brand management": ["branding"],
    "CRM": ["Salesforce", "HubSpot"],
    "Sales": ["business development", "B2B sales", "B2C sales"],
    "Lead generation": ["prospecting"],
    "Negotiation": [],
    "Account management": ["key account management"],
    "Customer success": ["customer support", "customer service"],
    "Financial analysis": ["financial modelling", "financial modeling", "FP&A"],
    "Accounting": ["bookkeeping", "GAAP", "IFRS"],
    "Budgeting": ["forecasting"],
    "Risk management": [],
    "Compliance": ["regulatory"],
    "Recruitment": ["recruiting", "talent acquisition", "sourcing"],
    "Onboarding": [],
    "Employee relations": [],
    "Payroll": [],
    "Supply chain": ["logistics", "procurement"],
    "Operations management": ["operations"],
    "Process improvement": ["Lean", "Six Sigma", "continuous improvement"],
    # Transferable
    "Leadership": ["team lead", "led a team", "managed a team", "people management"],
    "Mentoring": ["coaching", "mentored"],
    "Communication": ["communication skills", "presentation", "presentations"],
    "Problem solving": ["problem-solving", "troubleshooting"],
    "Teamwork": ["collaboration", "cross-functional"],
    "Time management": ["prioritization", "prioritisation"],
    "Analytical skills": ["analytical"],
    "Documentation": ["technical writing"],
}

_PATTERNS = {
    skill: re.compile(
        "|".join(
            rf"(?<![A-Za-z0-9+#.]){re.escape(alias)}(?![A-Za-z0-9+#])"
            for alias in sorted([skill, *aliases], key=len, reverse=True)
        ),
        re.IGNORECASE,
    )
    for skill, aliases in SKILLS.items()
}
# Very short names are matched case-sensitively, and not as part of "C-level"
# or "R&D", to avoid noise from ordinary sentences ("go", "c", "r").
for _skill in ("C", "R", "Go"):
    _PATTERNS[_skill] = re.compile(
        "|".join(
            rf"(?<![A-Za-z0-9+#.\-&]){re.escape(alias)}(?![A-Za-z0-9+#\-&])"
            for alias in sorted([_skill, *SKILLS[_skill]], key=len, reverse=True)
        )
    )

REQUIREMENT_WORDS = re.compile(
    r"\b(required|requirement|must|essential|mandatory|minimum|need|needs|strong|proven|"
    r"expert|proficien\w*|experience (?:with|in))\b",
    re.IGNORECASE,
)
PREFERENCE_WORDS = re.compile(
    r"\b(nice to have|bonus|plus|preferred|desirable|familiarity|exposure)\b", re.IGNORECASE
)


def find_skills(text):
    """Skills from the vocabulary mentioned in `text`, in vocabulary order."""
    text = text or ""
    return [skill for skill, pattern in _PATTERNS.items() if pattern.search(text)]


def skill_count(skill, text):
    return len(_PATTERNS[skill].findall(text or ""))


def evidence_line(skill, text, limit=220):
    """The first line of `text` that mentions `skill`, trimmed for display."""
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line and _PATTERNS[skill].search(line):
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def importance(skill, job_text):
    """high / medium / low from how the job description talks about `skill`."""
    lines = [line for line in (job_text or "").splitlines() if _PATTERNS[skill].search(line)]
    if any(PREFERENCE_WORDS.search(line) for line in lines):
        return "low"
    if skill_count(skill, job_text) >= 2 or any(REQUIREMENT_WORDS.search(line) for line in lines):
        return "high"
    return "medium"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_KEYWORDS = {
    "engineering": [
        "engineer",
        "developer",
        "programmer",
        "software",
        "backend",
        "back-end",
        "frontend",
        "front-end",
        "full stack",
        "full-stack",
        "devops",
        "sre",
        "architect",
        "mobile",
        "android",
        "ios",
        "qa",
        "tester",
        "test",
        "embedded",
        "firmware",
        "cloud",
    ],
    "data": [
        "data",
        "analyst",
        "analytics",
        "scientist",
        "machine learning",
        "ml",
        "ai",
        "bi",
        "business intelligence",
        "statistic",
    ],
    "product": ["product manager", "product owner", "product lead", "pm", "program manager"],
    "design": ["designer", "design", "ux", "ui", "researcher", "creative"],
    "marketing": [
        "marketing",
        "seo",
        "content",
        "brand",
        "growth",
        "social media",
        "communications",
    ],
    "sales": [
        "sales",
        "account executive",
        "business development",
        "account manager",
        "bdr",
        "sdr",
    ],
    "finance": [
        "finance",
        "financial",
        "accountant",
        "accounting",
        "audit",
        "controller",
        "investment",
    ],
    "people": [
        "hr",
        "human resources",
        "recruiter",
        "talent",
        "people partner",
        "people operations",
    ],
    "operations": ["operations", "supply chain", "logistics", "procurement", "project manager"],
    "support": ["support", "customer success", "customer service", "help desk", "service desk"],
}


def role_family(role_title, job_text=""):
    """The best-matching role family for a title (and job description), or "general"."""
    title = f" {(role_title or '').lower()} "
    scores = {}
    for family, words in ROLE_KEYWORDS.items():
        # "Analyst" says little on its own: "Financial analyst" is finance and
        # "Marketing analyst" is marketing, while "Analyst" alone is still data.
        score = sum(
            1 if word == "analyst" else 3
            for word in words
            if re.search(rf"\b{re.escape(word)}\b", title)
        )
        scores[family] = score
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        # "Product designer" is design, "data engineer" is data: the more specific wins.
        for family in ("data", "design", "product"):
            if scores[family] and scores[family] >= scores[best]:
                return family
        return best
    body = (job_text or "").lower()
    if body:
        counts = {
            family: sum(len(re.findall(rf"\b{re.escape(word)}\b", body)) for word in words)
            for family, words in ROLE_KEYWORDS.items()
        }
        best = max(counts, key=counts.get)
        if counts[best] >= 3:
            return best
    return "general"


WORD = re.compile(r"[A-Za-z][A-Za-z+#.'-]*")
STOPWORDS = set(
    """a an and are as at be been but by can could did do does for from had has have how i if in
    into is it its me my of on or our so that the their them then there these they this to was we
    were what when where which who why will with would you your about tell describe give example
    time explain walk us one more most some any all""".split()
)


def keywords(text):
    """Lower-cased content words of `text` (no stopwords, 3+ letters)."""
    return {
        word.lower().strip(".'-")
        for word in WORD.findall(text or "")
        if len(word) >= 3 and word.lower() not in STOPWORDS
    }
