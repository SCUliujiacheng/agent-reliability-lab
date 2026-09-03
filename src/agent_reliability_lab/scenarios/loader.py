"""Safe YAML scenario loading and byte-exact provenance hashes."""

from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_reliability_lab.domain.scenarios import Scenario


class ScenarioValidationError(ValueError):
    """Raised when a scenario cannot satisfy the public scenario contract."""


def load_scenario(path: Path) -> Scenario:
    """Load one YAML scenario and expose validation errors as a stable domain error."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ScenarioValidationError(f"unable to load scenario: {path}") from error
    if not isinstance(loaded, dict):
        raise ScenarioValidationError("scenario root must be a mapping")
    try:
        return Scenario.model_validate(loaded)
    except ValidationError as error:
        raise ScenarioValidationError("scenario validation failed") from error


def scenario_sha256(path: Path) -> str:
    """Return the SHA-256 digest over the scenario file's unmodified bytes."""
    return sha256(path.read_bytes()).hexdigest()
