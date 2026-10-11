#!/bin/sh
# Install or update udbi:
#   curl -fsSL https://raw.githubusercontent.com/Handara/udb-inspector/main/install.sh | sh
# The program goes to ~/.local/share/udbi and the udbi command to ~/.local/bin
# (set UDBI_INSTALL_DIR to put the command somewhere else).
set -eu

repo="Handara/udb-inspector"
bin="${UDBI_INSTALL_DIR:-$HOME/.local/bin}"
share="${XDG_DATA_HOME:-$HOME/.local/share}"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "udbi: no ready-made build for $(uname -s). See https://github.com/$repo#install" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "udbi: no ready-made build for $(uname -m). See https://github.com/$repo#install" >&2; exit 1 ;;
esac
if [ "$os" = macos ] && [ "$arch" = x86_64 ]; then
  echo "udbi: no ready-made build for Intel Macs yet; install it with pipx instead. See https://github.com/$repo#install" >&2
  exit 1
fi

url="${UDBI_URL:-https://github.com/$repo/releases/latest/download/udbi-$os-$arch.tar.gz}"
mkdir -p "$bin" "$share"
staged="$share/udbi.new"
old="$share/udbi.old"
rm -rf "$staged" "$old"
mkdir "$staged"
echo "Downloading $url"
curl -fL --progress-bar "$url" | tar -xz -C "$staged" --strip-components=1

# Swap the new folder in; a udbi that's running keeps the files it has open.
if [ -d "$share/udbi" ]; then mv "$share/udbi" "$old"; fi
mv "$staged" "$share/udbi"
rm -rf "$old"
# Link the command, replacing an older single-file udbi if there is one.
ln -sf "$share/udbi/udbi" "$bin/.udbi.link"
mv -f "$bin/.udbi.link" "$bin/udbi"
echo "Installed $share/udbi, linked from $bin/udbi"

case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "Add $bin to your PATH to run udbi from anywhere." ;;
esac
