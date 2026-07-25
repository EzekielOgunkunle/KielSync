"""The guardrail that keeps kielsync.core framework-independent.

Nothing in this file tests behaviour. It tests a structural promise: that
kielsync.core can be imported, reasoned about, and reused in a process
that has never heard of Django. That promise is easy to make and easy to
break by accident — one convenience import of django.utils.timezone in an
adapter and the core is no longer portable, with nothing failing to say
so until someone tries to use it outside Django.

The check reads source rather than importing it, so a module that would
only reveal the dependency at call time is still caught.
"""

import ast
import pathlib

import pytest

import kielsync.core

CORE_ROOT = pathlib.Path(kielsync.core.__file__).parent

# kielsync.django is banned alongside django itself: importing the Django
# integration layer from core would invert the dependency and drag Django
# in transitively, which is the same breakage by a longer route.
FORBIDDEN_ROOTS = ("django", "kielsync.django")


def core_modules():
    return sorted(CORE_ROOT.rglob("*.py"))


def _is_forbidden(dotted_name):
    return any(
        dotted_name == root or dotted_name.startswith(f"{root}.")
        for root in FORBIDDEN_ROOTS
    )


def _module_name(path):
    """Reconstruct the dotted module name so relative imports can resolve."""
    relative = path.relative_to(CORE_ROOT.parent.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree, module_name):
    """Yield every dotted name a module imports, with relative ones resolved."""
    package = module_name.rsplit(".", 1)[0] if "." in module_name else module_name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from ..y import z` — walk up from the
                # containing package so the name compares like an absolute one.
                base = package.split(".")
                ascend = node.level - 1
                base = base[: len(base) - ascend] if ascend else base
                prefix = ".".join(base)
                yield f"{prefix}.{node.module}" if node.module else prefix
            elif node.module:
                yield node.module


def test_core_package_contains_modules():
    """Guard the guard: a glob that matches nothing would pass vacuously."""
    assert len(core_modules()) >= 5


@pytest.mark.parametrize(
    "path", core_modules(), ids=lambda p: str(p.relative_to(CORE_ROOT))
)
def test_core_module_does_not_import_django(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders = sorted(
        {
            name
            for name in _imported_names(tree, _module_name(path))
            if _is_forbidden(name)
        }
    )
    assert not offenders, (
        f"{path.relative_to(CORE_ROOT)} imports {offenders}. "
        f"kielsync.core must stay pure Python: move the Django-dependent "
        f"code into kielsync.django and have it call into core instead."
    )


def test_detector_recognises_a_django_import():
    """The check must actually fail on the thing it claims to catch."""
    samples = [
        "import django",
        "import django.db",
        "from django.db import models",
        "from django.conf import settings",
        "from kielsync.django.models import Transaction",
    ]
    for source in samples:
        tree = ast.parse(source)
        names = list(_imported_names(tree, "kielsync.core.gateways.paystack"))
        assert any(_is_forbidden(name) for name in names), source


def test_detector_allows_core_and_stdlib_imports():
    samples = [
        "import json",
        "import httpx",
        "from kielsync.core.errors import classify",
        "from kielsync.core.gateways.base import Gateway",
        "from . import base",
        "from .base import Gateway",
    ]
    for source in samples:
        tree = ast.parse(source)
        names = list(_imported_names(tree, "kielsync.core.gateways.paystack"))
        assert not any(_is_forbidden(name) for name in names), source
