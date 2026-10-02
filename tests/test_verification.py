"""Application URL classification and verification with fake HTTP responses."""

import asyncio
from types import SimpleNamespace

import pytest

from hirevia.core.models import ApplicationStatus, Job
from hirevia.core.verification import UrlQuality, classify_application_url, verify_application


@pytest.mark.parametrize(
    "url, expected_quality, usable",
    [
        ("https://careers.acme.com/jobs/backend", "company", True),
        ("https://apply.acme.com/apply/123", "company", True),
        ("https://jobs.lever.co/acme/backend", "ats", True),
        ("https://indeed.com/viewjob?jk=123", "job_board", True),
        ("https://t.me/jobs_channel/123", "social", False),
        (None, "none", False),
        ("", "none", False),
        ("ftp://careers.acme.com/jobs/backend", "none", False),
        ("https://example.com:invalid/jobs/backend", "none", False),
        ("https://", "none", False),
    ],
)
def test_application_url_quality(url, expected_quality, usable):
    quality = classify_application_url(url)

    assert quality.quality == expected_quality
    assert quality.usable is usable


class FakeHTTPClient:
    def __init__(self, head_status, get_status=None, fail_head=False):
        self.head_status = head_status
        self.get_status = get_status
        self.fail_head = fail_head
        self.calls = []

    async def head(self, url):
        self.calls.append(("HEAD", url))
        if self.fail_head:
            raise TimeoutError("fake timeout")
        return SimpleNamespace(status_code=self.head_status)

    async def get(self, url):
        self.calls.append(("GET", url))
        return SimpleNamespace(status_code=self.get_status)


def make_job(application_url="https://jobs.lever.co/acme/backend"):
    return Job(
        source="telegram",
        source_job_id="test-1",
        title="Backend Developer",
        company="Acme",
        location="Pune",
        description="Python role",
        url="https://t.me/jobs/1",
        application_url=application_url,
    )


def test_verified_open_application_is_actionable():
    client = FakeHTTPClient(200)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.OPEN
    assert quality.usable is True
    assert client.calls[0][0] == "HEAD"


def test_definitively_missing_application_is_closed_and_unusable():
    client = FakeHTTPClient(404, get_status=404)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.CLOSED
    assert quality.usable is False
    assert [method for method, _ in client.calls] == ["HEAD", "GET"]


def test_head_404_followed_by_open_get_stays_open():
    client = FakeHTTPClient(404, get_status=200)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.OPEN
    assert quality.usable is True
    assert [method for method, _ in client.calls] == ["HEAD", "GET"]


def test_generic_host_404_does_not_confirm_expiration():
    client = FakeHTTPClient(404, get_status=404)

    status, quality = asyncio.run(
        verify_application(make_job("https://example.com/test-p1"), client=client)
    )

    assert status is ApplicationStatus.UNKNOWN
    assert quality.usable is True
    assert "unrecognised" in quality.reason
    assert [method for method, _ in client.calls] == ["HEAD", "GET"]


@pytest.mark.parametrize("status_code", [401, 403, 429, 451, 503])
def test_protected_or_temporary_status_is_unknown_without_extra_probe(status_code):
    client = FakeHTTPClient(status_code, get_status=200)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.UNKNOWN
    assert quality.usable is True
    assert [method for method, _ in client.calls] == ["HEAD"]


def test_method_not_allowed_uses_the_existing_get_fallback():
    client = FakeHTTPClient(405, get_status=200)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.OPEN
    assert quality.usable is True
    assert [method for method, _ in client.calls] == ["HEAD", "GET"]


def test_live_check_disabled_returns_unknown_without_http_calls():
    client = FakeHTTPClient(200)

    status, quality = asyncio.run(
        verify_application(make_job(), client=client, check_live=False)
    )

    assert status is ApplicationStatus.UNKNOWN
    assert quality.usable is True
    assert client.calls == []


def test_transport_failure_is_unknown_not_closed():
    client = FakeHTTPClient(200, fail_head=True)

    status, quality = asyncio.run(verify_application(make_job(), client=client))

    assert status is ApplicationStatus.UNKNOWN
    assert quality.usable is True
    assert len(client.calls) == 1
