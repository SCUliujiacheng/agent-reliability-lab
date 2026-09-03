"""Validated local configuration for persistence and public adapters."""

import re
from dataclasses import dataclass
from os import environ
from pathlib import Path

_CATALOG_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,127})$")
_DEFAULT_BODY_LIMIT = 64 * 1024


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime paths and HTTP limits resolved from trusted server configuration."""

    data_dir: Path
    database_path: Path
    scenario_dir: Path | None = None
    evaluation_suites: tuple[tuple[str, Path], ...] = ()
    cors_origins: tuple[str, ...] = ()
    max_request_body_bytes: int = _DEFAULT_BODY_LIMIT
    secret_values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 1 <= self.max_request_body_bytes <= 1024 * 1024:
            raise ValueError("request body limit must be between 1 byte and 1 MiB")
        if "*" in self.cors_origins:
            raise ValueError("CORS origins must be explicit")
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
        try:
            body_limit = int(
                environ.get("ARL_MAX_REQUEST_BODY_BYTES", str(_DEFAULT_BODY_LIMIT))
            )
        except ValueError as error:
            raise ValueError("request body limit must be an integer") from error
        secrets = frozenset(
            value for value in environ.get("ARL_SECRET_VALUES", "").split(",") if value
        )
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            scenario_dir=scenario_dir,
            evaluation_suites=((suite_name, suite_dir),),
            cors_origins=origins,
            max_request_body_bytes=body_limit,
            secret_values=secrets,
        )


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()
