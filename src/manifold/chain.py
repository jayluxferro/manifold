"""Chain wiring logic: compute upstreams, patch service configs, handle bypass."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from manifold.models import (
    GatewayConfig,
    PipelineState,
    ServiceConfig,
    ServiceStatus,
    UpstreamVia,
)

# Statuses that indicate a service should not receive traffic
_SKIP_STATUSES = frozenset({ServiceStatus.STOPPED, ServiceStatus.UNHEALTHY})

log = logging.getLogger(__name__)


def _deep_set(data: dict, dot_path: str, value: str) -> None:
    """Set a value in a nested dict using a dot-separated key path.

    Example: _deep_set(d, "cloud_target.endpoint", "http://...") sets
    d["cloud_target"]["endpoint"].
    """
    keys = dot_path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def _deep_get(data: dict, dot_path: str) -> str | None:
    """Read a value from a nested dict using a dot-separated key path."""
    keys = dot_path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, str) else None


def compute_upstreams(
    services: list[ServiceConfig],
    fallback_upstream: str,
) -> dict[str, str]:
    """Compute the upstream URL for each enabled service.

    Each service's upstream is the next enabled service in the pipeline.
    The last enabled service's upstream is the fallback (cloud API).
    Disabled services are skipped entirely.

    Returns a dict mapping service name -> upstream URL.
    """
    enabled = [s for s in services if s.enabled]
    upstreams: dict[str, str] = {}
    for i, svc in enumerate(enabled):
        if i + 1 < len(enabled):
            next_svc = enabled[i + 1]
            base = f"http://127.0.0.1:{next_svc.port}"
            upstreams[svc.name] = (
                f"{base}{svc.upstream_path}" if svc.upstream_path else base
            )
        else:
            upstreams[svc.name] = (
                f"{fallback_upstream}{svc.upstream_path}"
                if svc.upstream_path
                else fallback_upstream
            )
    return upstreams


def compute_active_upstreams(
    pipeline: PipelineState,
    fallback_upstream: str,
) -> dict[str, str]:
    """Compute upstreams for the *current* healthy/starting services.

    This accounts for bypassed/unhealthy services by skipping them,
    so the chain rewires around failures.
    """
    active = [
        s.config
        for s in pipeline.services
        if s.config.enabled and s.status not in _SKIP_STATUSES
    ]
    return compute_upstreams(active, fallback_upstream)


def patch_service_config(service: ServiceConfig, upstream_url: str) -> None:
    """Write the upstream URL into a service's YAML config file.

    Only applies to services with upstream_via=config_file.
    """
    if service.upstream_via != UpstreamVia.CONFIG_FILE:
        return

    config_path = Path(service.directory) / service.config_file
    if not config_path.exists():
        log.warning("Config file %s not found, skipping upstream patch", config_path)
        return

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    original = _deep_get(data, service.upstream_key)

    # Skip the write when the value is already correct — rewriting the file
    # would bump its mtime and trigger hot-reload in services that watch
    # their own config, causing unnecessary cascading restarts.
    if original == upstream_url:
        log.debug(
            "%s: %s already set to %s, skipping",
            config_path,
            service.upstream_key,
            upstream_url,
        )
        return

    _deep_set(data, service.upstream_key, upstream_url)

    with open(config_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    log.info(
        "Patched %s: %s = %s (was %s)",
        config_path,
        service.upstream_key,
        upstream_url,
        original,
    )


class _TemplateVars(dict):
    """``str.format_map`` lookup that fails loudly only for placeholder-like keys.

    A command string is shell, so braces are usually literals (awk programs,
    find's ``{}``, brace expansion).  The old bare ``.format()`` turned every
    brace into a template and died with a raw KeyError/IndexError at ``up``
    time — after validation had already passed.  The rules here, in order:

    - ``{port}`` / ``{upstream}`` -> substituted (the only valid placeholders);
    - ``{word}`` where *word* is an identifier -> :exc:`ValueError` naming the
      placeholder and the command.  That shape is almost certainly a typo, not
      shell syntax, so it must be a loud config error, not a traceback;
    - anything else (``{}``, ``{print $1}``, ``{0}``) -> passed through
      literally.

    ``{}`` and ``{digits}`` are positional slots to str.format and IndexError
    before ``__missing__`` is ever consulted, so ``resolve_command``
    pre-escapes them into ``{{...}}`` (str.format's own literal-brace escape).
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command

    def __missing__(self, key: str) -> str:
        if key.isidentifier():
            raise ValueError(
                f"Command template {self._command!r} contains unknown placeholder "
                f"{{{key}}}. Only {{port}} and {{upstream}} are substituted; to "
                "pass a literal single-brace group through to the shell, make it "
                "non-identifier-shaped (e.g. awk '{print $1}') or double the "
                "braces ({{...}})."
            )
        return "{" + key + "}"  # awk programs, brace expansion — shell syntax


# Positional slots ({} and {0}) — pre-escaped so they survive format_map as
# literals.  Lookarounds skip brace groups already escaped as {{...}}.
_POSITIONAL_SLOT_RE = re.compile(r"(?<!\{)\{(\d*)\}(?!\})")


def resolve_command(service: ServiceConfig, upstream_url: str) -> str:
    """Resolve template variables in a service's command string.

    Only ``{port}`` and ``{upstream}`` are placeholders; see :class:`_TemplateVars`
    for how literal shell braces are treated.  Raises :exc:`ValueError` for an
    identifier-shaped unknown placeholder so config errors surface at
    validation time, not mid-spawn.
    """
    command = _POSITIONAL_SLOT_RE.sub(
        lambda m: "{{" + m.group(1) + "}}", service.command
    )
    variables = _TemplateVars(command)
    variables["port"] = str(service.port)
    variables["upstream"] = upstream_url
    return command.format_map(variables)


def wire_pipeline(
    services: list[ServiceConfig],
    gateway: GatewayConfig,
) -> dict[str, str]:
    """Wire the full pipeline: compute upstreams and patch all configs.

    Returns the upstream map for reference.
    """
    upstreams = compute_upstreams(services, gateway.fallback_upstream)

    for svc in services:
        if not svc.enabled:
            continue
        upstream_url = upstreams[svc.name]
        if svc.upstream_via == UpstreamVia.CONFIG_FILE:
            patch_service_config(svc, upstream_url)
        # cli_arg services get their upstream injected via resolve_command at start time

    return upstreams


def rewire_around(
    pipeline: PipelineState,
    gateway: GatewayConfig,
) -> dict[str, str]:
    """Rewire the chain to bypass unhealthy services.

    Updates in-memory upstream state only.  Config files are NOT patched here
    because services that use config_file upstream don't hot-reload — the
    running process ignores the disk change, and the stale fallback endpoint
    on disk will cause auth failures if the service restarts later.  Instead,
    the auto-restart logic re-patches correctly before relaunching.

    Returns the new upstream map.
    """
    upstreams = compute_active_upstreams(pipeline, gateway.fallback_upstream)

    for state in pipeline.services:
        svc = state.config
        if svc.name not in upstreams:
            continue
        new_upstream = upstreams[svc.name]
        if state.upstream_url != new_upstream:
            state.upstream_url = new_upstream
            log.info(
                "Rewired %s upstream to %s (in-memory only)", svc.name, new_upstream
            )

    return upstreams


# Service-name fragments whose entry-hop bypass degrades privacy.  The
# redactor is the chain's PII scrubber: when the gateway's entry hop skips it,
# raw (un-redacted) payloads reach every downstream service and
# restore-on-response never runs.
_PRIVACY_CRITICAL_FRAGMENTS = ("redactor",)

# Entry services already warned about in the current bypass episode, cleared
# when the service is servable again so a recurring bypass re-warns.
# get_entry_url runs once per request — without this a dead redactor would
# spam one warning per request.
_entry_bypass_warned: set[str] = set()


def get_entry_route(
    pipeline: PipelineState, gateway: GatewayConfig
) -> tuple[str | None, list[str]]:
    """Resolve the entry URL and the enabled services this request bypasses.

    Returns ``(entry_url, bypassed)``: the first healthy/starting service's
    URL, plus the names of every enabled-but-down service skipped ahead of
    it.  ``bypassed`` is per-request truth — the gateway stamps it onto the
    response as ``x-manifold-bypassed`` so a bypass is visible on the traffic
    itself, not only in logs.  Empty when the chain is fully servable (or
    fully down: no route means nothing was bypassed *to*).

    The gateway must never bypass the pipeline and send directly to the
    cloud — if no service is available, requests should fail with 503.

    Skipping an enabled-but-down service is a real traffic bypass (the only
    one manifold performs — mid-chain deaths rely on auto-restart), so when
    the skipped service is a redactor we warn loudly about the privacy
    degradation: raw PII flows until it is restored.
    """
    bypassed: list[str] = []
    for state in pipeline.services:
        if not state.config.enabled:
            continue
        if state.status in (ServiceStatus.HEALTHY, ServiceStatus.STARTING):
            _entry_bypass_warned.discard(state.config.name)
            return f"http://127.0.0.1:{state.config.port}", bypassed
        # Enabled but not servable -> bypassed at the entry hop.
        name = state.config.name
        bypassed.append(name)
        if (
            any(frag in name.lower() for frag in _PRIVACY_CRITICAL_FRAGMENTS)
            and name not in _entry_bypass_warned
        ):
            _entry_bypass_warned.add(name)
            log.warning(
                "Entry-hop bypass of redactor service '%s' — privacy degradation: "
                "raw PII flows to the rest of the chain until it is restored; "
                "restore-on-response is skipped",
                name,
            )
    return None, bypassed


def get_entry_url(pipeline: PipelineState, gateway: GatewayConfig) -> str | None:
    """Get the URL the gateway should forward requests to (see get_entry_route)."""
    url, _ = get_entry_route(pipeline, gateway)
    return url
