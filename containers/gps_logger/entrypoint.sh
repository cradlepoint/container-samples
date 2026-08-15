#!/bin/sh
# No config generation needed for this sample -- exec straight into the
# Python process so it becomes PID 1.
exec python3 /opt/app/gps_logger.py
