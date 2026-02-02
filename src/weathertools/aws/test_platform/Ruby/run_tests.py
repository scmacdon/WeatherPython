import os
import subprocess
import json
import time
import shutil
import boto3
from datetime import datetime
import glob
import re

# ================= CONFIG =================
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "ruby/example_code"   # root folder containing service directories
GEMFILE_DIR = "ruby"                  # folder where Gemfile is located
S3_BUCKET_NAME = "weathertop2"

# ================= UTILS =================
def run_command(command, cwd=None, env=None):
    """Run a shell command and capture output."""
    try:
        result = subprocess.run(
            command, cwd=cwd, check=True, text=True, capture_output=True, env=env, shell=False
        )
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout + "\n" + e.stderr
    except FileNotFoundError:
        return 1, f"❌ Command not found: {command[0]}"

def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

# ================= RSpec summary parser =================
def extract_rspec_summary(output):
    """
    Parse RSpec summary line like '34 examples, 0 failures'.
    Returns (total, passed, failed)
    """
    total = failed = passed = 0
    summary_match = re.search(r'(\d+)\s+examples?,\s+(\d+)\s+failures?', output)
    if summary_match:
        total = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        passed = total - failed
    return total, passed, failed

# ================= STAGE 1: Clone + verify dependencies =================
def stage_1_clone_and_verify():
    print("===== STAGE 1: Clone repo and verify dependencies =====")

    if os.path.exists(CLONE_DIR):
        shutil.rmtree(CLONE_DIR)

    rc, out = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if rc != 0:
        print("❌ Failed to clone repo.")
        print(out)
        return False
    print(f"✅ Repo cloned to {CLONE_DIR}")

    gemfile_path = os.path.join(CLONE_DIR, GEMFILE_DIR, "Gemfile")
    if not os.path.exists(gemfile_path):
        print(f"⚠️ No Gemfile found in repo: {gemfile_path}")
        print("🎯 Stage 1 complete: Skipping bundle install.")
        return True

    cwd = os.path.join(CLONE_DIR, GEMFILE_DIR)

    # Install gems from Gemfile
    print(f"🔹 Installing gems from {gemfile_path} ...")
    rc, out = run_command(["bundle", "install"], cwd=cwd)
    if rc != 0:
        print(f"❌ Failed to install gems:\n{out}")
        return False
    print("✅ Gems installed via Gemfile")

    # Inject nokogiri dynamically for AWS XML parsing
    print("🔹 Ensuring nokogiri is installed for XML parsing ...")
    rc, out = run_command(["bundle", "add", "nokogiri", "--skip-install"], cwd=cwd)
    if rc == 0:
        print("✅ Added nokogiri to bundler environment (skip-install).")
    else:
        print(f"⚠️ Could not add nokogiri: {out}")

    rc, out = run_command(["bundle", "install"], cwd=cwd)
    if rc != 0:
        print(f"❌ Failed to install nokogiri:\n{out}")
        return False
    print("✅ Nokogiri installed inside bundler environment.")

    # Verify Bundler
    rc, out = run_command(["bundle", "--version"])
    if rc == 0:
        print(f"✅ Bundler found: {out.strip()}")
    else:
        print(f"❌ Bundler not found:\n{out}")
        return False

    # Verify RSpec
    rc, out = run_command(["bundle", "exec", "rspec", "--version"], cwd=cwd)
    if rc == 0:
        print(f"✅ RSpec found: {out.strip()}")
    else:
        print(f"❌ RSpec not found:\n{out}")
        return False

    print("🎯 Stage 1 complete: All dependencies verified and XML library installed.\n")
    return True

# ================= STAGE 2: Run Ruby tests =================
def stage_2_run_tests():
    print("===== STAGE 2: Run Ruby tests =====\n")

    root_path = os.path.join(CLONE_DIR, ROOT_TEST_DIR)
    if not os.path.exists(root_path):
        print(f"❌ Root test directory not found: {root_path}")
        return

    total_passed = total_failed = 0
    failed_tests = []
    service_details = []
    no_test_services = []
    services_tested = 0
    service_order_mapping = {}
    start_time = int(time.time() * 1000)

    # loop through all service folders under example_code
    for service_root in sorted(os.listdir(root_path)):
        service_path = os.path.join(root_path, service_root)
        if not os.path.isdir(service_path):
            continue

        services_tested += 1
        print(f"\n===== 🔎 Checking service: {service_root} =====")

        # find all test files under this service (any subfolder "tests")
        test_files = glob.glob(os.path.join(service_path, "**", "tests", "**", "test_*.rb"), recursive=True)

        if not test_files:
            print(f"⚠️ No tests found for {service_root}")
            no_test_services.append(service_root)
            service_details.append({
                "service_name": service_root,
                "order_tested": services_tested,
                "tests_run": 0,
                "passed": 0,
                "failed": 0,
                "has_tests": False
            })
            continue

        print(f"🎯 Found {len(test_files)} test files")
        env = os.environ.copy()
        env["RUBYLIB"] = service_path + os.pathsep + env.get("RUBYLIB", "")

        rc, test_output = run_command(
            ["bundle", "exec", "rspec", "--format", "documentation"] + test_files,
            cwd=service_path,
            env=env
        )
        print(test_output)

        # Extract totals from RSpec summary
        service_total, service_passed, service_failed = extract_rspec_summary(test_output)

        service_details.append({
            "service_name": service_root,
            "order_tested": services_tested,
            "tests_run": service_total,
            "passed": service_passed,
            "failed": service_failed,
            "has_tests": True
        })

        total_passed += service_passed
        total_failed += service_failed

        # Capture failed test details
        for line in test_output.splitlines():
            if re.search(r'failed', line, re.IGNORECASE):
                failed_tests.append({
                    "service": service_root,
                    "test_name": f"unknown",
                    "status": "failed",
                    "message": line.strip(),
                    "order_tested": services_tested
                })

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "ruby",
            "summary": {
                "services": services_tested,
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "service_details": service_details,
            "tests": failed_tests,
            "no_tests": no_test_services
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"ruby-{now}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)

    upload_to_s3(filename, S3_BUCKET_NAME, filename)
    print("\n===== 📝 JSON Output =====")
    print(json.dumps(schema, indent=2))

# ================= MAIN =================
def main():
    if stage_1_clone_and_verify():
        stage_2_run_tests()

if __name__ == "__main__":
    main()





