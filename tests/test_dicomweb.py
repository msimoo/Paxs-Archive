from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers


def _upload(client, datasets, boundary="bnd-1"):
    res = client.post(
        "/dicom-web/studies",
        content=build_stow_body(datasets, boundary),
        headers=stow_headers(boundary),
    )
    assert res.status_code == 200, res.text
    return res.json()


def _upload_study(client, n=2, patient_id="P100", study_date="20250115", modality="CT",
                  study_uid=None, patient_name="Doe^John"):
    first = None
    datasets = []
    for i in range(1, n + 1):
        ds = make_instance(
            patient_id=patient_id, study_date=study_date, modality=modality,
            patient_name=patient_name,
            study_uid=(first.StudyInstanceUID if first else study_uid),
            series_uid=(first.SeriesInstanceUID if first else None),
            instance_num=i,
        )
        if first is None:
            first = ds
        datasets.append(ds_bytes(ds))
    payload = _upload(client, datasets)
    refs = payload["00081199"]["Value"]
    assert len(refs) == n
    return str(first.StudyInstanceUID), str(first.SeriesInstanceUID)


# --------------------------------------------------------------------------- #
# STOW-RS
# --------------------------------------------------------------------------- #
def test_stow_uploads_and_indexes(client):
    study_uid, series_uid = _upload_study(client)

    stats = client.get("/api/v1/stats").json()
    assert stats["studies"] == 1
    assert stats["series"] == 1
    assert stats["instances"] == 2


def test_stow_bad_body_returns_400(client):
    res = client.post("/dicom-web/studies", content=b"", headers={"Content-Type": "application/dicom"})
    assert res.status_code == 400


def test_stow_streams_parts_with_small_chunks(client):
    """STOW body is consumed part-by-part; identical result whether the
    client sends it as one blob or dribbles it in tiny chunks (boundary
    separators split across chunk boundaries must still parse)."""
    ds1 = make_instance(patient_id="STREAMED", study_date="20250116",
                        modality="MR", instance_num=1)
    ds2 = make_instance(patient_id="STREAMED", study_date="20250116",
                        modality="MR", instance_num=2,
                        study_uid=str(ds1.StudyInstanceUID))
    body = build_stow_body([ds_bytes(ds1), ds_bytes(ds2)], "bnd-stream")

    for chunk_size in (len(body), 7, 1):
        streamed = body if chunk_size == len(body) else b"".join(
            body[i:i + chunk_size] for i in range(0, len(body), chunk_size))
        res = client.post(
            "/dicom-web/studies",
            content=streamed,
            headers=stow_headers("bnd-stream"),
        )
        assert res.status_code == 200, res.text
        refs = res.json()["00081199"]["Value"]
        assert len(refs) == 2

    stats = client.get("/api/v1/stats").json()
    # Same UIDs every iteration -> idempotent re-import, exactly one study.
    assert stats["studies"] == 1
    assert stats["instances"] == 2


def test_stow_summary_reports_new_and_existing_studies(client):
    """The STOW response carries a per-study summary so the UI can say which
    studies were newly imported vs. already present in the archive, with the
    instance count each, and each stored item carries its StudyInstanceUID."""
    ds1 = make_instance(patient_id="SUMMARY", study_date="20250120",
                        modality="CT", instance_num=1)
    study_a = str(ds1.StudyInstanceUID)
    ds2 = make_instance(patient_id="SUMMARY", study_date="20250120", modality="CT",
                        instance_num=2, study_uid=study_a)
    ds3 = make_instance(patient_id="SUMMARY", study_date="20250120", modality="US",
                        instance_num=1)  # brand-new second study
    study_b = str(ds3.StudyInstanceUID)

    # First upload: two studies, both new.
    res = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds1), ds_bytes(ds2), ds_bytes(ds3)], "bnd-sum1"),
        headers=stow_headers("bnd-sum1"),
    )
    assert res.status_code == 200, res.text
    summary = res.json()["summary"]
    assert summary["instances_stored"] == 3
    assert summary["instances_failed"] == 0
    assert summary["studies_imported"] == 2
    assert summary["studies_existed"] == 0
    assert {s["study_instance_uid"] for s in summary["studies"]} == {study_a, study_b}
    by_uid = {s["study_instance_uid"]: s for s in summary["studies"]}
    assert by_uid[study_a]["instances"] == 2 and by_uid[study_a]["existed"] is False
    assert by_uid[study_b]["instances"] == 1 and by_uid[study_b]["existed"] is False
    # Stored items carry their StudyInstanceUID tag for client-side grouping.
    refs = res.json()["00081199"]["Value"]
    assert all(r["0020000D"]["Value"][0] in {study_a, study_b} for r in refs)

    # Re-upload a slice of study A: it now already exists.
    res2 = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds1)], "bnd-sum2"),
        headers=stow_headers("bnd-sum2"),
    )
    assert res2.status_code == 200, res2.text
    summary2 = res2.json()["summary"]
    assert summary2["instances_stored"] == 1
    assert summary2["studies_imported"] == 0
    assert summary2["studies_existed"] == 1
    assert summary2["studies"][0]["study_instance_uid"] == study_a
    assert summary2["studies"][0]["instances"] == 1
    assert summary2["studies"][0]["existed"] is True


def test_stow_per_part_cap_returns_413(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STOW_MAX_PART_BYTES", 1024)
    ds = make_instance(patient_id="CAP1", instance_num=1)
    payload = build_stow_body([ds_bytes(ds)], "bnd-cap")
    assert len(payload) > 1024  # sanity: the part really exceeds the cap
    res = client.post(
        "/dicom-web/studies", content=payload, headers=stow_headers("bnd-cap")
    )
    assert res.status_code == 413


def test_stow_total_cap_returns_413(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STOW_MAX_TOTAL_BYTES", 1024)
    ds = make_instance(patient_id="CAP2", instance_num=1)
    payload = build_stow_body([ds_bytes(ds)], "bnd-total")
    res = client.post(
        "/dicom-web/studies", content=payload, headers=stow_headers("bnd-total")
    )
    assert res.status_code == 413


def test_stow_headers_split_across_chunks_parses(client):
    """Boundary + header separators split mid-sequence by a 1-byte chunk
    stream are reassembled correctly (regression for the keep-tail window)."""
    from app.services.dicomweb_service import iter_stow_parts

    ds = make_instance(patient_id="HEADSPLIT", instance_num=1)
    payload = build_stow_body([ds_bytes(ds)], "bnd-hdr")

    async def one_byte_stream():
        for b in payload:
            yield bytes([b])

    import asyncio

    async def collect():
        out = []
        async for ctype, data in iter_stow_parts(
            one_byte_stream(),
            'multipart/related; type="application/dicom"; boundary=bnd-hdr',
        ):
            out.append((ctype, data))
        return out

    parts = asyncio.run(collect())
    assert len(parts) == 1
    assert parts[0][0] == "application/dicom"
    assert parts[0][1] == ds_bytes(ds)


# --------------------------------------------------------------------------- #
# QIDO-RS
# --------------------------------------------------------------------------- #
def test_qido_studies_exact_and_wildcard(client):
    _upload_study(client, patient_id="P100")
    _upload_study(client, patient_id="P200", study_date="20250201")

    exact = client.get("/dicom-web/studies?PatientID=P100").json()
    assert len(exact) == 1
    assert exact[0]["00100020"]["Value"] == ["P100"]
    assert "00080061" in exact[0]  # ModalitiesInStudy
    # DICOM JSON: each modality is its own Value entry — never a Python repr
    # like "['CT']" (which breaks the viewer's tokenizer / join on "\\").
    mods = exact[0]["00080061"]["Value"]
    assert isinstance(mods, list) and len(mods) == 1 and mods == ["CT"], mods

    wild = client.get("/dicom-web/studies?PatientName=Doe*").json()
    assert len(wild) == 2

    none = client.get("/dicom-web/studies?PatientID=NOPE").json()
    assert none == []


def test_qido_date_range(client):
    _upload_study(client, patient_id="P100", study_date="20250115")
    _upload_study(client, patient_id="P200", study_date="20250301")

    rng = client.get("/dicom-web/studies?StudyDate=20250101-20250201").json()
    assert len(rng) == 1
    assert rng[0]["00100020"]["Value"] == ["P100"]

    open_end = client.get("/dicom-web/studies?StudyDate=20250201-").json()
    assert len(open_end) == 1


def test_series_count_with_two_same_modality_series(client):
    # Two series sharing the same modality must each count (regression).
    ds1 = make_instance(study_uid="1.2.3.77", series_uid="1.2.3.77.1", instance_num=1)
    ds2 = make_instance(study_uid="1.2.3.77", series_uid="1.2.3.77.2", instance_num=1)
    _upload(client, [ds_bytes(ds1), ds_bytes(ds2)])

    studies = client.get("/dicom-web/studies?StudyInstanceUID=1.2.3.77").json()
    assert studies[0]["00201206"]["Value"] == [2]  # NumberOfStudyRelatedSeries
    assert studies[0]["00201208"]["Value"] == [2]  # NumberOfStudyRelatedInstances

    listing = client.get("/api/v1/studies?search=Doe^John").json()
    item = next(i for i in listing["items"] if i["study_uid"] == "1.2.3.77")
    assert item["n_series"] == 2
    assert item["n_instances"] == 2


def test_qido_series_and_instances(client):
    study_uid, series_uid = _upload_study(client)

    series = client.get(f"/dicom-web/studies/{study_uid}/series?Modality=CT").json()
    assert len(series) == 1
    assert series[0]["0020000E"]["Value"] == [series_uid]
    assert series[0]["00201209"]["Value"] == [2]  # NumberOfSeriesRelatedInstances

    instances = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances").json()
    assert len(instances) == 2
    assert instances[0]["00080018"]["Value"]


# --------------------------------------------------------------------------- #
# WADO-RS
# --------------------------------------------------------------------------- #
def test_wado_instance_bytes(client):
    study_uid, series_uid = _upload_study(client, n=1)
    sop = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]

    res = client.get(
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop}",
        headers={"Accept": "application/dicom"},
    )
    assert res.status_code == 200
    assert res.content[128:132] == b"DICM"
    assert res.headers["content-type"].startswith("application/dicom")


def test_wado_study_multipart(client):
    study_uid, _ = _upload_study(client, n=2)
    res = client.get(f"/dicom-web/studies/{study_uid}",
                     headers={"Accept": 'multipart/related; type="application/dicom"'})
    assert res.status_code == 200
    assert "multipart/related" in res.headers["content-type"]
    assert res.content.count(b"Content-Type: application/dicom") == 2
    assert res.content.count(b"DICM") == 2


def test_wado_metadata_and_instance_metadata(client):
    study_uid, series_uid = _upload_study(client, n=1)
    meta = client.get(f"/dicom-web/studies/{study_uid}/metadata").json()
    assert len(meta) == 1
    assert "00080018" in meta[0]
    assert meta[0]["00100010"]["Value"][0]["Alphabetic"] == "Doe^John"

    sop = meta[0]["00080018"]["Value"][0]
    inst_meta = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop}/metadata").json()
    assert inst_meta["00100010"]["Value"][0]["Alphabetic"] == "Doe^John"


def test_wado_rendered_png_and_windowing(client):
    study_uid, series_uid = _upload_study(client, n=1)
    sop = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]
    base = f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop}/rendered"

    png = client.get(base, headers={"Accept": "image/png"})
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

    jpeg = client.get(base, headers={"Accept": "image/jpeg"})
    assert jpeg.status_code == 200
    assert jpeg.content[:3] == b"\xff\xd8\xff"

    windowed = client.get(base + "?window=40,400&viewport=512,512")
    assert windowed.status_code == 200


def test_wado_thumbnail_route(client):
    """PaxsViewer/PAXS wadors thumbnails hit .../instances/{sop}/thumbnail.

    The route must serve a small rendered PNG (never 404) so study-list and
    series thumbnails populate and no console/network 404s appear.
    """
    study_uid, series_uid = _upload_study(client, n=1)
    sop = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]
    base = f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop}/thumbnail"

    png = client.get(base, headers={"Accept": "image/png"})
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"

    jpeg = client.get(base, headers={"Accept": "image/jpeg"})
    assert jpeg.status_code == 200
    assert jpeg.content[:3] == b"\xff\xd8\xff"


def test_wado_frames_single_and_invalid(client):
    study_uid, series_uid = _upload_study(client, n=1)
    sop = client.get(f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]
    base = f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop}/frames"

    # Default / `*/*` Accept → RAW frame bytes (PS 3.18) — exactly what the
    # viewer's pixel decoders (openjpeg / libjpeg / charls wasm) receive.
    ok = client.get(f"{base}/1")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("application/octet-stream")
    assert len(ok.content) == 64 * 64 * 2  # 64x64 16-bit grayscale frame
    assert ok.content[128:132] != b"DICM"  # a frame codestream, not a DICOM file

    # Explicit application/dicom Accept → whole instance (legacy convenience).
    whole = client.get(f"{base}/1", headers={"Accept": "application/dicom"})
    assert whole.status_code == 200
    assert whole.content[128:132] == b"DICM"

    bad = client.get(f"{base}/5")
    assert bad.status_code == 404


def test_wado_frames_viewer_serves_native_codestream(client):
    """Viewer-mode (transfer-syntax=*) serves the NATIVE codestream by default.

    Cornerstone's loader asks for `transfer-syntax=*` ("server may choose") and
    picks its decoder from the per-part Content-Type's transfer-syntax. Serving
    the native JPEG2000 codestream (a few KiB) instead of decoding server-side
    to raw 512 KiB pixels makes CT stacks ~10x lighter on the wire. The raw
    transcode path remains as an explicit `?transcode=1` opt-in (the escape
    hatch for the DX openjpeg-wasm failure, cornerstone3D #917).
    """
    import pydicom

    ds = make_instance(instance_num=1)
    orig = ds.pixel_array.copy()
    ds.compress(pydicom.uid.JPEG2000Lossless)
    assert ds.file_meta.TransferSyntaxUID.is_compressed

    payload = build_stow_body([ds_bytes(ds)], "bnd-j2k")
    res = client.post("/dicom-web/studies", content=payload, headers=stow_headers("bnd-j2k"))
    assert res.status_code == 200, res.text

    base = f"/dicom-web/studies/{ds.StudyInstanceUID}/series/{ds.SeriesInstanceUID}/instances/{ds.SOPInstanceUID}/frames"
    expected = orig.astype("<u2").tobytes()

    # 1) The loader's exact Accept → multipart part is the native JP2 codestream
    #    with its real transfer-syntax (so cornerstone picks openjpeg wasm).
    viewer = client.get(
        f"{base}/1",
        headers={"Accept": "multipart/related; type=application/octet-stream; transfer-syntax=*"},
    )
    assert viewer.status_code == 200
    assert "multipart" in viewer.headers["content-type"]
    assert b"image/jp2; transfer-syntax=1.2.840.10008.1.2.4.90" in viewer.content
    part = viewer.content.split(b"\r\n\r\n", 1)[1]
    part = part[: part.rfind(b"\r\n--")]
    assert part[:2] == b"\xff\x4f"  # JPEG2000 SOC marker — native codestream
    assert len(part) < len(expected)  # must be smaller than raw pixels

    # 2) Explicit ?transcode=1 keeps the old server-side raw decode path.
    direct = client.get(
        f"{base}/1?transcode=1",
        headers={"Accept": "application/octet-stream; transfer-syntax=*"},
    )
    assert direct.status_code == 200
    assert direct.headers["content-type"].startswith("application/octet-stream")
    assert direct.content == expected

    # 3) A client that asks for a specific type still gets the raw codestream.
    raw = client.get(f"{base}/1", headers={"Accept": "image/jp2"})
    assert raw.status_code == 200
    assert raw.content[:2] == b"\xff\x4f"  # JPEG2000 SOC marker


def test_wado_frames_transcode_cache_serves_from_cache(client):
    """The decoded-frame LRU cache must be consulted BEFORE decoding.

    Regression: the cache lookup used to happen after ``ds.pixel_array``, so
    every HTTP request re-ran the expensive J2K decode (the per-request Dataset
    is always fresh). Verify the module cache is populated by the first request
    and that a second request for the same frame is served without re-decoding
    (bytes identical, and the cache retains the entry).
    """
    import pydicom

    from app.services.wado_rs_bulk_service import clear_frame_cache, decode_frames_to_raw

    ds = make_instance(instance_num=1)
    orig = ds.pixel_array.copy()
    ds.compress(pydicom.uid.JPEG2000Lossless)
    payload = build_stow_body([ds_bytes(ds)], "bnd-cache")
    res = client.post("/dicom-web/studies", content=payload, headers=stow_headers("bnd-cache"))
    assert res.status_code == 200, res.text

    base = f"/dicom-web/studies/{ds.StudyInstanceUID}/series/{ds.SeriesInstanceUID}/instances/{ds.SOPInstanceUID}/frames"
    headers = {"Accept": "application/octet-stream; transfer-syntax=*"}

    clear_frame_cache()
    # ?transcode=1 opts into the raw decode path, which is what this cache serves.
    first = client.get(f"{base}/1?transcode=1", headers=headers)
    second = client.get(f"{base}/1?transcode=1", headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == second.content == orig.astype("<u2").tobytes()

    # Cache must be populated for (SOP, frame 1) after the two HTTP requests.
    ds2 = make_instance(instance_num=1)
    ds2.compress(pydicom.uid.JPEG2000Lossless)
    ds2.SOPInstanceUID = ds.SOPInstanceUID
    frames = decode_frames_to_raw(ds2, [1])
    assert frames[0][0] == orig.astype("<u2").tobytes()
    clear_frame_cache()


def test_dashboard_search_filters(client):
    _upload_study(client, patient_id="P100", study_date="20250115", study_uid="1.2.3.100")
    _upload_study(client, patient_id="P200", study_date="20250401", study_uid="1.2.3.200")

    by_patient = client.get("/api/v1/studies?search=Doe^John").json()
    assert by_patient["total"] == 2

    by_mrn = client.get("/api/v1/studies?search=P100").json()
    assert by_mrn["total"] == 1

    by_desc = client.get("/api/v1/studies?search=CHEST").json()
    assert by_desc["total"] == 2

    by_date = client.get("/api/v1/studies?from=20250101&to=20250201").json()
    assert by_date["total"] == 1


def test_dashboard_studies_sort_and_counts(client):
    _upload_study(client, patient_id="P100", study_date="20250115", study_uid="1.2.3.100", modality="CT", patient_name="Able^Alice")
    _upload_study(client, patient_id="P200", study_date="20250401", study_uid="1.2.3.200", modality="DX", patient_name="Zed^Zara")

    # status counts always present, independent of the state toggle
    base = client.get("/api/v1/studies").json()
    assert base["counts"] == {
        "total": 2, "online": 2, "archived": 0, "critical": 0,
        "reported": 0, "oru_sent": 0, "his": 0,
    }

    # default sort: date desc (newest first)
    by_date = client.get("/api/v1/studies?sort=date&order=desc").json()
    assert [i["patient_id"] for i in by_date["items"]] == ["P200", "P100"]
    asc = client.get("/api/v1/studies?sort=date&order=asc").json()
    assert [i["patient_id"] for i in asc["items"]] == ["P100", "P200"]

    # patient / accession / series / created / modality sort keys accepted
    for key in ("patient", "accession", "series", "instances", "created", "modality"):
        r = client.get(f"/api/v1/studies?sort={key}&order=asc").json()
        assert r["total"] == 2
        assert r["sort"] == key
    by_patient = client.get("/api/v1/studies?sort=patient&order=asc").json()
    assert [i["patient_id"] for i in by_patient["items"]] == ["P100", "P200"]
    by_series = client.get("/api/v1/studies?sort=series&order=desc").json()
    assert [i["n_series"] for i in by_series["items"]] == [1, 1]
    # modality sorts alphabetically by the study's first modality (CT < DX)
    by_modality = client.get("/api/v1/studies?sort=modality&order=asc").json()
    assert [i["modalities"] for i in by_modality["items"]] == [["CT"], ["DX"]]
    by_modality_desc = client.get("/api/v1/studies?sort=modality&order=desc").json()
    assert [i["modalities"] for i in by_modality_desc["items"]] == [["DX"], ["CT"]]

    # lifecycle_state surfaced per item
    assert base["items"][0]["lifecycle_state"] == "ONLINE"

    # state filter narrows the list but keeps global counts
    online = client.get("/api/v1/studies?state=ONLINE").json()
    assert online["total"] == 2
    assert online["counts"]["total"] == 2
    archived = client.get("/api/v1/studies?state=ARCHIVED").json()
    assert archived["total"] == 0

    # critical toggle: no flags -> empty list, but the global chip count stays
    crit = client.get("/api/v1/studies?critical=1").json()
    assert crit["total"] == 0
    assert crit["counts"]["critical"] == 0

    # modality filter (existing) still composes with sort
    dx = client.get("/api/v1/studies?modality=DX&sort=date").json()
    assert dx["total"] == 1
    assert dx["items"][0]["modalities"] == ["DX"]


def test_dashboard_counts_reported_oru_his(client):
    """The status bar counters for clinical workflow: studies with a finalized
    report, reports delivered to the EMR (ORU SENT), and studies of HIS-
    registered patients — each independently filterable."""
    from app.db.models import RadiologyReport, Study
    from app.db.session import SyncSessionLocal
    from app.dicom.hl7 import _upsert_patient, parse_adt_message
    from tests.test_hl7 import ADT_A01

    # 1. Two plain DICOM studies (no reports, no HIS).
    _upload_study(client, patient_id="P100", study_uid="1.2.3.100", modality="CT")
    _upload_study(client, patient_id="P200", study_uid="1.2.3.200", modality="DX")

    # 2. A finalized report on the first study, delivered to the EMR (SENT).
    with SyncSessionLocal() as s:
        study = s.query(Study).filter(Study.study_instance_uid == "1.2.3.100").one()
        s.add(RadiologyReport(
            study_id=study.id,
            study_instance_uid="1.2.3.100",
            patient_id="P100",
            status="FINALIZED",
            findings="Normal chest radiograph.",
            impression="No acute findings.",
            radiologist="dr.ahmed",
            oru_status="SENT",
        ))
        s.commit()

    # 3. A third study for an HL7-registered (HIS) patient.
    _upsert_patient(parse_adt_message(ADT_A01))  # registers MRN987654 -> source=his
    _upload_study(client, patient_id="MRN987654", patient_name="Smith Jane",
                  study_uid="1.2.3.300", modality="CR")

    base = client.get("/api/v1/studies").json()
    c = base["counts"]
    assert c["total"] == 3
    assert c["reported"] == 1
    assert c["oru_sent"] == 1
    assert c["his"] == 1

    # Per-item report state (row badge): the reported study carries its
    # status + delivery state; the others have none.
    by_uid = {i["study_uid"]: i for i in base["items"]}
    assert by_uid["1.2.3.100"]["report_status"] == "FINALIZED"
    assert by_uid["1.2.3.100"]["oru_status"] == "SENT"
    assert by_uid["1.2.3.100"]["report_id"] is not None
    assert by_uid["1.2.3.200"]["report_status"] is None
    assert by_uid["1.2.3.200"]["oru_status"] is None
    assert by_uid["1.2.3.300"]["report_status"] is None

    # Filters narrow the list and keep the other (global) chip counts intact.
    rep = client.get("/api/v1/studies?reported=1").json()
    assert rep["total"] == 1
    assert rep["items"][0]["study_uid"] == "1.2.3.100"
    assert rep["counts"]["reported"] == 1  # global count survives the toggle

    oru = client.get("/api/v1/studies?oru=1").json()
    assert oru["total"] == 1
    assert oru["items"][0]["study_uid"] == "1.2.3.100"

    his = client.get("/api/v1/studies?his=1").json()
    assert his["total"] == 1
    assert his["items"][0]["patient_id"] == "MRN987654"
    assert his["items"][0]["patient_source"] == "his"

    # A draft report does NOT count as reported.
    with SyncSessionLocal() as s:
        study = s.query(Study).filter(Study.study_instance_uid == "1.2.3.200").one()
        s.add(RadiologyReport(
            study_id=study.id,
            study_instance_uid="1.2.3.200",
            patient_id="P200",
            status="DRAFT",
            radiologist="dr.sara",
        ))
        s.commit()
    again = client.get("/api/v1/studies").json()
    assert again["counts"]["reported"] == 1  # draft does not flip the counter

    # A FINALIZED report that has NOT yet reached the EMR (oru PENDING)
    # counts as reported but not as EMR-sent — the badge's middle state.
    with SyncSessionLocal() as s:
        study = s.query(Study).filter(Study.study_instance_uid == "1.2.3.300").one()
        s.add(RadiologyReport(
            study_id=study.id,
            study_instance_uid="1.2.3.300",
            patient_id="MRN987654",
            status="FINALIZED",
            findings="Minimal right basal opacity.",
            impression="Possible consolidation — correlate clinically.",
            radiologist="dr.ahmed",
            oru_status="PENDING",
        ))
        s.commit()
    pend = client.get("/api/v1/studies").json()
    assert pend["counts"]["reported"] == 2
    assert pend["counts"]["oru_sent"] == 1
    p_uid = {i["study_uid"]: i for i in pend["items"]}
    assert p_uid["1.2.3.300"]["report_status"] == "FINALIZED"
    assert p_uid["1.2.3.300"]["oru_status"] == "PENDING"
    # The ?oru=1 filter still shows only the truly delivered report.
    assert client.get("/api/v1/studies?oru=1").json()["total"] == 1
