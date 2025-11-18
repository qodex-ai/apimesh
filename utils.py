import tiktoken
import subprocess
import os
import re

def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(string))

def get_repo_path() -> str:
    """
    Get the repository path from APIMESH_USER_REPO_PATH environment variable.
    
    Returns:
        Repository path as a string (assumes APIMESH_USER_REPO_PATH is always set).
    """
    repo_path = os.environ["APIMESH_USER_REPO_PATH"]
    return os.path.abspath(repo_path)

def get_repo_name() -> str:
    """
    Get the repository name from git remote URL.
    
    Returns:
        Repository name extracted from git remote URL, or basename of path if git remote is not available.
    """
    repo_path = get_repo_path()
    try:
        original_dir = os.getcwd()
        try:
            os.chdir(repo_path)
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
            os.chdir(original_dir)
        except Exception:
            os.chdir(original_dir)
            return os.path.basename(repo_path)
        
        if result.returncode == 0 and result.stdout:
            remote_url = result.stdout.strip()
            # Extract repo name from various git URL formats
            # SSH: git@github.com:owner/repo.git -> repo
            # HTTPS: https://github.com/owner/repo.git -> repo
            ssh_pattern = r'git@[^:]+:(?:[^/]+/)?([^/]+?)(?:\.git)?$'
            https_pattern = r'https?://(?:[^@]+@)?[^/]+/[^/]+/([^/]+?)(?:\.git)?$'
            
            ssh_match = re.match(ssh_pattern, remote_url)
            if ssh_match:
                return ssh_match.group(1)
            
            https_match = re.match(https_pattern, remote_url)
            if https_match:
                return https_match.group(1)
        
        # Fallback to basename if git remote doesn't match expected patterns
        return os.path.basename(repo_path)
    except Exception:
        # Fallback to basename if any error occurs
        return os.path.basename(repo_path)

def format_repo_name(repo_name: str) -> str:
    """
    Format repository name for display.
    Converts snake_case, kebab-case, or camelCase to Title Case with spaces.
    
    Examples:
        sample_rails_app -> Sample Rails App
        sample-rails-app -> Sample Rails App
        sampleRailsApp -> Sample Rails App
    
    Args:
        repo_name: Raw repository name
    
    Returns:
        Formatted repository name in Title Case
    """
    # Replace underscores and hyphens with spaces
    formatted = repo_name.replace('_', ' ').replace('-', ' ')
    
    # Insert spaces before capital letters (for camelCase)
    formatted = re.sub(r'([a-z])([A-Z])', r'\1 \2', formatted)
    
    # Convert to title case (capitalize first letter of each word)
    formatted = formatted.title()
    
    return formatted

def get_output_filepath() -> str:
    """
    Get the output filepath from APIMESH_OUTPUT_FILEPATH environment variable.
    If not set, defaults to {repo_path}/apimesh/swagger.json
    
    Returns:
        Output filepath as a string.
    """
    output_filepath = os.environ.get("APIMESH_OUTPUT_FILEPATH")
    if output_filepath:
        return os.path.abspath(output_filepath)
    # Default to repo_path/apimesh/swagger.json
    repo_path = get_repo_path()
    default_path = os.path.join(repo_path, "apimesh", "swagger.json")
    return os.path.abspath(default_path)

def get_github_repo_url() -> str:
    """
    Get the GitHub repository URL from git remote.
    Uses APIMESH_USER_REPO_PATH environment variable to determine the repository path.
    
    Returns:
        GitHub repository URL (e.g., "https://github.com/owner/repo") or empty string if not available.
    """
    try:
        repo_path = get_repo_path()
        original_dir = os.getcwd()
        try:
            os.chdir(repo_path)
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
            os.chdir(original_dir)
        except Exception:
            os.chdir(original_dir)
            return ""
        
        if result.returncode == 0 and result.stdout:
            remote_url = result.stdout.strip()
            # Convert SSH format (git@github.com:owner/repo.git) to HTTPS format
            # or extract from HTTPS format (https://github.com/owner/repo.git)
            ssh_pattern = r'git@github\.com:(.+?)(?:\.git)?$'
            https_pattern = r'https?://(?:www\.)?github\.com/(.+?)(?:\.git)?$'
            
            ssh_match = re.match(ssh_pattern, remote_url)
            if ssh_match:
                owner_repo = ssh_match.group(1)
                return f"https://github.com/{owner_repo}"
            
            https_match = re.match(https_pattern, remote_url)
            if https_match:
                owner_repo = https_match.group(1)
                return f"https://github.com/{owner_repo}"
            
            # Return as-is if it doesn't match GitHub patterns
            return remote_url
        
        return ""
    except Exception:
        return ""

def get_git_commit_hash() -> str:
    """
    Get the current git commit hash for the repository.
    Uses APIMESH_USER_REPO_PATH environment variable to determine the repository path.
    
    Returns:
        Git commit hash as a string, or empty string if not available.
    """
    try:
        repo_path = get_repo_path()
        # Change to repo directory for git command
        original_dir = os.getcwd()
        try:
            os.chdir(repo_path)
            result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
            os.chdir(original_dir)
        except Exception:
            os.chdir(original_dir)
            return ""
        
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
        return ""
    except Exception:
        return ""