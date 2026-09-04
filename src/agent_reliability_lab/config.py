"""Validated local configuration for persistence and public adapters."""

import re
from dataclasses import dataclass
from os import environ
from pathlib import Path

_CATALOG_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,127})$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DEFAULT_BODY_LIMIT = 64 * 1024
_DEFAULT_TRUSTED_HOSTS = ("localhost", "127.0.0.1", "api")
DEFAULT_MAX_ACTION_STEPS = 64
MAX_ACTION_STEPS_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime paths and HTTP limits resolved from trusted server configuration."""

    data_dir: Path
    database_path: Path
    scenario_dir: Path | None = None
    evaluation_suites: tuple[tuple[str, Path], ...] = ()
    cors_origins: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = _DEFAULT_TRUSTED_HOSTS
    max_request_body_bytes: int = _DEFAULT_BODY_LIMIT
    max_action_steps: int = DEFAULT_MAX_ACTION_STEPS
    secret_values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 1 <= self.max_request_body_bytes <= 1024 * 1024:
            raise ValueError("request body limit must be between 1 byte and 1 MiB")
        validate_action_step_budget(self.max_action_steps)
        if "*" in self.cors_origins:
            raise ValueError("CORS origins must be explicit")
        normalized_hosts = tuple(host.lower() for host in self.trusted_hosts)
        if (
            not normalized_hosts
            or len(normalized_hosts) != len(set(normalized_hosts))
            or any(not _is_exact_host(host) for host in normalized_hosts)
        ):
            raise ValueError(
                "trusted hosts must be unique, exact hostnames or IPv4 addresses"
            )
        object.__setattr__(self, "trusted_hosts", normalized_hosts)
        names = [name for name, _ in self.evaluation_suites]
        if len(names) != len(set(names)):
            raise ValueError("evaluation suite names must be unique")
        if any(_CATALOG_NAME.fullmatch(name) is None for name in names):
            raise ValueError("evaluation suite names must be catalog identifiers")

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        """Build settings and reject a database path outside the data directory."""
        root = (project_root or Path.cwd()).resolve()
        data_dir = _resolve_path(environ.get("ARL_DATA_DIR", "data"), root)
        database_path = _resolve_path(
            environ.get("ARL_DATABASE_PATH", "agent-reliability-lab.db"), data_dir
        )
        try:
            database_path.relative_to(data_dir)
        except ValueError as error:
            raise ValueError(
                "database path must be inside the data directory"
            ) from error
        scenario_dir = _resolve_path(
            environ.get("ARL_SCENARIO_DIR", "scenarios/incident-response"), root
        )
        suite_name = environ.get("ARL_EVALUATION_SUITE", "incident-response")
        suite_dir = _resolve_path(
            environ.get("ARL_EVALUATION_SUITE_DIR", str(scenario_dir)), root
        )
        origins = tuple(
            origin.strip()
            for origin in environ.get("ARL_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
        raw_trusted_hosts = environ.get("ARL_TRUSTED_HOSTS")
        trusted_hosts = (
            _DEFAULT_TRUSTED_HOSTS
            if raw_trusted_hosts is None
            else tuple(host.strip() for host in raw_trusted_hosts.split(","))
        )
        try:
            body_limit = int(
                environ.get("ARL_MAX_REQUEST_BODY_BYTES", str(_DEFAULT_BODY_LIMIT))
            )
        except ValueError as error:
            raise ValueError("request body limit must be an integer") from error
        try:
            max_action_steps = int(
                environ.get("ARL_MAX_ACTION_STEPS", str(DEFAULT_MAX_ACTION_STEPS))
            )
        except ValueError as error:
            raise ValueError("action step budget must be an integer") from error
        secrets = frozenset(
            value for value in environ.get("ARL_SECRET_VALUES", "").split(",") if value
        )
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            scenario_dir=scenario_dir,
            evaluation_suites=((suite_name, suite_dir),),
            cors_origins=origins,
            trusted_hosts=trusted_hosts,
            max_request_body_bytes=body_limit,
            max_action_steps=max_action_steps,
            secret_values=secrets,
        )


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _is_exact_host(host: str) -> bool:
    if not host or host != host.strip() or len(host) > 253:
        return False
    return all(_HOST_LABEL.fullmatch(label) is not None for label in host.split("."))


def validate_action_step_budget(value: object) -> int:
    """Return one bounded logical-action budget or fail closed."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ACTION_STEPS_LIMIT
    ):
        raise ValueError(
            f"action step budget must be an integer between 1 and "
            f"{MAX_ACTION_STEPS_LIMIT}"
        )
    return value
