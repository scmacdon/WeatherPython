import os
import subprocess
import json
import time
import shutil
import boto3
import glob
import xml.etree.ElementTree as ET
import re
from datetime import datetime

REPO_URL = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
CLONE_DIR = "/app/aws-doc-sdk-examples"
ROOT_TEST_DIR = "dotnetv4"
S3_BUCKET_NAME = "weathertop2"

def run_command(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined
    except subprocess.CalledProcessError as e:
        combined = (e.stdout or "") + "\n" + (e.stderr or "")
        return e.returncode, combined

def parse_dotnet_test_results(output):
    """
    Extract passed/failed/skipped counts from dotnet textual output.
    """
    passed = failed = skipped = 0
    summary_pattern = re.compile(r"Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+)")
    for line in output.splitlines():
        m = summary_pattern.search(line)
        if m:
            failed = int(m.group(1))
            passed = int(m.group(2))
            skipped = int(m.group(3))
            break
    return passed, failed, skipped

def extract_failures_from_text(output, service_name, order_tested):
    """
    Extract failed test blocks from dotnet textual output.
    """
    lines = output.splitlines()
    failures = []

    header_regexes = [
        re.compile(r'^\s*Failed\s+(.+?)\s*\[', re.IGNORECASE),
        re.compile(r'^\s*(.+?)\s+\[(?:FAIL|FAILED)\b', re.IGNORECASE),
        re.compile(r'^\s*Xunit\.net.*\[FAIL\]', re.IGNORECASE)
    ]

    header_matches = []
    for i, line in enumerate(lines):
        for rx in header_regexes:
            m = rx.match(line)
            if m:
                test_name = m.group(1).strip() if m.groups() else line.strip()
                header_matches.append((i, test_name))
                break

    for idx, (start_idx, test_name) in enumerate(header_matches):
        end_idx = header_matches[idx + 1][0] - 1 if idx + 1 < len(header_matches) else len(lines) - 1
        block = "\n".join(lines[start_idx:end_idx + 1]).strip()
        failures.append({
            "service": service_name.lower(),
            "test_name": test_name,
            "status": "failed",
            "message": block,
            "order_tested": order_tested
        })

    return failures

def find_trx_file(search_dir, prefix=None):
    """
    Find the most recent .trx file under search_dir.
    """
    candidates = glob.glob(os.path.join(search_dir, "**", "*.trx"), recursive=True)
    if not candidates:
        return None
    if prefix:
        pref = [c for c in candidates if prefix in os.path.basename(c)]
        if pref:
            candidates = pref
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]

def parse_trx_for_failures(trx_path, service_name, order_tested):
    """
    Parse .trx XML for UnitTestResult elements with outcome="Failed".
    """
    failures = []
    try:
        tree = ET.parse(trx_path)
        root = tree.getroot()
    except Exception as e:
        print(f"❌ Failed to parse TRX {trx_path}: {e}")
        return failures

    for elem in root.iter():
        if elem.tag.endswith('UnitTestResult'):
            outcome = elem.attrib.get('outcome') or elem.attrib.get('Outcome')
            if outcome and outcome.lower() == 'failed':
                test_name = elem.attrib.get('testName') or elem.attrib.get('testname') or elem.attrib.get('test') or 'unknown'
                message_parts = []
                for sub in elem.iter():
                    tag = sub.tag.lower()
                    if tag.endswith('message') and (sub.text and sub.text.strip()):
                        message_parts.append(sub.text.strip())
                    if tag.endswith('stacktrace') and (sub.text and sub.text.strip()):
                        message_parts.append(sub.text.strip())
                message = "\n\n".join(message_parts).strip() if message_parts else ET.tostring(elem, encoding='unicode')
                failures.append({
                    "service": service_name.lower(),
                    "test_name": test_name,
                    "status": "failed",
                    "message": message,
                    "order_tested": order_tested
                })
    return failures

def extract_failures(output, service_name, order_tested, project_dir=None):
    """
    Try text parsing first; fallback to TRX parsing.
    """
    failures = extract_failures_from_text(output, service_name, order_tested)
    if failures:
        return failures

    if project_dir:
        trx = find_trx_file(project_dir)
        if trx:
            return parse_trx_for_failures(trx, service_name, order_tested)
    return []

def has_trait_annotation(service_path):
    """
    Returns True if any .cs file contains Trait("Category", "Integration"),
    ignoring Theory/Fact decorators.
    """
    trait_regex = re.compile(r'Trait\s*\(\s*"Category"\s*,\s*"Integration"\s*\)', re.IGNORECASE)
    for root, _, files in os.walk(service_path):
        for file in files:
            if file.endswith(".cs"):
                try:
                    with open(os.path.join(root, file), encoding="utf-8") as f:
                        for line in f:
                            if trait_regex.search(line):
                                return True
                except Exception:
                    pass
    return False

def run_dotnet_tests(service_path, service_name, order_tested, failed_tests):
    """
    Run tests for a service project and capture failures.
    """
    has_trait = has_trait_annotation(service_path)
    test_project = None
    project_dir = None

    for root, _, files in os.walk(service_path):
        for file in files:
            if file.endswith(".csproj") and "Test" in root:
                test_project = os.path.join(root, file)
                project_dir = os.path.dirname(test_project)
                break
        if test_project:
            break

    if not has_trait or not test_project:
        print(f"⚠️ Skipping {service_name}: No matching integration tests found.")
        return 0, 0, 0, False

    print(f"🔧 Testing: {service_name} (project: {test_project})")

    trx_filename = f"dotnet_results_{order_tested}.trx"
    log_filename = os.path.join(project_dir, f"dotnet_test_{order_tested}.log")

    cmd = [
        "dotnet", "test", test_project,
        "--filter", "Category=Integration",
        "--logger", f"trx;LogFileName={trx_filename}",
        "--verbosity", "minimal"
    ]

    rc, output = run_command(cmd, cwd=project_dir)

    try:
        with open(log_filename, "w", encoding="utf-8") as lf:
            lf.write(output)
    except Exception as e:
        print(f"⚠️ Could not write log file {log_filename}: {e}")

    passed, failed, skipped = parse_dotnet_test_results(output)
    print(f"📊 Result summary for {service_name}: Passed={passed} Failed={failed} Skipped={skipped} (rc={rc})")

    if failed > 0:
        extracted = extract_failures(output, service_name, order_tested, project_dir=project_dir)
        if not extracted:
            trx_path = find_trx_file(project_dir, prefix=f"dotnet_results_{order_tested}")
            if trx_path:
                print(f"ℹ️ Parsing TRX fallback: {trx_path}")
                extracted = parse_trx_for_failures(trx_path, service_name, order_tested)

        if extracted:
            failed_tests.extend(extracted)
        else:
            failed_tests.append({
                "service": service_name.lower(),
                "test_name": "unknown",
                "status": "failed",
                "message": "Failed tests detected but failure details could not be parsed. See test log.",
                "order_tested": order_tested
            })

    return passed, failed, skipped, True

def upload_to_s3(local_file, bucket_name, s3_key):
    s3 = boto3.client("s3")
    try:
        s3.upload_file(local_file, bucket_name, s3_key)
        print(f"✅ Uploaded {local_file} to S3 bucket: {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"❌ Failed to upload to S3: {e}")

def main():
    if os.path.exists(CLONE_DIR):
        print(f"🧹 Removing existing repo directory: {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)

    print(f"📥 Cloning repo: {REPO_URL}")
    rc, out = run_command(["git", "clone", REPO_URL, CLONE_DIR])
    if rc != 0:
        print("❌ Failed to clone repo.")
        print(out)
        return

    root_test_path = os.path.join(CLONE_DIR, ROOT_TEST_DIR)
    if not os.path.isdir(root_test_path):
        print(f"❌ Root test path not found: {root_test_path}")
        return

    service_dirs = sorted([d for d in os.listdir(root_test_path) if os.path.isdir(os.path.join(root_test_path, d))])

    total_passed = total_failed = total_skipped = 0
    failed_tests = []
    no_tests = []
    start_time = int(time.time() * 1000)

    for idx, service_name in enumerate(service_dirs, start=1):
        service_path = os.path.join(root_test_path, service_name)
        passed, failed, skipped, has_tests = run_dotnet_tests(service_path, service_name, idx, failed_tests)

        if not has_tests:
            no_tests.append(service_name.lower())

        total_passed += passed
        total_failed += failed
        total_skipped += skipped

    stop_time = int(time.time() * 1000)
    total_tests = total_passed + total_failed + total_skipped

    print("\n===== ✅ Final Test Summary =====")
    print(f"Services Checked: {len(service_dirs)}")
    print(f"Total Tests Passed: {total_passed}")
    print(f"Total Tests Failed: {total_failed}")
    print(f"Total Tests Skipped: {total_skipped}")
    print(f"Total Time (s): {(stop_time - start_time) // 1000}")

    schema = {
        "schema-version": "0.0.1",
        "results": {
            "tool": "dotnet",
            "summary": {
                "services": len(service_dirs),
                "tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "start_time": start_time,
                "stop_time": stop_time
            },
            "tests": failed_tests,
            "no_tests": no_tests
        }
    }

    now = datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
    filename = f"dotnetv4-{now}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    print(f"📁 Wrote schema to local file: {filename}")

    upload_to_s3(filename, S3_BUCKET_NAME, filename)

    print("\n===== 📊 Final JSON Schema =====")
    print(json.dumps(schema, indent=2))

if __name__ == "__main__":
    main()


