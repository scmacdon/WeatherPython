import boto3
import botocore
import subprocess
import sys
import json
import time

AWS_ACCOUNT_ID = '814548047983'
AWS_REGION = 'us-east-1'

ECR_REPO_NAME = 'weathertop-cpp-ecr-repo'
IMAGE_TAG = 'latest'
DOCKERFILE_DIR = 'C:/Users/scmacdon/Docker/CPP'  # Path to your Dockerfile/app

ECS_CLUSTER_NAME = 'MyCPPWeathertopCluster'
ECS_TASK_DEF_NAME = 'WeathertopCPP'
CONTAINER_NAME = 'app'
EXECUTION_ROLE_NAME = 'ecsTaskExecutionRole'
TASK_ROLE_NAME = 'ecsTaskRole'

SUBNETS = ['subnet-03c28397a3a7cd314', 'subnet-06dde61595900f899']
SECURITY_GROUPS = ['sg-0e357c99b6b13bf62']

LOG_GROUP = 'WeathertopCPPContainerLogs'
LOG_STREAM_PREFIX = 'weathertop-cpp-stream'

EVENTBRIDGE_RULE_NAME = 'ecs-cpp-schedule'
EVENTBRIDGE_SCHEDULE = 'cron(59 23 ? * SUN *)'

ecs = boto3.client('ecs', region_name=AWS_REGION)
logs = boto3.client('logs', region_name=AWS_REGION)
iam = boto3.client('iam')
ec2 = boto3.client('ec2', region_name=AWS_REGION)
ecr = boto3.client('ecr', region_name=AWS_REGION)
events = boto3.client('events', region_name=AWS_REGION)

def recreate_ecr_repo(repo_name):
    try:
        print(f"[ECR] Attempting to delete repo '{repo_name}' if it exists...")
        ecr.delete_repository(repositoryName=repo_name, force=True)
        print(f"[ECR] Deleted existing repo '{repo_name}'.")
    except ecr.exceptions.RepositoryNotFoundException:
        print(f"[ECR] Repo '{repo_name}' not found, creating fresh one.")

    # Create fresh repo
    response = ecr.create_repository(repositoryName=repo_name)
    repo_uri = response["repository"]["repositoryUri"]
    print(f"[ECR] Created new repo '{repo_name}' with URI {repo_uri}")
    return repo_uri

def ensure_iam_role(role_name, assume_role_service, s3_bucket):
    try:
        iam.get_role(RoleName=role_name)
        print(f"[IAM] Role '{role_name}' already exists.")
    except iam.exceptions.NoSuchEntityException:
        print(f"[IAM] Creating role '{role_name}'...")
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": assume_role_service},
                "Action": "sts:AssumeRole"
            }]
        }
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=f"Role for {role_name}"
        )
        print(f"[IAM] Role '{role_name}' created.")

        if role_name == EXECUTION_ROLE_NAME:
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'
            )
            print(f"[IAM] Attached ECS Task Execution managed policy to '{role_name}'.")
        elif role_name == TASK_ROLE_NAME:
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn='arn:aws:iam::aws:policy/AdministratorAccess'
            )
            print(f"[IAM] Attached AdministratorAccess policy to '{role_name}'.")

    # Attach inline policy to allow S3 PutObject
    s3_access_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": f"arn:aws:s3:::{s3_bucket}/*"
        }]
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="AllowS3PutObject",
        PolicyDocument=json.dumps(s3_access_policy)
    )
    print(f"[IAM] Attached inline S3 PutObject policy to '{role_name}'.")


def ensure_outbound_rule():
    sg_id = SECURITY_GROUPS[0]
    try:
        sg = ec2.describe_security_groups(GroupIds=[sg_id])['SecurityGroups'][0]
        egress_rules = sg.get('IpPermissionsEgress', [])
        has_all_traffic = any(
            rule['IpProtocol'] == '-1' and
            any(ipr.get('CidrIp') == '0.0.0.0/0' for ipr in rule.get('IpRanges', []))
            for rule in egress_rules
        )

        if not has_all_traffic:
            print(f"[EC2] Adding outbound rule to security group {sg_id}...")
            ec2.authorize_security_group_egress(
                GroupId=sg_id,
                IpPermissions=[{
                    'IpProtocol': '-1',
                    'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                }]
            )
            print(f"[EC2] Outbound rule added.")
        else:
            print(f"[EC2] Outbound rule already exists on security group {sg_id}.")

    except botocore.exceptions.ClientError as e:
        if "InvalidPermission.Duplicate" in str(e):
            print(f"[EC2] Outbound rule already exists (duplicate).")
        else:
            raise


def get_or_create_ecr_repo(repo_name):
    try:
        response = ecr.describe_repositories(repositoryNames=[repo_name])
        print(f"[ECR] Repository '{repo_name}' already exists.")
        return response["repositories"][0]["repositoryUri"]
    except ecr.exceptions.RepositoryNotFoundException:
        response = ecr.create_repository(repositoryName=repo_name)
        print(f"[ECR] Created repository '{repo_name}'.")
        return response["repository"]["repositoryUri"]


def validate_ecr_image_exists(repo_name, image_tag):
    try:
        ecr.describe_images(repositoryName=repo_name, imageIds=[{'imageTag': image_tag}])
        print(f"[ECR] Image '{repo_name}:{image_tag}' found.")
        return True
    except ecr.exceptions.ImageNotFoundException:
        print(f"[ECR] Image '{repo_name}:{image_tag}' not found. Push image first.")
        return False


def create_log_group():
    existing_groups = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP).get('logGroups', [])
    if not any(g['logGroupName'] == LOG_GROUP for g in existing_groups):
        print(f"[CloudWatch Logs] Creating log group '{LOG_GROUP}'...")
        logs.create_log_group(logGroupName=LOG_GROUP)
        print(f"[CloudWatch Logs] Log group '{LOG_GROUP}' created.")
    else:
        print(f"[CloudWatch Logs] Log group '{LOG_GROUP}' already exists.")


def create_ecs_cluster():
    try:
        response = ecs.describe_clusters(clusters=[ECS_CLUSTER_NAME])
        if response['clusters'][0]['status'] == 'ACTIVE':
            print(f"[ECS] Cluster '{ECS_CLUSTER_NAME}' already exists and active.")
            return
    except Exception as e:
        print(f"[ECS] Exception checking cluster: {e}")

    print(f"[ECS] Creating cluster '{ECS_CLUSTER_NAME}'...")
    ecs.create_cluster(clusterName=ECS_CLUSTER_NAME)
    print(f"[ECS] Cluster '{ECS_CLUSTER_NAME}' created.")


def register_task_definition(image_uri):
    """
    Registers a Fargate ECS task definition for running the Python test script.
    Ensures WORKDIR /app is used and Python runner is executed.
    """
    print(f"[ECS] Registering task definition '{ECS_TASK_DEF_NAME}' with image '{image_uri}'...")

    CPU_UNITS = '1024'       # 1 vCPU
    MEMORY_MB = '4096'       # 4 GB RAM

    response = ecs.register_task_definition(
        family=ECS_TASK_DEF_NAME,
        networkMode='awsvpc',
        requiresCompatibilities=['FARGATE'],
        cpu=CPU_UNITS,
        memory=MEMORY_MB,
        executionRoleArn=f'arn:aws:iam::{AWS_ACCOUNT_ID}:role/{EXECUTION_ROLE_NAME}',
        taskRoleArn=f'arn:aws:iam::{AWS_ACCOUNT_ID}:role/{TASK_ROLE_NAME}',
        containerDefinitions=[{
            'name': CONTAINER_NAME,
            'image': image_uri,
            'essential': True,
            'cpu': int(CPU_UNITS),
            'memory': int(MEMORY_MB),
            'workingDirectory': '/app',   # ensure Python runs in /tmp
            'logConfiguration': {
                'logDriver': 'awslogs',
                'options': {
                    'awslogs-group': LOG_GROUP,
                    'awslogs-region': AWS_REGION,
                    'awslogs-stream-prefix': LOG_STREAM_PREFIX
                }
            },
            'command': ["python3", "run_tests.py"]
        }]
    )

    task_def_arn = response['taskDefinition']['taskDefinitionArn']
    print(f"[ECS] Task definition ARN: {task_def_arn}")
    return task_def_arn




def deregister_old_task_definitions():
    print(f"[ECS] Deregistering old task definitions for family '{ECS_TASK_DEF_NAME}' except latest...")
    paginator = ecs.get_paginator('list_task_definitions')
    all_defs = []
    for page in paginator.paginate(familyPrefix=ECS_TASK_DEF_NAME, status='ACTIVE', sort='DESC'):
        all_defs.extend(page['taskDefinitionArns'])

    if not all_defs:
        print("[ECS] No active task definitions found.")
        return None

    latest = all_defs[0]
    print(f"[ECS] Latest task definition ARN is: {latest}")
    for task_def_arn in all_defs[1:]:
        try:
            ecs.deregister_task_definition(taskDefinition=task_def_arn)
            print(f"[ECS] Deregistered old task definition: {task_def_arn}")
        except Exception as e:
            print(f"[ECS] Failed to deregister {task_def_arn}: {e}")

    return latest


def stop_old_running_tasks(latest_task_def_arn):
    print("[ECS] Stopping running tasks with old task definitions...")
    paginator = ecs.get_paginator('list_tasks')
    running_tasks = []
    for page in paginator.paginate(cluster=ECS_CLUSTER_NAME, desiredStatus='RUNNING'):
        running_tasks.extend(page['taskArns'])

    if not running_tasks:
        print("[ECS] No running tasks found.")
        return

    # Describe tasks in batches (max 100 per call)
    for i in range(0, len(running_tasks), 100):
        batch = running_tasks[i:i + 100]
        desc = ecs.describe_tasks(cluster=ECS_CLUSTER_NAME, tasks=batch)
        for task in desc['tasks']:
            task_def_arn = task['taskDefinitionArn']
            task_arn = task['taskArn']
            if task_def_arn != latest_task_def_arn:
                print(f"[ECS] Stopping task {task_arn} with old task definition {task_def_arn}...")
                ecs.stop_task(cluster=ECS_CLUSTER_NAME, task=task_arn, reason='Cleanup old task definition')


def ensure_eventbridge_permission(event_rule_name, target_arn):
    event_role_name = "EventBridgeInvokeECSRole"
    try:
        iam.get_role(RoleName=event_role_name)
        print(f"[IAM] EventBridge role '{event_role_name}' already exists.")
    except iam.exceptions.NoSuchEntityException:
        print(f"[IAM] Creating IAM role '{event_role_name}' for EventBridge...")
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }
        iam.create_role(
            RoleName=event_role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description="Role for EventBridge to invoke ECS tasks"
        )
        iam.attach_role_policy(
            RoleName=event_role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceEventsRole'
        )
        print(f"[IAM] Created and attached policy to '{event_role_name}'.")

    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "ecs:RunTask",
            "Resource": [
                f"arn:aws:ecs:{AWS_REGION}:{AWS_ACCOUNT_ID}:cluster/{ECS_CLUSTER_NAME}",
                target_arn
            ]
        }]
    }

    iam.put_role_policy(
        RoleName=event_role_name,
        PolicyName="AllowRunTaskOnCluster",
        PolicyDocument=json.dumps(inline_policy)
    )
    print(f"[IAM] Inline policy 'AllowRunTaskOnCluster' attached to '{event_role_name}'.")
    return event_role_name


def ensure_eventbridge_rule(event_rule_name, schedule_expression=EVENTBRIDGE_SCHEDULE):
    try:
        events.describe_rule(Name=event_rule_name)
        print(f"[EventBridge] Rule '{event_rule_name}' already exists.")
    except events.exceptions.ResourceNotFoundException:
        print(f"[EventBridge] Creating rule '{event_rule_name}' with schedule '{schedule_expression}'...")
        events.put_rule(
            Name=event_rule_name,
            ScheduleExpression=schedule_expression,
            State='ENABLED',
            Description=f"Scheduled rule to run ECS task for {event_rule_name}"
        )
        print(f"[EventBridge] Rule '{event_rule_name}' created.")


def update_eventbridge_rule(event_rule_name, task_def_arn):
    if not task_def_arn:
        print("[EventBridge] ERROR: No valid task definition ARN provided, cannot update rule.")
        sys.exit(1)

    print(f"[EventBridge] Updating rule '{event_rule_name}' with task definition '{task_def_arn}'...")
    targets = events.list_targets_by_rule(Rule=event_rule_name).get('Targets', [])
    print(f"[EventBridge] Current targets: {json.dumps(targets, indent=2)}")

    target_id = '1'
    role_name = ensure_eventbridge_permission(event_rule_name, task_def_arn)

    target = {
        'Id': target_id,
        'Arn': f'arn:aws:ecs:{AWS_REGION}:{AWS_ACCOUNT_ID}:cluster/{ECS_CLUSTER_NAME}',
        'RoleArn': f'arn:aws:iam::{AWS_ACCOUNT_ID}:role/{role_name}',
        'EcsParameters': {
            'TaskDefinitionArn': task_def_arn,
            'TaskCount': 1,
            'LaunchType': 'FARGATE',
            'NetworkConfiguration': {
                'awsvpcConfiguration': {
                    'Subnets': SUBNETS,
                    'SecurityGroups': SECURITY_GROUPS,
                    'AssignPublicIp': 'ENABLED'
                }
            }
        }
    }

    if targets:
        print("[EventBridge] Updating existing target...")
    else:
        print("[EventBridge] Adding new target...")

    response = events.put_targets(
        Rule=event_rule_name,
        Targets=[target]
    )
    print(f"[EventBridge] put_targets response: {json.dumps(response, indent=2)}")

    # Extra verification after put_targets
    time.sleep(2)  # slight delay before re-fetching
    updated_targets = events.list_targets_by_rule(Rule=event_rule_name).get('Targets', [])
    print(f"[EventBridge] Updated targets after put_targets: {json.dumps(updated_targets, indent=2)}")

    print("[EventBridge] Rule update complete.")


def dump_eventbridge_targets(rule_name):
    print(f"[Debug] Dumping EventBridge targets for rule '{rule_name}':")
    response = events.list_targets_by_rule(Rule=rule_name)
    targets = response.get('Targets', [])
    for t in targets:
        print(json.dumps(t, indent=2))


def main():
    print("=== Starting Deployment ===")

    # 1. Ensure IAM roles with correct policies
    ensure_iam_role(EXECUTION_ROLE_NAME, 'ecs-tasks.amazonaws.com', s3_bucket='weathertop2')
    ensure_iam_role(TASK_ROLE_NAME, 'ecs-tasks.amazonaws.com', s3_bucket='weathertop2')

    # 2. Add outbound rule to security group
    ensure_outbound_rule()

    # 3. Recreate ECR repo (delete if exists)
    ecr_uri = recreate_ecr_repo(ECR_REPO_NAME)

    # 4. Login to ECR
    print("[ECR] Logging into ECR...")
    try:
        pw = subprocess.check_output(
            ["aws", "ecr", "get-login-password", "--region", AWS_REGION],
            stderr=subprocess.STDOUT
        )
        subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", ecr_uri],
            input=pw,
            check=True
        )
        print("[ECR] Docker login successful.")
    except subprocess.CalledProcessError as e:
        print(f"[ECR] Docker login failed: {e.output.decode() if e.output else e}")
        sys.exit(1)

    # 5. Always build & push Docker image
    print("[Docker] Building Docker image...")
    subprocess.run(
        ["docker", "build", "-t", f"{ECR_REPO_NAME}:{IMAGE_TAG}", DOCKERFILE_DIR],
        check=True
    )

    print("[Docker] Tagging Docker image...")
    subprocess.run(
        ["docker", "tag", f"{ECR_REPO_NAME}:{IMAGE_TAG}", f"{ecr_uri}:{IMAGE_TAG}"],
        check=True
    )

    print("[Docker] Pushing Docker image to ECR...")
    subprocess.run(
        ["docker", "push", f"{ecr_uri}:{IMAGE_TAG}"],
        check=True
    )

    print("[Docker] Docker image pushed successfully.")

    # 6. Create CloudWatch Logs group if missing
    create_log_group()

    # 7. Create ECS cluster if missing
    create_ecs_cluster()

    # 8. Register ECS task definition
    full_image_uri = f"{ecr_uri}:{IMAGE_TAG}"
    task_def_arn = register_task_definition(full_image_uri)

    # 9. Deregister old task definitions except latest
    latest_task_def_arn = deregister_old_task_definitions()
    if not latest_task_def_arn:
        print("[ECS] ERROR: No latest task definition ARN found after deregistration!")
        sys.exit(1)

    # 10. Stop running tasks using old task definitions
    stop_old_running_tasks(latest_task_def_arn)

    # 11a. Ensure EventBridge rule exists
    ensure_eventbridge_rule(EVENTBRIDGE_RULE_NAME, EVENTBRIDGE_SCHEDULE)

    # 11b. Update EventBridge rule to latest task definition
    update_eventbridge_rule(EVENTBRIDGE_RULE_NAME, latest_task_def_arn)

    # 12. Dump EventBridge targets for diagnostics
    dump_eventbridge_targets(EVENTBRIDGE_RULE_NAME)

    print("✅ Deployment completed successfully.")


if __name__ == '__main__':
    main()
