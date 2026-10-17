"""MCPB entry point. Dependencies are bundled in ./lib; site.addsitedir (unlike PYTHONPATH) also processes
their .pth files, which pywin32 needs to register its modules and DLLs."""

import os
import site

site.addsitedir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from etabs_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main([])
