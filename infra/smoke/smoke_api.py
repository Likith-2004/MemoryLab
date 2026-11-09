"""SMOKE ONLY (mounted by infra/compose.smoke.yaml, never part of an image): the production API whose key validator accepts exactly the
one smoke key (the real validator needs a funded provider key, BLOCKED.md B-4). Every other path is the production code."""

import os

import api.main as prod


class SmokeKey:
    def validate(self, provider: str, api_key: str) -> bool:
        return api_key == os.environ["SMOKE_PROVIDER_KEY"]


prod.UnconfiguredKeyValidator = SmokeKey  # type: ignore[misc]
build = prod.build
