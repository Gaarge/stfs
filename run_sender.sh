#!/usr/bin/env bash
# Bootstrap and run the Telegram sender on Debian, Ubuntu, Arch or Nyarch.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  cat <<'EOF'
Usage: ./run_sender.sh

Starts the Telegram sender interactively. The script asks for the number of
sender accounts and runs sender1 through senderN. Missing dependencies are
installed only when needed.
EOF
  exit 0
fi
if (($#)); then
  echo "This launcher has no command-line options. Run ./run_sender.sh or ./run_sender.sh --help." >&2
  exit 2
fi

run_as_root() {
  if [[ ${EUID} -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Root access is required to install missing system packages, but sudo is unavailable." >&2
    exit 1
  fi
}

install_missing_system_packages() {
  local need_python="$1"
  local need_venv="$2"
  local need_ffmpeg="$3"
  local -a packages=()

  if command -v apt-get >/dev/null 2>&1; then
    [[ "$need_python" == "1" ]] && packages+=(python3 python3-venv)
    [[ "$need_venv" == "1" ]] && packages+=(python3-venv)
    [[ "$need_ffmpeg" == "1" ]] && packages+=(ffmpeg)
    if ((${#packages[@]})); then
      echo "Installing missing system packages: ${packages[*]}"
      run_as_root apt-get update
      run_as_root apt-get install -y "${packages[@]}"
    fi
    return
  fi

  if command -v pacman >/dev/null 2>&1; then
    [[ "$need_python" == "1" ]] && packages+=(python)
    [[ "$need_ffmpeg" == "1" ]] && packages+=(ffmpeg)
    # Arch/Nyarch ships venv with the python package, so need_venv needs no
    # separate package.
    if ((${#packages[@]})); then
      echo "Installing missing system packages: ${packages[*]}"
      run_as_root pacman -Sy --needed --noconfirm "${packages[@]}"
    fi
    return
  fi

  echo "Unsupported Linux package manager. Install Python 3.10+, python venv support and ffmpeg manually." >&2
  exit 1
}

cd "$PROJECT_DIR"

PYTHON_COMMAND=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_COMMAND="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_COMMAND="python"
fi

need_python=0
need_venv=0
need_ffmpeg=0
if [[ -z "$PYTHON_COMMAND" ]]; then
  need_python=1
else
  if ! "$PYTHON_COMMAND" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    need_venv=1
  fi
fi
if ! command -v ffprobe >/dev/null 2>&1; then
  need_ffmpeg=1
fi

if ((need_python || need_venv || need_ffmpeg)); then
  install_missing_system_packages "$need_python" "$need_venv" "$need_ffmpeg"
fi

if [[ -z "$PYTHON_COMMAND" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_COMMAND="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_COMMAND="python"
  else
    echo "Python was not installed successfully." >&2
    exit 1
  fi
fi

"$PYTHON_COMMAND" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")
PY

if [[ ! -x "$VENV_PYTHON" ]]; then
  if [[ -e "$VENV_DIR" ]]; then
    echo "Existing $VENV_DIR is incomplete. Restore or remove it manually, then run this script again." >&2
    exit 1
  fi
  echo "Creating local Python environment in .venv…"
  "$PYTHON_COMMAND" -m venv "$VENV_DIR"
fi

if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  "$VENV_PYTHON" -m ensurepip --upgrade
fi

if ! "$VENV_PYTHON" -c 'import telethon, dotenv' >/dev/null 2>&1; then
  echo "Installing missing Python dependencies…"
  "$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

if [[ ! -f "$PROJECT_DIR/opros/message.txt" || ! -f "$PROJECT_DIR/prev.jpg" ]]; then
  echo "message.txt or prev.jpg is missing. Restore these project files before sending." >&2
  exit 1
fi

video_file="${PROMO_VIDEO_FILE:-}"
if [[ -z "$video_file" ]]; then
  for candidate in \
    "$PROJECT_DIR/промо_итог.mp4" \
    "/home/garg/Загрузки/промо_итог.mp4" \
    "$HOME/Загрузки/промо_итог.mp4" \
    "$HOME/Downloads/промо_итог.mp4"; do
    if [[ -f "$candidate" ]]; then
      video_file="$candidate"
      break
    fi
  done
fi
while [[ ! -f "$video_file" ]]; do
  read -r -p "Path to промо_итог.mp4: " video_file
  [[ -n "$video_file" ]] || continue
  video_file="${video_file/#\~/$HOME}"
done

account_count=""
while [[ ! "$account_count" =~ ^[1-9][0-9]*$ ]]; do
  read -r -p "How many Telegram accounts should send now? [1]: " account_count
  account_count="${account_count:-1}"
  if [[ ! "$account_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "Enter a positive whole number, for example 1, 2 or 5."
  fi
done

accounts=()
for ((number = 1; number <= account_count; number++)); do
  accounts+=("sender${number}")
done
account_list="$(IFS=,; echo "${accounts[*]}")"

echo
echo "Starting senders: $account_list"
echo "Video: $video_file"
echo "The sender will ask for Telegram QR login for any account without a saved session."
exec "$VENV_PYTHON" "$PROJECT_DIR/opros/send_queue.py" \
  --accounts "$account_list" \
  --video-file "$video_file" \
  --video-thumbnail "$PROJECT_DIR/prev.jpg"
