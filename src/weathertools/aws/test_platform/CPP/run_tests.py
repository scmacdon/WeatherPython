import os
import subprocess
import json
import time
import shutil
import boto3
from datetime import datetime

# ================= CONFIG =================
AWS_SDK_VERSION = "1.11.379"
AWS_SDK_DIR = "/app/aws-sdk-cpp"
AWS_INSTALL_PREFIX = "/app/aws-sdk-install"
REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "cpp/example_code"
S3_BUCKET_NAME = "weathertop2"

# ================= UTILS =================
def run_command(command, cwd=None):
    """Run a shell command and capture output."""
    try:
        result = subprocess.run(
            command, cwd=cwd, check=True, text=True, capture_output=True
        )
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout + "\n" + e.stderr

def ordinal(n):
    """Return ordinal string for an integer: 1 -> 1st, 2 -> 2nd, etc."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

# ================= STAGE 1: Build AWS SDK C++ =================
def stage_build_aws_sdk():
    header_path = os.path.join(AWS_INSTALL_PREFIX, "include/aws/core/Aws.h")
    lib_path_so = os.path.join(AWS_INSTALL_PREFIX, "lib/libaws-cpp-sdk-core.so")
    lib_path_a = os.path.join(AWS_INSTALL_PREFIX, "lib/libaws-cpp-sdk-core.a")

    if os.path.exists(header_path) and (os.path.exists(lib_path_so) or os.path.exists(lib_path_a)):
        print("✅ AWS SDK headers and libraries already installed.")
        return True

    if os.path.exists(AWS_SDK_DIR):
        shutil.rmtree(AWS_SDK_DIR)
    if os.path.exists(AWS_INSTALL_PREFIX):
        shutil.rmtree(AWS_INSTALL_PREFIX)
    os.makedirs(AWS_INSTALL_PREFIX, exist_ok=True)

    rc, out = run_command([
        "git", "clone", "--recurse-submodules",
        "--branch", AWS_SDK_VERSION,
        "https://github.com/aws/aws-sdk-cpp.git",
        AWS_SDK_DIR
    ])
    print(out)
    if rc != 0:
        print("❌ Failed to clone AWS SDK C++ repo.")
        return False

    build_dir = os.path.join(AWS_SDK_DIR, "build")
    os.makedirs(build_dir, exist_ok=True)

    cmake_cmd = [
        "cmake", "..",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_ONLY=core;s3;dynamodb",
        "-DBUILD_SHARED_LIBS=ON",
        "-DENABLE_TESTING=OFF",
        "-DLEGACY_BUILD=ON",
        "-DBUILD_DEPS=ON",
        "-DENABLE_RTTI=ON",
        f"-DCMAKE_INSTALL_PREFIX={AWS_INSTALL_PREFIX}",
        f"-DCMAKE_INSTALL_RPATH={AWS_INSTALL_PREFIX}/lib"
    ]
    rc, out = run_command(cmake_cmd, cwd=build_dir)
    print(out)
    if rc != 0:
        return False

    build_install_cmd = [
        "cmake", "--build", ".", "--target", "install",
        f"-j{os.cpu_count()}", "--verbose"
    ]
    rc, out = run_command(build_install_cmd, cwd=build_dir)
    print(out)
    if rc != 0:
        return False

    if os.path.exists(header_path) and (os.path.exists(lib_path_so) or os.path.exists(lib_path_a)):
        print(f"🎉 AWS SDK C++ installed successfully under {AWS_INSTALL_PREFIX}!")
        return True
    return False

# ================= STAGE 2: Build and Test Examples =================
def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

def stage_build_and_test_examples():
    if os.path.exists(CLONE_DIR):
        shutil.rmtree(CLONE_DIR)

    rc, out = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if rc != 0:
        print("❌ Failed to clone repo.")
        print(out)
        return

    root_path = os.path.join(CLONE_DIR, ROOT_TEST_DIR)
    if not os.path.exists(root_path):
        print(f"❌ Root test directory not found: {root_path}")
        return

    total_passed = total_failed = 0
    failed_tests = []
    services_tested = 0
    global_test_index = 1
    start_time = int(time.time() * 1000)
    service_summary = []

    for service_root in os.listdir(root_path):
        service_path = os.path.join(root_path, service_root)
        test_dir = os.path.join(service_path, "tests")
        if not os.path.exists(test_dir):
            continue

        services_tested += 1
        print(f"\n===== 🛠 Building tests for {service_root} =====")
        print(f"ℹ️ {service_root} is the {ordinal(services_tested)} service tested")

        build_dir = os.path.join(test_dir, "build")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir, exist_ok=True)

        rc, out = run_command([
            "cmake", "..",
            f"-DCMAKE_PREFIX_PATH={AWS_INSTALL_PREFIX}",
            f"-DCMAKE_INSTALL_RPATH={AWS_INSTALL_PREFIX}/lib"
        ], cwd=build_dir)
        print(out)
        if rc != 0:
            continue

        rc, out = run_command(["make", "-j", str(os.cpu_count())], cwd=build_dir)
        print(out)
        if rc != 0:
            continue

        rc, test_output = run_command(["ctest", "--output-on-failure"], cwd=build_dir)
        print(test_output)

        service_passed = sum(1 for line in test_output.splitlines() if "Passed" in line)
        service_failed = sum(1 for line in test_output.splitlines() if "Failed" in line or "failed" in line)
        service_total_tests = service_passed + service_failed

        # Add service summary for JSON
        service_summary.append({
            "service_name": service_root,
            "order_tested": services_tested,
            "tests_run": service_total_tests,
            "passed": service_passed,
            "failed": service_failed
        })

        for line in test_output.splitlines():
            if "Failed" in line or "failed" in line:
                failed_tests.append({
                    "service": service_root,
                    "test_name": f"test_{global_test_index}",
                    "status": "failed",
                    "message": line.strip()
                })
                global_test_index += 1

        total_passed += service_passed
        total_failed += service_failed

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "cpp",
            "summary": {
                "services": services_tested,
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "service_details": service_summary,
            "tests": failed_tests
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"cpp-{now}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)

    upload_to_s3(filename, S3_BUCKET_NAME, filename)

    print("\n===== 📝 JSON Output =====")
    print(json.dumps(schema, indent=2))

# ================= MAIN =================
def main():
    print("\n===== STAGE 1: Build AWS SDK C++ inside /app =====\n")
    if not stage_build_aws_sdk():
        print("❌ Cannot continue without AWS SDK. Exiting.")
        return

    print("\n===== STAGE 2: Build and Test AWS C++ Examples =====\n")
    stage_build_and_test_examples()

if __name__ == "__main__":
    main()





