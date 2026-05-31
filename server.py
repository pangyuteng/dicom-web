#!/usr/bin/env python3
"""
Simple DICOMweb server using Quart + pydicom.
File-based indexer for dicomstorage directory.
Implements core QIDO-RS, WADO-RS (and basic WADO-URI).
Reference: https://github.com/dcmjs-org/dicomweb-server/
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pydicom
from pydicom.errors import InvalidDicomError
from quart import (
    Quart,
    Response,
    jsonify,
    make_response,
    request,
    send_file,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
STORAGE_DIR = Path(os.environ.get("DICOM_STORAGE_DIR", "dicomstorage"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5985"))  # match reference server default
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dicomweb")

# -----------------------------------------------------------------------------
# DICOM Indexer (scans at startup using pathlib + pydicom)
# -----------------------------------------------------------------------------
class DicomIndexer:
    """In-memory index of DICOM studies/series/instances from filesystem."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.studies: Dict[str, Dict[str, Any]] = {}
        self.series: Dict[str, Dict[str, Any]] = {}
        self.instances: Dict[str, Dict[str, Any]] = {}
        # For quick lookup of a representative file per study/series (for metadata)
        self.study_reps: Dict[str, Path] = {}
        self.series_reps: Dict[str, Path] = {}
        self._lock = asyncio.Lock()

    async def scan(self) -> None:
        """Scan storage_dir recursively for *.dcm files (sync I/O but called at startup)."""
        async with self._lock:
            self.studies.clear()
            self.series.clear()
            self.instances.clear()
            self.study_reps.clear()
            self.series_reps.clear()

            if not self.storage_dir.exists():
                logger.warning("Storage dir %s does not exist", self.storage_dir)
                return

            # Use pathlib as specified
            dcm_files: List[Path] = sorted(
                list(self.storage_dir.rglob("*.dcm"))
                + list(self.storage_dir.rglob("*.DCM"))
            )
            logger.info("Found %d DICOM files under %s", len(dcm_files), self.storage_dir)

            for filepath in dcm_files:
                try:
                    # Fast header read
                    ds = pydicom.dcmread(
                        str(filepath), stop_before_pixels=True, force=True
                    )
                    self._index_dataset(ds, filepath)
                except InvalidDicomError:
                    logger.debug("Skipping non-DICOM file: %s", filepath)
                except Exception as exc:
                    logger.warning("Failed to index %s: %s", filepath, exc)

            # Compute counts etc.
            for study in self.studies.values():
                study["ModalitiesInStudy"] = sorted(study.get("ModalitiesInStudy", set()))
                study["NumberOfStudyRelatedSeries"] = len(study.get("series_uids", set()))
                # count instances via series later or precompute
            for ser in self.series.values():
                ser["NumberOfSeriesRelatedInstances"] = len(ser.get("instance_uids", set()))

            logger.info(
                "Indexed %d studies, %d series, %d instances",
                len(self.studies),
                len(self.series),
                len(self.instances),
            )

    def _index_dataset(self, ds: pydicom.Dataset, filepath: Path) -> None:
        study_uid: Optional[str] = ds.get("StudyInstanceUID")
        series_uid: Optional[str] = ds.get("SeriesInstanceUID")
        sop_uid: Optional[str] = ds.get("SOPInstanceUID")
        if not (study_uid and series_uid and sop_uid):
            return

        modality = str(ds.get("Modality", "")) or "OT"

        # Study level
        if study_uid not in self.studies:
            self.studies[study_uid] = {
                "StudyInstanceUID": study_uid,
                "PatientID": str(ds.get("PatientID", "")),
                "PatientName": str(ds.get("PatientName", "")),
                "PatientBirthDate": str(ds.get("PatientBirthDate", "")),
                "PatientSex": str(ds.get("PatientSex", "")),
                "StudyDate": str(ds.get("StudyDate", "")),
                "StudyTime": str(ds.get("StudyTime", "")),
                "AccessionNumber": str(ds.get("AccessionNumber", "")),
                "StudyID": str(ds.get("StudyID", "")),
                "StudyDescription": str(ds.get("StudyDescription", "")),
                "ModalitiesInStudy": set(),
                "series_uids": set(),
            }
            self.study_reps[study_uid] = filepath
        self.studies[study_uid]["ModalitiesInStudy"].add(modality)
        self.studies[study_uid]["series_uids"].add(series_uid)

        # Series level
        if series_uid not in self.series:
            self.series[series_uid] = {
                "SeriesInstanceUID": series_uid,
                "StudyInstanceUID": study_uid,
                "Modality": modality,
                "SeriesNumber": str(ds.get("SeriesNumber", "")),
                "SeriesDescription": str(ds.get("SeriesDescription", "")),
                "SeriesDate": str(ds.get("SeriesDate", "")),
                "SeriesTime": str(ds.get("SeriesTime", "")),
                "BodyPartExamined": str(ds.get("BodyPartExamined", "")),
                "instance_uids": set(),
            }
            self.series_reps[series_uid] = filepath
        self.series[series_uid]["instance_uids"].add(sop_uid)

        # Instance level
        self.instances[sop_uid] = {
            "SOPInstanceUID": sop_uid,
            "SeriesInstanceUID": series_uid,
            "StudyInstanceUID": study_uid,
            "SOPClassUID": str(ds.get("SOPClassUID", "")),
            "InstanceNumber": str(ds.get("InstanceNumber", "")),
            "file_path": filepath,
            "transfer_syntax": getattr(
                getattr(ds, "file_meta", None), "TransferSyntaxUID", ""
            ),
        }

    # --- Query helpers (basic QIDO filtering) ---
    def _matches(self, record: Dict[str, Any], filters: Dict[str, str]) -> bool:
        for key, value in filters.items():
            if not value:
                continue
            val_str = str(value).strip().lower()
            if key == "ModalitiesInStudy":
                mods = record.get("ModalitiesInStudy", [])
                if isinstance(mods, set):
                    mods = list(mods)
                if val_str not in ",".join(mods).lower():
                    return False
                continue
            # Direct field or fuzzy contains (support simple wildcard *)
            rec_val = str(record.get(key, "")).lower()
            pat = val_str.replace("*", "")
            if pat and pat not in rec_val:
                return False
        return True

    def search_studies(self, filters: Dict[str, str]) -> List[Dict[str, Any]]:
        results = []
        for uid, study in self.studies.items():
            if self._matches(study, filters):
                results.append(self._study_to_qido(study))
        return results

    def search_series(
        self, study_uid: Optional[str], filters: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        results = []
        for uid, ser in self.series.items():
            if study_uid and ser.get("StudyInstanceUID") != study_uid:
                continue
            if self._matches(ser, filters):
                results.append(self._series_to_qido(ser))
        return results

    def search_instances(
        self, study_uid: Optional[str], series_uid: Optional[str], filters: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        results = []
        for sop, inst in self.instances.items():
            if study_uid and inst.get("StudyInstanceUID") != study_uid:
                continue
            if series_uid and inst.get("SeriesInstanceUID") != series_uid:
                continue
            if self._matches(inst, filters):
                results.append(self._instance_to_qido(inst))
        return results

    # --- Response formatting (DICOM JSON model for QIDO) ---
    def _make_tag(self, vr: str, value: Any) -> Dict[str, Any]:
        if value in (None, ""):
            return {"vr": vr}
        if isinstance(value, list):
            return {"vr": vr, "Value": value}
        return {"vr": vr, "Value": [value]}

    def _study_to_qido(self, study: Dict[str, Any]) -> Dict[str, Any]:
        # Use hex tag keys as per DICOMweb / QIDO
        return {
            "0020000D": self._make_tag("UI", study.get("StudyInstanceUID")),  # StudyInstanceUID
            "00100020": self._make_tag("LO", study.get("PatientID")),
            "00100010": self._make_tag("PN", study.get("PatientName")),
            "00100030": self._make_tag("DA", study.get("PatientBirthDate")),
            "00100040": self._make_tag("CS", study.get("PatientSex")),
            "00080020": self._make_tag("DA", study.get("StudyDate")),
            "00080030": self._make_tag("TM", study.get("StudyTime")),
            "00080050": self._make_tag("SH", study.get("AccessionNumber")),
            "00200010": self._make_tag("SH", study.get("StudyID")),
            "00081030": self._make_tag("LO", study.get("StudyDescription")),
            "00080061": self._make_tag("CS", study.get("ModalitiesInStudy")),
            "00201206": self._make_tag("IS", study.get("NumberOfStudyRelatedSeries")),
            "00201208": self._make_tag("IS", self._count_study_instances(study)),
        }

    def _count_study_instances(self, study: Dict[str, Any]) -> int:
        total = 0
        for suid in study.get("series_uids", set()):
            total += len(self.series.get(suid, {}).get("instance_uids", set()))
        return total

    def _series_to_qido(self, ser: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "0020000D": self._make_tag("UI", ser.get("StudyInstanceUID")),
            "0020000E": self._make_tag("UI", ser.get("SeriesInstanceUID")),
            "00080060": self._make_tag("CS", ser.get("Modality")),
            "00200011": self._make_tag("IS", ser.get("SeriesNumber")),
            "0008103E": self._make_tag("LO", ser.get("SeriesDescription")),
            "00080021": self._make_tag("DA", ser.get("SeriesDate")),
            "00201209": self._make_tag("IS", ser.get("NumberOfSeriesRelatedInstances")),
        }

    def _instance_to_qido(self, inst: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "0020000D": self._make_tag("UI", inst.get("StudyInstanceUID")),
            "0020000E": self._make_tag("UI", inst.get("SeriesInstanceUID")),
            "00080018": self._make_tag("UI", inst.get("SOPInstanceUID")),
            "00080016": self._make_tag("UI", inst.get("SOPClassUID")),
            "00200013": self._make_tag("IS", inst.get("InstanceNumber")),
        }

    # --- Metadata (WADO-RS) using pydicom.to_json_dict on representative or target ---
    def get_study_metadata(self, study_uid: str) -> Optional[List[Dict[str, Any]]]:
        """Return list of per-instance metadata dicts (DICOM JSON) for the study."""
        if study_uid not in self.studies:
            return None
        metas: List[Dict[str, Any]] = []
        for sop in self._get_instance_uids_for_study(study_uid):
            meta = self.get_instance_metadata(sop)
            if meta:
                metas.append(meta)
        return metas

    def get_series_metadata(self, study_uid: str, series_uid: str) -> Optional[List[Dict[str, Any]]]:
        if series_uid not in self.series or self.series[series_uid].get("StudyInstanceUID") != study_uid:
            return None
        metas = []
        for sop in self.series[series_uid].get("instance_uids", set()):
            meta = self.get_instance_metadata(sop)
            if meta:
                metas.append(meta)
        return metas

    def get_instance_metadata(self, sop_uid: str) -> Optional[Dict[str, Any]]:
        inst = self.instances.get(sop_uid)
        if not inst:
            return None
        try:
            ds = pydicom.dcmread(str(inst["file_path"]), stop_before_pixels=True, force=True)
            # Remove pixel data if present (for metadata)
            if "PixelData" in ds:
                del ds.PixelData
            # to_json_dict gives the exact DICOMweb JSON model
            return ds.to_json_dict()
        except Exception as exc:
            logger.warning("Metadata read failed for %s: %s", sop_uid, exc)
            return None

    def _get_instance_uids_for_study(self, study_uid: str) -> Set[str]:
        uids: Set[str] = set()
        study = self.studies.get(study_uid, {})
        for suid in study.get("series_uids", set()):
            uids.update(self.series.get(suid, {}).get("instance_uids", set()))
        return uids

    # --- File retrieval ---
    def get_instance_path(self, sop_uid: str) -> Optional[Path]:
        inst = self.instances.get(sop_uid)
        return inst["file_path"] if inst else None

    def get_instance_info(self, sop_uid: str) -> Optional[Dict[str, Any]]:
        return self.instances.get(sop_uid)


# Global indexer instance
indexer = DicomIndexer(STORAGE_DIR)

# -----------------------------------------------------------------------------
# Quart Application
# -----------------------------------------------------------------------------
app = Quart(__name__)
app.config["JSON_SORT_KEYS"] = False


def _cors_headers(response: Response) -> Response:
    """Simple CORS for browser clients / OHIF etc."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
    return response


@app.before_request
async def handle_options():
    if request.method == "OPTIONS":
        resp = await make_response("", 204)
        return _cors_headers(resp)


@app.after_request
async def add_cors(response: Response):
    return _cors_headers(response)


@app.before_serving
async def startup():
    logger.info("Starting DICOMweb server (storage=%s)", STORAGE_DIR)
    await indexer.scan()
    logger.info("Ready. Studies: %d", len(indexer.studies))


@app.route("/health", methods=["GET"])
async def health():
    return jsonify(
        {
            "status": "ok",
            "studies": len(indexer.studies),
            "series": len(indexer.series),
            "instances": len(indexer.instances),
            "storage": str(STORAGE_DIR),
        }
    )


@app.route("/reindex", methods=["POST"])
async def reindex():
    """Force a full filesystem re-scan (useful after external changes to dicomstorage)."""
    await indexer.scan()
    return jsonify(
        {
            "status": "ok",
            "message": "Reindex complete",
            "studies": len(indexer.studies),
            "instances": len(indexer.instances),
        }
    )


# =============================================================================
# QIDO-RS : Query based on ID for DICOM Objects
# =============================================================================

@app.route("/studies", methods=["GET"])
async def qido_studies():
    """QIDO-RS Retrieve Studies"""
    filters = _extract_qido_filters(request.args)
    results = indexer.search_studies(filters)
    return jsonify(results)


@app.route("/studies/<study_uid>/series", methods=["GET"])
async def qido_series(study_uid: str):
    """QIDO-RS Retrieve Series"""
    filters = _extract_qido_filters(request.args)
    results = indexer.search_series(study_uid, filters)
    return jsonify(results)


@app.route("/studies/<study_uid>/series/<series_uid>/instances", methods=["GET"])
async def qido_instances(study_uid: str, series_uid: str):
    """QIDO-RS Retrieve Instances"""
    filters = _extract_qido_filters(request.args)
    results = indexer.search_instances(study_uid, series_uid, filters)
    return jsonify(results)


def _extract_qido_filters(args) -> Dict[str, str]:
    """Extract known query params. Supports common QIDO keys + fuzzy."""
    known = {
        "PatientID",
        "PatientName",
        "StudyInstanceUID",
        "StudyDate",
        "StudyTime",
        "AccessionNumber",
        "StudyID",
        "StudyDescription",
        "ModalitiesInStudy",
        "SeriesInstanceUID",
        "SeriesNumber",
        "SeriesDescription",
        "Modality",
        "SOPInstanceUID",
        "InstanceNumber",
    }
    filters = {}
    for k, v in args.items():
        if k in known or k.endswith("UID"):  # allow any *UID
            filters[k] = v
    # Also allow direct tag names like StudyInstanceUID etc already covered
    return filters


# =============================================================================
# WADO-RS : Retrieve via RESTful Services
# =============================================================================

@app.route("/studies/<study_uid>/metadata", methods=["GET"])
async def wado_study_metadata(study_uid: str):
    metas = indexer.get_study_metadata(study_uid)
    if metas is None:
        return jsonify({"error": "Study not found"}), 404
    return jsonify(metas)


@app.route("/studies/<study_uid>/series/<series_uid>/metadata", methods=["GET"])
async def wado_series_metadata(study_uid: str, series_uid: str):
    metas = indexer.get_series_metadata(study_uid, series_uid)
    if metas is None:
        return jsonify({"error": "Series not found"}), 404
    return jsonify(metas)


@app.route(
    "/studies/<study_uid>/series/<series_uid>/instances/<instance_uid>/metadata",
    methods=["GET"],
)
async def wado_instance_metadata(study_uid: str, series_uid: str, instance_uid: str):
    meta = indexer.get_instance_metadata(instance_uid)
    if not meta:
        # verify it belongs
        inst = indexer.instances.get(instance_uid)
        if not inst or inst.get("StudyInstanceUID") != study_uid or inst.get("SeriesInstanceUID") != series_uid:
            return jsonify({"error": "Instance not found"}), 404
    return jsonify(meta)


# Retrieve full instance (binary DICOM)
@app.route(
    "/studies/<study_uid>/series/<series_uid>/instances/<instance_uid>",
    methods=["GET"],
)
async def wado_retrieve_instance(study_uid: str, series_uid: str, instance_uid: str):
    fpath = indexer.get_instance_path(instance_uid)
    if not fpath or not fpath.exists():
        inst = indexer.instances.get(instance_uid)
        if not inst or inst["StudyInstanceUID"] != study_uid or inst["SeriesInstanceUID"] != series_uid:
            return jsonify({"error": "Instance not found"}), 404
        fpath = inst["file_path"]
    return await send_file(
        str(fpath),
        mimetype="application/dicom",
    )


# Basic frame support (single-frame assumption for this dataset; returns full instance for frame 1)
# Real impl would extract pixel data + proper multipart for multi-frame / encapsulated.
@app.route(
    "/studies/<study_uid>/series/<series_uid>/instances/<instance_uid>/frames/<frames>",
    methods=["GET"],
)
async def wado_retrieve_frames(study_uid: str, series_uid: str, instance_uid: str, frames: str):
    # For simplicity: if requesting frame 1 (or "1") and instance exists, return the DICOM
    # (clients that only want pixels will usually use rendered or wadouri + own decode)
    if frames not in ("1", "1,"):
        return jsonify({"error": "Only single frame (frame=1) supported in this simple server"}), 501
    return await wado_retrieve_instance(study_uid, series_uid, instance_uid)


# WADO-URI style (legacy / common for some viewers)
# GET /?studyUID=...&seriesUID=...&objectUID=...[&contentType=application/dicom]
@app.route("/", methods=["GET"])
async def wado_uri():
    study = request.args.get("studyUID") or request.args.get("StudyInstanceUID")
    series = request.args.get("seriesUID") or request.args.get("SeriesInstanceUID")
    obj = request.args.get("objectUID") or request.args.get("SOPInstanceUID")
    if not obj:
        return jsonify({"error": "objectUID (SOPInstanceUID) is required for WADO-URI"}), 400

    fpath = indexer.get_instance_path(obj)
    if not fpath:
        return jsonify({"error": "Instance not found"}), 404

    # Optional: verify study/series if provided
    if study or series:
        info = indexer.get_instance_info(obj) or {}
        if study and info.get("StudyInstanceUID") != study:
            return jsonify({"error": "Study mismatch"}), 400
        if series and info.get("SeriesInstanceUID") != series:
            return jsonify({"error": "Series mismatch"}), 400

    content_type = request.args.get("contentType", "application/dicom")
    return await send_file(str(fpath), mimetype=content_type)


# =============================================================================
# STOW-RS (basic): Store Instances
# POST /studies[/{study}]
# Accepts multipart/related or raw application/dicom
# Saves files into dicomstorage/<study_uid>/<series_uid>/<sop_uid>.dcm (or original name)
# Then triggers re-index of the new file(s)
# =============================================================================

@app.route("/studies", methods=["POST"])
@app.route("/studies/<study_uid>", methods=["POST"])
async def stow_rs(study_uid: Optional[str] = None):
    """Very basic STOW-RS implementation."""
    content_type = request.headers.get("Content-Type", "")
    saved_files: List[str] = []

    try:
        if "multipart/related" in content_type:
            # Use python-multipart via request.files or manual
            # Quart + hypercorn + python-multipart allows form parsing for mixed
            form = await request.form
            # For true multipart/related with binary parts, we need lower level
            # Fallback: read raw body and do naive parse for "application/dicom" parts
            data = await request.get_data()
            saved_files.extend(await _handle_multipart_stow(data, content_type))
        elif "application/dicom" in content_type:
            data = await request.get_data()
            sop = await _save_single_dicom(data, study_uid)
            if sop:
                saved_files.append(sop)
        else:
            return jsonify({"error": "Unsupported Content-Type for STOW-RS"}), 415

        if not saved_files:
            return jsonify({"error": "No valid DICOM instances stored"}), 400

        # Re-scan only affected? For simplicity full rescan (small data)
        await indexer.scan()

        return jsonify(
            {
                "status": "success",
                "stored": saved_files,
                "message": f"Stored {len(saved_files)} instance(s)",
            }
        ), 200

    except Exception as exc:
        logger.exception("STOW failed")
        return jsonify({"error": str(exc)}), 500


async def _save_single_dicom(data: bytes, suggested_study: Optional[str] = None) -> Optional[str]:
    """Parse bytes as DICOM, decide path, save, return sop_uid."""
    if not data:
        return None
    tmp_path = Path("/tmp/_stow_tmp.dcm")
    tmp_path.write_bytes(data)
    try:
        ds = pydicom.dcmread(str(tmp_path), force=True)
        study_uid = suggested_study or ds.get("StudyInstanceUID", "unknown_study")
        series_uid = ds.get("SeriesInstanceUID", "unknown_series")
        sop_uid = ds.get("SOPInstanceUID", tmp_path.stem)

        target_dir = STORAGE_DIR / str(study_uid) / str(series_uid)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sop_uid}.dcm"
        # avoid overwrite if exists
        if target.exists():
            target = target_dir / f"{sop_uid}_{len(data)}.dcm"
        target.write_bytes(data)
        logger.info("STOW saved %s", target)
        return str(sop_uid)
    except Exception as exc:
        logger.warning("STOW single save failed: %s", exc)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


async def _handle_multipart_stow(body: bytes, content_type: str) -> List[str]:
    """Naive multipart parser for STOW (looks for application/dicom parts)."""
    saved: List[str] = []
    # Very simple boundary based extraction (good enough for basic clients)
    if "boundary=" not in content_type:
        # try raw
        sop = await _save_single_dicom(body)
        if sop:
            saved.append(sop)
        return saved

    boundary = content_type.split("boundary=")[1].split(";")[0].strip().strip('"')
    parts = body.split(f"--{boundary}".encode())
    for part in parts:
        if b"application/dicom" not in part.lower() and b"\r\n\r\n" not in part:
            continue
        # split headers / body
        if b"\r\n\r\n" in part:
            _, payload = part.split(b"\r\n\r\n", 1)
            # strip trailing \r\n-- etc
            payload = payload.rsplit(b"\r\n", 1)[0]
            if payload:
                sop = await _save_single_dicom(payload)
                if sop:
                    saved.append(sop)
    return saved


# =============================================================================
# Other / convenience
# =============================================================================

@app.route("/patients", methods=["GET"])
async def get_patients():
    """Non-standard but present in reference server."""
    pats = {}
    for study in indexer.studies.values():
        pid = study.get("PatientID") or "UNKNOWN"
        if pid not in pats:
            pats[pid] = {
                "PatientID": pid,
                "PatientName": study.get("PatientName"),
                "PatientBirthDate": study.get("PatientBirthDate"),
                "PatientSex": study.get("PatientSex"),
                "studies": [],
            }
        pats[pid]["studies"].append(study.get("StudyInstanceUID"))
    return jsonify(list(pats.values()))


@app.route("/studies/<study_uid>", methods=["DELETE"])
async def delete_study(study_uid: str):
    """Delete study from index + filesystem (DANGEROUS - for dev only)."""
    if study_uid not in indexer.studies:
        return jsonify({"error": "not found"}), 404
    study = indexer.studies[study_uid]
    removed = 0
    for suid in list(study.get("series_uids", [])):
        for sop in list(indexer.series.get(suid, {}).get("instance_uids", [])):
            p = indexer.get_instance_path(sop)
            if p and p.exists():
                p.unlink()
                removed += 1
    # remove empty dirs
    for suid in study.get("series_uids", []):
        sdir = STORAGE_DIR / study_uid / suid
        if sdir.exists():
            try:
                sdir.rmdir()
            except OSError:
                pass
    study_dir = STORAGE_DIR / study_uid
    if study_dir.exists():
        try:
            study_dir.rmdir()
        except OSError:
            pass
    await indexer.scan()
    return jsonify({"deleted_study": study_uid, "files_removed": removed})


if __name__ == "__main__":
    # Dev server (for prod use hypercorn)
    app.run(host=HOST, port=PORT, debug=DEBUG)
