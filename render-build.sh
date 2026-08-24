#!/usr/bin/env bash
set -e

pip install -r backend/requirements.txt
playwright install chromium
