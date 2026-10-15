
import boto3
import os
import json
import requests
import subprocess
from urllib.parse import urlparse

def load_config():
    """Read and validate environment variables."""

    workspace = os.getenv("BITBUCKET_WORKSPACE").strip()
    repo_slug = os.getenv("BITBUCKET_REPO_SLUG").strip()
    pr_id = os.getenv("PR_ID").strip()
    secret_name = os.getenv("SECRET_NAME").strip()
    region = os.getenv("REGION")

    if not workspace or not repo_slug:
        raise ValueError("BITBUCKET_WORKSPACE and BITBUCKET_REPO_SLUG are required")
    if not region:
        raise ValueError("AWS region is required")

    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
        secrets = json.loads(response["SecretString"])
    except Exception as err:
        raise RuntimeError(f"Unable to retrieve Secrets Manager secret: {secret_name}") from err

    repo_token = f"{repo_slug}_BITBUCKET_TOKEN"
    required_keys = ["CLAUDE_CODE_OAUTH_TOKEN"]

    for key in required_keys:
        if not isinstance(secrets.get(key), str) or not secrets[key].strip():
            raise ValueError(f"Secret is missing a valid {key} value")

    bitbucket_token = secrets[repo_token].strip()
    claude_oauth_token = secrets["CLAUDE_CODE_OAUTH_TOKEN"].strip()

    return [
        bitbucket_token,
        claude_oauth_token,
        workspace,
        repo_slug,
        pr_id,
    ]

def resolve_pr_id():
    """Resolve PR ID from manual inputs"""

    pr_id = os.getenv("PR_ID").strip()
    # validate pr contain only in digit
    if not pr_id.isdigit():
        raise ValueError(f"Invalid PR ID: {pr_id}")
    return pr_id

def get_pull_request(pr_id):
    """Fetch pull request metadata from Bitbucket API."""
    config = load_config()

    bitbucket_token = config[0]
    workspace = config[2]
    repo_slug = config[3]
    # Build the Bitbucket REST API URL
    resultant_pr = (f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}")

    # after printing the url next thing is authenticate and fetch PR metadata using requests
    headers = {
        "Authorization": f"Bearer {bitbucket_token}",
        "Accept": "application/json"
    }
    response = requests.get(resultant_pr, headers=headers, timeout=30)
    response.raise_for_status()

    pr_data = response.json()
    return pr_data

def git_diff(pr_data):
    """Fetch branches and calculate BASE_SHA and HEAD_SHA."""

    source_branch = pr_data["source"]["branch"]["name"]
    destination_branch = pr_data["destination"]["branch"]["name"]

    head_sha = pr_data["source"]["commit"]["hash"]
    base_sha = pr_data["destination"]["commit"]["hash"]

    print(f"Source branch: {source_branch}") 
    print(f"Destination branch: {destination_branch}") 
    print(f"HEAD_SHA: {head_sha}") 
    print(f"Destination SHA: {base_sha}")
    return base_sha, head_sha

def claude_review(head_sha,base_sha,pr_id):
    """Run Claude Code and save review-output/review.md."""

    # laod claude_oauth_token inside claude review function
    config = load_config()
    claude_oauth_token = config[1]

    # Select the repository directory based on the configured repository slug.
    repo_slug = os.environ["BITBUCKET_REPO_SLUG"]

    secondary_repos = {
        "xxxxxxxxxxxxx",
        "xxxxxxxxxxxxxxxx",
        "xxxxxxxxxxxx",
        "xxxxxxxx",
    }
    if repo_slug in secondary_repos:
        # codebuild replace hypens in secondary soruce identifiers with underscore because inside the codebuild directory created for secondary repos in this format CODEBUILD_SRC_DIR_sourceIdentifer (eg: CODEBUILD_SRC_DIR_events_scheduler)
        source_env = f"CODEBUILD_SRC_DIR_{repo_slug.replace('-', '_')}"
        repo_dir = os.environ.get(source_env)

        if not repo_dir:
            raise RuntimeError (f"CodeBuild source directory variable {source_env} was not found")
    else:
        repo_dir = os.environ["CODEBUILD_SRC_DIR"]

    print(f"Repository: {repo_slug}")
    print(f"Repository directory: {repo_dir}")

    # Generate the diff from the selected repository.
    result = subprocess.run(
        ["git", "diff", base_sha, head_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True
    )
    diff_content = result.stdout
    
    prompt = f"""
    Review the code changes in Pull Request #{pr_id}
    Looks for:
    - Bug and logic error
    - security vulnerabilities
    - performance issues
    - error handling problems

    Reports only actionable findings. If no issues are found, clearly state that.
    Code Changes: {diff_content}
    """

    # Passing the CLAUDE_CODE_OAUTH_TOKEN through subprocess as env because claude-cli is separate process it reads creds from environment vars, not from the variable set in script
    claude_token = os.environ.copy()
    claude_token["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth_token

    review_result = subprocess.run(
        ["claude", "-p"],
        capture_output=True,
        input=prompt,
        cwd=repo_dir,
        text=True,
        env=claude_token,
        timeout=600
    )
    if review_result.returncode != 0:
        print(f"Claude exit code: {review_result.returncode}")
        print(f"Claude stderr: {review_result.stderr}")
        print(f"Claude stdout: {review_result.stdout}")
        raise RuntimeError("Claude Code review failed")
    
    review_output = review_result.stdout

    # Now save that output
    os.makedirs("review-output", exist_ok=True)
    with open("review-output/review.md", "w", encoding="utf-8") as file:
        file.write(review_output)
    print(f"Review saved for PR #{pr_id}")

def post_review_comment(pr_id, review_markdown):
    """Post comment to the Bitbucket pull request."""

    config = load_config()
    bitbucket_token = config[0]
    workspace = config[2]
    repo_slug = config[3]

    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    )
    headers = {
        "Authorization": f"Bearer {bitbucket_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {
        "content": {
            "raw": review_markdown
        }
    }
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30
    )
    response.raise_for_status()
    print(f"Review comment posted successfully for PR #{pr_id}")

def main():

    pr_id = resolve_pr_id()
    pr_data = get_pull_request(pr_id)
    base_sha, head_sha = git_diff(pr_data)
    claude_review(head_sha, base_sha, pr_id)

    with open(
        "review-output/review.md",
        "r",
        encoding="utf-8"
    ) as file:
        review_markdown = file.read()

    if not review_markdown.strip():
        raise ValueError("Claude review output is empty")

    post_review_comment(pr_id, review_markdown)


if __name__ == "__main__":
    main()
