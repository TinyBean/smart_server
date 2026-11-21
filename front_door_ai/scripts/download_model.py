"""Build-time model retrieval. Runtime never downloads weights.

Checksums and sizes are from Intel Open Model Zoo's 2023.0 model manifest.
https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/person-detection-retail-0013/model.yml
"""
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

BASE = "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-detection-retail-0013/FP16/"
FILES = {
    "person-detection-retail-0013.xml": (571233, "99ad3d4580a0123bef05ff77b6f46ccec16de974d1f5699fb94cd842e3242c6aa641f4977f9a5bb2f0fab42fe51cbb63"),
    "person-detection-retail-0013.bin": (1445734, "a67422e3b5ec76057651d2a0237eab862de00e968c7eef1e5f333849ae64f91900bcd30a23e1b7dbaa07313e358759b9"),
}


def download(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in FILES.items():
        target = destination / name
        if target.exists() and target.stat().st_size == size and hashlib.sha384(target.read_bytes()).hexdigest() == digest:
            continue
        with urllib.request.urlopen(BASE+name, timeout=60) as response:
            content = response.read(size+1)
        if len(content) != size or hashlib.sha384(content).hexdigest() != digest:
            raise ValueError(f"Model integrity check failed: {name}")
        temporary = target.with_suffix(target.suffix+".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
        print(f"Verified {name} ({size} bytes)")


if __name__ == "__main__":
    download(Path(sys.argv[1] if len(sys.argv) > 1 else "models"))
