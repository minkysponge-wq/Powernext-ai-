"""Optional PDF byte-range signature; laboratory PKI remains an external dependency."""

from io import BytesIO
from pathlib import Path

from django.conf import settings
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import signers


class SigningConfigurationError(ValueError):
    pass


def sign_issued_pdf(unsigned_pdf, signer_name):
    """Sign with a configured PKCS#12 identity, or retain legacy unsigned mode."""
    path = settings.PDF_SIGNING_P12_PATH
    if not path:
        return unsigned_pdf, False
    certificate = Path(path)
    if not certificate.is_file():
        raise SigningConfigurationError("Configured PDF signing identity is unavailable.")
    password = settings.PDF_SIGNING_P12_PASSWORD
    if not password:
        raise SigningConfigurationError("PDF signing identity password is not configured.")
    try:
        signer = signers.SimpleSigner.load_pkcs12(str(certificate), passphrase=password.encode())
        if signer is None:
            raise SigningConfigurationError("PDF signing identity could not be loaded.")
        output = signers.sign_pdf(
            IncrementalPdfFileWriter(BytesIO(unsigned_pdf)),
            signature_meta=signers.PdfSignatureMetadata(
                field_name="HoDApproval",
                md_algorithm="sha256",
                name=signer_name,
                reason="Laboratory report approval",
            ),
            signer=signer,
        )
        return output.getvalue(), True
    except SigningConfigurationError:
        raise
    except Exception as error:
        raise SigningConfigurationError(
            "PDF signature could not be applied; report was not issued."
        ) from error
