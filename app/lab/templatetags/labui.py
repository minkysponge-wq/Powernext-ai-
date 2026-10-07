import re

from django import template

register = template.Library()


@register.filter
def readable(value):
    text = str(value or "").replace("_", " ")
    text = re.sub(r"\.(\d+)\.", lambda m: f" · row {int(m[1])+1} · ", text)
    text = text.replace(".", " · ")
    aliases = {
        "rated hv": "Rated HV voltage",
        "rated lv": "Rated LV voltage",
        "rated power": "Rated power",
        "test series": "Test series",
        "serial number": "Serial number",
        "vector group": "Vector group",
        "customer request": "Customer request",
        "transformer proforma": "Transformer proforma",
        "loss calculation": "Loss calculation",
        "routine test": "No-load current test",
        "pressure oil leakage": "Oil leakage & pressure test",
        "job created": "Job created",
        "reading updated": "Reading reviewed",
        "draft snapshot created": "Report draft created",
        "fields extracted": "Readings extracted",
        "unverified reference imported": "Reference forms imported",
    }
    return aliases.get(text.lower(), text[:1].upper() + text[1:])
