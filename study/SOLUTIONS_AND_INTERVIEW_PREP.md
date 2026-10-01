# Hirevia — Solutions, Design Decisions & Interview Explanations

> A study guide for explaining how Hirevia's engineering challenges were approached.
>
> Use the explanations as a framework. Check the current source code before claiming a behavior is implemented.

## 1. Explain the project in 30–45 seconds

"Hirevia is a Python-based AI-assisted opportunity discovery agent. It collects job posts from multiple sources, normalizes them into a common structure, removes duplicates, checks freshness and application availability, evaluates candidate eligibility and role relevance, assigns a priority, and delivers opportunities through interfaces such as Telegram. The main engineering focus is making the results reliable and actionable rather than simply collecting a large number of job links."

## 2. Challenge-to-solution explanations

### 1. Different source formats

**Approach:** Give each source its own collector/adapter, but require collectors to produce a common internal job representation. Keep source-specific parsing at the boundary. Normalize titles, company names, locations, URLs, dates and employment details before the job enters the shared pipeline.

**Why this works:** The rest of the application can operate on one predictable schema instead of containing source-specific conditions everywhere.

**Interview explanation:** "I used a collector/adapter approach. Each source handles its own extraction, and the pipeline consumes normalized job objects. This separates integration details from business logic."

**Study next:** Adapter pattern, data transfer objects, schema validation, parsing failures.

### 2. Duplicate jobs

**Approach:** Normalize identifying fields and use stable source IDs or canonical URLs when available. Where IDs are missing, derive a fingerprint from normalized fields such as company, title, location and application URL. Persist deduplication state so rescans do not repeatedly notify the same opportunity.

**Why this works:** It reduces duplicate cards and duplicate alerts across repeated scans and sources.

**Trade-off:** A fingerprint can merge two genuinely different jobs if the fields are too broad, or miss duplicates if source data changes. Keep the identity strategy explainable and test collisions.

**Interview explanation:** "I treat deduplication as an identity problem. I prefer stable source identifiers, then canonical URLs, and use normalized-field fingerprints as a fallback."

### 3. Freshness and closed jobs

**Approach:** Keep source publication time and Hirevia's first-seen/detected time distinct. Apply a defined freshness policy and verify application pages where feasible. Represent uncertain states explicitly rather than assuming every HTTP failure means a job is closed.

**Why this works:** It avoids confusing newly discovered old jobs with genuinely new postings and avoids presenting clearly expired opportunities as actionable.

**Interview explanation:** "I separate when the source says a job was posted from when our system discovered it. I also treat application availability as a separate verification step."

**Study next:** UTC timestamps, time zones, TTL, HTTP status codes, retry policy.

### 4. Eligibility versus relevance

**Approach:** Use hard-veto rules for requirements that must be met, such as explicit experience or work-authorization constraints, and use relevance scoring for softer alignment such as skills and role similarity. Preserve explicit eligibility fields instead of relying only on an LLM-generated summary.

**Why this works:** A strong keyword match cannot compensate for a hard disqualifier.

**Interview explanation:** "I separate eligibility from relevance. Eligibility answers 'can this candidate apply based on known constraints?' Relevance answers 'how closely does this role match the candidate?'"

**Study next:** Rule engines, feature engineering, precision/recall, false positives and false negatives.

### 5. Explainable matching and scoring

**Approach:** Keep matching and priority decisions in the backend. Use structured candidate-profile data and the project's existing matching/scoring outputs. Where AI is used, treat it as one component rather than allowing the frontend to invent or silently override a score.

**Why this works:** CLI, web and Telegram can present the same decision. It also makes debugging possible because the backend can retain the signals used in the result.

**Interview explanation:** "I keep scoring backend-owned so every interface shows the same result. I want to explain a match using evidence such as matched skills, role alignment and eligibility rather than showing an unexplained number."

### 6. Provider configuration across cloud and local models

**Approach implemented/reported:** Separate provider configuration requirements by provider type. Cloud providers such as NVIDIA or Groq require their API key. Local providers such as llama.cpp or Ollama can be configured using the required endpoint and model, without requiring a real cloud API key.

**Why this works:** Local inference does not depend on an external billing credential in the same way a hosted API does.

**Interview explanation:** "I found that a universal API-key check was wrong for local inference. I made configuration validation provider-aware: cloud providers require credentials, while local providers are validated using endpoint and model settings."

**Study next:** Strategy pattern, provider interfaces, OpenAI-compatible endpoints, dependency injection.

### 7. Configuration loading and test isolation

**Approach implemented/reported:** Resolve `.env` and process environment values through the configuration loader, then construct the provider configuration from the resolved values. Keep process environment precedence predictable and test using temporary environment variables rather than the developer's real secrets.

**Why this works:** The status command, provider router and CLI should all see the same resolved configuration. Hermetic tests do not depend on a machine's personal `.env`.

**Interview explanation:** "I centralized configuration resolution and added tests with isolated environment variables. That prevents a local developer's settings from making CI tests behave differently."

**Study next:** Configuration layering, dotenv, monkeypatch, fixtures, test isolation.

### 8. Configured versus available versus working

**Approach:** Report these as separate states:
- **Configured:** required values exist.
- **Available:** the endpoint can be reached.
- **Working:** a representative inference request succeeds.

A status command can inspect configuration without contacting the model. A test command performs the actual connectivity/inference check.

**Why this works:** A stopped local server should not make a correctly configured provider appear unconfigured.

**Interview explanation:** "I separated configuration validation from health checks. This gives a more accurate diagnosis: a provider can be configured but unavailable because its local server is stopped."

**Example diagnostic commands:**

```powershell
python -m hirevia ai status
python -m hirevia ai test
netstat -ano | findstr :8080
curl http://127.0.0.1:8080/v1/models
```

The last two commands are useful only when the local llama.cpp server is expected to listen on port 8080.

### 9. Telegram bot versus user session

**Approach:** Treat message delivery and user-account source collection as different integrations. A bot can send messages to destinations where it has permission. An authenticated user session may access channels the account has joined, subject to Telegram's API rules and permissions.

**Why this works:** It avoids assuming bot permissions and user-account permissions are interchangeable.

**Interview explanation:** "I separated Telegram collection from Telegram notification delivery. They have different authentication models and capabilities, so they should not be hidden behind one misleading login state."

**Security:** Never log or expose API hashes, session strings, OTPs or two-factor passwords. Test authentication and joining with mocks before live use.

### 10. Shared CLI and web lifecycle

**Target design:** Put monitor lifecycle, scans and job operations behind shared application services/controller methods. CLI commands and web routes should call those same methods.

**Important process detail:** Sharing Python modules does not mean two independent OS processes share their in-memory objects. If CLI and web run separately, they need an intentional shared mechanism such as a single service process, persistent state, or inter-process communication.

**Interview explanation:** "I identified that separate CLI and web processes can each have their own in-memory monitor. The design therefore needs one application lifecycle or an explicit shared-state mechanism, rather than assuming imports create a global singleton across processes."

**Study next:** Python process model, ASGI lifespan, background tasks, database transactions, IPC, service boundaries.

### 11. Choosing web UI over a terminal UI

**Decision:** The persistent TUI experiment was abandoned because it did not meet the desired onboarding and configuration experience. The project returned to the known-good CLI and prioritized a browser-based command center.

**Interview explanation:** "I evaluated a terminal UI, but the product needed richer configuration, account onboarding and job investigation. I chose to keep the CLI simple and move the primary user workflow to the web interface."

**Lesson:** Engineering decisions should be based on user workflow and maintenance cost, not on novelty.

### 12. Regression testing

**Approach:** Add focused tests around the changed behavior, use mocks for network/model/Telegram operations, then run the full test suite. Clear all relevant environment variables in tests that assert providers are unconfigured.

**Why this works:** It catches both direct regressions and accidental dependencies on a developer's machine.

**Reported result:** The provider configuration work was reported with 29 focused tests passing and 620 tests passing in the full suite. Re-run tests on the current checkout before quoting these numbers as the latest result.

**Interview explanation:** "I use focused tests to validate the behavior I changed, then the full suite to check backward compatibility. I also isolate environment variables so tests are deterministic."

## 3. Useful commands for a project walkthrough

Run these from the project root, using the commands supported by the current checkout:

```powershell
# Inspect repository state
git status --short
git log --oneline -10
git diff --stat

# CLI
python -m hirevia --help
python -m hirevia status
python -m hirevia sources
python -m hirevia scan

# AI provider configuration
python -m hirevia ai status
python -m hirevia ai test

# Focused and full tests
python -m pytest tests/test_ai_providers.py -q
python -m pytest -q
```

Do not run commands that change primary providers or send Telegram messages during a demo unless you intend to change the live configuration or send a real message.

## 4. Interview questions and answer outlines

### Q1. What was the hardest engineering problem?

**Answer outline:** Reliable data from multiple heterogeneous sources. Explain collectors, normalization, deduplication and validation. Then explain why a common internal job model matters.

### Q2. How do you prevent duplicate jobs?

**Answer outline:** Stable source ID first, canonical URL next, normalized fingerprint as fallback, with persistent state and tests for repeated scans.

### Q3. How do you know a job is still open?

**Answer outline:** Freshness and application status are different. Explain timestamps, page checks, explicit closed indicators and uncertainty when a source cannot be verified.

### Q4. Why use both rules and an LLM?

**Answer outline:** Deterministic rules are suitable for explicit constraints and repeatable decisions. An LLM can help interpret unstructured descriptions or semantic fit. Explain why hard eligibility should not be overridden by a vague semantic match.

### Q5. What happens if the AI provider goes down?

**Answer outline:** Distinguish configured from reachable and inference-successful. Explain timeout/error handling and fallback only if it is actually implemented in the current code.

### Q6. Why did llama.cpp show configured but fail the test?

**Answer outline:** The configuration values existed, but the local endpoint refused connections. This is a runtime/network process issue, not a missing API key. Explain the difference between connection refused and timeout.

### Q7. How do you protect API keys?

**Answer outline:** Keep secrets in environment/configuration storage, mask them in status responses, avoid logging them, and never include Telegram session credentials in frontend responses.

### Q8. How would you make CLI and web share the same monitor?

**Answer outline:** Shared service/controller boundary is necessary, but two OS processes do not share memory. Choose a single long-running core or a persistent/IPC mechanism, and have both interfaces call it.

### Q9. How did you test the project?

**Answer outline:** Focused unit tests, mocks for external services, isolated environment variables, integration tests for pipeline behavior, and full regression suite.

### Q10. What would you improve next?

**Answer outline:** Verify and finish a single shared core lifecycle; complete the web workspace and configuration management; test Telegram user-session onboarding and routing end-to-end; improve observability and deployment hardening. Clearly label what is not yet complete.

## 5. Study plan

| Session | Study topic | Practical exercise |
|---|---|---|
| 1 | Project architecture | Trace `python -m hirevia` to the CLI entry point |
| 2 | Collectors and APIs | Trace one source collector and inspect its output |
| 3 | Normalization and deduplication | Follow a job through normalization and identity checks |
| 4 | Freshness and verification | Inspect date parsing and closed-job handling |
| 5 | Eligibility and scoring | Trace profile fields into matching output |
| 6 | AI providers | Follow `.env` values into provider construction |
| 7 | Local inference | Start/check llama.cpp and test `/v1/models` |
| 8 | Telegram | Trace collection and notification separately |
| 9 | Lifecycle and web | Identify process-local state and shared service boundaries |
| 10 | Testing | Run focused tests, then the full suite; explain one regression test |

## 6. A strong way to present the project

Use this structure in an interview:

1. **Problem:** Job discovery is fragmented and noisy; useful opportunities can be missed or duplicated.
2. **Solution:** A pipeline that collects, normalizes, verifies, matches and prioritizes jobs.
3. **Architecture:** Python collectors, shared job model, processing pipeline, storage, AI provider abstraction, CLI/web interfaces and Telegram integration.
4. **Engineering challenge:** Choose two or three real challenges, such as heterogeneous source data, deduplication, eligibility versus relevance, or local/cloud provider configuration.
5. **Trade-offs:** Explain why deterministic filters and AI matching are separated, why provider status differs from health, and why a shared core matters.
6. **Validation:** Show actual tests and a real end-to-end example from the current working checkout.
7. **Next steps:** Be transparent about unfinished web/Telegram/VPS work.

## 7. Final reminder

Never memorize claims that are not supported by the current code. During interview preparation, open the relevant implementation and be able to explain:

- What enters the function.
- What transformation happens.
- What leaves the function.
- What can fail.
- How the failure is handled.
- Which test proves the behavior.

That is stronger than memorizing feature names.
