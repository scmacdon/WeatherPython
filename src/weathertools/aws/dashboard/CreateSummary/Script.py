import json
import boto3
from botocore.exceptions import ClientError

# -------------------------------
# USER VARIABLES
# -------------------------------
S3_BUCKET = "codeboard"
COVERAGE_PREFIX = "coverage/"
SUMMARY_KEY = "summary.json"
AWS_REGION = "us-east-1"
# -------------------------------

s3_client = boto3.client("s3", region_name=AWS_REGION)

# -------------------------------
# Helper Functions
# -------------------------------

def list_coverage_files(bucket, prefix):
    """List all JSON files under the coverage prefix in S3."""
    paginator = s3_client.get_paginator("list_objects_v2")
    files = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".coverage.json"):  # safer filter
                files.append(key)
    return files

def load_json_from_s3(bucket, key):
    """Load JSON file content from S3."""
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)

def compute_summary_from_s3(bucket, prefix):
    """Compute summary stats for all coverage JSONs."""
    summary = {"services": []}
    coverage_files = list_coverage_files(bucket, prefix)

    print(f"[INFO] Found {len(coverage_files)} coverage JSON files in s3://{bucket}/{prefix}")

    global_sdk_count = 0  # total SDK examples across all services

    for key in coverage_files:
        data = load_json_from_s3(bucket, key)

        service_code = data.get("serviceCode", "")
        service_name = f"Amazon {service_code.upper()}"
        operations = data.get("operations", [])

        # Per-service metrics
        method_count = len(operations)
        found_count = sum(1 for op in operations if op.get("found", False))
        sdk_example_count = sum(len(op.get("languages", [])) for op in operations)

        global_sdk_count += sdk_example_count  # accumulate global total

        coverage_percent = round((found_count / method_count) * 100, 1) if method_count > 0 else 0.0

        summary["services"].append({
            "serviceCode": service_code,
            "serviceName": service_name,
            "methodCount": method_count,
            "foundCount": found_count,
            "sdkExampleCount": sdk_example_count,
            "coveragePercent": coverage_percent
        })

        print(f"[INFO] {service_code}: {found_count}/{method_count} methods covered "
              f"({coverage_percent}%), {sdk_example_count} SDK examples")

    # Global total across all services
    summary["globalSdkExampleCount"] = global_sdk_count
    print(f"[INFO] ✅ Total SDK code examples across all services: {global_sdk_count}")

    return summary

def delete_summary_from_s3(bucket, key):
    """Delete the existing summary JSON from S3 if it exists."""
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
        print(f"[INFO] Existing summary deleted from s3://{bucket}/{key}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            print(f"[INFO] No existing summary to delete at s3://{bucket}/{key}")
        else:
            raise

def save_summary_to_s3(bucket, key, summary_data):
    """Save JSON summary directly to S3."""
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(summary_data, indent=2),
        ContentType="application/json"
    )
    print(f"[INFO] Summary uploaded to s3://{bucket}/{key}")

# -------------------------------
# Main
# -------------------------------

def main():
    # Delete old summary first
    delete_summary_from_s3(S3_BUCKET, SUMMARY_KEY)

    # Compute and save new summary
    summary = compute_summary_from_s3(S3_BUCKET, COVERAGE_PREFIX)
    save_summary_to_s3(S3_BUCKET, SUMMARY_KEY, summary)

if __name__ == "__main__":
    main()
