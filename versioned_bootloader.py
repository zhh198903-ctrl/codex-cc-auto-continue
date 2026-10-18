"""Give each released EXE its own PyInstaller archive-identity variable.

Old updaters relaunch the same filename with the old PyInstaller environment.
That mistakes the replacement for a child of the old, already deleted runtime.
The stock bootloader reads/writes _PYI_ARCHIVE_FILE to decide whether to reset
that environment. A release-specific name makes the first launch independent;
the new bootloader's own parent and child still share exactly the same key.

Only a project-local copy of the stock bootloader is changed. Equal-length,
NUL-terminated replacement preserves all PE offsets; PyInstaller subsequently
assembles the executable and fixes its headers normally. Refuse unknown layouts.

Upstream source / license:
https://github.com/pyinstaller/pyinstaller/blob/v6.19.0/bootloader/src/pyi_main.c
https://github.com/pyinstaller/pyinstaller/blob/v6.19.0/COPYING.txt
"""
from pathlib import Path
import re

ARCHIVE_KEY = b'_PYI_ARCHIVE_FILE\0'


def release_key(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Bootloader version must be a numeric release')
    parts = tuple(map(int, version.split('.')))
    if any(value > 65535 for value in parts):
        raise ValueError('Release component exceeds bootloader identity range')
    return ('_ACV_' + ''.join(f'{value:04X}' for value in parts)).encode('ascii') + b'\0'


def prepare(original, destination, version):
    data = Path(original).read_bytes()
    identity = release_key(version)
    if len(identity) != len(ARCHIVE_KEY) or data.count(ARCHIVE_KEY) != 1:
        raise ValueError('Unsupported PyInstaller bootloader archive-identity layout')
    patched = data.replace(ARCHIVE_KEY, identity)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched)
    return str(destination)
