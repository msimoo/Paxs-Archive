# DCMTK integration

Install OFFIS DCMTK on the archive server. Keep the tools, dictionaries, and shared libraries together in the vendor distribution. Set `DCMTK_BIN_DIR` in the server's `.env` or service environment to the absolute path of its `bin` directory, then restart the archive. The server account needs permission to run the tools. The browser workstation needs no DCMTK installation.

Use the [official DCMTK releases](https://github.com/DCMTK/dcmtk/releases) or a trusted operating system package. On a packaged PAXS Server deployment, install DCMTK separately and configure the absolute path under the service account.

## Archive workflows

| Operation | OFFIS tool | PAXS workflow |
|---|---|---|
| C-ECHO | `echoscu` | **Nodes → Ping** |
| C-FIND | `findscu` | **Nodes → Find**, up to 200 matches per search |
| C-STORE | `dcmsend` | **Transfer Jobs → Send** and the registered-node send API |
| C-MOVE | `movescu` + `dcmrecv` | **Nodes → C-MOVE** or **Transfer Jobs → Retrieve → DCMTK (C-MOVE)** |

The native tools handle registered non-TLS peers. TLS operations use the PAXS certificate policy. **Nodes** shows administrators the toolkit version and configuration status. The same status is available at `GET /api/v1/nodes/dcmtk/status`.

## Configure C-MOVE

1. Set `DCMTK_MOVE_PORT` to a dedicated TCP port on the archive server, such as `10401`. Keep it separate from the archive's regular DIMSE port. Set `DCMTK_MOVE_AE_TITLE` or use the default `PAXS_MOVE`.
2. On the remote PACS, register that exact AE title as a move destination using the PAXS server's reachable IP address and the dedicated port. Authorize the PAXS calling AE (`PAXS_SCU` by default) for Study Root C-MOVE.
3. Restrict inbound TCP on the dedicated port to the remote PACS hosts in the host or network firewall. This port carries plaintext DICOM and belongs on the trusted DICOM network.
4. Restart PAXS. If your vendor layout differs, set `DCMTK_MOVE_PROFILE` to the absolute path of DCMTK's `storescp.cfg`, which supplies the `AllDICOM` presentation-context profile.

PAXS launches `dcmrecv` for one study job at a time. It validates the final C-MOVE status, received count, and Study/SOP UIDs before importing through the archive's normal pull pipeline. Study files use a temporary server directory. Set `DCMTK_MOVE_PORT=0` to disable C-MOVE.

## Interoperability checks

With `DCMTK_BIN_DIR` configured, run `python -m pytest -q .qa/test_dcmtk.py` from the archive root. These tests use synthetic data and loopback. They check native C-ECHO success and failure, C-STORE and Study Root C-FIND/C-GET/C-MOVE through PAXS DIMSE handlers with mocked persistence, DICOMDIR readability through `dcmdump`, and pixel-exact decoding of PAXS RLE output through `dcmdrle`.

DCMTK tools such as `findscu`, `storescu`, `getscu`, and `movescu` can act as independent DIMSE peers against an isolated PAXS test environment.
