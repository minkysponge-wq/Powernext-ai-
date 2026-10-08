"""Local certificate signing checks; self-signed fixtures are not CPRI identity proof."""

import tempfile
from io import StringIO
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase, override_settings
from django.core.management import call_command
from pyhanko.keys import load_certs_from_pemder
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext
from reportlab.pdfgen import canvas

from .pdf_signing import SigningConfigurationError, sign_issued_pdf


def demo_identity(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Demo HoD (untrusted)")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    p12 = Path(directory) / "demo-hod.p12"
    p12.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"Demo HoD",
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(b"fixture-password"),
        )
    )
    pem = Path(directory) / "demo-hod.pem"
    pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return p12, pem


def sample_pdf():
    output = BytesIO()
    page = canvas.Canvas(output)
    page.drawString(50, 750, "Synthetic report for signature integration test")
    page.save()
    return output.getvalue()


class PdfSigningTests(SimpleTestCase):
    def test_create_demo_signer_is_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            DEBUG=True, BASE_DIR=Path(directory) / "app"
        ):
            output = StringIO()
            call_command("create_demo_signer", stdout=output)
            p12 = Path(directory) / "private" / "demo-signing" / "vectorlab-demo.p12"
            self.assertTrue(p12.is_file())
            password = next(
                line.split("=", 1)[1]
                for line in output.getvalue().splitlines()
                if line.startswith("PDF_SIGNING_P12_PASSWORD=")
            )
            key, certificate, _ = pkcs12.load_key_and_certificates(
                p12.read_bytes(), password.encode()
            )
            self.assertIsNotNone(key)
            self.assertIn("VectorLab DEMO", certificate.subject.rfc4514_string())
            with self.assertRaisesMessage(Exception, "already exists"):
                call_command("create_demo_signer", stdout=StringIO())

    @override_settings(PDF_SIGNING_P12_PATH="", PDF_SIGNING_P12_PASSWORD="")
    def test_unconfigured_signer_blocks_issue(self):
        with self.assertRaisesRegex(SigningConfigurationError, "PDF signer is not configured"):
            sign_issued_pdf(sample_pdf(), "HoD")

    def test_configured_signer_writes_verifiable_pdf_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            p12, pem = demo_identity(directory)
            with override_settings(
                PDF_SIGNING_P12_PATH=str(p12), PDF_SIGNING_P12_PASSWORD="fixture-password"
            ):
                signed, enabled = sign_issued_pdf(sample_pdf(), "Demo HoD")
            self.assertTrue(enabled)
            reader = PdfFileReader(BytesIO(signed))
            self.assertEqual(len(reader.embedded_signatures), 1)
            context = ValidationContext(trust_roots=list(load_certs_from_pemder([str(pem)])))
            status = validate_pdf_signature(reader.embedded_signatures[0], context)
            self.assertTrue(status.intact)
            self.assertTrue(status.valid)

    @override_settings(
        PDF_SIGNING_P12_PATH="missing.p12", PDF_SIGNING_P12_PASSWORD="fixture-password"
    )
    def test_missing_identity_fails_closed(self):
        with self.assertRaises(SigningConfigurationError):
            sign_issued_pdf(sample_pdf(), "HoD")
