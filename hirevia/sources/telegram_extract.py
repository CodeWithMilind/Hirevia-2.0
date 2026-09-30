"""Telegram message -> normalized Hirevia Job extraction.

Pure functions over a small ``TgMessage`` dataclass so everything here is
unit-testable with mocked messages and without Telethon or a live account.
Final relevance is NOT decided here - this module only detects *candidate*
job posts and normalizes them; the existing Hirevia filters + AI matcher make
the actual relevance/scoring decisions downstream.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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


class IntentCategory(str, Enum):
    JOB = "job"
    INTERNSHIP = "internship"
    HIRING_DRIVE = "hiring_drive"
    APPRENTICESHIP = "apprenticeship"
    PROMOTIONAL = "promotional"
    EDUCATIONAL = "educational"
    INFORMATIONAL = "informational"
    SPAM = "spam"
    SELECTION_ANNOUNCEMENT = "selection_announcement"
    AGGREGATOR_LIST = "aggregator_list"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentDecision:
    category: IntentCategory
    accepted: bool
    reason: str
    signals: Tuple[str, ...] = ()
    manual_application_action: bool = False


_HIRING_INTENT_RE = re.compile(
    r"\b(?:hiring|recruiting|recruitment|vacanc(?:y|ies)|job\s+openings?|"
    r"open\s+positions?|looking\s+for\s+(?:candidates|applicants|engineers|developers))\b",
    re.IGNORECASE,
)
_DRIVE_INTENT_RE = re.compile(
    r"\b(?:(?:off[\s-]*campus|campus|hiring|recruitment)\s+drive|"
    r"walk[\s-]*in(?:\s+interview)?)\b",
    re.IGNORECASE,
)
_ROLE_INTENT_RE = re.compile(
    r"\b(?:developers?|engineers?|scientists?|analysts?|programmers?|intern(?:ship)?s?|"
    r"apprentice(?:ship)?s?|trainees?|designers?|consultants?|associates?|"
    r"software\s+roles?|data\s+roles?)\b",
    re.IGNORECASE,
)
_APPLICATION_ACTION_RE = re.compile(
    r"\b(?:apply(?:\s+now|\s+here)?|application\s+link|registrations?(?:\s+open)?|register(?:\s+now|\s+here|\s+using)?|"
    r"submit\s+(?:your\s+)?resume|send\s+(?:your\s+)?(?:resume|cv)|"
    r"walk[\s-]*in|interview\s+schedule)\b",
    re.IGNORECASE,
)
_DIRECT_APPLICATION_ACTION_RE = re.compile(
    r"\b(?:apply(?:\s+now|\s+here)?|application\s+link|submit\s+(?:your\s+)?resume|"
    r"send\s+(?:your\s+)?(?:resume|cv)|walk[\s-]*in|interview\s+schedule)\b",
    re.IGNORECASE,
)
_PROMOTION_RE = re.compile(
    r"\b(?:courses?|training\s+programs?|bootcamps?|coaching|career\s+counsel(?:ling|ing)|"
    r"mentorship|placement\s+assistance|enroll|admission|limited\s+seats?|"
    r"fees?|coupon|discount|subscription|buy\s+now|paid\s+programs?)\b",
    re.IGNORECASE,
)
_EDUCATIONAL_RE = re.compile(
    r"\b(?:certifi(?:ed|cations?)|webinars?|workshops?|master\s*classes?|"
    r"learn\s+\w+(?:\s+\w+)?|learning\s+paths?|get\s+job[ -]ready|learning\s+playlists?|"
    r"from\s+beginner\s+to\s+job[ -]ready|master\s+.{1,60}(?:playlist|learning|coding)|"
    r"beginner\s+to\s+(?:ai[ -]powered|job[ -]ready))\b",
    re.IGNORECASE,
)
_INFORMATIONAL_RE = re.compile(
    r"\b(?:career\s+tips?|career\s+advice|job\s+news|hiring\s+news|"
    r"interview\s+(?:tips?|questions?|preparation)|soft\s+skills\s+questions?|how\s+to\s+prepare|"
    r"roadmaps?|tutorials?|real[\s-]*world\s+.*\bexamples?|"
    r"(?:news|article|explained|definition|statistics|dataset))\b",
    re.IGNORECASE,
)
_SPAM_RE = re.compile(
    r"\b(?:subscribe\s+to\s+(?:our|my)\s+(?:(?:telegram|whatsapp)\s+)?(?:channel|group)|follow\s+our\s+channel|"
    r"join\s+(?:our\s+)?(?:telegram\s+)?(?:channel|community|group)|"
    r"boost\s+your\s+resume|resume[ -]writing\s+(?:service|services)|"
    r"professional\s+resume\s+service)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_OPPORTUNITY_RE = re.compile(
    r"\b(?:interview\s+opportunity|training\s+\+\s*interview)\b",
    re.IGNORECASE,
)
_SELECTION_ANNOUNCEMENT_RE = re.compile(
    r"\b(?:selected\s*(?:!|\.|,|$)|congratulations?\b.{0,100}\bselected\b|"
    r"\bselected\b.{0,70}\bcongratulations?\b|offer\s+(?:letter\s+)?released|"
    r"\b\d+\s+(?:students?|candidates?)\s+selected|"
    r"congratulations?\b.{0,80}\b(?:placement|placed|offer)\b)",
    re.IGNORECASE,
)
_AGGREGATOR_LIST_RE = re.compile(
    r"\b(?:\d+\+?\s+job\s+portals?|top\s+\d*\s*job\s+(?:portals?|websites?)|"
    r"(?:one\s+)?curated\s+list\s+of\s+jobs?|100\s+companies\s+hiring|"
    r"fresh\s+jobs\s+every\s+day|job\s+portals?\s*\.\s*one\s+curated\s+list)\b",
    re.IGNORECASE,
)
_COMMENT_GATE_RE = re.compile(
    r"\bcomment\b.{0,100}\b(?:get|receive|dm|link|yes)\b|"
    r"\bdm\s+(?:me|us)\b.{0,70}\blink\b|\blike\s+and\s+share\b|"
    r"\bjoin\s+(?:this|our)\s+group\b.{0,60}\blink\b",
    re.IGNORECASE,
)


def _canonicalize_text(text: str) -> str:
    """Normalize styled Unicode and invisible separators used in channel posts."""
    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", normalized)


def classify_job_intent(text: str, links: Optional[List[str]] = None) -> IntentDecision:
    """Classify a Telegram post by primary intent and evidence of an opening.

    Promotional words are contextual signals, never standalone vetoes. A
    concrete hiring/role/application combination takes precedence over them.
    """
    text = _canonicalize_text(text).strip()
    if not text:
        return IntentDecision(IntentCategory.INFORMATIONAL, False, "empty message")

    lower = text.lower()
    job_signals = tuple(sorted({
        signal for signal, rx in zip(JOB_SIGNALS, _JOB_SIGNAL_RES)
        if rx.search(" " + lower + " ")
    }))
    has_hiring = bool(_HIRING_INTENT_RE.search(text))
    has_drive = bool(_DRIVE_INTENT_RE.search(text))
    has_role = bool(_ROLE_INTENT_RE.search(text))
    has_action = bool(_APPLICATION_ACTION_RE.search(text))
    has_direct_action = bool(_DIRECT_APPLICATION_ACTION_RE.search(text))
    has_fresher = bool(re.search(r"\b(?:fresher|freshers|graduate hiring)\b", text, re.I))
    has_application_link = any(_is_application_link(link) for link in (links or []))
    has_concrete_opening = (
        (has_hiring and (has_role or has_fresher or has_action))
        or (has_drive and (has_role or has_action or has_fresher))
        or (has_role and (has_direct_action or has_application_link))
        or (has_role and has_hiring)
    )

    signals = list(job_signals)
    if has_hiring:
        signals.append("explicit hiring")
    if has_role:
        signals.append("job role")
    if has_action:
        signals.append("application action")
    if has_application_link:
        signals.append("application link")

    if has_concrete_opening:
        manual_action = bool(
            re.search(r"\bwalk[\s-]*in\b", text, re.I)
            and re.search(r"\b(?:venue|address|location|date|time|bring\s+(?:your\s+)?resume)\b", text, re.I)
        )
        if has_drive:
            category = IntentCategory.HIRING_DRIVE
        elif re.search(r"\b(?:apprentice|apprenticeship)\b", text, re.I):
            category = IntentCategory.APPRENTICESHIP
        elif re.search(r"\b(?:intern|internship)\b", text, re.I):
            category = IntentCategory.INTERNSHIP
        else:
            category = IntentCategory.JOB
        return IntentDecision(
            category, True, "explicit hiring/role evidence with an application path",
            tuple(sorted(set(signals))), manual_action,
        )

    if _AMBIGUOUS_OPPORTUNITY_RE.search(text):
        return IntentDecision(
            IntentCategory.AMBIGUOUS, True,
            "possible interview opportunity, but no specific opening is stated",
            tuple(sorted(set(signals + ["ambiguous opportunity"]))),
        )

    if _SELECTION_ANNOUNCEMENT_RE.search(text):
        return IntentDecision(
            IntentCategory.SELECTION_ANNOUNCEMENT, False,
            "selection or offer announcement; no separate open opportunity",
        )
    if _AGGREGATOR_LIST_RE.search(text):
        return IntentDecision(
            IntentCategory.AGGREGATOR_LIST, False,
            "curated job portal/list content; no individual actionable opening",
        )
    if _SPAM_RE.search(text):
        return IntentDecision(IntentCategory.SPAM, False, "channel/community or service promotion")
    if _PROMOTION_RE.search(text):
        return IntentDecision(IntentCategory.PROMOTIONAL, False, "course/training or paid-program promotion")
    if _EDUCATIONAL_RE.search(text):
        return IntentDecision(IntentCategory.EDUCATIONAL, False, "educational content; no actionable opening")
    if _INFORMATIONAL_RE.search(text):
        return IntentDecision(IntentCategory.INFORMATIONAL, False, "informational content; no actionable opening")
    if _COMMENT_GATE_RE.search(text):
        return IntentDecision(
            IntentCategory.AMBIGUOUS, True,
            "application link is gated behind comments, DMs, or group joining",
            tuple(sorted(set(signals + ["gated application link"]))),
        )

    if has_hiring or has_drive or has_role or any(word in lower for word in ("job", "career", "intern")):
        return IntentDecision(
            IntentCategory.AMBIGUOUS, True,
            "job-related wording without enough evidence to confirm an opening",
            tuple(sorted(set(signals + ["ambiguous job intent"]))),
        )
    return IntentDecision(IntentCategory.INFORMATIONAL, False, "no actionable job opportunity")

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
    "freshers / experienced", "freshers & experienced", "freshers and experienced",
    "experienced/freshers", "entry level", "entry-level", "graduate",
    "recent graduate", "0 years", "0+ years", "0-1 years", "0–1 years",
    "0 to 1 years", "0-2 years", "0–2 years", "no experience",
    "no experience required", "no prior experience required",
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
    """Compatibility wrapper for the context-based intent classifier."""
    decision = classify_job_intent(text)
    return decision.accepted, list(decision.signals)


def extract_fresher_signals(text: str) -> List[str]:
    lines = _metadata_lines(text)
    found = []
    for signal in FRESHER_SIGNALS:
        pattern = re.escape(signal).replace(r"\ ", r"\s+")
        if any(re.search(r"(?<!\w)" + pattern + r"(?!\w)", line, re.IGNORECASE) for line in lines):
            found.append(signal)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Link handling / application URL preference
# ---------------------------------------------------------------------------


def extract_links(text: str) -> List[str]:
    """Regex fallback link extraction (collector prefers Telegram entities)."""
    return [m.group(0).rstrip(".,;:!'\"") for m in URL_RE.finditer(text or "")]


def _is_application_link(url: Optional[str]) -> bool:
    """Accept recognized job platforms or URLs whose path signals application intent."""
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host or host in _TELEGRAM_HOSTS:
        return False
    if any(pattern.search(url) for pattern in _ATS_RES):
        return True
    if host.startswith(("jobs.", "careers.", "apply.")):
        return True
    return any(
        token in parsed.path.lower()
        for token in ("/job", "/career", "/apply", "/opening", "/vacanc", "/posting", "/position")
    )


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
    r"\b(navi\s+mumbai|bengaluru|bangalore|mumbai|pune|hyderabad|delhi|ncr|gurgaon|gurugram|"
    r"noida|chennai|kolkata|ahmedabad|jaipur|indore|kochi|coimbatore|chandigarh|"
    r"bhopal|nagpur|vadodara|lucknow)\b",
    re.IGNORECASE,
)
_WORK_MODE_RE = re.compile(
    r"\b(work\s+from\s+home|wfh|remote(?:\s+work)?|work\s+from\s+office|wfo|"
    r"on[ -]?site|onsite|hybrid)\b",
    re.IGNORECASE,
)
_LOCATION_LINE_RE = re.compile(
    r"^\s*(?:\W*\s*)?(?:job\s+)?(?:location|loc|locations|based)\s*[:\-]\s*(.{3,100})$",
    re.IGNORECASE | re.MULTILINE,
)

_METADATA_NOISE_RE = re.compile(
    r"\b(?:course|bootcamp|testimonial|review|subscribe|follow\b|"
    r"advertisement|sponsored|promotion|promotional|instruction\w*|comment\w*|"
    r"dm\s+(?:me|us))\b",
    re.IGNORECASE,
)


def _metadata_lines(text: str) -> List[str]:
    """Keep job-content lines and ignore links or obvious unrelated promotion."""
    return [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip()
        and not URL_RE.search(line)
        and not re.search(r"(?:^|\s)@[A-Za-z0-9_]{3,}", line)
        and not _METADATA_NOISE_RE.search(line)
    ]


def extract_location(text: str) -> str:
    text = text or ""
    text = "\n".join(_metadata_lines(text))
    match = _LOCATION_LINE_RE.search(text)
    if match:
        return match.group(1).strip().strip("-:* ")
    parts = []
    mode = extract_work_mode(text)
    if mode:
        parts.append(mode.lower())
    cities = [m.group(1).title() for m in _CITY_RE.finditer(text)]
    seen = set()
    cities = [c for c in cities if not (c.lower() in seen or seen.add(c.lower()))]
    parts.extend(cities[:3])
    return ", ".join(parts) if parts else "Not specified"


def extract_work_mode(text: str) -> Optional[str]:
    """Normalize an explicitly stated work arrangement; silence stays unknown."""
    for line in _metadata_lines(text):
        match = _WORK_MODE_RE.search(line)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1).lower())
        if value in {"wfh", "work from home", "remote", "remote work"}:
            return "Remote"
        if value in {"wfo", "work from office", "on-site", "onsite", "on site"}:
            return "On-site"
        return "Hybrid"
    return None


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
_EXPERIENCE_LABEL_RE = re.compile(
    r"^\s*(?:experience(?:\s+required)?|eligibility|candidate\s+eligibility|"
    r"job\s*type|jobtype|experience\s+level)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_MULTI_ELIGIBILITY_RE = re.compile(
    r"\b(?:freshers?|experienced)\b\s*(?:/|&|and)\s*\b(?:freshers?|experienced)\b",
    re.IGNORECASE,
)
_EXPERIENCE_PHRASE_RE = re.compile(
    r"\b(?:no\s+(?:prior\s+)?experience\s+required|entry[ -]level|recent\s+graduate|"
    r"graduate|freshers?|\d{1,2}\s*(?:-|–|to)\s*\d{1,2}\s*(?:years?|yrs?)|"
    r"\d{1,2}\s*(?:years?|yrs?))\b",
    re.IGNORECASE,
)


def _experience_value(line: str) -> Optional[str]:
    multi = _MULTI_ELIGIBILITY_RE.search(line)
    if multi:
        return "Freshers / Experienced"
    range_match = _EXPERIENCE_RANGE_RE.search(line)
    if range_match:
        return f"{range_match.group(1)}-{range_match.group(2)} years"
    phrase = _EXPERIENCE_PHRASE_RE.search(line)
    if not phrase:
        return None
    value = phrase.group(0).lower()
    if re.search(r"\bfresher[- ]friendly\b", line, re.IGNORECASE):
        return "Fresher / Entry level"
    if "no " in value:
        return "No Experience Required"
    if "entry" in value:
        return "Entry Level"
    if "recent" in value:
        return "Recent Graduate"
    if value == "graduate":
        return "Graduate"
    if value.startswith("fresh"):
        return "Freshers"
    if re.fullmatch(r"\d{1,2}\s*(?:years?|yrs?)", value):
        return f"{int(re.search(r'\d+', value).group())} years"
    return None


def extract_experience(text: str) -> Optional[str]:
    """Human-readable experience requirement. Missing info stays None -
    never a rejection (the existing filter/AI layers handle that)."""
    for line in _metadata_lines(text):
        label = _EXPERIENCE_LABEL_RE.match(line)
        candidate = label.group(1) if label else line
        value = _experience_value(candidate)
        if value:
            return value
    years = extract_years_experience(text or "")
    if years is not None:
        return f"{years} years" if years == 0 else f"{years}+ years"
    return None


_EMPLOYMENT_PATTERNS = [
    (EmploymentType.INTERNSHIP, re.compile(r"\b(internship|intern\b)\b", re.IGNORECASE)),
    (EmploymentType.PART_TIME, re.compile(r"\bpart[- ]time\b", re.IGNORECASE)),
    (EmploymentType.CONTRACT, re.compile(r"\b(contract|freelance|gig)\b", re.IGNORECASE)),
    (EmploymentType.FULL_TIME, re.compile(r"\bfull[- ]time\b", re.IGNORECASE)),
]


def extract_employment_type(text: str) -> Optional[EmploymentType]:
    for line in _metadata_lines(text):
        for emp_type, rx in _EMPLOYMENT_PATTERNS:
            if rx.search(line):
                return emp_type
    return None


def extract_employment_type_label(text: str) -> Optional[str]:
    """Display label also covers Apprenticeship without expanding the Job schema."""
    for line in _metadata_lines(text):
        if re.search(r"\bapprenticeship\b", line, re.IGNORECASE):
            return "Apprenticeship"
        emp_type = extract_employment_type(line)
        if emp_type:
            return {
                EmploymentType.INTERNSHIP: "Internship",
                EmploymentType.PART_TIME: "Part-time",
                EmploymentType.CONTRACT: "Contract",
                EmploymentType.FULL_TIME: "Full-time",
            }[emp_type]
    return None


_SALARY_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*k?\s*"
    r"(?:[-–]|\bto\b)\s*(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*k?\s*"
    r"(?:/\s*month|per\s+month|lpa|lakhs?|lacs?)|"
    r"(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*k?\s*"
    r"(?:/\s*month|per\s+month|lpa|lakhs?|lacs?|kpa|per\s+annum|p\.?m\.?)",
    re.IGNORECASE,
)
_STIPEND_RE = re.compile(r"stipend[^\n]{0,40}", re.IGNORECASE)


def extract_salary(text: str) -> Optional[str]:
    for line in _metadata_lines(text):
        match = _SALARY_RE.search(line)
        if match:
            value = " ".join(match.group(0).split())
            return re.sub(
                r"\s*/\s*(month)", lambda part: "/" + part.group(1),
                value, flags=re.IGNORECASE,
            )
        match = _STIPEND_RE.search(line)
        if match:
            return " ".join(match.group(0).split()).rstrip(".,;")
    return None


def _field_evidence(text: str, extractor) -> Optional[str]:
    """Return the source line supporting a deterministic metadata value."""
    value = extractor(text)
    if value is None or value == "Not specified":
        return None
    lines = _metadata_lines(text)
    for line in lines:
        if value.casefold() in line.casefold():
            return line
    return next((line for line in lines if extractor(line) == value), None)


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


def message_to_job(msg: TgMessage, intent: Optional[IntentDecision] = None) -> Optional[Job]:
    """Convert one Telegram message into a normalized Job, or None if the
    message is not a job candidate / cannot be normalized."""
    text = (msg.text or "").strip()
    if not text:
        return None

    links = list(msg.links) if msg.links else []
    if not links:
        links = extract_links(text)
    decision = intent or classify_job_intent(text, links)
    if not decision.accepted:
        return None

    text = _canonicalize_text(text)
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
                "source_url": message_url,
                "message_date": posted_at.isoformat(),
                "freshness_label": freshness_label(posted_at),
                "detection_signals": list(decision.signals),
                "intent_category": decision.category.value,
                "intent_reason": decision.reason,
                "manual_application_action": decision.manual_application_action,
                "content_risk": content_risk_signals(text),
                "fresher_signals": extract_fresher_signals(text),
                "salary": extract_salary(text),
                "work_mode": extract_work_mode(text),
                "employment_type_label": extract_employment_type_label(text),
                "extraction_evidence": {
                    key: evidence
                    for key, evidence in {
                        "experience": _field_evidence(text, extract_experience),
                        "location": _field_evidence(text, extract_location),
                        "employment_type": _field_evidence(text, extract_employment_type_label),
                        "work_mode": _field_evidence(text, extract_work_mode),
                        "salary": _field_evidence(text, extract_salary),
                    }.items()
                    if evidence
                },
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



