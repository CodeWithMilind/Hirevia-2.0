"""Tests for Telegram message -> Job extraction (pure logic, mocked messages)."""

from datetime import datetime, timedelta, timezone

from hirevia.core.models import EmploymentType
from hirevia.sources.telegram_extract import (
    TgMessage,
    content_stable_id,
    extract_company,
    extract_experience,
    extract_employment_type,
    extract_employment_type_label,
    extract_fresher_signals,
    extract_links,
    extract_location,
    extract_salary,
    extract_skills,
    extract_title,
    extract_work_mode,
    freshness_label,
    looks_like_job,
    message_to_job,
    messages_to_jobs,
    rank_application_url,
)

NOW = datetime(2026, 9, 28, 12, 0, 0)


def msg(text, links=None, message_id=42, channel="pythoninternships", date=None):
    return TgMessage(
        channel=channel,
        message_id=message_id,
        date=date or NOW,
        text=text,
        links=links or [],
    )


JOB_POST = (
    "🚨 Hiring: Backend Developer\n"
    "Company: Acme Corp\n"
    "Location: Pune (Hybrid)\n"
    "Experience: 0-2 years\n"
    "Skills: Python, FastAPI, PostgreSQL, Docker\n"
    "Salary: ₹4-6 LPA\n"
    "Apply: https://boards.greenhouse.io/acme-corp/jobs/12345\n"
    "Join our group: https://t.me/acme_hiring"
)


# --- detection -------------------------------------------------------------

def test_non_job_message_rejected():
    assert looks_like_job("Check out this funny meme of the day 😂")[0] is False
    assert looks_like_job("")[0] is False


def test_job_message_detected_with_signals():
    is_job, signals = looks_like_job(JOB_POST)
    assert is_job
    assert "hiring" in signals


def test_fresher_signals():
    assert extract_fresher_signals("Fresher friendly, no experience needed")
    assert extract_fresher_signals("Senior role, 10 years exp") == []


# --- links ------------------------------------------------------------------

def test_extract_links_strips_trailing_punctuation():
    links = extract_links("Apply now https://example.com/j/1, fast!")
    assert links == ["https://example.com/j/1"]


def test_telegram_links_never_application_urls():
    assert rank_application_url(["https://t.me/somegroup"]) is None


def test_ats_urls_preferred_over_generic():
    best = rank_application_url([
        "https://t.me/jobsgroup",
        "https://linktr.ee/hiring",
        "https://jobs.lever.co/startup/backend-eng",
    ])
    assert best == "https://jobs.lever.co/startup/backend-eng"


# --- field extraction --------------------------------------------------------

def test_title_prefers_role_line():
    # "🚨 Hiring: Backend Developer" -> the shouty "Hiring:" lead-in is noise,
    # the extractor keeps only the role itself.
    assert extract_title(JOB_POST) == "Backend Developer"


def test_company_from_label_and_url_and_unknown():
    assert extract_company(JOB_POST, "https://boards.greenhouse.io/acme-corp/jobs/12345") == "Acme Corp"
    assert extract_company("no label here", "https://boards.greenhouse.io/acme-corp/jobs/1") == "Acme Corp"
    assert extract_company("no label here", "https://linktr.ee/x") == "Unknown"


def test_location_label_and_heuristic():
    assert extract_location(JOB_POST) == "Pune (Hybrid)"
    loc = extract_location("Remote role for engineers in Bengaluru")
    assert "remote" in loc and "Bengaluru" in loc
    assert extract_location("nothing here") == "Not specified"


def test_skills_extracted():
    skills = [s.lower() for s in extract_skills(JOB_POST)]
    assert "python" in skills and "fastapi" in skills


def test_experience_range_and_fresher():
    assert extract_experience("Experience: 0-2 years") == "0-2 years"
    assert extract_experience("Fresher friendly role") == "Fresher / Entry level"
    assert extract_experience("nothing about experience") is None


def test_explicit_experience_and_multi_eligibility_variants():
    cases = {
        "Freshers": "Freshers",
        "Fresher": "Freshers",
        "Experience: Freshers / Experienced": "Freshers / Experienced",
        "JobType: Freshers/ Experienced": "Freshers / Experienced",
        "Freshers & Experienced": "Freshers / Experienced",
        "Experienced/Freshers": "Freshers / Experienced",
        "0–1 years": "0-1 years",
        "0-2 years": "0-2 years",
        "Entry Level": "Entry Level",
        "Entry-level": "Entry Level",
        "No Experience Required": "No Experience Required",
        "No prior experience required": "No Experience Required",
        "Experience: 0 years": "0 years",
        "Recent Graduate": "Recent Graduate",
        "Graduate": "Graduate",
    }
    for source, expected in cases.items():
        assert extract_experience(source) == expected


def test_explicit_location_preserves_full_place_name():
    assert extract_location("Job Location: Navi Mumbai") == "Navi Mumbai"


def test_employment_type_prefers_internship():
    assert extract_employment_type("Internship for freshers, full time later") == EmploymentType.INTERNSHIP
    assert extract_employment_type("Full-time senior role") == EmploymentType.FULL_TIME
    assert extract_employment_type("no hint") is None


def test_explicit_work_modes_and_employment_types():
    assert extract_work_mode("Work From Home") == "Remote"
    assert extract_work_mode("WFH") == "Remote"
    assert extract_work_mode("Remote Work") == "Remote"
    assert extract_work_mode("Work From Office") == "On-site"
    assert extract_work_mode("WFO") == "On-site"
    assert extract_work_mode("On-site") == "On-site"
    assert extract_work_mode("Hybrid") == "Hybrid"
    assert extract_employment_type("Internship") == EmploymentType.INTERNSHIP
    assert extract_employment_type("Full time") == EmploymentType.FULL_TIME
    assert extract_employment_type_label("Apprenticeship") == "Apprenticeship"
    assert extract_employment_type("trainee with stipend") is None
    assert extract_employment_type("no employment information") is None


def test_salary_and_stipend():
    assert extract_salary("Salary: ₹4-6 LPA good benefits") == "₹4-6 LPA"
    assert extract_salary("Stipend: ₹15,000/month,") == "₹15,000/month"
    assert extract_salary("no money mentioned at all") is None


def test_explicit_salary_formats():
    assert extract_salary("Expected Salary: ₹18k – ₹40k/ Month") == "₹18k – ₹40k/Month"
    assert extract_salary("Salary: 18,000 - 40,000 per month") == "18,000 - 40,000 per month"
    assert extract_salary("Salary: ₹18,000/month") == "₹18,000/month"
    assert extract_salary("Salary: ₹5 LPA") == "₹5 LPA"
    assert extract_salary("Salary: 5-8 LPA") == "5-8 LPA"


def test_metadata_extraction_ignores_promotional_and_link_lines():
    text = (
        "Join our Python course in Pune\n"
        "Testimonial: fresher hired remotely for ₹5 LPA\n"
        "More details https://example.com/remote-fresher-pune\n"
        "Follow @fresher_jobs for updates"
    )
    assert extract_experience(text) is None
    assert extract_location(text) == "Not specified"
    assert extract_work_mode(text) is None
    assert extract_employment_type(text) is None
    assert extract_salary(text) is None


def test_sutherland_post_preserves_explicit_job_metadata():
    text = (
        "Company Name: Sutherland\n"
        "Post Name: Applicant – Associate/Sr. Associate\n"
        "Expected Salary: ₹18k – ₹40k/ Month\n"
        "Job Location: Navi Mumbai\n"
        "JobType: Freshers/ Experienced\n"
        "Apply: https://job4freshers.co.in/sutherland-associate-sr-associate/"
    )
    job = message_to_job(msg(text))

    assert job is not None
    assert job.company == "Sutherland"
    assert job.title == "Applicant – Associate/Sr. Associate"
    assert job.experience == "Freshers / Experienced"
    assert job.location == "Navi Mumbai"
    assert job.raw_data["salary"] == "₹18k – ₹40k/Month"
    assert str(job.application_url) == "https://job4freshers.co.in/sutherland-associate-sr-associate/"
    assert job.description == text
    assert job.raw_data["extraction_evidence"]["experience"] == "JobType: Freshers/ Experienced"
    assert job.raw_data["extraction_evidence"]["location"] == "Job Location: Navi Mumbai"


def test_freshness_ladder():
    assert freshness_label(NOW, NOW) == "very_fresh"
    assert freshness_label(NOW - timedelta(hours=3), NOW) == "fresh"
    assert freshness_label(NOW - timedelta(hours=12), NOW) == "recent"
    assert freshness_label(NOW - timedelta(hours=48), NOW) == "older"
    assert freshness_label(NOW - timedelta(days=10), NOW) == "low_freshness"


# --- dedup key ---------------------------------------------------------------

def test_content_stable_id_from_url():
    a = content_stable_id("https://jobs.lever.co/x/role", "Unknown", "T", "Pune")
    b = content_stable_id("https://jobs.lever.co/x/role", "Other Co", "Different", "Mumbai")
    assert a == b  # repost with same application URL dedupes regardless of text drift


def test_content_stable_id_fallback_without_url():
    a = content_stable_id(None, "Acme Corp", "Backend Developer", "Pune")
    b = content_stable_id(None, "acme corp", "Backend  Developer", "pune")
    c = content_stable_id(None, "Other Co", "Backend Developer", "Pune")
    assert a == b
    assert a != c


# --- full conversion ----------------------------------------------------------

def test_message_to_job_full_conversion():
    aware = datetime(2026, 9, 28, 11, 30, 0, tzinfo=timezone.utc)
    job = message_to_job(msg(JOB_POST, message_id=77, date=aware))
    assert job is not None
    assert job.source == "telegram"
    assert job.title == "Backend Developer"  # "Hiring:" lead-in stripped
    assert job.company == "Acme Corp"
    assert str(job.url) == "https://t.me/pythoninternships/77"
    assert str(job.application_url) == "https://boards.greenhouse.io/acme-corp/jobs/12345"
    assert job.posted_at == datetime(2026, 9, 28, 11, 30, 0)  # stored naive UTC
    assert job.employment_type is None  # no explicit full-time/intern hint
    assert job.raw_data["source_channel"] == "pythoninternships"
    assert job.raw_data["source_message_id"] == 77
    assert job.raw_data["salary"] == "₹4-6 LPA"
    assert "https://t.me/acme_hiring" in job.raw_data["links"]


def test_message_to_job_none_for_non_jobs_and_empty():
    assert message_to_job(msg("Just chatting with friends 😄")) is None
    assert message_to_job(msg("   ")) is None


def test_repost_same_opening_same_id():
    """Channel reposts the same opening (new message id, extra emoji prefix):
    same application URL -> same source_job_id -> existing dedup absorbs it."""
    first = message_to_job(msg(JOB_POST, message_id=10))
    repost = message_to_job(msg("🔥🔥 BUMP " + JOB_POST, message_id=99))
    assert first.source_job_id == repost.source_job_id


def test_messages_to_jobs_filters_non_jobs():
    jobs = messages_to_jobs([
        msg("Random announcement about nothing relevant"),
        msg(JOB_POST, message_id=5),
    ])
    assert len(jobs) == 1
    assert jobs[0].source_job_id

