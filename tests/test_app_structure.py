"""FieldlogApp is assembled from one mixin per pane.

The split buys navigability: app.py holds the widget tree and the wiring, and
each pane's behaviour lives beside its own name. What must not drift is the
assembly — a mixin dropped from the bases takes its key bindings with it, and
every binding names an `action_*` that has to resolve on the class.
"""

from __future__ import annotations

import inspect

from textual.app import App

from fieldlog.app import FieldlogApp
from fieldlog.tui.args import ArgsBandMixin
from fieldlog.tui.catalog import CatalogMixin
from fieldlog.tui.jobs import JobsMixin
from fieldlog.tui.layout import LayoutMixin
from fieldlog.tui.tree import RecipeTreeMixin
from fieldlog.tui.variants import VariantsPaneMixin

MIXINS = [CatalogMixin, RecipeTreeMixin, VariantsPaneMixin, ArgsBandMixin, JobsMixin, LayoutMixin]


def test_every_mixin_is_in_the_bases():
    for mixin in MIXINS:
        assert mixin in FieldlogApp.__mro__, f"{mixin.__name__} is not mixed in"


def test_no_two_mixins_claim_the_same_name():
    """Nothing here relies on MRO order, and this is what keeps that true."""
    seen: dict[str, str] = {}
    for mixin in MIXINS:
        for name, value in vars(mixin).items():
            if name.startswith("__") or not callable(value) and not isinstance(value, property):
                continue
            assert name not in seen, f"{name} defined by both {seen[name]} and {mixin.__name__}"
            seen[name] = mixin.__name__


def test_every_binding_resolves_to_an_action():
    """A pane that fell out of the bases shows up here first."""
    for binding in FieldlogApp.BINDINGS:
        action = binding[1] if isinstance(binding, tuple) else binding.action
        assert callable(getattr(FieldlogApp, f"action_{action}", None)), \
            f"binding '{action}' has no action_{action}"


def test_the_mixins_are_not_apps():
    """They are behaviour attached to the app, not screens or widgets of their
    own — `self` is FieldlogApp throughout."""
    for mixin in MIXINS:
        assert mixin.__bases__ == (object,), f"{mixin.__name__} grew a base class"
        assert not issubclass(mixin, App)


def test_app_py_stays_a_composition_file():
    """A guard rail, not a style rule: the point of the split was that no single
    file holds every pane. Raise it deliberately if the widget tree grows."""
    import fieldlog.app as app_mod

    source = inspect.getsource(app_mod)
    assert len(source.splitlines()) < 1100
