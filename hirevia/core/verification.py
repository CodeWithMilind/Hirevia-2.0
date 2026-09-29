"""Application-link quality + light-touch availability verification.

Two questions are answered here:

1. Is the link the job carries actually *usable* to apply?
   Telegram / WhatsApp / YouTube / social-only / link-aggregator links are not
   application pages, so they lower confidence instead of producing a
   "verified open" job.

2. Does the page still look reachable?
   One polite HEAD (GET fallback) request. We NEVER try to defeat CAPTCHAs,
   logins, rate limits or any other access control: 401/403/429, timeouts and
   5xx all mean "undetermined", never "closed".
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from hirevia.core.models import ApplicationStatus, Job

logger = logging.getLogger(__name__)

# Link families that can never be a real application page on their own.
SOCIAL_HOSTS = {
    "t.me", "telegram.me", "telegram.org", "web.telegram.org",
    "wa.me", "chat.whatsapp.com", "whatsapp.com", "api.whatsapp.com",
    "youtube.com", "youtu.be", "instagram.com", "instagr.am",
    "facebook.com", "fb.com", "m.facebook.com", "twitter.com", "x.com",
    "sharechat.com", "quora.com", "medium.com", "blogspot.com",
    "linktr.ee", "bio.link", "join.com", "ojos.ai", "discord.gg",
    "discord.com", "skool.com", "pin.it", "pinterest.com", "snapchat.com",
}

# URL shorteners hide the destination - usable but low confidence.
SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "goo.gl", "t.ly", "rb.gy", "cutt.ly", "cutt.us",
    "is.gd", "is.gle", "buff.ly", "ow.ly", "shorturl.at", "tiny.cc",
    "rebrand.ly", "v.du.pw", "s.id", "dwz.cn", "linkvertise.com",
}

# Community repost / link-harvest hosts: index listings, never the employer.
AGGREGATOR_HOSTS = {
    "fhlinks.com", "fhlinks.xyz", "pdlink.net", "pdlink.io", "shortplus.in",
    "apptrickz.com", "jobpula.com", "getjobs.in",
}

# Recognised ATS / job-platform hosts -> high confidence application pages.
ATS_HOSTS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io",
    "jobs.lever.co", "lever.co", "jobs.ashbyhq.com", "ashbyhq.com",
    "careers.smartrecruiters.com", "smartrecruiters.com",
    "apply.workable.com", "jobs.workable.com", "workable.com",
    "recruiting.bing.com", "myworkdayjobs.com", "wd1.myworkdayjobs.com",
    "wd2.myworkdayjobs.com", "wd3.myworkdayjobs.com", "wd4.myworkdayjobs.com",
    "wd5.myworkdayjobs.com", "linkedin.com", "instahyre.com", "cutshort.io",
    "unstop.com", "internshala.com", "wellfound.com", "foundersearch.in",
    "hirist.com", "naukri.com", "teamtailor.com", "myworkday.com",
    "taleo.net", "icims.com", "successfactors.com", "bamboohr.com",
    "recruitee.com", "personalpage.co",
}

# Public job boards: legitimate application pages, just not the employer's own.
JOB_BOARD_HOSTS = {
    "naukri.com", "instahyre.com", "cutshort.io", "indeed.com", "foundit.in",
    "timesjobs.com", "shine.com", "glassdoor.com",
}

_USABLE_QUALITIES = {"ats", "company", "job_board", "generic", "shortener"}


@dataclass
class UrlQuality:
    quality: str          # ats|company|job_board|generic|social|shortener|aggregator|none
    usable: bool          # can a candidate realistically apply through it
    confidence: int       # 0-100
    reason: str


def _host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "@" in host:  # strip possible userinfo
        host = host.rsplit("@", 1)[-1]
    return host.replace("www.", "")


def _matches(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def classify_application_url(url: Optional[str]) -> UrlQuality:
    """Grade an application link. Link labels/aggregators are never trusted."""
    if not url or not str(url).strip():
        return UrlQuality("none", False, 0, "no application link found in the post")

    text = str(url).strip()
    if not text.lower().startswith(("http://", "https://")):
        return UrlQuality("none", False, 0, "application link is not a URL")

    host = _host_of(text)
    if not host:
        return UrlQuality("none", False, 0, "application link has no host")

    if _matches(host, SOCIAL_HOSTS):
        return UrlQuality("social", False, 10, f"{host} is a social/channel link, not an application page")
    if _matches(host, AGGREGATOR_HOSTS):
        return UrlQuality("aggregator", False, 15, f"{host} is a community link aggregator, not the employer")
    if _matches(host, SHORTENER_HOSTS):
        return UrlQuality("shortener", True, 40, f"{host} hides the real destination (link shortener)")
    if _matches(host, ATS_HOSTS):
        return UrlQuality("ats", True, 90, f"recognised ATS/platform link ({host})")
    if _matches(host, JOB_BOARD_HOSTS):
        return UrlQuality("job_board", True, 75, f"recognised job platform ({host})")

    path = urlparse(text).path.lower()
    for token in ("/job", "/career", "/apply", "/opening", "/vacanc", "/posting", "/position"):
        if token in path:
            return UrlQuality("company", True, 80, f"company career page on {host}")
    return UrlQuality("generic", True, 50, f"external page on {host} (not a recognised ATS)")


def is_channel_like_url(url: Optional[str]) -> bool:
    """t.me/<channel>/<id> style links point at a post, not at a job."""
    return bool(url) and _host_of(str(url)) in {"t.me", "telegram.me"}


async def verify_application(
    job: Job,
    client=None,
    check_live: bool = True,
    timeout: float = 8.0,
):
    """Return (ApplicationStatus, UrlQuality) for one job.

    Conservative by design: an unreachable or protected page stays UNKNOWN and
    only a definitive 404/410/451 marks the application CLOSED.
    """
    url = str(job.application_url) if job.application_url else None
    quality = classify_application_url(url)

    if not quality.usable or not check_live:
        return ApplicationStatus.UNKNOWN, quality

    owns_client = client is None
    status_code = None
    try:
        if owns_client:
            import httpx

            client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "HireviaBot/2.0 (personal job alert scanner)"},
            )
        try:
            response = await client.head(url)
            status_code = response.status_code
            if status_code in (403, 405, 500, 501):
                response = await client.get(url)
                status_code = response.status_code
        except Exception as e:  # timeout / DNS / connection reset: undetermined
            return ApplicationStatus.UNKNOWN, UrlQuality(
                quality.quality, True, quality.confidence,
                f"reachability not confirmed ({type(e).__name__}); link is not marked closed",
            )
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:
                pass

    if status_code in (404, 410, 451):
        return ApplicationStatus.CLOSED, UrlQuality(
            quality.quality, False, 0, f"application page gone (HTTP {status_code})"
        )
    if status_code in (401, 403, 429):
        return ApplicationStatus.UNKNOWN, UrlQuality(
            quality.quality, True, max(30, quality.confidence - 20),
            f"application page protected/rate limited (HTTP {status_code}) - access not probed further",
        )
    if status_code is not None and status_code < 400:
        return ApplicationStatus.OPEN, UrlQuality(
            quality.quality, True, min(100, quality.confidence + 10),
            f"application page reachable (HTTP {status_code})",
        )
    return ApplicationStatus.UNKNOWN, UrlQuality(
        quality.quality, True, quality.confidence,
        f"application page returned HTTP {status_code} - undetermined",
    )

