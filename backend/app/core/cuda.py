"""Make NVIDIA's CUDA DLLs findable on Windows.

CTranslate2 links against cuBLAS and cuDNN but does not ship them. On Linux they
usually arrive with the system CUDA toolkit; on Windows they do not, and the
failure is an unhelpful one:

    RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

The libraries are on disk — pip installs them under ``site-packages/nvidia/*/bin``
as a dependency of ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12`` — they are
simply not on any search path CTranslate2 will look at.

Both mechanisms below are needed, and it is worth saying why, because the obvious
one alone silently does nothing:

* ``os.add_dll_directory`` is the modern API, but it only affects loads that opt
  into the user-directory search (``LOAD_LIBRARY_SEARCH_USER_DIRS``). CTranslate2
  resolves cuBLAS lazily, at the first GPU call, with a plain ``LoadLibrary`` —
  so it never consults that list, and the fix appears to have no effect.

* Prepending to ``PATH`` is what actually works, because a plain ``LoadLibrary``
  does search it.

Doing this in code rather than telling everyone to edit their PATH means a fresh
clone works after ``pip install`` and nothing else, which is the point.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_initialized = False


def ensure_cuda_libraries() -> None:
    """Register the pip-installed NVIDIA library directories for DLL loading.

    A no-op off Windows, and safe to call repeatedly.
    """
    global _initialized
    if _initialized or sys.platform != "win32":
        return
    _initialized = True

    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not nvidia_root.is_dir():
        logger.warning(
            "NVIDIA runtime libraries not found under %s. If transcription fails with "
            "'cublas64_12.dll is not found', install them: "
            "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
            nvidia_root,
        )
        return

    bin_dirs = sorted(nvidia_root.glob("*/bin"))
    if not bin_dirs:
        logger.warning("No NVIDIA library directories under %s.", nvidia_root)
        return

    for bin_dir in bin_dirs:
        os.add_dll_directory(str(bin_dir))

    # The one that actually does the work. See the module docstring.
    os.environ["PATH"] = os.pathsep.join(
        [*(str(d) for d in bin_dirs), os.environ.get("PATH", "")]
    )
    logger.debug("Registered CUDA library directories: %s", bin_dirs)
