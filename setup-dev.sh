#!/usr/bin/env bash
# setup-dev.sh: Initializes local Git, sets remote origin, and registers pre-push hook

echo "Initializing developer environment for MeshHub..."

# Initialize git repository if not already done
if [ ! -d ".git" ]; then
  git init
  echo "Initialized empty Git repository."
else
  echo "Git repository is already initialized."
fi

# Set remote origin to Dhovin repository
REMOTE_URL="https://github.com/Dhovin/MeshHub.git"

if git remote | grep -q "^origin$"; then
  echo "Updating existing remote origin URL to: ${REMOTE_URL}"
  git remote set-url origin "${REMOTE_URL}"
else
  echo "Adding remote origin URL: ${REMOTE_URL}"
  git remote add origin "${REMOTE_URL}"
fi

# Register pre-push git hook
HOOK_PATH=".git/hooks/pre-push"
echo "Registering Git pre-push validation hook..."
mkdir -p .git/hooks
cat > "${HOOK_PATH}" <<'EOF'
#!/bin/sh
# Run pre-push validation script before allowing git push
python3 scripts/pre_push.py
EOF

chmod +x "${HOOK_PATH}"
echo "Pre-push hook successfully registered at ${HOOK_PATH}."

echo "Git configuration completed successfully."
echo "Remote origin set to: ${REMOTE_URL}"
