import os
import subprocess
import json
import time
import shutil
import boto3
from datetime import datetime

# === CONFIG ===
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "gov2"
S3_BUCKET_NAME = "weathertop2"

# === UTILS ===
def run_command(command, cwd=None):
    """Run a shell command and return (exit_code, output)."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout + "\n" + e.stderr

def parse_go_test_results(output):
    """Count test results from go test output."""
    passed = failed = skipped = 0
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("--- PASS:"):
            passed += 1
        elif line.startswith("--- FAIL:"):
            failed += 1
        elif "skipped" in line.lower():
            skipped += 1
    return passed, failed, skipped

def extract_failures(output, service_name):
    """Extract failed test details from go test output in required JSON shape."""
    failures = []
    lines = output.splitlines()
    current = []
    in_failure = False

    for line in lines:
        if line.startswith("--- FAIL:"):
            in_failure = True
        if in_failure:
            current.append(line)
            if line.startswith("FAIL") or line.strip() == "":
                failures.append({
                    "service": service_name,
                    "test_name": "FAIL",
                    "status": "failed",
                    "message": "\n".join(current).strip()
                })
                current = []
                in_failure = False

    if current:
        failures.append({
            "service": service_name,
            "test_name": "FAIL",
            "status": "failed",
            "message": "\n".join(current).strip()
        })

    return failures

def upload_to_s3(local_file, bucket_name, s3_key):
    """Upload a file to S3."""
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

# === MAIN ===
def main():
    # Clean up old clone
    if os.path.exists(CLONE_DIR):
        print(f"🧹 Removing existing repo directory: {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)

    # Clone repo
    print(f"📥 Cloning repo: {REPO_URL}")
    returncode, output = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if returncode != 0:
        print("❌ Failed to clone repo.")
        return

    total_passed = total_failed = total_skipped = 0
    failed_tests = []
    services_tested = 0
    no_tests = []
    start_time = int(time.time() * 1000)

    service_root = os.path.join(CLONE_DIR, ROOT_TEST_DIR)

    # 🔑 Dynamically discover service dirs in alphabetical order
    services = [
        d for d in os.listdir(service_root)
        if os.path.isdir(os.path.join(service_root, d))
    ]
    services.sort()

    for service in services:
        service_path = os.path.join(service_root, service)

        # Check if service has any tests
        returncode, test_list = run_command(["go", "test", "-list", "."], cwd=service_path)
        test_list = test_list.strip().splitlines()
        test_names = [t for t in test_list if t and not t.startswith("?")]
        if not test_names:
            print(f"⚠️  No tests found for service: {service}")
            no_tests.append(service)
            continue

        print(f"\n📦 Installing Go dependencies for: {service}")
        run_command(["go", "mod", "download"], cwd=service_path)

        print(f"🧪 Running tests for: {service}")
        returncode, output = run_command(["go", "test", "-v", "./..."], cwd=service_path)
        print(output)

        passed, failed, skipped = parse_go_test_results(output)
        total_passed += passed
        total_failed += failed
        total_skipped += skipped

        if failed > 0:
            failures = extract_failures(output, service)
            failed_tests.extend(failures)

        services_tested += 1

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed + total_skipped

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "go",
            "summary": {
                "services": services_tested,
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "tests": failed_tests,
            "no_tests": no_tests
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"gov2-{now}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"\n📁 Wrote schema to local file: {filename}")

    upload_to_s3(filename, S3_BUCKET_NAME, filename)

    # 🔑 Always display the final JSON in console
    print("\n===== 📊 Final JSON Schema =====")
    print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()
