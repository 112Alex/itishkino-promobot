"""Run inside VPN image, with TUN, NET_ADMIN and /run tmpfs; no network or real keys."""
import importlib.util
from pathlib import Path
import subprocess
import time


Path("/run/secrets").mkdir(exist_ok=True)
native = Path("/run/secrets/awg0.conf")
native.write_text("[Interface]\nH1 = 500-700\nH2 = 1000-2000\n")
native.chmod(0o600)
driver = subprocess.Popen(["amneziawg-go", "-f", "awg0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        if Path("/run/amneziawg/awg0.sock").exists():
            break
        if driver.poll() is not None:
            raise RuntimeError("Driver exited")
        time.sleep(0.05)
    subprocess.run(["awg", "setconf", "awg0", str(native)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    spec = importlib.util.spec_from_file_location("headers", "/app/fix-awg-headers.py")
    headers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(headers)
    result = headers.fix()
    assert result == {"h1_matches": True, "h2_matches": True, "updated": False}, "setconf lost native headers"
    print("PASS: real awg setconf applied H1/H2; no UAPI repair required")
finally:
    driver.terminate()
    driver.wait(timeout=5)
