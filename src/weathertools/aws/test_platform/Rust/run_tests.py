import os
import subprocess
import json
import time
import shutil
import re
import boto3
from datetime import datetime

# ================= HELPER FUNCTIONS =================
def run_cmd_raw(cmd, cwd=None, env=None):
    """Run shell command without modifying PATH. Returns (exit_code, stdout, stderr)."""
    try:
        final_env = os.environ.copy()
        if env:
            final_env.update(env)
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=final_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)


def run_cmd(cmd, cwd=None, env=None):
    """Run shell command and force Rust 1.88. Returns (exit_code, stdout, stderr)."""
    final_env = os.environ.copy()
    final_env["PATH"] = f"{RUST_BIN_PATH}:" + final_env.get("PATH", "")
    if env:
        final_env.update(env)

    print(f"\n💻 Running command: {' '.join(cmd)} (cwd={cwd})")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=final_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # Detect Rust version if rustc or cargo is called
    if "rustc" in cmd[0] or "cargo" in cmd[0]:
        rv_code, rv_out, rv_err = run_cmd_raw([f"{RUST_BIN_PATH}/rustc", "--version"])
        if rv_code == 0:
            print(f"🔧 Using Rust version: {rv_out.strip()}")
        else:
            print(f"⚠️ Could not detect Rust version: {rv_err.strip()}")

    print(f"✅ Command finished with exit code {result.returncode}")
    if result.stdout:
        print(f"📜 stdout:\n{result.stdout.strip()}")
    if result.stderr:
        print(f"⚠️ stderr:\n{result.stderr.strip()}")
    return result.returncode, result.stdout, result.stderr

# ================= CONFIG =================
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "rustv1/examples"
S3_BUCKET = "weathertop2"

# ================= RUST FIX =================
def get_rust_bin_path():
    """Get Rust binary folder for the default toolchain."""
    code, out, err = run_cmd_raw(["rustup", "which", "cargo"])
    if code != 0:
        raise RuntimeError(f"Cannot detect cargo path: {err}")
    return os.path.dirname(out.strip())

RUST_BIN_PATH = get_rust_bin_path()

# ================= REPO MANAGEMENT =================
def clone_repo():
    if os.path.exists(CLONE_DIR):
        print(f"🗑️ Removing old repo at {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)
    print(f"📥 Cloning repo {REPO_URL}...")
    code, out, err = run_cmd(["git", "clone", "--depth", "1", REPO_URL, CLONE_DIR])
    if code != 0:
        raise RuntimeError(f"Git clone failed: {err}")
    root_cargo = os.path.join(CLONE_DIR, ROOT_TEST_DIR, "Cargo.toml")
    if os.path.exists(root_cargo):
        os.remove(root_cargo)

def discover_services():
    services = []
    for entry in os.scandir(os.path.join(CLONE_DIR, ROOT_TEST_DIR)):
        if entry.is_dir():
            cargo_file = os.path.join(entry.path, "Cargo.toml")
            if os.path.exists(cargo_file):
                services.append(entry.name)
    print(f"📦 Discovered services: {services}")
    return sorted(services)

def has_tests(service_dir):
    """Detect if the Rust crate has any tests."""
    tests_folder = os.path.join(service_dir, "tests")
    if os.path.exists(tests_folder) and os.listdir(tests_folder):
        return True
    for root, _, files in os.walk(service_dir):
        for f in files:
            if f.endswith(".rs"):
                with open(os.path.join(root, f), encoding="utf-8") as fd:
                    if "#[test]" in fd.read():
                        return True
    return False

# ================= BUILD & TEST =================
def stage1_build(service_dir, service):
    print(f"🔨 Building {service}...")
    return run_cmd([f"{RUST_BIN_PATH}/cargo", "build"], cwd=service_dir)

def stage2_test(service_dir, service):
    print(f"🧪 Testing {service}...")
    return run_cmd([f"{RUST_BIN_PATH}/cargo", "test", "--quiet"], cwd=service_dir)

def cleanup_build(service_dir, service):
    """Remove build artifacts for a service to save space."""
    print(f"🗑️ Cleaning build artifacts for {service}...")
    code, out, err = run_cmd([f"{RUST_BIN_PATH}/cargo", "clean"], cwd=service_dir)
    if code != 0:
        print(f"⚠️ Cleanup failed for {service}: {err}")

def parse_test_output(output: str):
    """Parse cargo test output for counts."""
    summary = {"tests": 0, "passed": 0, "failed": 0, "ignored": 0}
    match = re.search(r"test result: .*? (\d+) passed; (\d+) failed; (\d+) ignored;", output)
    if match:
        passed, failed, ignored = map(int, match.groups())
        summary["tests"] = passed + failed + ignored
        summary["passed"] = passed
        summary["failed"] = failed
        summary["ignored"] = ignored
    print(f"📊 Parsed test summary: {summary}")
    return summary

# ================= S3 UPLOAD =================
def upload_to_s3(filename, bucket):
    s3 = boto3.client("s3")
    key = os.path.basename(filename)
    print(f"📤 Uploading {filename} to S3 bucket: {bucket}/{key}")
    s3.upload_file(filename, bucket, key)
    print(f"✅ Uploaded successfully")

# ================= MAIN =================
def main():
    start_time = int(time.time() * 1000)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
    results_file = f"rustv1-{timestamp}.json"

    print(f"🕒 Test run started at {datetime.now().isoformat()}")
    clone_repo()
    services = discover_services()

    summary = {"services": 0, "tests": 0, "passed": 0, "failed": 0, "ignored": 0,
               "start_time": start_time, "stop_time": None}

    tests_array = []
    no_tests = []
    services_tested = []

    for service in services:
        print(f"\n--- Processing service: {service} ---")
        service_dir = os.path.join(CLONE_DIR, ROOT_TEST_DIR, service)

        if not has_tests(service_dir):
            print(f"⚠️  Skipping service {service} as there are no tests")
            no_tests.append(service)
            continue

        services_tested.append(service)
        service_result = {"build": None, "tests": None, "parsed": None}

        # Stage 1: build
        build_code, build_out, build_err = stage1_build(service_dir, service)
        service_result["build"] = {"exit_code": build_code, "stdout": build_out, "stderr": build_err}

        if build_code != 0:
            print(f"❌ Build failed for {service}")
            failed_tests = 1
            summary["failed"] += failed_tests
            summary["tests"] += failed_tests
            summary["services"] += 1
            tests_array.append({"service": service, "test_name": "build",
                                "status": "failed", "message": build_err.strip()})
            cleanup_build(service_dir, service)
            continue

        # Stage 2: test
        test_code, test_out, test_err = stage2_test(service_dir, service)
        combined_out = test_out + "\n" + test_err
        parsed = parse_test_output(combined_out)

        service_result["tests"] = {"exit_code": test_code, "stdout": test_out, "stderr": test_err}
        service_result["parsed"] = parsed

        # Aggregate results
        summary["services"] += 1
        summary["passed"] += parsed["passed"]
        summary["failed"] += parsed["failed"]
        summary["ignored"] += parsed["ignored"]
        summary["tests"] = summary["passed"] + summary["failed"] + summary["ignored"]

        if parsed["failed"] > 0:
            tests_array.append({"service": service, "test_name": "result",
                                "status": "failed", "message": combined_out.strip()})

        # Cleanup
        cleanup_build(service_dir, service)

    summary["stop_time"] = int(time.time() * 1000)
    print(f"🕒 Test run finished at {datetime.now().isoformat()}")

    final_results = {"schema-version": "0.0.1", "results": {
        "tool": "rust",
        "summary": summary,
        "tests": tests_array,
        "no_tests": no_tests,
        "services_tested": services_tested
    }}

    # Write JSON results
    with open(results_file, "w") as f:
        json.dump(final_results, f, indent=2)
    print(f"\n📊 Final Results written to {results_file}")
    print(json.dumps(final_results, indent=2))

    # Upload to S3
    upload_to_s3(results_file, S3_BUCKET)


if __name__ == "__main__":
    main()

