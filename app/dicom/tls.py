"""DICOM TLS support — self-signed certificate bootstrap + SSL contexts.

pynetdicom wraps DIMSE sockets in TLS via `ssl_context` (server side,
`AE.start_server`) and `tls_args=(context, server_hostname)` (SCU side,
`AE.associate`). This module:

  * generates a self-signed RSA certificate with `openssl` when the configured
    cert/key files are missing (zero extra Python deps; `cryptography` is not
    required)
  * builds the server SSLContext (optionally requiring + verifying client
    certificates for mutual TLS)
  * builds the client SSLContext used by the SCU helpers (node ping,
    forwarding) — trusted against the configured CA bundle or, for plain
    self-signed deployments, the server's own certificate

Security note: self-signed DICOM TLS protects *confidentiality* (AES over
TLS 1.2/1.3) but authenticates by trusting the exact server certificate.
For production, point DICOM_TLS_CA at your hospital CA and issue real
certificates — the code path is identical.
"""

from __future__ import annotations

import logging
import ssl
import subprocess
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("paxs.tls")

_TLS_MIN_VERSION = ssl.TLSVersion.TLSv1_2


def ensure_certificate(cert_path: str | Path, key_path: str | Path,
                       common_name: str = "PAXS_SCP") -> tuple[str, str]:
    """Generate a self-signed certificate+key with `openssl` if missing.

    Returns (cert_path, key_path) as strings. Raises RuntimeError when the
    files are missing and `openssl` is unavailable.
    """
    cert = Path(cert_path)
    key = Path(key_path)
    if cert.exists() and key.exists():
        # Validate the existing pair up front: a stale/mismatched cert+key
        # (e.g. regenerated key with an old cert) would otherwise surface as a
        # confusing load_cert_chain error at boot.
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
            return str(cert), str(key)
        except (ssl.SSLError, OSError) as exc:
            log.warning("DICOM TLS: existing cert/key pair is invalid (%s) — "
                        "regenerating", exc)
        cert.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
    elif cert.exists() != key.exists():
        log.warning("DICOM TLS: only one of %s / %s exists — regenerating both",
                    cert, key)
        cert.unlink(missing_ok=True)
        key.unlink(missing_ok=True)

    key.parent.mkdir(parents=True, exist_ok=True)
    # One RSA-2048 cert valid 10 years, no passphrase (a server key must be
    # loadable without prompting). `-subj`/`-addext` keep it fully
    # non-interactive; the SAN makes the dev cert verifiable by hostname too
    # (OpenSSL >= 1.1.1; a missing -addext fails loudly with a clear error).
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key), "-out", str(cert),
        "-days", "3650", "-nodes",
        "-subj", f"/CN={common_name}/O=PAXS Archive",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                check=False)
        if result.returncode == 0:
            log.warning("Generated self-signed DICOM TLS certificate -> %s (dev use; "
                        "point DICOM_TLS_CA at your hospital CA for production)", cert)
            return str(cert), str(key)
    except FileNotFoundError:
        pass

    # Fallback to Python cryptography module if openssl binary is missing from PATH
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PAXS Archive"),
        ])
        cert_builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        )
        certificate = cert_builder.sign(private_key, hashes.SHA256())

        key.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        log.warning("Generated self-signed DICOM TLS certificate via cryptography module -> %s", cert)
        return str(cert), str(key)
    except ImportError:
        raise RuntimeError(
            "DICOM TLS is enabled but no certificate exists at "
            f"{cert} and 'openssl' was not found on PATH. Install OpenSSL or 'cryptography'."
        ) from None


def server_context() -> ssl.SSLContext:
    """The receiver's TLS context: server auth + optional client cert verify."""
    cert, key = ensure_certificate(settings.DICOM_TLS_CERT, settings.DICOM_TLS_KEY)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _TLS_MIN_VERSION
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ca = settings.DICOM_TLS_CA
    if ca:
        ctx.load_verify_locations(cafile=ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
        log.info("DICOM TLS: client certificates required (CA=%s)", ca)
    return ctx


def client_context(trust_cert: str | Path | None = None) -> ssl.SSLContext:
    """The SCU's TLS context for connecting to a TLS peer.

    Verification: if DICOM_TLS_CA is set the server certificate is validated
    against the CA; otherwise the caller may pass `trust_cert` (the server's
    own self-signed certificate) to pin exactly that peer. With neither, the
    connection is encrypted but the server identity is not verified — logged
    as a warning (self-signed dev default).
    """
    ca = settings.DICOM_TLS_CA
    if ca and Path(ca).exists():
        ctx = ssl.create_default_context(cafile=ca)
        ctx.minimum_version = _TLS_MIN_VERSION
        return ctx
    if trust_cert is not None and Path(trust_cert).exists():
        ctx = ssl.create_default_context(cafile=str(trust_cert))
        ctx.minimum_version = _TLS_MIN_VERSION
        # Self-signed certs have no matching SAN for the loopback hostname.
        ctx.check_hostname = False
        return ctx
    log.warning("DICOM TLS: no CA/cert to verify the peer — encrypting without "
                "server authentication (set DICOM_TLS_CA for production)")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = _TLS_MIN_VERSION
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def scu_tls_args(peer_host: str) -> tuple[ssl.SSLContext, str] | None:
    """(context, server_hostname) for `AE.associate(..., tls_args=...)` when
    DICOM TLS is enabled, else None (plaintext).

    `peer_host` is the address the SCU actually connects to — it becomes the
    TLS server_hostname, so hostname verification (when a CA is configured)
    checks the certificate against the real peer, never against the local
    bind address.
    """
    if not settings.DICOM_TLS_ENABLED:
        return None
    return client_context(), str(peer_host)


def ensure_cert_paths_exist() -> None:
    """Warm the certificate up at boot so failures surface early (called from
    the receiver bootstrap when TLS is enabled)."""
    ensure_certificate(settings.DICOM_TLS_CERT, settings.DICOM_TLS_KEY)


# Re-export for convenience in tests.
generate_self_signed = ensure_certificate
