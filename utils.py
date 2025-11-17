import tiktoken
import subprocess
import os
import re

def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(string))

def get_github_repo_url(repo_path: str = None) -> str:
    """
    Get the GitHub repository URL from git remote.
    
    Args:
        repo_path: Path to the repository. If None, uses current directory.
    
    Returns:
        GitHub repository URL (e.g., "https://github.com/owner/repo") or empty string if not available.
    """
    try:
        if repo_path:
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
        else:
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
        
        if result.returncode == 0 and result.stdout:
            remote_url = result.stdout.strip()
            # Convert SSH format (git@github.com:owner/repo.git) to HTTPS format
            # or extract from HTTPS format (https://github.com/owner/repo.git)
            ssh_pattern = r'git@github\.com:(.+?)(?:\.git)?$'
            https_pattern = r'https?://(?:www\.)?github\.com/(.+?)(?:\.git)?$'
            
            ssh_match = re.match(ssh_pattern, remote_url)
            if ssh_match:
                repo_path = ssh_match.group(1)
                return f"https://github.com/{repo_path}"
            
            https_match = re.match(https_pattern, remote_url)
            if https_match:
                repo_path = https_match.group(1)
                return f"https://github.com/{repo_path}"
            
            # Return as-is if it doesn't match GitHub patterns
            return remote_url
        
        return ""
    except Exception:
        return ""

def get_git_commit_hash(repo_path: str = None) -> str:
    """
    Get the current git commit hash for the repository.
    
    Args:
        repo_path: Path to the repository. If None, uses current directory.
    
    Returns:
        Git commit hash as a string, or empty string if not available.
    """
    try:
        if repo_path:
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
        else:
            result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
        
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
        return ""
    except Exception:
        return ""