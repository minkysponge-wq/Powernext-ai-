"""Create an untrusted local PKCS#12 signer for synthetic demonstrations."""

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a self-signed, untrusted VectorLab DEMO PDF signer in ignored private/."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo signer creation is local-only (DJANGO_DEBUG=1).")
        directory = Path(settings.BASE_DIR).parent / "private" / "demo-signing"
        identity = directory / "vectorlab-demo.p12"
        public = directory / "vectorlab-demo.pem"
        if identity.exists() or public.exists():
            raise CommandError("Demo signer already exists; refusing to overwrite its key.")
        directory.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        label = "VectorLab DEMO — not a CPRI identity"
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=30))
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
        password = secrets.token_urlsafe(36)
        identity.write_bytes(
            pkcs12.serialize_key_and_certificates(
                label.encode("utf-8"),
                key,
                certificate,
                None,
                serialization.BestAvailableEncryption(password.encode()),
            )
        )
        public.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        self.stdout.write("Self-signed DEMO identity created; no CPRI identity claim.")
        self.stdout.write(f"PDF_SIGNING_P12_PATH={identity}")
        self.stdout.write(f"PDF_SIGNING_P12_PASSWORD={password}")
        self.stdout.write(f"Public certificate: {public}")
