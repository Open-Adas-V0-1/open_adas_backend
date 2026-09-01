import logging

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings

# stdlib logger bridge — tenacity's before_sleep_log expects a stdlib Logger,
# not a structlog one.
_retry_logger = logging.getLogger("llm.retry")


def is_retryable_exception(exc: BaseException) -> bool:
    """Rate-limit (429) or timeout errors are retryable. Detected by
    status_code / exception type — not by matching error message strings.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True

    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True

    if isinstance(exc, TimeoutError):
        return True

    exc_type_name = type(exc).__name__
    return "RateLimit" in exc_type_name or "Timeout" in exc_type_name


def build_retry_decorator(settings: Settings):
    """Build a tenacity retry decorator fully driven by env-backed Settings.
    Returns a no-op decorator when retries are disabled.
    """
    if not settings.llm_retry_enabled:
        return lambda fn: fn

    return retry(
        stop=stop_after_attempt(settings.llm_retry_max_attempts),
        wait=wait_exponential_jitter(
            initial=settings.llm_retry_initial_wait,
            max=settings.llm_retry_max_wait,
            jitter=settings.llm_retry_jitter,
        ),
        retry=retry_if_exception(is_retryable_exception),
        before_sleep=before_sleep_log(_retry_logger, logging.WARNING),
        reraise=True,
    )
