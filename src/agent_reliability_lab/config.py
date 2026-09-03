"""Configuration with filesystem paths kept inside the data directory."""

from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime paths resolved from safe, local environment configuration."""

    data_dir: Path
    database_path: Path

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
        return cls(data_dir=data_dir, database_path=database_path)


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()
