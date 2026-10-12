"""Generate/check deployment binary checksums without contacting a tablet."""
import argparse
import hashlib
import json
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent.parent
PAYLOADS = (
    "tablet/bin/injector-aarch64",
    "tablet/bin/injector-armv7l",
    "tablet/bin/touch-injector-aarch64",
    "tablet/direct-paste/direct_paste.so",
    "tablet/direct-paste/xovi.so",
    "art/pen-bridge",
    "art/viewport/prebuilt/krita_viewport.so",
    "ink/stylus-injector-aarch64",
)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check",action="store_true")
    options=parser.parse_args()
    digests={name:hashlib.sha256((RESOURCES/name).read_bytes()).hexdigest() for name in PAYLOADS}
    destination=RESOURCES/"tablet/artifacts.json"
    if options.check:
        if json.loads(destination.read_text())!={"schema":1,"sha256":digests}:
            raise SystemExit("Tablet payload inventory differs from the packaged binaries. Inspect rebuild changes before regenerating.")
        print("Tablet payload checksums verified.")
    else:
        destination.write_text(json.dumps({"schema":1,"sha256":digests},indent=2)+"\n",encoding="utf-8")
        print("Updated tablet/artifacts.json from the included build artifacts.")


if __name__=="__main__":main()
