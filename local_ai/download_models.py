"""Download public weights; pin their exact upstream revisions for later offline use."""
import json
import hashlib
from pathlib import Path
from urllib.request import urlopen
from runtime import MODELS
from huggingface_hub import HfApi, snapshot_download

manifest = {}
for name, repo in MODELS.items():
    info = HfApi().model_info(repo, files_metadata=True)
    revision = info.sha
    folder = Path(snapshot_download(repo, revision=revision, allow_patterns=['*.json', '*.txt', '*.jinja', 'LICENSE*'], max_workers=3))
    # Ordinary HTTPS avoids a stalled Xet transport on some Windows networks.
    for entry in info.siblings:
        if not entry.rfilename.endswith('.safetensors'):
            continue
        target = folder / entry.rfilename
        expected = entry.lfs.sha256
        if target.exists():
            with target.open('rb') as existing:
                if hashlib.file_digest(existing, 'sha256').hexdigest() == expected:
                    continue
        partial = target.with_suffix('.partial')
        digest = hashlib.sha256()
        total = 0
        with urlopen(f'https://huggingface.co/{repo}/resolve/{revision}/{entry.rfilename}?download=true', timeout=60) as response, partial.open('wb') as output:
            while chunk := response.read(8*1024*1024):
                output.write(chunk); digest.update(chunk); total += len(chunk)
                if total % (256*1024*1024) == 0:
                    print(f'{name}: {total//1024//1024} MB downloaded', flush=True)
        if digest.hexdigest() != expected:
            raise ValueError('Model checksum mismatch')
        partial.replace(target)
    manifest[name] = {'repo': repo, 'revision': revision, 'license': 'Apache-2.0'}
    Path(__file__).with_name('models.json').write_text(json.dumps(manifest, indent=2))
    print(f'{name}: downloaded revision {revision}', flush=True)
