import os
import subprocess
import json
import datetime
import xml.etree.ElementTree as ET
import boto3
import shutil
import tempfile
import logging
import time
import stat
import traceback

# -------------------
# Logging configuration
# -------------------
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger()

# -------------------
# Constants
# -------------------
GITHUB_REPO = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
LOCAL_REPO_NAME = "aws-doc-sdk-examples"
REPORT_FILE_PREFIX = "kotlin"
S3_BUCKET = "weathertop2"
S3_FOLDER = ""  # optional prefix in S3

# Services to skip (like Kotlin S3 that hangs Docker)
SKIP_SERVICES = {"s3"}

# S3 client
s3 = boto3.client("s3")

# -------------------
# Functions
# -------------------

def clone_repo(temp_dir="/tmp"):
    repo_path = os.path.join(temp_dir, LOCAL_REPO_NAME)
    logger.info(f"📥 Cloning repository into {repo_path}...")
    subprocess.run(["git", "clone", "--depth", "1", GITHUB_REPO, repo_path], check=True)
    return repo_path

def make_gradlew_executable(folder):
    gradlew_path = os.path.join(folder, "gradlew")
    if os.path.exists(gradlew_path):
        st = os.stat(gradlew_path)
        os.chmod(gradlew_path, st.st_mode | stat.S_IEXEC)
        return gradlew_path
    return None

def find_services(repo_path):
    services_root = os.path.join(repo_path, "kotlin", "services")
    if not os.path.isdir(services_root):
        raise RuntimeError(f"Services folder not found: {services_root}")
    # Alphabetical order always
    services = sorted([d for d in os.listdir(services_root) if os.path.isdir(os.path.join(services_root, d))])
    return services

def has_tests(service_path):
    for lang in ["kotlin", "java"]:
        test_root = os.path.join(service_path, "src", "test", lang)
        if os.path.exists(test_root):
            for _, _, files in os.walk(test_root):
                if any(f.endswith(".kt") or f.endswith(".java") for f in files):
                    return True
    return False

def run_gradle_tests(service, repo_path):
    service_path = os.path.join(repo_path, "kotlin", "services", service)
    logger.info(f"🔍 Target service path: {service_path}")

    gradlew_path = make_gradlew_executable(service_path)
    gradle_cmd = "./gradlew" if gradlew_path else "gradle"

    logger.info(f"⚙️ Running Gradle tests in: {service_path}")
    result = subprocess.run(
        [gradle_cmd, "test", "--no-daemon", "--console=plain"],
        cwd=service_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600  # ⏱ safety timeout (10 mins)
    )

    logger.info(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(f"❌ Gradle tests failed for service '{service}'")
    return service_path

def parse_test_results(service_path, service_name):
    results_dir = os.path.join(service_path, "build", "test-results", "test")
    failed_tests = []
    summary = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "pending": 0, "other": 0}

    if not os.path.exists(results_dir):
        return summary, failed_tests

    for file in os.listdir(results_dir):
        if file.endswith(".xml"):
            file_path = os.path.join(results_dir, file)
            try:
                tree = ET.parse(file_path)
                root = tree.getroot()
                for testcase in root.findall("testcase"):
                    name = testcase.attrib.get("name")
                    status = "passed"
                    message = ""

                    skipped_node = testcase.find("skipped")
                    failure_node = testcase.find("failure")
                    error_node = testcase.find("error")

                    if skipped_node is not None:
                        status = "skipped"
                        summary["skipped"] += 1
                        message = skipped_node.attrib.get("message", "Test skipped")
                    elif failure_node is not None:
                        status = "failed"
                        message = failure_node.attrib.get("message", "")
                        if failure_node.text:
                            message += "\n" + failure_node.text
                        summary["failed"] += 1
                    elif error_node is not None:
                        status = "failed"
                        message = error_node.attrib.get("message", "")
                        if error_node.text:
                            message += "\n" + error_node.text
                        summary["failed"] += 1
                    else:
                        summary["passed"] += 1

                    summary["tests"] += 1

                    if status != "passed":
                        failed_tests.append({
                            "service": service_name,
                            "test_name": name,
                            "status": status,
                            "message": message
                        })
            except Exception as e:
                logger.error(f"Failed to parse {file_path}: {str(e)}")
                summary["tests"] += 1
                summary["failed"] += 1
                failed_tests.append({
                    "service": service_name,
                    "test_name": "parsing",
                    "status": "failed",
                    "message": traceback.format_exc()
                })

    return summary, failed_tests

def generate_schema_report(all_tests, total_summary, start_ts, stop_ts, no_tests, service_order_map):
    """
    Generates a JSON report with only failed tests, keeping no_tests list and summary.
    """
    failed_tests = []
    for t in all_tests:
        if t.get("status") == "failed":
            t_copy = t.copy()
            t_copy["order_tested"] = service_order_map.get(t["service"], -1)
            failed_tests.append(t_copy)

    tested_services = len(service_order_map)

    total_tests = total_summary.get("tests", 0)
    total_passed = total_summary.get("passed", 0)
    total_failed = total_summary.get("failed", 0)
    total_skipped = total_summary.get("skipped", 0)
    pass_rate = (float(total_passed) / total_tests) if total_tests > 0 else 0.0

    runid = f"{REPORT_FILE_PREFIX}-{datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M')}"

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "summary": {
                "services": tested_services,
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "pass_rate": pass_rate,
                "start_time": start_ts,
                "stop_time": stop_ts
            },
            "tests": failed_tests,
            "no_tests": no_tests
        },
        "runid": runid
    }

    filename = f"{runid}.json"
    with open(filename, "w") as f:
        json.dump(schema, f, indent=2)
    logger.info(f"\n📁 Wrote JSON schema file: {filename}")

    return filename, schema

def upload_to_s3(local_file, bucket, key):
    try:
        s3.upload_file(local_file, bucket, key)
        logger.info(f"✅ Uploaded {local_file} to S3: s3://{bucket}/{key}")
    except Exception as e:
        logger.error(f"❌ Failed to upload to S3: {str(e)}")

# -------------------
# Main execution
# -------------------
def main():
    start_epoch_ms = int(time.time() * 1000)
    temp_dir = tempfile.mkdtemp()
    all_tests = []
    total_summary = {"tests": 0, "passed": 0, "failed": 0, "pending": 0, "skipped": 0, "other": 0}
    service_details = []
    no_tests = []

    try:
        repo_path = clone_repo(temp_dir)
        services = find_services(repo_path)

        for order, service in enumerate(services, start=1):
            if service.lower() in SKIP_SERVICES:
                logger.info(f"⏭️ Skipping service (in skip list): {service}")
                service_details.append({
                    "service_name": service,
                    "order_tested": order,
                    "tests_run": 0,
                    "passed": 0,
                    "failed": 0,
                    "has_tests": False,
                    "skipped_reason": "explicitly skipped"
                })
                continue

            service_path = os.path.join(repo_path, "kotlin", "services", service)
            if not has_tests(service_path):
                logger.info(f"⚠️ No tests found for service: {service}")
                no_tests.append(service)
                service_details.append({
                    "service_name": service,
                    "order_tested": order,
                    "tests_run": 0,
                    "passed": 0,
                    "failed": 0,
                    "has_tests": False
                })
                continue

            try:
                run_gradle_tests(service, repo_path)
                service_summary, service_tests = parse_test_results(service_path, service)
            except Exception as e:
                logger.error(f"Exception for service {service}: {str(e)}")
                service_summary = {"tests": 1, "passed": 0, "failed": 1, "pending": 0, "skipped": 0, "other": 0}
                service_tests = [{
                    "service": service,
                    "test_name": "setup",
                    "status": "failed",
                    "message": traceback.format_exc()
                }]

            service_details.append({
                "service_name": service,
                "order_tested": order,
                "tests_run": service_summary["tests"],
                "passed": service_summary["passed"],
                "failed": service_summary["failed"],
                "has_tests": True
            })

            all_tests.extend(service_tests)
            for key in total_summary:
                total_summary[key] += service_summary.get(key, 0)

    except Exception as e:
        logger.error(f"Global exception: {str(e)}")
        all_tests.append({
            "service": "global",
            "test_name": "setup",
            "status": "failed",
            "message": traceback.format_exc()
        })
        total_summary["tests"] += 1
        total_summary["failed"] += 1

    finally:
        stop_epoch_ms = int(time.time() * 1000)
        service_order_map = {s['service_name']: s['order_tested'] for s in service_details if s.get("order_tested")}
        report_file, schema = generate_schema_report(
            all_tests, total_summary, start_epoch_ms, stop_epoch_ms, service_details, service_order_map
        )
        s3_key = os.path.join(S3_FOLDER, report_file) if S3_FOLDER else report_file
        upload_to_s3(report_file, S3_BUCKET, s3_key)
        shutil.rmtree(temp_dir)

        # ✅ Print full JSON to console
        print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()

