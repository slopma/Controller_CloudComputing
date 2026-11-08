#!/usr/bin/env python3
"""Punto de entrada directo para ControllerASG."""

import os
import sys

# Asegurar que el directorio raíz está en sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controllerasg.main import main

if __name__ == "__main__":
    main()
