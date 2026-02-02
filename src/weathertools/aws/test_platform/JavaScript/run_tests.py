import os
import subprocess
import json
import time
import shutil
import boto3
import re
from datetime import datetime

# === CONFIG ===
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "javascriptv3/example_code"
S3_BUCKET_NAME = "weathertop2"

# === UTILS ===
def run_command(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout + "\n" + e.stderr

def parse_js_test_results(output):
    """Parse vitest output summary (passed, failed, skipped)."""
    passed = failed = skipped = 0
    for line in output.splitlines():
        line = line.strip()
        if re.search(r"(\d+)\s+passed", line):
            passed = int(re.search(r"(\d+)\s+passed", line).group(1))
        if re.search(r"(\d+)\s+failed", line):
            failed = int(re.search(r"(\d+)\s+failed", line).group(1))
        if re.search(r"(\d+)\s+skipped", line):
            skipped = int(re.search(r"(\d+)\s+skipped", line).group(1))
    return passed, failed, skipped

def extract_failures(output, service_name, test_index_start):
    failures = []
    lines = output.splitlines()
    current_block = []
    test_index = test_index_start
    in_failure = False
    current_test_name = None

    for line in lines:
        # Vitest marks failures with ×, FAIL, or ERROR
        if "×" in line or "FAIL" in line or "ERROR" in line:
            in_failure = True
            test_name_match = re.search(r"[\w\./-]+", line)
            if test_name_match:
                current_test_name = test_name_match.group(0)

        if in_failure:
            current_block.append(line)
            if line.strip() == "":
                failures.append({
                    "service": service_name,
                    "test_name": current_test_name or f"test_{test_index}",
                    "status": "failed",
                    "message": "\n".join(current_block).strip()
                })
                current_block = []
                current_test_name = None
                in_failure = False
                test_index += 1

    if current_block:
        failures.append({
            "service": service_name,
            "test_name": current_test_name or f"test_{test_index}",
            "status": "failed",
            "message": "\n".join(current_block).strip()
        })
        test_index += 1

    return failures, test_index

def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

# === MAIN ===
def main():
    if os.path.exists(CLONE_DIR):
        print(f"🧹 Removing existing repo directory: {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)

    print(f"📥 Cloning repo: {REPO_URL}")
    returncode, output = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if returncode != 0:
        print("❌ Failed to clone repo.")
        return

    total_passed = total_failed = total_skipped = 0
    failed_tests = []
    service_details = []
    no_tests = []
    services_tested = 0
    test_index = 1
    start_time = int(time.time() * 1000)

    # Crawl all service folders
    services = sorted(os.listdir(os.path.join(CLONE_DIR, ROOT_TEST_DIR)))
    for order, service in enumerate(services, start=1):
        service_path = os.path.join(CLONE_DIR, ROOT_TEST_DIR, service)
        if not os.path.isdir(service_path):
            continue

        test_dir = os.path.join(service_path, "tests")
        test_dir_alt = os.path.join(service_path, "test")
        has_tests = os.path.exists(test_dir) or os.path.exists(test_dir_alt)

        if not has_tests:
            print(f"⚠️ No tests found for: {service}")
            service_details.append({
                "service_name": service.lower(),
                "order_tested": order,
                "tests_run": 0,
                "passed": 0,
                "failed": 0,
                "has_tests": False
            })
            no_tests.append(service.lower())
            continue

        print(f"\n📦 Installing NPM dependencies for: {service}")
        run_command(["npm", "install"], cwd=service_path)

        print(f"🧪 Running tests for: {service}")
        returncode, output = run_command(["npx", "vitest", "--run"], cwd=service_path)
        print(output)

        passed, failed, skipped = parse_js_test_results(output)
        total_passed += passed
        total_failed += failed
        total_skipped += skipped

        if failed > 0:
            failures, test_index = extract_failures(output, service, test_index)
            failed_tests.extend(failures)

        service_details.append({
            "service_name": service.lower(),
            "order_tested": order,
            "tests_run": passed + failed + skipped,
            "passed": passed,
            "failed": failed,
            "has_tests": True
        })

        services_tested += 1

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed + total_skipped

    print("\n===== ✅ Final Test Summary =====")
    print(f"Services Scanned: {len(services)}")
    print(f"Services With Tests: {services_tested}")
    print(f"Total Tests: {total_tests} (Passed {total_passed}, Failed {total_failed}, Skipped {total_skipped})")

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "javascript",
            "summary": {
                "services": len(services),
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "service_details": service_details,
            "tests": failed_tests,
            "no_tests": no_tests
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"javascriptv3-{now}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"📁 Wrote schema to local file: {filename}")

    upload_to_s3(filename, S3_BUCKET_NAME, filename)

    # Print JSON to stdout for visibility
    print("\n===== 📊 Final JSON Schema =====")
    print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()
