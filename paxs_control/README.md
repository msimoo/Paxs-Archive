# PAXS Control for Windows

Place `PAXS-Control.exe`, `PAXSViewer.exe`, and the `PAXS-Server` distribution
directory together. The control application uses the packaged server and does
not require Python on the target computer. The viewer requires Microsoft Edge
WebView2 Runtime.

Before starting the server, create `PAXS-Server/.env` using the supplied
`.env.example` as a reference. Configure the database, storage, credentials,
network access, and production security for the installation. Existing local
configuration and patient data are not copied into the package. The operator is
responsible for database provisioning and backups.

Start `PAXS-Control.exe`, then select **Start PAXS Server** and **Open PAXS
Viewer**. The viewer may ask for the server address and user login. Closing the
control window does not stop the server. The server runs in its own console;
use its normal shutdown procedure when work is complete.

By default the display probes loopback HTTP port 8000, DICOM port 10400,
and Redis port 6379. Configure different monitoring targets in the dialog.
Redis is checked with an unauthenticated PING. An authenticated
Redis server reports `AUTH REQUIRED`; configure authenticated health checks
separately. A DICOM TCP listener is not a protocol health check.
Viewer HTTP availability does not establish successful image rendering. The
server readiness endpoint reports database availability; it does not establish
storage, image integrity, or clinical readiness. Redis is optional and is not
installed or started by this control panel.

The **Monitoring targets** dialog saves non-secret API, DICOM and Redis targets
for the current Windows user. It does not edit the server's configuration; its
ports, database, storage, TLS and credentials must be configured separately.
Remote HTTP targets require HTTPS. Redis PING does not support credentials or
TLS. The DICOM check currently uses an unencrypted association with the calling
AE `PAXS_CONTROL`, so the SCP must permit that AE. C-ECHO sends a verification
request. C-FIND and MWL send unique synthetic empty queries; success does not
verify real study or worklist retrieval. Other listed services show only whether
their presentation contexts were negotiated. Negotiation does not establish that
those operations work. Overall status remains `INCOMPLETE` until every required
operation is actually tested and succeeds.
The CT storage context is one storage SOP class, not a complete storage test.
No C-STORE, MPPS, Storage Commitment, or STOW write is sent to the live target.

**Check DICOM** additionally runs eight fixed end-to-end tests using a temporary
SQLite database, temporary local storage, and loopback synthetic SCPs. It reports
each test and reports `ALL OK` only if every isolated test passes. This result
checks the source checkout, not the packaged or live server. The live check is
reported separately. This feature currently requires the PAXS source checkout
beside the distribution directory and a `python` executable with the project's
test dependencies installed; otherwise it reports `UNAVAILABLE`. It does not
provide a standalone test runner on a workstation without those dependencies.
