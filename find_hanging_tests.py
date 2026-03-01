"""Find which test files hang by running each with a timeout."""
import subprocess
import glob
import sys

TIMEOUT = 25  # seconds per file

files = sorted(glob.glob("tests/test_*.py"))
print(f"Testing {len(files)} files with {TIMEOUT}s timeout each...")
hangs = []
fails = []

for i, f in enumerate(files):
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f, "--no-cov", "-q", "--tb=no", "-x"],
            capture_output=True, text=True, timeout=TIMEOUT,
            env={"TESTING": "1", "PYTHONPATH": "/root/availai",
                 "PATH": "/usr/local/bin:/usr/bin:/bin",
                 "HOME": "/root", "RATE_LIMIT_ENABLED": "false"},
        )
        last = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else "no output"
        status = "FAIL" if r.returncode != 0 else "OK"
        if r.returncode != 0:
            fails.append(f)
        print(f"[{i+1}/{len(files)}] {status}: {f} | {last}")
    except subprocess.TimeoutExpired:
        hangs.append(f)
        print(f"[{i+1}/{len(files)}] HANG: {f} (>{TIMEOUT}s)")

print(f"\n=== SUMMARY ===")
print(f"Total: {len(files)}, Hangs: {len(hangs)}, Fails: {len(fails)}")
if hangs:
    print("Hanging files:")
    for h in hangs:
        print(f"  - {h}")
