#!/usr/bin/env bash
# install.sh: Linux Installer for MeshHub Central Hub

set -e # Exit immediately on error

echo "=================================================="
echo "Starting MeshHub Linux Installation Script"
echo "=================================================="

# Helper function to check package manager
detect_package_manager() {
  if command -v apt-get &> /dev/null; then
    echo "apt"
  elif command -v dnf &> /dev/null; then
    echo "dnf"
  elif command -v pacman &> /dev/null; then
    echo "pacman"
  else
    echo "unknown"
  fi
}

PKG_MGR=$(detect_package_manager)

# 1. System Prerequisites & Architecture Checks
echo "[Install] Checking system prerequisites..."

ARCH=$(uname -m)
echo "[Install] Detected CPU Architecture: $ARCH"

# Detect single-board computer / hardware model if devicetree is present
BOARD_MODEL=""
if [ -f "/proc/device-tree/model" ]; then
  BOARD_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
elif [ -f "/sys/firmware/devicetree/base/model" ]; then
  BOARD_MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model 2>/dev/null || true)
fi

if [ -n "$BOARD_MODEL" ]; then
  echo "[Install] Detected Hardware Model: $BOARD_MODEL"
  if echo "$BOARD_MODEL" | grep -qi "odroid-xu"; then
    echo "--------------------------------------------------"
    echo "[Notice] ODROID-XU3/XU4 hardware detected:"
    echo "  * Expansion header GPIO UART uses 1.8V logic."
    echo "    Connecting 3.3V/5V UART directly will damage the board."
    echo "    Use USB connection or an active 1.8V <-> 3.3V level shifter."
    echo "  * ODROID-XU4 has no onboard BLE/Wi-Fi. If using BLE,"
    echo "    an external USB Bluetooth 4.0+ adapter is required."
    echo "  * Recommended: Connect companion radio to USB 2.0 port"
    echo "    or powered USB hub for optimal power stability."
    echo "--------------------------------------------------"
  fi
fi

# Check available memory and warn if low RAM without swap
TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
TOTAL_SWAP_KB=$(grep SwapTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
if [ "$TOTAL_MEM_KB" -gt 0 ] && [ "$TOTAL_MEM_KB" -lt 2500000 ] && [ "$TOTAL_SWAP_KB" -lt 500000 ]; then
  echo "[Notice] Low memory system detected (<= 2GB RAM) without swap."
  echo "         If compiling Python C-extensions runs out of memory, consider adding a swapfile."
fi

# Python 3.10+ Check
if ! command -v python3 &> /dev/null; then
  echo "[Error] Python 3 is missing. Python 3.10+ is required."
  exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
  echo "[Error] Python 3.10+ is required. Found Python $PYTHON_VERSION"
  exit 1
else
  echo "[Install] Python 3.10+ is verified. Found Python $PYTHON_VERSION"
fi

# 2. Package installation for virtual environment, build tools, & C-extension dependencies
echo "[Install] Installing system build dependencies, virtualenv, and BlueZ..."
if [ "$PKG_MGR" = "apt" ]; then
  sudo apt-get update
  # libffi-dev, libssl-dev, libsodium-dev, python3-nacl, and pkg-config are required to provide or build pynacl on ARM (e.g. armv7l / ODROID)
  sudo apt-get install -y python3-venv python3-pip python3-dev build-essential libffi-dev libssl-dev libsodium-dev python3-nacl pkg-config git bluez
elif [ "$PKG_MGR" = "dnf" ]; then
  sudo dnf install -y python3-pip python3-virtualenv python3-devel development-tools libffi-devel openssl-devel libsodium-devel python3-pynacl pkgconf-pkg-config git bluez
elif [ "$PKG_MGR" = "pacman" ]; then
  sudo pacman -Syu --noconfirm python-pip python-virtualenv base-devel libffi openssl libsodium python-pynacl pkgconf git bluez
else
  echo "[Install] Non-standard package manager. Assuming build dependencies and python3-venv are present."
fi

# Ensure bluetooth service is active if available
if command -v systemctl &> /dev/null && systemctl list-unit-files 2>/dev/null | grep -q "bluetooth.service"; then
  sudo systemctl enable --now bluetooth 2>/dev/null || true
fi

# 3. Setup Project Virtual Environment
# Detect repository directory
if ([ -f "bin/meshhub" ] || [ -f "bin/meshbot" ]) && [ -d "core" ]; then
  REPO_DIR=$(pwd)
else
  echo "[Install] Standalone execution detected (not running in repository directory)."
  if [ -d "${HOME}/MeshHub" ]; then
    INSTALL_DIR="${HOME}/MeshHub"
  elif [ -d "${HOME}/Meshcore-bot" ]; then
    INSTALL_DIR="${HOME}/Meshcore-bot"
  else
    INSTALL_DIR="${HOME}/MeshHub"
  fi
  
  if [ -d "$INSTALL_DIR" ]; then
    echo "[Install] Existing directory found at ${INSTALL_DIR}. Updating repository..."
    cd "$INSTALL_DIR"
    git pull
  else
    echo "[Install] Cloning MeshHub repository into ${INSTALL_DIR}..."
    git clone https://github.com/Dhovin/Meshcore-bot.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi
  REPO_DIR=$(pwd)
fi

VENV_DIR="${REPO_DIR}/venv"

echo "[Install] Creating Python virtual environment in ${VENV_DIR}..."
python3 -m venv --system-site-packages "${VENV_DIR}"

echo "[Install] Upgrading pip, setuptools, wheel, and cffi in virtual environment..."
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel cffi

echo "[Install] Installing Python libraries (pyserial, bleak, meshcore, meshcore-cli, paho-mqtt, pynacl, requests, aiohttp)..."
# SODIUM_INSTALL=system instructs PyNaCl to link against system libsodium rather than building bundled source
export SODIUM_INSTALL=system
"${VENV_DIR}/bin/pip" install pyserial bleak meshcore meshcore-cli paho-mqtt requests aiohttp
if ! SODIUM_INSTALL=system "${VENV_DIR}/bin/pip" install pynacl; then
  echo "[Install Warning] Standard pynacl install failed. Attempting fallback with --no-build-isolation..."
  if ! SODIUM_INSTALL=system "${VENV_DIR}/bin/pip" install --no-build-isolation pynacl; then
    echo "[Install Warning] Could not compile PyNaCl. Local offline Ed25519 signing will fall back to on-device signing."
  fi
fi

# 4. Setup Project Configuration
CONFIG_FILE="${REPO_DIR}/config/config.json"
TEMPLATE_FILE="${REPO_DIR}/config/config.json.template"

# Create a backup template config if not exists
if [ ! -f "$TEMPLATE_FILE" ] && [ -f "$CONFIG_FILE" ]; then
  cp "$CONFIG_FILE" "$TEMPLATE_FILE"
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "[Install] Copying template configuration to config.json..."
  if [ -f "$TEMPLATE_FILE" ]; then
    cp "$TEMPLATE_FILE" "$CONFIG_FILE"
  else
    # Fallback default configuration
    mkdir -p "${REPO_DIR}/config"
    cat > "$CONFIG_FILE" <<EOF
{
  "connection": {
    "type": "auto",
    "address": "",
    "port": "",
    "baudrate": 115200,
    "host": "127.0.0.1",
    "tcpPort": 5000
  },
  "core": {
    "timeSyncInterval": "0 0 * * *",
    "shutdownTimeoutMs": 10000
  },
  "modules": {
    "template": {
      "enabled": true,
      "messagePrefix": "[MeshHub]",
      "logChannel": 0
    }
  }
}
EOF
  fi
else
  echo "[Install] Existing config.json found. Keeping original settings."
fi

# 5. Create global shell wrapper runners
echo "[Install] Deploying global CLI runner wrappers to /usr/local/bin/meshhub and /usr/local/bin/meshbot..."
WRAPPER_PATH="/usr/local/bin/meshhub"
LEGACY_WRAPPER_PATH="/usr/local/bin/meshbot"

CLI_SCRIPT="${REPO_DIR}/bin/meshhub"
if [ ! -f "$CLI_SCRIPT" ]; then
  CLI_SCRIPT="${REPO_DIR}/bin/meshbot"
fi

sudo bash -c "cat > ${WRAPPER_PATH}" <<EOF
#!/bin/sh
# Shell wrapper routing meshhub command calls to the virtual environment
exec "${VENV_DIR}/bin/python" "${CLI_SCRIPT}" "\$@"
EOF

sudo chmod +x "${WRAPPER_PATH}"
sudo ln -sf "${WRAPPER_PATH}" "${LEGACY_WRAPPER_PATH}"
echo "[Install] CLI wrappers successfully created at ${WRAPPER_PATH} and ${LEGACY_WRAPPER_PATH}."

# 6. Generate systemd Service file
echo "[Install] Generating systemd service unit file..."
SERVICE_PATH="/etc/systemd/system/meshhub.service"

sudo bash -c "cat > ${SERVICE_PATH}" <<EOF
[Unit]
Description=MeshHub Central Hub Daemon
After=network.target
Alias=meshcore-bot.service

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${WRAPPER_PATH} start-daemon
Restart=on-failure
SupplementaryGroups=dialout tty

[Install]
WantedBy=multi-user.target
EOF

# 7. Reload systemd, enable and start service
echo "[Install] Enabling and starting systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable meshhub.service
sudo systemctl start meshhub.service

# 8. Grant serial port permissions to non-root user
TARGET_USER="${SUDO_USER:-$USER}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  echo "[Install] Ensuring user '$TARGET_USER' has access to serial devices (dialout, tty)..."
  sudo usermod -aG dialout,tty "$TARGET_USER" 2>/dev/null || true
fi

echo "=================================================="
echo "MeshHub Installation Complete!"
echo "=================================================="
echo "Verification instructions:"
echo "1. Check service status: sudo systemctl status meshhub"
echo "2. View logs: sudo journalctl -u meshhub -f"
echo "3. Run config wizard: meshhub config"
echo "4. Open Web Viewer dashboard: http://<device-ip>:8080"
echo "5. Check status: meshhub status (or meshbot status)"
echo "=================================================="
