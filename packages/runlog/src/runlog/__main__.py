import sys

from .cli import main

if __name__ == "__main__":  # the emulator child is spawned, which re-imports __main__
    sys.exit(main())
