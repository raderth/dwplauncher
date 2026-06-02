#!/usr/bin/env python3
"""
main.py  –  DWP Launcher entry point.
"""
import argparse
from ui.app import run

def main():
    parser = argparse.ArgumentParser(description="DWP Launcher")
    parser.add_argument("--remote-config", default="http://private.playdwp.net/config",
                        help="URL of the remote mod/version config JSON")
    parser.add_argument("--dir", default="./content", help="Game directory")
    args = parser.parse_args()
    run(args.remote_config, args.dir)

if __name__ == "__main__":
    main()