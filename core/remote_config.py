"""
core/remote_config.py  –  Fetch the launcher config from a remote JSON URL.
"""
import json
from urllib.request import urlopen

def fetch_remote_config(url: str) -> dict:
    print(f"[CONFIG] Fetching remote config from: {url}")
    try:
        with urlopen(url) as resp:
            data = json.loads(resp.read())
        print(f"[CONFIG] Successfully loaded config with version: {data.get('version')}")
        print(f"[CONFIG] Config keys: {list(data.keys())}")
        return data
    except Exception as e:
        print(f"[CONFIG] Error fetching {url}: {type(e).__name__}: {str(e)}")
        raise