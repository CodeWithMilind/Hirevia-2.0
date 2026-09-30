"""Skill normalisation and matching (Phase 7).

Two postings describe the same skill differently ("ML", "Machine Learning",
"scikit-learn models"), so every skill is normalised to one canonical spelling
before it is compared, counted or shown to the user.

Rules that keep scores honest:
  * matching is whole-word, so "R" never matches "React" and "Java" never
    matches "JavaScript";
  * aliases map equivalent spellings ONLY (ML == Machine Learning);
  * related-but-different skills (FastAPI vs Django) count as transferable
    evidence at a discount - they are never reported as a match.
"""

import re
from typing import Dict, Iterable, List, Sequence, Set, Tuple

# canonical name -> spellings that mean exactly the same thing
SKILL_ALIASES: Dict[str, List[str]] = {
    "Python": ["python", "python3", "python 3", "python programming", "core python"],
    "FastAPI": ["fastapi", "fast api", "fast api framework"],
    "Flask": ["flask", "flask framework"],
    "Django": ["django", "django framework", "django rest framework", "drf"],
    "REST API": ["rest api", "rest APIs", "restful", "restful api", "rest", "web services", "api development"],
    "GraphQL": ["graphql", "graph ql"],
    "SQL": ["sql", "sql queries", "query writing"],
    "PostgreSQL": ["postgresql", "postgres", "postgre sql"],
    "MySQL": ["mysql", "my sql"],
    "MongoDB": ["mongodb", "mongo db", "mongo"],
    "Redis": ["redis"],
    "SQLite": ["sqlite"],
    "Docker": ["docker", "containerisation", "containerization", "containers"],
    "Kubernetes": ["kubernetes", "k8s"],
    "AWS": ["aws", "amazon web services"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Azure": ["azure", "microsoft azure"],
    "Linux": ["linux", "ubuntu", "unix"],
    "Bash": ["bash", "shell scripting", "shell", "zsh"],
    "Git": ["git", "github", "gitlab", "version control"],
    "CI/CD": ["ci/cd", "ci cd", "continuous integration", "continuous delivery", "continuous deployment"],
    "GitHub Actions": ["github actions", "gh actions"],
    "Jenkins": ["jenkins"],
    "Machine Learning": ["machine learning", "ml", "ml models", "predictive modelling", "predictive modeling"],
    "Deep Learning": ["deep learning", "dl", "neural networks"],
    "TensorFlow": ["tensorflow", "tf"],
    "PyTorch": ["pytorch", "torch"],
    "Scikit-learn": ["scikit-learn", "scikit learn", "sklearn"],
    "Pandas": ["pandas", "pandas dataframe", "dataframes"],
    "NumPy": ["numpy", "np"],
    "Matplotlib": ["matplotlib", "plotting"],
    "Seaborn": ["seaborn"],
    "Natural Language Processing": ["natural language processing", "nlp", "text processing", "text mining"],
    "Large Language Models": ["large language models", "llm", "llms", "genai", "gen ai", "generative ai", "gpt",
                              "chatgpt", "openai api", "prompt engineering"],
    "RAG": ["rag", "retrieval augmented generation", "retrieval-augmented generation"],
    "Vector Database": ["vector db", "vector database", "vector search", "pinecone", "faiss", "weaviate", "chroma"],
    "Embeddings": ["embeddings", "sentence embeddings", "word embeddings"],
    "LangChain": ["langchain", "llamaindex", "llama index", "crewai"],
    "Computer Vision": ["computer vision", "opencv", "image processing"],
    "Statistics": ["statistics", "statistical analysis"],
    "Probability": ["probability", "probability theory", "bayesian"],
    "Data Analysis": ["data analysis", "data analytics", "exploratory data analysis", "eda"],
    "Data Visualization": ["data visualization", "data visualisation", "data viz"],
    "Power BI": ["power bi", "powerbi"],
    "Tableau": ["tableau"],
    "Excel": ["excel", "ms excel", "google sheets"],
    "Web Scraping": ["web scraping", "scraping"],
    "Selenium": ["selenium", "selenium webdriver", "webdriver"],
    "BeautifulSoup": ["beautifulsoup", "beautiful soup", "bs4"],
    "Playwright": ["playwright"],
    "Requests": ["requests library", "requests", "http requests", "requests-based"],
    "httpx": ["httpx", "http x"],
    "aiohttp": ["aiohttp"],
    "AsyncIO": ["asyncio", "async python", "async programming", "asynchronous programming"],
    "Testing": ["testing", "unit testing", "test automation", "test cases", "qa testing"],
    "pytest": ["pytest"],
    "Apache Spark": ["apache spark", "pyspark"],
    "Airflow": ["airflow", "apache airflow"],
    "Kafka": ["kafka", "apache kafka", "event streaming"],
    "JavaScript": ["javascript", "js", "es6", "ecmascript"],
    "TypeScript": ["typescript", "ts"],
    "Node.js": ["node.js", "nodejs", "node js"],
    "React": ["react", "react.js", "reactjs"],
    "Next.js": ["next.js", "nextjs"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3", "tailwind", "bootstrap"],
    "Java": ["java", "core java"],
    "Spring Boot": ["spring boot", "springboot"],
    "C++": ["c++", "cpp"],
    "Go": ["golang"],
    "Rust": ["rust"],
    "C#": ["c#", "csharp", "c sharp"],
    ".NET": [".net", "dotnet", "asp.net", "aspnet"],
    "PHP": ["php"],
    "Laravel": ["laravel"],
    "MERN": ["mern", "mern stack"],
    "MEAN": ["mean", "mean stack"],
    "Microservices": ["microservices", "micro services"],
    "RabbitMQ": ["rabbitmq", "celery", "message queue", "message queues"],
    "Nginx": ["nginx"],
    "Terraform": ["terraform", "infrastructure as code"],
    "Streamlit": ["streamlit", "gradio"],
    "Jupyter": ["jupyter", "jupyter notebook", "google colab", "colab"],
    "ETL": ["etl", "data pipeline", "data pipelines", "data ingestion"],
    "Communication": ["communication skills", "presentation skills", "soft skills"],
}

# Groups used ONLY for transferable credit, never for equality.
SKILL_FAMILIES: Dict[str, Set[str]] = {
    "backend_framework": {"FastAPI", "Flask", "Django", "Spring Boot", "Node.js", "PHP", "MERN"},
    "web_technology": {"REST API", "GraphQL", "Microservices", "Web Scraping", "Requests", "AsyncIO"},
    "http_client": {"Requests", "httpx", "aiohttp"},
    "web_scraping": {"Web Scraping", "Selenium", "BeautifulSoup", "Playwright"},
    "test_tooling": {"Testing", "pytest"},
    "ci_cd_tooling": {"CI/CD", "GitHub Actions", "Jenkins"},
    "ml_framework": {"TensorFlow", "PyTorch", "Scikit-learn", "LangChain"},
    "data_science": {"Machine Learning", "Deep Learning", "Statistics", "Probability", "Data Analysis",
                     "Computer Vision", "Natural Language Processing", "Large Language Models"},
    "rag_stack": {"RAG", "Vector Database", "Embeddings"},
    "bi_tooling": {"Data Visualization", "Power BI", "Tableau"},
    "dotnet_ecosystem": {"C#", ".NET", "Java"},
    "php_ecosystem": {"PHP", "Laravel"},
    "js_stack": {"JavaScript", "TypeScript", "React", "Next.js", "Node.js", "MERN", "MEAN"},
    "data_tooling": {"Pandas", "NumPy", "Matplotlib", "Seaborn", "Jupyter", "Excel", "Apache Spark", "Airflow", "ETL",
                    "Data Visualization", "Power BI", "Tableau"},
    "database": {"SQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite"},
    "cloud": {"AWS", "GCP", "Azure"},
    "devops": {"Docker", "Kubernetes", "CI/CD", "Terraform", "Linux", "Bash", "Nginx", "Git"},
    "messaging": {"RabbitMQ", "Kafka", "Redis"},
    "frontend": {"React", "Next.js", "HTML", "CSS", "JavaScript", "TypeScript"},
}

_ALIAS_TO_CANONICAL: Dict[str, str] = {}
_COMPACT_INDEX: Dict[str, str] = {}
for _canonical, _aliases in SKILL_ALIASES.items():
    for _alias in [_canonical, *_aliases]:
        _key = _alias.strip().casefold()
        _ALIAS_TO_CANONICAL.setdefault(_key, _canonical)
        _COMPACT_INDEX.setdefault(re.sub(r"[\s\-_./]+", "", _key), _canonical)

# A skill may legitimately belong to several families (Requests is both an HTTP
# client and a web technology; PHP is both a backend framework and part of the
# PHP ecosystem). The first family used to win outright, which silently
# disconnected the second group's members - "httpx ~ Requests" stopped being
# reported as transferable. Membership is now a set and any shared family
# counts.
_FAMILIES_OF: Dict[str, Set[str]] = {}
for _family, _members in SKILL_FAMILIES.items():
    for _member in _members:
        _FAMILIES_OF.setdefault(_member, set()).add(_family)


def normalize_skill(skill: str) -> str:
    """Map any spelling onto its canonical name; unknown skills pass through."""
    text = str(skill or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if lowered in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[lowered]
    compact = re.sub(r"[\s\-_./]+", "", lowered)
    if compact in _COMPACT_INDEX:
        return _COMPACT_INDEX[compact]
    return text


def canonical_set(skills: Iterable[str]) -> Set[str]:
    return {s for s in (normalize_skill(skill) for skill in skills or []) if s}


def skill_variants(skill: str) -> List[str]:
    """Every spelling of one canonical skill, longest first so alternation
    prefers "machine learning" over "ml" when both could match."""
    canonical = normalize_skill(skill)
    spellings = [canonical, *SKILL_ALIASES.get(canonical, [])]
    return sorted({s.strip().lower() for s in spellings if s and s.strip()}, key=len, reverse=True)

# --------------------------------------------------------------------------
# Whole-word detection
# --------------------------------------------------------------------------

_TOKEN_BEFORE = r"(?<![A-Za-z0-9+#.])"
_TOKEN_AFTER = r"(?![A-Za-z0-9+#])"

_PATTERN_CACHE: Dict[str, "re.Pattern"] = {}


def _pattern(skill: str) -> "re.Pattern":
    """Compiled whole-word matcher covering every spelling of one skill.

    Boundaries use lookarounds instead of \\b because skill names contain
    punctuation (C++, C#, .NET, Node.js, CI/CD) that \\b gets wrong.
    """
    canonical = normalize_skill(skill)
    cached = _PATTERN_CACHE.get(canonical)
    if cached is not None:
        return cached

    terms = [re.escape(term) for term in skill_variants(canonical)] or [re.escape(canonical.lower())]
    compiled = re.compile(
        _TOKEN_BEFORE + "(?:" + "|".join(terms) + ")" + _TOKEN_AFTER,
        re.IGNORECASE,
    )
    _PATTERN_CACHE[canonical] = compiled
    return compiled


def skill_in_text(skill: str, text: str) -> bool:
    """True when the skill (or any alias of it) appears as its own word."""
    if not text or not normalize_skill(skill):
        return False
    return bool(_pattern(skill).search(text))


def find_skills(text: str, candidates: Sequence[str], cap: int = 30) -> List[str]:
    """Which of `candidates` appear in `text` - canonical, deduped, stable order."""
    found, seen = [], set()
    for candidate in candidates or []:
        canonical = normalize_skill(candidate)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        if skill_in_text(canonical, text):
            found.append(canonical)
            if len(found) >= cap:
                break
    return found


# --------------------------------------------------------------------------
# Relationships between skills
# --------------------------------------------------------------------------

def same_skill(a: str, b: str) -> bool:
    return bool(normalize_skill(a)) and normalize_skill(a) == normalize_skill(b)


def related_skill(a: str, b: str) -> bool:
    """Different skill, shared family - transferable evidence, never a match.

    Sharing ANY family is enough: a skill sitting in two groups (Requests is
    both an HTTP client and a web technology) is related to both.
    """
    first, second = normalize_skill(a), normalize_skill(b)
    if not first or not second or first == second:
        return False
    return bool(_FAMILIES_OF.get(first, set()) & _FAMILIES_OF.get(second, set()))


def transferable_matches(missing: Sequence[str], present: Sequence[str]) -> List[Tuple[str, str]]:
    """(what the job wants, what the candidate has instead) adjacency pairs."""
    pairs: List[Tuple[str, str]] = []
    for wanted in missing or []:
        for offered in present or []:
            if related_skill(offered, wanted):
                pairs.append((normalize_skill(wanted), normalize_skill(offered)))
    return pairs


def skill_overlap(job_skills: Sequence[str], profile_skills: Sequence[str]) -> float:
    """Fraction (0..1) of the profile skills that the job asks for."""
    wanted = canonical_set(profile_skills)
    if not wanted:
        return 0.0
    offered = canonical_set(job_skills)
    return len(wanted & offered) / len(wanted)
