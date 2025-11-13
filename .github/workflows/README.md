# GitHub Actions Workflows

## Docker Build Workflow

This workflow automatically builds and pushes Docker images to Docker Hub with auto-incrementing versions.

### How it works:

1. **Triggers**:
   - **Tag push**: When you push a tag matching `v*.*.*` (e.g., `v1.0.0`), it uses that exact version
   - **Main branch push**: Automatically increments the patch version and creates a new tag
   - **Manual dispatch**: Allows you to choose patch/minor/major increment

2. **Auto-incrementing**: 
   - Finds the latest semantic version tag (e.g., `v1.2.3`)
   - Increments based on trigger type:
     - Branch push → patch increment (1.2.3 → 1.2.4)
     - Manual dispatch → your choice (patch/minor/major)
   - Creates a new git tag automatically

3. **Build**: Builds the Docker image using the Dockerfile

4. **Tag**: Tags the image with multiple versions:
   - Full tag: `v1.2.4`
   - Version only: `1.2.4`
   - With build number: `1.2.4-build.123`
   - Short commit SHA: `abc1234`
   - `latest` (always updated to the newest)

5. **Push**: Pushes all tags to Docker Hub

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

3. **Usage**:

   **Option A: Auto-increment on main branch push**
   ```bash
   # Just push to main branch - version auto-increments
   git push origin main
   ```

   **Option B: Manual version tag**
   ```bash
   # Create a specific version tag
   git tag v1.0.0
   git push origin v1.0.0
   ```

   **Option C: Manual workflow dispatch**
   - Go to Actions → Build and Push Docker Image → Run workflow
   - Choose version increment type (patch/minor/major)
   - Click "Run workflow"

### Version Tagging:

- **Auto-increment**: Latest tag `v1.2.3` → new version `v1.2.4` (patch), `v1.3.0` (minor), or `v2.0.0` (major)
- **Images will be tagged as**:
  - `qodexai/apimesh:v1.2.4` (full tag)
  - `qodexai/apimesh:1.2.4` (version only)
  - `qodexai/apimesh:1.2.4-build.123` (with build number)
  - `qodexai/apimesh:abc1234` (short commit SHA)
  - `qodexai/apimesh:latest` (always points to newest)

### Examples:

- **First build**: Starts at `v0.0.0`, increments to `v0.0.1`
- **Patch increment**: `v1.2.3` → `v1.2.4`
- **Minor increment**: `v1.2.3` → `v1.3.0` (via manual dispatch)
- **Major increment**: `v1.2.3` → `v2.0.0` (via manual dispatch)

