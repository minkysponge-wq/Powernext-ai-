"""Exercise full synthetic approval with an ephemeral, untrusted demo signer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from lab.test_pdf_signing import demo_identity
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.keys import load_certs_from_pemder
from pyhanko_certvalidator import ValidationContext


def main():
    output = ROOT / 'output' / 'synthetic-acceptance'
    with tempfile.TemporaryDirectory(prefix='vectorlab-demo-signer-') as directory:
        p12, pem = demo_identity(directory)
        environment = os.environ.copy()
        environment.update(DJANGO_DEBUG='1', PDF_SIGNING_P12_PATH=str(p12),
                           PDF_SIGNING_P12_PASSWORD='fixture-password',
                           VECTORLAB_ACCEPTANCE_OUTPUT=str(output))
        completed = subprocess.run([sys.executable, 'manage.py', 'test', 'lab.test_acceptance', '--noinput'],
                                   cwd=ROOT / 'app', env=environment, check=False)
        if completed.returncode:
            return completed.returncode
        pdf_path = output / 'synthetic-approved-report.pdf'
        with pdf_path.open('rb') as pdf_file:
            reader = PdfFileReader(pdf_file)
            if len(reader.embedded_signatures) != 1:
                raise RuntimeError('Expected one signed PDF revision.')
            context = ValidationContext(trust_roots=list(load_certs_from_pemder([str(pem)])))
            result = validate_pdf_signature(reader.embedded_signatures[0], context)
            if not result.intact or not result.valid:
                raise RuntimeError('Demo PDF signature failed integrity validation.')
        (output / 'demo-signer-public.pem').write_bytes(pem.read_bytes())
        report = json.loads((output / 'acceptance-results.json').read_text(encoding='utf-8'))
        report.update(pdf_digitally_signed=True, signer='Ephemeral self-signed Demo HoD (untrusted outside this test)',
                      signature_integrity_verified=True, email_sent=False,
                      pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest())
        (output / 'acceptance-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'signed_pdf': str(pdf_path), 'signature_integrity_verified': True,
                          'signer_trust': 'Explicit demo-only public certificate; not CPRI PKI',
                          'email_sent': False}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
