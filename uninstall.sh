#!/usr/bin/env bash
# uninstall.sh: Linux Uninstaller for MeshHub Central Hub

set -e # Exit immediately on error

echo "=================================================="
echo "Starting MeshHub Linux Uninstallation Script"
echo "=================================================="

# 1. Stop and disable systemd service
for SVC in "meshhub.service" "meshcore-bot.service"; do
  if systemctl is-active --quiet "$SVC" 2>/dev/null; then
    echo "[Uninstall] Stopping $SVC..."
    sudo systemctl stop "$SVC" || true
  fi
  if systemctl is-enabled --quiet "$SVC" 2>/dev/null; then
    echo "[Uninstall] Disabling $SVC..."
    sudo systemctl disable "$SVC" || true
  fi
done

# 2. Remove systemd service files
for SVC_FILE in "/etc/systemd/system/meshhub.service" "/etc/systemd/system/meshcore-bot.service"; do
  if [ -f "$SVC_FILE" ]; then
    echo "[Uninstall] Removing systemd service unit file: $SVC_FILE..."
    sudo rm -f "$SVC_FILE"
  fi
done
sudo systemctl daemon-reload || true

# 3. Remove global CLI wrappers
for WRAPPER in "/usr/local/bin/meshhub" "/usr/local/bin/meshbot"; do
  if [ -f "$WRAPPER" ] || [ -L "$WRAPPER" ]; then
    echo "[Uninstall] Removing global CLI wrapper: $WRAPPER..."
    sudo rm -f "$WRAPPER"
  fi
done

# 4. Remove project virtual environment
# Detect repository directory
if ([ -f "bin/meshhub" ] || [ -f "bin/meshbot" ]) && [ -d "core" ]; then
  REPO_DIR=$(pwd)
elif [ -d "${HOME}/MeshHub" ] && ([ -f "${HOME}/MeshHub/bin/meshhub" ] || [ -f "${HOME}/MeshHub/bin/meshbot" ]); then
  REPO_DIR="${HOME}/MeshHub"
elif [ -d "${HOME}/Meshcore-bot" ] && ([ -f "${HOME}/Meshcore-bot/bin/meshhub" ] || [ -f "${HOME}/Meshcore-bot/bin/meshbot" ]); then
  REPO_DIR="${HOME}/Meshcore-bot"
else
  REPO_DIR=""
fi

if [ -n "$REPO_DIR" ]; then
  VENV_DIR="${REPO_DIR}/venv"
  if [ -d "$VENV_DIR" ]; then
    echo "[Uninstall] Removing python virtual environment..."
    rm -rf "$VENV_DIR"
  fi
fi

# 5. Clean up process lockfiles
if [ -n "$REPO_DIR" ]; then
  for PF in "${REPO_DIR}/config/meshhub.pid" "${REPO_DIR}/config/meshbot.pid"; do
    if [ -f "$PF" ]; then
      echo "[Uninstall] Cleaning process lockfile: $PF..."
      rm -f "$PF"
    fi
  done
fi

# 6. Prompt to clean up configurations
if [ -n "$REPO_DIR" ]; then
  echo "--------------------------------------------------"
  read -p "Do you want to clean up your config.json and bot configurations? [y/N]: " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "[Uninstall] Cleaning up configurations..."
    rm -f "${REPO_DIR}/config/config.json"
    echo "Configuration files removed."
  else
    echo "[Uninstall] Configuration files preserved."
  fi
fi

# 7. Optionally remove repo directory itself
if [ -n "$REPO_DIR" ] && ([ "$REPO_DIR" = "${HOME}/MeshHub" ] || [ "$REPO_DIR" = "${HOME}/Meshcore-bot" ]); then
  echo "--------------------------------------------------"
  read -p "Do you want to completely remove the repository directory ${REPO_DIR}? [y/N]: " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "[Uninstall] Removing repository directory..."
    rm -rf "$REPO_DIR"
    echo "Repository directory removed."
  fi
fi

echo "=================================================="
echo "MeshHub has been successfully uninstalled."
echo "=================================================="
