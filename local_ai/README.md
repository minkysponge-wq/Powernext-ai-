# Local AI for VectorLab

The model service runs separately from Django, on `127.0.0.1:8011` only. Images and
findings stay on the machine during inference. Downloading public weights requires
internet once; inference uses pinned cached revisions with `local_files_only=True`.
No reference transcriptions are read by the runtime or application provider.

## Installation

From `C:\hackathons\Track3`, using Python 3.12:

```powershell
python -m venv .venv-model
.\.venv-model\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.\.venv-model\Scripts\python.exe -m pip install "transformers>=5,<6" accelerate sentencepiece
.\.venv-model\Scripts\python.exe local_ai\download_models.py
.\.venv-model\Scripts\python.exe local_ai\server.py
```

The tested package versions are recorded separately in `requirements.lock.txt`.
`models.json` pins upstream model revisions. Model downloads use `local_ai/cache`.
No cloud API credential is needed for local inference. CPU execution is supported
by the code but has not been performance-qualified.

## Application configuration

Add these non-secret settings to `app/.env`, then restart Django and its Q worker:

```dotenv
EXTRACTION_PROVIDER=local_qwen
INSIGHT_PROVIDER=local_qwen
LOCAL_AI_TIMEOUT=300
```

Uploads with a selected form type use the existing extraction task. Schema-guided
readings, units and proposed source boxes enter review as **unreviewed**. Missing
readings remain explicit. Malformed or truncated responses fail without importing
partial document readings. The configured cloud provider is never used as a silent
fallback. Existing reviewed readings are not overwritten on retries.

The findings screen can also request local explanations of calculated evidence
checks. Referenced finding IDs are validated; this does not prove the wording is
factually correct. An engineer must check the explanation before report inclusion.
Calculations, conformity rules and final approval remain outside the neural model.

## Development comparison

With the service running:

```powershell
.\.venv\Scripts\python.exe benchmark\compare_local_vlm.py --model paddle --mode crop
.\.venv\Scripts\python.exe benchmark\compare_local_vlm.py --model qwen --mode crop
.\.venv\Scripts\python.exe benchmark\compare_local_vlm.py --model qwen --mode whole
```

The fixed 20-field pilot uses two previously inspected pages. The crop route uses
manually specified regions and is not an automatic page-alignment pipeline.
Predictions are saved before draft answers are read for scoring. These labels have
not been independently checked: report **draft agreement**, never validated accuracy.
Whole-page single-field reads and production batched structured extraction are
different tasks; both need evaluation. Cached development outputs are reused by the
comparison script, so archive the run directory before evaluating a changed prompt.

## Source models

- https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct
- https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6

Both upstream cards list Apache-2.0. Preserve upstream notices when redistributing.
Public benchmark results are not evidence of performance on CPRI handwritten forms.
