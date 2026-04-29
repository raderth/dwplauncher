#!/usr/bin/env python3
"""
main.py  –  DWP Launcher entry point.

Usage:
    python main.py
    python main.py --server http://playdwp.net:5000 --map http://playdwp.net:8080 --dir ./my_game_files

Requirements:
    pip install pywebview msal requests
    pip install Pillow  # optional, for mod icons
    pip install psutil  # optional, for RAM detection
"""
import argparse
from ui.app import run

def main():
    parser = argparse.ArgumentParser(description="DWP Launcher")
    parser.add_argument("--server", default="http://private.playdwp.net:5000",
                        help="Launcher server URL")
    parser.add_argument("--dir",    default="./content",
                        help="Game directory")
    args = parser.parse_args()
    run(args.server, args.dir)

if __name__ == "__main__":
    main()