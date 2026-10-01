# Hirevia — Project Challenges

> Interview and revision notes for understanding the engineering problems encountered while building Hirevia.
>
> **Accuracy note:** These notes distinguish work that was implemented or tested from ideas that were planned. A planned feature should not be described in an interview as production-ready.

## 1. Project overview

Hirevia is an AI-assisted job and opportunity discovery system. Its goal is to collect opportunities from multiple sources, convert them into a consistent format, remove duplicates, check freshness and application availability, evaluate relevance against a candidate profile, assign priority, and deliver useful results.

The intended processing flow is:

1. Source discovery and collection.
2. Job extraction and normalization.
3. Duplicate detection.
4. Freshness and application-status checks.
5. Candidate eligibility and profile matching.
6. Relevance scoring and P1/P2/P3 classification.
7. Storage and notification, including Telegram.

The engineering challenge is not simply fetching job links. It is producing opportunities that are relevant, current, understandable, and actionable.

## 2. Challenges encountered

### Challenge 1 — Different job sources return different data

**Problem:** Job sources do not share one schema. An ATS career page, a public job API, a Telegram post, and a remote-job board can describe the same opportunity in different formats. Some provide structured fields; others provide mostly free text.

**Why it matters:** Downstream matching and filtering become unreliable if every collector returns a different shape or leaves important fields in inconsistent formats.

**Engineering concepts to study:** API integration, web scraping, adapters, data contracts, schema normalization, defensive parsing.

### Challenge 2 — Duplicate opportunities across sources

**Problem:** The same job may appear on a company career page, an ATS board, and a Telegram channel. URL variants and slightly different titles can make exact-string deduplication insufficient.

**Why it matters:** Duplicate cards waste the user's time and duplicate Telegram notifications reduce trust in the system.

**Engineering concepts to study:** Canonical URLs, stable external IDs, normalized company/title/location fields, fingerprints, idempotency, persistent deduplication.

### Challenge 3 — Expired or closed jobs

**Problem:** A collected job can be old, have a passed deadline, or have a page that is no longer accepting applications. A job's posting date is not always the same as the time Hirevia discovered it.

**Why it matters:** A relevant job is not useful if the user cannot apply.

**Engineering concepts to study:** Freshness windows, timestamps, TTL/retention, HTTP status handling, application-state verification, scheduled rechecks, uncertainty states.

### Challenge 4 — Distinguishing eligibility from skill relevance

**Problem:** A job may match a candidate's Python or AI skills but require several years of experience, a different country, or work authorization the candidate does not have.

**Why it matters:** A high skill match must not hide a hard eligibility mismatch.

**Engineering concepts to study:** Hard filters versus soft ranking, structured candidate profiles, explicit eligibility fields, rule-based filtering, explainable matching.

### Challenge 5 — Designing useful relevance scoring

**Problem:** Matching a job to a candidate is not just keyword overlap. Skills, role type, experience, location, employment type, and freshness can all matter. A model can also make inconsistent judgments if the input is incomplete.

**Why it matters:** Scores should be explainable and stable enough to support prioritization.

**Engineering concepts to study:** Feature engineering, deterministic rules, weighted scoring, LLM-assisted classification, score calibration, explainability, evaluation datasets.

### Challenge 6 — AI provider configuration and fallback behavior

**Problem:** Hirevia supports multiple model providers, but providers differ in authentication, endpoint format, model naming, and whether they are local or cloud-hosted. Configuration and runtime availability are separate concerns.

**Observed issue:** The provider status logic initially treated every provider as requiring an API key. This incorrectly marked local llama.cpp/Ollama configurations as unconfigured even when endpoint and model settings were present.

**Engineering concepts to study:** Provider abstraction, environment variables, configuration validation, dependency injection, fallback strategies, OpenAI-compatible APIs.

### Challenge 7 — Configuration loading and environment consistency

**Problem:** Values in `.env`, process environment variables, YAML configuration, and runtime objects can disagree if loading order and precedence are unclear.

**Observed issue:** Provider-specific endpoint/model environment variables were not consistently copied into the resolved provider configuration. Tests could also accidentally inherit values from a developer's real `.env`.

**Engineering concepts to study:** Twelve-factor configuration, dotenv loading, environment precedence, immutable settings objects, test isolation, monkeypatching.

### Challenge 8 — Local llama.cpp connectivity versus configuration

**Problem:** A provider can be correctly configured while its local server is stopped or unreachable.

**Observed issue:** Hirevia recognized llama.cpp as configured, but a live test to `http://127.0.0.1:8080/v1` failed with `WinError 10061` because nothing was accepting connections on that port. Earlier attempts also encountered inference timeouts.

**Engineering concepts to study:** TCP ports, localhost, HTTP APIs, process lifecycle, SSH tunnels, health checks, connection refused versus timeout, inference latency.

### Challenge 9 — Telegram bot access versus Telegram user-account access

**Problem:** A Telegram bot and an authenticated Telegram user account have different capabilities. Bot API access does not automatically provide the same ability to browse or join channels as a user session.

**Design question:** The project explored QR-based user login so a user-authorized session could access configured public job channels, while keeping bot-based delivery as a separate concern.

**Engineering concepts to study:** Bot API versus MTProto, session authentication, QR login, API ID/hash, permissions, rate limits, secret storage, user consent.

**Status caution:** Do not claim that QR login, bulk channel joining, or Telegram folder management is live unless it has been tested in the current checkout.

### Challenge 10 — Monitor lifecycle and multiple processes

**Problem:** A monitor started by one process is not automatically the same monitor used by another process. A CLI command and a separately launched web server can therefore have different in-memory state.

**Why it matters:** The web page might show one status while a CLI process is scanning independently. Multiple processes can also compete for ports or send duplicate notifications.

**Engineering concepts to study:** Process boundaries, shared services, application lifecycle, singleton scope, background workers, inter-process communication, database-backed state, graceful shutdown.

### Challenge 11 — Choosing the right user interface

**Problem:** A persistent terminal UI was explored, but it added interaction complexity and did not provide the setup and management experience wanted for the product.

**Decision:** Return to the known-good terminal interface and focus on a browser-based command center, while retaining the CLI as a launcher and administration tool.

**Engineering concepts to study:** UX trade-offs, interface separation, product scope, incremental delivery, backward compatibility.

### Challenge 12 — Regression testing while changing a mature codebase

**Problem:** New features can break older CLI commands, provider behavior, source collectors, or pipeline assumptions. Tests that rely on real environment values can also become flaky.

**Observed testing history:** A full-suite run reached 620 passing tests after the provider-configuration fix. Earlier project work had other reported baselines; always verify the current checkout rather than assuming a historical count.

**Engineering concepts to study:** Unit tests, integration tests, regression suites, fixtures, mocks, hermetic tests, CI, test doubles.

## 3. Important engineering distinctions

Be ready to explain these clearly:

| Concept | Meaning in Hirevia |
|---|---|
| Collected time | When Hirevia first found the opportunity |
| Posted time | When the source says the job was published |
| Freshness | How recently the opportunity was posted or discovered, according to the chosen policy |
| Configured | Required settings exist |
| Available | The provider endpoint can be reached |
| Working | The provider completes a valid inference request |
| Eligibility | The candidate meets hard requirements |
| Relevance | How closely the role matches the candidate |
| Priority | A category assigned by the backend using the project's rules |
| Bot delivery | Messages sent through a Telegram bot |
| User session | An authenticated Telegram account session |

## 4. What is implemented versus what was planned

Use the current repository and tests as the final source of truth.

Known work reported during development:
- Existing CLI commands and job pipeline were preserved during the terminal rollback.
- Provider configuration was updated so cloud providers remain API-key-gated while local providers can be configured through endpoint/model settings.
- Provider status was intended to be configuration-only and not make network calls.
- Focused provider tests were reported as 29 passing.
- A full suite was reported as 620 passing after the provider fix.
- The live llama.cpp test still failed when the local endpoint was not listening.

Discussed or planned capabilities that require fresh verification:
- A unified web-first core lifecycle.
- Browser-based editing of YAML and AI settings.
- QR-based Telegram user login.
- Automatic joining of configured Telegram job channels.
- A Telegram folder named `Hirevia Jobs`.
- P1/P2/P3 Telegram community destinations.
- Automatic removal from active web views after 24 hours.
- A fully shared monitor instance between CLI and web.

Do not present planned work as completed.

## 5. Interview preparation checklist

Before an interview, be able to answer:

- Why did you build Hirevia?
- What is the end-to-end job processing pipeline?
- How do you normalize data from different sources?
- How do you detect duplicate jobs?
- How do you distinguish expired jobs from fresh jobs?
- Why separate hard eligibility filters from relevance scoring?
- What happens when an AI provider is configured but unavailable?
- How do you keep API keys and Telegram session data private?
- Why can a local model be configured but unreachable?
- Why can two CLI/web processes have different in-memory state?
- How do you prevent regressions while adding features?
- Which features are implemented, and which are still planned?

## 6. Suggested study order

1. Read the project README and trace the CLI entry point.
2. Trace one source collector from fetch to normalized job object.
3. Follow one job through the pipeline.
4. Study the deduplication and storage implementation.
5. Study eligibility and matching separately.
6. Trace provider configuration from `.env` to the model client.
7. Run provider status and provider tests.
8. Study Telegram collection and notification paths.
9. Trace monitor startup and shutdown.
10. Review the web implementation and identify which state is shared versus process-local.
