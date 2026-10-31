"""Shared constants for broker workers and coordinators."""

# -- Error categories for result_payload.error_category --
ERR_RATE_LIMIT = "rate_limit"
ERR_TIMEOUT = "timeout"
ERR_CLI_NOT_FOUND = "cli_not_found"
ERR_EXECUTION_ERROR = "execution_error"
ERR_AUTH_FAILURE = "auth_failure"
ERR_NETWORK_ERROR = "network_error"
ERR_CANCELLED = "cancelled"

# -- Status codes for result_payload.status_code --
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_CANCELLED = "cancelled"

# -- Priority bands --
PRIORITY_BACKGROUND = 100
PRIORITY_LOW = 300
PRIORITY_NORMAL = 500
PRIORITY_HIGH = 700
PRIORITY_CRITICAL = 900


def build_result_payload(
    *,
    status_code: str,
    error_category: str | None = None,
    failure_bucket: str | None = None,
    output_summary: str = "",
    output_full: str | None = None,
    duration_seconds: float | None = None,
    retry_count: int = 0,
    next_action: str | None = None,
    flow_action: str | None = None,
) -> dict:
    """Build a structured result_payload dict for task completion/failure."""
    payload: dict = {
        "status_code": status_code,
        "output_summary": output_summary[:5000],
    }
    if error_category:
        payload["error_category"] = error_category
    if failure_bucket:
        payload["failure_bucket"] = failure_bucket
    if output_full is not None:
        payload["output_full"] = output_full
    if next_action:
        payload["next_action"] = next_action
    if flow_action:
        payload["flow_action"] = flow_action
    payload["metrics"] = {
        "duration_seconds": duration_seconds,
        "retry_count": retry_count,
    }
    return payload
