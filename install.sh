#!/bin/sh
# Install the latest udbi executable:
#   curl -fsSL https://raw.githubusercontent.com/Handara/udb-inspector/main/install.sh | sh
# It goes to ~/.local/bin, or $UDBI_INSTALL_DIR if set.
set -eu

repo="Handara/udb-inspector"
dir="${UDBI_INSTALL_DIR:-$HOME/.local/bin}"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "udbi: no ready-made executable for $(uname -s). See https://github.com/$repo#install" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "udbi: no ready-made executable for $(uname -m). See https://github.com/$repo#install" >&2; exit 1 ;;
esac
if [ "$os" = macos ] && [ "$arch" = x86_64 ]; then
  echo "udbi: no ready-made executable for Intel Macs yet; install it with pipx instead. See https://github.com/$repo#install" >&2
  exit 1
fi

url="https://github.com/$repo/releases/latest/download/udbi-$os-$arch"
mkdir -p "$dir"
# Download beside the old one and rename over it, so a running udbi isn't disturbed.
staged="$dir/.udbi.download"
echo "Downloading $url"
curl -fL --progress-bar "$url" -o "$staged"
chmod +x "$staged"
mv "$staged" "$dir/udbi"
echo "Installed $dir/udbi"

case ":$PATH:" in
  *":$dir:"*) ;;
  *) echo "Add $dir to your PATH to run udbi from anywhere." ;;
esac
