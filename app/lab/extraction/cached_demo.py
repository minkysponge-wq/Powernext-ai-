"""Exact-page replay of previously saved organiser responses for an offline demo.

This is not a new OCR result. Legacy replay is opt-in and only reuses exact
source-image matches; new schema fields are explicit abstentions.
The mode is unavailable when DEBUG is off and never contacts a cloud provider.
"""

import hashlib
import json
from pathlib import Path

from .gemini import PROMPT, ExtractionError


class CachedDemoProvider:
    name = "cached_demo"
    model = "saved-organiser-responses"

    def __init__(self, directory, allow_legacy=False):
        self.directory = Path(directory).resolve()
        self.allow_legacy = allow_legacy

    def extract_page(self, image, schema, page):
        form = schema["form_type"]
        saved = self.directory / f"{form}-p{page}.json"
        if not saved.is_file():
            raise ExtractionError("No exact saved extraction is available for this demo page.")
        try:
            result = json.loads(saved.read_text(encoding="utf-8"))
            fields = [
                field
                for field in schema["fields"]
                if field["page"] == page and not field.get("digital_only")
            ]
            prompt = PROMPT + json.dumps(
                {
                    "form_type": form,
                    "page": page,
                    "instructions": schema.get("instructions", []),
                    "fields": fields,
                },
                sort_keys=True,
            )
            expected = {
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "schema_sha256": hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()
                ).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
            if result["metadata"].get("image_sha256") != expected["image_sha256"]:
                raise ExtractionError("Saved extraction does not match this source image.")
            exact = all(result["metadata"].get(key) == value for key, value in expected.items())
            if exact:
                if len(result["fields"]) != len(fields) or any(
                    actual["key"] != configured["key"] or actual["page"] != page
                    for actual, configured in zip(result["fields"], fields)
                ):
                    raise ExtractionError("Saved extraction field order differs from this schema.")
            elif not self.allow_legacy:
                raise ExtractionError(
                    "Saved extraction does not match this page, schema, or prompt."
                )
            else:
                original = result["fields"]
                by_key = {item["key"]: item for item in original}
                if len(by_key) != len(original) or not set(by_key).issubset(
                    {field["key"] for field in fields}
                ):
                    raise ExtractionError("Legacy cache contains duplicate or unknown fields.")
                result["fields"] = [
                    by_key.get(
                        field["key"],
                        {
                            "key": field["key"],
                            "page": page,
                            "raw_text": "",
                            "unit": "",
                            "status": "missing",
                            "bbox": None,
                            "issue": "added_after_cached_extraction",
                        },
                    )
                    for field in fields
                ]
        except (OSError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, ExtractionError):
                raise
            raise ExtractionError("Saved extraction could not be validated.") from error
        result["metadata"] = {
            **result["metadata"],
            "provider": self.name,
            "replay_of_provider": result["metadata"].get("provider"),
            "demo_replay": True,
            "legacy_schema_replay": not exact,
            "schema_additions_abstained": sum(
                item.get("issue") == "added_after_cached_extraction" for item in result["fields"]
            ),
        }
        return result
