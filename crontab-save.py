#!/usr/bin/env python3
"""Точка входа: python3 crontab-save.py [gui | run | install-cron | remove-cron | status]."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from crontab_save.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
