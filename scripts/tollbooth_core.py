"""tollbooth_core: what every gh-tollbooth process on this machine shares, starting with its settings.

Settings are TOLLBOOTH_* environment variables, read by pydantic-settings. A bad value is ignored with
one line on stderr and the default is used: a typo in one agent's environment must not stop its calls.
Not here: talking to GitHub (tollbooth_http) and the rules for one turn (spacing, blocks, budgets).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from pydantic import (AfterValidator, BeforeValidator, Field, ValidationError, ValidationInfo,
                      ValidatorFunctionWrapHandler, WrapValidator)
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("gh-tollbooth")
if not log.handlers:  # each notice is one "gh-tollbooth: ..." line on stderr
    _stderr = logging.StreamHandler()
    _stderr.setFormatter(logging.Formatter("gh-tollbooth: %(message)s"))
    log.addHandler(_stderr)
    log.setLevel(logging.INFO)
    log.propagate = False

DEFAULT_DEADLINE = 100     # seconds per GitHub call; under the Bash tool's 120 s default


# ---------- settings ----------

def _default_state_dir() -> Path:
    return Path.home() / ".cache" / "gh-tollbooth"


def _whole_seconds(value: Any) -> Any:
    """Digits only: pydantic alone would also take " 45", "4_5" and "45.0"."""
    if isinstance(value, str) and not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("not a whole number of seconds")
    return value


def _absolute(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("not an absolute path")
    return path


def _or_default(default: Callable[[], Any], why: str, shown: str, cut: int) -> WrapValidator:
    """Validate a setting; if it is bad, log one line (the value cut to `cut` characters) and use the default."""
    def validate(value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
        try:
            return handler(value)
        except ValidationError:
            log.warning("ignoring TOLLBOOTH_%s=%r (%s); using %s", str(info.field_name).upper(), str(value)[:cut],
                        why, shown)
            return default()
    return WrapValidator(validate)


class Settings(BaseSettings):
    """The TOLLBOOTH_* environment variables. An empty one counts as unset."""

    model_config = SettingsConfigDict(env_prefix="TOLLBOOTH_", env_ignore_empty=True)

    # seconds one GitHub call may take, waits included; 1 to 999,999
    deadline: Annotated[int, Field(ge=1, le=999_999), BeforeValidator(_whole_seconds),
                        _or_default(lambda: DEFAULT_DEADLINE, "not a whole number of seconds",
                                    str(DEFAULT_DEADLINE), 20)] = DEFAULT_DEADLINE
    # the folder every agent shares; an absolute path, so all agents find the same one
    state_dir: Annotated[Path, AfterValidator(_absolute),
                         _or_default(_default_state_dir, "not an absolute path", "~/.cache/gh-tollbooth", 40)
                         ] = Field(default_factory=_default_state_dir)


SETTINGS = Settings()
