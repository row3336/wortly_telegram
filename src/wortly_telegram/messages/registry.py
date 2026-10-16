"""Registry of job templates, found by scanning wortly_telegram.messages (design T-D §4.3, ВП-21).

A package that adds a job kind adds one module next to this one and edits no shared
list (T2, T3, T4). The module declares ``TEMPLATE = Template(...)`` for the jobs.kind
it renders. Modules whose name starts with "_" are helpers; the registry's own modules
(registry, plural) are skipped by name. Any other module without a Template stops the
start instead of being ignored, and so does a kind declared twice (as jobs/kinds.py).

``prepare`` turns the facts of a committed snapshot into what the channel sends. Every
fault on the way (an envelope of another schema or kind, an unknown language, a
recipient that is not a Telegram ID, a template that cannot render) raises RenderError
before any network call; the channel reports it as pre_send_error (§3.3, Д4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
import json
import pkgutil
from typing import Any

from wortly.identity.principal import is_telegram_id

from . import LANGUAGES, Language, OutgoingMessage, RenderError, Template


PACKAGE = "wortly_telegram.messages"
_FRAMEWORK_MODULES = frozenset({"registry", "plural"})

# The envelope number builders write by default (ВП-17; core jobs/builders/*.py, ENVELOPE_SCHEMA of each builder).
# A kind whose builder writes another number declares it in its Template (``schema``).
ENVELOPE_SCHEMA = 1


class TemplateRegistryError(RuntimeError):
    """The template modules do not describe a usable set of kinds; the role cannot start."""


@dataclass(frozen=True)
class Prepared:
    """One rendered delivery: the private chat of the recipient and the message."""

    chat_id: int = field(repr=False)
    language: Language
    message: OutgoingMessage


def _mapping(value: Any, name: str, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RenderError(f"The delivery facts' {name} must be an object", reason=reason)
    return value


def _chat_id(recipient: Mapping[str, Any]) -> int:
    value = recipient.get("telegram_user_id")
    if not is_telegram_id(value):
        raise RenderError("The delivery facts name no Telegram recipient", reason="recipient_invalid")
    return int(value)


class TemplateRegistry:
    def __init__(self, templates: Iterable[Template]) -> None:
        self._templates: dict[str, Template] = {}
        for template in templates:
            if not isinstance(template, Template):
                raise TemplateRegistryError("The registry holds Template values only")
            if template.kind in self._templates:
                raise TemplateRegistryError(f"Template for {template.kind!r} is declared twice")
            self._templates[template.kind] = template

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._templates)

    def template(self, kind: str) -> Template:
        try:
            return self._templates[kind]
        except (KeyError, TypeError):
            raise RenderError("No template for this job kind", reason="unknown_kind") from None

    def missing(self, kinds: Iterable[str]) -> frozenset[str]:
        """Job kinds the core can deliver that have no template here."""
        return frozenset(kinds) - self.kinds

    def prepare(self, kind: str, facts_json: str) -> Prepared:
        """Render the committed facts of one attempt; raise RenderError before any network call."""
        template = self.template(kind)
        try:
            facts = json.loads(facts_json)
        except (TypeError, ValueError):
            raise RenderError("The delivery facts are not JSON", reason="not_json") from None
        facts = _mapping(facts, "envelope", "envelope_shape")
        if facts.get("schema") != template.schema or type(facts.get("schema")) is not int:
            raise RenderError("The delivery facts use an unsupported envelope schema", reason="schema_mismatch")
        if facts.get("kind") != kind:
            raise RenderError("The delivery facts belong to another job kind", reason="kind_mismatch")
        language = facts.get("ui_language")
        if type(language) is not str or language not in LANGUAGES:
            # Never substitute another language: the recipient chose this one (§4.2).
            raise RenderError("The delivery facts name an unsupported language", reason="language_unsupported")
        chat_id = _chat_id(_mapping(facts.get("recipient"), "recipient", "recipient_invalid"))
        body = _mapping(facts.get("body"), "body", "body_invalid")
        try:
            message = template.render(body, language)
        except RenderError:
            raise
        except Exception as error:  # noqa: BLE001 - any template fault happens before the network
            raise RenderError(f"The template failed: {type(error).__name__}", reason="template_fault") from None
        if not isinstance(message, OutgoingMessage):
            raise RenderError("The template returned no OutgoingMessage", reason="template_fault")
        return Prepared(chat_id, language, message)


def discover(package: str = PACKAGE) -> TemplateRegistry:
    """Import every template module of ``package`` and collect its Template."""
    root = import_module(package)
    templates = []
    for module in sorted(pkgutil.iter_modules(root.__path__), key=lambda item: item.name):
        if module.name.startswith("_") or (package == PACKAGE and module.name in _FRAMEWORK_MODULES):
            continue
        name = f"{package}.{module.name}"
        declared = getattr(import_module(name), "TEMPLATE", None)
        if not isinstance(declared, Template):
            raise TemplateRegistryError(f"Template module {name} declares no TEMPLATE")
        templates.append(declared)
    return TemplateRegistry(templates)
