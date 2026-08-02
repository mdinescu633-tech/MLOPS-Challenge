#!/usr/bin/env python3
"""
Simple load test for the /burn endpoint behind Keycloak + oauth2-proxy.

  1. Get an access token from Keycloak (username/password grant).
  2. Spawn N threads, each hammering /burn in a loop for DURATION seconds,
     recording (status_code, latency) for every request.
  3. In parallel, poll `kubectl get pods` + `kubectl top pods` every 5s to
     watch the HPA scale and track CPU/memory usage per pod.
  4. Print total requests, accepted/conflict/error breakdown, latency
     percentiles (p50/p95/p99), and average CPU/memory across pods at the end.

Note: /burn returns 409 Conflict when a pod already has a burn in progress
(it's a single-flight endpoint per pod). That's expected behavior, not an
error, so it's tracked separately from real errors (5xx / connection issues).

Usage:
  export DEVUSER_PASSWORD="your-password"
  export CLIENT_SECRET="your-client-secret"
  python load_test.py
"""

import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import json

# ---------- CONFIG ----------
TARGET_URL = "http://loadtester.127.0.0.1.nip.io/burn"
TOKEN_URL = "http://keycloak.127.0.0.1.nip.io/realms/loadtester-realm/protocol/openid-connect/token"
CLIENT_ID = "loadtester-client"
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
USERNAME = "devuser"
PASSWORD = os.environ["DEVUSER_PASSWORD"]  # export DEVUSER_PASSWORD=... before running

NAMESPACE = "loadtester"
APP_LABEL = "loadtester"

# /burn is single-flight per pod (one pod can only run one burn at a time).
CONCURRENCY = 6    # parallel workers
DURATION = 60      # seconds to run

# When a worker gets a 409 (pod already busy with a burn), back off for this
# long before retrying instead of hammering the endpoint uselessly.
CONFLICT_BACKOFF = 1.5  # seconds
# -----------------------------

results = []          # list of (status_code, latency_seconds)
results_lock = threading.Lock()
stop_event = threading.Event()

cpu_mem_samples = []  # list of (cpu_millicores, mem_mi), one sample per pod per poll


def percentile(values: list, p: float) -> float:
    """Simple linear-interpolation percentile. p is 0-100."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def get_token() -> str:
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": USERNAME,
        "password": PASSWORD,
        "scope": "openid",
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["access_token"]


def worker(token: str, end_time: float):
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < end_time:
        start = time.perf_counter()
        try:
            req = urllib.request.Request(TARGET_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            status = 0  # connection error / timeout
        latency = time.perf_counter() - start
        with results_lock:
            results.append((status, latency))

        # 409 = pod busy (single-flight per pod, see module docstring) —
        # back off instead of hammering the endpoint uselessly.
        if status == 409:
            time.sleep(CONFLICT_BACKOFF)


def monitor_pods():
    while not stop_event.is_set():
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", NAMESPACE, "-l", f"app={APP_LABEL}", "--no-headers"],
            capture_output=True, text=True
        ).stdout
        count = len([line for line in out.splitlines() if line.strip()])

        # kubectl top pods: NAME  CPU(cores)  MEMORY(bytes) -> e.g. "123m" and "45Mi"
        top_out = subprocess.run(
            ["kubectl", "top", "pods", "-n", NAMESPACE, "-l", f"app={APP_LABEL}", "--no-headers"],
            capture_output=True, text=True
        ).stdout
        cpu_line, mem_line = [], []
        for line in top_out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            cpu_raw, mem_raw = parts[1], parts[2]
            try:
                cpu_m = int(cpu_raw.rstrip("m"))
            except ValueError:
                continue
            if mem_raw.endswith("Mi"):
                mem_mi = int(mem_raw.rstrip("Mi"))
            elif mem_raw.endswith("Gi"):
                mem_mi = int(float(mem_raw.rstrip("Gi")) * 1024)
            else:
                continue
            cpu_line.append(cpu_m)
            mem_line.append(mem_mi)
            cpu_mem_samples.append((cpu_m, mem_mi))

        avg_cpu = sum(cpu_line) / len(cpu_line) if cpu_line else None
        avg_mem = sum(mem_line) / len(mem_line) if mem_line else None
        cpu_str = f"{avg_cpu:.0f}m" if avg_cpu is not None else "n/a"
        mem_str = f"{avg_mem:.0f}Mi" if avg_mem is not None else "n/a"

        print(f"[{time.strftime('%H:%M:%S')}] {count} pods running | avg_cpu={cpu_str} avg_mem={mem_str}")
        time.sleep(5)


def main():
    print("1. Getting access token from Keycloak...")
    token = get_token()
    print("Token acquired.")

    print("2. Starting pod monitor in the background...")
    mon_thread = threading.Thread(target=monitor_pods, daemon=True)
    mon_thread.start()

    print(f"3. Firing {CONCURRENCY} concurrent workers against {TARGET_URL} for {DURATION}s...")
    end_time = time.monotonic() + DURATION
    threads = [threading.Thread(target=worker, args=(token, end_time)) for _ in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stop_event.set()
    mon_thread.join(timeout=1)

    # ---- Summary ----
    total = len(results)
    latencies = [lat for _, lat in results]
    avg_latency = sum(latencies) / total if total else 0

    # Single pass over results: build the per-code breakdown once, then
    # derive accepted/conflicts/errors from it instead of re-scanning the
    # list separately for each bucket (202/409/5xx semantics are explained
    # in the module docstring).
    codes = {}
    for status, _ in results:
        codes[status] = codes.get(status, 0) + 1

    accepted = codes.get(202, 0)
    conflicts = codes.get(409, 0)
    errors = sum(count for status, count in codes.items() if status >= 500 or status == 0)
    other = total - accepted - conflicts - errors

    cpus = [c for c, _ in cpu_mem_samples]
    mems = [m for _, m in cpu_mem_samples]
    avg_cpu = sum(cpus) / len(cpus) if cpus else None
    avg_mem = sum(mems) / len(mems) if mems else None

    print("\n===================== SUMMARY =====================")
    print(f"Total requests: {total}")
    print(f"Accepted (202): {accepted} ({(accepted / total * 100) if total else 0:.1f}%)")
    print(f"Conflicts (409, pod already busy): {conflicts} ({(conflicts / total * 100) if total else 0:.1f}%)")
    print(f"Real errors (5xx/conn):            {errors} ({(errors / total * 100) if total else 0:.1f}%)")
    if other:
        print(f"Other status codes:                {other} ({(other / total * 100) if total else 0:.1f}%)")
    print(f"Avg latency:    {avg_latency:.3f}s")
    print(f"p50 latency:    {percentile(latencies, 50):.3f}s")
    print(f"p95 latency:    {percentile(latencies, 95):.3f}s")
    print(f"p99 latency:    {percentile(latencies, 99):.3f}s")
    print(f"Avg CPU/pod:    {avg_cpu:.0f}m" if avg_cpu is not None else "Avg CPU/pod:    n/a")
    print(f"Avg mem/pod:    {avg_mem:.0f}Mi" if avg_mem is not None else "Avg mem/pod:    n/a")
    print("Status code breakdown:")
    for status, count in sorted(codes.items()):
        print(f"  {status}: {count}")
    print("=====================================================")


if __name__ == "__main__":
    main()