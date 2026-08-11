#!/usr/bin/env python3
"""Write a kernel buffer to a fixed path via filp_open (cmpunlocker RE helper)."""
from __future__ import annotations

import pathlib
import sys

ANCHOR = """NV_STATUS NV_API_CALL os_write_file
(
    void *pFile,
    NvU8 *pBuffer,
    NvU64 size,
    NvU64 offset
)
{
"""

PATCH = """NV_STATUS NV_API_CALL os_cmpWritePathFile
(
    const char *path,
    NvU8 *pBuffer,
    NvU64 size
)
{
#if NV_FILESYSTEM_ACCESS_AVAILABLE
    struct file *file;
    loff_t pos = 0;
    NV_STATUS status = NV_OK;

    if ((path == NULL) || (pBuffer == NULL) || (size == 0))
        return NV_ERR_INVALID_ARGUMENT;

    if (current->fs == NULL)
        return NV_ERR_OPERATING_SYSTEM;

    file = filp_open(path, O_WRONLY | O_CREAT | O_TRUNC | O_LARGEFILE, 0644);
    if (IS_ERR(file))
        return NV_ERR_OPERATING_SYSTEM;

    if (os_write_file((void *)file, pBuffer, size, 0) != NV_OK)
        status = NV_ERR_OPERATING_SYSTEM;

    os_close_file((void *)file);
    return status;
#else
    return NV_ERR_NOT_SUPPORTED;
#endif
}

NV_STATUS NV_API_CALL os_write_file
(
    void *pFile,
    NvU8 *pBuffer,
    NvU64 size,
    NvU64 offset
)
{
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <kernel-open/nvidia/os-interface.c>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()
    if "os_cmpWritePathFile" in text:
        print(f"{path}: already patched")
        return 0
    if ANCHOR not in text:
        print(f"{path}: anchor not found", file=sys.stderr)
        return 1
    path.write_text(text.replace(ANCHOR, PATCH, 1))
    print(f"{path}: added os_cmpWritePathFile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
