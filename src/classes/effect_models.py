"""
 @file
 @brief AI model files for the processing effects (Object Detector, Object Mask)

 The packaged allow-lists (``src/resources/*-models.json``), where each model
 installs, whether an install is intact (sha256 metadata), and downloading,
 verifying and extracting a model archive. Shared by the Process Effect
 dialog (windows/process_effect.py) and the agent's processing tool. Everything
 here does file or network I/O: call it off the Qt GUI thread (the dialog
 pumps its own event loop through the progress callback).
"""

import hashlib
import json
import os
import zipfile
from urllib.parse import urljoin

from classes import http_client, info

YOLO_MODELS_PATH = os.path.join(info.RESOURCES_PATH, "yolo-models.json")
EFFICIENT_SAM_MODELS_PATH = os.path.join(info.RESOURCES_PATH, "efficient-sam-models.json")
CUTIE_MODELS_PATH = os.path.join(info.RESOURCES_PATH, "cutie-models.json")
YOLO_MODEL_FILENAME = "model.onnx"
YOLO_CLASSES_FILENAME = "classes.names"
YOLO_INSTALL_METADATA = "install.json"
YOLO_FALLBACK_MODEL_NAME = "YOLO"
EFFICIENT_SAM_INSTALL_METADATA = "install-efficient-sam.json"
CUTIE_INSTALL_METADATA = "install-cutie.json"

EFFICIENT_SAM_MODEL_FILES = {
    "efficient-sam-tiny-1024": "image_segmentation_efficientsam_ti_2025april.onnx",
    "efficient-sam-small-static-1024": "image_segmentation_efficientsam_s_static_1024.onnx",
}

CUTIE_MODEL_FILES = {
    "cutie-low": {
        "encode-key": "cutie-encode-key-480x272.onnx",
        "encode-value": "cutie-encode-value-480x272.onnx",
        "memory-readout": "cutie-memory-readout-floatmask-valid-480x272-m6-topk30-opencv.onnx",
        "decode": "cutie-decode-480x272.onnx",
    },
    "cutie-medium": {
        "encode-key": "cutie-encode-key-640x368.onnx",
        "encode-value": "cutie-encode-value-640x368.onnx",
        "memory-readout": "cutie-memory-readout-floatmask-valid-640x368-m6-topk30-opencv.onnx",
        "decode": "cutie-decode-640x368.onnx",
    },
    "cutie-high": {
        "encode-key": "cutie-encode-key-960x544.onnx",
        "encode-value": "cutie-encode-value-960x544.onnx",
        "memory-readout": "cutie-memory-readout-floatmask-valid-960x544-m6-topk30-opencv.onnx",
        "decode": "cutie-decode-960x544.onnx",
    },
    "cutie-very-high": {
        "encode-key": "cutie-encode-key-1280x720.onnx",
        "encode-value": "cutie-encode-value-1280x720.onnx",
        "memory-readout": "cutie-memory-readout-floatmask-valid-1280x720-m6-topk30-opencv.onnx",
        "decode": "cutie-decode-1280x720.onnx",
    },
}


class DownloadCancelled(Exception):
    """Raised when a user cancels an in-progress download."""


# ---------------------------------------------------------------------------
# Manifests and install locations
# ---------------------------------------------------------------------------

def load_yolo_models_manifest():
    """Load the packaged allow-list of YOLO model downloads."""
    return load_model_manifest(YOLO_MODELS_PATH)


def load_model_manifest(path):
    """Load a packaged allow-list of model downloads."""
    with open(path, "r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    manifest.setdefault("models", [])
    return manifest


def recommended_model(models):
    """Return the recommended model entry, or the first available entry."""
    for model in models:
        if model.get("recommended"):
            return model
    return models[0] if models else None


def model_by_id(models, model_id):
    """The manifest entry with *model_id*, the recommended one for an empty id, else None."""
    if not model_id:
        return recommended_model(models)
    for model in models:
        if model.get("id") == model_id:
            return model
    return None


def yolo_model_dir(model):
    """Return the install directory for a packaged YOLO model entry."""
    model_id = model.get("id", "")
    if not model_id or os.path.basename(model_id) != model_id:
        raise ValueError("Invalid YOLO model id: %s" % model_id)
    return os.path.join(info.YOLO_PATH, model_id)


def model_install_dir(model):
    """Return the shared AI model install directory for a manifest entry."""
    return yolo_model_dir(model)


def yolo_model_path(model):
    """Return the installed ONNX path for a packaged YOLO model entry."""
    return os.path.join(yolo_model_dir(model), YOLO_MODEL_FILENAME)


def yolo_classes_path(model):
    """Return the installed class names path for a packaged YOLO model entry."""
    return os.path.join(yolo_model_dir(model), YOLO_CLASSES_FILENAME)


def object_mask_cutie_paths(cutie_model):
    """{encode-key, encode-value, memory-readout, decode} ONNX paths for a Cutie quality tier."""
    model_files = CUTIE_MODEL_FILES.get(cutie_model.get("id"), CUTIE_MODEL_FILES["cutie-medium"])
    install_dir = model_install_dir(cutie_model)
    return {key: os.path.join(install_dir, filename) for key, filename in model_files.items()}


def object_mask_efficient_sam_path(efficient_sam_model):
    """The EfficientSAM ONNX path for a SAM manifest entry."""
    filename = EFFICIENT_SAM_MODEL_FILES.get(
        efficient_sam_model.get("id"),
        EFFICIENT_SAM_MODEL_FILES["efficient-sam-tiny-1024"],
    )
    return os.path.join(model_install_dir(efficient_sam_model), filename)


def model_download_url(manifest, model):
    """Return the download URL for a model manifest entry."""
    base_url = manifest.get("base_url", "")
    return urljoin("%s/" % base_url.rstrip("/"), model.get("asset", ""))


# ---------------------------------------------------------------------------
# Install checks
# ---------------------------------------------------------------------------

def file_sha256(path):
    """Return the SHA256 checksum of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def yolo_installed_files_match(model):
    """Return whether an installed YOLO model matches recorded metadata."""
    model_path = yolo_model_path(model)
    classes_path = yolo_classes_path(model)
    metadata_path = os.path.join(yolo_model_dir(model), YOLO_INSTALL_METADATA)
    try:
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        return (
            metadata.get("id") == model.get("id")
            and metadata.get("asset_sha256") == model.get("sha256")
            and os.path.isfile(model_path)
            and os.path.isfile(classes_path)
            and metadata.get("model_sha256") == file_sha256(model_path)
            and metadata.get("classes_sha256") == file_sha256(classes_path)
        )
    except (OSError, ValueError):
        return False


def installed_model_files_match(install_dir, metadata_name, model, installed_paths):
    """Return whether installed model files match recorded metadata."""
    metadata_path = os.path.join(install_dir, metadata_name)
    try:
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        file_hashes = metadata.get("files", {})
        return (
            metadata.get("id") == model.get("id")
            and metadata.get("asset_sha256") == model.get("sha256")
            and all(
                os.path.isfile(path)
                and file_hashes.get(os.path.basename(path)) == file_sha256(path)
                for path in installed_paths
            )
        )
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Extract + metadata
# ---------------------------------------------------------------------------

def extract_yolo_zip_member(yolo_zip, suffixes, destination_path):
    """Extract the first matching file from a verified YOLO model archive."""
    if isinstance(suffixes, str):
        suffixes = (suffixes,)
    for member in yolo_zip.infolist():
        if member.is_dir():
            continue
        if os.path.basename(member.filename).lower().endswith(tuple(suffixes)):
            with yolo_zip.open(member) as source_file, open(destination_path, "wb") as output_file:
                output_file.write(source_file.read())
            return
    raise ValueError("Downloaded YOLO files are invalid.")


def extract_zip_members_to_dir(zip_path, destination_dir):
    """Extract all regular files from a verified model archive."""
    os.makedirs(destination_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as model_zip:
        for member in model_zip.infolist():
            if member.is_dir():
                continue
            filename = os.path.basename(member.filename)
            if not filename:
                continue
            destination_path = os.path.join(destination_dir, filename)
            download_path = "{}.download".format(destination_path)
            with model_zip.open(member) as source_file, open(download_path, "wb") as output_file:
                output_file.write(source_file.read())
            if os.path.getsize(download_path) <= 0:
                os.remove(download_path)
                raise ValueError("Downloaded model files are invalid.")
            os.replace(download_path, destination_path)


def write_model_install_metadata(install_dir, metadata_name, model, installed_paths):
    """Record extracted-file hashes for future already-downloaded checks."""
    metadata_path = os.path.join(install_dir, metadata_name)
    metadata_download_path = "{}.download".format(metadata_path)
    with open(metadata_download_path, "w", encoding="utf-8") as metadata_file:
        json.dump(
            {
                "id": model.get("id"),
                "asset": model.get("asset"),
                "asset_sha256": model.get("sha256"),
                "files": {
                    os.path.basename(path): file_sha256(path)
                    for path in installed_paths
                },
            },
            metadata_file,
            indent=2,
            sort_keys=True,
        )
        metadata_file.write("\n")
    os.replace(metadata_download_path, metadata_path)


def write_yolo_install_metadata(model, model_path, classes_path):
    """Record extracted-file hashes for future already-downloaded checks."""
    metadata_path = os.path.join(yolo_model_dir(model), YOLO_INSTALL_METADATA)
    metadata_download_path = "{}.download".format(metadata_path)
    with open(metadata_download_path, "w", encoding="utf-8") as metadata_file:
        json.dump(
            {
                "id": model.get("id"),
                "asset": model.get("asset"),
                "asset_sha256": model.get("sha256"),
                "model": YOLO_MODEL_FILENAME,
                "model_sha256": file_sha256(model_path),
                "classes": YOLO_CLASSES_FILENAME,
                "classes_sha256": file_sha256(classes_path),
            },
            metadata_file,
            indent=2,
            sort_keys=True,
        )
        metadata_file.write("\n")
    os.replace(metadata_download_path, metadata_path)


# ---------------------------------------------------------------------------
# Downloads (report_progress(downloaded_bytes, total_bytes) may raise DownloadCancelled)
# ---------------------------------------------------------------------------

def download_yolo_model(manifest, model, report_progress=None, label=None, invalid_message=None):
    """Download, verify (sha256) and install a YOLO model; staged files never replace a good install.

    Raises DownloadCancelled when *report_progress* cancels, ValueError for a bad archive,
    and whatever http_client raises for network failures. Partial files are removed.
    """
    invalid_message = invalid_message or "Downloaded %s files are invalid." % (model.get("name") or "YOLO")
    model_dir = yolo_model_dir(model)
    model_path = yolo_model_path(model)
    classes_path = yolo_classes_path(model)
    os.makedirs(model_dir, exist_ok=True)
    zip_download_path = os.path.join(model_dir, "%s.download" % model.get("asset", "model.zip"))
    model_download_path = "{}.download".format(model_path)
    classes_download_path = "{}.download".format(classes_path)
    metadata_download_path = os.path.join(model_dir, "%s.download" % YOLO_INSTALL_METADATA)

    def remove_partial_downloads():
        for path in (zip_download_path, model_download_path, classes_download_path, metadata_download_path):
            if os.path.exists(path):
                os.remove(path)

    try:
        http_client.download_file(
            model_download_url(manifest, model),
            zip_download_path,
            label or "%s files" % (model.get("name") or "YOLO"),
            report_progress,
            cancel_exceptions=(DownloadCancelled,),
        )
        if file_sha256(zip_download_path) != model.get("sha256"):
            raise ValueError(invalid_message)
        with zipfile.ZipFile(zip_download_path) as yolo_zip:
            extract_yolo_zip_member(yolo_zip, ".onnx", model_download_path)
            extract_yolo_zip_member(yolo_zip, (".names", ".txt"), classes_download_path)
        if os.path.getsize(model_download_path) <= 0 or os.path.getsize(classes_download_path) <= 0:
            raise ValueError(invalid_message)
        os.replace(model_download_path, model_path)
        os.replace(classes_download_path, classes_path)
        write_yolo_install_metadata(model, model_path, classes_path)
    except BaseException:
        remove_partial_downloads()
        raise
    finally:
        if os.path.exists(zip_download_path):
            os.remove(zip_download_path)


def download_manifest_archive(manifest, model, install_dir, metadata_name, installed_paths,
                              report_progress=None, label=None, invalid_message="Downloaded model files are invalid."):
    """Download, verify, and extract a generic model archive (no-op when already installed)."""
    os.makedirs(install_dir, exist_ok=True)
    if installed_model_files_match(install_dir, metadata_name, model, installed_paths):
        return
    zip_download_path = os.path.join(install_dir, "%s.download" % model.get("asset", "model.zip"))
    try:
        http_client.download_file(
            model_download_url(manifest, model),
            zip_download_path,
            label or model.get("name") or model.get("id") or "Model",
            report_progress,
            cancel_exceptions=(DownloadCancelled,),
        )
        if file_sha256(zip_download_path) != model.get("sha256"):
            raise ValueError(invalid_message)
        extract_zip_members_to_dir(zip_download_path, install_dir)
        if not all(os.path.isfile(path) and os.path.getsize(path) > 0 for path in installed_paths):
            raise ValueError(invalid_message)
        write_model_install_metadata(install_dir, metadata_name, model, installed_paths)
    finally:
        if os.path.exists(zip_download_path):
            os.remove(zip_download_path)
