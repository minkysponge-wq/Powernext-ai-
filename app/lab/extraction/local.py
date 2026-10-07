"""Schema-guided local vision extraction; all output still requires human review."""

import base64
import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .gemini import PROMPT, ExtractionError, validate_fields


def generate(prompt, image=None, max_tokens=2400):
    from django.conf import settings

    body = {"model": "qwen", "prompt": prompt, "max_tokens": max_tokens}
    if image is not None:
        body["image"] = base64.b64encode(image).decode()
    request = Request(
        "http://127.0.0.1:8011/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=settings.LOCAL_AI_TIMEOUT) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, ConnectionError):
        raise ExtractionError(
            "Local AI is unavailable. Start the local model service and retry."
        ) from None
    if result.get("truncated"):
        raise ExtractionError(
            "Local AI reached its output limit. No partial readings were imported."
        )
    try:
        text = result["text"].strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        payload = json.loads(text)
    except (KeyError, TypeError, ValueError):
        raise ExtractionError(
            "Local AI returned an incomplete response. Review the source or retry."
        ) from None
    return payload, {
        k: result.get(k) for k in ("model", "revision", "seconds", "tokens", "peak_gpu_mb")
    }


class LocalProvider:
    name = "local_qwen"

    def extract_page(self, image_bytes, schema, page):
        fields = [f for f in schema["fields"] if f["page"] == page and not f.get("digital_only")]
        readings, calls, hashes = [], [], []
        # Limit output size for dense tables. Each batch sees the original full page.
        for offset in range(0, len(fields), 10):
            batch = fields[offset : offset + 10]
            prompt = (
                PROMPT
                + json.dumps(
                    {
                        "form_type": schema["form_type"],
                        "page": page,
                        "instructions": schema.get("instructions", []),
                        "fields": batch,
                    }
                )
                + '\nJSON shape: {"fields":[{"key":"requested key","raw_text":"visible value",'
                '"unit":"visible unit or empty","status":"read|blank|illegible|struck_out|not_applicable",'
                '"box_2d":[0,0,1000,1000]}]}. Replace the example box with the actual value location.'
                " Table row indices in keys are zero-based. Do not include markdown or commentary."
            )
            payload, metadata = generate(prompt, image_bytes)
            validated = validate_fields(payload, {"fields": batch}, page)
            readings.extend(validated)
            calls.append(metadata)
            hashes.append(hashlib.sha256(prompt.encode()).hexdigest())
        return {
            "fields": readings,
            "metadata": {
                "provider": self.name,
                "model": calls[0]["model"] if calls else "",
                "model_version": calls[0]["revision"] if calls else "",
                "page": page,
                "seconds": sum(c["seconds"] or 0 for c in calls),
                "calls": calls,
                "prompt_sha256": hashes,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "schema_sha256": hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()
                ).hexdigest(),
                "evidence_note": "Model-proposed locations; confirm each reading and source box.",
            },
        }
