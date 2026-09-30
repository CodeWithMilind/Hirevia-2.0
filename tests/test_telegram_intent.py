"""Telegram job-intent classification keeps openings and rejects content noise."""

from datetime import datetime

import pytest

from hirevia.sources.telegram_extract import (
    IntentCategory,
    TgMessage,
    classify_job_intent,
    message_to_job,
)


@pytest.mark.parametrize(
    ("text", "accepted", "category"),
    [
        ("Join our Python course", False, IntentCategory.PROMOTIONAL),
        ("AI/ML Bootcamp - Enroll Now", False, IntentCategory.PROMOTIONAL),
        ("Get certified in Data Science", False, IntentCategory.EDUCATIONAL),
        ("Free webinar on Generative AI", False, IntentCategory.EDUCATIONAL),
        ("Paid internship training program", False, IntentCategory.PROMOTIONAL),
        ("XYZ Technologies is hiring Python Developers", True, IntentCategory.JOB),
        ("Hiring Freshers - Software Developer - Pune", True, IntentCategory.JOB),
        ("AI Engineer Internship - Apply Now", True, IntentCategory.INTERNSHIP),
        ("Off Campus Drive 2026 - Multiple Software Roles", True, IntentCategory.HIRING_DRIVE),
        ("Walk-in Interview - Junior Developer", True, IntentCategory.HIRING_DRIVE),
        ("Free training + interview opportunity with XYZ", True, IntentCategory.UNKNOWN),
        ("Referral hiring drive - apply here", True, IntentCategory.HIRING_DRIVE),
        ("Registration open for XYZ hiring drive", True, IntentCategory.HIRING_DRIVE),
        ("Career tips for data analysts", False, IntentCategory.INFORMATIONAL),
        ("Subscribe to our Telegram channel", False, IntentCategory.SPAM),
        ("Boost Your Resume", False, IntentCategory.SPAM),
        ("Job news: company announces layoffs", False, IntentCategory.INFORMATIONAL),
        ("Professional resume-writing service", False, IntentCategory.SPAM),
    ],
)
def test_job_intent_scenarios(text, accepted, category):
    decision = classify_job_intent(text)

    assert decision.accepted is accepted
    assert decision.category is category
    assert decision.reason


def test_real_scan_educational_titles_are_not_jobs():
    for text in (
        "Real-World Data Science Example",
        "Soft skills questions will be part of your next data job interview",
        "Learning Python for data science can be a rewarding experience",
    ):
        assert not classify_job_intent(text).accepted


def test_real_role_is_preserved_when_training_is_a_job_duty():
    text = (
        "Data Analyst Project Intern (Training & Quality)\n"
        "Company: ByteDance\n"
        "Apply: https://jobs.lever.co/bytedance/data-analyst-intern"
    )
    job = message_to_job(TgMessage("internships", 1, datetime(2026, 9, 30), text))

    assert job is not None
    assert job.raw_data["intent_category"] == IntentCategory.INTERNSHIP.value


def test_hiring_call_to_action_wins_over_promotional_wording():
    decision = classify_job_intent(
        "XYZ is hiring freshers. Register using the application link. Training is provided."
    )

    assert decision.accepted
    assert decision.category is IntentCategory.JOB


def test_ambiguous_intent_is_preserved_as_unknown_not_promotional():
    decision = classify_job_intent("Free training + interview opportunity with XYZ")

    assert decision.accepted
    assert decision.category is IntentCategory.UNKNOWN
    assert "no specific opening" in decision.reason
