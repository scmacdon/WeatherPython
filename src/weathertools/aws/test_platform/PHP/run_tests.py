import os
import subprocess
import json
import time
import shutil
import re
from datetime import datetime, timezone
import boto3

# === CONFIG ===
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
PHP_ROOT = "php/example_code"
S3_BUCKET_NAME = "weathertop2"

# Services to skip testing
SKIP_SERVICES = {"bedrock-agent-runtime"}

# === UTILS ===
def run_command(command, cwd=None):
    env = os.environ.copy()
    in_fargate = "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" in env or "AWS_CONTAINER_CREDENTIALS_FULL_URI" in env
    if in_fargate:
        # ✅ Strip profile creds, enforce ECS Task Role use
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"]:
            env.pop(key, None)
        env["AWS_REGION"] = env.get("AWS_REGION", "us-east-1")
        env["AWS_DEFAULT_REGION"] = env["AWS_REGION"]
        env["AWS_SDK_LOAD_CONFIG"] = "0"
        env["AWS_EC2_METADATA_DISABLED"] = "false"
        if "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" in os.environ:
            env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] = os.environ["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"]
        if "AWS_CONTAINER_CREDENTIALS_FULL_URI" in os.environ:
            env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
        print("✅ ECS/Fargate detected. Using task role credentials.")
    else:
        print("ℹ️ Using existing environment credentials if available.")
    env["HOME"] = env.get("HOME", "/root")

    try:
        print(f"Running command: {' '.join(command)}")
        result = subprocess.run(command, cwd=cwd, env=env, check=True, text=True, capture_output=True)
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, (e.stdout or "") + "\n" + (e.stderr or "")

def parse_phpunit_output(output):
    passed = failed = skipped = 0
    match_ok = re.search(r"OK\s*\((\d+)\s+tests?", output)
    if match_ok:
        passed = int(match_ok.group(1))
        return passed, 0, 0
    match_summary = re.search(r"Tests:\s*(\d+),.*Failures:\s*(\d+),.*Skipped:\s*(\d+)", output, re.DOTALL)
    if match_summary:
        total = int(match_summary.group(1))
        failed = int(match_summary.group(2))
        skipped = int(match_summary.group(3))
        passed = total - failed - skipped
        return passed, failed, skipped
    if "FAILURES!" in output:
        failed_count = len(re.findall(r"\d+\) ", output))
        return 0, failed_count, 0
    return 0, 0, 0

def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

# === MAIN ===
def main():
    # Remove existing clone
    if os.path.exists(CLONE_DIR):
        print(f"🧹 Removing existing repo directory: {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)

    # Clone repo
    print(f"📥 Cloning repo: {REPO_URL}")
    returncode, output = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if returncode != 0:
        print("❌ Failed to clone repo.")
        print(output)
        return

    php_example_root = os.path.join(CLONE_DIR, PHP_ROOT)
    if not os.path.exists(php_example_root):
        print(f"❌ PHP root directory not found: {php_example_root}")
        return

    # Run composer install at root
    composer_json_root = os.path.join(php_example_root, "composer.json")
    if os.path.exists(composer_json_root):
        print(f"📦 Installing PHP dependencies at {php_example_root}...")
        returncode, output = run_command(["composer", "install", "--no-interaction", "--prefer-dist", "--no-progress"], cwd=php_example_root)
        if returncode != 0:
            print("❌ Composer install failed at root.")
            print(output)
            return

    services = sorted([d for d in os.listdir(php_example_root) if os.path.isdir(os.path.join(php_example_root, d))])

    total_passed = total_failed = total_skipped = 0
    tests_array = []
    service_details = []
    no_tests_list = []

    for idx, service_name in enumerate(services, 1):
        if service_name in SKIP_SERVICES:
            print(f"⏭️ Skipping service: {service_name}")
            service_details.append({
                "service_name": service_name,
                "order_tested": idx,
                "tests_run": 0,
                "passed": 0,
                "failed": 0,
                "has_tests": False,
                "skipped_manually": True
            })
            continue

        service_root = os.path.join(php_example_root, service_name)
        test_folder = os.path.join(service_root, "tests")
        if not os.path.isdir(test_folder):
            print(f"⚠️ No tests folder for service: {service_name}")
            no_tests_list.append(service_name)
            service_details.append({
                "service_name": service_name,
                "order_tested": idx,
                "tests_run": 0,
                "passed": 0,
                "failed": 0,
                "has_tests": False
            })
            continue

        # Determine PHPUnit binary (use vendor-installed phpunit from example root)
        phpunit_bin_vendor = os.path.join(php_example_root, "vendor", "bin", "phpunit")
        phpunit_bin_global = "/usr/local/bin/phpunit"
        phpunit_bin = phpunit_bin_vendor if os.path.isfile(phpunit_bin_vendor) else phpunit_bin_global
        if not os.path.isfile(phpunit_bin):
            print(f"❌ PHPUnit not found for service: {service_name}, skipping")
            continue

        # Collect all test files
        test_files = [os.path.join(test_folder, f) for f in os.listdir(test_folder) if f.endswith(".php")]

        tests_run = 0
        passed = failed = skipped = 0

        # ✅ Run each test with CWD forced to php_example_root (autoload + bootstrap fix)
        vendor_autoload = os.path.join(php_example_root, "vendor", "autoload.php")
        php_cwd = php_example_root

        for test_file in test_files:
            cmd = [phpunit_bin, "--colors=never", "--bootstrap", vendor_autoload, test_file]
            returncode, output = run_command(cmd, cwd=php_cwd)
            print(f"PHPUnit output for {service_name} ({os.path.basename(test_file)}):\n{output}")
            p, f, s = parse_phpunit_output(output)
            passed += p
            failed += f
            skipped += s
            tests_run += p + f + s

            if f > 0:
                tests_array.append({
                    "service": service_name,
                    "test_name": os.path.basename(test_file),
                    "status": "failed",
                    "message": output.strip(),
                    "order_tested": idx
                })

        total_passed += passed
        total_failed += failed
        total_skipped += skipped

        service_details.append({
            "service_name": service_name,
            "order_tested": idx,
            "tests_run": tests_run,
            "passed": passed,
            "failed": failed,
            "has_tests": True
        })

    total_tests = total_passed + total_failed + total_skipped
    pass_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0

    # ✅ Array of services that actually ran tests
    services_tested_list = [d["service_name"] for d in service_details if d.get("has_tests")]

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "phpunit",
            "summary": {
                "services": len(services_tested_list),   # <-- only services that ran tests
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "pass_rate": round(pass_rate, 2),
                "start_time": int(time.time() * 1000),
                "stop_time": int(time.time() * 1000)
            },
            "tests": tests_array,
            "no_tests": no_tests_list,
            "services_tested": services_tested_list
        }
    }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    filename = f"php-{now}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"📁 Wrote schema to local file: {filename}")

    upload_to_s3(filename, S3_BUCKET_NAME, filename)

    print("\n=== FINAL JSON ===")
    print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()
