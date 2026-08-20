"""Pure status parsing helpers for the 3DXChat status cog."""

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional

STATUS_API_URL = "https://status.3dxchat.net/stats.php"
STATUS_PAGE_URL = "https://status.3dxchat.net/index"
STALE_AFTER_SECONDS = 600

SERVICE_KEYS = (
    "authserver",
    "gameserver",
    "translateserver",
    "api",
    "contentserver",
    "website",
    "forum",
)

SERVICE_LABELS = {
    "authserver": "Auth server",
    "gameserver": "Game server",
    "translateserver": "Translation server",
    "api": "REST API",
    "contentserver": "Content server",
    "website": "Website",
    "forum": "Forum",
}

STATUS_LABELS = {
    "ONLINE": "All monitored 3DXChat services are operational.",
    "ISSUES": "One or more monitored 3DXChat services may have issues.",
    "MAINTENANCE": "3DXChat is currently reporting a maintenance window.",
    "OFFLINE": "The overall 3DXChat service is currently offline.",
    "UNKNOWN": "The 3DXChat status API could not be verified.",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _is_stale(payload: Mapping[str, Any]) -> bool:
    last_update = _number(payload.get("lastupdate"))
    if last_update is None:
        return False

    reference_time = _number(payload.get("now"))
    if reference_time is None:
        reference_time = time.time()

    return reference_time - last_update > STALE_AFTER_SECONDS


def classify_status(payload: Optional[Mapping[str, Any]]) -> str:
    """Return the status label used by the Discord presence and embed."""
    if not isinstance(payload, Mapping) or not payload:
        return "UNKNOWN"

    overall = _mapping(payload.get("overall"))
    stats = _mapping(payload.get("stats"))

    if overall.get("isUp") is False or stats.get("overallOnline") is False:
        return "OFFLINE"

    maintenance = _mapping(payload.get("maintenance"))
    if maintenance.get("active") is True:
        return "MAINTENANCE"

    if overall.get("runningWithIssues") is True:
        return "ISSUES"

    recent_failures = _number(stats.get("recentChecksFailed"))
    if recent_failures is not None and recent_failures > 0:
        return "ISSUES"

    down_services = payload.get("downServices")
    if isinstance(down_services, (list, tuple, set)) and down_services:
        return "ISSUES"

    for service_name in SERVICE_KEYS:
        service = _mapping(payload.get(service_name))
        if service.get("isUp") is False:
            return "ISSUES"
        if service.get("previousCheckFailed") is True:
            return "ISSUES"
        service_failures = _number(service.get("recentChecksFailed"))
        if service_failures is not None and service_failures > 0:
            return "ISSUES"

    if _is_stale(payload):
        return "UNKNOWN"

    return "ONLINE"


def service_label(service_name: str) -> str:
    """Return a human-readable service name."""
    return SERVICE_LABELS.get(service_name, service_name)


def service_snapshot(payload: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return the small service snapshot used to detect meaningful changes."""
    if not isinstance(payload, Mapping):
        return {}

    snapshot: Dict[str, Dict[str, Any]] = {}
    for service_name in SERVICE_KEYS:
        service = _mapping(payload.get(service_name))
        latency = _number(service.get("latency"))
        snapshot[service_name] = {
            "isUp": service.get("isUp"),
            "previousCheckFailed": service.get("previousCheckFailed"),
            "recentChecksFailed": service.get("recentChecksFailed"),
            "latency": round(latency, 3) if latency is not None else None,
            "statusChanged": service.get("statusChanged"),
        }
    return snapshot


def state_signature(payload: Optional[Mapping[str, Any]], status: str) -> str:
    """Create a stable fingerprint for deciding whether a message needs editing."""
    if not isinstance(payload, Mapping):
        return "unknown"

    stats = _mapping(payload.get("stats"))
    uptime_day = _mapping(payload.get("uptimeday"))
    uptime_week = _mapping(payload.get("uptimeweek"))
    latest_patch = _mapping(payload.get("latestpatch"))
    signature_data = {
        "status": status,
        "services": service_snapshot(payload),
        "uptime_day": uptime_day.get("uptimePerc"),
        "uptime_week": uptime_week.get("uptimePerc"),
        "latest_patch": latest_patch.get("version"),
        "down_services": payload.get("downServices") or [],
        "message": payload.get("message"),
        "recent_checks_failed": stats.get("recentChecksFailed"),
    }
    encoded = json.dumps(signature_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def status_description(status: str) -> str:
    """Return the standard description for a status label."""
    return STATUS_LABELS.get(status, STATUS_LABELS["UNKNOWN"])
