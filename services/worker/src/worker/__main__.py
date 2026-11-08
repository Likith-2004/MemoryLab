import sys

from .main import main

if __name__ == "__main__":  # required: the emulator child is spawned, which re-imports __main__
    sys.exit(main())
