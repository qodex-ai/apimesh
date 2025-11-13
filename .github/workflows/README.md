# GitHub Actions Workflows

## Docker Build Workflow

This workflow automatically builds and pushes Docker images to Docker Hub when tags are pushed to the repository.

### How it works:

1. **Trigger**: Automatically runs when you push a tag matching pattern `v*.*.*` (e.g., `v1.0.0`, `v2.1.3`)
2. **Build**: Builds the Docker image using the Dockerfile
3. **Tag**: Tags the image with:
   - The full tag name (e.g., `v1.0.0`)
   - `latest` (always updated to the newest tag)
4. **Push**: Pushes all tags to Docker Hub

### Setup Instructions:

1. **Create a Docker Hub token**:
   - Go to Docker Hub → Account Settings → Security
   - Click "New Access Token"
   - Give it a name (e.g., "github-actions")
   - Copy the token

2. **Add the token to GitHub Secrets**:
   - Go to your GitHub repository → Settings → Secrets and variables → Actions
   - Click "New repository secret"
   - Name: `DOCKER_HUB_TOKEN`
   - Value: Paste your Docker Hub token
   - Click "Add secret"

3. **Create and push a tag**:
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

### Version Tagging:

- Tag format: `v1.0.0`, `v2.1.3`, etc.
- Images will be tagged as:
  - `qodexai/apimesh:v1.0.0` (full tag)
  - `qodexai/apimesh:latest` (always points to newest)

### Manual Trigger:

You can also manually trigger the workflow from the Actions tab in GitHub.

