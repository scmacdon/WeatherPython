#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import shutil
import zipfile
import glob
import boto3
import requests
import yaml

# ========================================================================
# USER VARIABLES
# ========================================================================
S3_BUCKET        = "codeboard"                # S3 bucket
INPUT_PREFIX     = "data/"                    # S3 prefix where input JSONs are stored
OUTPUT_PREFIX    = "coverage/"                # S3 prefix to write coverage JSONs
REPO_URL         = "https://github.com/awsdocs/aws-doc-sdk-examples.git"
REPO_DIR         = "aws-doc-sdk-examples"
CLEAN_REPO_FIRST = False                      # Set True to delete repo before cloning

# List of services to process; empty = process all
SERVICE_TO_PROCESS = ["sns"]                       # e.g., ["s3", "ec2"]

METADATA_REL_PATH  = os.path.join(".doc_gen", "metadata")
LOCAL_OUTPUT_DIR   = "DataOutput"            # local output directory

# ------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------

def save_local_json(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[INFO] Saved locally → {filepath}")

def run_git_clone(repo_url, dest_dir):
    try:
        print(f"[INFO] Cloning {repo_url} → {dest_dir}")
        subprocess.check_call(["git", "clone", "--depth", "1", repo_url, dest_dir])
        return True
    except Exception as e:
        print("[WARN] git clone failed:", e)
        return False

def download_and_extract_zip(repo_url, dest_dir):
    base = repo_url.rstrip(".git")
    zip_url = base + "/archive/refs/heads/main.zip"
    print(f"[INFO] Downloading ZIP: {zip_url}")
    r = requests.get(zip_url, stream=True)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as td:
        zpath = os.path.join(td, "repo.zip")
        with open(zpath, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        print("[INFO] Extracting ZIP...")
        with zipfile.ZipFile(zpath, "r") as zf:
            zf.extractall(td)
        folders = [os.path.join(td, name) for name in os.listdir(td) if os.path.isdir(os.path.join(td, name))]
        if not folders:
            raise RuntimeError("Could not extract downloaded repo ZIP")
        print(f"[INFO] Copying extracted repo → {dest_dir}")
        shutil.copytree(folders[0], dest_dir)

def ensure_repo_present(repo_url, repo_dir):
    if os.path.isdir(repo_dir):
        print(f"[INFO] Using existing repo: {repo_dir}")
        return repo_dir
    if run_git_clone(repo_url, repo_dir):
        return repo_dir
    print("[INFO] git not available — downloading ZIP...")
    download_and_extract_zip(repo_url, repo_dir)
    return repo_dir

def list_s3_json_files(bucket, prefix):
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) if page.get("Contents") else []:
            key = obj.get("Key")
            if key and key.lower().endswith(".json"):
                keys.append(key)
    return keys

def load_s3_json(bucket, key):
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)

def upload_s3_json(bucket, key, data):
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, indent=2).encode("utf-8"))
    full_s3_path = f"s3://{bucket}/{key}"
    print(f"[INFO] Uploaded → {full_s3_path}")
    return full_s3_path

def delete_s3_prefix(bucket, prefix):
    s3 = boto3.client("s3")
    print(f"[INFO] Clearing S3 prefix: s3://{bucket}/{prefix}")
    paginator = s3.get_paginator("list_objects_v2")
    delete_us = {'Objects': []}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) if page.get("Contents") else []:
            delete_us['Objects'].append({'Key': obj['Key']})
        if delete_us['Objects']:
            s3.delete_objects(Bucket=bucket, Delete=delete_us)
            delete_us = {'Objects': []}

def _normalize_service_token(token):
    if token is None:
        return ""
    return token.lower().replace("-", "").replace("_", "")

def find_metadata_files(repo_dir, service):
    metadata_dir = os.path.join(repo_dir, METADATA_REL_PATH)
    if not os.path.isdir(metadata_dir):
        raise FileNotFoundError(f"Metadata directory missing: {metadata_dir}")

    candidates = [
        f"{service}_metadata.yaml",
        f"{service.replace('-', '_')}_metadata.yaml",
        f"{service.replace('-', '').replace('_', '')}_metadata.yaml"
    ]
    candidates = list(dict.fromkeys([c.lower() for c in candidates]))

    files = []
    for cand in candidates:
        p = os.path.join(metadata_dir, cand)
        if os.path.isfile(p):
            files.append(p)

    for path in glob.glob(os.path.join(metadata_dir, "*.yaml")):
        if path not in files:
            files.append(path)

    return files

def load_yaml_file(path):
    with open(path, "r", encoding="utf-8") as f:
        docs = yaml.safe_load_all(f)
        combined = {}
        for d in docs:
            if isinstance(d, dict):
                combined.update(d)
        return combined

def extract_languages_from_entry(entry):
    langs_set = set()
    languages = entry.get("languages") or {}
    if isinstance(languages, dict):
        for lang in languages.keys():
            langs_set.add(lang)
    return langs_set

def aggregate_operations_from_yaml(yaml_map, target_service):
    agg = {}
    norm_target = _normalize_service_token(target_service)

    for key, entry in yaml_map.items():
        if not isinstance(entry, dict):
            continue

        if "_" in key:
            svc_part, op_name = key.split("_", 1)
            if _normalize_service_token(svc_part) == norm_target:
                langs_set = extract_languages_from_entry(entry)
                if op_name in agg:
                    agg[op_name].update(langs_set)
                else:
                    agg[op_name] = set(langs_set)

        services_dict = entry.get("services") or {}
        svc_ops = services_dict.get(target_service) or services_dict.get(target_service.lower())
        if svc_ops:
            if isinstance(svc_ops, (list, set)):
                ops_list = list(svc_ops)
            elif isinstance(svc_ops, dict):
                ops_list = list(svc_ops.keys())
            else:
                ops_list = []

            langs_set = extract_languages_from_entry(entry)

            for op in ops_list:
                if op:
                    if op in agg:
                        agg[op].update(langs_set)
                    else:
                        agg[op] = set(langs_set)

    return agg

def capitalize_first_letter(name):
    if not name:
        return name
    return name[0].upper() + name[1:]

def create_report_for_methods(methods, agg_map):
    report = []
    for m in methods:
        if not m:
            continue
        found = False
        langs = set()
        for key in agg_map:
            if key.lower() == m.lower() or key == m:
                found = True
                langs.update(agg_map[key])
                break
        report.append({"name": capitalize_first_letter(m), "found": found, "languages": sorted(list(langs))})
    return report

# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------

def main():
    if CLEAN_REPO_FIRST and os.path.isdir(REPO_DIR):
        print(f"[INFO] Cleaning repo {REPO_DIR}...")
        shutil.rmtree(REPO_DIR)

    ensure_repo_present(REPO_URL, REPO_DIR)

    service_keys = list_s3_json_files(S3_BUCKET, INPUT_PREFIX)
    print(f"[INFO] Found {len(service_keys)} JSON files in s3://{S3_BUCKET}/{INPUT_PREFIX}")

    # Normalize target services; None = process all
    normalized_target_services = [_normalize_service_token(s)
                                  for s in SERVICE_TO_PROCESS] if SERVICE_TO_PROCESS else None

    for key in service_keys:
        print(f"\n[INFO] Loading service JSON: s3://{S3_BUCKET}/{key}")
        try:
            data = load_s3_json(S3_BUCKET, key)
        except Exception as e:
            print(f"[ERROR] Failed to load s3://{S3_BUCKET}/{key}: {e}")
            continue

        # Determine and normalize service code
        service_code = (
            data.get("serviceCode") or
            data.get("service") or
            os.path.basename(key).split('.')[0]
        )
        service_code = service_code.lower()
        if service_code.startswith("aws"):
            service_code = service_code[3:]
        elif service_code.startswith("amazon"):
            service_code = service_code[6:]

        service_code_normalized = _normalize_service_token(service_code)

        # Skip if service not in target list
        if normalized_target_services and service_code_normalized not in normalized_target_services:
            print(f"[INFO] Skipping service: {service_code}")
            continue

        # Extract operations
        operations = []
        for op in data.get("operations") or []:
            if isinstance(op, dict):
                operations.append(op.get("name"))
            elif isinstance(op, str):
                operations.append(op)
            else:
                print(f"[WARN] Unexpected operation format: {op}")

        print(f"[INFO] Processing service: {service_code} ({len(operations)} operations)")

        # Find YAML metadata
        try:
            metadata_files = find_metadata_files(REPO_DIR, service_code_normalized)
        except Exception as e:
            print(f"[ERROR] Metadata dir problem: {e}")
            metadata_files = []

        print(f"[INFO] Inspecting {len(metadata_files)} metadata files for {service_code}")

        # Aggregate operation-language mappings
        agg_map = {}
        for path in metadata_files:
            try:
                yaml_map = load_yaml_file(path)
            except Exception as e:
                print(f"[WARN] Failed to load YAML {path}: {e}")
                continue

            try:
                part = aggregate_operations_from_yaml(yaml_map, service_code_normalized)
                for op, langs in part.items():
                    if op in agg_map:
                        agg_map[op].update(langs)
                    else:
                        agg_map[op] = set(langs)
            except Exception as e:
                print(f"[WARN] Failed to aggregate from {path}: {e}")

        report = create_report_for_methods(operations, agg_map)

        # Build output JSON and the S3 key for it
        out_json = {"serviceCode": service_code, "operations": report}
        out_key = os.path.join(OUTPUT_PREFIX, f"{service_code}.coverage.json").replace("\\", "/")

        # Upload directly to S3
        s3_path = None
        try:
            s3_path = upload_s3_json(S3_BUCKET, out_key, out_json)
            print(f"[INFO] Coverage JSON uploaded for {service_code}: {s3_path}")
        except Exception as e:
            print(f"[ERROR] Failed to upload coverage JSON to S3: {e}")

    print("\n[INFO] Coverage processed successfully!")


if __name__ == "__main__":
    main()
