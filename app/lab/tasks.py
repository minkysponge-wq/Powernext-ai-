import hashlib
import inspect
import json

from django.db import transaction
from django.db.models import F

from .extraction import get_provider, get_schema
from .extraction.gemini import ExtractionError
from .extraction.rendering import render_page
from .models import AuditEvent, Document, Field, Job
from .report_workflow import job_locked


def inspect_document(document_id):
    doc = Document.objects.select_related("job").get(pk=document_id)
    if doc.status == "ready" and (not doc.form_type or doc.extraction_data):
        return "Already processed"
    try:
        from .pdf_probe import MAX_PDF_BYTES
        from .pdf_safety import probe_pdf

        with doc.file.open("rb") as stream:
            content = stream.read(MAX_PDF_BYTES + 1)
        result = probe_pdf(content, include_text=True)
        if result["pages"] != doc.page_count:
            raise ValueError("Source PDF page count changed since upload.")
        text = result["text"]
        if doc.form_type:
            return extract_document(doc, text)
        note = (
            "Text layer available. Confirm readings against the source."
            if text.strip()
            else "Scanned source. Automatic handwriting extraction is not enabled for this document; choose a form to extract."
        )
        Document.objects.filter(pk=doc.pk).update(
            status="ready", extracted_text=text, processing_note=note
        )
        return note
    except Exception as error:
        note = (
            str(error)
            if isinstance(error, ExtractionError)
            else "Could not process this PDF. Check the file and retry."
        )
        Document.objects.filter(pk=doc.pk).update(status="failed", processing_note=note[:300])
        # A visible retry button allows recovery without repeatedly billing a failed request.
        return note


def extract_document(doc, text):
    schema = get_schema(doc.form_type)
    if doc.page_count != schema["pages"]:
        raise ExtractionError(
            f"This form expects {schema['pages']} pages; uploaded document has {doc.page_count}."
        )
    provider = get_provider()
    from django.conf import settings

    from .extraction.gemini import PROMPT

    signature = hashlib.sha256(
        json.dumps(
            {
                "source": doc.sha256,
                "schema": schema,
                "provider": provider.name,
                "model": getattr(provider, "model", None),
                "local_revision": getattr(settings, "LOCAL_MODEL_REVISION", ""),
                "prompt": PROMPT,
                "adapter": inspect.getsource(type(provider)),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    checkpoint = doc.extraction_checkpoint
    saved = checkpoint.get("pages", {}) if checkpoint.get("signature") == signature else {}
    pages = []
    for number in range(1, doc.page_count + 1):
        image = render_page(doc.file.path, number)
        image_hash = hashlib.sha256(image).hexdigest()
        page = saved.get(str(number))
        if page is not None and page.get("metadata", {}).get("image_sha256") != image_hash:
            page = None
        if page is None:
            page = provider.extract_page(image, schema, number)
            page["metadata"]["image_sha256"] = image_hash
            saved[str(number)] = page
            Document.objects.filter(pk=doc.pk).update(
                extraction_checkpoint={"signature": signature, "pages": saved},
                processing_note=f"{len(saved)} of {doc.page_count} pages extracted. Readings will appear when the document is complete.",
            )
        pages.append(page)
    return import_pages(doc, pages, text)


def import_pages(doc, pages, text="", entry_method="automatic extraction"):
    schema = get_schema(doc.form_type)
    labels = {(f["page"], f["key"]): f["label"] for f in schema["fields"]}
    created = 0
    with transaction.atomic():
        current_job = Job.objects.select_for_update().get(pk=doc.job_id)
        if job_locked(current_job):
            raise ExtractionError(
                "Report data was locked while extraction ran. No readings were imported."
            )
        current = Document.objects.select_for_update().get(pk=doc.pk)
        if current.extraction_data:
            return "Already extracted"
        if current.form_type != doc.form_type:
            raise ExtractionError(
                "The selected form changed during extraction. Retry using the current form."
            )
        for page in pages:
            for item in page["fields"]:
                key = f"{doc.pk.hex}.{doc.form_type}.p{item['page']}.{item['key']}"
                _, added = Field.objects.get_or_create(
                    job_id=doc.job_id,
                    key=key,
                    defaults={
                        "label": labels[(item["page"], item["key"])],
                        "value": item["raw_text"],
                        "raw_value": item["raw_text"],
                        "unit": item["unit"],
                        "origin": "scan",
                        "document": doc,
                        "page": item["page"],
                        "bbox": item["bbox"],
                        "status": "unreviewed",
                        "updated_by_id": doc.job.owner_id,
                        "context": {
                            "entry_method": entry_method,
                            "extraction_status": item["status"],
                            "extraction_issue": item.get("issue", ""),
                            "schema_key": item["key"],
                            "form_type": doc.form_type,
                            **page["metadata"],
                        },
                    },
                )
                created += added
        Document.objects.filter(pk=doc.pk).update(
            status="ready",
            extracted_text=text,
            extraction_data={"pages": pages},
            processing_note=f"{created} fields extracted. Review against the source before using results.",
        )
        Job.objects.filter(pk=doc.job_id).update(version=F("version") + 1)
        AuditEvent.objects.create(
            job_id=doc.job_id,
            actor=None,
            action="fields_extracted",
            details={
                "document": str(doc.pk),
                "created": created,
                "provider": pages[0]["metadata"]["provider"],
                "entry_method": entry_method,
            },
        )
    return f"{created} fields extracted"
