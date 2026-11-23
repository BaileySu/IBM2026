"""Immutable, local calculation receipts. Chat messages are never stored here."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def publish(directory, report):
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    result_id = hashlib.sha256(content).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (result_id + ".json")
    if target.exists() and target.read_bytes() == content:
        return result_id
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as out:
        temporary = Path(out.name)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return result_id


def read_result(directory, result_id):
    content = (Path(directory) / (result_id + ".json")).read_bytes()
    if hashlib.sha256(content).hexdigest() != result_id:
        raise ValueError("The saved result failed its integrity check.")
    return content
