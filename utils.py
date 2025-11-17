import tiktoken
import subprocess
import os

def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(string))

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