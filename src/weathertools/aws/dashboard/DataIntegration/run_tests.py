import subprocess
import os
import sys
import re
import time
import uuid
import json
import boto3
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime

# Configuration
GIT_REPO = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "javav2/example_code"

# ✅ New exclusion list for entire services
EXCLUDED_SERVICES = {"cloudfront", "ecr", "lookoutvision", "transcribe", "timestream", "support", "emr"}

# ✅ Exclude test package path under S3
EXCLUDED_TEST_PATHS = ["com/example/s3"]

S3_BUCKET_NAME = "weathertop2"


def run_command(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    return result.returncode, result.stdout


def clone_repo():
    if os.path.exists(CLONE_DIR):
        print("Repo already cloned.")
        return
    result = run_command(["git", "clone", GIT_REPO, CLONE_DIR])
    if result[0] != 0:
        sys.exit("❌ Failed to clone repo.")


def parse_test_results(output):
    total_passed = total_failed = total_skipped = 0
    matches = re.findall(
        r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", output
    )
    for run, failures, errors, skipped in matches:
        run, failures, errors, skipped = map(int, (run, failures, errors, skipped))
        passed = run - (failures + errors + skipped)
        total_passed += passed
        total_failed += failures + errors
        total_skipped += skipped
    return total_passed, total_failed, total_skipped


def has_integration_tests(path):
    """
    Detects if there are @Test annotations in .java files,
    but skips any files in EXCLUDED_TEST_PATHS.
    """
    for root, _, files in os.walk(path):
        normalized_root = root.replace("\\", "/")
        if any(excluded in normalized_root for excluded in EXCLUDED_TEST_PATHS):
            continue  # 🚫 Skip excluded paths entirely

        for file in files:
            if file.endswith(".java"):
                with open(os.path.join(root, file), encoding="utf-8") as f:
                    content = f.read()
                    if "@Test" in content:
                        return True
    return False


def extract_failures(output, service_name, test_index_start):
    failures = []
    test_index = test_index_start
    failure_blocks = re.findall(
        r"(Tests run:.*?)(?=^\[(?:INFO|ERROR|WARNING)\]|\Z)",
        output, re.DOTALL | re.MULTILINE
    )
    for block in failure_blocks:
        lines = block.splitlines()
        log_lines = [line for line in lines if "Exception" in line or "FAILURE" in line or "ERROR" in line]
        if not log_lines:
            continue
        error_log = "\n".join(log_lines).strip()
        failures.append({
            "service": service_name,
            "test_name": f"test_{service_name}_{test_index}",
            "status": "failed",
            "message": error_log
        })
        test_index += 1
    return failures, test_index


def parse_surefire_reports(service_path, service_name, test_index_start):
    report_dir = os.path.join(service_path, "target", "surefire-reports")
    if not os.path.isdir(report_dir):
        return None

    total_tests = 0
    total_failed = 0
    total_skipped = 0
    failures_list = []
    test_index = test_index_start

    xml_found = False
    for fname in os.listdir(report_dir):
        if not fname.endswith(".xml"):
            continue
        xml_found = True
        fpath = os.path.join(report_dir, fname)
        try:
            tree = ET.parse(fpath)
            root = tree.getroot()
        except Exception:
            continue

        suites = []
        if root.tag == "testsuites":
            suites = root.findall("testsuite")
        elif root.tag == "testsuite":
            suites = [root]
        else:
            suites = root.findall(".//testsuite")

        for suite in suites:
            tests = int(suite.attrib.get("tests", 0))
            failures = int(suite.attrib.get("failures", 0))
            errors = int(suite.attrib.get("errors", 0))
            skipped = int(suite.attrib.get("skipped", 0))

            total_tests += tests
            total_failed += (failures + errors)
            total_skipped += skipped

            for testcase in suite.findall("testcase"):
                tc_name = testcase.attrib.get("name", "unknown")
                tc_class = testcase.attrib.get("classname", suite.attrib.get("name", ""))
                failure_nodes = testcase.findall("failure") + testcase.findall("error")
                skipped_node = testcase.find("skipped")
                if failure_nodes:
                    parts = []
                    for node in failure_nodes:
                        msg = node.attrib.get("message", "")
                        txt = node.text or ""
                        combined = (msg + "\n" + txt).strip()
                        parts.append(combined)
                    message = "\n\n".join([p for p in parts if p]).strip() or "FAILED"
                    failures_list.append({
                        "service": service_name,
                        "test_name": f"test_{service_name}_{test_index}",
                        "status": "failed",
                        "message": f"{tc_class}.{tc_name}\n{message}"
                    })
                    test_index += 1
                elif skipped_node is not None:
                    pass

    if not xml_found:
        return None

    total_passed = total_tests - total_failed - total_skipped
    return total_passed, total_failed, total_skipped, failures_list, test_index


def run_maven_tests(service_path, service_name, failed_tests, test_index):
    print(f"🚀 Running tests for {service_name}")

    report_dir = os.path.join(service_path, "target", "surefire-reports")
    if os.path.exists(report_dir):
        try:
            shutil.rmtree(report_dir)
        except Exception:
            pass

    # ✅ Exclude com.example.s3 test folder using -Dtest
    returncode, output = run_command([
        "mvn", "test",
        "-DtrimStackTrace=false",
        "-Dtest=!com.example.s3.*"
    ], cwd=service_path)

    xml_parsed = parse_surefire_reports(service_path, service_name, test_index)
    if xml_parsed is not None:
        passed, failed, skipped, failures_from_xml, test_index = xml_parsed
        if failures_from_xml:
            failed_tests.extend(failures_from_xml)
        print(f"  -> (xml) passed={passed} failed={failed} skipped={skipped}")
        return passed, failed, skipped, test_index

    passed, failed, skipped = parse_test_results(output)
    if failed > 0:
        failures, test_index = extract_failures(output, service_name, test_index)
        failed_tests.extend(failures)

    print(f"  -> (console) passed={passed} failed={failed} skipped={skipped}")
    return passed, failed, skipped, test_index


def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")


def main():
    clone_repo()

    total_passed = total_failed = total_skipped = 0
    tested_services = 0
    failed_tests = []
    test_index = 1
    start_time = int(time.time() * 1000)

    root_test_path = os.path.join(CLONE_DIR, ROOT_TEST_DIR)
    service_dirs = sorted([
        d for d in os.listdir(root_test_path)
        if os.path.isdir(os.path.join(root_test_path, d)) and d not in EXCLUDED_SERVICES
    ])

    for service_name in service_dirs:
        service_path = os.path.join(root_test_path, service_name)
        if os.path.exists(os.path.join(service_path, "pom.xml")) and has_integration_tests(service_path):
            passed, failed, skipped, test_index = run_maven_tests(
                service_path, service_name, failed_tests, test_index
            )
            total_passed += passed
            total_failed += failed
            total_skipped += skipped
            tested_services += 1
        else:
            print(f"⚠️ Skipping {service_name}: No integration tests found or no pom.xml.")

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed + total_skipped

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "maven",
            "summary": {
                "services": tested_services,
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "pass_rate": (float(total_passed) / total_tests) if total_tests > 0 else 0.0,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "tests": failed_tests
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"java-{now}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"📁 Wrote schema to local file: {filename}")

    upload_to_s3(filename, S3_BUCKET_NAME, filename)


if __name__ == "__main__":
    main()
