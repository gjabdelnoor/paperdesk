# Third-party notices

PaperDesk's original source is AGPL-3.0-or-later. Dependencies retain their own licenses.
No proprietary reMarkable firmware, xochitl application, Qt display plugin, or private
tablet document is distributed.

- Python: PSF license, https://www.python.org/
- PySide6 Essentials and Qt: LGPL-3.0/GPL-3.0/commercial; dynamically linked native widgets.
  https://doc.qt.io/qtforpython-6/ and https://code.qt.io/
  Bundles keep Qt libraries separate so users can replace compatible versions.
- Shiboken: LGPL-3.0/GPL-3.0/commercial, https://code.qt.io/pyside/pyside-setup.git/
- Paramiko: LGPL-2.1, https://github.com/paramiko/paramiko
- Pillow: HPND, https://github.com/python-pillow/Pillow
- PyMuPDF: AGPL-3.0/commercial; this project uses the AGPL distribution.
  https://github.com/pymupdf/PyMuPDF
- platformdirs: MIT, https://github.com/tox-dev/platformdirs
- portalocker: BSD-3-Clause, https://github.com/wolph/portalocker
- cryptography: Apache-2.0/BSD-3-Clause, https://github.com/pyca/cryptography
- bcrypt and PyNaCl: Apache-2.0; CFFI: MIT.
- evdev (optional Linux art mode): BSD-3-Clause, https://github.com/gvalkov/python-evdev
- PyInstaller (build tooling): GPL-2.0 with bootloader exception,
  https://github.com/pyinstaller/pyinstaller

Vendored art, keyboard and direct-paste components are derived from the author's
existing reMarkable Type and desktop projects. Their component directories include
corresponding source, attribution and third-party notices. XOVI's LGPL-3.0
source/relinking notices, full source and build commands are included under
`paperdesk/vendor/tablet`. Consult each vendored component for its exact provenance.
