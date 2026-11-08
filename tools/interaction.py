"""Presentation-neutral workflow interactions; importing this module needs only stdlib."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class OperationPaused(Exception):
    """The owner requested a stop at a durable, safe boundary."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    default: Any = ""
    kind: str = "text"  # text, multiline, bool, select
    choices: tuple[tuple[str, str], ...] = ()  # (label, value)
    required: bool = True


_current: ContextVar[Any] = ContextVar("maven_interaction", default=None)


def current():
    return _current.get()


@contextmanager
def using(interaction):
    token = _current.set(interaction)
    try:
        yield interaction
    finally:
        _current.reset(token)


def checkpoint():
    interaction = current()
    if interaction is not None:
        interaction.checkpoint()


def message(text):
    interaction = current()
    if interaction is None:
        print(text)
    else:
        interaction.message(str(text))


def stage(name):
    interaction = current()
    if interaction is None:
        print(name)
    else:
        interaction.stage(name)


def require():
    interaction = current()
    if interaction is None:
        raise OperationPaused("Interactive choices require the MAVEN TUI; use explicit noninteractive options otherwise.")
    return interaction


def confirmation_prefix(identity: str) -> str:
    if not isinstance(identity, str) or not identity:
        raise ValueError("A destructive approval requires the complete target identity")
    return identity[:6]
