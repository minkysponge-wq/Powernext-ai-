"""Image-to-field providers. Reference answers are never imported here."""

import json
from pathlib import Path


def schemas():
    return json.loads(Path(__file__).with_name("form_schemas.json").read_text(encoding="utf-8"))


def get_schema(form_type):
    for schema in schemas():
        if schema["form_type"] == form_type:
            return schema
    raise ValueError("Choose a supported form type.")


def get_provider():
    from django.conf import settings

    if settings.EXTRACTION_PROVIDER == "cached_demo":
        if not settings.DEBUG or settings.DEMO_EXTRACTION_CACHE is None:
            from .gemini import ExtractionError

            raise ExtractionError(
                "Cached extraction replay is permitted only in an explicit local demo."
            )
        from .cached_demo import CachedDemoProvider

        return CachedDemoProvider(
            settings.DEMO_EXTRACTION_CACHE, allow_legacy=settings.DEMO_ALLOW_LEGACY_CACHE
        )
    if settings.EXTRACTION_PROVIDER == "local_qwen":
        from .local import LocalProvider

        return LocalProvider()
    if settings.EXTRACTION_PROVIDER == "gemini":
        if not settings.DEBUG and not settings.CLOUD_AI_ALLOWED:
            from .gemini import ExtractionError

            raise ExtractionError(
                "Cloud AI is disabled for production source data. Set CLOUD_AI_ALLOWED=1 only after lab authorisation."
            )
        from .gemini import GeminiProvider

        if not settings.PAID_AI_ALLOWED:
            from .gemini import ExtractionError

            raise ExtractionError(
                "Paid Gemini requests are off. Enable PAID_AI_ALLOWED=1 only for a budgeted run."
            )
        return GeminiProvider(
            settings.GEMINI_API_KEY, settings.GEMINI_MODEL, settings.GEMINI_THINKING_LEVEL
        )
    raise ValueError("Extraction provider is not configured.")
