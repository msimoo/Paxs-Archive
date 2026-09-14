"""Tests for DICOM TLS on the receiver — Phase B2.

Real encrypted associations over pynetdicom with a freshly generated
self-signed certificate:

  * C-ECHO succeeds over TLS when the client pins the server certificate
  * C-STORE delivers an instance over the encrypted channel (full pipeline)
  * a plaintext association to the TLS port is refused
  * a client that does not trust the self-signed cert is refused (real TLS
    authentication, not just an encrypted socket)
  * self-signed certificate generation is idempotent and non-interactive
"""

import shutil
import ssl
import tempfile
import time
from pathlib import Path

import pytest

def _has_tls_cert_tool():
    if shutil.which("openssl"):
        return True
    # Fallback used by ensure_certificate when openssl is missing from PATH.
    try:
        import importlib.util
        return importlib.util.find_spec("cryptography") is not None
    except ImportError:
        return False

pytestmark = pytest.mark.skipif(
    not _has_tls_cert_tool(),
    reason="openssl binary or cryptography Python module required for DICOM TLS tests",
)

from pynetdicom import AE
from pynetdicom.sop_class import CTImageStorage, Verification
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.db.models import Instance
from app.db.session import SyncSessionLocal
from app.dicom.tls import ensure_certificate
from tests.conftest import free_port
from tests.helpers import ds_bytes, make_instance

SERVER_HOST = "127.0.0.1"


def _client_ctx(server_cert: str) -> ssl.SSLContext:
    """Client context that pins the server's self-signed certificate."""
    ctx = ssl.create_default_context(cafile=server_cert)
    ctx.check_hostname = False  # self-signed cert has no matching SAN
    return ctx


@pytest.fixture()
def tls_receiver(schema):
    """Start the DICOM SCP wrapped in TLS with a temp self-signed cert.

    Yields (port, server_cert_path). Truncates the DB first so instance-count
    assertions are deterministic.
    """
    from app.db.models import Base
    from app.db.session import sync_engine
    from app.dicom.receiver import shutdown_receiver, start_receiver

    tmp = Path(tempfile.mkdtemp(prefix="paxs_tls_"))
    cert, key = ensure_certificate(tmp / "paxs.crt", tmp / "paxs.key")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)

    with sync_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    port = free_port()
    start_receiver(SERVER_HOST, port, ae_title="PAXS_TLS_SCP", tls_context=ctx)
    yield port, str(cert)
    shutdown_receiver()


def _echo(port: int, server_cert: str | None) -> int:
    """C-ECHO against `port`. Returns the Status code, or -1 if not established."""
    ae = AE(ae_title="PAXS_TLS_SCU")
    ae.add_requested_context(Verification, ExplicitVRLittleEndian)
    tls_args = None
    if server_cert is not None:
        tls_args = (_client_ctx(server_cert), SERVER_HOST)
    assoc = ae.associate(SERVER_HOST, port, ae_title="PAXS_TLS_SCP", tls_args=tls_args)
    if not assoc.is_established:
        assoc.release()
        return -1
    status = assoc.send_c_echo()
    assoc.release()
    return int(status.Status) if status is not None else -1


def _store(port: int, server_cert: str, data: bytes) -> int:
    ae = AE(ae_title="PAXS_TLS_SCU")
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    assoc = ae.associate(
        SERVER_HOST, port, ae_title="PAXS_TLS_SCP",
        tls_args=(_client_ctx(server_cert), SERVER_HOST),
    )
    assert assoc.is_established, "TLS association failed"
    import io

    import pydicom

    status = assoc.send_c_store(pydicom.dcmread(io.BytesIO(data)), msg_id=1)
    assoc.release()
    return int(status.Status) if status is not None else -1


def _instance_exists(sop_uid: str) -> bool:
    with SyncSessionLocal() as s:
        return s.query(Instance).filter(Instance.sop_instance_uid == sop_uid).count() == 1


# --------------------------------------------------------------------------- #
# Real encrypted DIMSE
# --------------------------------------------------------------------------- #
def test_tls_c_echo_encrypted(tls_receiver):
    port, cert = tls_receiver
    assert _echo(port, cert) == 0x0000


def test_tls_c_store_encrypted_full_pipeline(tls_receiver):
    port, cert = tls_receiver
    sop = generate_uid()
    ds = make_instance(study_uid="1.2.840.113619.2.408.400",
                       series_uid="1.2.3.4.400", sop_uid=sop)
    assert _store(port, cert, ds_bytes(ds)) == 0x0000
    for _ in range(50):
        if _instance_exists(sop):
            break
        time.sleep(0.1)
    assert _instance_exists(sop), "instance not indexed over the TLS channel"


def test_plaintext_association_to_tls_port_is_refused(tls_receiver):
    port, _ = tls_receiver
    # no tls_args -> plaintext TCP to a TLS port must fail to associate
    assert _echo(port, None) == -1


def test_untrusted_client_is_refused(tls_receiver):
    port, _ = tls_receiver
    # trust the system CAs only — the self-signed server cert must be rejected
    # at the TLS handshake, proving real server authentication
    ae = AE(ae_title="PAXS_TLS_SCU")
    ae.add_requested_context(Verification, ExplicitVRLittleEndian)
    ctx = ssl.create_default_context()  # does NOT trust our self-signed cert
    assoc = ae.associate(
        SERVER_HOST, port, ae_title="PAXS_TLS_SCP",
        tls_args=(ctx, SERVER_HOST),
    )
    assert not assoc.is_established
    assoc.release()


# --------------------------------------------------------------------------- #
# Certificate bootstrap
# --------------------------------------------------------------------------- #
def test_self_signed_certificate_generation():
    tmp = Path(tempfile.mkdtemp(prefix="paxs_cert_"))
    cert, key = ensure_certificate(tmp / "srv.crt", tmp / "srv.key")
    assert Path(cert).exists() and Path(key).exists()
    # idempotent — calling again with existing files does not regenerate
    import os

    cert_mtime = os.path.getmtime(cert)
    ensure_certificate(cert, key)
    assert os.path.getmtime(cert) == cert_mtime
    # the certificate actually loads into a context (valid PEM + key pair)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
