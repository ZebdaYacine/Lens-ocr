"""Compatibility entry point; the implementation lives in ``sinlens.ui``."""

from sinlens.ui import *
from sinlens.ui import ClearReadDesktop


if __name__ == "__main__":
    ClearReadDesktop().mainloop()
