# Open-source tablet payloads

This directory contains no proprietary firmware, xochitl binary, SDK,
sysroot, Qt shared libraries, credentials, document data or SSH configuration.
The fixed xochitl digest is an identity check, not a firmware artifact.

`injector.c` is PaperDesk's freestanding Linux uinput keyboard implementation
for ARM32 and AArch64. `touch-injector.c` and `direct-paste/{extension.cpp,
native_pages.h,build.sh}` are adapted from the owner's reMarkable Type
implementation, distributed here under AGPL-3.0-or-later. The direct-paste
binary retains its exact firmware/hash gates and is rebuilt from the
included source. The touch helper is Paper Pro only. Keyboard helpers have
architecture-correct `input_event` layouts and do not depend on tablet libc.

`xovi/` contains upstream XOVI source from
<https://github.com/asivery/xovi>, commit
`2b99649f5e4fd6288be7792a8570bd16418adb70`. Its LGPL-3.0 license and the
incorporated GPL-3.0 license are included. The `uthash`/`utlist` license
notices remain in their source headers. `direct-paste/xovi.c` is an
XOVI-generated extension base; the shared XOVI loader is built from
`xovi/`, rather than redistributed without corresponding source. Modified,
interface-compatible libraries can be rebuilt and the local artifact
inventory regenerated before packaging or installation.

The extensions link dynamically against the tablet's external Qt 6
libraries. Qt LGPL-3.0/GPL-3.0 terms apply; Qt is not copied here. The SDK is
an external toolchain prerequisite; this repository ships no SDK binaries.

## Rebuild

The keyboard requires Clang and LLD, and has no system headers:

```sh
clang --target=armv7-linux-gnueabihf -march=armv7-a -marm -O2 \
  -ffreestanding -fno-builtin -fomit-frame-pointer -nostdlib -static \
  -fuse-ld=lld -Wl,-e,_start injector.c -o bin/injector-armv7l
clang --target=aarch64-linux-gnu -O2 -ffreestanding -fno-builtin \
  -nostdlib -static -fuse-ld=lld -Wl,-e,_start \
  injector.c -o bin/injector-aarch64
```

For the other payloads, set `RM_SDK` to your installed reMarkable SDK
directory and use the included `build-tablet.sh`. The included source pins
correspond to firmware `3.28.0.172` despite the toolchain's SDK version.
The build maps source/SDK prefixes to neutral paths, so assertion filenames
do not embed a user's home directory. After rebuilding, inspect the ELF
architecture, then regenerate `artifacts.json` using
`python paperdesk/vendor/tablet/build_inventory.py` from the repository
root (the script accepts no credentials or remote device).

Source recompilation, transport checksums and simulator tests do not
substitute for testing on a disposable physical tablet. Advanced helpers
should remain firmware-pinned until that validation is complete.
