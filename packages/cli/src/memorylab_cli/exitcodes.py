"""Stable process exit codes, one per API error `code` (and a few local conditions). Scripts may rely on these numbers;
a test pins the table. 0 is success; 2 is click's usage error."""

OK = 0
GENERIC = 1
USAGE = 2

EXIT_CODES: dict[str, int] = {
    "unauthenticated": 3,
    "not_logged_in": 3,
    "not_found": 4,
    "validation_failed": 5,
    "caps_required": 5,
    "invalid_runs": 5,
    "variable_mismatch": 5,
    "rate_limited": 6,
    "key_rejected": 7,
    "benchmark_patch_invalid": 8,
    "capacity_full": 9,
    "breaker_open": 10,
    "export_not_ready": 11,
    "export_refused": 11,
    "runs_still_active": 12,
    "report_closed": 12,
    "not_verified": 12,
    "limit_reached": 12,
    "internal": 13,
    "network": 14,
    "sha_mismatch": 15,
    "fork_disabled": 16,
    "config": 17,
    "payload_too_large": 5,
    "run_failed": 20,
    "run_killed": 21,
}


def exit_code_for(code: str) -> int:
    return EXIT_CODES.get(code, GENERIC)
