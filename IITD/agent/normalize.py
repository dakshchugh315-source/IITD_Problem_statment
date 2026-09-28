"""Parsers for messy profile fields.

Every function takes whatever the API returned (str, number, None) and returns a clean value,
or None when the field is missing or unreadable. Callers decide what "missing" means.
"""
import re

# ------------------------------------------------------------------ generic field access
def pick(record, *names, default=None):
    """First present, non-empty value among several possible key spellings (case-insensitive)."""
    if not isinstance(record, dict):
        return default
    lower = {str(k).lower(): v for k, v in record.items()}
    for name in names:
        v = lower.get(name.lower())
        if v not in (None, "", [], {}):
            return v
    return default


def _numbers(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", str(text))]


# ------------------------------------------------------------------ skills
SKILL_SYNONYMS = {
    "js": "javascript", "javascript": "javascript", "ecmascript": "javascript", "es6": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "py": "python", "python": "python", "python3": "python",
    "k8s": "kubernetes", "kube": "kubernetes", "kubernetes": "kubernetes",
    "postgres": "postgresql", "postgresql": "postgresql", "psql": "postgresql", "pg": "postgresql",
    "mongo": "mongodb", "mongodb": "mongodb",
    "golang": "go", "go": "go",
    "node": "nodejs", "nodejs": "nodejs", "node.js": "nodejs",
    "react": "react", "reactjs": "react", "react.js": "react",
    "vue": "vue", "vuejs": "vue", "vue.js": "vue",
    "angular": "angular", "angularjs": "angular",
    "ml": "machine learning", "machinelearning": "machine learning", "machine learning": "machine learning",
    "dl": "deep learning", "deep learning": "deep learning",
    "nlp": "nlp", "natural language processing": "nlp",
    "cv": "computer vision", "computer vision": "computer vision",
    "ai": "ai", "artificial intelligence": "ai",
    "aws": "aws", "amazon web services": "aws",
    "gcp": "gcp", "google cloud": "gcp", "google cloud platform": "gcp",
    "azure": "azure", "microsoft azure": "azure",
    "c++": "c++", "cpp": "c++", "cplusplus": "c++",
    "c#": "c#", "csharp": "c#", "c sharp": "c#",
    "dotnet": ".net", ".net": ".net", "net": ".net",
    "sql": "sql", "mysql": "mysql",
    "tf": "tensorflow", "tensorflow": "tensorflow",
    "torch": "pytorch", "pytorch": "pytorch",
    "sklearn": "scikit-learn", "scikit-learn": "scikit-learn", "scikit learn": "scikit-learn",
    "docker": "docker", "containers": "docker",
    "ci/cd": "ci/cd", "cicd": "ci/cd", "ci cd": "ci/cd",
    "rest": "rest", "rest api": "rest", "restful": "rest",
    "spark": "spark", "apache spark": "spark", "pyspark": "spark",
    "k8": "kubernetes", "tf2": "tensorflow",
    "rest apis": "rest apis", "rest api": "rest apis", "restful apis": "rest apis", "rest": "rest apis",
    "restapi": "rest apis", "restapis": "rest apis", "restful": "rest apis",
    "a/b testing": "a/b testing", "ab testing": "a/b testing", "a/b tests": "a/b testing",
    "ab tests": "a/b testing", "experimentation": "a/b testing", "a b testing": "a/b testing",
    "stats": "statistics", "statistics": "statistics",
    "ms excel": "excel", "excel": "excel", "microsoft excel": "excel",
    "html5": "html", "html": "html", "css3": "css", "css": "css",
    "apache kafka": "kafka", "kafka": "kafka", "apache airflow": "airflow", "airflow": "airflow",
    "ml ops": "mlops", "mlops": "mlops", "ml-ops": "mlops",
    "selenium webdriver": "selenium", "selenium": "selenium",
    "github actions": "ci/cd", "jenkins": "jenkins", "terraform": "terraform", "tf": "tf",
    "microservice": "microservices", "microservices": "microservices",
    "gql": "graphql", "graphql": "graphql",
}


def canon_skill(raw):
    s = re.sub(r"\s+", " ", str(raw).strip().lower())
    s = re.sub(r"[^a-z0-9+#./ -]", "", s).strip(" .-")
    if not s:
        return None
    if s in SKILL_SYNONYMS:
        return SKILL_SYNONYMS[s]
    squashed = s.replace(" ", "").replace("-", "")
    return SKILL_SYNONYMS.get(squashed, s)


def skill_set(value):
    """'Python, k8s | Postgres' or ['JS','ReactJS'] -> {'python','kubernetes','postgresql'}."""
    if value is None:
        return set()
    items = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;|\n]", str(value))
    out = set()
    for item in items:
        if isinstance(item, dict):
            item = pick(item, "name", "skill", default="")
        c = canon_skill(item)
        if c:
            out.add(c)
    return out


# ------------------------------------------------------------------ numbers with units
def parse_notice_days(value):
    """'Immediate' -> 0, '45 days' -> 45, '2 months' -> 60, '3 weeks' -> 21, 30 -> 30."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).lower()
    if any(w in text for w in ("immediate", "immediately", "serving", "none", "no notice")):
        return 0.0
    nums = _numbers(text)
    if not nums:
        return None
    n = nums[0]
    if "month" in text or re.search(r"\bmo\b", text):
        return n * 30
    if "week" in text or re.search(r"\bwk", text):
        return n * 7
    return n


def parse_money_rupees(value):
    """CTC to rupees per year. '18 LPA', '18L', '₹18,00,000', '1.8M', '1.2 Cr', 18 -> 1,800,000.

    A bare number below 1000 is read as lakhs, which is how Indian CTCs are usually written.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n, text = float(value), ""
    else:
        text = str(value).lower()
        nums = _numbers(text)
        if not nums:
            return None
        n = nums[0]
    if re.search(r"\bcr|crore", text):
        return n * 1e7
    if re.search(r"lpa|lakh|lac|\bl\b|\d\s*l\b", text):
        return n * 1e5
    if re.search(r"\d\s*m\b|million|\bmn\b", text):
        return n * 1e6
    if re.search(r"\d\s*k\b|thousand", text):
        return n * 1e3
    return n * 1e5 if n < 1000 else n


def parse_score_100(value):
    """Assessment on a 0-100 scale. '71/100' -> 71, '7.1/10' -> 71, '0.71' -> 0.71*100, '71%' -> 71."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = pick(value, "score", "value", "assessment", "verified_score")
        if value is None:
            return None
    if isinstance(value, (int, float)):
        n, denom = float(value), None
    else:
        text = str(value).lower()
        if any(w in text for w in ("n/a", "not taken", "pending", "missing", "none")):
            return None
        m = re.search(r"(\d+\.?\d*)\s*(?:/|out of)\s*(\d+\.?\d*)", text)
        if m:
            n, denom = float(m.group(1)), float(m.group(2))
            return 100.0 * n / denom if denom else None
        nums = _numbers(text)
        if not nums:
            return None
        n, denom = nums[0], None
    if n <= 1.0:
        return n * 100
    if n <= 10.0:
        return n * 10
    return min(n, 100.0)


def parse_years(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).lower()
    nums = _numbers(text)
    if not nums:
        return None
    return nums[0] / 12 if "month" in text and "year" not in text else nums[0]


def norm_name(value):
    """'Dr. Priya  SHARMA' and 'sharma priya' -> 'priya sharma' (sorted tokens, titles dropped)."""
    tokens = re.findall(r"[a-z]+", str(value or "").lower())
    tokens = [t for t in tokens if t not in ("mr", "mrs", "ms", "dr", "prof", "jr", "sr")]
    return " ".join(sorted(tokens))


def norm_role(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
