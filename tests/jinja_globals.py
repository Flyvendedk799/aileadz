"""Template globals the real app registers (run.create_app) for tests that render
templates through a bare ``jinja2.Environment``."""

import os

import asset_version
import capabilities
import order_lifecycle

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def add_app_globals(env):
    env.globals.setdefault("asset_version", lambda filename: asset_version.asset_version(_STATIC, filename))
    env.globals.setdefault("can", capabilities.can)
    env.globals.setdefault("has_endpoint", lambda name: False)
    env.globals.setdefault("credit_chip", lambda: {"scope": "personal", "balance": 0, "label": "0"})
    env.globals.setdefault("order_status_label", order_lifecycle.status_label)
    env.globals.setdefault("order_status_tone", order_lifecycle.status_tone)
    env.globals.setdefault("order_billing_label", order_lifecycle.billing_label)
    env.globals.setdefault("order_billing_tone", order_lifecycle.billing_tone)
    env.globals.setdefault("order_status_choices", order_lifecycle.status_choices)
    return env
