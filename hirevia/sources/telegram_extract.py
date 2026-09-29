"""Telegram message -> normalized Hirevia Job extraction.

Pure functions over a small ``TgMessage`` dataclass so everything here is
unit-testable with mocked messages and without Telethon or a live account.
Final relevance is NOT decided here - this module only detects *candidate*
job posts and normalizes them; the existing Hirevia filters + AI matcher make
the actual relevance/scoring decisions downstream.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from hirevia.core.dedup import normalize_company, normalize_text, normalize_url
from hirevia.core.filtering import extract_years_experience
from hirevia.core.models import EmploymentType, Job

# ---------------------------------------------------------------------------
# Message container (collector converts real Telethon messages into this)
# ---------------------------------------------------------------------------


@dataclass
class TgMessage:
    channel: str                     # public channel username (no @)
    message_id: int
    date: datetime                   # message date, naive UTC
    text: str
    links: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Job detection (lightweight gate - downstream filters/AI decide relevance)
# ---------------------------------------------------------------------------

JOB_SIGNALS = [
    "hiring", "hiring now", "we are hiring", "job", "jobs", "job opening",
    "vacancy", "vacancies", "opening", "openings", "opportunity",
    "recruitment", "recruiting", "career", "careers", "position",
    "apply now", "apply", "resume", "cv", "internship", "intern",
    "trainee", "fresher", "freshers", "entry level", "walkin", "walk-in",
    "developer", "engineer", "engineering", "software", "programmer",
    "data scientist", "data analyst", "machine learning", "ai engineer",
    "backend", "frontend", "full stack", "python", "java developer",
    "job alert", "job update", "placement",
]

# Signals that almost always accompany a real opening (an actual call to act).
STRONG_JOB_SIGNALS = {
    "hiring", "hiring now", "we are hiring", "job opening", "vacancy",
    "vacancies", "recruitment", "recruiting", "apply", "apply now",
    "walkin", "walk-in", "placement", "openings",
}

# Non-job content markers (courses, study material, events...).
NEGATIVE_SIGNALS = [
    "course", "courses", "free course", "free courses", "online course",
    "tutorial", "tutorials", "roadmap", "roadmaps", "webinar", "webinars",
    "certification", "certifications", "learning", "workshop", "workshops",
    "bootcamp", "masterclass", "master class", "training program",
    "syllabus", "notes pdf", "pdf notes", "ebook", "cheatsheet", "cheat sheet",
]

# Markers that are near-proof the post is content, not an opening.
FATAL_NEGATIVE_SIGNALS = {
    "course", "courses", "free course", "free courses", "online course",
    "tutorial", "tutorials", "roadmap", "roadmaps", "webinar", "webinars",
    "bootcamp", "masterclass", "master class",
}

_JOB_SIGNAL_RES = [re.compile(r"(?<!\w)" + re.escape(s).replace(r"\ ", r"[\s\-]") + r"(?!\w)") for s in JOB_SIGNALS]
_NEGATIVE_SIGNAL_RES = [re.compile(r"(?<!\w)" + re.escape(s).replace(r"\ ", r"[\s\-]") + r"(?!\w)") for s in NEGATIVE_SIGNALS]

FRESHER_SIGNALS = [
    "fresher", "freshers", "fresh graduate", "fresh grad", "fresher friendly",
    "entry level", "entry-level", "0 years", "0+ years", "0-1 years", "0–1 years",
    "0 to 1 years", "0-2 years", "0–2 years", "no experience",
    "internship", "intern", "trainee", "apprentice", "junior",
    "recent graduate", "new batch", "2024 batch", "2025 batch", "2026 batch",
]

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)

# t.me / telegram.me links are forwarding or contact links, never application URLs
_TELEGRAM_HOSTS = {"t.me", "telegram.me"}

_ATS_URL_PATTERNS = [
    r"(boards\.greenhouse\.io|job-boards\.greenhouse\.io|[a-z0-9-]+\.greenhouse\.io)/",
    r"(jobs\.lever\.co|[a-z0-9-]+\.lever\.co)/",
    r"jobs\.ashbyhq\.com/",
    r"careers\.smartrecruiters\.com/",
    r"(apply\.workable\.com|jobs\.workable\.com)/",
    r"(recruiting\.bing\.com|myworkdayjobs\.com|wd[1-5]\.myworkday\.com)/",
    r"(linkedin\.com/jobs|instahyre\.com|cutshort\.io|unstop\.com|internshala\.com|"
    r"wellfound\.com|foundersearch\.in|hirist\.com|naukri\.com)",
    r"[a-z0-9-]+\.teamtailor\.com",
]
_ATS_RES = [re.compile(p, re.IGNORECASE) for p in _ATS_URL_PATTERNS]


def content_risk_signals(text: str) -> List[str]:
    """Negative (non-job content) markers found in the text - stored in
    raw_data for later quality analysis, never used alone to reject."""
    if not text or not text.strip():
        return []
    lower = " " + text.lower() + " "
    found = [sig for sig, rx in zip(NEGATIVE_SIGNALS, _NEGATIVE_SIGNAL_RES) if rx.search(lower)]
    return sorted(set(found))


def looks_like_job(text: str) -> Tuple[bool, List[str]]:
    """Multi-signal gate over word boundaries. Returns (is_candidate, signals).

    Decision rules (a single weak negative never kills a legitimate job):
      * any STRONG job signal (hiring/vacancy/apply/walk-in/...)  -> accept
      * else any FATAL negative (course/roadmap/webinar/tutorial/.) -> reject
      * else >=2 negatives (e.g. "certification" + "learning")      -> reject
      * else any weak positive signal                               -> accept
    """
    if not text or not text.strip():
        return False, []
    lower = " " + text.lower() + " "
    signals: List[str] = []
    has_strong = False
    for signal, rx in zip(JOB_SIGNALS, _JOB_SIGNAL_RES):
        if rx.search(lower):
            signals.append(signal)
            if signal in STRONG_JOB_SIGNALS:
                has_strong = True

    if has_strong:
        return True, sorted(set(signals))

    risks = content_risk_signals(text)
    if any(r in FATAL_NEGATIVE_SIGNALS for r in risks):
        return False, []
    if len(risks) >= 2:
        return False, []
    return len(signals) > 0, sorted(set(signals))


def extract_fresher_signals(text: str) -> List[str]:
    lower = " " + (text or "").lower() + " "
    found = []
    for signal in FRESHER_SIGNALS:
        needle = " " + signal + " "
        if needle in lower:
            found.append(signal)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Link handling / application URL preference
# ---------------------------------------------------------------------------


def extract_links(text: str) -> List[str]:
    """Regex fallback link extraction (collector prefers Telegram entities)."""
    return [m.group(0).rstrip(".,;:!'\"") for m in URL_RE.finditer(text or "")]


def _is_telegram_link(url: str) -> bool:
    host = urlparse(url).netloc.lower().replace("www.", "")
    return host in _TELEGRAM_HOSTS


def rank_application_url(links: List[str]) -> Optional[str]:
    """Pick the best application URL. Actual job/ATS links beat generic links;
    t.me forwarding/contact links are never used as application URLs."""
    candidates = [u for u in links if u and not _is_telegram_link(u)]
    if not candidates:
        return None
    for url in candidates:
        if any(rx.search(url) for rx in _ATS_RES):
            return url
    return candidates[0]


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

_ROLE_WORDS = re.compile(
    r"\b(developer|engineer|intern|internship|analyst|scientist|designer|"
    r"architect|consultant|associate|manager|executive|trainee|devops|"
    r"programmer|dev)\b",
    re.IGNORECASE,
)
_LEADING_NOISE_RE = re.compile(r"^[^\w#.+]+")
_TRAILING_NOISE_RE = re.compile(r"[^\w)%.\]\u0900-\u097F]+$")

# "Role: X", "Job Role: X", "Position: X", "Post Name: X", "Designation - X"
_TITLE_LABEL_RE = re.compile(
    r"^\W*(?:job\s*(?:role|title|posting)?|role|position|post\s*name|designation|"
    r"vacancy|opening|requirement)\s*[:\-]\s*(.{3,90})$",
    re.IGNORECASE | re.MULTILINE,
)
# "Hiring:" / "Urgent Hiring: Backend Developer"
_HIRING_LEAD_RE = re.compile(r"^\W*(?:urgently\s+)?hiring\s*[:\-]\s*(.*)$", re.IGNORECASE)
# "TCS is hiring Data Analysts across India" / "Zoho urgently hiring for SDE-2"
_HIRING_PHRASE_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9&.,'\- ]{2,40}?)\s+(?:is|are)\s+"
    r"(?:urgently\s+|immediately\s+|now\s+)?hiring\s+(?:for\s+|a\s+|an\s+)?([^\n]{3,70})",
    re.IGNORECASE,
)
_TITLE_SPLIT_RE = re.compile(r"\s*(?:\||@|\s[-–]\s| across | in | at | for )\s*")


def _clean_phrase(value: str) -> str:
    value = _LEADING_NOISE_RE.sub("", (value or "").strip()).strip()
    return _TRAILING_NOISE_RE.sub("", value).strip()


def _title_is_noise(title: str) -> bool:
    lower = " " + (title or "").lower() + " "
    return any(rx.search(lower) for sig, rx in zip(NEGATIVE_SIGNALS, _NEGATIVE_SIGNAL_RES)
               if sig in FATAL_NEGATIVE_SIGNALS)


def extract_title(text: str) -> Optional[str]:
    """Best-effort job title representing the actual ROLE.

    Hierarchy: labeled fields (Role:/Position:/Post Name:) > "Hiring: X" >
    "Company is hiring X" > first role-word line > first non-noise line.
    Lines about courses/roadmaps/webinars are never used as titles.
    """
    text = text or ""

    # 1. Labeled role field
    m = _TITLE_LABEL_RE.search(text)
    if m:
        candidate = _clean_phrase(m.group(1))
        if 3 <= len(candidate) <= 100 and not _title_is_noise(candidate):
            return candidate

    lines = [_clean_phrase(l) for l in text.splitlines()]
    lines = [l for l in lines if l and not l.lower().startswith(("http://", "https://", "t.me/"))]

    # 2. "Hiring:" / "Hiring: Backend Developer"
    for line in lines:
        m = _HIRING_LEAD_RE.match(line)
        if m:
            tail = _clean_phrase(m.group(1))
            if len(tail) >= 3 and not _title_is_noise(tail):
                return _TITLE_SPLIT_RE.split(tail)[0].strip() or tail

    # 3. "X is hiring Y" - role follows the phrase
    m = _HIRING_PHRASE_RE.search(text)
    if m:
        role = _clean_phrase(_TITLE_SPLIT_RE.split(m.group(2).strip())[0])
        if 3 <= len(role) <= 90 and not _title_is_noise(role):
            return role

    # 4. First line that mentions a role word (skip course/webinar noise)
    fallback = None
    for line in lines:
        if len(line) < 5 or len(line) > 120 or _title_is_noise(line):
            continue
        if _ROLE_WORDS.search(line):
            return line
        if fallback is None and not line.endswith(":"):
            fallback = line
    return fallback


_GENERIC_DOMAINS = {
    "t.me", "telegram.me", "bit.ly", "tinyurl.com", "linktr.ee", "goo.gl",
    "forms.gle", "docs.google.com", "drive.google.com", "wa.me", "youtube.com",
    "youtu.be", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "facebook.com", "gmail.com", "outlook.com", "yahoo.com", "redd.it",
    "reddit.com", "whatsapp.com", "chat.whatsapp.com", "lnkd.in", "ow.ly",
    "buff.ly", "cutt.ly", "cutt.us", "is.gle", "rebrand.ly", "shorturl.at",
    "rb.gy", "t.ly", "tiny.cc", "dwz.cn", "s.id", "v.du.pw", "telegra.ph",
    "blogspot.com", "medium.com", "quora.com", "linkvertise.com", "join.com",
    "sharechat.com", "ojos.ai", "instagr.am",
}

# Community repost/link-aggregator hosts - their name is NEVER the employer.
_AGGREGATOR_HOSTS = {
    "fhlinks.com", "fhlinks.xyz", "pdlink.net", "pdlink.io", "shortplus.in",
    "apptrickz.com", "jobpula.com", "sarkresultresults", "getjobs.in",
}

# Generic second-level domains that carry no company signal.
_JUNK_SLD = {
    "go", "youtu", "pdlink", "fhlinks", "short", "shorturl", "shortplus",
    "link", "links", "url", "urls", "bit", "tiny", "join", "click", "read",
    "view", "download", "downloads", "share", "get", "apk", "free", "job",
    "jobs", "apply", "careers", "www", "boards", "recruiting", "myworkdayjobs",
    "t", "me", "s", "ly", "gg", "gl", "co", "www2", "m", "app", "play",
}


def _company_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = [p for p in parsed.path.split("/") if p]
    try:
        if host.endswith("greenhouse.io") and path and path[0] != "jobs":
            return path[0].replace("-", " ").title()
        if host == "jobs.ashbyhq.com" and path:
            return path[0].replace("-", " ").title()
        if host == "jobs.lever.co" and path:
            return path[0].replace("-", " ").title()
        if host.endswith(".lever.co"):
            return host.split(".")[0].replace("-", " ").title()
        if host == "careers.smartrecruiters.com" and path:
            return path[0].replace("+", " ").replace("-", " ").title()
        if "workable.com" in host and path:
            return path[0].replace("-", " ").title()
        if host.endswith(".teamtailor.com"):
            return host.split(".")[0].replace("-", " ").title()
    except Exception:
        return None
    if host in _GENERIC_DOMAINS or any(host.endswith(g) for g in _GENERIC_DOMAINS):
        return None
    if host in _AGGREGATOR_HOSTS:
        return None
    sld = host.split(".")[0]
    if sld in _JUNK_SLD or len(sld) < 4:
        return None
    if sld not in ("careers", "jobs", "apply", "www", "boards", "recruiting", "myworkdayjobs"):
        return sld.replace("-", " ").title()
    return None


_COMPANY_LINE_RE = re.compile(
    r"^\s*(?:\W*\s*)?(?:company(?:\s*name)?|org(?:anization|isation)?|firm|employer)"
    r"\s*[:\-]\s*(.{3,80})$",
    re.IGNORECASE | re.MULTILINE,
)

# Words that are never a company when they appear alone in a candidate slot.
_COMPANY_NOISE_WORDS = {
    "unknown", "n/a", "na", "none", "nil", "-", "we", "you", "they", "this",
    "that", "now", "today", "urgent", "urgently", "immediately", "alot",
    "multiple", "various", "several", "positions", "openings", "freshers",
    "company", "organization", "organisation", "the", "and", "for", "at",
}


def _clean_company_candidate(value: str) -> Optional[str]:
    """Validate a free-text company candidate; None if it is noise."""
    value = _clean_phrase(value)
    if not value or len(value) < 3 or len(value) > 60:
        return None
    if value.lower().startswith(("http://", "https://", "t.me", "@")):
        return None
    if _title_is_noise(value):
        return None
    tokens = [t for t in re.split(r"[\s,;/]+", value.lower()) if t]
    if tokens and all(t.strip(".:") in _COMPANY_NOISE_WORDS for t in tokens):
        return None
    # A "company" slot holding a whole sentence is a false extraction.
    if len(tokens) > 6:
        return None
    return value


def extract_company(text: str, best_url: Optional[str]) -> str:
    """Company hierarchy (per Phase 6): explicit Company/Company Name field >
    '<Company> is hiring' pattern > recognized ATS/company URL > Unknown.
    Link-label hosts (Fhlinks/Pdlink/Youtu/Go...) never become companies."""
    match = _COMPANY_LINE_RE.search(text or "")
    if match:
        cleaned = _clean_company_candidate(match.group(1))
        if cleaned:
            return cleaned

    m = _HIRING_PHRASE_RE.search(text or "")
    if m:
        cleaned = _clean_company_candidate(m.group(1))
        if cleaned:
            return cleaned

    from_url = _company_from_url(best_url)
    if from_url:
        return from_url
    return "Unknown"


_CITY_RE = re.compile(
    r"\b(bengaluru|bangalore|mumbai|pune|hyderabad|delhi|ncr|gurgaon|gurugram|"
    r"noida|chennai|kolkata|ahmedabad|jaipur|indore|kochi|coimbatore|chandigarh|"
    r"bhopal|nagpur|vadodara|lucknow)\b",
    re.IGNORECASE,
)
_WORK_MODE_RE = re.compile(r"\b(remote|work from home|wfh|hybrid|onsite|on-site)\b", re.IGNORECASE)
_LOCATION_LINE_RE = re.compile(
    r"^\s*(?:\W*\s*)?(?:location|loc|locations|based)\s*[:\-]\s*(.{3,100})$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_location(text: str) -> str:
    text = text or ""
    match = _LOCATION_LINE_RE.search(text)
    if match:
        return match.group(1).strip().strip("-:* ")
    parts = []
    mode = _WORK_MODE_RE.search(text)
    if mode:
        parts.append(mode.group(1).lower().replace("on-site", "onsite"))
    cities = [m.group(1).title() for m in _CITY_RE.finditer(text)]
    seen = set()
    cities = [c for c in cities if not (c.lower() in seen or seen.add(c.lower()))]
    parts.extend(cities[:3])
    return ", ".join(parts) if parts else "Not specified"


_KNOWN_SKILLS = [
    "python", "fastapi", "flask", "django", "sql", "postgresql", "mysql",
    "mongodb", "redis", "docker", "kubernetes", "aws", "gcp", "azure",
    "machine learning", "deep learning", "tensorflow", "pytorch", "scikit-learn",
    "pandas", "numpy", "selenium", "beautifulsoup", "requests", "asyncio",
    "git", "ci/cd", "rest api", "graphql", "java", "javascript", "typescript",
    "react", "node.js", "nodejs", "go", "rust", "spark", "hadoop",
    "airflow", "kafka", "langchain", "llm", "nlp", "opencv", "power bi", "tableau",
]
_SKILL_RES = {s: re.compile(r"(?<!\w)" + re.escape(s) + r"(?!\w)", re.IGNORECASE) for s in _KNOWN_SKILLS}


def extract_skills(text: str, cap: int = 15) -> List[str]:
    text = text or ""
    found = []
    for skill, rx in _SKILL_RES.items():
        if rx.search(text):
            found.append(skill.title() if len(skill) <= 3 else skill.capitalize())
    return found[:cap]


_EXPERIENCE_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:years?|yrs?)", re.IGNORECASE)


def extract_experience(text: str) -> Optional[str]:
    """Human-readable experience requirement. Missing info stays None -
    never a rejection (the existing filter/AI layers handle that)."""
    text = text or ""
    range_match = _EXPERIENCE_RANGE_RE.search(text)
    if range_match:
        return f"{range_match.group(1)}-{range_match.group(2)} years"
    years = extract_years_experience(text)
    if years is not None:
        return "0 years" if years == 0 else f"{years}+ years"
    if extract_fresher_signals(text):
        return "Fresher / Entry level"
    return None


_EMPLOYMENT_PATTERNS = [
    (EmploymentType.INTERNSHIP, re.compile(r"\b(internship|intern\b|trainee|stipend)\b", re.IGNORECASE)),
    (EmploymentType.PART_TIME, re.compile(r"\bpart[- ]time\b", re.IGNORECASE)),
    (EmploymentType.CONTRACT, re.compile(r"\b(contract|freelance|gig)\b", re.IGNORECASE)),
    (EmploymentType.FULL_TIME, re.compile(r"\bfull[- ]time\b", re.IGNORECASE)),
]


def extract_employment_type(text: str) -> Optional[EmploymentType]:
    text = text or ""
    for emp_type, rx in _EMPLOYMENT_PATTERNS:
        if rx.search(text):
            return emp_type
    return None


_SALARY_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s?\d+(?:[.,]\d+)?\s*(?:[-–to]+\s*(?:₹|rs\.?|inr)?\s?\d+(?:[.,]\d+)?)?\s*"
    r"(?:lpa|lakhs?|lakh|lac|kpa|k/month|/month|k per month|per month|p\.?m\.?|per annum|pa\b)",
    re.IGNORECASE,
)
_STIPEND_RE = re.compile(r"stipend[^\n]{0,40}", re.IGNORECASE)


def extract_salary(text: str) -> Optional[str]:
    text = text or ""
    match = _SALARY_RE.search(text)
    if match:
        return " ".join(match.group(0).split())
    match = _STIPEND_RE.search(text)
    if match:
        return " ".join(match.group(0).split()).rstrip(".,;")
    return None


def freshness_label(posted_at: datetime, now: Optional[datetime] = None) -> str:
    """Telegram-style freshness ladder used for logging/inspection."""
    if now is None:
        now = datetime.utcnow()
    if posted_at.tzinfo is not None:
        posted_at = posted_at.astimezone(timezone.utc).replace(tzinfo=None)
    age_hours = (now - posted_at).total_seconds() / 3600
    if age_hours < 1:
        return "very_fresh"
    if age_hours < 6:
        return "fresh"
    if age_hours < 24:
        return "recent"
    if age_hours <= 72:
        return "older"
    return "low_freshness"


def content_stable_id(application_url: Optional[str], company: str, title: str, location: str) -> str:
    """Stable ID for a *job*, not for a message: reposts of the same opening
    (same application URL, or company+title+location) map to the same ID, so
    the existing Hirevia dedup (source+source_job_id / fingerprint) absorbs
    repeated Telegram posts without any core changes."""
    if application_url:
        key = f"app:{normalize_url(application_url)}"
    else:
        key = f"ctl:{normalize_company(company)}|{normalize_text(title)}|{normalize_text(location)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def message_to_job(msg: TgMessage) -> Optional[Job]:
    """Convert one Telegram message into a normalized Job, or None if the
    message is not a job candidate / cannot be normalized."""
    text = (msg.text or "").strip()
    if not text:
        return None

    is_candidate, signals = looks_like_job(text)
    if not is_candidate:
        return None

    links = list(msg.links) if msg.links else []
    if not links:
        links = extract_links(text)
    application_url = rank_application_url(links)
    message_url = f"https://t.me/{msg.channel}/{msg.message_id}"

    title = extract_title(text)
    if not title:
        return None

    company = extract_company(text, application_url)
    location = extract_location(text)
    posted_at = _to_naive_utc(msg.date)

    try:
        job = Job(
            source="telegram",
            source_job_id=content_stable_id(application_url, company, title, location),
            title=title,
            company=company,
            location=location,
            description=text[:4000],
            url=message_url,
            application_url=application_url,
            posted_at=posted_at,
            employment_type=extract_employment_type(text),
            experience=extract_experience(text),
            skills=extract_skills(text),
            raw_data={
                "source_type": "community",
                "source_channel": msg.channel,
                "source_channels": [msg.channel],
                "source_message_id": msg.message_id,
                "source_message_url": message_url,
                "message_date": posted_at.isoformat(),
                "freshness_label": freshness_label(posted_at),
                "detection_signals": signals,
                "content_risk": content_risk_signals(text),
                "fresher_signals": extract_fresher_signals(text),
                "salary": extract_salary(text),
                "links": links[:10],
            },
        )
    except Exception:
        # Malformed content pydantic rejects (e.g. invalid URLs) - skip quietly.
        return None
    return job


def messages_to_jobs(messages: List[TgMessage]) -> List[Job]:
    jobs = []
    for msg in messages:
        job = message_to_job(msg)
        if job:
            jobs.append(job)
    return jobs



